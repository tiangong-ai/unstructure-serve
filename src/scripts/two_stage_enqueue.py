import hashlib
import json
import logging
import os
import pickle
import tempfile
import time
import tomllib
from collections import deque
from pathlib import Path
from typing import Dict, Iterable

import requests
from dotenv import load_dotenv

load_dotenv()

API_BASE = (
    os.environ.get("TWO_STAGE_BASE")
    or os.environ.get("MINERU_TASK_BASE")
    or "http://localhost:8770"
).rstrip("/")
SUBMIT_URL = f"{API_BASE}/two_stage/task"
LOG_FILE = "celery_two_stage.log"
DEFAULT_INPUT_DIR = Path("pdfs")
DEFAULT_OUTPUT_DIR = Path("pickle")
DEFAULT_INTERVAL = float(os.environ.get("TWO_STAGE_POLL_INTERVAL", 1))
DEFAULT_TIMEOUT = float(os.environ.get("TWO_STAGE_POLL_TIMEOUT", 800))
MAX_ATTEMPTS = 3  # Retry only confirmed terminal failures.
MAX_IN_FLIGHT = int(os.environ.get("TWO_STAGE_MAX_IN_FLIGHT", 6))

VISION_PROVIDER = (os.environ.get("VISION_PROVIDER") or "").strip()
VISION_MODEL = (os.environ.get("VISION_MODEL") or "").strip()
VISION_PROMPT = (os.environ.get("VISION_PROMPT") or "").strip()
PRIORITY = (os.environ.get("TWO_STAGE_PRIORITY") or "normal").strip().lower() or "normal"


