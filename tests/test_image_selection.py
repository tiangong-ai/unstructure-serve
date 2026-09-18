from PIL import Image

from src.services import two_stage_pipeline as pipeline
from src.utils.text_output import sanitize_vision_text


def test_sixth_qualifying_figure_is_not_silently_dropped(tmp_path):
    Image.new("RGB", (300, 300), "white").save(tmp_path / "figure.png")
    contents = [
        dict(type="image", img_path="figure.png", img_caption=[f"Experiment {i}"], page_idx=0)
        for i in range(8)
    ]
    jobs, annotated = pipeline._build_image_jobs(contents, str(tmp_path))
    assert len(jobs) == 8
    assert all(item.get("__image_seq") for item in annotated)


def test_generated_sdk_caption_does_not_qualify_decorative_image(tmp_path):
    Image.new("RGB", (20, 20), "white").save(tmp_path / "icon.png")
    item = dict(
        type="image",
        img_path="icon.png",
        img_caption=["A generic company emblem"],
        content="A generic company emblem",
        image_caption=[],
        page_idx=0,
    )
    jobs, _ = pipeline._build_image_jobs([item], str(tmp_path))
    assert not jobs


def test_printed_caption_keeps_compressible_diagram(tmp_path):
    Image.new("RGB", (300, 300), "white").save(tmp_path / "diagram.png")
    assert (tmp_path / "diagram.png").stat().st_size < 2048
    jobs, _ = pipeline._build_image_jobs(
        [
            dict(
                type="image",
                img_path="diagram.png",
                image_caption=["Figure: process flow"],
                page_idx=0,
            )
        ],
        str(tmp_path),
    )
    assert len(jobs) == 1


def test_strict_ocr_preserves_printed_metadata_syntax():
    text = "Image Description: [Page 4]\n[ChunkType=Title]\n52%"
    assert sanitize_vision_text(text, strip_boilerplate=False) == text
