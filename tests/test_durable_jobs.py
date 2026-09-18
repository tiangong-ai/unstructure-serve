import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
import pypdfium2 as pdfium

from src.services import job_store


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("MINERU_JOB_STORE_DIR", str(tmp_path / "jobs"))
    source = tmp_path / "source.pdf"
    document = pdfium.PdfDocument.new()
    document.new_page(595, 842).close()
    document.save(source)
    document.close()
    return source


def create(source, key="same-request", **options):
    return job_store.create_job(
        "parse", source, {"tier": "advanced", **options}, idempotency_key=key
    )


def test_concurrent_same_request_gets_one_durable_job(store):
    with ThreadPoolExecutor(max_workers=4) as pool:
        ids = list(pool.map(lambda _: create(store)["job_id"], range(8)))
    assert len(set(ids)) == 1
    record = job_store.read_job(ids[0])
    assert record["state"] == "PENDING"
    assert job_store.source_path(ids[0]).read_bytes() == store.read_bytes()
    assert len(list(job_store.store_root().glob("*/job.json"))) == 1


def test_idempotency_key_rejects_different_parameters_and_input(store):
    create(store)
    with pytest.raises(job_store.JobConflict):
        create(store, tier="basic")
    store.write_bytes(b"changed")
    with pytest.raises(job_store.JobConflict):
        create(store)


def test_result_survives_state_update_failure_and_contains_only_a_small_reference(
    store, monkeypatch
):
    job = create(store)
    payload = {"result": [{"text": "value 1600 " * 10000, "page_number": 1}], "txt": None}

    def failed_update(*a, **k):
        raise OSError("state write interrupted")

    monkeypatch.setattr(job_store, "update_job", failed_update)
    with pytest.raises(OSError):
        job_store.save_result(job["job_id"], payload)
    assert job_store.status(job["job_id"])["state"] == "SUCCESS"
    assert job_store.read_result(job["job_id"]) == payload
    assert len(json.dumps(job_store.result_reference(job["job_id"]))) < 200


def test_failed_publish_keeps_source_and_known_id(store):
    job = create(store)

    def failed_publish(*a):
        raise ConnectionError("Accepted by broker, then response lost")

    with pytest.raises(job_store.PublishUncertain) as error:
        job_store.publish_job(job["job_id"], failed_publish)
    assert error.value.job_id == job["job_id"]
    assert job_store.source_path(job["job_id"]).is_file()
    assert job_store.read_job(job["job_id"])["publication"] == "uncertain"
    calls = []
    job_store.publish_job(job["job_id"], lambda record: calls.append(record["job_id"]))
    job_store.publish_job(job["job_id"], lambda record: calls.append(record["job_id"]))
    assert calls == [job["job_id"]]


def test_resume_fences_old_messages_and_refuses_active_task(store):
    job = create(store)
    with job_store.execution(job["job_id"], 0):
        with pytest.raises(job_store.JobBusy):
            job_store.resume_job(job["job_id"])
    resumed = job_store.resume_job(job["job_id"])
    assert resumed["generation"] == 1
    with pytest.raises(job_store.StaleGeneration):
        with job_store.execution(job["job_id"], 0):
            pytest.fail("Old message executed")


def test_only_terminal_inactive_jobs_are_collectable(store):
    job = create(store)
    assert not job_store.collect_job(job["job_id"], retention_seconds=0)
    job_store.save_result(job["job_id"], {"result": [], "txt": None})
    with job_store.execution(job["job_id"], 0):
        assert not job_store.collect_job(job["job_id"], retention_seconds=0)
    assert job_store.collect_job(job["job_id"], retention_seconds=0)
    assert job_store.status(job["job_id"])["state"] == "EXPIRED"
    assert not job_store.source_path(job["job_id"]).exists()


def test_expired_tombstone_takes_precedence_over_interrupted_cleanup(store):
    job = create(store)
    job_store.save_result(job["job_id"], {"result": []})
    job_store.update_job(job["job_id"], state="EXPIRED", stage="expired")
    assert job_store.status(job["job_id"])["state"] == "EXPIRED"


def test_resume_refuses_inflight_publication(store):
    job = create(store)
    with job_store.stage_lock(job["job_id"], "publication"):
        with pytest.raises(job_store.JobBusy):
            job_store.resume_job(job["job_id"])


@pytest.mark.parametrize("broker_fails", [False, True])
def test_publishing_keeps_known_id_when_status_disk_write_fails(store, monkeypatch, broker_fails):
    job = create(store)

    def publisher(_record):
        if broker_fails:
            raise ConnectionError("broker response lost")

    def disk_full(*_args, **_kwargs):
        raise OSError("metadata volume full")

    monkeypatch.setattr(job_store, "update_job", disk_full)
    with pytest.raises(job_store.PublishUncertain) as error:
        job_store.publish_job(job["job_id"], publisher)
    assert error.value.job_id == job["job_id"]
    assert job_store.source_path(job["job_id"]).is_file()


def test_retention_uses_completion_time_even_if_metadata_stays_old(store, monkeypatch):
    job = create(store)
    job["updated_at"] = time.time() - 10 * 86400
    job_store.atomic_json(job_store.job_dir(job["job_id"]) / "job.json", job)
    monkeypatch.setattr(job_store, "update_job", lambda *_a, **_k: None)
    job_store.save_result(job["job_id"], {"result": []})
    assert not job_store.collect_job(job["job_id"], retention_seconds=7 * 86400)


def test_start_does_not_clear_a_concurrent_failure(store):
    job = create(store)
    job_store.start_job(job["job_id"])
    assert job_store.status(job["job_id"])["state"] == "STARTED"
    job_store.fail_job(job["job_id"], RuntimeError("other image failed"))
    job_store.start_job(job["job_id"])
    assert job_store.status(job["job_id"])["state"] == "FAILURE"
