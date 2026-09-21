"""Host-wide vision endpoint leases, rotation and circuit recovery (Linux flock).

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


def model_key(model: str) -> str:
    return hashlib.sha256(model.encode()).hexdigest()


class EndpointScheduler:
    def __init__(
        self,
        directory,
        *,
        slots=16,
        wait_seconds=180,
        cooldown_seconds=30,
        health_ttl_seconds=30,
    ):
        if isinstance(slots, bool) or not isinstance(slots, int) or slots < 1:
            raise ValueError("Vision endpoint slots must be a positive integer")
        if not math.isfinite(wait_seconds) or wait_seconds <= 0:
            raise ValueError("Vision capacity wait must be finite and positive")
        if not math.isfinite(cooldown_seconds) or cooldown_seconds < 0:
            raise ValueError("Vision cooldown must be finite and nonnegative")
        if not math.isfinite(health_ttl_seconds) or health_ttl_seconds <= 0:
            raise ValueError("Vision health TTL must be finite and positive")
        self.directory = Path(directory)
        self.slots = slots
        self.wait_seconds = wait_seconds
        self.cooldown_seconds = cooldown_seconds
        self.health_ttl_seconds = health_ttl_seconds

    @classmethod
    def from_env(cls):
        return cls(
            os.getenv("VLLM_VISION_SLOT_DIR")
            or Path(tempfile.gettempdir()) / "tiangong_vision_slots",
            slots=int(os.getenv("VLLM_VISION_ENDPOINT_SLOTS", "16")),
            wait_seconds=float(os.getenv("VLLM_VISION_SLOT_WAIT_SECONDS", "180")),
            cooldown_seconds=float(os.getenv("VLLM_VISION_COOLDOWN_SECONDS", "30")),
            health_ttl_seconds=float(os.getenv("VLLM_VISION_HEALTH_TTL_SECONDS", "30")),
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
            self._fail(endpoint)

    def _fail(self, endpoint):
        endpoint["until"] = max(endpoint.get("until", 0), time.time() + self.cooldown_seconds)
        endpoint["generation"] = endpoint.get("generation", 0) + 1

    def _finish_recovery(self, key, generation, succeeded):
        with self._state() as state:
            endpoint = state["endpoints"][key]
            # An older in-flight result must not undo a newer failure/probe.
            if endpoint.get("generation", 0) == generation:
                if succeeded:
                    endpoint.pop("until", None)
                else:
                    self._fail(endpoint)

    def _try_lock(self, name):
        fd = _open(self.directory / name)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except BlockingIOError:
            _close(fd)
            return None
        except BaseException:
            _close(fd)
            raise

    @contextmanager
    def monitor_lease(self):
        """One active monitor per host directory; process death releases ownership."""
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = self._try_lock("monitor.lock")
        try:
            yield fd is not None
        finally:
            if fd is not None:
                _close(fd)

    def record_health(self, key, *, healthy, models, reason=""):
        self._validate_key(key)
        with self._state() as state:
            endpoint = state["endpoints"].setdefault(key, {"slots": self.slots})
            old = endpoint.get("health", {})
            catalog = sorted({model_key(model) for model in models})
            endpoint["health"] = {
                "checked_at": time.time(),
                "healthy": healthy,
                "models": catalog,
                "reason": reason,
            }
            if not healthy:
                self._fail(endpoint)
            elif old.get("healthy") and old.get("models") != catalog:
                # A changed catalog may indicate reloaded weights/model servers.
                # Require an inference trial, without adding a network cooldown.
                endpoint.setdefault("until", 0)
                endpoint["generation"] = endpoint.get("generation", 0) + 1
            return old.get("healthy") != healthy or old.get("reason") != reason

    def _fresh_health(self, endpoint):
        health = endpoint.get("health")
        if health and 0 <= time.time() - health["checked_at"] < self.health_ttl_seconds:
            return health
        return None

    def _health_blocks(self, endpoint, model):
        health = self._fresh_health(endpoint)
        return bool(
            health
            and (not health["healthy"] or (model is not None and model not in health["models"]))
        )

    def health_snapshot(self, keys):
        """Local operator view; no URLs, credentials or model names."""
        with self._state() as state:
            rows = []
            for key in keys:
                self._validate_key(key)
                endpoint = state["endpoints"].get(key, {})
                health = endpoint.get("health", {})
                rows.append(
                    {
                        "endpoint_id": key,
                        "fresh": self._fresh_health(endpoint) is not None,
                        "healthy": health.get("healthy"),
                        "checked_at": health.get("checked_at"),
                        "reason": health.get("reason"),
                        "model_count": len(health.get("models", [])),
                        "circuit": (
                            "open"
                            if endpoint.get("until", 0) > time.time()
                            else "recovery" if "until" in endpoint else "closed"
                        ),
                    }
                )
            return rows

    @contextmanager
    def acquire(self, keys, *, model=None):
        keys = sorted(set(keys))
        if not keys:
            raise ValueError("No eligible vision endpoints")
        for key in keys:
            self._validate_key(key)
        model = model_key(model) if model is not None else None
        group = hashlib.sha256("".join(keys).encode()).hexdigest()
        deadline = time.monotonic() + self.wait_seconds
        held = None
        chosen = None
        recovery = None
        generation = None
        succeeded = False
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
                    if all(self._health_blocks(state["endpoints"][key], model) for key in keys):
                        raise RuntimeError("No vision endpoint passes fresh health/model checks")
                    for offset in range(len(keys)):
                        index = (start + offset) % len(keys)
                        key = keys[index]
                        endpoint = state["endpoints"][key]
                        if self._health_blocks(endpoint, model):
                            continue
                        if endpoint.get("until", 0) > time.time():
                            continue
                        trial = None
                        if "until" in endpoint:
                            trial = self._try_lock(f"{key}.recovery.lock")
                            if trial is None:
                                continue
                        try:
                            for slot in range(self.slots):
                                fd = self._try_lock(f"{key}.{slot}.lock")
                                if fd is None:
                                    continue
                                held, chosen = fd, key
                                recovery, generation = trial, endpoint.get("generation", 0)
                                state["next"][group] = (index + 1) % len(keys)
                                break
                        finally:
                            if held is None and trial is not None:
                                _close(trial)
                        if held is not None:
                            break
                if held is None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("Timed out waiting for shared vision endpoint capacity")
                    time.sleep(min(0.05, remaining))
            yield chosen
            succeeded = True
        finally:
            try:
                if recovery is not None:
                    self._finish_recovery(chosen, generation, succeeded)
            finally:
                if recovery is not None:
                    _close(recovery)
                if held is not None:
                    _close(held)
