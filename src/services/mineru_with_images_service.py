import os
import tempfile
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from typing import Dict, List, Optional, Sequence, Tuple, Union

from loguru import logger

from src.models.models import ResponseWithPageNum, TextElementWithPageNum
from src.services.mineru_service_full import parse_doc
from src.utils.text_output import build_plain_text, clean_text, sanitize_vision_text
from src.services.vision_service import (
    VisionModel,
    VisionProvider,
    vision_completion,
)


def _env_context_window() -> int:
    raw_value = os.getenv("VISION_CONTEXT_WINDOW")
    if raw_value is None:
        return 2
    try:
        parsed = int(raw_value)
        return max(parsed, 0)
    except (TypeError, ValueError):
        logger.warning(
            "Invalid VISION_CONTEXT_WINDOW=%s, falling back to default (2).",
            raw_value,
        )
        return 2


CONTEXT_WINDOW = _env_context_window()


def _env_vision_batch_size() -> int:
    raw_value = os.getenv("VISION_BATCH_SIZE")
    if raw_value is None:
        return 3
    try:
        parsed = int(raw_value)
        return max(parsed, 1)
    except (TypeError, ValueError):
        logger.warning(
            "Invalid VISION_BATCH_SIZE=%s, falling back to default (3).",
            raw_value,
        )
        return 3


VISION_BATCH_SIZE = _env_vision_batch_size()

STRICT_DOCX_IMAGE_OCR_PROMPT = (
    "Perform strict OCR and visible-content extraction for this embedded document image. "
    "Return raw plain text only, in reading order. Output only content that is directly visible "
    "in the image. Do not summarize, explain, infer identities, organizations, websites, "
    "projects, authors, intent, or relationships. Do not translate, rewrite, normalize, or "
    "group the content into categories. Do not add headings, bullets, markdown, tables, labels, "
    "or bracketed descriptions unless those exact words or symbols are visibly present in the "
    "image. Do not use surrounding context as factual evidence; use it only to understand where "
    "the image appears in the document. Prefer verbatim transcription of prominent visible text "
    "in the original language. For screenshots or diagrams, transcribe the visible words and "
    "short phrases only. If there is almost no readable text, return one short literal "
    "description of only the visibly obvious content. Do not mention the context, and do not "
    "output phrases such as 'Based on the context', 'The image shows', '根据上下文', "
    "'根据您提供的图片', or '以下是'."
)


def _coerce_text_parts(value: object) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        cleaned = clean_text(value)
        return [cleaned] if cleaned.strip() else []
    if isinstance(value, (list, tuple)):
        parts: List[str] = []
        for item in value:
            parts.extend(_coerce_text_parts(item))
        return parts
    cleaned = clean_text(str(value))
    return [cleaned] if cleaned.strip() else []


def _image_captions(item: Dict) -> List[str]:
    return _coerce_text_parts(item.get("img_caption") or item.get("image_caption"))


def _image_footnotes(item: Dict) -> List[str]:
    return _coerce_text_parts(item.get("img_footnote") or item.get("image_footnote"))


def image_text(item: Dict) -> str:
    captions = _image_captions(item)
    footnotes = _image_footnotes(item)
    text = "\n".join([*captions, *footnotes])
    return clean_text(text)


def table_text(item: Dict) -> str:
    text = "\n".join(
        filter(
            None,
            [
                "\n".join(item.get("table_caption", [])),
                item.get("table_body", ""),
                "\n".join(item.get("table_footnote", [])),
            ],
        )
    )
    return clean_text(text)


def list_text(item: Dict) -> str:
    items = item.get("list_items") or []
    if items:
        return clean_text("\n".join(items))
    return clean_text(item.get("text", ""))


