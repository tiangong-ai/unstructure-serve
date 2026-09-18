"""Real process/lock tests; no network or model dependency."""

import hashlib
import multiprocessing as mp
import time

import pytest

from src.services.vision_capacity import EndpointScheduler, endpoint_key

ENDPOINTS = [hashlib.sha256(x.encode()).hexdigest() for x in ("one", "two")]


def _choose_once(directory, queue):
    scheduler = EndpointScheduler(directory, slots=1, wait_seconds=2, cooldown_seconds=0.1)
    with scheduler.acquire(ENDPOINTS) as key:
        queue.put(key)


def _hold(directory, queue):
    scheduler = EndpointScheduler(directory, slots=1, wait_seconds=0.2, cooldown_seconds=0.1)
    with scheduler.acquire(ENDPOINTS[:1]):
        queue.put("held")
        time.sleep(20)


def _sleep():
    time.sleep(20)


def test_forked_child_does_not_keep_parent_lease(tmp_path):
    scheduler = EndpointScheduler(tmp_path, slots=1, wait_seconds=0.1)
    child = None
    try:
        with scheduler.acquire(ENDPOINTS[:1]):
            child = mp.get_context("fork").Process(target=_sleep)
            child.start()
        assert child.is_alive()
        with scheduler.acquire(ENDPOINTS[:1]) as selected:
            assert selected == ENDPOINTS[0]
    finally:
        if child is not None:
            child.kill()
            child.join(10)


def test_threads_share_the_same_endpoint_limit(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Lock

    scheduler = EndpointScheduler(tmp_path, slots=2, wait_seconds=2)
    active = peak = 0
    lock = Lock()

    def work():
        nonlocal active, peak
        with scheduler.acquire(ENDPOINTS[:1]):
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.03)
            with lock:
                active -= 1

    with ThreadPoolExecutor(max_workers=6) as executor:
        list(executor.map(lambda _: work(), range(12)))
    assert peak == 2


@pytest.mark.parametrize("start_method", ["fork", "spawn"])
def test_fresh_processes_share_rotation(tmp_path, start_method):
    ctx = mp.get_context(start_method)
    queue = ctx.Queue()
    selected = []
    for _ in range(6):
        proc = ctx.Process(target=_choose_once, args=(str(tmp_path), queue))
        proc.start()
        selected.append(queue.get(timeout=10))
        proc.join(10)
        assert proc.exitcode == 0
    queue.close()
    queue.join_thread()
    assert selected.count(ENDPOINTS[0]) == selected.count(ENDPOINTS[1]) == 3
    assert all(a != b for a, b in zip(selected, selected[1:]))


def test_slot_is_bounded_and_process_death_releases_it(tmp_path):
    ctx = mp.get_context("spawn")
    queue = ctx.Queue()
    proc = ctx.Process(target=_hold, args=(str(tmp_path), queue))
    proc.start()
    try:
        assert queue.get(timeout=10) == "held"
        scheduler = EndpointScheduler(tmp_path, slots=1, wait_seconds=0.1, cooldown_seconds=0.1)
        with pytest.raises(TimeoutError):
            with scheduler.acquire(ENDPOINTS[:1]):
                pytest.fail("Another process already holds the only slot")
        proc.kill()
        proc.join(10)
        with scheduler.acquire(ENDPOINTS[:1]) as selected:
            assert selected == ENDPOINTS[0]
    finally:
        if proc.is_alive():
            proc.kill()
            proc.join(10)
        queue.close()
        queue.join_thread()


def test_busy_endpoint_and_cooldown_are_shared(tmp_path):
    first = EndpointScheduler(tmp_path, slots=1, wait_seconds=0.1, cooldown_seconds=0.3)
    second = EndpointScheduler(tmp_path, slots=1, wait_seconds=0.1, cooldown_seconds=0.3)
    with first.acquire(ENDPOINTS) as held:
        with second.acquire(ENDPOINTS) as other:
            assert other != held
    first.mark_failed(ENDPOINTS[0])
    with second.acquire(ENDPOINTS) as selected:
        assert selected == ENDPOINTS[1]
    with pytest.raises(TimeoutError):
        with second.acquire(ENDPOINTS[:1]):
            pytest.fail("Endpoint cooling down")
    time.sleep(0.25)
    with second.acquire(ENDPOINTS[:1]) as selected:
        assert selected == ENDPOINTS[0]


def test_configuration_mismatch_fails_closed(tmp_path):
    first = EndpointScheduler(tmp_path, slots=1, wait_seconds=0.1, cooldown_seconds=1)
    with first.acquire(ENDPOINTS):
        conflicting = EndpointScheduler(tmp_path, slots=2, wait_seconds=0.1, cooldown_seconds=1)
        with pytest.raises(ValueError, match="slots"):
            with conflicting.acquire(ENDPOINTS):
                pass


def test_unknown_shared_state_version_fails_closed(tmp_path):
    import json

    (tmp_path / "state.json").write_text(json.dumps({"version": 2, "endpoints": {}, "next": {}}))
    scheduler = EndpointScheduler(tmp_path, slots=1, wait_seconds=0.1)
    with pytest.raises(ValueError, match="version"):
        with scheduler.acquire(ENDPOINTS):
            pass


def test_equivalent_http_url_spellings_share_capacity_identity():
    assert endpoint_key("HTTP://Example.COM:80/v1/") == endpoint_key("http://example.com/v1")
    assert endpoint_key("https://example.com:443/v1") == endpoint_key("https://example.com/v1/")
    assert endpoint_key("http://example.com:81/v1") != endpoint_key("http://example.com/v1")


def test_capacity_wait_does_not_rewrite_shared_state(tmp_path):
    scheduler = EndpointScheduler(tmp_path, slots=1, wait_seconds=0.06)
    with scheduler.acquire(ENDPOINTS[:1]):
        modified = (tmp_path / "state.json").stat().st_mtime_ns
        with pytest.raises(TimeoutError):
            with scheduler.acquire(ENDPOINTS[:1]):
                pass
        assert (tmp_path / "state.json").stat().st_mtime_ns == modified


@pytest.mark.parametrize(
    "name,value", [("slots", 0), ("wait_seconds", float("nan")), ("cooldown_seconds", -1)]
)
def test_invalid_budgets_rejected(tmp_path, name, value):
    with pytest.raises(ValueError):
        EndpointScheduler(tmp_path, **{name: value})
