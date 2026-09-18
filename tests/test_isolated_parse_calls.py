"""Real child-process faults must be reported promptly without losing large results."""

import importlib
import os
import signal
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux isolated task groups")


def _exit_without_result(*_args):
    os._exit(7)


def _killed_without_result():
    os.kill(os.getpid(), signal.SIGKILL)


def _large_result(size):
    return {"payload": "x" * size}


def _block():
    time.sleep(30)


def _crash_with_helper(pid_file):
    import subprocess

    helper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    Path(pid_file).write_text(str(helper.pid))
    os._exit(9)


def _alive(pid):
    try:
        return Path(f"/proc/{pid}/stat").read_text().split()[2] != "Z"
    except FileNotFoundError:
        return False


def test_ordinary_parser_reports_early_process_exit(monkeypatch):
    scheduler = importlib.import_module("src.services.gpu_scheduler")
    monkeypatch.setattr(scheduler, "_actual_parse", _exit_without_result)
    monkeypatch.setenv("MINERU_DEFAULT_HARD_TIMEOUT_SECONDS", "2")
    started = time.monotonic()
    with pytest.raises(RuntimeError, match=r"exit code 7"):
        scheduler._worker_process_file("unused.pdf", "default")
    assert time.monotonic() - started < 1.5


@pytest.mark.parametrize("size", [1, 16 * 1024 * 1024])
def test_isolated_call_transfers_complete_results_before_exit(size):
    scheduler = importlib.import_module("src.services.gpu_scheduler")
    assert scheduler.run_isolated_call(_large_result, size, hard_timeout=5) == {
        "payload": "x" * size
    }


def test_killed_child_reports_signal_without_waiting_for_timeout():
    scheduler = importlib.import_module("src.services.gpu_scheduler")
    started = time.monotonic()
    with pytest.raises(RuntimeError, match=r"exit code -9"):
        scheduler.run_isolated_call(_killed_without_result, hard_timeout=5)
    assert time.monotonic() - started < 1.5


def test_crashed_child_still_cleans_its_helper(tmp_path):
    scheduler = importlib.import_module("src.services.gpu_scheduler")
    pid_file = tmp_path / "helper.pid"
    try:
        with pytest.raises(RuntimeError, match=r"exit code 9"):
            scheduler.run_isolated_call(_crash_with_helper, str(pid_file), hard_timeout=5)
        assert not _alive(int(pid_file.read_text()))
    finally:
        if pid_file.exists() and _alive(pid := int(pid_file.read_text())):
            os.kill(pid, signal.SIGKILL)


def test_hard_timeout_still_terminates_a_live_child():
    scheduler = importlib.import_module("src.services.gpu_scheduler")
    with pytest.raises(TimeoutError, match="hard timeout"):
        scheduler.run_isolated_call(_block, hard_timeout=0.1)
