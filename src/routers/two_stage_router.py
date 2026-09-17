"""Two-stage MinerU+vision API router (不改现有路由，独立挂载）。"""

import json
import os
import shutil
import uuid
from enum import Enum
from pathlib import Path
from typing import Optional

from celery import states
from celery.result import AsyncResult
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile

from src.config.config import MINERU_TASK_STORAGE_DIR
from src.models.models import ResponseWithPageNum, TextElementWithPageNum
from src.services.two_stage_pipeline import (
    celery_app,
    resolve_two_stage_queues,
    submit_two_stage_job,
)
from src.services.vision_service import (
    AVAILABLE_MODEL_VALUES,
    AVAILABLE_PROVIDER_VALUES,
    VisionModel,
    VisionProvider,
)
from src.utils.file_conversion import (
    CONVERTIBLE_OFFICE_EXTENSIONS,
    format_extension_list,
    maybe_convert_to_pdf,
)
from src.utils.mineru_backend import MinerUTier
from src.utils.mineru_support import mineru_supported_extensions

router = APIRouter()

SUPPORTED_EXTENSIONS = mineru_supported_extensions()
ACCEPTED_EXTENSIONS = SUPPORTED_EXTENSIONS | CONVERTIBLE_OFFICE_EXTENSIONS
ACCEPTED_EXTENSIONS_STR = format_extension_list(ACCEPTED_EXTENSIONS)


class TaskPriority(str, Enum):
    NORMAL = "normal"
    URGENT = "urgent"


def _two_stage_queue_names() -> list[str]:
    normal_queues = resolve_two_stage_queues(TaskPriority.NORMAL.value)
    urgent_queues = resolve_two_stage_queues(TaskPriority.URGENT.value)
    ordered_names = dict.fromkeys([*normal_queues.values(), *urgent_queues.values()])
    return [name for name in ordered_names if name]


def _extract_unacked_queue_name(raw_entry: bytes | str) -> Optional[str]:
    if isinstance(raw_entry, bytes):
        raw_entry = raw_entry.decode("utf-8")

    try:
        payload = json.loads(raw_entry)
    except (TypeError, ValueError):
        return None

    if not isinstance(payload, list) or len(payload) < 3:
        return None

    queue_name = payload[2]
    if isinstance(queue_name, str) and queue_name:
        return queue_name
    return None


def _normalize_filename(filename: str, fallback_ext: str) -> str:
    candidate = os.path.basename(filename or "")
    if candidate:
        return candidate
    return f"upload{fallback_ext}"


def _ensure_workspace() -> Path:
    root = Path(MINERU_TASK_STORAGE_DIR)
    root.mkdir(parents=True, exist_ok=True)
    workspace = root / uuid.uuid4().hex
    workspace.mkdir(parents=True, exist_ok=False)
    return workspace


def _form_provider(
    provider: Optional[str] = Form(
        None,
        description="Vision provider: one of the configured providers.",
        json_schema_extra={"enum": AVAILABLE_PROVIDER_VALUES},
    )
) -> Optional[VisionProvider]:
    if provider is None or provider.strip() == "":
        return None
    try:
        return VisionProvider(provider.strip())
    except ValueError:
        allowed = ", ".join(p.value for p in VisionProvider)
        raise HTTPException(
            status_code=422, detail=f"Invalid provider '{provider}'. Allowed: {allowed}."
        )


def _form_model(
    model: Optional[str] = Form(
        None,
        description="Vision model identifier to use.",
        json_schema_extra={"enum": AVAILABLE_MODEL_VALUES},
    )
) -> Optional[VisionModel]:
    if model is None or model.strip() == "":
        return None
    try:
        return VisionModel(model.strip())
    except ValueError:
        allowed = ", ".join(m.value for m in VisionModel)
        raise HTTPException(status_code=422, detail=f"Invalid model '{model}'. Allowed: {allowed}.")


