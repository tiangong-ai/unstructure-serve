from threading import Event, Lock
from types import SimpleNamespace

import pytest

from src.utils.text_output import (
    UNRECOGNIZED_IMAGE_TEXT,
    UnusableVisionOutput,
    sanitize_vision_text,
    validate_vision_output,
)
import src.services.mineru_with_images_service as images
import src.services.vision_service_openai_compatible as compatible
from src.services.vision_prompts import build_vision_prompt


@pytest.mark.parametrize(
    "text, expected",
    [
        (
            "<think>internal reasoning\nnot output</think>\nHere is the summary:\nYield: 52%; unreadable label.",
            "Yield: 52%; unreadable label.",
        ),
        ("以下是图片的分析：\n产量约 385 kt，标签无法辨认。", "产量约 385 kt，标签无法辨认。"),
        ("Image Description: [Page 2] [ChunkType=Image]\nCO₂: 11.7 t/t", "CO₂: 11.7 t/t"),
        (
            "Summary of carbon emissions: 52% lower\nThe image shows 6.1 t CO₂/t.",
            "Summary of carbon emissions: 52% lower\nThe image shows 6.1 t CO₂/t.",
        ),
    ],
)
def test_sanitize_only_known_generated_wrappers(text, expected):
    assert sanitize_vision_text(text) == expected


def test_strict_ocr_preserves_literal_boilerplate():
    text = "Here is the summary:\n<think>printed text</think>\n52%"
    assert sanitize_vision_text(text, strip_boilerplate=False) == text


def test_unclosed_reasoning_is_not_returned():
    with pytest.raises(RuntimeError, match="reasoning"):
        sanitize_vision_text("<think>unfinished reasoning")


def test_prompt_preserves_facts_and_custom_ocr():
    prompt = build_vision_prompt("Figure 1: output 52%", None)
    assert "units" in prompt and "unreadable" in prompt
    assert "Do not repeat" in prompt and "Figure 1: output 52%" in prompt
    assert build_vision_prompt("", "Literal OCR only") == "Literal OCR only"


@pytest.mark.parametrize(
    "reason, content, error",
    [
        ("length", "partial 52", RuntimeError),
        ("stop", "", UnusableVisionOutput),
        ("stop", None, UnusableVisionOutput),
        ("stop", "<think>unfinished reasoning", RuntimeError),
        (None, "<think>unfinished reasoning", RuntimeError),
    ],
)
def test_compatible_rejects_incomplete_or_empty_output(monkeypatch, reason, content, error):
    monkeypatch.setattr(compatible, "encode_image", lambda _: "base64")
    response = SimpleNamespace(
        choices=[SimpleNamespace(finish_reason=reason, message=SimpleNamespace(content=content))]
    )
    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **_: response))
    )
    pool = SimpleNamespace(get_client=lambda: client)
    with pytest.raises(error):
        compatible.vision_completion_openai_compatible(
            "x.jpg",
            default_model="model",
            client_pool=pool,
            output_validator=validate_vision_output,
        )


def test_image_window_refills_before_slow_first_image_finishes(monkeypatch, tmp_path):
    # Third image must start while first is still waiting: the old batch barrier fails.
    third_started = Event()
    lock = Lock()
    active = peak = 0
    contents = []
    for i in range(3):
        (tmp_path / f"{i}.jpg").write_bytes(f"distinct image {i}".encode())
        contents.append({"type": "image", "img_path": f"{i}.jpg", "page_idx": 0})
    monkeypatch.setattr(images, "VISION_BATCH_SIZE", 2)

    def vision(path, *args):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        try:
            if path.endswith("0.jpg"):
                assert third_started.wait(2), "batch barrier blocked third image"
            if path.endswith("2.jpg"):
                third_started.set()
            return path.rsplit("/", 1)[-1]
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(images, "vision_completion", vision)
    result = images._run_image_vision(contents, str(tmp_path))
    assert [result[id(item)] for item in contents] == ["0.jpg", "1.jpg", "2.jpg"]
    assert peak <= 2


@pytest.mark.parametrize("text", ["<think>reasoning only</think>", "Here is the summary:"])
def test_wrapper_only_output_fails_instead_of_falling_back_to_caption(text):
    with pytest.raises(ValueError, match="no facts"):
        sanitize_vision_text(text)


def test_unrecognized_image_is_marked_and_other_images_continue(monkeypatch, tmp_path):
    contents = []
    for index in range(2):
        path = tmp_path / f"{index}.jpg"
        path.write_bytes(f"image {index}".encode())
        contents.append({"type": "image", "img_path": path.name, "page_idx": index})

    def vision(path, *args):
        if path.endswith("0.jpg"):
            raise UnusableVisionOutput("no usable facts")
        return "Visible value: 42"

    monkeypatch.setattr(images, "vision_completion", vision)
    result = images._run_image_vision(contents, str(tmp_path))
    assert result[id(contents[0])] == UNRECOGNIZED_IMAGE_TEXT
    assert result[id(contents[1])] == "Visible value: 42"


def test_strict_ocr_marks_empty_output_without_removing_literal_text(monkeypatch, tmp_path):
    path = tmp_path / "image.jpg"
    path.write_bytes(b"image")
    item = {"type": "image", "img_path": path.name, "page_idx": 0}
    monkeypatch.setattr(images, "vision_completion", lambda *args: "")
    result = images._run_image_vision([item], str(tmp_path), strict_ocr_only=True)
    assert result[id(item)] == UNRECOGNIZED_IMAGE_TEXT
