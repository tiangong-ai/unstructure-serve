"""Durable local job records and immutable stage outputs shared by API and workers.

Requires a local filesystem shared by every process on this host. Messages carry
IDs only. File locks protect execution; atomic files protect completed outputs.
"""

from contextlib import contextmanager, suppress
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
from threading import Lock
import time
import uuid

_NAMESPACE = uuid.UUID("b1d26d74-2a41-46ad-a20a-d1322cc5a535")
_fds = set()
_fork_lock = Lock()


class JobConflict(ValueError):
    pass


class JobBusy(RuntimeError):
    pass


class StaleGeneration(RuntimeError):
    pass


class PublishUncertain(RuntimeError):
    def __init__(self, job_id):
        self.job_id = job_id
        super().__init__("Publication uncertain; the durable job and input are retained")


def _after_fork():
    for fd in _fds:
        os.close(fd)
    _fds.clear()
    _fork_lock.release()


os.register_at_fork(
    before=_fork_lock.acquire, after_in_parent=_fork_lock.release, after_in_child=_after_fork
)


def store_root():
    return Path(
        os.getenv("MINERU_JOB_STORE_DIR") or Path(__file__).resolve().parents[2] / "output" / "jobs"
    )


def job_dir(job_id):
    try:
        value = str(uuid.UUID(str(job_id)))
    except (ValueError, AttributeError, TypeError):
        raise ValueError("Invalid durable job ID") from None
    if value != str(job_id):
        raise ValueError("Noncanonical durable job ID")
    return store_root() / value


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="." + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