def _format_context_line(page_idx: int, text: str, is_title: bool = False) -> str:
    if not text:
        return ""
    prefixes: List[str] = []
    if page_idx is not None and page_idx >= 0:
        prefixes.append(f"[Page {int(page_idx) + 1}]")
    chunk_marker = "[ChunkType=Title]" if is_title else "[ChunkType=Body]"
    prefixes.append(chunk_marker)
    if prefixes:
        return f"{' '.join(prefixes)} {text}"
    return text


def get_prev_context(context_elements: List[Dict], cur_idx: int, n: int) -> str:
    """获取前 n 个非空上下文块文本，倒序拼接。"""
    if cur_idx is None or cur_idx < 0 or not context_elements:
        return ""
    res: List[str] = []
    j = cur_idx - 1
    while j >= 0 and len(res) < n:
        if j < len(context_elements):
            block = context_elements[j]
            text = block["text"].strip()
            if text:
                formatted = _format_context_line(
                    block.get("page_idx", -1),
                    text,
                    block.get("is_title", False),
                )
                res.insert(0, formatted)
        j -= 1
    return "\n".join(res)


def get_next_context(context_elements: List[Dict], cur_idx: int, n: int) -> str:
    """获取后 n 个非空上下文块文本，正序拼接。"""
    if cur_idx is None or not context_elements:
        return ""
    res: List[str] = []
    j = cur_idx + 1
    while j < len(context_elements) and len(res) < n:
        if j >= 0:
            block = context_elements[j]
            text = block["text"].strip()
            if text:
                formatted = _format_context_line(
                    block.get("page_idx", -1),
                    text,
                    block.get("is_title", False),
                )
                res.append(formatted)
        j += 1
    return "\n".join(res)


def _build_context_blocks(
    content_list: List[Dict], *, include_image_notes: bool = True
) -> List[Dict]:
    context_blocks: List[Dict] = []
    for item in content_list:
        block: Optional[Dict] = None
        if item["type"] in ("text", "equation"):
            if item.get("text", "").strip():
                is_title = bool(item.get("text_level") is not None)
                block = {
                    "type": item["type"],
                    "text": clean_text(item["text"]),
                    "page_idx": item.get("page_idx", -1),
                    "orig_item": item,
                    "is_title": is_title,
                }
        elif item["type"] == "list":
            list_txt = list_text(item)
            if list_txt.strip():
                block = {
                    "type": "list",
                    "text": list_txt,
                    "page_idx": item.get("page_idx", -1),
                    "orig_item": item,
                    "is_title": bool(item.get("text_level") is not None),
                }
        elif item["type"] == "table":
            table_txt = table_text(item)
            if table_txt.strip():
                block = {
                    "type": "table",
                    "text": table_txt,
                    "page_idx": item.get("page_idx", -1),
                    "orig_item": item,
                    "is_title": False,
                }
        elif item["type"] == "image":
            img_txt = image_text(item) if include_image_notes else ""
            has_image_payload = bool(item.get("img_path") and item["img_path"].strip())
            if img_txt.strip() or has_image_payload:
                block = {
                    "type": "image_caption" if img_txt.strip() else "image",
                    "text": img_txt,
                    "page_idx": item.get("page_idx", -1),
                    "orig_item": item,
                    "is_title": False,
                }
        if block:
            context_blocks.append(block)
    return context_blocks


def _reindex_blocks(blocks: List[Dict]) -> Dict[int, int]:
    return {
        id(block.get("orig_item")): idx
        for idx, block in enumerate(blocks)
        if block.get("orig_item") is not None
    }


