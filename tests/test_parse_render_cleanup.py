"""A completed isolated task must not wait for the watchdog to kill its renderer."""

import importlib
import json
import multiprocessing
import os
from pathlib import Path
import queue
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest


def _parse_with_renderer(*_args):
    from concurrent.futures.process import ProcessPoolExecutor
    from docvortex.document.pdf import images
    from docvortex.document.pdf.images import _get_pdf_render_executor

    # The API test fixture stubs concurrent.futures.ProcessPoolExecutor. This
    # child must create a real nested pool to exercise multiprocessing shutdown.
    images.ProcessPoolExecutor = ProcessPoolExecutor
    pid = _get_pdf_render_executor().submit(os.getpid).result(timeout=10)
    return {"helper_pid": pid}


@pytest.mark.skipif(sys.platform != "linux", reason="Linux task process groups")
def test_completed_parse_child_closes_its_real_nested_render_pool(monkeypatch):
    scheduler = importlib.import_module("src.services.gpu_scheduler")
    monkeypatch.setattr(scheduler, "_actual_parse", _parse_with_renderer)
    context = multiprocessing.get_context("fork")
    result_queue = context.Queue()
    process = context.Process(
        target=scheduler._child_worker, args=(result_queue, "unused.pdf", "default", None)
    )
    process.start()
    try:
        message = result_queue.get(timeout=20)
        assert message["ok"], message
        process.join(timeout=3)
        assert process.exitcode == 0, "Finished parser is stuck waiting for its render child"
        with pytest.raises(ProcessLookupError):
            os.kill(message["data"]["helper_pid"], 0)
    finally:
        scheduler._cleanup_child_process(process, terminate=process.is_alive())
        result_queue.close()
        result_queue.join_thread()


def test_failed_parse_still_releases_render_resources(monkeypatch):
    scheduler = importlib.import_module("src.services.gpu_scheduler")
    closed = []
    monkeypatch.setattr(scheduler, "_configure_parse_child_process", lambda: None)
    monkeypatch.setitem(
        sys.modules,
        "docvortex.document.pdf.images",
        SimpleNamespace(shutdown_pdf_render_executor=lambda: closed.append(True)),
    )

    def fail(*args):
        raise ValueError("parse failed")

    monkeypatch.setattr(scheduler, "_actual_parse", fail)
    result_queue = queue.Queue()
    scheduler._child_worker(result_queue, "unused.pdf", "default", None)
    assert result_queue.get_nowait() == {"ok": False, "error": "parse failed"}
    assert closed == [True]


@pytest.mark.skipif(sys.platform != "linux", reason="Linux task process groups")
def test_generic_isolated_call_closes_real_nested_render_pool():
    scheduler = importlib.import_module("src.services.gpu_scheduler")
    payload = scheduler.run_isolated_call(_parse_with_renderer, hard_timeout=20)
    with pytest.raises(ProcessLookupError):
        os.kill(payload["helper_pid"], 0)


@pytest.mark.parametrize("parse_fails", [False, True])
@pytest.mark.parametrize("sdk_cleanup_fails", [False, True])
def test_task_publishes_once_before_sdk_and_render_cleanup(
    monkeypatch, caplog, parse_fails, sdk_cleanup_fails
):
    scheduler = importlib.import_module("src.services.gpu_scheduler")
    events = []
    messages = []
    monkeypatch.setattr(scheduler, "_configure_parse_child_process", lambda: None)

    def close_sdk():
        events.append("sdk")
        if sdk_cleanup_fails:
            raise RuntimeError("private upstream response")

    monkeypatch.setitem(
        sys.modules,
        "mineru.model.vlm.runtime",
        SimpleNamespace(shutdown_cached_models=close_sdk),
    )
    monkeypatch.setitem(
        sys.modules,
        "docvortex.document.pdf.images",
        SimpleNamespace(shutdown_pdf_render_executor=lambda: events.append("renderer")),
    )

    def parse():
        if parse_fails:
            raise ValueError("parse failed")
        return {"result": "complete"}

    def publish(message):
        events.append("publish")
        messages.append(message)

    scheduler._run_child_job(publish, parse, (), {})
    expected = (
        {"ok": False, "error": "parse failed"}
        if parse_fails
        else {"ok": True, "data": {"result": "complete"}}
    )
    assert messages == [expected]
    assert events == ["publish", "sdk", "renderer"]
    assert "private upstream response" not in caplog.text
    assert ("MinerU runtime cleanup failed" in caplog.text) is sdk_cleanup_fails


