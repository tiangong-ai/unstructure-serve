"""Health protocol and stale-state tests; HTTP fixtures do not run a model."""

import asyncio
import json
import multiprocessing as mp
import time

import httpx
import pytest

from src.services.vision_capacity import EndpointScheduler, endpoint_key

KEYS = [endpoint_key(f"http://fixture-{i}/v1") for i in range(2)]


def test_active_failure_avoids_endpoint_before_any_business_request(tmp_path):
    scheduler = EndpointScheduler(tmp_path, slots=4, wait_seconds=0.05, cooldown_seconds=0)
    scheduler.record_health(KEYS[0], healthy=False, models=(), reason="ConnectError")
    scheduler.record_health(KEYS[1], healthy=True, models=("wanted",))
    for _ in range(4):
        with scheduler.acquire(KEYS, model="wanted") as key:
            assert key == KEYS[1]
    with pytest.raises(RuntimeError, match="health"):
        with scheduler.acquire(KEYS, model="missing"):
            pytest.fail("A model not listed by any healthy endpoint must not be sent")
    state = (tmp_path / "state.json").read_text()
    assert "wanted" not in state and "http://" not in state


def test_health_success_does_not_close_business_circuit(tmp_path):
    scheduler = EndpointScheduler(tmp_path, slots=4, wait_seconds=0.05, cooldown_seconds=0)
    scheduler.mark_failed(KEYS[0])
    scheduler.record_health(KEYS[0], healthy=True, models=("wanted",))
    with scheduler.acquire(KEYS[:1], model="wanted"):
        with pytest.raises(TimeoutError):
            with scheduler.acquire(KEYS[:1], model="wanted"):
                pytest.fail("Healthy HTTP status does not verify inference")
    with scheduler.acquire(KEYS[:1], model="wanted"):
        with scheduler.acquire(KEYS[:1], model="wanted"):
            pass


def test_stale_health_cannot_block_recovery_forever(tmp_path, monkeypatch):
    scheduler = EndpointScheduler(tmp_path, slots=4, wait_seconds=0.05, cooldown_seconds=30)
    now = 1000
    monkeypatch.setattr("src.services.vision_capacity.time.time", lambda: now)
    scheduler.record_health(KEYS[0], healthy=False, models=())
    with pytest.raises(RuntimeError, match="health"):
        with scheduler.acquire(KEYS[:1]):
            pass
    now += 31
    with scheduler.acquire(KEYS[:1]):
        with pytest.raises(TimeoutError):
            with scheduler.acquire(KEYS[:1]):
                pytest.fail("Expired health still requires a single recovery trial")


def test_probe_failure_during_inference_invalidates_success(tmp_path):
    scheduler = EndpointScheduler(tmp_path, slots=4, wait_seconds=0.05, cooldown_seconds=0)
    scheduler.mark_failed(KEYS[0])
    with scheduler.acquire(KEYS[:1]):
        scheduler.record_health(KEYS[0], healthy=False, models=())
        scheduler.record_health(KEYS[0], healthy=True, models=("wanted",))
    with scheduler.acquire(KEYS[:1]):
        with pytest.raises(TimeoutError):
            with scheduler.acquire(KEYS[:1]):
                pytest.fail("The older trial cannot undo a newer probe failure")


@pytest.mark.parametrize("health_status", [200, 404, 405, 401, 503])
def test_probe_preserves_auth_and_base_prefix(health_status):
    from src.services.vision_health import probe_endpoint

    requests = []

    def handler(request):
        requests.append(request)
        assert request.headers["Authorization"] == "Bearer fixture-key"
        if request.url.path == "/gateway/health":
            return httpx.Response(health_status)
        assert request.url.path == "/gateway/v1/models"
        return httpx.Response(200, json={"data": [{"id": "wanted"}]})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await probe_endpoint(
                client, "https://fixture/gateway/v1/", "fixture-key", timeout=0.2
            )

    result = asyncio.run(run())
    assert result.healthy is (health_status in (200, 404, 405))
    assert len(requests) == (2 if result.healthy else 1)
    if result.healthy:
        assert result.models == ("wanted",)


@pytest.mark.parametrize("body", [{"data": []}, {"bad": []}, {"data": [{"id": ""}]}, []])
def test_probe_rejects_unusable_model_catalog(body):
    from src.services.vision_health import probe_endpoint

    async def run():
        def handler(request):
            return httpx.Response(200, json=body)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await probe_endpoint(client, "https://fixture/v1", "", timeout=0.2)

    assert not asyncio.run(run()).healthy


