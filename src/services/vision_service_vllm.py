import os
import math
from typing import Any, Dict, List, Optional

from loguru import logger
from openai import APIConnectionError, APIStatusError

from src.config.config import VLLM_API_KEY, VLLM_BASE_URL, VLLM_BASE_URLS
from src.services.vision_service_openai_compatible import (
    OpenAICompatibleClientPool,
    prepare_vision_request,
    vision_completion_openai_compatible,
)
from src.services.vision_capacity import EndpointScheduler

DEFAULT_VISION_MODEL = "Qwen/Qwen3-VL-30B-A3B-Instruct-FP8"
_FALLBACK_API_KEY = "not-required"
_ENABLE_THINKING_ENV = "VLLM_ENABLE_THINKING"
_TEMPERATURE_ENV = "VLLM_VISION_TEMPERATURE"
_TOP_P_ENV = "VLLM_VISION_TOP_P"
_TOP_K_ENV = "VLLM_VISION_TOP_K"
_MIN_P_ENV = "VLLM_VISION_MIN_P"
_PRESENCE_PENALTY_ENV = "VLLM_VISION_PRESENCE_PENALTY"
_REPETITION_PENALTY_ENV = "VLLM_VISION_REPETITION_PENALTY"

_DEFAULT_TEMPERATURE = 1.0
_DEFAULT_TOP_P = 1.0
_DEFAULT_TOP_K = 40
_DEFAULT_MIN_P = 0.0
_DEFAULT_PRESENCE_PENALTY = 2.0
_DEFAULT_REPETITION_PENALTY = 1.0


def _resolve_api_key() -> str:
    env_override = os.getenv("VLLM_API_KEY")
    if env_override:
        return env_override
    if VLLM_API_KEY:
        return VLLM_API_KEY
    return ""


def _parse_base_urls(raw_value: Optional[str]) -> List[str]:
    if not raw_value:
        return []
    parts = [item.strip() for item in raw_value.split(",")]
    return [item for item in parts if item]


def _resolve_base_urls() -> List[str]:
    env_override = os.getenv("VLLM_BASE_URLS") or os.getenv("VLLM_BASE_URL")
    urls = _parse_base_urls(env_override)
    if urls:
        return urls
    urls = _parse_base_urls(VLLM_BASE_URLS)
    if urls:
        return urls
    return _parse_base_urls(VLLM_BASE_URL)


def has_vllm_credentials() -> bool:
    return bool(_resolve_base_urls())


def _env_enable_thinking() -> bool:
    raw_value = os.getenv(_ENABLE_THINKING_ENV)
    if raw_value is None:
        return False
    value = raw_value.strip().lower()
    if value not in {"1", "true", "yes", "on", "0", "false", "no", "off"}:
        raise ValueError(f"Invalid {_ENABLE_THINKING_ENV}: expected a boolean")
    return value in {"1", "true", "yes", "on"}


def _env_float(
    var_name: str, default: float, low: float, high: float, *, exclusive_low=False
) -> float:
    raw_value = os.getenv(var_name)
    if raw_value is None:
        return default
    try:
        value = float(raw_value.strip())
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid {var_name}: expected a finite number") from exc
    if not math.isfinite(value) or value < low or value > high or (exclusive_low and value == low):
        raise ValueError(f"Invalid {var_name}: outside supported range")
    return value


def _env_positive_int(var_name: str, default: int) -> int:
    raw_value = os.getenv(var_name)
    if raw_value is None:
        return default
    try:
        parsed = int(raw_value.strip())
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid {var_name}: expected an integer") from exc
    if parsed <= 0 and parsed != -1:
        raise ValueError(f"Invalid {var_name}: expected -1 or a positive integer")
    return parsed


def _build_request_options() -> Dict[str, float]:
    return {
        "temperature": _env_float(_TEMPERATURE_ENV, _DEFAULT_TEMPERATURE, 0, 2),
        "top_p": _env_float(_TOP_P_ENV, _DEFAULT_TOP_P, 0, 1, exclusive_low=True),
        "presence_penalty": _env_float(_PRESENCE_PENALTY_ENV, _DEFAULT_PRESENCE_PENALTY, -2, 2),
    }