def _bool_env(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


CHUNK_TYPE = _bool_env("TWO_STAGE_CHUNK_TYPE", False)
RETURN_TXT = _bool_env("TWO_STAGE_RETURN_TXT", False)


def _build_form_data() -> Dict[str, str]:
    form: Dict[str, str] = {}
    form["priority"] = PRIORITY
    if VISION_PROVIDER:
        form["provider"] = VISION_PROVIDER
    if VISION_MODEL:
        form["model"] = VISION_MODEL
    if VISION_PROMPT:
        form["prompt"] = VISION_PROMPT
    if CHUNK_TYPE:
        form["chunk_type"] = "true"
    if RETURN_TXT:
        form["return_txt"] = "true"
    return form


def submit_task(session: requests.Session, pdf_path: Path, token: str) -> str:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    form_data = _build_form_data()
    logging.info(
        "Submitting %s with priority=%s provider=%s model=%s",
        pdf_path,
        form_data.get("priority", "<default>"),
        form_data.get("provider", "<default>"),
        form_data.get("model", "<default>"),
    )
    with pdf_path.open("rb") as f:
        resp = session.post(
            SUBMIT_URL,
            files={"file": f},
            data=form_data,
            headers=headers,
            timeout=120,
        )
    resp.raise_for_status()
    data = resp.json()
    task_id = data.get("task_id")
    if not task_id:
        raise RuntimeError(f"Task ID missing in response for {pdf_path}")
    logging.info("Submitted %s -> task %s", pdf_path, task_id)
    return task_id


def fetch_status(
    session: requests.Session,
    task_id: str,
    token: str,
) -> Dict:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    resp = session.get(
        f"{API_BASE}/two_stage/task/{task_id}",
        headers=headers,
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def iter_pdfs(input_dir: Path) -> Iterable[Path]:
    if not input_dir.exists():
        return []
    for path in input_dir.iterdir():
        if path.is_file() and path.suffix.lower() == ".pdf":
            yield path


def _bearer_token() -> str:
    token = (os.getenv("FASTAPI_BEARER_TOKEN") or "").strip()
    if not token:
        secrets = Path(".secrets/secrets.toml")
        if secrets.is_file():
            with secrets.open("rb") as stream:
                token = (tomllib.load(stream).get("FASTAPI", {}).get("BEARER_TOKEN") or "").strip()
    if not token:
        raise RuntimeError("Configure FASTAPI_BEARER_TOKEN or FASTAPI.BEARER_TOKEN in secrets.toml")
    return token


def _atomic_write(path: Path, value, *, binary=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            data = pickle.dumps(value) if binary else json.dumps(value, ensure_ascii=False).encode()
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def run_batch(
    session,
    paths,
    token,
    output_dir: Path,
    *,
    max_in_flight=MAX_IN_FLIGHT,
    poll_interval=DEFAULT_INTERVAL,
    poll_timeout=DEFAULT_TIMEOUT,
    max_attempts=MAX_ATTEMPTS,
):
    """Feed a bounded window, retaining task IDs across restarts and poll errors.

    The CLI holds an exclusive output-directory lock. A timeout stops local
    waiting; it never cancels or resubmits a possibly running server task.
    """
    if max_in_flight < 1 or poll_timeout <= 0 or poll_interval < 0 or max_attempts < 1:
        raise ValueError("Invalid batch limits")
    output_dir = Path(output_dir)
    state_dir = output_dir / ".tasks"
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    waiting = deque()
    active = {}
    summary = {"successes": 0, "failures": 0, "skipped": 0}
    stems = set()
    request = {"api_base": API_BASE, "form": _build_form_data()}

    # Validate all resumptions before submitting new work.
    for path in paths:
        path = Path(path)
        if path.stem in stems:
            raise ValueError(f"Duplicate output name: {path.stem}")
        stems.add(path.stem)
        if (output_dir / f"{path.stem}.pkl").is_file():
            summary["skipped"] += 1
            continue
        with path.open("rb") as source:
            fingerprint = hashlib.file_digest(source, "sha256").hexdigest()
        state_path = state_dir / f"{path.stem}.json"
        if state_path.exists():
            state = json.loads(state_path.read_text())
            if state["sha256"] != fingerprint or state["request"] != request:
                raise ValueError(f"Input or request changed for {path}; use a new output directory")
            if state["state"] == "SUBMITTING":
                raise RuntimeError(f"Submission outcome unknown for {path}; journal: {state_path}")
        else:
            state = {"sha256": fingerprint, "request": request, "attempts": 0, "state": "NEW"}
        record = {"path": path, "state_path": state_path, "state": state}
        if state.get("task_id") and state["state"] in {"SUBMITTED", "SUCCESS"}:
            record["deadline"] = time.monotonic() + poll_timeout
            active[state["task_id"]] = record
        else:
            waiting.append(record)

    while waiting or active:
        while waiting and len(active) < max_in_flight:
            record = waiting.popleft()
            path, state = record["path"], record["state"]
            if state["attempts"] >= max_attempts:
                summary["failures"] += 1
                logging.error("Attempt limit reached for %s", path)
                continue
            state.update(state="SUBMITTING", attempts=state["attempts"] + 1, task_id=None)
            _atomic_write(record["state_path"], state)
            try:
                task_id = submit_task(session, path, token)
            except Exception as exc:
                # POST might have reached the server. A second upload could
                # duplicate expensive work, so retain the ambiguous journal.
                raise RuntimeError(
                    f"Submission outcome unknown for {path}; journal: {record['state_path']}"
                ) from exc
            state.update(state="SUBMITTED", task_id=task_id)
            _atomic_write(record["state_path"], state)
            record["deadline"] = time.monotonic() + poll_timeout
            active[task_id] = record

        finished = False
        for task_id, record in list(active.items()):
            try:
                data = fetch_status(session, task_id, token)
            except requests.RequestException as exc:
                if exc.response is not None and exc.response.status_code in {401, 403}:
                    raise
                logging.warning("Status query failed for %s; retaining the task ID", task_id)
                data = {"state": "PENDING"}
            status = data.get("state")
            if status == "SUCCESS":
                result = data.get("result")
                if result is None:
                    raise RuntimeError(f"Task {task_id} succeeded without a result")
                _atomic_write(output_dir / f"{record['path'].stem}.pkl", result, binary=True)
                record["state"]["state"] = "SUCCESS"
                _atomic_write(record["state_path"], record["state"])
                summary["successes"] += 1
                del active[task_id]
                finished = True
            elif status in {"FAILURE", "REVOKED"}:
                record["state"]["state"] = "FAILED"
                _atomic_write(record["state_path"], record["state"])
                logging.error("Task %s failed for %s", task_id, record["path"])
                del active[task_id]
                waiting.append(record)
                finished = True
            elif status not in {"PENDING", "STARTED", "RETRY", "RECEIVED"}:
                raise RuntimeError(f"Unexpected state {status!r} for task {task_id}")
            elif time.monotonic() >= record["deadline"]:
                raise TimeoutError(
                    f"Stopped waiting for {task_id}; server task was not cancelled. "
                    "Run again with the same output directory to resume."
                )
        # Refill immediately when there is room; don't wait for the slowest PDF
        # in a fixed batch. Otherwise limit the status-query rate.
        if active and not (finished and waiting and len(active) < max_in_flight):
            time.sleep(poll_interval)
    return summary


def main() -> None:
    import fcntl

    logging.basicConfig(
        filename=LOG_FILE,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    token = _bearer_token()
    input_dir = Path(
        os.environ.get("TWO_STAGE_INPUT_DIR")
        or os.environ.get("ESG_INPUT_DIR")
        or DEFAULT_INPUT_DIR
    )
    output_dir = Path(
        os.environ.get("TWO_STAGE_OUTPUT_DIR")
        or os.environ.get("ESG_OUTPUT_DIR")
        or DEFAULT_OUTPUT_DIR
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    if not input_dir.is_dir():
        raise FileNotFoundError(f"Input directory does not exist: {input_dir}")
    with (output_dir / ".batch.lock").open("a") as lock, requests.Session() as session:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(f"Another batch writer owns {output_dir}") from exc
        summary = run_batch(session, sorted(iter_pdfs(input_dir)), token, output_dir)
    logging.info("Batch finished: %s", summary)
    print(json.dumps(summary))
    if summary["failures"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
