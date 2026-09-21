"""Dedicated, host-shared active vision health monitor (no inference traffic)."""

import argparse
import asyncio
from contextlib import AsyncExitStack
from dataclasses import dataclass
import json
import logging
import math
import os
import signal
import time

import httpx
from loguru import logger

from src.services.vision_capacity import EndpointScheduler, endpoint_key


@dataclass(frozen=True)
class ProbeResult:
    healthy: bool
    models: tuple[str, ...] = ()
    reason: str = ""


async def probe_endpoint(client, base_url, api_key, *, timeout):
    """Both lightweight GETs share a total deadline, including TLS and body reads."""
    base = httpx.URL(base_url.rstrip("/") + "/")
    health = base.join("../health" if base.path.rstrip("/").endswith("/v1") else "health")
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        async with asyncio.timeout(timeout):
            response = await client.get(
                health, headers=headers, timeout=timeout, follow_redirects=False
            )
            # OpenAI-compatible gateways need not expose vLLM's /health route.
            # Only a genuinely absent method/path falls back to the model list.
            if response.status_code not in (404, 405) and not response.is_success:
                return ProbeResult(False, reason=f"health_HTTP_{response.status_code}")
            response = await client.get(
                base.join("models"), headers=headers, timeout=timeout, follow_redirects=False
            )
            if not response.is_success:
                return ProbeResult(False, reason=f"models_HTTP_{response.status_code}")
            body = response.json()
            data = body.get("data") if isinstance(body, dict) else None
            if (
                not isinstance(data, list)
                or not data
                or any(
                    not isinstance(item, dict)
                    or not isinstance(item.get("id"), str)
                    or not item["id"].strip()
                    for item in data
                )
            ):
                return ProbeResult(False, reason="invalid_model_catalog")
            return ProbeResult(True, tuple(sorted({item["id"] for item in data})))
    except (httpx.HTTPError, TimeoutError, ValueError) as exc:
        return ProbeResult(False, reason=type(exc).__name__)


async def monitor(
    urls,
    api_key,
    scheduler,
    stopped,
    *,
    interval=10,
    timeout=2,
    concurrency=8,
    client=None,
    once=False,
):
    endpoints = {endpoint_key(url): url for url in urls}
    semaphore = asyncio.Semaphore(concurrency)
    async with AsyncExitStack() as stack:
        if client is None:
            client = await stack.enter_async_context(httpx.AsyncClient(timeout=timeout))

        async def check(key, url):
            async with semaphore:
                result = await probe_endpoint(client, url, api_key, timeout=timeout)
                changed = scheduler.record_health(
                    key, healthy=result.healthy, models=result.models, reason=result.reason
                )
                if changed:
                    logger.info(
                        "Vision endpoint {} probe: {} ({})",
                        key[:12],
                        "available" if result.healthy else "unavailable",
                        result.reason or "ok",
                    )

        while not stopped.is_set():
            # Standby instances never probe; their next loop can take over after
            # a leader exits. File locks, not PID files, define live ownership.
            with scheduler.monitor_lease() as leader:
                if leader:
                    while not stopped.is_set():
                        started = time.monotonic()
                        running = asyncio.gather(
                            *(check(key, url) for key, url in endpoints.items())
                        )
                        stopping = asyncio.create_task(stopped.wait())
                        try:
                            await asyncio.wait(
                                (running, stopping), return_when=asyncio.FIRST_COMPLETED
                            )
                            if stopped.is_set():
                                return
                            await running
                        finally:
                            running.cancel()
                            stopping.cancel()
                            await asyncio.gather(running, stopping, return_exceptions=True)
                        if once:
                            return
                        await _wait(stopped, max(0.01, interval - (time.monotonic() - started)))
                elif once:
                    raise RuntimeError("A vision health monitor already owns this directory")
            await _wait(stopped, interval)


async def _wait(stopped, seconds):
    try:
        await asyncio.wait_for(stopped.wait(), timeout=seconds)
    except TimeoutError:
        pass


def _positive_env(name, default):
    value = float(os.getenv(name, str(default)))
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be finite and positive")
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="Probe once and print local status")
    parser.add_argument("--status", action="store_true", help="Read local status without probing")
    args = parser.parse_args()
    # Same .env/TOML resolution as inference; no monitor is started by imports.
    from src.services.vision_service_vllm import _resolve_api_key, _resolve_base_urls

    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)
    scheduler = EndpointScheduler.from_env()
    urls = _resolve_base_urls()
    keys = list(dict.fromkeys(endpoint_key(url) for url in urls))
    if args.status:
        print(json.dumps(scheduler.health_snapshot(keys), indent=2))
        return
    interval = _positive_env("VLLM_VISION_HEALTH_INTERVAL_SECONDS", 10)
    timeout = _positive_env("VLLM_VISION_HEALTH_TIMEOUT_SECONDS", 2)
    if scheduler.health_ttl_seconds <= interval:
        raise ValueError("Vision health TTL must exceed the probe interval")
    if not urls:
        logger.info("No remote vision endpoints configured; monitor will remain idle")

    async def run():
        stopped = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stopped.set)
        await monitor(
            urls,
            _resolve_api_key(),
            scheduler,
            stopped,
            interval=interval,
            timeout=timeout,
            once=args.once,
        )

    asyncio.run(run())
    if args.once:
        print(json.dumps(scheduler.health_snapshot(keys), indent=2))


if __name__ == "__main__":
    main()
