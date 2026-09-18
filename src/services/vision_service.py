import os
import re
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Callable, Dict, List, Optional, Tuple, Union

from loguru import logger

from src.config.config import GENIMI_API_KEY, OPENAI_API_KEY
from src.services.vision_service_genimi import vision_completion_genimi
from src.services.vision_service_openai import vision_completion_openai
from src.services.vision_service_vllm import (
    DEFAULT_VISION_MODEL as DEFAULT_VLLM_MODEL,
    has_vllm_credentials,
    vision_completion_vllm,
)


@dataclass(frozen=True)
class ProviderSpec:
    key: str
    models: List[str]
    default_model: str
    call: Callable[[str, str, Optional[str], Optional[str]], str]
    has_credentials: Callable[[], bool]


def _env_list(name: str, fallback: List[str]) -> List[str]:
    raw = os.getenv(name)
    if not raw:
        return list(fallback)
    items = [item.strip() for item in raw.split(",") if item.strip()]
    return items or list(fallback)


def _base_providers() -> Dict[str, ProviderSpec]:
    return {
        "openai": ProviderSpec(
            key="openai",
            models=["gpt-5-mini"],
            default_model="gpt-5-mini",
            call=vision_completion_openai,
            has_credentials=lambda: bool(OPENAI_API_KEY),
        ),
        "gemini": ProviderSpec(
            key="gemini",
            models=["gemini-2.5-flash"],
            default_model="gemini-2.5-flash",
            call=vision_completion_genimi,
            has_credentials=lambda: bool(GENIMI_API_KEY),
        ),
        "vllm": ProviderSpec(
            key="vllm",
            models=[DEFAULT_VLLM_MODEL],
            default_model=DEFAULT_VLLM_MODEL,
            call=vision_completion_vllm,
            has_credentials=has_vllm_credentials,
        ),
    }


def _load_provider_specs() -> Dict[str, ProviderSpec]:
    base_specs = _base_providers()
    allowed = _env_list("VISION_PROVIDER_CHOICES", list(base_specs.keys()))

    specs: Dict[str, ProviderSpec] = {}
    for provider_key in allowed:
        base = base_specs.get(provider_key)
        if base is None:
            logger.warning(
                "Ignoring unsupported vision provider '%s' declared in VISION_PROVIDER_CHOICES.",
                provider_key,
            )
            continue

        models = _env_list(
            f"VISION_MODELS_{provider_key.upper()}",
            base.models,
        )
        if not models:
            logger.warning(
                "Skipping vision provider '%s' because it has no models configured.",
                provider_key,
            )
            continue

        default_model = (
            os.getenv(f"VISION_DEFAULT_MODEL_{provider_key.upper()}") or base.default_model
        ).strip()
        if default_model not in models:
            logger.warning(
                "Default model '%s' is not listed for provider '%s'. Using '%s' instead.",
                default_model,
                provider_key,
                models[0],
            )
            default_model = models[0]

        specs[provider_key] = ProviderSpec(
            key=provider_key,
            models=models,
            default_model=default_model,
            call=base.call,
            has_credentials=base.has_credentials,
        )

    if not specs:
        raise RuntimeError("No vision providers configured. Check VISION_PROVIDER_CHOICES.")

    return specs


PROVIDER_SPECS: Dict[str, ProviderSpec] = _load_provider_specs()


if TYPE_CHECKING:

    class VisionProvider(str, Enum): ...

else:
    VisionProvider = Enum(
        "VisionProvider",
        {spec.key.upper(): spec.key for spec in PROVIDER_SPECS.values()},
        module=__name__,
        type=str,
    )


def _sanitize_model_member(provider_key: str, model_name: str) -> str:
    base = f"{provider_key}_{re.sub(r'[^0-9A-Za-z]+', '_', model_name)}"
    sanitized = re.sub(r"_+", "_", base).strip("_")
    return sanitized.upper() or f"{provider_key.upper()}_MODEL"


def _build_model_enum() -> Tuple[Enum, Dict[str, VisionProvider]]:
    members: Dict[str, str] = {}
    lookup: Dict[str, VisionProvider] = {}

    for provider_key, spec in PROVIDER_SPECS.items():
        provider_enum = VisionProvider[provider_key.upper()]
        for model_name in spec.models:
            member_name = _sanitize_model_member(provider_key, model_name)
            suffix = 2
            while member_name in members:
                member_name = f"{member_name}_{suffix}"
                suffix += 1
            members[member_name] = model_name
            lookup[model_name] = provider_enum

    if not members:
        raise RuntimeError("No vision models configured.")

    return (
        Enum("VisionModel", members, module=__name__, type=str),
        lookup,
    )


if TYPE_CHECKING:

    class VisionModel(str, Enum): ...

    MODEL_PROVIDER_LOOKUP: Dict[str, VisionProvider]
else:
    VisionModel, MODEL_PROVIDER_LOOKUP = _build_model_enum()


DEFAULT_MODELS = {
    VisionProvider[spec.key.upper()]: spec.default_model for spec in PROVIDER_SPECS.values()
}

