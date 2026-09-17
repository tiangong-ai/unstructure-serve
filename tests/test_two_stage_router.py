from pathlib import Path
from types import SimpleNamespace

from src.routers import two_stage_router
from src.services.vision_service import VisionModel, VisionProvider


def test_two_stage_rejects_missing_extension(client):
    resp = client.post(
        "/two_stage/task",
        files={"file": ("noext", b"content", "application/octet-stream")},
    )
    assert resp.status_code == 400
    assert "extension" in resp.json()["detail"].lower()


def test_two_stage_enqueues_and_returns_task_id(client, monkeypatch, tmp_path):
    workspace_root = tmp_path / "workspace"
    provider_value = next(iter(VisionProvider))
    model_value = next(iter(VisionModel))

    def fake_ensure_workspace() -> Path:
        workspace_root.mkdir(parents=True, exist_ok=True)
        return workspace_root

    monkeypatch.setattr(two_stage_router, "_ensure_workspace", fake_ensure_workspace)

    captured = {}

    class DummyAsyncResult:
        def __init__(self) -> None:
            self.id = "fake-task-id"
            self.state = "PENDING"

    def fake_submit_two_stage_job(
        processing_path: str,
        *,
        backend=None,
        chunk_type=False,
        return_txt=False,
        provider=None,
        model=None,
        prompt=None,
        workspace=None,
        cleanup_source=False,
        extra_cleanup=None,
        parse_queue=None,
        vision_queue=None,
        dispatch_queue=None,
        merge_queue=None,
    ):
        captured.update(
            {
                "processing_path": processing_path,
                "backend": backend,
                "chunk_type": chunk_type,
                "return_txt": return_txt,
                "provider": provider,
                "model": model,
                "prompt": prompt,
                "workspace": workspace,
                "cleanup_source": cleanup_source,
                "extra_cleanup": extra_cleanup,
                "parse_queue": parse_queue,
                "vision_queue": vision_queue,
                "dispatch_queue": dispatch_queue,
                "merge_queue": merge_queue,
            }
        )
        return DummyAsyncResult()

    monkeypatch.setattr(two_stage_router, "submit_two_stage_job", fake_submit_two_stage_job)
    monkeypatch.setattr(
        two_stage_router,
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
    assert resp.json() == {"task_id": "fake-task-id", "state": "PENDING"}

    assert captured["workspace"] == str(workspace_root)
    assert captured["processing_path"].endswith("sample.pdf")
    assert captured["chunk_type"] is True
    assert captured["return_txt"] is True
    assert captured["provider"] == provider_value
    assert captured["model"] == model_value
    assert captured["prompt"] == "describe"
    assert captured["parse_queue"] == "queue_parse_urgent"
    assert captured["vision_queue"] == "queue_vision_urgent"
    assert captured["dispatch_queue"] == "queue_dispatch_urgent"
    assert captured["merge_queue"] == "queue_merge_urgent"


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