def _build_extra_body() -> Dict[str, Any]:
    return {
        "top_k": _env_positive_int(_TOP_K_ENV, _DEFAULT_TOP_K),
        "min_p": _env_float(_MIN_P_ENV, _DEFAULT_MIN_P, 0, 1),
        "repetition_penalty": _env_float(
            _REPETITION_PENALTY_ENV,
            _DEFAULT_REPETITION_PENALTY,
            0,
            float("inf"),
            exclusive_low=True,
        ),
        "chat_template_kwargs": {"enable_thinking": _env_enable_thinking()},
    }


_RESOLVED_BASE_URLS = _resolve_base_urls()


def _client_budgets() -> dict:
    timeout = float(os.getenv("VLLM_VISION_TIMEOUT_SECONDS", "180"))
    retries = int(os.getenv("VLLM_VISION_MAX_RETRIES", "0"))
    if not 0 < timeout < float("inf") or retries < 0:
        raise ValueError("Vision timeout must be finite and positive; retries must be nonnegative")
    return {"timeout": timeout, "max_retries": retries}


_CLIENT_POOL = OpenAICompatibleClientPool(
    api_key=_resolve_api_key() if _RESOLVED_BASE_URLS else "",
    base_urls=_RESOLVED_BASE_URLS,
    fallback_api_key=_FALLBACK_API_KEY if _RESOLVED_BASE_URLS else None,
    **_client_budgets(),
)

# Reject invalid deployment settings before accepting jobs, not once per image.
_build_request_options()
_build_extra_body()
EndpointScheduler.from_env()


class _SingleClientPool:
    def __init__(self, client: Any):
        self._client = client

    def get_client(self) -> Any:
        return self._client


def vision_completion_vllm(
    image_path: str,
    context: str = "",
    model: Optional[str] = None,
    prompt: Optional[str] = None,
) -> str:
    if not _CLIENT_POOL.has_clients():
        raise RuntimeError(
            "vLLM vision client is not configured. Set VLLM_BASE_URLS / VLLM_BASE_URL"
            " (comma-separated). VLLM_API_KEY is optional auth only."
        )

    errors: List[str] = []
    last_error: Optional[Exception] = None
    clients = dict(_CLIENT_POOL.get_endpoint_clients())
    scheduler = EndpointScheduler.from_env()
    request_options, extra_body = _build_request_options(), _build_extra_body()
    prepared = prepare_vision_request(
        image_path,
        context=context,
        model=model,
        prompt=prompt,
        default_model=DEFAULT_VISION_MODEL,
        extra_body=extra_body,
        request_options=request_options,
    )
    total = len(clients)
    while clients:
        with scheduler.acquire(clients) as key:
            client = clients.pop(key)
            attempt = total - len(clients)
            try:
                return vision_completion_openai_compatible(
                    image_path,
                    context=context,
                    model=model,
                    prompt=prompt,
                    default_model=DEFAULT_VISION_MODEL,
                    client_pool=_SingleClientPool(client),
                    extra_body=extra_body,
                    request_options=request_options,
                    prepared_request=prepared,
                )
            except Exception as exc:  # noqa: BLE001 - upstream client may fail
                # These failures belong to the request/configuration, not endpoint capacity.
                if isinstance(exc, (ValueError, FileNotFoundError)) or (
                    isinstance(exc, APIStatusError)
                    and exc.status_code not in (408, 429)
                    and exc.status_code < 500
                ):
                    raise
                if isinstance(exc, APIConnectionError) or (
                    isinstance(exc, APIStatusError)
                    and (exc.status_code in (408, 429) or exc.status_code >= 500)
                ):
                    scheduler.mark_failed(key)
                last_error = exc
                errors.append(type(exc).__name__)
                logger.warning(
                    "vLLM vision attempt {}/{} failed: {}", attempt, total, type(exc).__name__
                )

    assert last_error is not None
    detail = "; ".join(errors)
    raise RuntimeError(f"All configured vLLM vision endpoints failed: {detail}") from last_error
