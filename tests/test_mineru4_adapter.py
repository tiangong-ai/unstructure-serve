"""Behavioral contracts at the MinerU SDK / service boundary."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.services import mineru_service_full as service


@pytest.fixture
def sdk(monkeypatch):
    from mineru.parser import ParseResult

    calls = []
    payload = {
        "schema": "docvortex.middle",
        "schema_version": "2.0",
        "metadata": {"file_suffix": "pdf", "producer": {"name": "mineru", "version": "4.0.0"}},
        "extensions": {"mineru": {"tier": "standard", "parse_mode": "txt"}},
        "pages": [{"page_idx": 10, "blocks": []}],
        "is_full_document": False,
    }

    def save(self, writer):
        writer.write_string("middle_json.json", json.dumps(payload))
        writer.write("images/figure.png", b"image-bytes")

    monkeypatch.setattr(ParseResult, "save", save)

    def parse(path, **kwargs):
        calls.append((path, kwargs))
        return ParseResult.from_dict(payload)

    monkeypatch.setattr(service, "mineru_parse", parse, raising=False)
    monkeypatch.setenv("MINERU_DEFAULT_TIER", "standard")
    monkeypatch.setenv("MINERU_MODEL_VLM_SERVER_URL", "http://127.0.0.1:31000")
    monkeypatch.setattr(service, "reconcile_content_list_checkboxes", lambda *args: None)
    return calls


def test_materialized_images_and_new_fields_reach_existing_consumers(monkeypatch, tmp_path, sdk):
    source = tmp_path / "report.pdf"
    source.write_bytes(b"%PDF")
    rendered = [
        {"type": "header", "text": "header", "page_idx": 10},
        {"type": "text", "text": "Title", "text_level": 1, "page_idx": 10},
        {
            "type": "image",
            "img_path": "images/figure.png",
            "image_caption": ["caption"],
            "image_footnote": ["note"],
            "content": "visible labels",
            "page_idx": 10,
        },
        {"type": "code", "code_body": "print(1)", "page_idx": 10},
        {"type": "index", "list_items": ["Introduction"], "page_idx": 10},
        {"type": "page_footnote", "text": "footnote", "page_idx": 10},
    ]
    monkeypatch.setattr(service, "render", lambda *args, **kwargs: rendered, raising=False)
    items, output, txt = service.parse_doc(
        [source], tmp_path / "out", tier="standard", start_page_id=10, end_page_id=10
    )
    assert txt is None
    assert sdk[0][1]["page_range"] == "11-11"
    assert items[2]["img_caption"] == ["caption", "visible labels"]
    assert items[2]["img_footnote"] == ["note"]
    assert (Path(output) / items[2]["img_path"]).read_bytes() == b"image-bytes"
    assert [(item["type"], item.get("text")) for item in items[3:]] == [
        ("text", "print(1)"),
        ("list", None),
        ("text", "footnote"),
    ]
    assert [item["page_idx"] for item in items] == [10] * 6


def test_native_docx_uses_flash_without_pdf_range(monkeypatch, tmp_path, sdk):
    source = tmp_path / "report.docx"
    source.write_bytes(b"docx")
    monkeypatch.setattr(service, "render", lambda *args: [], raising=False)
    service.parse_doc([source], tmp_path / "out", backend="vlm-http-client")
    assert sdk[0][1]["tier"] == "flash"
    assert sdk[0][1]["page_range"] == ""


def test_missing_materialized_asset_fails_instead_of_skipping_vision(monkeypatch, tmp_path, sdk):
    source = tmp_path / "report.pdf"
    source.write_bytes(b"%PDF")
    monkeypatch.setattr(
        service,
        "render",
        lambda *args: [{"type": "image", "img_path": "images/missing.png", "page_idx": 0}],
        raising=False,
    )
    with pytest.raises(RuntimeError, match="[Aa]sset"):
        service.parse_doc([source], tmp_path / "out", tier="standard")


def test_missing_result_json_reports_a_parse_error(monkeypatch, tmp_path, sdk):
    from mineru.parser import ParseResult

    source = tmp_path / "report.pdf"
    source.write_bytes(b"%PDF")
    monkeypatch.setattr(ParseResult, "save", lambda self, writer: None)
    with pytest.raises(RuntimeError, match="materialized result"):
        service.parse_doc([source], tmp_path / "out", tier="standard")


def test_custom_auth_is_not_silently_dropped(monkeypatch, tmp_path, sdk):
    monkeypatch.setenv("MINERU_VLLM_AUTH_HEADER", "Basic opaque")
    source = tmp_path / "report.pdf"
    source.write_bytes(b"%PDF")
    with pytest.raises(ValueError, match="Bearer"):
        service.parse_doc([source], tmp_path / "out", tier="standard")


def test_geometry_uses_matching_units_for_image_filters(tmp_path):
    middle = SimpleNamespace(
        extensions={
            "docvortex_layout": {"pages": [{"page_idx": 0, "width_pt": 600, "height_pt": 800}]}
        }
    )
    items = service._normalize_content_list(
        [{"type": "image", "page_idx": 0, "bbox": [100, 200, 500, 600]}], tmp_path, middle
    )
    assert items[0]["bbox"] == [60, 160, 300, 480]
    assert items[0]["page_size"] == [600, 800]


def test_docker_endpoints_rotate_without_changing_global_config(monkeypatch):
    from mineru.config import config

    monkeypatch.delenv("MINERU_VLLM_AUTH_HEADER", raising=False)
    initial = config.model.vlm.model_dump()
    urls = ["http://127.0.0.1:31001", "http://127.0.0.1:31002"]
    first = service._vlm_config("vlm-vllm-engine", urls, None)
    second = service._vlm_config("vlm-vllm-engine", urls, None)
    assert first.server_url == urls[0] + "/"
    assert second.server_url == urls[1] + "/"
    assert config.model.vlm.model_dump() == initial


@pytest.mark.parametrize("start,end", [(-1, None), (2, 1), (True, None)])
def test_invalid_page_ranges_are_rejected(start, end):
    with pytest.raises(ValueError):
        service._page_range(start, end)


@pytest.mark.parametrize("debug", [False, True])
def test_model_diagnostics_require_explicit_debug(monkeypatch, tmp_path, debug):
    from mineru.parser import ParseResult
    from mineru.types import ModelJson

    source = tmp_path / "report.pdf"
    source.write_bytes(b"%PDF")
    metadata = {"file_suffix": "pdf", "producer": {"name": "mineru", "version": "4.0.2"}}
    result = ParseResult.from_dict(
        {
            "schema": "docvortex.middle",
            "schema_version": "2.0",
            "metadata": metadata,
            "extensions": {"mineru": {"tier": "flash", "parse_mode": "txt"}},
            "pages": [{"page_idx": 0, "blocks": []}],
            "is_full_document": True,
        }
    )
    result._model_output = ModelJson.model_validate(
        {
            "metadata": metadata,
            "pages": [[{"diagnostic": "private-model-trace"}]],
            "page_index_map": [0],
        }
    )
    monkeypatch.setattr(service, "mineru_parse", lambda *args, **kwargs: result)
    monkeypatch.setattr(service, "_pdf_has_substantial_text", lambda *_args: False)
    items, output, _ = service.parse_doc(
        [source], tmp_path / "out", tier="flash", dump_debug_intermediate=debug
    )
    assert items == []
    assert (Path(output) / "middle_json.json").is_file()
    assert (Path(output) / "report_content_list.json").is_file()
    assert (Path(output) / "model_output.json").exists() is debug
    assert result._model_output is not None, "Export must not mutate the SDK result"


def _write_text_layer_pdf(path: Path, page_texts: list[str]) -> None:
    from reportlab.pdfgen import canvas

    document = canvas.Canvas(str(path))
    for page_text in page_texts:
        if page_text:
            document.drawString(72, 720, page_text)
        document.showPage()
    document.save()


def test_empty_parse_fails_for_pdf_with_substantial_text(monkeypatch, tmp_path, sdk):
    source = tmp_path / "text.pdf"
    _write_text_layer_pdf(
        source, ["This source page clearly contains more than thirty two text characters."]
    )
    monkeypatch.setattr(service, "render", lambda *args: [], raising=False)

    with pytest.raises(RuntimeError, match="nonempty text layer"):
        service.parse_doc([source], tmp_path / "out", tier="advanced")


def test_empty_parse_allows_blank_selected_pdf_page(monkeypatch, tmp_path, sdk):
    source = tmp_path / "blank-selected.pdf"
    _write_text_layer_pdf(
        source, ["This first page has plenty of text that is outside the selected range.", ""]
    )
    monkeypatch.setattr(service, "render", lambda *args: [], raising=False)

    items, _, _ = service.parse_doc(
        [source], tmp_path / "out", tier="advanced", start_page_id=1, end_page_id=1
    )
    assert items == []
