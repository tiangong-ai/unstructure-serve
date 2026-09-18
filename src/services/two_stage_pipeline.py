"""
Two-stage MinerU+vision Celery pipeline (解析队列 + 视觉队列 + 汇总).

- 解析任务只占用 GPU 队列，产出 content_list + 图片元数据，不做视觉调用。
- 视觉任务在独立队列并发调用 vision_completion。
- merge 任务按 seq 回填视觉结果并清理临时目录。

环境变量：
  CELERY_TASK_PARSE_QUEUE（默认使用 CELERY_TASK_MINERU_QUEUE 或 queue_parse_gpu）
  CELERY_TASK_PARSE_URGENT_QUEUE（默认 queue_parse_urgent）
  CELERY_TASK_VISION_QUEUE（默认 queue_vision）
  CELERY_TASK_VISION_URGENT_QUEUE（默认 queue_vision_urgent）
  CELERY_TASK_DISPATCH_QUEUE（默认 CELERY_TASK_DEFAULT_QUEUE 或 default）
  CELERY_TASK_DISPATCH_URGENT_QUEUE（默认 queue_dispatch_urgent）
  CELERY_TASK_MERGE_QUEUE（默认 CELERY_TASK_DEFAULT_QUEUE 或 default）
  CELERY_TASK_MERGE_URGENT_QUEUE（默认 queue_merge_urgent）
  MINERU_TASK_STORAGE_DIR（默认系统临时目录内的 tiangong_mineru_tasks）
"""

import hashlib
import os
import shutil
import uuid
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Union

from celery import Celery, chord, chain
from loguru import logger
from PIL import Image

from src.config.config import (
    CELERY_BROKER_URL,
    CELERY_RESULT_BACKEND,
    CELERY_RESULT_EXPIRES,
    CELERY_TASK_DEFAULT_QUEUE,
    CELERY_TASK_MINERU_QUEUE,
    MINERU_TASK_STORAGE_DIR,
)
from src.models.models import TextElementWithPageNum
from src.services.celery_runtime import runtime_options
from src.services.mineru_service_full import parse_doc
from src.services.mineru_with_images_service import (
    _build_context_blocks,
    _build_vision_prompt,
    _reindex_blocks,
    _resolve_context_windows,
    clean_text,
    image_text,
    list_text,
    table_text,
)
from src.services.vision_service import VisionModel, VisionProvider, vision_completion
from src.services.vision_prompts import vision_request_key
from src.utils.text_output import build_plain_text, sanitize_vision_text

MIN_IMAGE_AREA_RATIO = 0.01
MIN_IMAGE_AREA_RATIO_WITH_CAPTION = 0.005
MAX_IMAGE_ASPECT_RATIO = 10.0
MIN_IMAGE_BYTES = 10 * 1024
MIN_IMAGE_BYTES_WITH_CAPTION = 2 * 1024
MIN_IMAGE_MIN_DIM = 96
MIN_IMAGE_PIXEL_AREA = MIN_IMAGE_MIN_DIM * MIN_IMAGE_MIN_DIM
PER_PAGE_IMAGE_LIMIT = 5


def _queue_env(name: str, default: str) -> str:
    value = os.getenv(name)
    if value is None:
        return default
    stripped = value.strip()
    return stripped or default


PARSE_QUEUE = _queue_env("CELERY_TASK_PARSE_QUEUE", CELERY_TASK_MINERU_QUEUE or "queue_parse_gpu")
VISION_QUEUE = _queue_env("CELERY_TASK_VISION_QUEUE", "queue_vision")
DISPATCH_QUEUE = _queue_env("CELERY_TASK_DISPATCH_QUEUE", CELERY_TASK_DEFAULT_QUEUE or "default")
MERGE_QUEUE = _queue_env("CELERY_TASK_MERGE_QUEUE", CELERY_TASK_DEFAULT_QUEUE or "default")
PARSE_URGENT_QUEUE = _queue_env("CELERY_TASK_PARSE_URGENT_QUEUE", "queue_parse_urgent")
VISION_URGENT_QUEUE = _queue_env("CELERY_TASK_VISION_URGENT_QUEUE", "queue_vision_urgent")
DISPATCH_URGENT_QUEUE = _queue_env("CELERY_TASK_DISPATCH_URGENT_QUEUE", "queue_dispatch_urgent")
MERGE_URGENT_QUEUE = _queue_env("CELERY_TASK_MERGE_URGENT_QUEUE", "queue_merge_urgent")


