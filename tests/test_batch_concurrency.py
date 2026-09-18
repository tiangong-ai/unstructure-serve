"""Network ordering and durable journals under concurrent batch I/O."""

import hashlib
import json
import re
import threading
import time

import httpx
import pytest

from src.scripts import batch_parse as batch
from src.scripts import batch_runner


def options(tmp_path, count=2, **overrides):
    source = tmp_path / "input"
    source.mkdir()
    for index in range(count):
        (source / f"{index}.pdf").write_bytes(f"%PDF-source-{index}".encode())
    return batch.Options(
        input_dir=source,
        output_dir=tmp_path / "out",
        base_url="http://service",
        poll_interval=0,
        **overrides,
    )


def filename(request):
    return re.search(rb'filename="([^"]+)"', request.read()).group(1).decode()


def reply(request, task_id=None, state="SUCCESS"):
    return httpx.Response(
        200,
        json={
            "task_id": task_id or request.url.path.rsplit("/", 1)[-1],
            "state": state,
            "result": {"result": [{"text": "ok", "page_number": 1}]},
        },
    )


def test_slow_upload_does_not_block_other_result_collection(tmp_path):
    opts = options(tmp_path)
    fast_collected = threading.Event()

    def handle(request):
        if request.method == "POST":
            name = filename(request)
            if name == "0.pdf":
                assert fast_collected.wait(2), "slow upload blocked other task collection"
            return reply(request, name)
        if request.url.path.endswith("1.pdf"):
            fast_collected.set()
        return reply(request)

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        assert batch.run(opts, client=client, token="")["successes"] == 2


def test_slow_query_does_not_block_refilling_the_window(tmp_path):
    opts = options(tmp_path, count=3)
    third_submitted = threading.Event()

    def handle(request):
        if request.method == "POST":
            name = filename(request)
            if name == "2.pdf":
                third_submitted.set()
            return reply(request, name)
        if request.url.path.endswith("0.pdf"):
            assert third_submitted.wait(2), "slow GET blocked collecting/refilling other tasks"
        return reply(request)

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        assert batch.run(opts, client=client, token="")["successes"] == 3


def test_io_pools_are_bounded_and_journal_has_one_writer(tmp_path, monkeypatch):
    opts = options(tmp_path, count=8, max_in_flight=4, upload_concurrency=2, query_concurrency=2)
    main_thread = threading.get_ident()
    original_write = batch_runner._atomic_write
    counts = {"POST": 0, "GET": 0}
    maxima = dict(counts)
    lock = threading.Lock()

    def write(*args, **kwargs):
        assert threading.get_ident() == main_thread
        return original_write(*args, **kwargs)

    monkeypatch.setattr(batch_runner, "_atomic_write", write)

    def handle(request):
        with lock:
            counts[request.method] += 1
            maxima[request.method] = max(maxima[request.method], counts[request.method])
        time.sleep(0.03)
        with lock:
            counts[request.method] -= 1
        return reply(request, filename(request) if request.method == "POST" else None)

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        assert batch.run(opts, client=client, token="")["successes"] == 8
    assert maxima == {"POST": 2, "GET": 2}


@pytest.mark.parametrize("mode", ["parse", "images", "two-stage"])
def test_idempotency_key_is_stable_per_batch_input_attempt(tmp_path, mode):
    opts = options(tmp_path, count=1, mode=mode, max_attempts=2)
    keys = []

    def handle(request):
        if request.method == "POST":
            keys.append(request.headers["Idempotency-Key"])
            return reply(request, f"task-{len(keys)}")
        return reply(request, state="FAILURE" if len(keys) == 1 else "SUCCESS")

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        assert batch.run(opts, client=client, token="secret-token")["successes"] == 1
        assert batch.run(opts, client=client, token="secret-token")["skipped"] == 1
    manifest = json.loads((opts.output_dir / ".batch.json").read_text())
    journal = json.loads((opts.output_dir / ".tasks/0.pdf.json").read_text())
    assert len(keys) == 2 and len(set(keys)) == 2
    for attempt, key in enumerate(keys, 1):
        identity = json.dumps([manifest["batch_id"], "0.pdf", attempt], separators=(",", ":"))
        assert key == hashlib.sha256(identity.encode()).hexdigest()
    assert journal["idempotency_key"] == keys[-1]
    assert "secret-token" not in "".join(p.read_text() for p in opts.output_dir.rglob("*.json"))


