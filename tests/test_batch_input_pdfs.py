"""Opt-in real batch acceptance; full private p2/paper PDFs, no model stubs."""

import json
import os
from pathlib import Path
import shutil

import pytest

from src.scripts.batch_parse import Options, run

pytestmark = [
    pytest.mark.mineru_integration,
    pytest.mark.skipif(
        os.getenv("MINERU_RUN_BATCH_PDFS") != "1",
        reason="Set MINERU_RUN_BATCH_PDFS=1 for real batch API acceptance",
    ),
]
INPUT = Path(__file__).parents[1] / "input"
PAPER = "wu-et-al-2025-carbon-footprint-of-battery-grade-lithium-chemicals-in-china.pdf"


def prepare(tmp_path, mode, names):
    source = tmp_path / "input"
    source.mkdir()
    for name in names:
        original = INPUT / name
        assert original.is_file(), original
        shutil.copyfile(original, source / name)
    return Options(
        input_dir=source,
        output_dir=tmp_path / "out",
        mode=mode,
        base_url=os.getenv("MINERU_TEST_API_URL", "http://127.0.0.1:7770"),
        return_txt=True,
        poll_interval=1,
        poll_timeout=900,
    )


@pytest.mark.parametrize("mode", ["parse", "images", "two-stage"])
def test_actual_pdf_batch_and_resume(tmp_path, mode):
    opts = prepare(tmp_path, mode, ["p2.pdf", PAPER])
    assert run(opts) == {"successes": 2, "failures": 0, "skipped": 0}
    p2 = json.loads((opts.output_dir / "results/p2.pdf.json").read_text())
    assert "1600" in p2["txt"]
    assert {item["page_number"] for item in p2["result"]} == {1, 2}
    paper = json.loads((opts.output_dir / f"results/{PAPER}.json").read_text())
    assert {item["page_number"] for item in paper["result"]} == set(range(1, 10))
    if mode != "parse":
        assert any(item.get("type") == "image" and item["text"].strip() for item in paper["result"])
    assert run(opts) == {"successes": 0, "failures": 0, "skipped": 2}
