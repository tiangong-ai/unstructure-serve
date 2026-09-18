"""Durable submission, legacy response contracts, and streamed result delivery."""

import pytest
from types import SimpleNamespace

from src.routers import mineru_task_router, mineru_with_images_task_router, two_stage_router
from src.services import job_store, two_stage_pipeline
from src.services.tasks import mineru_tasks
from src.routers import job_router

ENDPOINTS = {
    "parse": "/mineru/task",
    "images": "/mineru_with_images/task",
    "two-stage": "/two_stage/task",
}


@pytest.fixture
def publications(monkeypatch):
    calls = []

    class Accepted:
        id = "celery-internal-id"

        @property
        def state(self):
            raise AssertionError("POST must not query the result backend after acceptance")

    def publish(*args, **kwargs):
        calls.append((args, kwargs))
        return Accepted()

    monkeypatch.setattr(mineru_tasks.run_mineru_task, "apply_async", publish)
    monkeypatch.setattr(mineru_tasks.run_mineru_with_images_task, "apply_async", publish)
    monkeypatch.setattr(two_stage_router, "submit_two_stage_job", publish, raising=False)
    monkeypatch.setattr(two_stage_pipeline, "submit_durable_two_stage", publish, raising=False)
    return calls


def post(client, mode, *, content=b"%PDF-real-upload-bytes", key="request-key", **data):
    return client.post(
        ENDPOINTS[mode],
        headers={"Idempotency-Key": key},
        files={"file": ("sample.pdf", content, "application/pdf")},
        data=data,
    )


@pytest.mark.parametrize("mode", ENDPOINTS)
def test_idempotent_post_retains_original_input_and_publishes_only_references(
    client, publications, mode
):
    first = post(client, mode)
    second = post(client, mode)
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    job_id = first.json()["task_id"]
    assert first.json()["state"] == "PENDING"
    assert len(publications) == 1
    record = job_store.read_job(job_id)
    assert record["mode"] == mode
    assert record["options"]["backend"] == "advanced"
    assert job_store.source_path(job_id).read_bytes() == b"%PDF-real-upload-bytes"
    args, kwargs = publications[0]
    ref = args[0] if mode == "two-stage" else kwargs["args"][0]
    assert ref == {"job_id": job_id, "generation": 0}


@pytest.mark.parametrize("mode", ENDPOINTS)
def test_key_reuse_with_different_input_or_tier_conflicts(client, publications, mode):
    original = post(client, mode)
    assert original.status_code == 200
    assert post(client, mode, content=b"changed").status_code == 409
    assert post(client, mode, tier="basic").status_code == 409
    assert len(publications) == 1


def test_ambiguous_publish_retains_job_and_input_and_exposes_recoverable_id(client, monkeypatch):
    def fail(**kwargs):
        raise ConnectionError("private backend address must not escape")

    monkeypatch.setattr(mineru_tasks.run_mineru_task, "apply_async", fail)
    response = post(client, "parse")
    assert response.status_code == 503
    detail = response.json()["detail"]
    job_id = detail["task_id"]
    assert "private backend" not in response.text
    assert job_store.read_job(job_id)["publication"] == "uncertain"
    assert job_store.source_path(job_id).is_file()
    monkeypatch.setattr(mineru_tasks.run_mineru_task, "apply_async", lambda **kwargs: None)
    recovered = post(client, "parse")
    assert recovered.status_code == 200 and recovered.json()["task_id"] == job_id


def test_office_is_persisted_without_conversion_during_submission(
    client, publications, monkeypatch
):
    def never(*args, **kwargs):
        pytest.fail("Office conversion belongs to worker execution")

    monkeypatch.setattr(two_stage_router, "maybe_convert_to_pdf", never, raising=False)
    response = client.post(
        "/two_stage/task",
        files={"file": ("office.docx", b"original-office-bytes", "application/octet-stream")},
    )
    assert response.status_code == 200
    assert (
        job_store.source_path(response.json()["task_id"]).read_bytes() == b"original-office-bytes"
    )


def complete_job(tmp_path, mode="parse"):
    source = tmp_path / "completed.pdf"
    source.write_bytes(b"%PDF")
    record = job_store.create_job(mode, source, {"backend": "advanced"})
    payload = {"result": [{"text": "retained", "page_number": 1, "type": None}], "txt": None}
    job_store.save_result(record["job_id"], payload)
    return record["job_id"], payload


@pytest.mark.parametrize("mode", ENDPOINTS)
def test_old_status_contract_reads_durable_results_before_redis(
    client, monkeypatch, tmp_path, mode
):
    job_id, payload = complete_job(tmp_path, mode)
    module = {
        "parse": mineru_task_router,
        "images": mineru_with_images_task_router,
        "two-stage": two_stage_router,
    }[mode]
    monkeypatch.setattr(module, "AsyncResult", lambda *a, **kw: pytest.fail("Redis not needed"))
    response = client.get(f"{ENDPOINTS[mode]}/{job_id}")
    assert response.status_code == 200
    body = response.json()
    assert body["state"] == "SUCCESS" and body["result"]["result"][0]["text"] == "retained"
    assert ("txt" in body["result"]) == (mode == "two-stage")


def test_light_status_avoids_result_reads_and_download_returns_stored_json(
    client, monkeypatch, tmp_path
):
    job_id, payload = complete_job(tmp_path)
    monkeypatch.setattr(job_store, "read_result", lambda *a: pytest.fail("No whole-result read"))
    response = client.get(f"/tasks/{job_id}/status")
    assert response.status_code == 200 and response.json()["state"] == "SUCCESS"
    assert "result" not in response.json()
    download = client.get(f"/tasks/{job_id}/result")
    assert download.status_code == 200 and download.json() == payload
    assert "attachment" in download.headers["content-disposition"]


