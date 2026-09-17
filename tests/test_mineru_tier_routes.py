"""Request-level tier contracts, including the tier snapshotted into queued jobs."""

import importlib
from concurrent.futures import Future
from pathlib import Path
from types import SimpleNamespace

import pytest

ROUTES = (
    ("/mineru", "mineru_router"),
    ("/mineru_sci", "mineru_sci_router"),
    ("/mineru_with_images", "mineru_with_images_router"),
    ("/mineru/task", "mineru_task_router"),
    ("/mineru_with_images/task", "mineru_with_images_task_router"),
    ("/two_stage/task", "two_stage_router"),
)
TIERS = ("flash", "basic", "standard", "advanced")


@pytest.fixture
def submissions(client, monkeypatch, tmp_path, route):
    module = importlib.import_module(f"src.routers.{route[1]}")
    captured = []
    # API defaults must not depend on a worker's or API process's legacy config.
    monkeypatch.setenv("MINERU_DEFAULT_TIER", "standard")
    monkeypatch.setenv("MINERU_DEFAULT_BACKEND", "pipeline")

    def record(source, tier):
        assert Path(source).read_bytes().startswith(b"%PDF")
        captured.append(tier)

    if hasattr(module, "scheduler"):

        def submit(source, **kwargs):
            record(source, kwargs.get("backend"))
            future = Future()
            future.set_result({"result": [{"text": "parsed", "page_number": 1}]})
            return future

        monkeypatch.setattr(module.scheduler, "submit", submit)
    elif route[0] == "/two_stage/task":

        def submit(source, **kwargs):
            record(source, kwargs.get("backend"))
            return SimpleNamespace(id="tier-test", state="PENDING")

        monkeypatch.setattr(module, "_ensure_workspace", lambda: tmp_path)
        monkeypatch.setattr(module, "submit_two_stage_job", submit)
    else:

        def apply_async(*, args, queue):
            record(args[0]["source_path"], args[0]["backend_value"])
            return SimpleNamespace(id="tier-test", state="PENDING")

        task = (
            module.run_mineru_task
            if route[0] == "/mineru/task"
            else module.run_mineru_with_images_task
        )
        monkeypatch.setattr(module, "_ensure_storage_root", lambda: tmp_path)
        monkeypatch.setattr(task, "apply_async", apply_async)
    return captured


@pytest.mark.parametrize("route", ROUTES, ids=[route[0] for route in ROUTES])
@pytest.mark.parametrize("tier", [None, *TIERS], ids=["default", *TIERS])
def test_tier_reaches_parser_or_task_payload(client, route, tier, submissions):
    response = client.post(
        route[0],
        files={"file": ("sample.pdf", b"%PDF-1.4\n", "application/pdf")},
        data={} if tier is None else {"tier": tier},
    )
    assert response.status_code == 200, response.text
    assert submissions == [tier or "advanced"]


@pytest.mark.parametrize("route", ROUTES, ids=[route[0] for route in ROUTES])
@pytest.mark.parametrize("tier", ["pipeline", "unknown"])
def test_invalid_tier_is_rejected_before_dispatch(client, route, tier, submissions):
    response = client.post(
        route[0],
        files={"file": ("sample.pdf", b"%PDF-1.4\n", "application/pdf")},
        data={"tier": tier},
    )
    assert response.status_code == 422, response.text
    assert response.json()["detail"][0]["loc"] == ["body", "tier"]
    assert submissions == []


@pytest.mark.parametrize("route", ROUTES, ids=[route[0] for route in ROUTES])
def test_openapi_exposes_four_tiers_with_advanced_default(app, route):
    schema = app.openapi()
    body = schema["paths"][route[0]]["post"]["requestBody"]["content"]["multipart/form-data"][
        "schema"
    ]
    body = schema["components"]["schemas"][body["$ref"].split("/")[-1]]
    tier = body["properties"]["tier"]
    assert tier["default"] == "advanced"
    assert "tier" not in body.get("required", [])
    choices = schema["components"]["schemas"][tier["$ref"].split("/")[-1]]
    assert choices["enum"] == list(TIERS)
