"""Opt-in durable API contracts using actual input PDFs and live model workers."""

import json
import os
from pathlib import Path
import time
import uuid

import httpx
import pytest

pytestmark = [
    pytest.mark.mineru_integration,
    pytest.mark.skipif(
        os.getenv("MINERU_RUN_DURABLE_PDFS") != "1",
        reason="Set MINERU_RUN_DURABLE_PDFS=1 for live durable job acceptance",
    ),
]


@pytest.mark.parametrize(
    "endpoint", ["/mineru/task", "/mineru_with_images/task", "/two_stage/task"]
)
def test_real_pdf_submission_identity_status_and_download(endpoint, tmp_path):
    from src.config.config import FASTAPI_BEARER_TOKEN
    from src.models.models import ResponseWithPageNum

    source = Path(os.getenv("MINERU_TEST_INPUT_DIR", "input")) / "p2.pdf"
    assert source.is_file(), "The real p2 PDF is required"
    request = {"tier": "advanced", "chunk_type": "true", "return_txt": "true"}
    form = request if endpoint == "/two_stage/task" else {"tier": "advanced"}
    params = {} if endpoint == "/two_stage/task" else {"chunk_type": "true", "return_txt": "true"}
    key = uuid.uuid4().hex
    headers = {"Idempotency-Key": key}
    if FASTAPI_BEARER_TOKEN:
        headers["Authorization"] = f"Bearer {FASTAPI_BEARER_TOKEN}"
    # Save the key before the first POST, including when its response is lost.
    evidence = {"endpoint": endpoint, "idempotency_key": key, "request": request}
    journal = tmp_path / "submission.json"
    journal.write_text(json.dumps(evidence))
    with httpx.Client(
        base_url=os.getenv("MINERU_TEST_API_URL", "http://127.0.0.1:7770"),
        headers=headers,
        timeout=300,
    ) as client:
        identities = []
        for _ in range(2):
            with source.open("rb") as stream:
                response = client.post(
                    endpoint,
                    params=params,
                    data=form,
                    files={"file": (source.name, stream, "application/pdf")},
                )
            response.raise_for_status()
            identities.append(response.json()["task_id"])
            evidence["task_ids"] = identities
            journal.write_text(json.dumps(evidence))
        assert identities[0] == identities[1]
        task_id = identities[0]
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline:
            response = client.get(f"/tasks/{task_id}/status")
            response.raise_for_status()
            status = response.json()
            assert len(response.content) < 4096 and "result" not in status
            assert status["state"] not in {"FAILURE", "REVOKED", "EXPIRED"}, status
            if status["state"] == "SUCCESS":
                break
            time.sleep(1)
        else:
            pytest.fail(f"Still waiting for retained task {task_id}; do not create another ID")
        download = client.get(f"/tasks/{task_id}/result")
        download.raise_for_status()
        (tmp_path / "result.json").write_bytes(download.content)
        payload = download.json()
        assert {item["page_number"] for item in payload["result"]} == {1, 2}
        assert "1600" in payload["txt"]
        old_response = client.get(f"{endpoint}/{task_id}")
        old_response.raise_for_status()
        expected = ResponseWithPageNum(**payload).model_dump(
            exclude_none=endpoint != "/two_stage/task"
        )
        assert old_response.json()["result"] == expected
        assert client.post(f"/tasks/{task_id}/resume").status_code == 409
