import os
from typing import Optional

from starlette.concurrency import run_in_threadpool

from src.utils.upload_io import persist_upload, await_parse_future, cleanup_after_parse

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile

from src.models.models import MinioAssetSummary, ResponseWithPageNum, TextElementWithPageNum
from src.routers.mineru_minio_utils import (
    MinioContext,
    build_minio_prefix,
    initialize_minio_context,
    upload_meta_text,
    upload_pdf_assets,
)
from src.services.gpu_scheduler import scheduler
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


@router.post(
    "/mineru",
    summary="Parse document with MinerU and return page-numbered chunks",
    response_model=ResponseWithPageNum,
    response_description="List of text chunks with page numbers",
    description=(
        f"Supported file types: {ACCEPTED_EXTENSIONS_STR}.\n"
        f"Office formats ({OFFICE_EXTENSIONS_STR}) auto-convert to PDF before parsing."
    ),
)
async def mineru(
    file: UploadFile = File(...),
    tier: MinerUTier = Form(
        MinerUTier.ADVANCED,
        description="MinerU parsing quality: flash, basic, standard, or advanced (default).",
    ),
    save_to_minio: bool = Form(
        False,
        description="Store the parsed PDF, JSON payload, and per-page images in MinIO.",
    ),
    minio_address: Optional[str] = Form(
        None, description="MinIO server address, e.g. https://minio.local:9000"
    ),
    minio_access_key: Optional[str] = Form(None, description="MinIO access key"),
    minio_secret_key: Optional[str] = Form(None, description="MinIO secret key"),
    minio_bucket: Optional[str] = Form(None, description="Target MinIO bucket name"),
    minio_prefix: Optional[str] = Form(
        None,
        description="Optional custom prefix for stored assets; defaults to mineru/<filename>.",
    ),
    minio_meta: Optional[str] = Form(
        None,
        description="Optional string stored as meta.txt next to source.pdf when save_to_minio=true.",
    ),
    pretty: bool = Depends(pretty_response_flag),
    chunk_type: bool = False,
    return_txt: bool = False,
):
    f"""
    Use MinerU to parse a document and return text chunks with page numbers.

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

    if not save_to_minio:
        # Ignore meta payloads when MinIO persistence is disabled.
        minio_meta = None

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
        minio_context: MinioContext = None
        minio_prefix_value: Optional[str] = None
        if save_to_minio:
            if not processing_path.lower().endswith(".pdf"):
                raise HTTPException(
                    status_code=400,
                    detail="MinIO storage requires a PDF input after preprocessing.",
                )
            minio_context = await run_in_threadpool(
                initialize_minio_context,
                save_to_minio,
                minio_address,
                minio_access_key,
                minio_secret_key,
                minio_bucket,
            )
            minio_prefix_value = build_minio_prefix(filename, minio_prefix)

        # Dispatch to GPU scheduler; this returns a Future
        fut = scheduler.submit(
            processing_path,
            chunk_type=chunk_type,
            return_txt=return_txt,
            backend=backend_value,
        )
        payload = await _await_future(fut)
        # Map back into Pydantic model
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
        txt_text = payload.get("txt")
        if return_txt:
            txt_text = build_plain_text(items)
        minio_assets_summary: Optional[MinioAssetSummary] = None
        if minio_context:
            assert minio_prefix_value is not None  # for mypy
            chunks_with_pages = [
                (item.text, item.page_number, item.type)
                for item in items
                if item.text and item.text.strip()
            ]
            minio_assets_summary = await run_in_threadpool(
                upload_pdf_assets,
                minio_context,
                minio_prefix_value,
                processing_path,
                chunks_with_pages,
            )
            if minio_meta is not None:
                meta_object = await run_in_threadpool(
                    upload_meta_text,
                    minio_context,
                    minio_prefix_value,
                    minio_meta,
                )
                minio_assets_summary.meta_object = meta_object

        response_model = ResponseWithPageNum(
            result=items,
            txt=txt_text if return_txt else None,
            minio_assets=minio_assets_summary,
        )
        return await run_in_threadpool(json_response, response_model, pretty)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        cleanup_after_parse(cleanup_paths, fut)


_await_future = await_parse_future
