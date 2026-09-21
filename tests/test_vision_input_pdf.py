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


def test_paper_figure_retains_numeric_labels_and_units(tmp_path, monkeypatch):
    import asyncio
    import httpx

    from src.services.mineru_service_full import parse_doc
    from src.services.two_stage_pipeline import _build_image_jobs
    from src.services.vision_service import vision_completion
    from src.utils.text_output import sanitize_vision_text
    from src.services.vision_capacity import EndpointScheduler, endpoint_key
    from src.services.vision_health import probe_endpoint
    from src.services.vision_service_vllm import _resolve_base_urls, _resolve_api_key

    # Isolated routing state; probe the actual remote services without stopping
    # or changing any shared model. Exercise a real half-open inference below.
    monkeypatch.setenv("VLLM_VISION_SLOT_DIR", str(tmp_path / "vision-slots"))
    monkeypatch.setenv("VLLM_VISION_COOLDOWN_SECONDS", "0")
    capacity = EndpointScheduler.from_env()
    urls = _resolve_base_urls()
    assert urls, "Real vision endpoints must be configured"

    async def probe():
        async with httpx.AsyncClient() as client:
            for url in urls:
                result = await probe_endpoint(client, url, _resolve_api_key(), timeout=2)
                key = endpoint_key(url)
                capacity.record_health(
                    key, healthy=result.healthy, models=result.models, reason=result.reason
                )
                if result.healthy:
                    capacity.mark_failed(key)

    asyncio.run(probe())
    keys = [endpoint_key(url) for url in urls]
    before = capacity.health_snapshot(keys)
    assert any(row["healthy"] and row["circuit"] == "recovery" for row in before)

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
    after = capacity.health_snapshot(keys)
    assert any(row["healthy"] and row["circuit"] == "closed" for row in after)
    (tmp_path / "vision-health.json").write_text(json.dumps({"before": before, "after": after}))
    (tmp_path / "vision-result.json").write_text(json.dumps({"text": text}, ensure_ascii=False))
    # Visible labels, not equality with another stochastic model response.
    for number in ("13.3", "24.5", "8.5", "11.5", "15.3", "4.4", "9.0", "11.7"):
        assert re.search(rf"(?<![\d.]){re.escape(number)}(?![\d.])", text), number
    assert re.search(r"52\s*%", text)
    # Accept plain/Unicode CO2 and LaTeX grouped forms such as \mathrm{CO}_{2eq}.
    assert re.search(r"CO[₂2]|CO\}?\s*_\{?2", text, re.I)
    assert not re.search(r"<think>|\[Page\s|\[ChunkType=", text, re.I)