def _resolve_context_windows(
    working_blocks: List[Dict], cur_idx: Optional[int], item: Dict
) -> Dict[str, str]:
    before_ctx = ""
    after_ctx = ""
    if not working_blocks:
        return {"before": before_ctx, "after": after_ctx}

    if cur_idx is not None and 0 <= cur_idx < len(working_blocks):
        before_ctx = get_prev_context(working_blocks, cur_idx, n=CONTEXT_WINDOW)
        after_ctx = get_next_context(working_blocks, cur_idx, n=CONTEXT_WINDOW)
        return {"before": before_ctx, "after": after_ctx}

    current_page = item.get("page_idx", -1)
    ref_idx: Optional[int] = None
    for idx, block in enumerate(working_blocks):
        block_page = block.get("page_idx", -1)
        if block_page <= current_page:
            ref_idx = idx
        else:
            break

    if ref_idx is not None:
        before_ctx = get_prev_context(working_blocks, ref_idx + 1, n=CONTEXT_WINDOW)
        after_ctx = get_next_context(working_blocks, ref_idx, n=CONTEXT_WINDOW)
    else:
        after_ctx = get_next_context(working_blocks, -1, n=CONTEXT_WINDOW)
    return {"before": before_ctx, "after": after_ctx}


def _build_vision_prompt(
    item: Dict, contexts: Dict[str, str], *, include_image_notes: bool = True
) -> Tuple[str, List[Tuple[str, str]]]:
    captions = "\n".join(_image_captions(item)) if include_image_notes else ""
    footnotes = "\n".join(_image_footnotes(item)) if include_image_notes else ""
    prompt_parts: List[Tuple[str, str]] = []
    page_idx = item.get("page_idx", -1)
    page_suffix = f" (Page {int(page_idx) + 1})" if page_idx is not None and page_idx >= 0 else ""
    if captions.strip():
        prompt_parts.append((f"Image caption{page_suffix}", captions))
    if footnotes.strip():
        prompt_parts.append((f"Image footnote{page_suffix}", footnotes))
    if contexts["before"].strip():
        prompt_parts.append(("Context before", contexts["before"]))
    if contexts["after"].strip():
        prompt_parts.append(("Context after", contexts["after"]))
    prompt_lines = [f"{label}: {value}" for label, value in prompt_parts]
    return "\n".join(prompt_lines), prompt_parts


def _log_vision_prompt(
    page_number: int, contexts: Dict[str, str], prompt_parts: Sequence[Tuple[str, str]]
) -> None:
    before_ctx = contexts.get("before", "").strip() or "<empty>"
    after_ctx = contexts.get("after", "").strip() or "<empty>"
    logger.debug(
        "Vision context for page {page_number}\n  before: {before}\n  after: {after}",
        page_number=page_number,
        before=before_ctx,
        after=after_ctx,
    )
    if prompt_parts:
        formatted = "\n".join(f"  {label}: {value}" for label, value in prompt_parts)
        logger.info(f"Vision prompt payload for page {page_number}:\n{formatted}")
    else:
        logger.info(f"Vision prompt payload for page {page_number}: <empty>")


def _normalize_prompt_override(vision_prompt: Optional[str]) -> Optional[str]:
    if vision_prompt is None:
        return None
    stripped = vision_prompt.strip()
    return stripped or None


def _strict_docx_prompt(vision_prompt: Optional[str]) -> str:
    prompt_override = _normalize_prompt_override(vision_prompt)
    if not prompt_override:
        return STRICT_DOCX_IMAGE_OCR_PROMPT
    return (
        f"{STRICT_DOCX_IMAGE_OCR_PROMPT}\n\n"
        "Additional user instruction below must not override the OCR-only rule:\n"
        f"{prompt_override}"
    )


