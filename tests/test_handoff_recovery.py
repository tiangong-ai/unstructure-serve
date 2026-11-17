"""The shared parse recovery index covers only jobs that may need a follow-up."""

import os
from pathlib import Path

import pytest

from src.services import job_store

WORKFLOW = "shared-parse"


def _job(tmp_path):
    source = tmp_path / "source.pdf"
    source.write_bytes(b"%PDF")
    return job_store.create_job("parse", source, {})


def _index(job):
    return job_store.store_root() / ".handoffs" / f"{job['job_id']}.{job['generation']}.json"


def test_index_write_failure_prevents_broker_publication_and_preserves_job(tmp_path, monkeypatch):
    job = _job(tmp_path)
    write = job_store.atomic_json
    sent = []

    def fail_index(path, value):
        if Path(path) == _index(job):
            raise OSError("local write failed")
        return write(path, value)

    monkeypatch.setattr(job_store, "atomic_json", fail_index)
    with pytest.raises(job_store.PublishUncertain) as error:
        job_store.publish_job(job["job_id"], sent.append, workflow=WORKFLOW)
    assert error.value.job_id == job["job_id"]
    assert sent == []
    assert job_store.source_path(job["job_id"]).is_file()
    assert job_store.status(job["job_id"])["publication"] == "uncertain"
    assert not _index(job).exists()


def test_broker_error_leaves_preexisting_index_without_autoretrying_initial_publish(
    tmp_path, monkeypatch
):
    job = _job(tmp_path)
    ref = {"job_id": job["job_id"], "generation": 0}

    def broker_failure(record):
        assert job_store.read_json(_index(record)) == ref
        raise OSError("broker response lost")

    with pytest.raises(job_store.PublishUncertain):
        job_store.publish_job(job["job_id"], broker_failure, workflow=WORKFLOW)
    assert job_store.read_json(_index(job)) == ref
    assert job_store.status(job["job_id"])["publication"] == "uncertain"

    from src.services import shared_parse
    from src.services.handoff_recovery import HandoffRecovery

    def do_not_publish(*args, **kwargs):
        pytest.fail("Automatic recovery must not publish an unconfirmed generation")

    # The broker can deliver a message even though its acknowledgement was lost.
    job_store.start_job(job["job_id"])
    manifest = job_store.job_dir(job["job_id"]) / "parse" / "manifest.json"
    job_store.atomic_json(manifest, {})
    monkeypatch.setattr(shared_parse, "recover_handoff", do_not_publish)
    worker = HandoffRecovery()
    try:
        assert worker.scan(limit=64) == 1
        assert _index(job).is_file()
    finally:
        worker.close()


def test_recovery_cleans_old_generation_without_touching_new_index(tmp_path, monkeypatch):
    from src.services import shared_parse
    from src.services.handoff_recovery import HandoffRecovery

    old = _job(tmp_path)
    job_store.publish_job(old["job_id"], lambda record: None, workflow=WORKFLOW)
    new = job_store.resume_job(old["job_id"])
    job_store.publish_job(new["job_id"], lambda record: None, workflow=WORKFLOW)

    def do_not_publish(*args, **kwargs):
        pytest.fail("Stale or unparsed jobs cannot receive a handoff")

    monkeypatch.setattr(shared_parse, "recover_handoff", do_not_publish)
    worker = HandoffRecovery()
    try:
        assert _index(old).is_file() and _index(new).is_file()
        assert worker.scan(limit=64) == 2
        assert not _index(old).exists()
        assert _index(new).is_file()
    finally:
        worker.close()


def test_recovery_cursor_advances_past_unparsed_jobs_within_limit(tmp_path, monkeypatch):
    from src.services import shared_parse
    from src.services.handoff_recovery import HandoffRecovery

    jobs = [_job(tmp_path) for _ in range(3)]
    for job in jobs:
        job_store.publish_job(job["job_id"], lambda record: None, workflow=WORKFLOW)
    with os.scandir(job_store.store_root() / ".handoffs") as entries:
        last = list(entries)[-1]
    ref = job_store.read_json(last.path)
    job_store.start_job(ref["job_id"])
    job_store.atomic_json(job_store.job_dir(ref["job_id"]) / "parse" / "manifest.json", {})
    recovered = []

    def recover(job_id, *, expected_generation):
        recovered.append((job_id, expected_generation))
        job_store.record_handoff(job_id, expected_generation, "published")

    monkeypatch.setattr(shared_parse, "recover_handoff", recover)
    worker = HandoffRecovery()
    try:
        assert worker.scan(limit=1) == 1
        assert worker.scan(limit=1) == 1
        assert recovered == []
        assert worker.scan(limit=1) == 1
        assert recovered == [(ref["job_id"], ref["generation"])]
        assert not Path(last.path).exists()
    finally:
        worker.close()


def test_recovery_removes_terminal_parse_failure_without_retry(tmp_path, monkeypatch):
    from src.services import shared_parse
    from src.services.handoff_recovery import HandoffRecovery

    job = _job(tmp_path)
    job_store.publish_job(job["job_id"], lambda record: None, workflow=WORKFLOW)
    job_store.fail_job(job["job_id"], RuntimeError("real parse error"))

    def forbidden(*args, **kwargs):
        pytest.fail("A terminal parse error needs explicit resume")

    monkeypatch.setattr(shared_parse, "recover_handoff", forbidden)
    worker = HandoffRecovery()
    try:
        assert worker.scan(limit=64) == 1
        assert not _index(job).exists()
    finally:
        worker.close()


def test_recovery_failure_log_identifies_index_without_private_details(
    tmp_path, monkeypatch, caplog
):
    from src.services import shared_parse
    from src.services.handoff_recovery import HandoffRecovery

    failed, healthy = _job(tmp_path), _job(tmp_path)
    for job in (failed, healthy):
        job_id = job["job_id"]
        job_store.publish_job(job_id, lambda record: None, workflow=WORKFLOW)
        job_store.start_job(job_id)
        job_store.atomic_json(job_store.job_dir(job_id) / "parse" / "manifest.json", {})

    def recover(job_id, *, expected_generation):
        if job_id == failed["job_id"]:
            raise RuntimeError("private path or configuration")
        job_store.record_handoff(job_id, expected_generation, "published")

    monkeypatch.setattr(shared_parse, "recover_handoff", recover)
    worker = HandoffRecovery()
    try:
        assert worker.scan(limit=64) == 2
        assert _index(failed).is_file()
        assert not _index(healthy).exists()
    finally:
        worker.close()
    assert failed["job_id"] in caplog.text
    assert "generation=0" in caplog.text
    assert "type=RuntimeError" in caplog.text
    assert "private" not in caplog.text
