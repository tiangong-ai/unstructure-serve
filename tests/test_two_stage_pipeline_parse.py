from pathlib import Path

import pytest

from src.services import mineru_service_full as msf
from src.services import two_stage_pipeline


def test_parse_doc_raises_when_sdk_returns_none(monkeypatch, tmp_path):
    source = tmp_path / "sample.pdf"
    source.write_bytes(b"%PDF-1.4\n")
    monkeypatch.setattr(msf, "mineru_parse", lambda *args, **kwargs: None)
    with pytest.raises(RuntimeError, match="MinerU returned no result"):
        msf.parse_doc([source], tmp_path, tier="flash")


def test_parse_doc_saves_and_renders_sdk_result(monkeypatch, tmp_path):
    from mineru.parser import ParseResult

    source = tmp_path / "sample.pdf"
    source.write_bytes(b"%PDF-1.4\n")
    result = ParseResult.from_dict(
        {
            "schema": "docvortex.middle",
            "schema_version": "2.0",
            "metadata": {"file_suffix": "pdf", "producer": {"name": "mineru", "version": "4.0.0"}},
            "extensions": {"mineru": {"tier": "flash", "parse_mode": "txt"}},
            "pages": [
                {
                    "page_idx": 0,
                    "blocks": [
                        {
                            "type": "text",
                            "index": 0,
                            "bbox": [0.1, 0.1, 0.9, 0.2],
                            "content": [{"type": "text", "content": "hello"}],
                        }
                    ],
                }
            ],
            "is_full_document": True,
        }
    )
    monkeypatch.setattr(msf, "mineru_parse", lambda *args, **kwargs: result)
    content, output, txt = msf.parse_doc([source], tmp_path, tier="flash")
    assert [(item["type"], item["text"], item["page_idx"]) for item in content] == [
        ("text", "hello", 0)
    ]
    assert output == str(tmp_path / "sample" / "flash")
    assert (Path(output) / "sample_content_list.json").is_file()
    assert txt is None


def _build_payload(tmp_path: Path, filename: str = "doc.pdf") -> dict:
    source = tmp_path / filename
    source.write_bytes(b"%PDF-1.4\n")
    workspace = tmp_path / "workspace"
    return {
        "source_path": str(source),
        "backend": "vlm-http-client",
        "chunk_type": False,
        "return_txt": False,
        "workspace": str(workspace),
        "cleanup_source": False,
        "extra_cleanup": [],
    }


def test_parse_task_surfaces_missing_parse_doc_result(monkeypatch, tmp_path):
    monkeypatch.setattr(two_stage_pipeline, "parse_doc", lambda *args, **kwargs: None)

    payload = _build_payload(tmp_path)
    with pytest.raises(RuntimeError, match="returned no content"):
        two_stage_pipeline.parse_task.run(payload)


def test_parse_task_wraps_parse_doc_exception(monkeypatch, tmp_path):
    def boom(*args, **kwargs):
        raise ValueError("boom")

    monkeypatch.setattr(two_stage_pipeline, "parse_doc", boom)

    payload = _build_payload(tmp_path, filename="doc2.pdf")
    with pytest.raises(RuntimeError, match="parse_doc raised"):
        two_stage_pipeline.parse_task.run(payload)


def test_vision_task_passes_provider_model_and_normalized_prompt(monkeypatch):
    captured = {}

    def fake_completion(image_path, context, prompt=None, provider=None, model=None):
        captured.update(
            {
                "image_path": image_path,
                "context": context,
                "prompt": prompt,
                "provider": provider,
                "model": model,
            }
        )
        return "vision"

    monkeypatch.setattr(two_stage_pipeline, "vision_completion", fake_completion)

    job = {
        "seq": 1,
        "img_path": "/tmp/fake.jpg",
        "context_payload": "ctx",
        "base_text": "base",
    }

    result = two_stage_pipeline.vision_task.run(
        job, provider="vllm", model="demo-model", prompt="  hello  "
    )

    assert result == {"seq": 1, "vision_text": "vision"}
    assert captured["provider"] == "vllm"
    assert captured["model"] == "demo-model"
    assert captured["prompt"] == "hello"


def test_vision_task_raises_when_completion_fails(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("vision down")

    monkeypatch.setattr(two_stage_pipeline, "vision_completion", boom)

    job = {
        "seq": 7,
        "img_path": "/tmp/fake.jpg",
        "context_payload": "ctx",
        "base_text": "base",
    }

    with pytest.raises(RuntimeError, match="Vision call failed for seq=7"):
        two_stage_pipeline.vision_task.run(job, provider="vllm", model="demo-model")


def test_two_stage_merge_keeps_mineru_reading_order_when_chunk_type_enabled():
    content_list = [
        {"type": "header", "text": "Page 1 header", "page_idx": 0},
        {"type": "text", "text": "Page 1 body", "page_idx": 0},
        {"type": "header", "text": "Page 2 header", "page_idx": 1},
        {"type": "text", "text": "Page 2 body", "page_idx": 1},
    ]

    items, txt_text = two_stage_pipeline._merge_content(
        content_list,
        [],
        chunk_type=True,
        return_txt=True,
    )

    assert [(item.text, item.page_number, item.type) for item in items] == [
        ("Page 1 header", 1, "header"),
        ("Page 1 body", 1, None),
        ("Page 2 header", 2, "header"),
        ("Page 2 body", 2, None),
    ]
    assert txt_text == "Page 1 header\nPage 1 body\nPage 2 header\nPage 2 body"