def resolve_two_stage_queues(priority: Optional[str]) -> Dict[str, str]:
    if priority == "urgent":
        return {
            "parse": PARSE_URGENT_QUEUE,
            "vision": VISION_URGENT_QUEUE,
            "dispatch": DISPATCH_URGENT_QUEUE,
            "merge": MERGE_URGENT_QUEUE,
        }
    return {
        "parse": PARSE_QUEUE,
        "vision": VISION_QUEUE,
        "dispatch": DISPATCH_QUEUE,
        "merge": MERGE_QUEUE,
    }


celery_app = Celery(
    "mineru_two_stage",
    broker=CELERY_BROKER_URL,
    backend=CELERY_RESULT_BACKEND,
)

celery_conf = {
    "task_serializer": "json",
    "result_serializer": "json",
    "accept_content": ["json"],
    "task_default_queue": CELERY_TASK_DEFAULT_QUEUE or "default",
    "task_track_started": True,
    "task_routes": {
        "two_stage.parse": {"queue": PARSE_QUEUE},
        "two_stage.vision": {"queue": VISION_QUEUE},
        "two_stage.merge": {"queue": MERGE_QUEUE},
        "two_stage.dispatch": {"queue": DISPATCH_QUEUE},
    },
}
celery_conf.update(runtime_options(CELERY_BROKER_URL, CELERY_RESULT_EXPIRES))
celery_app.conf.update(**celery_conf)


def _ensure_workspace(existing: Optional[str] = None) -> Path:
    root = Path(MINERU_TASK_STORAGE_DIR)
    root.mkdir(parents=True, exist_ok=True)
    if existing:
        workspace = Path(existing)
        workspace.mkdir(parents=True, exist_ok=True)
        return workspace
    workspace = root / uuid.uuid4().hex
    workspace.mkdir(parents=True, exist_ok=False)
    return workspace


