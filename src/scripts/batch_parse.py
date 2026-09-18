"""Resumable CLI for all three asynchronous parsing APIs.

Run from the repository: uv run python -m src.scripts.batch_parse --help
"""

import argparse
from contextlib import nullcontext
from dataclasses import dataclass
import fcntl
import json
import logging
import math
import os
from pathlib import Path
import tomllib
from urllib.parse import quote, urlsplit
import uuid

from dotenv import load_dotenv
import httpx

from src.scripts.batch_runner import _atomic_write, run_batch
from src.utils.file_conversion import CONVERTIBLE_OFFICE_EXTENSIONS
from src.utils.mineru_support import mineru_supported_extensions

ENDPOINTS = {
    "parse": "/mineru/task",
    "images": "/mineru_with_images/task",
    "two-stage": "/two_stage/task",
}
ROOT = Path(__file__).resolve().parents[2]


@dataclass
class Options:
    input_dir: Path
    output_dir: Path
    base_url: str = "http://127.0.0.1:7770"
    mode: str = "parse"
    tier: str = "advanced"
    priority: str = "normal"
    chunk_type: bool = True
    return_txt: bool = False
    provider: str | None = None
    model: str | None = None
    prompt: str | None = None
    recursive: bool = False
    extensions: str = "pdf"
    max_in_flight: int = 2
    max_attempts: int = 1
    poll_interval: float = 5
    poll_timeout: float = 21600
    upload_timeout: float = 600
    query_timeout: float = 60
    connect_timeout: float = 10
    no_auth: bool = False
    dry_run: bool = False


def validate(opts):
    if opts.mode not in ENDPOINTS or opts.tier not in {"flash", "basic", "standard", "advanced"}:
        raise ValueError("Invalid mode or tier")
    if opts.priority not in {"normal", "urgent"}:
        raise ValueError("Invalid priority")
    if opts.mode == "parse" and any((opts.provider, opts.model, opts.prompt)):
        raise ValueError("Vision options require images or two-stage mode")
    for name in (
        "max_in_flight",
        "max_attempts",
        "poll_timeout",
        "upload_timeout",
        "query_timeout",
        "connect_timeout",
    ):
        value = getattr(opts, name)
        if not math.isfinite(value) or value <= 0:
            raise ValueError(f"{name} must be positive and finite")
    if not math.isfinite(opts.poll_interval) or opts.poll_interval < 0:
        raise ValueError("poll_interval must be non-negative and finite")
    url = urlsplit(opts.base_url)
    if (
        url.scheme not in {"http", "https"}
        or not url.hostname
        or url.query
        or url.fragment
        or url.username is not None
    ):
        raise ValueError("base_url must be an HTTP(S) URL without credentials/query/fragment")


def discover(opts):
    root = Path(opts.input_dir).resolve()
    if not root.is_dir():
        raise ValueError(f"Input directory does not exist: {root}")
    allowed = mineru_supported_extensions() | CONVERTIBLE_OFFICE_EXTENSIONS
    selected = (
        allowed
        if opts.extensions == "all"
        else {"." + value.strip().lower().lstrip(".") for value in opts.extensions.split(",")}
    )
    if not selected <= allowed:
        raise ValueError(f"Unsupported extensions: {sorted(selected - allowed)}")
    paths = sorted(
        p
        for p in (root.rglob("*") if opts.recursive else root.iterdir())
        if p.is_file() and p.suffix.lower() in selected
    )
    if not paths:
        raise ValueError("No supported input files found")
    return root, paths


def bearer_token():
    value = (os.getenv("FASTAPI_BEARER_TOKEN") or "").strip()
    secrets = ROOT / ".secrets/secrets.toml"
    if not value and secrets.is_file():
        with secrets.open("rb") as stream:
            value = (tomllib.load(stream).get("FASTAPI", {}).get("BEARER_TOKEN") or "").strip()
    if not value:
        raise ValueError(
            "Configure FASTAPI_BEARER_TOKEN or local secrets; use --no-auth only for an unauthenticated API"
        )
    return value


