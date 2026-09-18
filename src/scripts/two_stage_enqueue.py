import json
import logging
import os
import time  # noqa: F401 - retained for legacy callers/tests controlling the clock
import tomllib
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
LOG_FILE = Path(
    os.environ.get("TWO_STAGE_LOG_FILE")
    or Path(__file__).resolve().parents[2] / "output/logs/celery_two_stage.log"
)
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
    # Keep the legacy request identity and pickle layout for existing journals.
    from src.scripts.batch_runner import run_batch as run

    return run(
        session,
        paths,
        token,
        output_dir,
        max_in_flight=max_in_flight,
        poll_interval=poll_interval,
        poll_timeout=poll_timeout,
        max_attempts=max_attempts,
        request={"api_base": API_BASE, "form": _build_form_data()},
        submit=submit_task,
        fetch=fetch_status,
    )


def main() -> None:
    import fcntl

    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
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
