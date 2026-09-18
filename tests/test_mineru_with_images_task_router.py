from types import SimpleNamespace

from src.services import job_store
from src.services.tasks import mineru_tasks


def test_mineru_with_images_task_rejects_markdown(client, monkeypatch):
    called = False

    def fake_apply_async(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("task should not be queued for Markdown uploads")

    monkeypatch.setattr(mineru_tasks.run_mineru_with_images_task, "apply_async", fake_apply_async)

    response = client.post(
        "/mineru_with_images/task",
        files={"file": ("sample.md", b"# Title\n\nBody", "text/markdown")},
    )

    assert response.status_code == 400
    assert "Unsupported file type" in response.json()["detail"]
    assert called is False


def test_mineru_with_images_task_invalid_model_no_longer_returns_422(client, monkeypatch):
    captured: dict[str, object] = {}

    def fake_apply_async(*, args, queue, task_id):
        captured["payload"] = args[0]
        captured["queue"] = queue
        return SimpleNamespace(id="task-123", state="PENDING")

    monkeypatch.setattr(mineru_tasks.run_mineru_with_images_task, "apply_async", fake_apply_async)

    response = client.post(
        "/mineru_with_images/task",
        files={"file": ("sample.pdf", b"%PDF-1.4\n", "application/pdf")},
        data={"provider": "missing-provider", "model": "missing-model"},
    )

    assert response.status_code == 200
    job_id = response.json()["task_id"]
    assert response.json()["state"] == "PENDING"
    assert captured["payload"] == {"job_id": job_id, "generation": 0}
    options = job_store.read_job(job_id)["options"]
    assert options["vision_provider"] == "missing-provider"
    assert options["vision_model"] == "missing-model"
