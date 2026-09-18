"""Host-wide parse capacity for API children and Celery workers (Linux flock)."""

from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import tempfile
from threading import Lock
import time

# A durable conversion redirects tempfile.tempdir into its private attempt.
# Capacity must remain shared with other tasks and API processes on this host.
_DEFAULT_SLOT_DIRECTORY = Path(tempfile.gettempdir()) / "tiangong_mineru_parse_slots"

# A render pool fork must not keep its parent's capacity lease alive. Closing
# an inherited descriptor is safe; LOCK_UN here would unlock the parent's lease.
_fds: set[int] = set()
_fork_lock = Lock()


def _after_fork():
    for fd in _fds:
        os.close(fd)
    _fds.clear()
    _fork_lock.release()


os.register_at_fork(
    before=_fork_lock.acquire,
    after_in_parent=_fork_lock.release,
    after_in_child=_after_fork,
)


@contextmanager
def parse_slot(*, wait_seconds: float | None = None):
    count = int(os.getenv("MINERU_PARSE_SLOTS", "3"))
    if count < 1:
        raise ValueError("MINERU_PARSE_SLOTS must be positive")
    timeout = (
        wait_seconds
        if wait_seconds is not None
        else float(os.getenv("MINERU_PARSE_SLOT_WAIT_SECONDS", "1800"))
    )
    if timeout <= 0:
        raise ValueError("MINERU_PARSE_SLOT_WAIT_SECONDS must be positive")
    directory = Path(os.getenv("MINERU_PARSE_SLOT_DIR") or _DEFAULT_SLOT_DIRECTORY)
    directory.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    handles = []
    held = None
    try:
        for index in range(count):
            # Never unlink lock files: all contenders must use the same inode.
            with _fork_lock:
                fd = os.open(directory / f"{index}.lock", os.O_CREAT | os.O_RDWR, 0o600)
                handles.append(fd)
                _fds.add(fd)
        while held is None:
            for fd in handles:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    continue
                held = fd
                break
            if held is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Timed out waiting for shared MinerU parse capacity")
                time.sleep(min(0.05, remaining))
        yield
    finally:
        with _fork_lock:
            if held is not None:
                fcntl.flock(held, fcntl.LOCK_UN)
            for fd in handles:
                _fds.discard(fd)
                os.close(fd)
