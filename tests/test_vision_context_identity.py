from copy import deepcopy

from PIL import Image
import pytest

from src.services import mineru_with_images_service as images
from src.services.durable_pipeline import _all_image_jobs
from src.services.two_stage_pipeline import _build_image_jobs
from src.services.vision_prompts import vision_request_key


def _pages(tmp_path, field):
    Image.new("RGB", (200, 200), "white").save(tmp_path / "figure.png")
    content = []
    for page, literal in enumerate(("[Page 1]", "[Page 9]", "[Page 1]")):
        before = {"type": "text", "text": "Before", "page_idx": page}
        figure = {
            "type": "image",
            "img_path": "figure.png",
            "img_caption": ["Printed caption"],
            "page_idx": page,
        }
        after = {"type": "text", "text": "After", "page_idx": page}
        if field == "before":
            before["text"] += " " + literal
        elif field == "after":
            after["text"] += " " + literal
        else:
            figure[field] = [literal]
        content.extend([before, figure, after])
    return content


@pytest.mark.parametrize("field", ["before", "after", "img_caption", "img_footnote"])
def test_literal_page_references_remain_distinct_in_every_image_mode(tmp_path, monkeypatch, field):
    monkeypatch.setattr(images, "CONTEXT_WINDOW", 1)
    content = _pages(tmp_path, field)
    for build in (_build_image_jobs, _all_image_jobs):
        jobs, annotated = build(deepcopy(content), str(tmp_path), keep_positions=False)
        assert len(jobs) == 2
        assert [x["__image_seq"] for x in annotated if x["type"] == "image"] == [1, 2, 1]
    requests = []
    monkeypatch.setattr(
        images,
        "vision_completion",
        lambda image, context, *args: requests.append(context) or "Measured 52%",
    )
    result = images._run_image_vision(content, str(tmp_path))
    assert len(requests) == 2 and len(result) == 3


def test_cache_hash_never_removes_any_supplied_printed_text():
    assert vision_request_key("same pixels", "Citation [Page 1]") != vision_request_key(
        "same pixels", "Citation [Page 9]"
    )


def test_generated_positions_are_omitted_only_from_cache_context(tmp_path, monkeypatch):
    monkeypatch.setattr(images, "CONTEXT_WINDOW", 1)
    content = _pages(tmp_path, "before")
    blocks = images._build_context_blocks(content)
    idx = images._reindex_blocks(blocks)[id(content[1])]
    contexts = images._resolve_context_windows(blocks, idx, content[1])
    prompt, _ = images._build_vision_prompt(content[1], contexts)
    assert (
        prompt
        == "Image caption (Page 1): Printed caption\nContext before: [Page 1] [ChunkType=Body] Before [Page 1]\nContext after: [Page 1] [ChunkType=Body] After"
    )
    canonical = images._build_vision_cache_context(blocks, idx, content[1])
    assert (
        canonical
        == "Image caption: Printed caption\nContext before: [ChunkType=Body] Before [Page 1]\nContext after: [ChunkType=Body] After"
    )
    assert (
        images._build_vision_cache_context(blocks, idx, content[1], keep_positions=True) == prompt
    )


@pytest.mark.parametrize("strict", [False, True])
def test_custom_and_strict_ocr_requests_keep_generated_positions(tmp_path, monkeypatch, strict):
    monkeypatch.setattr(images, "CONTEXT_WINDOW", 1)
    content = _pages(tmp_path, "before")
    for build in (_build_image_jobs, _all_image_jobs):
        jobs, _ = build(deepcopy(content), str(tmp_path), keep_positions=True)
        assert len(jobs) == 3
    requests = []
    monkeypatch.setattr(
        images,
        "vision_completion",
        lambda image, context, *args: requests.append(context) or "Measured 52%",
    )
    images._run_image_vision(
        content,
        str(tmp_path),
        vision_prompt=None if strict else "Custom extraction",
        strict_ocr_only=strict,
    )
    assert len(requests) == 3
    assert all(
        any(f"[Page {page}] [ChunkType=Body]" in context for context in requests)
        for page in range(1, 4)
    )
