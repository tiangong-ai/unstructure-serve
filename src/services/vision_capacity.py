"""Host-wide vision endpoint leases, rotation and cooldown (Linux flock).

Only endpoint SHA-256 identifiers are persisted. All callers on one host must
share the directory and slot count. Lock files must never be removed while a
caller is running. Changing capacity requires a drained, fresh shared directory.
"""

from contextlib import contextmanager
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
from threading import Lock
import time

import httpx

_fds: set[int] = set()
_fork_lock = Lock()


def _after_fork():
    # Close inherited descriptors without LOCK_UN: a forked child must neither
    # retain its parent's lease nor release the parent's still-active lock.
    for fd in _fds:
        os.close(fd)
    _fds.clear()
    _fork_lock.release()


os.register_at_fork(
    before=_fork_lock.acquire,
    after_in_parent=_fork_lock.release,
    after_in_child=_after_fork,
)


def _open(path):
    with _fork_lock:
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        _fds.add(fd)
    return fd


def _close(fd):
    with _fork_lock:
        _fds.discard(fd)
        os.close(fd)


def endpoint_key(url: str) -> str:
    # HTTPX normalizes host/scheme case and default ports. These spellings must
    # share slots, but distinct DNS names/backend paths cannot safely be inferred
    # to represent the same physical model server.
    canonical = str(httpx.URL(url.strip()).copy_with(fragment=None)).rstrip("/")
    return hashlib.sha256(canonical.encode()).hexdigest()


class EndpointScheduler:
    def __init__(self, directory, *, slots=16, wait_seconds=180, cooldown_seconds=30):
        if isinstance(slots, bool) or not isinstance(slots, int) or slots < 1:
            raise ValueError("Vision endpoint slots must be a positive integer")
        if not math.isfinite(wait_seconds) or wait_seconds <= 0:
            raise ValueError("Vision capacity wait must be finite and positive")
        if not math.isfinite(cooldown_seconds) or cooldown_seconds < 0:
            raise ValueError("Vision cooldown must be finite and nonnegative")
        self.directory = Path(directory)
        self.slots = slots
        self.wait_seconds = wait_seconds
        self.cooldown_seconds = cooldown_seconds

    @classmethod
    def from_env(cls):
        return cls(
            os.getenv("VLLM_VISION_SLOT_DIR")
            or Path(tempfile.gettempdir()) / "tiangong_vision_slots",
            slots=int(os.getenv("VLLM_VISION_ENDPOINT_SLOTS", "16")),
            wait_seconds=float(os.getenv("VLLM_VISION_SLOT_WAIT_SECONDS", "180")),
            cooldown_seconds=float(os.getenv("VLLM_VISION_COOLDOWN_SECONDS", "30")),
        )

    @contextmanager
    def _state(self):
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = _open(self.directory / "control.lock")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            path = self.directory / "state.json"
            previous = path.read_text() if path.exists() else None
            state = (
                json.loads(previous)
                if previous is not None
                else {"version": 1, "endpoints": {}, "next": {}}
            )
            if (
                not isinstance(state, dict)
                or type(state.get("version", 1)) is not int
                or state.get("version", 1) != 1
            ):
                raise ValueError("Unsupported shared vision state version")
            if not isinstance(state.get("endpoints"), dict) or not isinstance(
                state.get("next"), dict
            ):
                raise ValueError("Invalid shared vision state schema")
            # Accept the initial unversioned format produced before deployment
            # of this module; future incompatible versions must fail closed.
            state["version"] = 1
            yield state
            serialized = json.dumps(state)
            if serialized == previous:
                return
            handle, temporary = tempfile.mkstemp(prefix=".state-", dir=self.directory)
            try:
                with os.fdopen(handle, "w") as stream:
                    stream.write(serialized)
                os.replace(temporary, path)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
        finally:
            _close(fd)

    @staticmethod
    def _validate_key(key):
        if not isinstance(key, str) or not re.fullmatch(r"[a-f0-9]{64}", key):
            raise ValueError("Vision endpoint identifier must be a SHA-256 digest")

    def mark_failed(self, key):
        self._validate_key(key)
        with self._state() as state:
            endpoint = state["endpoints"].setdefault(key, {"slots": self.slots})
            endpoint["until"] = max(endpoint.get("until", 0), time.time() + self.cooldown_seconds)

    @contextmanager
    def acquire(self, keys):
        keys = sorted(set(keys))
        if not keys:
            raise ValueError("No eligible vision endpoints")
        for key in keys:
            self._validate_key(key)
        group = hashlib.sha256("".join(keys).encode()).hexdigest()
        deadline = time.monotonic() + self.wait_seconds
        held = None
        chosen = None
        try:
            while held is None:
                with self._state() as state:
                    for key in keys:
                        endpoint = state["endpoints"].setdefault(key, {"slots": self.slots})
                        if endpoint["slots"] != self.slots:
                            raise ValueError(
                                "Shared vision endpoint slots differ; drain callers and use a "
                                "fresh shared directory to change capacity"
                            )
                    start = state["next"].get(group, 0) % len(keys)
                    for offset in range(len(keys)):
                        index = (start + offset) % len(keys)
                        key = keys[index]
                        if state["endpoints"][key].get("until", 0) > time.time():
                            continue
                        for slot in range(self.slots):
                            fd = _open(self.directory / f"{key}.{slot}.lock")
                            try:
                                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                            except BlockingIOError:
                                _close(fd)
                                continue
                            except BaseException:
                                _close(fd)
                                raise
                            held, chosen = fd, key
                            state["next"][group] = (index + 1) % len(keys)
                            break
                        if held is not None:
                            break
                if held is None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("Timed out waiting for shared vision endpoint capacity")
                    time.sleep(min(0.05, remaining))
            yield chosen
        finally:
            if held is not None:
                _close(held)
