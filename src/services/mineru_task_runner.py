import os
from typing import Optional

from loguru import logger

from src.models.models import TextElementWithPageNum
from src.services.gpu_scheduler import scheduler
from src.utils.file_conversion import (
    CONVERTIBLE_OFFICE_EXTENSIONS,
    format_extension_list,
    maybe_convert_to_pdf,
)
from src.utils.mineru_backend import resolve_backend_from_env
from src.utils.mineru_support import mineru_supported_extensions
from src.utils.text_output import build_plain_text

SUPPORTED_EXTENSIONS = mineru_supported_extensions()
ACCEPTED_EXTENSIONS = SUPPORTED_EXTENSIONS | CONVERTIBLE_OFFICE_EXTENSIONS


class MineruTaskError(Exception):
    """Custom exception to surface predictable task failures."""


def _normalize_filename(filename: str) -> str:
    candidate = os.path.basename(filename or "")
    return candidate or "upload"


def _validate_extension(file_ext: str) -> None:
    if not file_ext:
        raise MineruTaskError(
            "Uploaded file is missing an extension; MinerU requires a supported file type."
        )
    if file_ext not in ACCEPTED_EXTENSIONS:
        raise MineruTaskError(
            f"Unsupported file type. Allowed types: {format_extension_list(ACCEPTED_EXTENSIONS)}"
        )


def _parse_with_scheduler(
    processing_path: str,
    chunk_type: bool,
    return_txt: bool,
    backend_value: str,
    *,
    pipeline: str = "default",
    **scheduler_options: object,
) -> tuple[list[TextElementWithPageNum], Optional[str]]:
    options = {k: v for k, v in scheduler_options.items() if v is not None}
    payload = scheduler.submit(
        processing_path,
        pipeline=pipeline,
        chunk_type=chunk_type,
        return_txt=return_txt,
        backend=backend_value,
        **options,
    ).result()

    ordered_chunks: list[dict] = []
    for it in payload.get("result", []):
        item_type = it.get("type")
        if not chunk_type and item_type in {"header", "footer", "page_number"}:
            continue
        if chunk_type and item_type == "page_number":
            continue
        ordered_chunks.append(
            {
                "text": it["text"],
                "page_number": int(it["page_number"]),
                "type": item_type,
            }
        )
    items = [
        TextElementWithPageNum(
            text=chunk["text"],
            page_number=chunk["page_number"],
            type=chunk["type"] if chunk_type else None,
        )
        for chunk in ordered_chunks
    ]
    txt_text: Optional[str] = payload.get("txt")
    if return_txt:
        txt_text = build_plain_text(items)
    return items, txt_text


def run_mineru_local_job(
    *,
    source_path: str,
    original_filename: str,
    chunk_type: bool,
    return_txt: bool,
    backend_value: Optional[str] = None,
    pipeline: str = "default",
    vision_provider: Optional[str] = None,
    vision_model: Optional[str] = None,
    vision_prompt: Optional[str] = None,
) -> dict:
    """Execute MinerU parsing against a local file path.

    Returns a JSON-serializable dict aligned with ResponseWithPageNum.
    """
    filename = _normalize_filename(original_filename)
    _, file_ext = os.path.splitext(filename)
    file_ext = file_ext.lower()

    _validate_extension(file_ext)

    backend = backend_value or resolve_backend_from_env()
    cleanup_paths: set[str] = {source_path}
    processing_path = source_path
    scheduler_options: dict[str, object] = {}

    if pipeline == "images":
        if vision_provider:
            scheduler_options["vision_provider"] = vision_provider
        if vision_model:
            scheduler_options["vision_model"] = vision_model
        if vision_prompt:
            scheduler_options["vision_prompt"] = vision_prompt

    try:
        if file_ext in CONVERTIBLE_OFFICE_EXTENSIONS:
            processing_path, conversion_cleanup = maybe_convert_to_pdf(source_path, file_ext)
            cleanup_paths.update(conversion_cleanup)

        items, txt_text = _parse_with_scheduler(
            processing_path,
            chunk_type,
            return_txt,
            backend,
            pipeline=pipeline,
            **scheduler_options,
        )
        return {
            "result": [item.model_dump() for item in items],
            "txt": txt_text if return_txt else None,
        }
    except MineruTaskError:
        raise
    except Exception:
        logger.exception("MinerU task failed for %s", filename)
        raise
    finally:
        for path in cleanup_paths:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
            except Exception:
                logger.debug("Failed to clean temp path %s", path)
