"""Opt-in real PDF -> MinerU image -> configured multimodal endpoint regression."""

import json
import os
from pathlib import Path
import re

import pytest

pytestmark = [
    pytest.mark.mineru_integration,
    pytest.mark.skipif(
        os.getenv("MINERU_RUN_VISION_PDFS") != "1",
        reason="Set MINERU_RUN_VISION_PDFS=1 with MinerU and vision model services",
    ),
]


def test_paper_figure_retains_numeric_labels_and_units(tmp_path):
    from src.services.mineru_service_full import parse_doc
    from src.services.two_stage_pipeline import _build_image_jobs
    from src.services.vision_service import vision_completion
    from src.utils.text_output import sanitize_vision_text

    source_dir = Path(os.getenv("MINERU_TEST_INPUT_DIR", Path(__file__).parents[1] / "input"))
    source = (
        source_dir
        / "wu-et-al-2025-carbon-footprint-of-battery-grade-lithium-chemicals-in-china.pdf"
    )
    assert source.is_file(), source
    items, output, _ = parse_doc(
        [source], tmp_path, tier="advanced", start_page_id=4, end_page_id=4
    )
    jobs, _ = _build_image_jobs(items, output)
    matches = [job for job in jobs if re.search(r"Figure\s*4\b", job["base_text"])]
    assert len(matches) == 1, "Expected the six-panel waterfall figure on source page five"
    job = matches[0]
    assert Path(job["img_path"]).is_file()
    text = sanitize_vision_text(vision_completion(job["img_path"], job["context_payload"]))
    (tmp_path / "vision-result.json").write_text(json.dumps({"text": text}, ensure_ascii=False))
    # Visible labels, not equality with another stochastic model response.
    for number in ("13.3", "24.5", "8.5", "11.5", "15.3", "4.4", "9.0", "11.7"):
        assert re.search(rf"(?<![\d.]){re.escape(number)}(?![\d.])", text), number
    assert re.search(r"52\s*%", text)
    assert re.search(r"CO[₂2]|CO_\{?2", text, re.I)
    assert not re.search(r"<think>|\[Page\s|\[ChunkType=", text, re.I)
