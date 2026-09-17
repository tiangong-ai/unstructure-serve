"""Real input-PDF contracts; opt in with MINERU_RUN_INPUT_PDFS=1.

Run after installing the selected MinerU runtime and preparing its model service.
No uploaded document content is copied into the test source or committed fixtures.
"""

import json
import os
from pathlib import Path

import pytest

from src.services.mineru_service_full import parse_doc

INPUT_DIR = Path(os.getenv("MINERU_TEST_INPUT_DIR", Path(__file__).parents[1] / "input"))
PDF_NAMES = (
    "B19_NIO, Inc..pdf",
    "fese.pdf",
    "p2.pdf",
    "wu-et-al-2025-carbon-footprint-of-battery-grade-lithium-chemicals-in-china.pdf",
    "人工智能.清华大学出版社.pdf",
    "人工智能导论(第4版).高等教育出版社.pdf",
    "人工智能导论.北京邮电大学出版社.pdf",
    "人工智能简史_湛庐文化.pdf",
    "大连理工大学-环境生态工程-赵冬明.pdf",
    "开封民众制药有限公司年产110吨利福平、12吨利福霉素钠监测报告(1).pdf",
    "汴环审批书[2024]14号报告表最终版(1).pdf",
)
pytestmark = [
    pytest.mark.mineru_integration,
    pytest.mark.skipif(
        os.getenv("MINERU_RUN_INPUT_PDFS") != "1",
        reason="Set MINERU_RUN_INPUT_PDFS=1 to parse the actual input PDFs",
    ),
]


def _check_assets(items, output_dir):
    for item in items:
        if item.get("img_path"):
            image = Path(output_dir) / item["img_path"]
            assert image.is_file(), f"Missing {item['type']} asset: {image}"
            assert image.stat().st_size > 0


@pytest.mark.parametrize("tier", ["flash", "basic", "standard", "advanced"])
def test_p2_full_document_preserves_table_and_checked_options(tmp_path, tier):
    source = INPUT_DIR / "p2.pdf"
    assert source.is_file(), source
    items, output_dir, txt = parse_doc([source], tmp_path, tier=tier)
    assert txt is None
    assert {item["page_idx"] for item in items} == {0, 1}
    tables = "\n".join(item.get("table_body", "") for item in items)
    assert "项目名称" in tables
    assert "1600" in tables
    # Normalize selected glyphs, but require both the mark and the actual option.
    compact = tables.replace("☑", "■").replace(" ", "").replace("\n", "")
    assert "■重大共性关键技术" in compact
    assert "■公开竞争" in compact
    _check_assets(items, output_dir)


@pytest.mark.parametrize("filename", PDF_NAMES)
def test_input_pdf_first_and_late_pages_keep_source_indices(filename, tmp_path):
    import pypdfium2 as pdfium

    source = INPUT_DIR / filename
    assert source.is_file(), source
    with pdfium.PdfDocument(str(source)) as document:
        count = len(document)
    # Includes page 11 (Doclib's default cutoff) and the end of the 1,016-page PDF.
    page_ids = sorted({0, min(10, count - 1), count - 1})
    reports = []
    for page_id in page_ids:
        items, output_dir, _ = parse_doc(
            [source],
            tmp_path / str(page_id),
            tier="standard",
            start_page_id=page_id,
            end_page_id=page_id,
        )
        assert all(item["page_idx"] == page_id for item in items)
        _check_assets(items, output_dir)
        reports.append({"page": page_id + 1, "blocks": len(items)})
    assert any(report["blocks"] for report in reports), "All selected pages were empty"
    (tmp_path / "coverage.json").write_text(json.dumps(reports), encoding="utf-8")


def test_research_paper_keeps_body_equations_and_vision_assets(tmp_path):
    from src.services.two_stage_pipeline import _build_image_jobs

    source = INPUT_DIR / PDF_NAMES[3]
    items, output_dir, _ = parse_doc([source], tmp_path, tier="standard")
    assert {item["page_idx"] for item in items} == set(range(9))
    text = "\n".join(item.get("text", "") for item in items)
    assert "Carbon Footprint" in text
    assert any(item["type"] == "equation" and item.get("text") for item in items)
    assert any(item["type"] == "image" and item.get("img_path") for item in items)
    _check_assets(items, output_dir)
    jobs, _ = _build_image_jobs(items, output_dir)
    assert jobs, "Real paper figures must survive two-stage geometry and asset filtering"
    assert all(Path(job["img_path"]).is_file() for job in jobs)


def test_full_46_page_pdf_is_not_truncated_to_ten_pages(tmp_path):
    source = INPUT_DIR / "fese.pdf"
    items, output_dir, _ = parse_doc([source], tmp_path, tier="standard")
    middle = json.loads((Path(output_dir) / "middle_json.json").read_text("utf-8"))
    assert middle["is_full_document"] is True
    assert {page["page_idx"] for page in middle["pages"]} == set(range(46))
    assert any(item["page_idx"] > 10 for item in items)
    _check_assets(items, output_dir)
