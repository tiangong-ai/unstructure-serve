"""Opt-in PDF regression proving each vLLM data-parallel replica performs inference."""

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
        reason="Set MINERU_RUN_DP_PDFS=1 with a multi-GPU model service",
    ),
]


def _headers():
    key = os.getenv("MINERU_TEST_VLM_API_KEY", "").strip()
    return {"Authorization": f"Bearer {key}"} if key else {}


def _successes(url):
    response = httpx.get(url.rstrip("/") + "/metrics", headers=_headers(), timeout=10)
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


def test_input_pdfs_reach_all_data_parallel_replicas(tmp_path):
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
    dp_size = int(os.getenv("MINERU_TEST_DP_SIZE", "3"))
    assert dp_size > 0
    expected = {str(rank) for rank in range(dp_size)}
    assert set(before) <= expected, f"Unexpected data-parallel engines: {set(before) - expected}"
    for path, pages in sources:
        items, output, _ = parse_doc(
            [path], tmp_path, tier="advanced", server_url=url, server_headers=_headers() or None
        )
        assert {item["page_idx"] for item in items} == set(range(pages))
        for item in items:
            if item.get("img_path"):
                assert (Path(output) / item["img_path"]).is_file()
        if path.name == "p2.pdf":
            tables = "\n".join(item.get("table_body", "") for item in items)
            assert "项目名称" in tables and "1600" in tables
            assert "■公开竞争" in tables.replace("☑", "■").replace(" ", "").replace("\n", "")
    after = _successes(url)
    assert set(after) == expected, f"Expected data-parallel engines {expected}"
    delta = {rank: after[rank] - before.get(rank, 0) for rank in expected}
    (tmp_path / "replica-requests.json").write_text(
        json.dumps({"before": before, "after": after, "delta": delta})
    )
    assert all(count > 0 for count in delta.values()), delta