def _build_image_jobs(
    content_list: List[Dict], output_dir: str, *, keep_positions: bool = False
) -> tuple[List[Dict], List[Dict]]:
    """Prepare image jobs with context and stable seq, and annotate content_list with seq."""
    context_blocks = _build_context_blocks(content_list)
    idx_map = _reindex_blocks(context_blocks)
    image_jobs: List[Dict] = []
    seq = 1
    per_page_counts: Dict[int, int] = defaultdict(int)
    seen_requests: Dict[str, int] = {}

    def _extract_bbox(item: Dict) -> Optional[tuple[float, float, float, float]]:
        bbox = item.get("bbox")
        if not (isinstance(bbox, (list, tuple)) and len(bbox) == 4):
            return None
        try:
            x0, y0, x1, y1 = (float(v) for v in bbox)
        except Exception:
            return None
        return x0, y0, x1, y1

    def _page_size(item: Dict) -> Optional[tuple[float, float]]:
        page_size = item.get("page_size")
        if isinstance(page_size, (list, tuple)) and len(page_size) == 2:
            try:
                w, h = (float(v) for v in page_size)
                if w > 0 and h > 0:
                    return w, h
            except Exception:
                pass
        w = item.get("page_width") or item.get("page_w")
        h = item.get("page_height") or item.get("page_h")
        try:
            if w and h and float(w) > 0 and float(h) > 0:
                return float(w), float(h)
        except Exception:
            return None
        return None

    def _image_area_ratio(item: Dict) -> Optional[float]:
        bbox = _extract_bbox(item)
        page = _page_size(item)
        if not bbox or not page:
            return None
        x0, y0, x1, y1 = bbox
        pw, ph = page
        area = max(x1 - x0, 0) * max(y1 - y0, 0)
        page_area = pw * ph
        if page_area <= 0:
            return None
        return area / page_area

    def _aspect_ratio(item: Dict) -> Optional[float]:
        if item.get("bbox_normalized"):
            # A 0-1000 box distorts aspect ratio on non-square pages. The
            # intrinsic image dimensions are checked below in the same filter.
            return None
        bbox = _extract_bbox(item)
        if not bbox:
            return None
        x0, y0, x1, y1 = bbox
        width = max(x1 - x0, 0)
        height = max(y1 - y0, 0)
        if width <= 0 or height <= 0:
            return None
        ratio = width / height
        return ratio if ratio >= 1 else 1 / ratio

    def _image_dims(path: str) -> tuple[Optional[int], Optional[int]]:
        try:
            with Image.open(path) as im:
                return im.width, im.height
        except Exception:
            return None, None

    def _file_stats(path: str) -> tuple[int, Optional[str]]:
        try:
            size = os.path.getsize(path)
        except OSError:
            return 0, None
        hash_value: Optional[str] = None
        try:
            with open(path, "rb") as fh:
                hash_value = hashlib.file_digest(fh, "sha256").hexdigest()
        except OSError:
            hash_value = None
        return size, hash_value

    for item in content_list:
        item.pop("__image_seq", None)
        if item.get("type") != "image" or not (item.get("img_path") and item["img_path"].strip()):
            continue

        img_path = os.path.join(output_dir, item["img_path"])
        if not os.path.exists(img_path):
            raise FileNotFoundError(f"Missing image asset: {img_path}")

        page_number = int(item.get("page_idx", 0)) + 1
        has_caption = bool(item.get("img_caption") or item.get("img_footnote"))
        area_ratio = _image_area_ratio(item)
        aspect_ratio = _aspect_ratio(item)
        dim_w, dim_h = _image_dims(img_path)
        file_size, file_hash = _file_stats(img_path)

        min_area_ratio = MIN_IMAGE_AREA_RATIO_WITH_CAPTION if has_caption else MIN_IMAGE_AREA_RATIO
        if area_ratio is not None and area_ratio < min_area_ratio:
            logger.debug(
                "Skip image (small area {:.4f} < {:.4f}) at {} page {}",
                area_ratio,
                min_area_ratio,
                img_path,
                page_number,
            )
            continue

        if aspect_ratio is not None and aspect_ratio > MAX_IMAGE_ASPECT_RATIO:
            logger.debug(
                "Skip image (extreme aspect {:.2f} > {:.2f}) at {} page {}",
                aspect_ratio,
                MAX_IMAGE_ASPECT_RATIO,
                img_path,
                page_number,
            )
            continue

        if dim_w and dim_h:
            dim_aspect = dim_w / dim_h if dim_w >= dim_h else dim_h / dim_w
            if dim_aspect > MAX_IMAGE_ASPECT_RATIO:
                logger.debug(
                    "Skip image (extreme intrinsic aspect {:.2f} > {:.2f}) at {} page {}",
                    dim_aspect,
                    MAX_IMAGE_ASPECT_RATIO,
                    img_path,
                    page_number,
                )
                continue
            if not has_caption:
                min_side = min(dim_w, dim_h)
                if min_side < MIN_IMAGE_MIN_DIM:
                    logger.debug(
                        "Skip image (min side {} < {}) at {} page {}",
                        min_side,
                        MIN_IMAGE_MIN_DIM,
                        img_path,
                        page_number,
                    )
                    continue
                if dim_w * dim_h < MIN_IMAGE_PIXEL_AREA:
                    logger.debug(
                        "Skip image (pixel area {} < {}) at {} page {}",
                        dim_w * dim_h,
                        MIN_IMAGE_PIXEL_AREA,
                        img_path,
                        page_number,
                    )
                    continue

        min_bytes = MIN_IMAGE_BYTES_WITH_CAPTION if has_caption else MIN_IMAGE_BYTES
        if file_size and file_size < min_bytes and not has_caption:
            logger.debug(
                "Skip image (size {}B < {}B) at {} page {}",
                file_size,
                min_bytes,
                img_path,
                page_number,
            )
            continue

        if per_page_counts[page_number] >= PER_PAGE_IMAGE_LIMIT:
            logger.debug(
                "Skip image due to per-page limit {} at page {} ({})",
                PER_PAGE_IMAGE_LIMIT,
                page_number,
                img_path,
            )
            continue

        cur_idx = idx_map.get(id(item))
        contexts = _resolve_context_windows(context_blocks, cur_idx, item)
        context_payload, _ = _build_vision_prompt(item, contexts)

        key = (
            vision_request_key(file_hash, context_payload, keep_positions=keep_positions)
            if file_hash
            else None
        )
        per_page_counts[page_number] += 1
        if key in seen_requests:
            item["__image_seq"] = seen_requests[key]
            continue

        item["__image_seq"] = seq
        image_jobs.append(
            {
                "seq": seq,
                "page_number": int(item.get("page_idx", 0)) + 1,
                "is_title": bool(item.get("text_level") is not None),
                "img_path": img_path,
                "context_payload": context_payload,
                "base_text": image_text(item, include_generated=False),
            }
        )
        seq += 1
        if key:
            seen_requests[key] = item["__image_seq"]

    return image_jobs, content_list


