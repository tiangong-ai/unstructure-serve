import os
from typing import Optional

from starlette.concurrency import run_in_threadpool

from src.utils.upload_io import persist_upload, await_parse_future, cleanup_after_parse

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile

from src.models.models import ResponseWithPageNum, TextElementWithPageNum
from src.services.gpu_scheduler import scheduler
from src.services.vision_service import AVAILABLE_MODEL_VALUES, AVAILABLE_PROVIDER_VALUES
from src.utils.file_conversion import (
    CONVERTIBLE_OFFICE_EXTENSIONS,
    format_extension_list,
    maybe_convert_to_pdf,
)
from src.utils.mineru_backend import MinerUTier
from src.utils.mineru_support import (
    mineru_supported_extensions,
)
from src.utils.response_utils import json_response, pretty_response_flag
from src.utils.text_output import build_plain_text

router = APIRouter()

SUPPORTED_EXTENSIONS = mineru_supported_extensions()
OFFICE_EXTENSIONS_STR = format_extension_list(CONVERTIBLE_OFFICE_EXTENSIONS)
ACCEPTED_EXTENSIONS = SUPPORTED_EXTENSIONS | CONVERTIBLE_OFFICE_EXTENSIONS
ACCEPTED_EXTENSIONS_STR = format_extension_list(ACCEPTED_EXTENSIONS)


def _form_provider(
    provider: Optional[str] = Form(
        None,
        description="Vision model provider to use.",
        json_schema_extra={"enum": AVAILABLE_PROVIDER_VALUES},
    )
) -> Optional[str]:
    if provider is None:
        return None
    stripped = provider.strip()
    return stripped or None


def _form_model(
    model: Optional[str] = Form(
        None,
        description="Vision model identifier to use.",
        json_schema_extra={"enum": AVAILABLE_MODEL_VALUES},
    )
) -> Optional[str]:
    if model is None:
        return None
    stripped = model.strip()
    return stripped or None


@router.post(
    "/mineru_with_images",
    summary="Parse with MinerU (image-aware) and return page-numbered chunks",
    response_model=ResponseWithPageNum,
    response_description="List of text chunks with page numbers",
    description=(
        f"Supported file types: {ACCEPTED_EXTENSIONS_STR}.\n"
        f"Office formats ({OFFICE_EXTENSIONS_STR}) auto-convert to PDF before parsing."
    ),
)
async def mineru_with_images(
    file: UploadFile = File(...),
    tier: MinerUTier = Form(
        MinerUTier.ADVANCED,
        description="MinerU parsing quality: flash, basic, standard, or advanced (default).",
    ),
    provider: Optional[str] = Depends(_form_provider),
    model: Optional[str] = Depends(_form_model),
    prompt: Optional[str] = Form(
        None,
        description="Optional instruction prompt override passed to the vision model.",
    ),
    pretty: bool = Depends(pretty_response_flag),
    chunk_type: bool = False,
    return_txt: bool = False,
):
    f"""
    Use MinerU with image-aware extraction (figures/tables) and return text chunks with page numbers.

    Accepted: {ACCEPTED_EXTENSIONS_STR}
    Office formats ({OFFICE_EXTENSIONS_STR}) auto-convert to PDF before parsing.
    Output: [(text, page_number), ...]
    """
    filename = file.filename or ""
    _, file_ext = os.path.splitext(filename)
    file_ext = file_ext.lower()

    if not file_ext:
        raise HTTPException(
            status_code=400,
            detail="Uploaded file is missing an extension; MinerU requires a supported file type.",
        )

    # Check if file extension is allowed
    if file_ext not in ACCEPTED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type. Allowed types: {ACCEPTED_EXTENSIONS_STR}",
        )

    backend_value = tier.value

    tmp_path = await run_in_threadpool(persist_upload, file, suffix=file_ext)

    conversion_cleanup: list[str] = []
    processing_path = tmp_path

    if file_ext in CONVERTIBLE_OFFICE_EXTENSIONS:
        try:
            processing_path, conversion_cleanup = await run_in_threadpool(
                maybe_convert_to_pdf, tmp_path, file_ext
            )
        except RuntimeError as exc:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    cleanup_paths = {tmp_path, *conversion_cleanup}
    fut = None

    try:
        # Dispatch to GPU scheduler; this returns a Future
        scheduler_options: dict[str, object] = {
            "chunk_type": chunk_type,
            "vision_prompt": prompt,
            "vision_provider": provider,
            "vision_model": model,
            "return_txt": return_txt,
            "backend": backend_value,
        }
        if file_ext == ".docx" and return_txt:
            scheduler_options["txt_from_native_docx"] = True
            scheduler_options["txt_source_path"] = tmp_path

        fut = scheduler.submit(
            processing_path,
            pipeline="images",
            **scheduler_options,
        )
        payload = await _await_future(fut)
        result_payload = payload.get("result")
        if not isinstance(result_payload, list):
            raise HTTPException(
                status_code=500,
                detail="Invalid scheduler response payload for MinerU with images.",
            )

        ordered_chunks: list[dict] = []
        for it in result_payload:
            try:
                text = it["text"]
                page_number = int(it["page_number"])
            except (KeyError, TypeError, ValueError) as exc:
                raise HTTPException(
                    status_code=500,
                    detail=f"Malformed item returned by MinerU scheduler: {exc}",
                )
            item_type = it.get("type")
            if not chunk_type and item_type in {"header", "footer", "page_number"}:
                continue
            if chunk_type and item_type == "page_number":
                continue
            ordered_chunks.append(
                {
                    "text": text,
                    "page_number": page_number,
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
        txt_text = payload.get("txt")
        if return_txt:
            if file_ext != ".docx" or txt_text is None:
                txt_text = build_plain_text(items)
        response_model = ResponseWithPageNum(
            result=items,
            txt=txt_text if return_txt else None,
        )
        return await run_in_threadpool(json_response, response_model, pretty)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        cleanup_after_parse(cleanup_paths, fut)


_await_future = await_parse_future