def _run_image_vision(
    content_list: List[Dict],
    output_dir: str,
    *,
    vision_provider: Optional[Union[VisionProvider, str]] = None,
    vision_model: Optional[Union[VisionModel, str]] = None,
    vision_prompt: Optional[str] = None,
    strict_ocr_only: bool = False,
) -> Dict[int, str]:
    include_image_notes = not strict_ocr_only
    context_blocks = _build_context_blocks(content_list, include_image_notes=include_image_notes)
    item_to_block_idx = _reindex_blocks(context_blocks)
    prompt_override = (
        _strict_docx_prompt(vision_prompt)
        if strict_ocr_only
        else _normalize_prompt_override(vision_prompt)
    )

    image_jobs: List[Dict[str, object]] = []
    for item in content_list:
        if not (item["type"] == "image" and item.get("img_path") and item["img_path"].strip()):
            continue

        img_path = os.path.join(output_dir, item["img_path"])
        page_number = int(item.get("page_idx", 0)) + 1
        if not os.path.exists(img_path):
            logger.info(f"Skipping image on page {page_number}: file not found at {img_path}")
            continue

        cur_idx = item_to_block_idx.get(id(item))
        contexts = _resolve_context_windows(context_blocks, cur_idx, item)
        context_payload, prompt_parts = _build_vision_prompt(
            item,
            contexts,
            include_image_notes=include_image_notes,
        )
        _log_vision_prompt(page_number, contexts, prompt_parts)

        logger.info(
            f"Queueing image {len(image_jobs) + 1} "
            f"(page {page_number}, batch size {VISION_BATCH_SIZE})..."
        )

        image_jobs.append(
            {
                "item": item,
                "img_path": img_path,
                "page_number": page_number,
                "context_payload": context_payload,
                "base_text": "" if strict_ocr_only else image_text(item),
            }
        )

    for idx, job in enumerate(image_jobs, start=1):
        job["seq"] = idx

    total_images = len(image_jobs)
    image_results: Dict[int, str] = {}
    # Keep one bounded rolling window: a slow image must not hold up the next batch.
    pending_jobs = iter(image_jobs)
    with ThreadPoolExecutor(max_workers=VISION_BATCH_SIZE) as executor:
        futures = {}

        def submit_next():
            job = next(pending_jobs, None)
            if job is None:
                return
            future = executor.submit(
                vision_completion,
                job["img_path"],
                job["context_payload"],
                prompt_override,
                vision_provider,
                vision_model,
            )
            futures[future] = job

        for _ in range(min(VISION_BATCH_SIZE, total_images)):
            submit_next()
        try:
            while futures:
                completed, _ = wait(futures, return_when=FIRST_COMPLETED)
                for future in completed:
                    job = futures.pop(future)
                    seq = int(job["seq"])
                    page_number = int(job["page_number"])
                    try:
                        vision_result = sanitize_vision_text(
                            clean_text(future.result()), strip_boilerplate=not strict_ocr_only
                        )
                        combined_text = "\n".join(
                            part for part in (str(job["base_text"]), vision_result) if part
                        )
                        if combined_text:
                            image_results[id(job["item"])] = clean_text(combined_text)
                        logger.info(f"Vision analysis complete for image {seq}/{total_images}")
                    except Exception as exc:  # noqa: BLE001 - vision call can fail
                        raise RuntimeError(
                            f"Vision analysis failed for image {seq}/{total_images} "
                            f"on page {page_number}: {exc}"
                        ) from exc
                for _ in completed:
                    submit_next()
        finally:
            for future in futures:
                future.cancel()

    logger.info(f"Completed processing all {total_images} images")
    return image_results


