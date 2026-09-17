from __future__ import annotations

import concurrent.futures

from src.routers import mineru_router, mineru_sci_router
from src.services import mineru_task_runner


def _reading_order_payload() -> dict:
    return {
        "result": [
            {"text": "Page 1 header", "page_number": 1, "type": "header"},
            {"text": "Page 1 body", "page_number": 1},
            {"text": "Page 2 header", "page_number": 2, "type": "header"},
            {"text": "Page 2 body", "page_number": 2},
        ]
    }


def _fake_submit(*args, **kwargs):
    future = concurrent.futures.Future()
    future.set_result(_reading_order_payload())
    return future


def test_mineru_keeps_mineru_reading_order_when_chunk_type_enabled(client, monkeypatch):
    monkeypatch.setattr(mineru_router.scheduler, "submit", _fake_submit)

    response = client.post(
        "/mineru",
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


def test_mineru_sci_keeps_mineru_reading_order_when_chunk_type_enabled(client, monkeypatch):
    monkeypatch.setattr(mineru_sci_router.scheduler, "submit", _fake_submit)

    response = client.post(
        "/mineru_sci",
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


def test_mineru_task_runner_keeps_mineru_reading_order_when_chunk_type_enabled(monkeypatch):
    monkeypatch.setattr(mineru_task_runner.scheduler, "submit", _fake_submit)

    items, txt_text = mineru_task_runner._parse_with_scheduler(
        "/tmp/sample.pdf",
        chunk_type=True,
        return_txt=True,
        backend_value="vlm-http-client",
    )

    assert [(item.text, item.page_number, item.type) for item in items] == [
        ("Page 1 header", 1, "header"),
        ("Page 1 body", 1, None),
        ("Page 2 header", 2, "header"),
        ("Page 2 body", 2, None),
    ]
    assert txt_text == "Page 1 header\nPage 1 body\nPage 2 header\nPage 2 body"
