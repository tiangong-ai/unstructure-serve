from contextlib import closing
from pathlib import Path

import pypdfium2 as pdfium
import pytest

from src.scripts.build_pdf_case import build_case


def test_repeated_actual_pdf_preserves_each_source_page(tmp_path):
    source = Path("input/p2.pdf")
    if not source.is_file():
        pytest.skip("Private input PDF unavailable; opt-in long tests require it")
    manifest = build_case([source], 7, tmp_path / "case")
    assert [item["source_page"] for item in manifest["page_map"]] == [1, 2, 1, 2, 1, 2, 1]
    with (
        pdfium.PdfDocument(source) as original,
        pdfium.PdfDocument(tmp_path / "case/case.pdf") as expanded,
    ):
        assert len(expanded) == 7
        for i in range(7):
            with closing(original[i % 2]) as a, closing(expanded[i]) as b:
                with closing(a.get_textpage()) as ta, closing(b.get_textpage()) as tb:
                    assert ta.get_text_range() == tb.get_text_range()
    with pytest.raises(FileExistsError):
        build_case([source], 7, tmp_path / "case")


def test_invalid_case_does_not_create_output(tmp_path):
    with pytest.raises(ValueError):
        build_case([], 1, tmp_path / "case")
    with pytest.raises(ValueError):
        build_case([Path("missing.pdf")], 0, tmp_path / "case")
    assert not (tmp_path / "case").exists()