@contextmanager
def stage_lock(job_id, stage, *, shared=False, blocking=True):
    job_dir(job_id)  # validate before constructing the lock name
    if not re.fullmatch(r"[a-z0-9_-]+", stage):
        raise ValueError("Invalid stage lock name")
    directory = store_root() / ".locks"
    directory.mkdir(parents=True, exist_ok=True)
    with _fork_lock:
        fd = os.open(directory / f"{job_id}-{stage}.lock", os.O_CREAT | os.O_RDWR, 0o600)
        _fds.add(fd)
    try:
        try:
            fcntl.flock(
                fd,
                (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | (0 if blocking else fcntl.LOCK_NB),
            )
        except BlockingIOError:
            raise JobBusy("The job still has an active execution") from None
        yield
    finally:
        with _fork_lock:
            _fds.discard(fd)
            os.close(fd)


def read_job(job_id):
    return read_json(job_dir(job_id) / "job.json")


def find_job(job_id):
    try:
        return read_job(job_id)
    except (FileNotFoundError, ValueError):
        return None


def update_job(job_id, **values):
    with stage_lock(job_id, "metadata"):
        record = read_job(job_id)
        record.update(values, updated_at=time.time())
        atomic_json(job_dir(job_id) / "job.json", record)
        return record


def start_job(job_id):
    """Promote a queued job without overwriting a concurrent terminal state."""
    with stage_lock(job_id, "metadata"):
        record = read_job(job_id)
        if record["state"] == "PENDING":
            record.update(state="STARTED", updated_at=time.time())
            atomic_json(job_dir(job_id) / "job.json", record)
        return record


def create_job(mode, source, options, *, idempotency_key=None):
    if mode not in {"parse", "images", "two-stage"}:
        raise ValueError("Invalid job mode")
    if idempotency_key is not None and (
        not idempotency_key
        or len(idempotency_key) > 200
        or not idempotency_key.isascii()
        or any(ord(c) < 33 or ord(c) > 126 for c in idempotency_key)
    ):
        raise ValueError("Idempotency-Key must contain 1..200 printable ASCII characters")
    source = Path(source)
    with source.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    identity = {"mode": mode, "sha256": digest, "filename": source.name, "options": options}
    fingerprint = hashlib.sha256(
        json.dumps(identity, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()
    job_id = (
        str(uuid.uuid5(_NAMESPACE, mode + "\0" + idempotency_key))
        if idempotency_key
        else str(uuid.uuid4())
    )
    directory = job_dir(job_id)
    with stage_lock(job_id, "creation"):
        existing = find_job(job_id)
        if existing:
            if existing["fingerprint"] != fingerprint:
                raise JobConflict(
                    "Idempotency-Key already identifies different input or parameters"
                )
            return existing
        store_root().mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".creating-", dir=store_root()))
        try:
            target = staging / "input" / source.name
            target.parent.mkdir()
            shutil.copyfile(source, target)
            with target.open("rb") as stream:
                copied_digest = hashlib.file_digest(stream, "sha256").hexdigest()
                os.fsync(stream.fileno())
            fd = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
            if copied_digest != digest:
                raise JobConflict("Input changed while preparing the job")
            now = time.time()
            record = dict(
                job_id=job_id,
                mode=mode,
                options=options,
                fingerprint=fingerprint,
                source=f"input/{source.name}",
                source_sha256=digest,
                source_bytes=target.stat().st_size,
                state="PENDING",
                stage="queued",
                generation=0,
                publication="pending",
                created_at=now,
                updated_at=now,
            )
            atomic_json(staging / "job.json", record)
            os.replace(staging, directory)
            fd = os.open(store_root(), os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
            return record
        finally:
            if staging.exists():
                shutil.rmtree(staging)


def source_path(job_id):
    record = read_job(job_id)
    return job_dir(job_id) / record["source"]


@contextmanager
def execution(job_id, generation):
    with stage_lock(job_id, "lifetime", shared=True):
        record = read_job(job_id)
        if record["generation"] != generation or record["state"] == "EXPIRED":
            raise StaleGeneration("Superseded job attempt")
        yield record


def result_reference(job_id):
    return {"job_id": job_id, "stored_result": True}


def save_result(job_id, payload):
    directory = job_dir(job_id)
    atomic_json(directory / "result.json", payload)
    with (directory / "result.json").open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    atomic_json(directory / "complete.json", {"sha256": digest, "completed_at": time.time()})
    update_job(job_id, state="SUCCESS", stage="complete", error=None)
    return result_reference(job_id)


def verified_result_path(job_id):
    """Verify without loading JSON; caller must hold a lifetime lease until consumed."""
    directory = job_dir(job_id)
    if read_job(job_id)["state"] == "EXPIRED":
        raise FileNotFoundError("Stored job has expired")
    marker = read_json(directory / "complete.json")
    with (directory / "result.json").open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != marker["sha256"]:
            raise RuntimeError("Stored job result failed integrity verification")
    return directory / "result.json"


def read_result(job_id):
    return read_json(verified_result_path(job_id))


def status(job_id):
    record = read_job(job_id)
    state = record["state"]
    if state != "EXPIRED" and (job_dir(job_id) / "complete.json").is_file():
        state = "SUCCESS"
    result = {
        "task_id": job_id,
        "state": state,
        "stage": "complete" if state == "SUCCESS" else record["stage"],
        "generation": record["generation"],
        "publication": record["publication"],
    }
    if state == "FAILURE":
        result["error"] = record.get("error", "Task failed")
    if record.get("progress") is not None:
        result["progress"] = record["progress"]
    return result


def publish_job(job_id, publisher):
    with stage_lock(job_id, "publication"):
        record = read_job(job_id)
        if record["publication"] == "published" or status(job_id)["state"] in {
            "SUCCESS",
            "EXPIRED",
        }:
            return record
        try:
            publisher(record)
            return update_job(job_id, publication="published")
        except Exception as exc:
            # A broker response or the subsequent metadata write may fail after
            # acceptance. Always preserve the known ID in the API error even
            # when the filesystem cannot record the uncertain state.
            with suppress(OSError):
                update_job(job_id, publication="uncertain")
            raise PublishUncertain(job_id) from exc


def resume_job(job_id):
    with (
        stage_lock(job_id, "publication", blocking=False),
        stage_lock(job_id, "lifetime", blocking=False),
    ):
        record = read_job(job_id)
        if status(job_id)["state"] in {"SUCCESS", "EXPIRED"}:
            raise JobConflict("A completed or expired job cannot be resumed")
        return update_job(
            job_id,
            generation=record["generation"] + 1,
            state="PENDING",
            stage="queued",
            publication="pending",
            error=None,
        )


def fail_job(job_id, exc):
    if status(job_id)["state"] != "SUCCESS":
        update_job(job_id, state="FAILURE", error=f"{type(exc).__name__}: {exc}")


def retention_timestamp(record):
    marker = job_dir(record["job_id"]) / "complete.json"
    if record["state"] != "EXPIRED" and marker.is_file():
        return read_json(marker)["completed_at"]
    return record["updated_at"]


def collect_job(job_id, *, retention_seconds):
    if retention_seconds < 0:
        raise ValueError("retention_seconds must be nonnegative")
    try:
        with stage_lock(job_id, "lifetime", blocking=False):
            record = read_job(job_id)
            if status(job_id)["state"] not in {"SUCCESS", "FAILURE"}:
                return False
            if time.time() - retention_timestamp(record) < retention_seconds:
                return False
            # Keep the stable identity tombstone, so a reused key never silently resubmits.
            update_job(job_id, state="EXPIRED", stage="expired")
            for child in job_dir(job_id).iterdir():
                if child.name != "job.json":
                    if child.is_dir():
                        shutil.rmtree(child)
                    else:
                        child.unlink()
            return True
    except JobBusy:
        return False
