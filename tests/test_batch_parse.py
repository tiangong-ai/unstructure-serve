import fcntl
import json

import httpx
import pytest

from src.scripts import batch_parse as batch


def options(tmp_path, **overrides):
    source = tmp_path / "input"
    source.mkdir(exist_ok=True)
    (source / "p2.pdf").write_bytes(b"%PDF-test")
    return batch.Options(
        input_dir=source,
        output_dir=tmp_path / "out",
        base_url="http://service/prefix",
        poll_interval=0,
        **overrides,
    )


def response(request, *, state="SUCCESS", status=200):
    return httpx.Response(
        status,
        json={
            "task_id": "task-1",
            "state": state,
            "result": {"result": [{"text": "1600", "page_number": 1}], "txt": "1600"},
        },
    )


@pytest.mark.parametrize(
    "mode,endpoint",
    [
        ("parse", "mineru/task"),
        ("images", "mineru_with_images/task"),
        ("two-stage", "two_stage/task"),
    ],
)
def test_modes_send_real_contract_and_resume_without_post(tmp_path, mode, endpoint):
    opts = options(tmp_path, mode=mode, chunk_type=True, return_txt=True)
    seen = []

    def handle(request):
        seen.append(request)
        assert request.url.path.startswith("/prefix/" + endpoint)
        if request.method == "POST":
            body = request.read()
            assert b'name="tier"\r\n\r\nadvanced' in body
            if mode == "two-stage":
                assert b'name="chunk_type"\r\n\r\ntrue' in body
                assert not request.url.query
            else:
                assert request.url.params["chunk_type"] == "true"
                assert request.url.params["return_txt"] == "true"
                assert b'name="chunk_type"' not in body
        return response(request)

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        assert batch.run(opts, client=client, token="private-token")["successes"] == 1
        assert batch.run(opts, client=client, token="private-token")["skipped"] == 1
    assert len(seen) == 2
    assert json.loads((opts.output_dir / "results/p2.pdf.json").read_text())["txt"] == "1600"
    assert "private-token" not in "".join(p.read_text() for p in opts.output_dir.rglob("*.json"))


def test_ordinary_http_500_terminal_failure_is_reported_not_polled_forever(tmp_path):
    opts = options(tmp_path, max_attempts=1)
    seen = []

    def handle(request):
        seen.append(request.method)
        return response(request, state="FAILURE", status=500 if request.method == "GET" else 200)

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        assert batch.run(opts, client=client, token="")["failures"] == 1
    assert seen == ["POST", "GET"]


def test_network_poll_error_keeps_same_task(tmp_path):
    opts = options(tmp_path)
    calls = []

    def handle(request):
        calls.append(request.method)
        if calls == ["POST", "GET"]:
            raise httpx.ReadTimeout("temporary", request=request)
        return response(request)

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        batch.run(opts, client=client, token="")
    assert calls == ["POST", "GET", "GET"]


def test_unknown_submission_is_never_reposted(tmp_path):
    opts = options(tmp_path)
    calls = []

    def handle(request):
        calls.append(request.method)
        raise httpx.ReadTimeout("lost response", request=request)

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        for _ in range(2):
            with pytest.raises(RuntimeError, match="unknown"):
                batch.run(opts, client=client, token="")
    assert calls == ["POST"]


def test_changed_completed_input_does_not_silently_skip(tmp_path):
    opts = options(tmp_path)
    with httpx.Client(transport=httpx.MockTransport(response)) as client:
        batch.run(opts, client=client, token="")
        (opts.input_dir / "p2.pdf").write_bytes(b"changed")
        with pytest.raises(ValueError, match="changed"):
            batch.run(opts, client=client, token="")


def test_recursive_same_names_and_extensions_have_distinct_outputs(tmp_path):
    opts = options(tmp_path, recursive=True, extensions="all")
    (opts.input_dir / "sub").mkdir()
    (opts.input_dir / "sub/p2.pdf").write_bytes(b"pdf")
    (opts.input_dir / "p2.docx").write_bytes(b"office")
    n = [0]

    def handle(request):
        if request.method == "POST":
            n[0] += 1
        result = response(request).json()
        result["task_id"] = f"task-{n[0]}"
        return httpx.Response(200, json=result)

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        assert batch.run(opts, client=client, token="")["successes"] == 3
    assert len(list((opts.output_dir / "results").rglob("*.json"))) == 3


@pytest.mark.parametrize(
    "overrides",
    [
        {"mode": "parse", "provider": "vllm"},
        {"max_in_flight": 0},
        {"upload_timeout": 0},
        {"extensions": "txt"},
    ],
)
def test_invalid_options_fail_before_upload(tmp_path, overrides):
    opts = options(tmp_path, **overrides)
    with pytest.raises(ValueError):
        batch.run(opts, token="")


def test_empty_directory_fails(tmp_path):
    opts = options(tmp_path)
    (opts.input_dir / "p2.pdf").unlink()
    with pytest.raises(ValueError, match="No supported"):
        batch.run(opts, token="")


def test_corrupt_result_is_refetched_without_upload(tmp_path):
    opts = options(tmp_path)
    methods = []

    def handle(request):
        methods.append(request.method)
        return response(request)

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        batch.run(opts, client=client, token="")
        (opts.output_dir / "results/p2.pdf.json").write_text("partial")
        batch.run(opts, client=client, token="")
    assert methods == ["POST", "GET", "GET"]