def _merge_content(
    content_list: List[Dict],
    vision_results: Sequence[Dict],
    *,
    chunk_type: bool,
    return_txt: bool,
) -> tuple[List[TextElementWithPageNum], Optional[str]]:
    vision_map = {entry.get("seq"): entry.get("vision_text") for entry in vision_results if entry}
    result_items: List[Dict[str, object]] = []

    for item in content_list:
        page_number = int(item.get("page_idx", 0)) + 1
        is_title = item.get("type") == "text" and item.get("text_level") is not None

        if item.get("type") == "image" and item.get("img_path") and item["img_path"].strip():
            seq = item.get("__image_seq")
            base_text = image_text(item, include_generated=seq is None)
            vision_text = vision_map.get(seq, "")
            if seq is not None and (not isinstance(vision_text, str) or not vision_text.strip()):
                raise RuntimeError(f"Missing or empty vision result for seq={seq}")
            if base_text and vision_text:
                combined_text = f"{base_text}\n{vision_text}"
            elif base_text:
                combined_text = base_text
            else:
                combined_text = vision_text or ""

            if combined_text.strip():
                chunk = {"text": clean_text(combined_text), "page_number": page_number}
                if chunk_type:
                    chunk["type"] = "image"
                result_items.append(chunk)
        elif item.get("type") in ("header", "footer"):
            if not chunk_type:
                continue
            header_txt = clean_text(item.get("text", ""))
            if header_txt.strip():
                result_items.append(
                    {"text": header_txt, "page_number": page_number, "type": item["type"]}
                )
        elif item.get("type") == "list":
            list_txt = list_text(item)
            if list_txt.strip():
                chunk = {"text": list_txt, "page_number": page_number}
                if chunk_type and is_title:
                    chunk["type"] = "title"
                result_items.append(chunk)
        elif item.get("type") in ("text", "equation") and item.get("text", "").strip():
            chunk = {"text": clean_text(item["text"]), "page_number": page_number}
            if chunk_type and is_title:
                chunk["type"] = "title"
            result_items.append(chunk)
        elif item.get("type") == "table" and (
            item.get("table_caption") or item.get("table_body") or item.get("table_footnote")
        ):
            chunk = {"text": table_text(item), "page_number": page_number}
            if chunk_type and is_title:
                chunk["type"] = "title"
            result_items.append(chunk)
        elif (
            item.get("type") == "image"
            and (item.get("img_caption") or item.get("img_footnote"))
            and not (item.get("img_path") and item["img_path"].strip())
        ):
            img_txt = image_text(item)
            if img_txt.strip():
                chunk = {"text": img_txt, "page_number": page_number}
                if chunk_type:
                    chunk["type"] = "image"
                result_items.append(chunk)

    items = [
        TextElementWithPageNum(
            text=chunk["text"],
            page_number=int(chunk["page_number"]),
            type=chunk.get("type") if chunk_type else None,
        )
        for chunk in result_items
    ]
    txt_text = build_plain_text(items) if return_txt else None
    return items, txt_text


