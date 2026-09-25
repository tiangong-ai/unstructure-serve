# Copyright (c) Opendatalab. All rights reserved.
import json
import os
from itertools import cycle
from pathlib import Path
from threading import Lock
from typing import Optional

from dotenv import load_dotenv
from loguru import logger

from src.services.parse_capacity import parse_slot
from src.services.pdf_text_layer_reconcile import reconcile_content_list_checkboxes
from src.utils.mineru_backend import normalize_backend, resolve_tier

DEFAULT_VLLM_SERVER_URL = "http://127.0.0.1:30000"
_SERVER_URL_ENV_KEYS: tuple[str, ...] = (
    "MINERU_MODEL_VLM_SERVER_URL",
    "MINERU_VLLM_SERVER_URLS",
    "MINERU_VLLM_SERVER_URL",
    "MINERU_VLM_SERVER_URLS",
    "MINERU_VLM_SERVER_URL",
)
_DEFAULT_LANG = "ch"
_DEFAULT_METHOD = "auto"
_SERVER_URL_CYCLE_LOCK = Lock()
_SERVER_URL_CACHE: tuple[str, ...] = ()
_SERVER_URL_CYCLE = None

load_dotenv()


def mineru_parse(*args, **kwargs):
    # Load .env before MinerU constructs its config singleton.
    from mineru.parser import parse

    return parse(*args, **kwargs)


def render(middle_json):
    from mineru.render import RenderFormat, render as render_result

    return render_result(middle_json, RenderFormat.CONTENT_LIST)


def _normalize_server_url_input(raw_value) -> list[str]:
    """Accept strings, comma-separated strings, JSON arrays, or iterables."""
    if raw_value is None:
        return []

    if isinstance(raw_value, (list, tuple, set)):
        normalized: list[str] = []
        for item in raw_value:
            normalized.extend(_normalize_server_url_input(item))
        return normalized

    if isinstance(raw_value, str):
        candidate = raw_value.strip()
        if not candidate:
            return []
        if candidate.startswith("[") and candidate.endswith("]"):
            try:
                parsed = json.loads(candidate)
            except json.JSONDecodeError:
                pass
            else:
                return _normalize_server_url_input(parsed)
        if "," in candidate:
            return [part.strip() for part in candidate.split(",") if part.strip()]
        return [candidate]

    return _normalize_server_url_input(str(raw_value))


def _server_urls_from_env() -> list[str]:
    for key in _SERVER_URL_ENV_KEYS:
        raw_value = os.getenv(key)
        if not raw_value:
            continue
        urls = _normalize_server_url_input(raw_value)
        if urls:
            return urls
    return []


def _resolve_server_urls(server_url) -> list[str]:
    explicit_urls = _normalize_server_url_input(server_url)
    if explicit_urls:
        return explicit_urls

    env_urls = _server_urls_from_env()
    if env_urls:
        return env_urls

    return [DEFAULT_VLLM_SERVER_URL]


def _resolve_server_headers(headers: Optional[dict[str, str]] = None) -> Optional[dict[str, str]]:
    if headers:
        return headers

    auth_header = os.getenv("MINERU_VLLM_AUTH_HEADER")
    api_key = os.getenv("MINERU_VLLM_API_KEY")
    resolved: dict[str, str] = {}
    if auth_header and auth_header.strip():
        resolved["Authorization"] = auth_header.strip()
    elif api_key and api_key.strip():
        resolved["Authorization"] = f"Bearer {api_key.strip()}"

    return resolved or None


def _next_server_url(urls: list[str]) -> str:
    if not urls:
        raise ValueError("No VLM server URLs available.")
    if len(urls) == 1:
        return urls[0]

    global _SERVER_URL_CYCLE, _SERVER_URL_CACHE
    urls_tuple = tuple(urls)
    with _SERVER_URL_CYCLE_LOCK:
        if _SERVER_URL_CYCLE is None or _SERVER_URL_CACHE != urls_tuple:
            _SERVER_URL_CYCLE = cycle(urls_tuple)
            _SERVER_URL_CACHE = urls_tuple
            logger.debug("Configured VLM server pool: %s", urls_tuple)
        return next(_SERVER_URL_CYCLE)


def _env_default_lang() -> str:
    raw_value = os.getenv("MINERU_DEFAULT_LANG")
    if raw_value is not None:
        candidate = raw_value.strip()
        if candidate:
            return candidate
    return _DEFAULT_LANG


