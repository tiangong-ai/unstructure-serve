import asyncio

import httpx
from fastapi import APIRouter, Depends, status

from src.services.mineru_service_full import (
    DEFAULT_VLLM_SERVER_URL,
    _normalize_server_url_input,
    _server_urls_from_env,
    _vlm_config,
)
from src.utils.response_utils import json_response, pretty_response_flag

router = APIRouter()


@router.get("/health", summary="API process liveness")
async def health_check(pretty: bool = Depends(pretty_response_flag)):
    """Report process liveness; use /ready to probe the MinerU VLM backend."""
    return json_response({"status": "healthy"}, pretty, status_code=status.HTTP_200_OK)


@router.get("/ready", summary="API and MinerU VLM readiness")
async def readiness_check(pretty: bool = Depends(pretty_response_flag)):
    """Probe every configured VLM endpoint, without exposing URLs or credentials.

    This does not test Redis, MinIO or the separate image-description service.
    The endpoint pool has no failover, so every endpoint must be reachable.
    """
    from mineru.config import config

    urls = (
        _server_urls_from_env()
        or _normalize_server_url_input(config.model.vlm.server_url)
        or [DEFAULT_VLLM_SERVER_URL]
    )

    async with httpx.AsyncClient(timeout=3) as client:

        async def check(url):
            try:
                # Reuse parsing's config and auth precedence, without rotating
                # the process-local pool: each probe selects an explicit URL.
                vlm = _vlm_config(None, url, None)
                headers = {"Authorization": f"Bearer {vlm.api_key}"} if vlm.api_key else {}
                url = url.rstrip("/").removesuffix("/v1") + "/health"
                response = await client.get(url, headers=headers)
                return response.status_code == 200
            except (httpx.HTTPError, httpx.InvalidURL, ValueError):
                return False

        checks = await asyncio.gather(*(check(url) for url in urls))
    ready = all(checks)
    return json_response(
        {
            "status": "ready" if ready else "not_ready",
            "mineru_vlm": "healthy" if ready else "unavailable",
        },
        pretty,
        status_code=200 if ready else 503,
    )