def _normalize_prompt(prompt: Optional[str]) -> Optional[str]:
    if prompt is None:
        return None
    normalized = prompt.strip()
    return normalized or None


@celery_app.task(name="two_stage.parse", acks_late=True)
def parse_task(payload: Dict[str, object]) -> Dict[str, object]:
    """Stage 1: MinerU parse only, no vision calls."""
    source_path = Path(payload["source_path"])
    backend = payload.get("backend")
    chunk_type = bool(payload.get("chunk_type"))
    return_txt = bool(payload.get("return_txt"))
    workspace_hint = payload.get("workspace")
    cleanup_source = bool(payload.get("cleanup_source"))
    extra_cleanup = list(payload.get("extra_cleanup") or [])

    workspace = _ensure_workspace(existing=workspace_hint)
    target_path = workspace / source_path.name
    if source_path != target_path:
        shutil.copy2(source_path, target_path)
    if cleanup_source:
        try:
            source_path.unlink()
        except FileNotFoundError:
            pass
        except Exception:
            logger.debug("Failed to remove source file {}", source_path)

    logger.info("Parsing {} into workspace {}", target_path, workspace)
    try:
        response = parse_doc([target_path], workspace, backend=backend)
    except Exception as exc:
        size_hint = None
        try:
            size_hint = target_path.stat().st_size
        except Exception:
            size_hint = "unknown"
        raise RuntimeError(
            f"parse_doc raised for {target_path} (size={size_hint} bytes): {exc}"
        ) from exc

    if not response:
        raise RuntimeError(f"parse_doc returned no content for {target_path}")

    try:
        content_list, output_dir, _ = response
    except Exception as exc:
        raise RuntimeError(f"Unexpected parse_doc payload for {target_path}: {response!r}") from exc

    if content_list is None:
        raise RuntimeError("parse_doc returned empty content_list.")

    image_jobs, annotated_content = _build_image_jobs(
        content_list,
        output_dir or str(workspace),
        keep_positions=bool(payload.get("custom_vision_prompt")),
    )
    logger.info("Parsed {}: {} images found", target_path, len(image_jobs))

    return {
        "workspace": str(workspace),
        "upload_workspace": payload.get("upload_workspace"),
        "extra_cleanup": extra_cleanup,
        "content_list": annotated_content,
        "image_jobs": image_jobs,
        "chunk_type": chunk_type,
        "return_txt": return_txt,
    }


@celery_app.task(name="two_stage.vision", acks_late=True)
def vision_task(
    job: Dict[str, object],
    provider: Optional[Union[VisionProvider, str]] = None,
    model: Optional[Union[VisionModel, str]] = None,
    prompt: Optional[str] = None,
) -> Dict[str, object]:  # type: ignore[override]
    """Stage 2 header: run vision model for one image."""
    seq = job.get("seq")
    prompt_override = _normalize_prompt(prompt)
    try:
        vision_text = sanitize_vision_text(
            clean_text(
                vision_completion(
                    job["img_path"],
                    job.get("context_payload", "") or "",
                    prompt=prompt_override,
                    provider=provider,
                    model=model,
                )
            )
        )
        return {"seq": seq, "vision_text": vision_text}
    except Exception as exc:  # noqa: BLE001 - external call may fail
        logger.info("Vision call failed for seq={}: {}", seq, type(exc).__name__)
        raise RuntimeError(f"Vision call failed for seq={seq}: {exc}") from exc