def test_expired_result_and_reused_key_are_explicit(client, publications):
    response = post(client, "parse")
    job_id = response.json()["task_id"]
    job_store.save_result(job_id, {"result": [], "txt": None})
    assert job_store.collect_job(job_id, retention_seconds=0)
    assert client.get(f"/tasks/{job_id}/status").json()["state"] == "EXPIRED"
    assert client.get(f"/tasks/{job_id}/result").status_code == 410
    assert post(client, "parse").status_code == 410


def test_explicit_resume_uses_same_job_id_new_generation_and_blocks_active_job(
    client, publications
):
    job_id = post(client, "parse").json()["task_id"]
    with job_store.execution(job_id, 0):
        assert client.post(f"/tasks/{job_id}/resume").status_code == 409
    job_store.fail_job(job_id, RuntimeError("failed stage"))
    response = client.post(f"/tasks/{job_id}/resume")
    assert response.status_code == 200
    assert response.json()["task_id"] == job_id
    assert publications[-1][1]["args"] == [{"job_id": job_id, "generation": 1}]


def test_unknown_durable_routes_do_not_fall_back_to_redis(client):
    job_id = "00000000-0000-0000-0000-000000000001"
    for suffix in ("status", "result"):
        assert client.get(f"/tasks/{job_id}/{suffix}").status_code == 404
    assert client.post(f"/tasks/{job_id}/resume").status_code == 404


@pytest.mark.parametrize("mode", ENDPOINTS)
def test_failure_status_codes_preserve_each_legacy_contract(client, publications, mode):
    job_id = post(client, mode).json()["task_id"]
    job_store.fail_job(job_id, RuntimeError("stage failed"))
    response = client.get(f"{ENDPOINTS[mode]}/{job_id}")
    assert response.status_code == (200 if mode == "two-stage" else 500)
    assert response.json()["state"] == "FAILURE"
    assert "stage failed" in response.json()["error"]


@pytest.mark.parametrize("mode", ENDPOINTS)
def test_legacy_task_ids_still_query_the_original_result_backend(client, monkeypatch, mode):
    module = {
        "parse": mineru_task_router,
        "images": mineru_with_images_task_router,
        "two-stage": two_stage_router,
    }[mode]
    monkeypatch.setattr(
        module,
        "AsyncResult",
        lambda task_id, **kwargs: SimpleNamespace(
            state="SUCCESS", result={"result": [{"text": "legacy", "page_number": 1}]}
        ),
    )
    response = client.get(f"{ENDPOINTS[mode]}/legacy-task-id")
    assert response.status_code == 200
    assert response.json()["result"]["result"][0]["text"] == "legacy"


def test_result_download_checks_readiness_and_integrity(client, publications):
    job_id = post(client, "parse").json()["task_id"]
    assert client.get(f"/tasks/{job_id}/result").status_code == 409
    job_store.save_result(job_id, {"result": [], "txt": None})
    (job_store.job_dir(job_id) / "result.json").write_text('{"result": ["damaged"]}')
    response = client.get(f"/tasks/{job_id}/result")
    assert response.status_code == 500
    assert "damaged" not in response.text


def test_download_lease_survives_until_streaming_and_releases_on_disconnect(tmp_path):
    import asyncio

    job_id, _ = complete_job(tmp_path)
    response = job_router.task_result(job_id)
    assert not job_store.collect_job(job_id, retention_seconds=0)

    async def disconnected_send(message):
        raise OSError("connection lost")

    async def receive():
        return {"type": "http.disconnect"}

    with pytest.raises(OSError, match="connection lost"):
        asyncio.run(
            response({"type": "http", "method": "GET", "headers": []}, receive, disconnected_send)
        )
    assert job_store.collect_job(job_id, retention_seconds=0)


def test_legacy_result_read_is_protected_from_retention_cleanup(client, monkeypatch, tmp_path):
    job_id, _ = complete_job(tmp_path)
    original = job_store.read_result

    def protected_read(task_id):
        assert not job_store.collect_job(task_id, retention_seconds=0)
        return original(task_id)

    monkeypatch.setattr(job_store, "read_result", protected_read)
    assert client.get(f"/mineru/task/{job_id}").status_code == 200
    assert job_store.collect_job(job_id, retention_seconds=0)


@pytest.mark.parametrize("key", ["", "x" * 201])
def test_invalid_idempotency_key_is_rejected_without_publication(client, publications, key):
    assert post(client, "parse", key=key).status_code == 422
    assert publications == []


@pytest.mark.parametrize(
    "task", [mineru_tasks.run_mineru_task, mineru_tasks.run_mineru_with_images_task]
)
def test_ordinary_celery_accepts_reference_and_uses_late_ack(monkeypatch, task):
    from src.services import durable_pipeline

    captured = []
    ref = {"job_id": "00000000-0000-0000-0000-000000000001", "generation": 2}

    def run(payload):
        captured.append(payload)
        return {"job_id": payload["job_id"], "stored_result": True}

    monkeypatch.setattr(durable_pipeline, "run_durable_job", run)
    assert task.acks_late is True
    assert task.run(ref) == {"job_id": ref["job_id"], "stored_result": True}
    assert captured == [ref]
