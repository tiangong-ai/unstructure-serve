from types import SimpleNamespace

from src.routers import mineru_with_images_task_router as router


def test_mineru_with_images_task_rejects_markdown(client, monkeypatch):
    called = False

    def fake_apply_async(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("task should not be queued for Markdown uploads")

    monkeypatch.setattr(router.run_mineru_with_images_task, "apply_async", fake_apply_async)

    response = client.post(
        "/mineru_with_images/task",
        files={"file": ("sample.md", b"# Title\n\nBody", "text/markdown")},
    )

    assert response.status_code == 400
    assert "Unsupported file type" in response.json()["detail"]
    assert called is False


def test_mineru_with_images_task_invalid_model_no_longer_returns_422(client, monkeypatch):
    captured: dict[str, object] = {}

    def fake_apply_async(*, args, queue):
        captured["payload"] = args[0]
        captured["queue"] = queue
        return SimpleNamespace(id="task-123", state="PENDING")

    monkeypatch.setattr(router.run_mineru_with_images_task, "apply_async", fake_apply_async)

    response = client.post(
        "/mineru_with_images/task",
        files={"file": ("sample.pdf", b"%PDF-1.4\n", "application/pdf")},
        data={"provider": "missing-provider", "model": "missing-model"},
    )

    assert response.status_code == 200
    assert response.json() == {"task_id": "task-123", "state": "PENDING"}
    assert captured["payload"]["vision_provider"] == "missing-provider"
    assert captured["payload"]["vision_model"] == "missing-model"