class TaskAPI:
    def __init__(self, opts, root, batch_id):
        self.opts, self.root, self.batch_id = opts, root, batch_id
        self.url = opts.base_url.rstrip("/") + ENDPOINTS[opts.mode]
        self.form = {"tier": opts.tier, "priority": opts.priority}
        flags = {
            "chunk_type": str(opts.chunk_type).lower(),
            "return_txt": str(opts.return_txt).lower(),
        }
        self.params = {} if opts.mode == "two-stage" else flags
        if opts.mode == "two-stage":
            self.form.update(flags)
        for name in ("provider", "model", "prompt"):
            value = getattr(opts, name)
            if value:
                self.form[name] = value

    def identity(self):
        return {"url": self.url, "params": self.params, "form": self.form, "format": "json"}

    def submit(self, client, path, token):
        form = dict(self.form)
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        with path.open("rb") as stream:
            response = client.post(
                self.url,
                params=self.params,
                data=form,
                files={"file": (path.name, stream, "application/octet-stream")},
                headers=headers,
                timeout=httpx.Timeout(self.opts.upload_timeout, connect=self.opts.connect_timeout),
            )
        response.raise_for_status()
        data = response.json()
        task_id = data.get("task_id") if isinstance(data, dict) else None
        if not isinstance(task_id, str) or not task_id.strip():
            raise ValueError("Submission response missing task_id")
        logging.info("Submitted %s -> %s", path.name, task_id)
        return task_id

    def fetch(self, client, task_id, token):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        response = client.get(
            f"{self.url}/{quote(task_id, safe='')}",
            headers=headers,
            timeout=httpx.Timeout(self.opts.query_timeout, connect=self.opts.connect_timeout),
        )
        if response.status_code in {401, 403}:
            response.raise_for_status()
        try:
            data = response.json()
        except ValueError:
            response.raise_for_status()
            raise ValueError(f"Invalid JSON for task {task_id}") from None
        # Ordinary tasks report a real terminal failure as HTTP 500.
        if (
            isinstance(data, dict)
            and data.get("state") in {"FAILURE", "REVOKED"}
            and response.status_code in {200, 500}
        ):
            return data
        response.raise_for_status()
        if not isinstance(data, dict):
            raise ValueError(f"Invalid task response for {task_id}")
        if data.get("state") == "SUCCESS":
            result = data.get("result")
            if not isinstance(result, dict) or not isinstance(result.get("result"), list):
                raise ValueError(f"Task {task_id} succeeded without a business result list")
        return data


def run(opts, *, client=None, token=None):
    validate(opts)
    root, paths = discover(opts)
    # Validate destination configuration even for dry-run, before making a POST.
    api = TaskAPI(opts, root, "")
    if opts.dry_run:
        return {
            "files": len(paths),
            "bytes": sum(p.stat().st_size for p in paths),
            "mode": opts.mode,
            "tier": opts.tier,
            "max_in_flight": opts.max_in_flight,
        }
    if token is None:
        token = "" if opts.no_auth else bearer_token()
    output = Path(opts.output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    with (output / ".batch.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError(f"Another batch writer owns {output}") from None
        manifest_path = output / ".batch.json"
        identity = {"version": 1, "input_dir": str(root), "request": api.identity()}
        if manifest_path.exists():
            manifest = json.loads(manifest_path.read_text())
            if manifest["identity"] != identity:
                raise ValueError("Input directory or request changed; use a new output directory")
            api.batch_id = manifest["batch_id"]
        else:
            api.batch_id = uuid.uuid4().hex
            _atomic_write(manifest_path, {"identity": identity, "batch_id": api.batch_id})
        with (
            nullcontext(client) if client is not None else httpx.Client(follow_redirects=False)
        ) as session:
            return run_batch(
                session,
                paths,
                token,
                output,
                max_in_flight=opts.max_in_flight,
                poll_interval=opts.poll_interval,
                poll_timeout=opts.poll_timeout,
                max_attempts=opts.max_attempts,
                request=api.identity(),
                submit=api.submit,
                fetch=api.fetch,
                key_for_path=lambda p: p.relative_to(root).as_posix(),
                output_format="json",
                strict=True,
            )


def main(argv=None):
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--base-url", default=os.getenv("BATCH_API_BASE", "http://127.0.0.1:7770"))
    parser.add_argument("--mode", choices=ENDPOINTS, default="parse")
    parser.add_argument(
        "--tier", choices=["flash", "basic", "standard", "advanced"], default="advanced"
    )
    parser.add_argument("--priority", choices=["normal", "urgent"], default="normal")
    parser.add_argument("--chunk-type", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--return-txt", action="store_true")
    for name in ("provider", "model", "prompt"):
        parser.add_argument("--" + name)
    parser.add_argument(
        "--extensions",
        default="pdf",
        help="Comma-separated extensions, or all supported PDF/image/Office formats",
    )
    for name in ("recursive", "no-auth", "dry-run"):
        parser.add_argument("--" + name, action="store_true")
    for name, default in (("max-in-flight", 2), ("max-attempts", 1)):
        parser.add_argument("--" + name, type=int, default=default)
    for name, default in (
        ("poll-interval", 5),
        ("poll-timeout", 21600),
        ("upload-timeout", 600),
        ("query-timeout", 60),
        ("connect-timeout", 10),
    ):
        parser.add_argument("--" + name, type=float, default=default)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        summary = run(Options(**vars(args)))
    except (ValueError, RuntimeError, OSError, httpx.HTTPError) as exc:
        # Never print HTTP request bodies, tokens or credential-bearing tracebacks.
        logging.error("%s", exc)
        raise SystemExit(1) from None
    print(json.dumps(summary, ensure_ascii=False))
    if summary.get("failures"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
