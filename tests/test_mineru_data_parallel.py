"""Opt-in PDF regression proving all three vLLM replicas perform inference."""

import json
import os
from pathlib import Path
import re

import httpx
import pytest

from src.services.mineru_service_full import parse_doc

pytestmark = [
    pytest.mark.mineru_integration,
    pytest.mark.skipif(
        os.getenv("MINERU_RUN_DP_PDFS") != "1",
        reason="Set MINERU_RUN_DP_PDFS=1 with a three-GPU model service",
    ),
]


def _successes(url):
    response = httpx.get(url.rstrip("/") + "/metrics", timeout=10)
    response.raise_for_status()
    counts = {}
    for labels, value in re.findall(
        r"^vllm:request_success_total\{([^}]+)\} ([\d.eE+-]+)$", response.text, re.M
    ):
        labels = dict(re.findall(r'(\w+)="([^"]*)"', labels))
        if labels.get("finished_reason") not in {"stop", "length"}:
            continue
        rank = labels["engine"]
        counts[rank] = counts.get(rank, 0) + float(value)
    return counts


def test_input_pdfs_reach_all_three_data_parallel_replicas(tmp_path):
    source_dir = Path(os.getenv("MINERU_TEST_INPUT_DIR", Path(__file__).parents[1] / "input"))
    sources = [
        (source_dir / "p2.pdf", 2),
        (
            source_dir
            / "wu-et-al-2025-carbon-footprint-of-battery-grade-lithium-chemicals-in-china.pdf",
            9,
        ),
    ]
    assert all(path.is_file() for path, _ in sources)
    url = os.getenv("MINERU_TEST_VLM_URL", "http://127.0.0.1:30000")
    before = _successes(url)
    assert set(before) == {"0", "1", "2"}, "Expected three data-parallel engines"
    for path, pages in sources:
        items, output, _ = parse_doc([path], tmp_path, tier="advanced", server_url=url)
        assert {item["page_idx"] for item in items} == set(range(pages))
        for item in items:
            if item.get("img_path"):
                assert (Path(output) / item["img_path"]).is_file()
        if path.name == "p2.pdf":
            tables = "\n".join(item.get("table_body", "") for item in items)
            assert "项目名称" in tables and "1600" in tables
            assert "■公开竞争" in tables.replace("☑", "■").replace(" ", "").replace("\n", "")
    after = _successes(url)
    delta = {rank: after[rank] - before[rank] for rank in before}
    (tmp_path / "replica-requests.json").write_text(
        json.dumps({"before": before, "after": after, "delta": delta})
    )
    assert all(count > 0 for count in delta.values()), delta