AVAILABLE_PROVIDER_VALUES: List[str] = [spec.key for spec in PROVIDER_SPECS.values()]
AVAILABLE_MODEL_VALUES: List[str] = list(MODEL_PROVIDER_LOOKUP.keys())


def _normalize_provider(value: Optional[Union[VisionProvider, str]]) -> Optional[VisionProvider]:
    if value is None:
        return None
    if isinstance(value, VisionProvider):
        return value
    candidate = value.strip().lower()
    for provider in VisionProvider:
        if provider.value == candidate:
            return provider
    return None


def _provider_value(value: Optional[Union[VisionProvider, str]]) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, VisionProvider):
        return value.value
    candidate = value.strip().lower()
    return candidate or None


def _resolve_provider(explicit: Optional[VisionProvider]) -> VisionProvider:
    if explicit:
        return explicit

    env_provider = _normalize_provider(os.getenv("VISION_PROVIDER"))
    if env_provider:
        return env_provider

    for provider in VisionProvider:
        spec = PROVIDER_SPECS[provider.value]
        if spec.has_credentials():
            return provider

    return next(iter(VisionProvider))


def _model_value(model: Optional[Union[VisionModel, str]]) -> Optional[str]:
    if model is None:
        return None
    if isinstance(model, VisionModel):
        return model.value
    candidate = model.strip()
    return candidate or None


def _resolve_model(
    provider: VisionProvider,
    explicit: Optional[Union[VisionModel, str]],
) -> str:
    explicit_value = _model_value(explicit)
    if explicit_value:
        return explicit_value

    env_model = (os.getenv("VISION_MODEL") or "").strip()
    if env_model and env_model in PROVIDER_SPECS[provider.value].models:
        return env_model

    return PROVIDER_SPECS[provider.value].default_model


def _provider_from_model(model: Optional[Union[VisionModel, str]]) -> Optional[VisionProvider]:
    model_value = _model_value(model)
    if not model_value:
        return None
    return MODEL_PROVIDER_LOOKUP.get(model_value)


def _normalize_request_overrides(
    provider: Optional[Union[VisionProvider, str]],
    model: Optional[Union[VisionModel, str]],
) -> Tuple[Optional[VisionProvider], Optional[str]]:
    raw_provider = _provider_value(provider)
    explicit_provider = _normalize_provider(provider)
    explicit_model = _model_value(model)
    model_provider = _provider_from_model(model)

    if raw_provider and explicit_provider is None:
        logger.warning(
            "Ignoring unsupported vision provider override '{}' and using configured defaults.",
            raw_provider,
        )

    if explicit_model and model_provider is None:
        logger.warning(
            "Ignoring unsupported vision model override '{}' and falling back to VISION_PROVIDER/VISION_MODEL.",
            explicit_model,
        )
        return None, None

    if explicit_provider and model_provider and explicit_provider != model_provider:
        logger.warning(
            "Ignoring mismatched vision provider/model override provider='{}' model='{}'. Using provider '{}' inferred from the model.",
            explicit_provider.value,
            explicit_model,
            model_provider.value,
        )
        return model_provider, explicit_model

    return explicit_provider or model_provider, explicit_model


def vision_completion(
    image_path: str,
    context: str = "",
    prompt: Optional[str] = None,
    provider: Optional[Union[VisionProvider, str]] = None,
    model: Optional[Union[VisionModel, str]] = None,
) -> str:
    requested_provider, requested_model = _normalize_request_overrides(provider, model)
    chosen = _resolve_provider(requested_provider)
    resolved_model = _resolve_model(chosen, requested_model)
    chosen_spec = PROVIDER_SPECS[chosen.value]

    result: Optional[str] = None

    if chosen_spec.has_credentials():
        logger.info(f"Vision request using provider='{chosen.value}' model='{resolved_model}'")
        try:
            result = chosen_spec.call(image_path, context, resolved_model, prompt)
            if result is not None:
                logger.info(
                    f"Vision response received from provider='{chosen.value}' model='{resolved_model}'"
                )
        except Exception as exc:  # noqa: BLE001 - provider call may raise
            logger.info(f"Vision provider '{chosen.value}' failed: {exc}")
    else:
        logger.info(
            f"Vision provider '{chosen.value}' skipped because credentials are not configured."
        )

    if result is not None:
        return result

    for backup in VisionProvider:
        if backup == chosen:
            continue
        backup_spec = PROVIDER_SPECS[backup.value]
        if not backup_spec.has_credentials():
            continue
        fallback_model = DEFAULT_MODELS.get(backup, backup_spec.default_model)
        logger.info(f"Vision fallback to provider='{backup.value}' model='{fallback_model}'")
        try:
            fallback_result = backup_spec.call(image_path, context, fallback_model, prompt)
            if fallback_result is not None:
                logger.info(
                    f"Vision response received from provider='{backup.value}' model='{fallback_model}'"
                )
                return fallback_result
        except Exception as exc:  # noqa: BLE001 - provider call may raise
            logger.info(f"Vision provider '{backup.value}' failed: {exc}")

    raise RuntimeError(
        "No working vision provider found. Ensure provider configuration and API keys are set."
    )
