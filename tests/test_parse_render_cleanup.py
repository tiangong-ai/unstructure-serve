"""A completed isolated task must not wait for the watchdog to kill its renderer."""

import importlib
import multiprocessing
import os
import queue
import sys
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
