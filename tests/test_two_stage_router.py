from types import SimpleNamespace

from src.routers import two_stage_router
from src.services import job_store, two_stage_pipeline
from src.services.vision_service import VisionModel, VisionProvider


def test_two_stage_rejects_missing_extension(client):
    resp = client.post(
        "/two_stage/task",
        files={"file": ("noext", b"content", "application/octet-stream")},
    )
    assert resp.status_code == 400
    assert "extension" in resp.json()["detail"].lower()


def test_two_stage_enqueues_and_returns_task_id(client, monkeypatch):
    provider_value = next(iter(VisionProvider))
    model_value = next(iter(VisionModel))
    captured = {}

    def publish(ref, **queues):
        captured.update(ref=ref, queues=queues)
        return SimpleNamespace(id="internal-celery-id")

    monkeypatch.setattr(two_stage_pipeline, "submit_durable_two_stage", publish, raising=False)
    monkeypatch.setattr(
        two_stage_pipeline,
        "resolve_two_stage_queues",
        lambda priority: {
            "parse": "queue_parse_urgent",
            "vision": "queue_vision_urgent",
            "dispatch": "queue_dispatch_urgent",
            "merge": "queue_merge_urgent",
        },
    )
    resp = client.post(
        "/two_stage/task",
        data={
            "chunk_type": "true",
            "return_txt": "true",
            "priority": "urgent",
            "provider": provider_value.value,
            "model": model_value.value,
            "prompt": "describe",
        },
        files={"file": ("sample.pdf", b"%PDF-1.4 content", "application/pdf")},
    )
    assert resp.status_code == 200
    job_id = resp.json()["task_id"]
    assert resp.json()["state"] == "PENDING"
    assert captured["ref"] == {"job_id": job_id, "generation": 0}
    options = job_store.read_job(job_id)["options"]
    assert options["chunk_type"] is True
    assert options["return_txt"] is True
    assert options["vision_provider"] == provider_value.value
    assert options["vision_model"] == model_value.value
    assert options["prompt"] == "describe"
    assert captured["queues"] == {
        "parse_queue": "queue_parse_urgent",
        "vision_queue": "queue_vision_urgent",
        "dispatch_queue": "queue_dispatch_urgent",
        "merge_queue": "queue_merge_urgent",
    }


def test_two_stage_queue_status_reports_redis_backlog(client, monkeypatch):
    class FakeRedis:
        def __init__(self) -> None:
            self.lengths = {
                "queue_parse_gpu": 2,
                "queue_vision": 3,
                "queue_dispatch": 0,
                "default": 1,
            }

        def llen(self, queue_name: str) -> int:
            return self.lengths.get(queue_name, 0)

        def hvals(self, _name: str):
            return [
                b'["body", "", "queue_vision"]',
                b'["body", "", "queue_vision"]',
                b'["body", "", "queue_parse_gpu"]',
                b"not-json",
            ]

    class FakeRedisFactory:
        @staticmethod
        def from_url(_url: str) -> FakeRedis:
            return FakeRedis()

    monkeypatch.setattr(
        two_stage_router.celery_app,
        "conf",
        SimpleNamespace(broker_url="redis://localhost:6379/0"),
    )
    monkeypatch.setattr(
        two_stage_router,
        "resolve_two_stage_queues",
        lambda priority: {
            "parse": "queue_parse_urgent" if priority == "urgent" else "queue_parse_gpu",
            "vision": "queue_vision_urgent" if priority == "urgent" else "queue_vision",
            "dispatch": "queue_dispatch_urgent" if priority == "urgent" else "queue_dispatch",
            "merge": "queue_merge_urgent" if priority == "urgent" else "default",
        },
    )
    monkeypatch.setitem(__import__("sys").modules, "redis", SimpleNamespace(Redis=FakeRedisFactory))

    resp = client.get("/two_stage/queue_status")

    assert resp.status_code == 200
    assert resp.json() == {
        "broker": "redis",
        "queues": {
            "queue_parse_gpu": 2,
            "queue_vision": 3,
            "queue_dispatch": 0,
            "default": 1,
            "queue_parse_urgent": 0,
            "queue_vision_urgent": 0,
            "queue_dispatch_urgent": 0,
            "queue_merge_urgent": 0,
        },
        "unacked": {
            "queue_parse_gpu": 1,
            "queue_vision": 2,
            "queue_dispatch": 0,
            "default": 0,
            "queue_parse_urgent": 0,
            "queue_vision_urgent": 0,
            "queue_dispatch_urgent": 0,
            "queue_merge_urgent": 0,
        },
    }