@router.post(
    "/two_stage/task",
    summary="Queue two-stage MinerU+vision job",
    description=f"Supported file types: {ACCEPTED_EXTENSIONS_STR}.",
)
async def two_stage_task(
    file: UploadFile = File(...),
    tier: MinerUTier = Form(
        MinerUTier.ADVANCED,
        description="MinerU parsing quality: flash, basic, standard, or advanced (default).",
    ),
    chunk_type: bool = Form(False),
    return_txt: bool = Form(False),
    priority: TaskPriority = Form(
        TaskPriority.NORMAL,
        description=(
            'Queue priority: default as normal, "urgent" routes to urgent two-stage queues.'
        ),
    ),
    provider: Optional[VisionProvider] = Depends(_form_provider),
    model: Optional[VisionModel] = Depends(_form_model),
    prompt: Optional[str] = Form(None),
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

    workspace = _ensure_workspace()
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

    processing_path = str(target_path)
    extra_cleanup: set[str] = set()

    if file_ext in CONVERTIBLE_OFFICE_EXTENSIONS:
        try:
            processing_path, cleanup_paths = maybe_convert_to_pdf(str(target_path), file_ext)
            extra_cleanup.update(cleanup_paths)
        except Exception as exc:
            shutil.rmtree(workspace, ignore_errors=True)
            raise HTTPException(status_code=500, detail=f"Office conversion failed: {exc}") from exc

    queue_names = resolve_two_stage_queues(priority)
    try:
        async_result = submit_two_stage_job(
            processing_path,
            backend=backend_value,
            chunk_type=chunk_type,
            return_txt=return_txt,
            provider=provider,
            model=model,
            prompt=prompt,
            workspace=str(workspace),
            cleanup_source=False,
            extra_cleanup=list(extra_cleanup),
            parse_queue=queue_names["parse"],
            vision_queue=queue_names["vision"],
            dispatch_queue=queue_names["dispatch"],
            merge_queue=queue_names["merge"],
        )
    except Exception as exc:
        shutil.rmtree(workspace, ignore_errors=True)
        raise HTTPException(
            status_code=503, detail=f"Failed to enqueue two-stage task: {exc}"
        ) from exc

    return {"task_id": async_result.id, "state": async_result.state}


@router.get(
    "/two_stage/task/{task_id}",
    summary="Fetch two-stage MinerU+vision task status/result",
)
def two_stage_task_status(task_id: str):
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
        response = ResponseWithPageNum(result=items, txt=payload.get("txt"), minio_assets=None)
        return {"task_id": task_id, "state": state, "result": response}

    if state in {states.FAILURE, states.REVOKED}:
        error_detail = str(async_result.info) if async_result.info else state
        return {"task_id": task_id, "state": state, "error": error_detail}

    return {"task_id": task_id, "state": state}


@router.get(
    "/two_stage/queue_status",
    summary="Fetch two-stage Celery queue backlog",
)
def two_stage_queue_status():
    broker_url = celery_app.conf.broker_url
    if isinstance(broker_url, (list, tuple)):
        broker_url = broker_url[0] if broker_url else ""
    broker_url = str(broker_url or "").strip()

    if not broker_url.startswith(("redis://", "rediss://")):
        raise HTTPException(
            status_code=503,
            detail="Queue status is only available when Celery uses a Redis broker.",
        )

    try:
        import redis
    except ImportError as exc:
        raise HTTPException(
            status_code=503,
            detail=f"Redis client is unavailable for queue status: {exc}",
        ) from exc

    queue_names = _two_stage_queue_names()
    try:
        redis_client = redis.Redis.from_url(broker_url)
        queue_lengths = {
            queue_name: int(redis_client.llen(queue_name)) for queue_name in queue_names
        }
        unacked_counts = {queue_name: 0 for queue_name in queue_names}

        for raw_entry in redis_client.hvals("unacked"):
            queue_name = _extract_unacked_queue_name(raw_entry)
            if queue_name in unacked_counts:
                unacked_counts[queue_name] += 1
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail=f"Failed to query Redis queue status: {exc}",
        ) from exc

    return {"broker": "redis", "queues": queue_lengths, "unacked": unacked_counts}