def test_probe_has_a_total_deadline():
    from src.services.vision_health import probe_endpoint

    async def run():
        async def handler(request):
            await asyncio.sleep(0.05)
            return httpx.Response(200, json={"data": [{"id": "wanted"}]})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await probe_endpoint(client, "https://fixture/v1", "", timeout=0.08)

    result = asyncio.run(run())
    assert not result.healthy and result.reason == "TimeoutError"


def test_probe_does_not_follow_redirects():
    from src.services.vision_health import probe_endpoint

    async def run():
        calls = []

        def handler(request):
            calls.append(request.url)
            return httpx.Response(302, headers={"Location": "https://other/private"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await probe_endpoint(client, "https://fixture/v1", "key", timeout=0.2)
        assert len(calls) == 1
        assert result.reason == "health_HTTP_302"
        assert not result.healthy

    asyncio.run(run())


def test_only_one_monitor_owns_shared_probe_lease(tmp_path):
    first = EndpointScheduler(tmp_path)
    second = EndpointScheduler(tmp_path)
    with first.monitor_lease() as leader:
        assert leader
        with second.monitor_lease() as other:
            assert not other
    with second.monitor_lease() as next_leader:
        assert next_leader


def _hold_monitor(directory, queue):
    with EndpointScheduler(directory).monitor_lease() as leader:
        queue.put(leader)
        time.sleep(20)


def test_monitor_process_death_allows_takeover(tmp_path):
    ctx = mp.get_context("spawn")
    queue = ctx.Queue()
    process = ctx.Process(target=_hold_monitor, args=(str(tmp_path), queue))
    process.start()
    try:
        assert queue.get(timeout=10)
        with EndpointScheduler(tmp_path).monitor_lease() as leader:
            assert not leader
        process.kill()
        process.join(10)
        with EndpointScheduler(tmp_path).monitor_lease() as leader:
            assert leader
    finally:
        if process.is_alive():
            process.kill()
            process.join(10)
        queue.close()
        queue.join_thread()


def test_model_catalog_change_requires_new_trial(tmp_path):
    scheduler = EndpointScheduler(tmp_path, slots=4, wait_seconds=0.05)
    scheduler.record_health(KEYS[0], healthy=True, models=("old",))
    scheduler.record_health(KEYS[0], healthy=True, models=("new",))
    with scheduler.acquire(KEYS[:1], model="new"):
        with pytest.raises(TimeoutError):
            with scheduler.acquire(KEYS[:1], model="new"):
                pass


@pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf")])
def test_invalid_health_ttl_rejected(tmp_path, value):
    with pytest.raises(ValueError, match="TTL"):
        EndpointScheduler(tmp_path, health_ttl_seconds=value)


def test_probe_round_is_bounded_parallel_and_repeats(tmp_path):
    from src.services.vision_health import monitor

    calls = []
    active = peak = 0

    async def run():
        stopped = asyncio.Event()

        async def handler(request):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            try:
                await asyncio.sleep(0.01)
                calls.append(request.url.path)
                if len(calls) >= 16:
                    stopped.set()
                return httpx.Response(200, json={"data": [{"id": "wanted"}]})
            finally:
                active -= 1

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await asyncio.wait_for(
                monitor(
                    [f"http://fixture-{i}/v1" for i in range(4)],
                    "",
                    EndpointScheduler(tmp_path),
                    stopped,
                    interval=0.03,
                    timeout=0.2,
                    concurrency=2,
                    client=client,
                ),
                timeout=2,
            )

    asyncio.run(run())
    assert peak == 2
    assert len(calls) >= 16
    assert len(json.loads((tmp_path / "state.json").read_text())["endpoints"]) == 4


def test_stop_cancels_probes_and_releases_leader_lease(tmp_path):
    from src.services.vision_health import monitor

    async def run():
        started, stopped = asyncio.Event(), asyncio.Event()

        async def handler(request):
            started.set()
            await asyncio.sleep(60)

        scheduler = EndpointScheduler(tmp_path)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            task = asyncio.create_task(
                monitor(
                    ["http://fixture/v1"],
                    "",
                    scheduler,
                    stopped,
                    client=client,
                    timeout=30,
                )
            )
            await asyncio.wait_for(started.wait(), timeout=1)
            stopped.set()
            await asyncio.wait_for(task, timeout=0.5)
        with scheduler.monitor_lease() as leader:
            assert leader

    asyncio.run(run())
