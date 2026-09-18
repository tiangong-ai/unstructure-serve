"""Capacity is shared by independent processes and released after failure."""

import multiprocessing as mp
import os
import time


def _hold_slot(directory, events, delay):
    os.environ["MINERU_PARSE_SLOT_DIR"] = directory
    os.environ["MINERU_PARSE_SLOTS"] = "2"
    from src.services.parse_capacity import parse_slot

    with parse_slot():
        events.put(("enter", time.monotonic()))
        time.sleep(delay)
        events.put(("exit", time.monotonic()))


def test_independent_processes_share_capacity(tmp_path):
    ctx = mp.get_context("spawn")
    events = ctx.Queue()
    children = [ctx.Process(target=_hold_slot, args=(str(tmp_path), events, 0.2)) for _ in range(5)]
    try:
        for child in children:
            child.start()
        records = [events.get(timeout=15) for _ in range(10)]
        active = peak = 0
        for kind, _ in sorted(records, key=lambda pair: pair[1]):
            active += 1 if kind == "enter" else -1
            peak = max(peak, active)
        assert active == 0
        assert peak == 2
        for child in children:
            child.join(5)
            assert child.exitcode == 0
    finally:
        for child in children:
            if child.is_alive():
                child.kill()
            child.join(5)
        events.close()
        events.join_thread()


def test_exception_releases_slot(monkeypatch, tmp_path):
    import pytest
    from src.services.parse_capacity import parse_slot

    monkeypatch.setenv("MINERU_PARSE_SLOT_DIR", str(tmp_path))
    monkeypatch.setenv("MINERU_PARSE_SLOTS", "1")
    with pytest.raises(ValueError):
        with parse_slot():
            raise ValueError("parser failed")
    with parse_slot(wait_seconds=0.1):
        pass


def _hold_with_render_child(directory, events):
    os.environ["MINERU_PARSE_SLOT_DIR"] = directory
    os.environ["MINERU_PARSE_SLOTS"] = "1"
    from src.services.parse_capacity import parse_slot

    with parse_slot():
        child = os.fork()
        if child == 0:
            time.sleep(30)
            os._exit(0)
        events.put(child)
        time.sleep(30)


def test_killed_owner_releases_slot_even_with_live_render_child(tmp_path, monkeypatch):
    import signal
    from src.services.parse_capacity import parse_slot

    ctx = mp.get_context("spawn")
    events = ctx.Queue()
    owner = ctx.Process(target=_hold_with_render_child, args=(str(tmp_path), events))
    descendant = None
    monkeypatch.setenv("MINERU_PARSE_SLOT_DIR", str(tmp_path))
    monkeypatch.setenv("MINERU_PARSE_SLOTS", "1")
    try:
        owner.start()
        descendant = events.get(timeout=10)
        owner.kill()
        owner.join(5)
        assert owner.exitcode is not None
        with parse_slot(wait_seconds=0.5):
            pass
    finally:
        if owner.is_alive():
            owner.kill()
        owner.join(5)
        if descendant is not None:
            try:
                os.kill(descendant, signal.SIGKILL)
            except ProcessLookupError:
                pass
        events.close()
        events.join_thread()


def test_wait_is_bounded(monkeypatch, tmp_path):
    import pytest
    from src.services.parse_capacity import parse_slot

    monkeypatch.setenv("MINERU_PARSE_SLOT_DIR", str(tmp_path))
    monkeypatch.setenv("MINERU_PARSE_SLOTS", "1")
    with parse_slot():
        with pytest.raises(TimeoutError, match="shared MinerU parse capacity"):
            with parse_slot(wait_seconds=0.05):
                raise AssertionError("must not acquire a full capacity pool")