def test_unloaded_sdk_is_not_imported_for_cleanup(monkeypatch):
    import builtins

    scheduler = importlib.import_module("src.services.gpu_scheduler")
    monkeypatch.setattr(scheduler, "_configure_parse_child_process", lambda: None)
    monkeypatch.delitem(sys.modules, "mineru.model.vlm.runtime", raising=False)
    original_import = builtins.__import__
    events = []
    monkeypatch.setitem(
        sys.modules,
        "docvortex.document.pdf.images",
        SimpleNamespace(shutdown_pdf_render_executor=lambda: events.append("renderer")),
    )

    def guarded_import(name, *args, **kwargs):
        assert not name.startswith("mineru"), "An unused VLM runtime must not be initialized"
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    scheduler._run_child_job(lambda message: events.append(message), lambda: "done", (), {})
    assert events == [{"ok": True, "data": "done"}, "renderer"]
    assert "mineru.model.vlm.runtime" not in sys.modules


@pytest.mark.parametrize("parse_fails", [False, True])
def test_real_sdk_clients_close_before_interpreter_exit(parse_fails):
    # Exercise the actual cached HTTP runtime and its loop/executor shutdown;
    # replacing only the network client keeps this deterministic and offline.
    script = """
import asyncio
import json
import sys
from src.services import gpu_scheduler as scheduler
from mineru.model.vlm import runtime
closed = []
messages = []
class OfflineClient:
    def __init__(self, **kwargs):
        pass
    async def aclose(self):
        closed.append('client')
runtime.MinerUClient = OfflineClient
def parse():
    predictor = runtime.ModelSingleton().get_model(
        'http-client', None, 'http://unused.invalid', model_name='offline-test'
    )
    # Create the SDK loop's default executor before Python's shutdown phase.
    asyncio.run_coroutine_threadsafe(
        asyncio.to_thread(lambda: None), predictor._loop
    ).result(timeout=5)
    if sys.argv[1] == 'fail':
        raise ValueError('parse failed')
    return 'done'
scheduler._run_child_job(messages.append, parse, (), {})
assert closed == ['client'], closed
assert len(messages) == 1
assert messages[0]['ok'] == (sys.argv[1] != 'fail')
print(json.dumps({'closed': closed, 'messages': messages}))
"""
    result = subprocess.run(
        [sys.executable, "-c", script, "fail" if parse_fails else "success"],
        cwd=Path(__file__).parents[1],
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip().splitlines()[-1])["closed"] == ["client"]
    assert "Exception ignored in atexit callback" not in result.stderr
    assert "cannot schedule new futures" not in result.stderr


def _result_with_blocking_sdk_cleanup(marker):
    def close_sdk():
        Path(marker).write_text("entered")
        time.sleep(30)

    sys.modules["mineru.model.vlm.runtime"] = SimpleNamespace(shutdown_cached_models=close_sdk)
    return {"child_pid": os.getpid()}


@pytest.mark.skipif(sys.platform != "linux", reason="Linux task process groups")
def test_published_result_survives_blocked_sdk_cleanup(monkeypatch, tmp_path):
    scheduler = importlib.import_module("src.services.gpu_scheduler")
    monkeypatch.setattr(scheduler, "_CHILD_EXIT_GRACE_SECONDS", 0.2)
    monkeypatch.setattr(scheduler, "_CHILD_TERMINATE_GRACE_SECONDS", 0.2)
    marker = tmp_path / "sdk-cleanup-entered"
    started = time.monotonic()
    result = scheduler.run_isolated_call(
        _result_with_blocking_sdk_cleanup, str(marker), hard_timeout=2
    )
    assert marker.read_text() == "entered"
    assert time.monotonic() - started < 3
    with pytest.raises(ProcessLookupError):
        os.kill(result["child_pid"], 0)
