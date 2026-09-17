import json
import pickle
from pathlib import Path

import pytest
import requests

from src.scripts import two_stage_enqueue as batch


def test_token_uses_environment_then_local_secrets(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".secrets").mkdir()
    (tmp_path / ".secrets/secrets.toml").write_text('[FASTAPI]\nBEARER_TOKEN="local-token"\n')
    monkeypatch.setenv("FASTAPI_BEARER_TOKEN", " env-token ")
    assert batch._bearer_token() == "env-token"
    monkeypatch.setenv("FASTAPI_BEARER_TOKEN", "")
    assert batch._bearer_token() == "local-token"


def sources(tmp_path, count=4):
    paths = []
    for i in range(count):
        path = tmp_path / f"{i}.pdf"
        path.write_bytes(b"%PDF-test")
        paths.append(path)
    return paths


def test_rolling_window_refills_without_waiting_for_slow_task(monkeypatch, tmp_path):
    paths = sources(tmp_path)
    output = tmp_path / "out"
    active = {}
    submitted = []
    maximum = []

    def submit(session, path, token):
        task_id = str(len(submitted))
        submitted.append(path)
        active[task_id] = 0
        maximum.append(len(active))
        return task_id

    def fetch(session, task_id, token):
        active[task_id] += 1
        if task_id == "0" and active[task_id] < 3:
            return {"state": "STARTED"}
        if task_id == "0":
            assert len(submitted) > 2
        del active[task_id]
        return {"state": "SUCCESS", "result": {"txt": task_id}}

    monkeypatch.setattr(batch, "submit_task", submit)
    monkeypatch.setattr(batch, "fetch_status", fetch)
    summary = batch.run_batch(None, paths, "token", output, max_in_flight=2, poll_interval=0)
    assert summary["successes"] == 4 and summary["failures"] == 0
    assert max(maximum) == 2
    assert pickle.loads((output / "0.pkl").read_bytes()) == {"txt": "0"}


def test_status_network_failure_retries_same_id(monkeypatch, tmp_path):
    paths = sources(tmp_path, 1)
    submitted = []
    calls = []

    def submit(*args):
        submitted.append(True)
        return "known-id"

    def fetch(*args):
        calls.append(True)
        if len(calls) == 1:
            raise requests.ConnectionError("temporary")
        return {"state": "SUCCESS", "result": {"txt": "ok"}}

    monkeypatch.setattr(batch, "submit_task", submit)
    monkeypatch.setattr(batch, "fetch_status", fetch)
    batch.run_batch(None, paths, "token", tmp_path / "out", poll_interval=0)
    assert len(submitted) == 1 and len(calls) == 2


def test_timeout_retains_id_and_next_run_resumes(monkeypatch, tmp_path):
    paths = sources(tmp_path, 1)
    output = tmp_path / "out"
    submitted = []
    clock = [0]
    monkeypatch.setattr(batch.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(batch.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + 1))

    def submit(*args):
        submitted.append(True)
        return "preserved-id"

    monkeypatch.setattr(batch, "submit_task", submit)
    monkeypatch.setattr(batch, "fetch_status", lambda *args: {"state": "PENDING"})
    with pytest.raises(TimeoutError):
        batch.run_batch(None, paths, "token", output, poll_interval=1, poll_timeout=2)
    state = json.loads((output / ".tasks/0.json").read_text())
    assert state["task_id"] == "preserved-id"
    monkeypatch.setattr(
        batch, "fetch_status", lambda *args: {"state": "SUCCESS", "result": {"txt": "ok"}}
    )
    batch.run_batch(None, paths, "token", output, poll_interval=0)
    assert len(submitted) == 1


def test_ambiguous_submit_is_not_automatically_repeated(monkeypatch, tmp_path):
    paths = sources(tmp_path, 1)
    output = tmp_path / "out"
    calls = []

    def submit(*args):
        calls.append(True)
        raise requests.Timeout("lost response")

    monkeypatch.setattr(batch, "submit_task", submit)
    with pytest.raises(RuntimeError, match="unknown"):
        batch.run_batch(None, paths, "token", output)
    with pytest.raises(RuntimeError, match="unknown"):
        batch.run_batch(None, paths, "token", output)
    assert len(calls) == 1


def test_only_terminal_failure_is_resubmitted_with_limit(monkeypatch, tmp_path):
    paths = sources(tmp_path, 1)
    submitted = []

    def submit(*args):
        submitted.append(True)
        return str(len(submitted))

    monkeypatch.setattr(batch, "submit_task", submit)
    monkeypatch.setattr(
        batch, "fetch_status", lambda *args: {"state": "FAILURE", "error": "failed"}
    )
    result = batch.run_batch(
        None, paths, "token", tmp_path / "out", poll_interval=0, max_attempts=2
    )
    assert result["failures"] == 1 and len(submitted) == 2


def test_resuming_changed_input_is_rejected(monkeypatch, tmp_path):
    paths = sources(tmp_path, 1)
    output = tmp_path / "out"
    monkeypatch.setattr(batch, "submit_task", lambda *args: "original-id")
    monkeypatch.setattr(
        batch, "fetch_status", lambda *args: (_ for _ in ()).throw(KeyboardInterrupt())
    )
    with pytest.raises(KeyboardInterrupt):
        batch.run_batch(None, paths, "token", output)
    paths[0].write_bytes(b"changed")
    with pytest.raises(ValueError, match="changed"):
        batch.run_batch(None, paths, "token", output)


def test_resuming_changed_request_is_rejected(monkeypatch, tmp_path):
    paths = sources(tmp_path, 1)
    output = tmp_path / "out"
    monkeypatch.setattr(batch, "submit_task", lambda *args: "original-id")
    monkeypatch.setattr(
        batch, "fetch_status", lambda *args: (_ for _ in ()).throw(KeyboardInterrupt())
    )
    with pytest.raises(KeyboardInterrupt):
        batch.run_batch(None, paths, "token", output)
    monkeypatch.setattr(batch, "CHUNK_TYPE", not batch.CHUNK_TYPE)
    with pytest.raises(ValueError, match="changed"):
        batch.run_batch(None, paths, "token", output)


def test_completed_result_can_be_fetched_again_without_resubmitting(monkeypatch, tmp_path):
    paths = sources(tmp_path, 1)
    output = tmp_path / "out"
    submitted = []

    def submit(*args):
        submitted.append(True)
        return "original-id"

    monkeypatch.setattr(batch, "submit_task", submit)
    monkeypatch.setattr(
        batch, "fetch_status", lambda *args: {"state": "SUCCESS", "result": {"txt": "ok"}}
    )
    batch.run_batch(None, paths, "token", output)
    (output / "0.pkl").unlink()
    batch.run_batch(None, paths, "token", output)
    assert len(submitted) == 1