def test_upload_uses_verified_snapshot_even_if_source_changes_during_http(tmp_path):
    opts = options(tmp_path, count=1)
    source = opts.input_dir / "0.pdf"
    original = source.read_bytes()

    class Transport(httpx.BaseTransport):
        def handle_request(self, request):
            if request.method == "POST":
                source.write_bytes(b"%PDF-CHANGED-after-snapshot")
                data = request.read()
                assert original in data
                assert b"CHANGED-after-snapshot" not in data
                return reply(request, "task-1")
            return reply(request)

    with httpx.Client(transport=Transport()) as client:
        assert batch.run(opts, client=client, token="")["successes"] == 1
    assert not list((opts.output_dir / ".uploads").glob("**/*.pdf"))


def test_input_changed_after_preflight_is_not_uploaded_or_marked_ambiguous(tmp_path, monkeypatch):
    opts = options(tmp_path, count=1)
    original_submit = batch.TaskAPI.submit

    def submit(self, client, path, token, **kwargs):
        path.write_bytes(b"%PDF-CHANGED-before-snapshot")
        return original_submit(self, client, path, token, **kwargs)

    monkeypatch.setattr(batch.TaskAPI, "submit", submit)

    def never(request):
        pytest.fail("Changed input must be rejected before any HTTP request")

    with httpx.Client(transport=httpx.MockTransport(never)) as client:
        with pytest.raises(ValueError, match="changed"):
            batch.run(opts, client=client, token="")
    journal = json.loads((opts.output_dir / ".tasks/0.pdf.json").read_text())
    assert journal["state"] == "NEW" and journal["attempts"] == 0


def test_ambiguous_upload_preserves_other_concurrent_submission_id(tmp_path):
    opts = options(tmp_path)
    second_started = threading.Event()

    def handle(request):
        if request.method == "POST":
            name = filename(request)
            if name == "0.pdf":
                assert second_started.wait(2)
                raise httpx.ReadTimeout("response lost", request=request)
            second_started.set()
            time.sleep(0.05)
            return reply(request, "preserved-id")
        return reply(request)

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(RuntimeError, match="unknown"):
            batch.run(opts, client=client, token="")
    states = [
        json.loads((opts.output_dir / f".tasks/{index}.pdf.json").read_text()) for index in range(2)
    ]
    assert states[0]["state"] == "SUBMITTING"
    assert states[1]["task_id"] == "preserved-id"
    assert states[1]["state"] in {"SUBMITTED", "SUCCESS"}


@pytest.mark.parametrize("name", ["upload_concurrency", "query_concurrency"])
@pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf"), 1.5])
def test_invalid_io_limits_fail_before_creating_a_batch(tmp_path, name, value):
    opts = options(tmp_path, count=1, **{name: value})
    with httpx.Client(transport=httpx.MockTransport(reply)) as client:
        with pytest.raises(ValueError):
            batch.run(opts, client=client, token="")
    assert not opts.output_dir.exists()


def test_existing_version_one_journal_resumes_without_new_header_or_upload(tmp_path):
    opts = options(tmp_path, count=1)
    identity = batch.TaskAPI(opts).identity()
    opts.output_dir.mkdir()
    (opts.output_dir / ".tasks").mkdir()
    (opts.output_dir / ".batch.json").write_text(
        json.dumps(
            {
                "identity": {
                    "version": 1,
                    "input_dir": str(opts.input_dir.resolve()),
                    "request": identity,
                },
                "batch_id": "batch-from-an-earlier-client",
            }
        )
    )
    (opts.output_dir / ".tasks/0.pdf.json").write_text(
        json.dumps(
            {
                "sha256": hashlib.sha256((opts.input_dir / "0.pdf").read_bytes()).hexdigest(),
                "request": identity,
                "attempts": 1,
                "state": "SUBMITTED",
                "task_id": "earlier-task-id",
            }
        )
    )

    def handle(request):
        assert request.method == "GET"
        assert request.url.path.endswith("earlier-task-id")
        return reply(request)

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        assert batch.run(opts, client=client, token="")["successes"] == 1


def test_legacy_runner_never_shares_requests_session_across_threads(tmp_path):
    opts = options(tmp_path)
    main_thread = threading.get_ident()

    def submit(session, path, token):
        assert threading.get_ident() == main_thread
        return path.name

    def fetch(session, task_id, token):
        assert threading.get_ident() == main_thread
        return {"state": "SUCCESS", "result": {"txt": task_id}}

    result = batch_runner.run_batch(
        object(),
        sorted(opts.input_dir.iterdir()),
        "",
        opts.output_dir,
        request={},
        submit=submit,
        fetch=fetch,
        poll_interval=0,
    )
    assert result["successes"] == 2
