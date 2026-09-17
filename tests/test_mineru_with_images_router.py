from __future__ import annotations

import concurrent.futures
from pathlib import Path

from src.routers import mineru_with_images_router as router


def test_mineru_with_images_rejects_markdown(client, monkeypatch):
    called = False

    def fake_submit(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("scheduler should not be called for Markdown uploads")

    monkeypatch.setattr(router.scheduler, "submit", fake_submit)

    response = client.post(
        "/mineru_with_images",
        files={"file": ("sample.md", b"# Title\n\nBody", "text/markdown")},
    )

    assert response.status_code == 400
    assert "Unsupported file type" in response.json()["detail"]
    assert called is False


def test_mineru_with_images_docx_return_txt_uses_native_docx_payload_txt(
    client, monkeypatch, tmp_path
):
    captured: dict[str, object] = {}

    def fake_convert_to_pdf(input_path: str, extension: str):
        assert extension == ".docx"
        pdf_path = f"{input_path}.pdf"
        Path(pdf_path).write_bytes(b"%PDF-1.4\n")
        return pdf_path, [pdf_path]

    def fake_submit(file_path: str, pipeline: str = "default", **kwargs):
        captured["file_path"] = file_path
        captured["pipeline"] = pipeline
        captured["kwargs"] = kwargs
        future = concurrent.futures.Future()
        future.set_result(
            {
                "result": [
                    {
                        "text": "JSON body chunk",
                        "page_number": 1,
                    }
                ],
                "txt": "native docx txt with image summary",
            }
        )
        return future

    monkeypatch.setattr(router, "maybe_convert_to_pdf", fake_convert_to_pdf)
    monkeypatch.setattr(router.scheduler, "submit", fake_submit)

    response = client.post(
        "/mineru_with_images",
        files={
            "file": (
                "sample.docx",
                b"fake-docx",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
        params={"return_txt": "true"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "result": [{"text": "JSON body chunk", "page_number": 1}],
        "txt": "native docx txt with image summary",
    }
    assert captured["pipeline"] == "images"
    assert str(captured["file_path"]).endswith(".pdf")
    assert captured["kwargs"]["txt_from_native_docx"] is True
    assert str(captured["kwargs"]["txt_source_path"]).endswith(".docx")


def test_mineru_with_images_invalid_model_no_longer_returns_422(client, monkeypatch):
    captured: dict[str, object] = {}

    def fake_submit(file_path: str, pipeline: str = "default", **kwargs):
        captured["file_path"] = file_path
        captured["pipeline"] = pipeline
        captured["kwargs"] = kwargs
        future = concurrent.futures.Future()
        future.set_result(
            {
                "result": [
                    {
                        "text": "body",
                        "page_number": 1,
                    }
                ]
            }
        )
        return future

    monkeypatch.setattr(router.scheduler, "submit", fake_submit)

    response = client.post(
        "/mineru_with_images",
        files={"file": ("sample.pdf", b"%PDF-1.4\n", "application/pdf")},
        data={"provider": "missing-provider", "model": "missing-model"},
    )

    assert response.status_code == 200
    assert response.json() == {"result": [{"text": "body", "page_number": 1}]}
    assert captured["pipeline"] == "images"
    assert captured["kwargs"]["vision_provider"] == "missing-provider"
    assert captured["kwargs"]["vision_model"] == "missing-model"


def test_mineru_with_images_keeps_mineru_reading_order_when_chunk_type_enabled(client, monkeypatch):
    def fake_submit(*args, **kwargs):
        future = concurrent.futures.Future()
        future.set_result(
            {
                "result": [
                    {"text": "Page 1 header", "page_number": 1, "type": "header"},
                    {"text": "Page 1 body", "page_number": 1},
                    {"text": "Page 2 header", "page_number": 2, "type": "header"},
                    {"text": "Page 2 body", "page_number": 2},
                ]
            }
        )
        return future

    monkeypatch.setattr(router.scheduler, "submit", fake_submit)

    response = client.post(
        "/mineru_with_images",
        params={"chunk_type": "true", "return_txt": "true"},
        files={"file": ("sample.pdf", b"%PDF-1.4\n", "application/pdf")},
    )

    assert response.status_code == 200
    payload = response.json()
    assert [item["text"] for item in payload["result"]] == [
        "Page 1 header",
        "Page 1 body",
        "Page 2 header",
        "Page 2 body",
    ]
    assert payload["txt"] == "Page 1 header\nPage 1 body\nPage 2 header\nPage 2 body"
