"""Real PDF upload contracts, with model/broker doubles to isolate API behavior."""

import asyncio
from concurrent.futures import Future
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.datastructures import UploadFile

from src.routers import (
    mineru_router,
    mineru_sci_router,
    mineru_with_images_router,
)


from src.services import job_store, job_submission


@pytest.mark.parametrize(
    "endpoint",
    [
        "/mineru",
        "/mineru_sci",
        "/mineru_with_images",
        "/mineru/task",
        "/mineru_with_images/task",
        "/two_stage/task",
    ],
)
def test_pdf_upload_never_materializes_whole_file(client, monkeypatch, tmp_path, endpoint):
    source = Path(__file__).parents[1] / "input/p2.pdf"
    if not source.is_file():
        pytest.skip("input/p2.pdf required for real upload-byte contract")
    original_read = UploadFile.read

    async def bounded_read(self, size=-1):
        assert 0 < size <= 1024 * 1024, "Upload must use bounded reads"
        return await original_read(self, size)

    monkeypatch.setattr(UploadFile, "read", bounded_read)
    captured = []

    def submit(path, **kwargs):
        captured.append(Path(path).read_bytes())
        future = Future()
        future.set_result({"result": [{"text": "test", "page_number": 1}]})
        return future

    for module in (mineru_router, mineru_sci_router, mineru_with_images_router):
        monkeypatch.setattr(module, "scheduler", SimpleNamespace(submit=submit))

    def publish(record):
        captured.append(job_store.source_path(record["job_id"]).read_bytes())

    monkeypatch.setattr(job_submission, "publish_record", publish)
    with source.open("rb") as file:
        response = client.post(endpoint, files={"file": ("p2.pdf", file, "application/pdf")})
    assert response.status_code == 200, response.text
    assert captured == [source.read_bytes()]


@pytest.mark.parametrize(
    "endpoint", ["/mineru/task", "/mineru_with_images/task", "/two_stage/task"]
)
def test_broker_submission_runs_outside_event_loop(client, monkeypatch, tmp_path, endpoint):
    called_in_loop = []

    def assert_thread():
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            called_in_loop.append(False)
        else:
            called_in_loop.append(True)
        return SimpleNamespace(id="nonblocking-test", state="PENDING")

    monkeypatch.setattr(job_submission, "publish_record", lambda record: assert_thread())
    response = client.post(endpoint, files={"file": ("p2.pdf", b"%PDF-1.4", "application/pdf")})
    assert response.status_code == 200, response.text
    assert called_in_loop == [False]


def test_persistence_bounds_memory_and_removes_partial_output(tmp_path):
    from src.utils.upload_io import persist_upload
    import io

    class BoundedStream(io.BytesIO):
        def read(self, size=-1):
            assert 0 < size <= 1024 * 1024
            return super().read(size)

    data = b"x" * (3 * 1024 * 1024 + 17)
    target = tmp_path / "large.pdf"
    persist_upload(UploadFile(BoundedStream(data)), target)
    assert target.read_bytes() == data

    class BrokenStream(BoundedStream):
        def read(self, size=-1):
            if self.tell():
                raise OSError("upload source failed")
            return super().read(size)

    with pytest.raises(OSError, match="upload source failed"):
        persist_upload(UploadFile(BrokenStream(data)), target)
    assert not target.exists()


def test_timeout_keeps_source_until_running_parser_finishes(tmp_path):
    from src.utils.upload_io import await_parse_future, cleanup_after_parse

    path = tmp_path / "source.pdf"
    path.write_bytes(b"%PDF")
    future = Future()
    future.set_running_or_notify_cancel()

    async def request():
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(await_parse_future(future), timeout=0.01)
        cleanup_after_parse({str(path)}, future)
        assert path.exists()
        assert not future.cancelled()
        future.set_result({"result": []})
        await asyncio.sleep(0)

    asyncio.run(request())
    assert not path.exists()