def _env_default_method() -> str:
    raw_value = os.getenv("MINERU_DEFAULT_METHOD")
    if raw_value is not None:
        candidate = raw_value.strip()
        if candidate:
            return candidate
    return _DEFAULT_METHOD


def _page_range(start_page_id: int, end_page_id: Optional[int]) -> str:
    if isinstance(start_page_id, bool) or not isinstance(start_page_id, int) or start_page_id < 0:
        raise ValueError("start_page_id must be a non-negative integer")
    if end_page_id is not None:
        if (
            isinstance(end_page_id, bool)
            or not isinstance(end_page_id, int)
            or end_page_id < start_page_id
        ):
            raise ValueError("end_page_id must be an integer >= start_page_id")
        return f"{start_page_id + 1}-{end_page_id + 1}"
    return "all" if start_page_id == 0 else f"{start_page_id + 1}-r1"


def _vlm_config(backend, server_url, server_headers):
    from mineru.config import VlmConfig, config

    values = config.model.vlm.model_dump()
    normalize_backend(backend)
    # Legacy backend names select quality only; VLM inference lives in Docker.
    explicit = _normalize_server_url_input(server_url)
    urls = explicit or _server_urls_from_env() or _normalize_server_url_input(values["server_url"])
    values["server_url"] = _next_server_url(urls or [DEFAULT_VLLM_SERVER_URL])

    headers = _resolve_server_headers(server_headers)
    if headers:
        if len(headers) != 1 or next(iter(headers)).lower() != "authorization":
            raise ValueError("MinerU 4 supports only a Bearer Authorization header")
        auth = next(iter(headers.values())).strip()
        scheme, _, key = auth.partition(" ")
        if scheme.lower() != "bearer" or not key.strip():
            raise ValueError("MinerU 4 requires Bearer authentication; custom auth needs a proxy")
        values["api_key"] = key.strip()
    if os.getenv("MINERU_MODEL_VLM_API_KEY") is not None and not server_headers:
        values["api_key"] = os.environ["MINERU_MODEL_VLM_API_KEY"].strip()
    return VlmConfig(**values)


def _normalize_content_list(items: list[dict], output_dir: Path, middle_json) -> list[dict]:
    """Project the V1 renderer into the service's stable downstream vocabulary."""
    geometries = {
        page["page_idx"]: page
        for page in middle_json.extensions.get("docvortex_layout", {}).get("pages", [])
    }
    normalized = []
    for original in items:
        item = dict(original)
        kind = item.get("type")
        if kind == "image":
            item["img_caption"] = item.get("image_caption") or item.get("img_caption") or []
            item["img_footnote"] = item.get("image_footnote") or item.get("img_footnote") or []
            if item.get("content"):
                item["img_caption"] = [*item["img_caption"], item["content"]]
        elif kind == "chart":
            item.update(
                type="image",
                img_caption=item.get("chart_caption") or [],
                img_footnote=item.get("chart_footnote") or [],
            )
            if item.get("content"):
                item["img_caption"] = [*item["img_caption"], item["content"]]
        elif kind == "code":
            parts = [
                *(item.get("code_caption") or []),
                item.get("code_body", ""),
                *(item.get("code_footnote") or []),
            ]
            item.update(type="text", text="\n".join(part for part in parts if part))
        elif kind == "index":
            item["type"] = "list"
        elif kind == "page_footnote":
            item["type"] = "text"

        geometry = geometries.get(item.get("page_idx"))
        if geometry and item.get("bbox"):
            width, height = geometry["width_pt"], geometry["height_pt"]
            item["bbox"] = [
                value * scale / 1000
                for value, scale in zip(item["bbox"], (width, height, width, height))
            ]
            item["page_size"] = [width, height]
        elif item.get("bbox"):
            item["page_size"] = [1000, 1000]
            item["bbox_normalized"] = True

        if item.get("img_path"):
            asset = (output_dir / item["img_path"]).resolve()
            if not asset.is_relative_to(output_dir.resolve()) or not asset.is_file():
                raise RuntimeError(f"Missing or invalid MinerU asset: {item['img_path']}")
        normalized.append(item)
    return normalized


_PDF_TEXT_LAYER_MIN_CHARS = 32


