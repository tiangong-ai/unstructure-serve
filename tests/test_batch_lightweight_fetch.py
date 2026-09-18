"""Batch polling uses small statuses and downloads only completed business JSON."""

import httpx
import pytest

from src.scripts.batch_parse import Options, TaskAPI


def api(tmp_path, mode="parse"):
    return TaskAPI(
        Options(
            input_dir=tmp_path,
            output_dir=tmp_path / "out",
            base_url="http://service/prefix/",
            mode=mode,
            query_timeout=45,
            connect_timeout=6,
        )
    )


@pytest.mark.parametrize("state", ["PENDING", "STARTED", "RETRY", "RECEIVED", "FAILURE", "REVOKED"])
def test_incomplete_status_never_downloads_result(tmp_path, state):
    seen = []

    def handle(request):
        seen.append(request.url.path)
        assert request.url.path == "/prefix/tasks/job-1/status"
        return httpx.Response(200, json={"task_id": "job-1", "state": state})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        assert api(tmp_path).fetch(client, "job-1", "")["state"] == state
    assert seen == ["/prefix/tasks/job-1/status"]


@pytest.mark.parametrize("mode", ["parse", "images", "two-stage"])
def test_success_downloads_once_and_preserves_mode_null_contract(tmp_path, mode):
    seen = []
    stored = {"result": [{"text": "1600", "page_number": 1, "type": None}], "txt": None}

    def handle(request):
        seen.append(request.url.path)
        assert request.headers["Authorization"] == "Bearer private-token"
        assert request.extensions["timeout"]["read"] == 45
        assert request.extensions["timeout"]["connect"] == 6
        if request.url.path.endswith("/status"):
            return httpx.Response(200, json={"task_id": "job-1", "state": "SUCCESS"})
        assert request.url.path == "/prefix/tasks/job-1/result"
        return httpx.Response(200, json=stored)

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        response = api(tmp_path, mode).fetch(client, "job-1", "private-token")
    expected = stored if mode == "two-stage" else {"result": [{"text": "1600", "page_number": 1}]}
    assert response["result"] == expected
    assert seen == ["/prefix/tasks/job-1/status", "/prefix/tasks/job-1/result"]


@pytest.mark.parametrize("mode", ["parse", "images", "two-stage"])
@pytest.mark.parametrize("state,status", [("SUCCESS", 200), ("FAILURE", 500)])
def test_only_missing_lightweight_status_falls_back_to_legacy(tmp_path, mode, state, status):
    task_api = api(tmp_path, mode)
    seen = []
    legacy = {
        "task_id": "old-task",
        "state": state,
        "result": {"result": [{"text": "1600", "page_number": 1}]},
    }

    def handle(request):
        seen.append(request.url.path)
        if request.url.path == "/prefix/tasks/old-task/status":
            return httpx.Response(404)
        assert str(request.url) == task_api.url + "/old-task"
        return httpx.Response(status, json=legacy)

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        assert task_api.fetch(client, "old-task", "") == legacy
    assert len(seen) == 2


@pytest.mark.parametrize("status", [401, 403, 410, 500, 503])
def test_status_errors_never_fall_back_or_download(tmp_path, status):
    seen = []

    def handle(request):
        seen.append(request.url.path)
        return httpx.Response(status, json={"state": "FAILURE"})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(httpx.HTTPStatusError):
            api(tmp_path).fetch(client, "job-1", "")
    assert seen == ["/prefix/tasks/job-1/status"]


def test_status_timeout_never_falls_back(tmp_path):
    seen = []

    def handle(request):
        seen.append(request.url.path)
        raise httpx.ReadTimeout("private transient failure", request=request)

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(httpx.ReadTimeout):
            api(tmp_path).fetch(client, "job-1", "")
    assert seen == ["/prefix/tasks/job-1/status"]


def test_expired_status_fails_clearly_without_legacy_fallback(tmp_path):
    seen = []

    def handle(request):
        seen.append(request.url.path)
        return httpx.Response(200, json={"state": "EXPIRED"})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(RuntimeError, match="job-1.*expired"):
            api(tmp_path).fetch(client, "job-1", "")
    assert seen == ["/prefix/tasks/job-1/status"]


@pytest.mark.parametrize("status", [401, 403, 404, 409, 410, 500, 503])
def test_result_error_does_not_retry_through_legacy_route(tmp_path, status):
    seen = []

    def handle(request):
        seen.append(request.url.path)
        if request.url.path.endswith("/status"):
            return httpx.Response(200, json={"state": "SUCCESS"})
        return httpx.Response(status)

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(httpx.HTTPStatusError):
            api(tmp_path).fetch(client, "job-1", "")
    assert seen == ["/prefix/tasks/job-1/status", "/prefix/tasks/job-1/result"]