def _build_result_items(
    content_list: List[Dict],
    image_results: Dict[int, str],
    *,
    chunk_type: bool,
) -> List[Dict[str, object]]:
    result_items: List[Dict[str, object]] = []
    for item in content_list:
        page_number = int(item.get("page_idx", 0)) + 1
        is_title = item.get("type") == "text" and item.get("text_level") is not None

        if item["type"] == "image" and item.get("img_path") and item["img_path"].strip():
            combined_text = image_results.get(id(item), "").strip()
            if combined_text:
                chunk = {"text": combined_text, "page_number": page_number}
                if chunk_type:
                    chunk["type"] = "image"
                result_items.append(chunk)
        elif item["type"] in ("header", "footer"):
            if not chunk_type:
                continue
            header_txt = clean_text(item.get("text", ""))
            if header_txt.strip():
                chunk = {
                    "text": header_txt,
                    "page_number": page_number,
                    "type": item["type"],
                }
                result_items.append(chunk)
        elif item["type"] == "list":
            list_txt = list_text(item)
            if list_txt.strip():
                chunk = {"text": list_txt, "page_number": page_number}
                if chunk_type and is_title:
                    chunk["type"] = "title"
                result_items.append(chunk)
        elif item["type"] in ("text", "equation") and item.get("text", "").strip():
            chunk = {"text": clean_text(item["text"]), "page_number": page_number}
            if chunk_type and is_title:
                chunk["type"] = "title"
            result_items.append(chunk)
        elif item["type"] == "table" and (
            item.get("table_caption") or item.get("table_body") or item.get("table_footnote")
        ):
            chunk = {"text": table_text(item), "page_number": page_number}
            if chunk_type and is_title:
                chunk["type"] = "title"
            result_items.append(chunk)
        elif (
            item["type"] == "image"
            and (_image_captions(item) or _image_footnotes(item))
            and not (item.get("img_path") and item["img_path"].strip())
        ):
            img_txt = image_text(item)
            if img_txt.strip():
                chunk = {"text": img_txt, "page_number": page_number}
                if chunk_type:
                    chunk["type"] = "image"
                result_items.append(chunk)

    return result_items


def _build_native_docx_txt_items(
    file_path: str,
    *,
    chunk_type: bool,
    backend: Optional[str] = None,
    vision_provider: Optional[Union[VisionProvider, str]] = None,
    vision_model: Optional[Union[VisionModel, str]] = None,
    vision_prompt: Optional[str] = None,
) -> List[Dict[str, object]]:
    with tempfile.TemporaryDirectory() as tmp_dir:
        content_list, output_dir, _ = parse_doc([file_path], tmp_dir, backend=backend)
        image_results = _run_image_vision(
            content_list,
            output_dir,
            vision_provider=vision_provider,
            vision_model=vision_model,
            vision_prompt=vision_prompt,
            strict_ocr_only=True,
        )
        return _build_result_items(content_list, image_results, chunk_type=chunk_type)


def parse_with_images(
    file_path: str,
    *,
    chunk_type: bool = False,
    backend: Optional[str] = None,
    vision_provider: Optional[Union[VisionProvider, str]] = None,
    vision_model: Optional[Union[VisionModel, str]] = None,
    vision_prompt: Optional[str] = None,
    return_txt: bool = False,
    txt_source_path: Optional[str] = None,
    txt_from_native_docx: bool = False,
) -> Tuple[List[Dict[str, object]], Optional[str]]:
    """Run MinerU parsing (GPU scheduler friendly) then enrich figures via multimodal vision."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        content_list, output_dir, _ = parse_doc([file_path], tmp_dir, backend=backend)
        image_results = _run_image_vision(
            content_list,
            output_dir,
            vision_provider=vision_provider,
            vision_model=vision_model,
            vision_prompt=vision_prompt,
        )
        result_items = _build_result_items(content_list, image_results, chunk_type=chunk_type)

        txt_items = result_items
        if return_txt and txt_from_native_docx and txt_source_path:
            txt_items = _build_native_docx_txt_items(
                txt_source_path,
                chunk_type=chunk_type,
                backend=backend,
                vision_provider=vision_provider,
                vision_model=vision_model,
                vision_prompt=vision_prompt,
            )

        txt_text = build_plain_text(txt_items) if return_txt else None
        return result_items, txt_text


def mineru_service(
    file_path: str, *, chunk_type: bool = False, return_txt: bool = False
) -> ResponseWithPageNum:
    payload, txt_text = parse_with_images(
        file_path,
        chunk_type=chunk_type,
        return_txt=return_txt,
    )
    items = [
        TextElementWithPageNum(
            text=entry["text"],
            page_number=int(entry["page_number"]),
            type=entry.get("type"),
        )
        for entry in payload
    ]
    return ResponseWithPageNum(result=items, txt=txt_text)
