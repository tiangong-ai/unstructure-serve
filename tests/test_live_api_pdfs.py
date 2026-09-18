"""Opt-in real HTTP and Celery acceptance using private input PDFs.

Unlike router tests, this connects to an already running deployment. Do not run
against a busy service. No retrying submissions after ambiguous HTTP failures.
"""

import json
import os
from pathlib import Path
import subprocess
import time

import httpx
import pytest

pytestmark = [
    pytest.mark.mineru_integration,
    pytest.mark.skipif(
        os.getenv("MINERU_RUN_API_PDFS") != "1",
        reason="Set MINERU_RUN_API_PDFS=1 for live API acceptance",
    ),
]
INPUT = Path(os.getenv("MINERU_TEST_INPUT_DIR", Path(__file__).parents[1] / "input"))
PAPER = "wu-et-al-2025-carbon-footprint-of-battery-grade-lithium-chemicals-in-china.pdf"


@pytest.fixture
def live_client():
    from src.config.config import FASTAPI_BEARER_TOKEN

    headers = {"Authorization": f"Bearer {FASTAPI_BEARER_TOKEN}"} if FASTAPI_BEARER_TOKEN else {}
    with httpx.Client(
        base_url=os.getenv("MINERU_TEST_API_URL", "http://127.0.0.1:7770"),
        headers=headers,
        timeout=300,
    ) as client:
        yield client


def _submit(client, endpoint, source, data=None):
    assert source.is_file(), source
    with source.open("rb") as file:
        response = client.post(
            endpoint, data=data or {}, files={"file": (source.name, file, "application/pdf")}
        )
    response.raise_for_status()
    return response.json()


def _wait(client, endpoint, task_id):
    deadline = time.monotonic() + 600
    while time.monotonic() < deadline:
        response = client.get(f"{endpoint}/{task_id}")
        response.raise_for_status()
        body = response.json()
        if body["state"] == "SUCCESS":
            return body["result"]
        assert body["state"] not in {"FAILURE", "REVOKED"}, body
        time.sleep(1)
    raise TimeoutError(f"Existing task {task_id} still pending; do not resubmit")


def _p2(payload):
    assert {item["page_number"] for item in payload["result"]} == {1, 2}
    assert "1600" in payload["txt"]


def test_live_sync_pdf(live_client):
    _p2(_submit(live_client, "/mineru?return_txt=true", INPUT / "p2.pdf"))


def test_live_ordinary_pdf_task(live_client, tmp_path):
    body = _submit(live_client, "/mineru/task?return_txt=true", INPUT / "p2.pdf")
    (tmp_path / "task.json").write_text(json.dumps(body))
    _p2(_wait(live_client, "/mineru/task", body["task_id"]))


def test_live_two_stage_paper(live_client, tmp_path):
    body = _submit(
        live_client, "/two_stage/task", INPUT / PAPER, {"return_txt": "true", "chunk_type": "true"}
    )
    (tmp_path / "task.json").write_text(json.dumps(body))
    result = _wait(live_client, "/two_stage/task", body["task_id"])
    assert {item["page_number"] for item in result["result"]} == set(range(1, 10))
    assert any(item.get("type") == "image" and item["text"].strip() for item in result["result"])
    assert "Carbon Footprint" in result["txt"]


def test_live_office_conversion_from_pdf_text(live_client, tmp_path):
    from docx import Document

    source = INPUT / "p2.pdf"
    assert source.is_file(), source
    text = subprocess.check_output(["pdftotext", str(source), "-"], text=True)
    doc = Document()
    for paragraph in text.splitlines():
        doc.add_paragraph(paragraph)
    target = tmp_path / "from-p2.docx"
    doc.save(target)
    result = _submit(live_client, "/mineru_with_images?return_txt=true", target)
    assert "1600" in result["txt"]
    assert result["result"]