def _pdf_has_substantial_text(source: Path, start_page_id: int, end_page_id: Optional[int]) -> bool:
    """Check selected PDF pages only when MinerU returned no content."""
    import pypdfium2 as pdfium

    try:
        document = pdfium.PdfDocument(str(source))
    except pdfium.PdfiumError as exc:
        raise RuntimeError("Cannot validate empty MinerU PDF result") from exc

    try:
        last_page = len(document) - 1
        if end_page_id is not None:
            last_page = min(last_page, end_page_id)
        text_chars = 0
        for page_index in range(start_page_id, last_page + 1):
            page = document[page_index]
            try:
                text_page = page.get_textpage()
                try:
                    text_chars += sum(not char.isspace() for char in text_page.get_text_bounded())
                finally:
                    text_page.close()
            finally:
                page.close()
            if text_chars >= _PDF_TEXT_LAYER_MIN_CHARS:
                return True
        return False
    finally:
        document.close()


def parse_doc(
    path_list: list[Path],
    output_dir,
    lang: Optional[str] = None,
    backend: Optional[str] = None,
    method: Optional[str] = None,
    server_url=None,
    server_headers=None,
    start_page_id=0,
    end_page_id=None,
    dump_debug_intermediate=False,
    log_debug_intermediate=False,
    return_txt=False,
    *,
    tier: Optional[str] = None,
):
    """Adapt MinerU 4's stateless SDK to (content_list, artifact_dir, None).

    PDF indices remain source-document indices. Native DOCX is only used by
    the existing txt-only branch; the API still converts Office files to PDF.
    """
    from mineru.parser import ParseResult
    from mineru.parser.writer import FileBasedDataWriter

    del return_txt
    if not path_list:
        raise ValueError("path_list must not be empty.")
    if dump_debug_intermediate or log_debug_intermediate:
        logger.debug("MinerU 4 saves structured diagnostics alongside materialized assets")
    effective_method = (method or "").strip() or _env_default_method()
    if effective_method not in {"auto", "txt", "ocr"}:
        raise ValueError("MinerU ocr_mode must be auto/txt/ocr")
    effective_lang = (lang or "").strip() or _env_default_lang()
    if effective_lang not in {"ch", "auto"}:
        logger.warning(
            "MinerU 4 selects OCR languages internally; legacy lang={} is not forwarded",
            effective_lang,
        )
    requested_pages = _page_range(start_page_id, end_page_id)
    effective_tier = resolve_tier(backend, tier)
    last_content_list, last_output = None, None
    for path in path_list:
        source = Path(path)
        is_pdf = source.suffix.lower() == ".pdf"
        native_docx = source.suffix.lower() == ".docx"
        if not is_pdf and (start_page_id != 0 or end_page_id is not None):
            raise ValueError("Page ranges are supported only for PDF inputs")
        current_tier = "flash" if native_docx else effective_tier
        artifact_dir = Path(output_dir) / source.stem / current_tier
        artifact_dir.mkdir(parents=True, exist_ok=True)
        connection = (
            _vlm_config(backend, server_url, server_headers)
            if current_tier in {"standard", "advanced"}
            else None
        )
        with parse_slot():
            result = mineru_parse(
                source,
                tier=current_tier,
                ocr_mode=effective_method,
                page_range=requested_pages if is_pdf else "",
                vlm_config=connection,
            )
            if result is None:
                raise RuntimeError(f"MinerU returned no result for {source.name}")
            # ModelJson contains the raw analysis trace and can exceed hundreds
            # of MB. The public ParseResult constructor preserves the materialized
            # document/asset export while omitting that optional diagnostic.
            exported = (
                result if dump_debug_intermediate else ParseResult(middle_json=result.middle_json)
            )
            exported.save(FileBasedDataWriter(str(artifact_dir)))
            try:
                saved = ParseResult.from_json(
                    (artifact_dir / "middle_json.json").read_text("utf-8")
                )
            except (OSError, ValueError) as exc:
                raise RuntimeError(f"Invalid MinerU materialized result for {source.name}") from exc
            last_content_list = _normalize_content_list(
                render(saved.middle_json), artifact_dir, saved.middle_json
            )
            if (
                is_pdf
                and not last_content_list
                and _pdf_has_substantial_text(source, start_page_id, end_page_id)
            ):
                raise RuntimeError(
                    "MinerU returned no content for a PDF with a nonempty text layer"
                )
            if is_pdf:
                reconcile_content_list_checkboxes(last_content_list, source)
            (artifact_dir / f"{source.stem}_content_list.json").write_text(
                json.dumps(last_content_list, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            last_output = str(artifact_dir)
    return last_content_list, last_output, None
