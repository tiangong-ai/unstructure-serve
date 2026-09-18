"""Shared rolling task runner; no API/provider assumptions or credential storage."""

import hashlib
import json
import logging
import os
import pickle
import tempfile
import time
from collections import deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path

import httpx
import requests


class InputChangedBeforeUpload(ValueError):
    """A verified local failure that occurred before any POST was sent."""


def _atomic_write(path: Path, value, *, binary=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = "wb" if binary else "w"
    with tempfile.NamedTemporaryFile(
        dir=path.parent, delete=False, mode=mode, encoding=None if binary else "utf-8"
    ) as stream:
        temporary = Path(stream.name)
        try:
            if binary:
                pickle.dump(value, stream)
            else:
                json.dump(value, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
            os.replace(temporary, path)
            directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            temporary.unlink(missing_ok=True)


def run_batch(
    session,
    paths,
    token,
    output_dir: Path,
    *,
    max_in_flight=6,
    poll_interval=1,
    poll_timeout=800,
    max_attempts=3,
    request,
    submit,
    fetch,
    key_for_path=lambda path: path.stem,
    output_format="pickle",
    strict=False,
    resume_only=False,
    batch_id=None,
    upload_concurrency=1,
    query_concurrency=1,
):
    """Feed a bounded window, retaining task IDs across restarts and poll errors.

    The CLI holds an exclusive output-directory lock. A timeout stops local
    waiting; it never cancels or resubmits a possibly running server task.
    """
    if (
        max_in_flight < 1
        or poll_timeout <= 0
        or poll_interval < 0
        or max_attempts < 1
        or upload_concurrency < 1
        or query_concurrency < 1
    ):
        raise ValueError("Invalid batch limits")
    output_dir = Path(output_dir)
    state_dir = output_dir / ".tasks"
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    waiting = deque()
    active = {}
    summary = {"successes": 0, "failures": 0, "skipped": 0}
    if resume_only:
        summary["deferred"] = 0
    stems = set()
    suffix = ".pkl" if output_format == "pickle" else ".json"
    result_dir = output_dir / "results" if strict else output_dir
    journal_paths = set()

    # Validate all resumptions before submitting new work.
    for path in paths:
        path = Path(path)
        key = key_for_path(path)
        if Path(key).is_absolute() or ".." in Path(key).parts:
            raise ValueError("Unsafe output key")
        if key in stems:
            raise ValueError(f"Duplicate output name: {path.stem}")
        stems.add(key)
        result_path = result_dir / f"{key}{suffix}"
        if not strict and result_path.is_file():
            summary["skipped"] += 1
            continue
        with path.open("rb") as source:
            fingerprint = hashlib.file_digest(source, "sha256").hexdigest()
        state_path = state_dir / f"{key}.json"
        journal_paths.add(state_path)
        if state_path.exists():
            state = json.loads(state_path.read_text())
            if state["sha256"] != fingerprint or state["request"] != request:
                raise ValueError(f"Input or request changed for {path}; use a new output directory")
            if state["state"] == "SUBMITTING":
                raise RuntimeError(f"Submission outcome unknown for {path}; journal: {state_path}")
        else:
            if strict and result_path.exists():
                raise ValueError(f"Result without journal: {result_path}")
            state = {"sha256": fingerprint, "request": request, "attempts": 0, "state": "NEW"}
        if strict and state["state"] not in {"NEW", "SUBMITTED", "SUCCESS", "FAILED"}:
            raise ValueError(f"Invalid journal state: {state_path}")
        if strict and state["state"] in {"SUBMITTED", "SUCCESS"} and not state.get("task_id"):
            raise ValueError(f"Missing task ID: {state_path}")
        if strict and state["state"] == "SUCCESS" and result_path.is_file():
            with result_path.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            if digest == state.get("result_sha256"):
                summary["skipped"] += 1
                continue
        record = {
            "key": key,
            "path": path,
            "state_path": state_path,
            "state": state,
            "result_path": result_path,
        }
        if state.get("task_id") and state["state"] in {"SUBMITTED", "SUCCESS"}:
            record["deadline"] = time.monotonic() + poll_timeout
            active[state["task_id"]] = record
        elif resume_only:
            summary["failures" if state["state"] == "FAILED" else "deferred"] += 1
        else:
            waiting.append(record)

    if strict and any(path not in journal_paths for path in state_dir.rglob("*.json")):
        raise ValueError(
            "Previously journaled input is missing or filtered out; restore the input selection before resuming"
        )

    if batch_id is not None:
        return _run_parallel(
            session,
            token,
            output_dir,
            waiting,
            active,
            summary,
            batch_id=batch_id,
            max_in_flight=max_in_flight,
            max_attempts=max_attempts,
            poll_interval=poll_interval,
            poll_timeout=poll_timeout,
            upload_concurrency=upload_concurrency,
            query_concurrency=query_concurrency,
            submit=submit,
            fetch=fetch,
            output_format=output_format,
            strict=strict,
            resume_only=resume_only,
        )

    # Preserve the legacy requests.Session contract: it is not shared by threads.
    while waiting or active:
        while waiting and len(active) < max_in_flight:
            record = waiting.popleft()
            path, state = record["path"], record["state"]
            if state["attempts"] >= max_attempts:
                summary["failures"] += 1
                logging.error("Attempt limit reached for %s", path)
                continue
            if state.get("task_id"):
                state.setdefault("previous_task_ids", []).append(state["task_id"])
            state.update(state="SUBMITTING", attempts=state["attempts"] + 1, task_id=None)
            _atomic_write(record["state_path"], state)
            try:
                task_id = submit(session, path, token)
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
                data = fetch(session, task_id, token)
            except (requests.RequestException, httpx.HTTPError) as exc:
                response = getattr(exc, "response", None)
                if response is not None and response.status_code not in {
                    408,
                    429,
                    500,
                    502,
                    503,
                    504,
                }:
                    raise
                logging.warning("Status query failed for %s; retaining the task ID", task_id)
                data = {"state": "PENDING"}
            status = data.get("state")
            if status == "SUCCESS":
                result = data.get("result")
                if result is None:
                    raise RuntimeError(f"Task {task_id} succeeded without a result")
                _atomic_write(record["result_path"], result, binary=output_format == "pickle")
                if strict:
                    with record["result_path"].open("rb") as stream:
                        record["state"]["result_sha256"] = hashlib.file_digest(
                            stream, "sha256"
                        ).hexdigest()
                record["state"]["state"] = "SUCCESS"
                _atomic_write(record["state_path"], record["state"])
                summary["successes"] += 1
                logging.info("Saved %s (task %s)", record["result_path"], task_id)
                del active[task_id]
                finished = True
            elif status in {"FAILURE", "REVOKED"}:
                record["state"]["state"] = "FAILED"
                _atomic_write(record["state_path"], record["state"])
                logging.error("Task %s failed for %s", task_id, record["path"])
                del active[task_id]
                if resume_only:
                    summary["failures"] += 1
                else:
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


def _run_parallel(
    session,
    token,
    output_dir,
    waiting,
    active,
    summary,
    *,
    batch_id,
    max_in_flight,
    max_attempts,
    poll_interval,
    poll_timeout,
    upload_concurrency,
    query_concurrency,
    submit,
    fetch,
    output_format,
    strict,
    resume_only,
):
    """Only the caller writes journals; independent bounded pools perform HTTP.

    In-progress uploads occupy the same document window as accepted jobs. If any
    operation fails, stop issuing work and retain IDs from uploads already sent.
    """
    uploads = {}
    queries = {}
    querying = set()

    def accept_upload(future, record):
        try:
            task_id = future.result()
        except InputChangedBeforeUpload:
            record["state"] = record.pop("before_submit")
            _atomic_write(record["state_path"], record["state"])
            raise
        except Exception as exc:
            raise RuntimeError(
                f"Submission outcome unknown for {record['path']}; "
                f"journal: {record['state_path']}"
            ) from exc
        state = record["state"]
        state.update(state="SUBMITTED", task_id=task_id)
        _atomic_write(record["state_path"], state)
        record.pop("before_submit", None)
        record["deadline"] = time.monotonic() + poll_timeout
        record["next_poll"] = 0
        if task_id in active:
            raise RuntimeError(f"Server returned duplicate task ID for different inputs: {task_id}")
        active[task_id] = record

    def accept_query(future, task_id):
        record = active[task_id]
        try:
            data = future.result()
        except (requests.RequestException, httpx.HTTPError) as exc:
            response = getattr(exc, "response", None)
            if response is not None and response.status_code not in {408, 429, 500, 502, 503, 504}:
                raise
            logging.warning("Status query failed for %s; retaining the task ID", task_id)
            data = {"state": "PENDING"}
        status = data.get("state")
        if status == "SUCCESS":
            result = data.get("result")
            if result is None:
                raise RuntimeError(f"Task {task_id} succeeded without a result")
            _atomic_write(record["result_path"], result, binary=output_format == "pickle")
            if strict:
                with record["result_path"].open("rb") as stream:
                    record["state"]["result_sha256"] = hashlib.file_digest(
                        stream, "sha256"
                    ).hexdigest()
            record["state"]["state"] = "SUCCESS"
            _atomic_write(record["state_path"], record["state"])
            summary["successes"] += 1
            logging.info("Saved %s (task %s)", record["result_path"], task_id)
            del active[task_id]
        elif status in {"FAILURE", "REVOKED"}:
            record["state"]["state"] = "FAILED"
            _atomic_write(record["state_path"], record["state"])
            logging.error("Task %s failed for %s", task_id, record["path"])
            del active[task_id]
            if resume_only:
                summary["failures"] += 1
            else:
                waiting.append(record)
        elif status not in {"PENDING", "STARTED", "RETRY", "RECEIVED"}:
            raise RuntimeError(f"Unexpected state {status!r} for task {task_id}")
        else:
            record["next_poll"] = time.monotonic() + poll_interval

    with (
        ThreadPoolExecutor(max_workers=min(upload_concurrency, max_in_flight)) as upload_pool,
        ThreadPoolExecutor(max_workers=query_concurrency) as query_pool,
    ):
        try:
            while waiting or active or uploads or queries:
                # Persist completed operations before submitting replacements.
                pending_queries = False
                for future in list(uploads):
                    if future.done():
                        accept_upload(future, uploads.pop(future))
                for future in list(queries):
                    if future.done():
                        task_id = queries.pop(future)
                        querying.remove(task_id)
                        accept_query(future, task_id)
                        pending_queries |= task_id in active

                for task_id, record in active.items():
                    if time.monotonic() >= record["deadline"]:
                        raise TimeoutError(
                            f"Stopped waiting for {task_id}; server task was not cancelled. "
                            "Run again with the same output directory to resume."
                        )

                while (
                    waiting
                    and len(active) + len(uploads) < max_in_flight
                    and len(uploads) < upload_concurrency
                ):
                    record = waiting.popleft()
                    state = record["state"]
                    if state["attempts"] >= max_attempts:
                        summary["failures"] += 1
                        logging.error("Attempt limit reached for %s", record["path"])
                        continue
                    record["before_submit"] = dict(state)
                    if state.get("task_id"):
                        state["previous_task_ids"] = [
                            *state.get("previous_task_ids", []),
                            state["task_id"],
                        ]
                    state.update(state="SUBMITTING", attempts=state["attempts"] + 1, task_id=None)
                    identity = json.dumps(
                        [batch_id, record["key"], state["attempts"]], separators=(",", ":")
                    )
                    state["idempotency_key"] = hashlib.sha256(identity.encode()).hexdigest()
                    _atomic_write(record["state_path"], state)
                    future = upload_pool.submit(
                        submit,
                        session,
                        record["path"],
                        token,
                        idempotency_key=state["idempotency_key"],
                        expected_sha256=state["sha256"],
                        snapshot_dir=output_dir / ".uploads",
                    )
                    uploads[future] = record

                # Poll fairly by due time, rather than repeatedly preferring the
                # first document when more IDs exist than query threads.
                for task_id, record in sorted(
                    active.items(), key=lambda item: item[1].get("next_poll", 0)
                ):
                    if len(queries) >= query_concurrency:
                        break
                    if task_id not in querying and record.get("next_poll", 0) <= time.monotonic():
                        queries[query_pool.submit(fetch, session, task_id, token)] = task_id
                        querying.add(task_id)

                futures = [*uploads, *queries]
                if futures:
                    wait(futures, timeout=0.05, return_when=FIRST_COMPLETED)
                    if pending_queries and poll_interval == 0:
                        time.sleep(0)
                elif active:
                    now = time.monotonic()
                    next_wake = min(
                        min(record.get("next_poll", 0), record["deadline"])
                        for record in active.values()
                    )
                    time.sleep(min(0.05, max(0, next_wake - now)))
        finally:
            # An upload in another thread may have succeeded after an error or
            # Ctrl-C. Wait for its bounded HTTP call and preserve its ID before
            # releasing the output lock. Never submit another task here.
            for future, record in list(uploads.items()):
                try:
                    accept_upload(future, record)
                except Exception:
                    logging.warning("Retained submission journal for %s", record["path"])
    return summary