@celery_app.task(name="two_stage.merge", acks_late=True)
def merge_task(
    vision_results: Sequence[Dict], parse_payload: Dict[str, object]
) -> Dict[str, object]:
    """Stage 2 body: merge vision outputs back into parsed content."""
    items, txt_text = _merge_content(
        parse_payload["content_list"],
        vision_results,
        chunk_type=bool(parse_payload.get("chunk_type")),
        return_txt=bool(parse_payload.get("return_txt")),
    )

    workspace = parse_payload.get("workspace")
    if workspace:
        shutil.rmtree(workspace, ignore_errors=True)
    upload_workspace = parse_payload.get("upload_workspace")
    if upload_workspace:
        shutil.rmtree(upload_workspace, ignore_errors=True)
    for path in parse_payload.get("extra_cleanup") or []:
        try:
            Path(path).unlink()
        except FileNotFoundError:
            pass
        except Exception:
            logger.debug("Failed to remove cleanup path {}", path)

    return {
        "result": [item.model_dump() for item in items],
        "txt": txt_text,
    }


@celery_app.task(name="two_stage.dispatch", acks_late=True, bind=True)
def dispatch(
    self,
    parse_payload: Dict[str, object],
    provider: Optional[Union[VisionProvider, str]] = None,
    model: Optional[Union[VisionModel, str]] = None,
    prompt: Optional[str] = None,
    vision_queue: Optional[str] = None,
    merge_queue: Optional[str] = None,
) -> Dict[str, object]:  # type: ignore[override]
    """Kick off vision fan-out + merge without blocking inside a task."""
    image_jobs: List[Dict[str, object]] = parse_payload.get("image_jobs") or []
    prompt_override = _normalize_prompt(prompt)
    resolved_vision_queue = vision_queue or VISION_QUEUE
    resolved_merge_queue = merge_queue or MERGE_QUEUE
    if not image_jobs:
        raise self.replace(merge_task.s([], parse_payload).set(queue=resolved_merge_queue))

    header = [
        vision_task.s(job, provider=provider, model=model, prompt=prompt_override).set(
            queue=resolved_vision_queue
        )
        for job in image_jobs
    ]
    raise self.replace(chord(header, merge_task.s(parse_payload).set(queue=resolved_merge_queue)))


def submit_two_stage_job(
    source_path: str,
    *,
    backend: Optional[str] = None,
    chunk_type: bool = False,
    return_txt: bool = False,
    provider: Optional[Union[VisionProvider, str]] = None,
    model: Optional[Union[VisionModel, str]] = None,
    prompt: Optional[str] = None,
    workspace: Optional[str] = None,
    cleanup_source: bool = False,
    extra_cleanup: Optional[Sequence[str]] = None,
    parse_queue: Optional[str] = None,
    vision_queue: Optional[str] = None,
    dispatch_queue: Optional[str] = None,
    merge_queue: Optional[str] = None,
):
    """Enqueue two-stage workflow; returns AsyncResult for the final merge."""
    resolved_parse_queue = parse_queue or PARSE_QUEUE
    resolved_dispatch_queue = dispatch_queue or DISPATCH_QUEUE
    resolved_vision_queue = vision_queue or VISION_QUEUE
    resolved_merge_queue = merge_queue or MERGE_QUEUE
    payload = {
        "source_path": source_path,
        "backend": backend,
        "chunk_type": chunk_type,
        "return_txt": return_txt,
        "workspace": workspace,
        "upload_workspace": workspace,
        "cleanup_source": cleanup_source,
        "extra_cleanup": list(extra_cleanup or []),
        "custom_vision_prompt": bool(_normalize_prompt(prompt)),
    }
    workflow = chain(
        parse_task.s(payload).set(queue=resolved_parse_queue),
        dispatch.s(
            provider=provider,
            model=model,
            prompt=prompt,
            vision_queue=resolved_vision_queue,
            merge_queue=resolved_merge_queue,
        ).set(queue=resolved_dispatch_queue),
    )
    return workflow.apply_async()
