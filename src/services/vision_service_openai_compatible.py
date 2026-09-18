import base64
import mimetypes
from threading import Lock
from typing import Any, Dict, List, Optional, Sequence

from openai import OpenAI

from src.services.vision_prompts import build_vision_messages
from src.services.vision_capacity import endpoint_key


def encode_image(image_path: str) -> str:
    with open(image_path, "rb") as image_file:
        return base64.b64encode(image_file.read()).decode("utf-8")


class OpenAICompatibleClientPool:
    """Lightweight client pool that supports OpenAI-compatible endpoints."""

    def __init__(
        self,
        api_key: str,
        base_urls: Optional[Sequence[str]] = None,
        fallback_api_key: Optional[str] = None,
        timeout: float = 600,
        max_retries: int = 2,
    ):
        resolved_urls = list(
            {
                endpoint_key(url): url.strip() for url in base_urls or [] if url and url.strip()
            }.values()
        )
        resolved_key = (api_key or "").strip()
        if resolved_urls and not resolved_key and fallback_api_key is not None:
            resolved_key = fallback_api_key

        self._clients = self._build_clients(resolved_key, resolved_urls, timeout, max_retries)
        self._endpoint_keys = [endpoint_key(url) for url in resolved_urls]
        self._single = self._clients[0] if len(self._clients) == 1 else None
        self._next_index = 0
        self._lock = Lock()

    @staticmethod
    def _build_clients(
        api_key: str, base_urls: List[str], timeout: float, max_retries: int
    ) -> List[OpenAI]:
        clients: List[OpenAI] = []
        if base_urls:
            clients = [
                OpenAI(api_key=api_key, base_url=url, timeout=timeout, max_retries=max_retries)
                for url in base_urls
            ]
        elif api_key:
            clients = [OpenAI(api_key=api_key, timeout=timeout, max_retries=max_retries)]
        return clients

    def has_clients(self) -> bool:
        return bool(self._clients)

    def get_clients_in_priority_order(self) -> List[OpenAI]:
        if not self._clients:
            raise RuntimeError("OpenAI-compatible vision client is not configured.")
        if self._single:
            return [self._single]

        with self._lock:
            start_index = self._next_index
            self._next_index = (self._next_index + 1) % len(self._clients)

        return [*self._clients[start_index:], *self._clients[:start_index]]

    def get_client(self) -> OpenAI:
        return self.get_clients_in_priority_order()[0]

    def get_endpoint_clients(self):
        """Stable endpoint identities for shared scheduling; no process-local rotation."""
        return list(zip(self._endpoint_keys, self._clients))


def prepare_vision_request(
    image_path: str,
    *,
    context: str = "",
    model: Optional[str] = None,
    prompt: Optional[str] = None,
    default_model: str,
    extra_body: Optional[Dict[str, Any]] = None,
    request_options: Optional[Dict[str, Any]] = None,
) -> dict:
    base64_image = encode_image(image_path)
    mime = mimetypes.guess_type(image_path)[0] or "image/jpeg"
    if not mime.startswith("image/"):
        raise ValueError("Vision asset must be an image")
    payload = {
        "model": model or default_model,
        "messages": build_vision_messages(context, prompt, f"data:{mime};base64,{base64_image}"),
    }
    if extra_body:
        payload["extra_body"] = extra_body
    if request_options:
        payload.update(request_options)
    return payload


def vision_completion_openai_compatible(
    image_path: str,
    *,
    context: str = "",
    model: Optional[str] = None,
    prompt: Optional[str] = None,
    default_model: str,
    client_pool: OpenAICompatibleClientPool,
    extra_body: Optional[Dict[str, Any]] = None,
    request_options: Optional[Dict[str, Any]] = None,
    prepared_request: Optional[Dict[str, Any]] = None,
) -> str:
    client = client_pool.get_client()
    request_payload = (
        prepared_request
        if prepared_request is not None
        else prepare_vision_request(
            image_path,
            context=context,
            model=model,
            prompt=prompt,
            default_model=default_model,
            extra_body=extra_body,
            request_options=request_options,
        )
    )

    response = client.chat.completions.create(
        **request_payload,
    )
    if not response.choices:
        raise RuntimeError("Vision endpoint returned no choices")
    choice = response.choices[0]
    finish_reason = getattr(choice, "finish_reason", None)
    if finish_reason not in (None, "stop"):
        raise RuntimeError(f"Vision output is incomplete (finish_reason={finish_reason})")
    content = choice.message.content
    if not isinstance(content, str) or not content.strip():
        raise RuntimeError("Vision endpoint returned empty content")
    return content
