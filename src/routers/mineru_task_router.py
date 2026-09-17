import os
import shutil
import uuid
from pathlib import Path
from typing import Optional

from celery import states
from celery.result import AsyncResult
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile

from src.config.config import MINERU_TASK_STORAGE_DIR
from src.models.models import (
    MineruTaskStatusResponse,
    MineruTaskSubmitResponse,
    MinioAssetSummary,
    ResponseWithPageNum,
    TextElementWithPageNum,
)
from src.services.celery_app import celery_app
from src.services.tasks.mineru_tasks import run_mineru_task
from src.utils.file_conversion import (
    CONVERTIBLE_OFFICE_EXTENSIONS,
    format_extension_list,
)
from src.utils.mineru_backend import MinerUTier
from src.utils.mineru_support import mineru_supported_extensions
from src.utils.response_utils import json_response, pretty_response_flag
from src.config.config import CELERY_TASK_MINERU_QUEUE, CELERY_TASK_URGENT_QUEUE

router = APIRouter()

SUPPORTED_EXTENSIONS = mineru_supported_extensions()
ACCEPTED_EXTENSIONS = SUPPORTED_EXTENSIONS | CONVERTIBLE_OFFICE_EXTENSIONS
ACCEPTED_EXTENSIONS_STR = format_extension_list(ACCEPTED_EXTENSIONS)


def _normalize_filename(filename: str, fallback_ext: str) -> str:
    candidate = os.path.basename(filename or "")
    if candidate:
        return candidate
    return f"upload{fallback_ext}"


def _ensure_storage_root() -> Path:
    storage_root = Path(MINERU_TASK_STORAGE_DIR)
    storage_root.mkdir(parents=True, exist_ok=True)
    return storage_root


@router.post(
    "/mineru/task",
    summary="Queue MinerU parse job via Celery",
    response_model=MineruTaskSubmitResponse,
    description=(
        f"Supported file types: {ACCEPTED_EXTENSIONS_STR}.\n"
        "Uploads are persisted to a local workspace and executed by Celery workers "
        "talking to Redis/Flower."
    ),
)
async def mineru_task(
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
    priority: str = Form(
        "normal",
        description='Queue priority: "urgent" routes to queue_urgent, anything else goes to queue_normal.',
    ),
):
    filename = file.filename or ""
    _, file_ext = os.path.splitext(filename)
    file_ext = file_ext.lower()

    if not file_ext:
        raise HTTPException(
            status_code=400,
            detail="Uploaded file is missing an extension; MinerU requires a supported file type.",
        )
    if file_ext not in ACCEPTED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type. Allowed types: {ACCEPTED_EXTENSIONS_STR}",
        )
    backend_value = tier.value

    storage_root = _ensure_storage_root()
    workspace = storage_root / uuid.uuid4().hex
    workspace.mkdir(parents=True, exist_ok=False)
    target_filename = _normalize_filename(filename, file_ext)
    target_path = workspace / target_filename

    file_bytes = await file.read()
    try:
        target_path.write_bytes(file_bytes)
    except Exception:
        shutil.rmtree(workspace, ignore_errors=True)
        raise HTTPException(
            status_code=500, detail="Failed to persist uploaded file for Celery job."
        )

    queue_name = (
        CELERY_TASK_URGENT_QUEUE if priority.lower() == "urgent" else CELERY_TASK_MINERU_QUEUE
    )

    try:
        async_result = run_mineru_task.apply_async(
            args=[
                {
                    "source_path": str(target_path),
                    "workspace": str(workspace),
                    "original_filename": filename,
                    "chunk_type": chunk_type,
                    "return_txt": return_txt,
                    "save_to_minio": save_to_minio,
                    "minio_address": minio_address,
                    "minio_access_key": minio_access_key,
                    "minio_secret_key": minio_secret_key,
                    "minio_bucket": minio_bucket,
                    "minio_prefix": minio_prefix,
                    "minio_meta": minio_meta if save_to_minio else None,
                    "backend_value": backend_value,
                }
            ],
            queue=queue_name,
        )
    except Exception as exc:
        # Clean up on enqueue failure
        shutil.rmtree(workspace, ignore_errors=True)
        raise HTTPException(
            status_code=503, detail=f"Failed to enqueue MinerU task: {exc}"
        ) from exc

    response_model = MineruTaskSubmitResponse(task_id=async_result.id, state=async_result.state)
    return json_response(response_model, pretty)


@router.get(
    "/mineru/task/{task_id}",
    summary="Fetch Celery task status/result for MinerU",
    response_model=MineruTaskStatusResponse,
)
def mineru_task_status(task_id: str, pretty: bool = Depends(pretty_response_flag)):
    try:
        async_result = AsyncResult(task_id, app=celery_app)
        state = async_result.state
    except Exception as exc:
        raise HTTPException(
            status_code=503, detail=f"Failed to query Celery backend: {exc}"
        ) from exc

    if state == states.SUCCESS:
        payload = async_result.result or {}
        items = [TextElementWithPageNum(**chunk) for chunk in payload.get("result", [])]
        minio_assets_payload = payload.get("minio_assets")
        minio_assets = MinioAssetSummary(**minio_assets_payload) if minio_assets_payload else None
        response = MineruTaskStatusResponse(
            task_id=task_id,
            state=state,
            result=ResponseWithPageNum(
                result=items,
                txt=payload.get("txt"),
                minio_assets=minio_assets,
            ),
        )
        return json_response(response, pretty)

    if state in {states.FAILURE, states.REVOKED}:
        error_detail = str(async_result.info) if async_result.info else state
        response = MineruTaskStatusResponse(task_id=task_id, state=state, error=error_detail)
        return json_response(response, pretty, status_code=500)

    response = MineruTaskStatusResponse(task_id=task_id, state=state)
    return json_response(response, pretty)