def test_output_lock_prevents_second_writer(tmp_path):
    opts = options(tmp_path)
    opts.output_dir.mkdir()
    with (opts.output_dir / ".batch.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(RuntimeError, match="Another batch writer"):
            batch.run(opts, token="")


def test_dry_run_has_no_requests_or_output(tmp_path):
    opts = options(tmp_path, dry_run=True)
    assert batch.run(opts)["files"] == 1
    assert not opts.output_dir.exists()


def test_missing_previously_submitted_file_blocks_new_upload(tmp_path):
    opts = options(tmp_path)
    with httpx.Client(transport=httpx.MockTransport(response)) as client:
        batch.run(opts, client=client, token="")
    (opts.input_dir / "p2.pdf").rename(opts.input_dir / "renamed.pdf")

    def never(request):
        pytest.fail("Input selection must be validated before submitting")

    with httpx.Client(transport=httpx.MockTransport(never)) as client:
        with pytest.raises(ValueError, match="missing or filtered"):
            batch.run(opts, client=client, token="")


def test_changed_mode_is_rejected_even_after_success(tmp_path):
    opts = options(tmp_path)
    with httpx.Client(transport=httpx.MockTransport(response)) as client:
        batch.run(opts, client=client, token="")
        opts.mode = "images"
        with pytest.raises(ValueError, match="changed"):
            batch.run(opts, client=client, token="")


def test_upload_is_chunked_and_timeouts_are_independent(tmp_path):
    opts = options(tmp_path, upload_timeout=123, query_timeout=45, connect_timeout=6)
    path = opts.input_dir / "p2.pdf"
    path.write_bytes(b"x" * (3 * 1024 * 1024))

    class Transport(httpx.BaseTransport):
        def handle_request(self, request):
            if request.method == "POST":
                chunks = list(request.stream)
                assert max(map(len, chunks)) <= 65536
                assert sum(map(len, chunks)) > path.stat().st_size
                assert request.extensions["timeout"]["write"] == 123
            else:
                assert request.extensions["timeout"]["read"] == 45
            assert request.extensions["timeout"]["connect"] == 6
            return response(request)

    with httpx.Client(transport=Transport()) as client:
        batch.run(opts, client=client, token="")


def test_success_without_business_result_does_not_create_output(tmp_path):
    opts = options(tmp_path)

    def handle(request):
        if request.method == "POST":
            return response(request)
        return httpx.Response(200, json={"state": "SUCCESS", "result": {}})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(ValueError, match="business result"):
            batch.run(opts, client=client, token="")
    assert not (opts.output_dir / "results/p2.pdf.json").exists()


def test_timeout_and_smaller_window_resume_existing_jobs(tmp_path, monkeypatch):
    from src.scripts import batch_runner

    opts = options(tmp_path, poll_timeout=2, max_in_flight=2)
    (opts.input_dir / "second.pdf").write_bytes(b"second")
    clock = [0]
    monkeypatch.setattr(batch_runner.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(batch_runner.time, "sleep", lambda _: clock.__setitem__(0, clock[0] + 1))
    posts = []
    complete = [False]

    def handle(request):
        if request.method == "POST":
            posts.append(request)
        body = response(request, state="SUCCESS" if complete[0] else "STARTED").json()
        body["task_id"] = f"task-{len(posts)}"
        return httpx.Response(200, json=body)

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(TimeoutError):
            batch.run(opts, client=client, token="")
        opts.max_in_flight = 1
        complete[0] = True
        assert batch.run(opts, client=client, token="")["successes"] == 2
    assert len(posts) == 2


def test_resume_only_collects_existing_tasks_without_refill_or_retry(tmp_path, monkeypatch):
    from src.scripts import batch_runner

    opts = options(tmp_path, max_in_flight=1, poll_timeout=1)
    (opts.input_dir / "second.pdf").write_bytes(b"second")
    clock = [0]
    monkeypatch.setattr(batch_runner.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(batch_runner.time, "sleep", lambda _: clock.__setitem__(0, clock[0] + 1))
    methods = []
    complete = [False]

    def handle(request):
        methods.append(request.method)
        return response(request, state="SUCCESS" if complete[0] else "STARTED")

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(TimeoutError):
            batch.run(opts, client=client, token="")
        complete[0] = True
        opts.resume_only = True
        result = batch.run(opts, client=client, token="")
    assert methods.count("POST") == 1
    assert result == {"successes": 1, "skipped": 0, "failures": 0, "deferred": 1}
    assert not (opts.output_dir / "results/second.pdf.json").exists()


def test_resume_only_requires_existing_batch(tmp_path):
    opts = options(tmp_path)
    opts.resume_only = True
    with pytest.raises(ValueError, match="existing batch"):
        batch.run(opts, token="")


def test_resume_only_does_not_retry_newly_failed_task(tmp_path, monkeypatch):
    from src.scripts import batch_runner

    opts = options(tmp_path, poll_timeout=1, max_attempts=3)
    clock = [0]
    monkeypatch.setattr(batch_runner.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(batch_runner.time, "sleep", lambda _: clock.__setitem__(0, clock[0] + 1))
    methods = []
    failed = [False]

    def handle(request):
        methods.append(request.method)
        return response(request, state="FAILURE" if failed[0] else "STARTED")

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(TimeoutError):
            batch.run(opts, client=client, token="")
        opts.resume_only = True
        failed[0] = True
        assert batch.run(opts, client=client, token="")["failures"] == 1
    assert methods.count("POST") == 1
