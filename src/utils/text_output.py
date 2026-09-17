from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Optional

_PAGE_MARKER_RE = re.compile(r"\[Page\s+\d+\]", re.IGNORECASE)
_CHUNK_MARKER_RE = re.compile(r"\[ChunkType=[^\]]+\]", re.IGNORECASE)
_IMAGE_PREFIX_RE = re.compile(r"^\s*Image Description:\s*", re.IGNORECASE)
_THINK_PREFIX_RE = re.compile(r"^\s*<think>.*?</think>\s*", re.IGNORECASE | re.DOTALL)
_BOILERPLATE_PREFIX_RE = re.compile(
    r"^\s*(?:Here (?:is|are) (?:the |a )?(?:summary|analysis|key findings)|"
    r"以下是(?:图片|图像|该图|这张图片)?(?:的)?(?:分析|描述|摘要)|"
    r"根据(?:您提供的|提供的)?上下文(?:信息)?)[：:,，]\s*",
    re.IGNORECASE,
)
_SURROGATE_RE = re.compile(r"[\ud800-\udfff]")


def clean_text(text: str) -> str:
    """Remove surrogate code points; all remaining Python characters are valid UTF-8."""
    return _SURROGATE_RE.sub("", text) if text else ""


def _extract_text_and_type(item) -> tuple[str, Optional[str]]:
    """Extract text and type metadata from either mapping or object-like chunk."""
    if isinstance(item, Mapping):
        raw_text = item.get("text")
        item_type = item.get("type")
    else:
        raw_text = getattr(item, "text", None)
        item_type = getattr(item, "type", None)

    text = (raw_text or "").strip()
    if not text:
        return "", None

    if isinstance(item_type, str) and item_type.strip():
        return text, item_type.strip()
    return text, None


def build_plain_text(chunks: Iterable[object]) -> str:
    """Compose a plain-text export from parsed MinerU chunks.

    Titles receive a double newline suffix, regular text gets a single newline.
    """
    parts: list[str] = []
    for chunk in chunks:
        text, chunk_type = _extract_text_and_type(chunk)
        if not text:
            continue

        if chunk_type == "title":
            parts.append(f"{text}\n\n")
        else:
            parts.append(f"{text}\n")

    return "".join(parts).rstrip("\n")


def sanitize_vision_text(text: str, *, strip_boilerplate: bool = True) -> str:
    """Remove internal context markers and helper prefixes from vision outputs."""
    if not text:
        return ""
    cleaned = text.strip()
    if strip_boilerplate:
        cleaned = _THINK_PREFIX_RE.sub("", cleaned)
        if cleaned.lower().startswith("<think>"):
            raise ValueError("Incomplete vision reasoning block")
        cleaned = _BOILERPLATE_PREFIX_RE.sub("", cleaned)
    cleaned = _IMAGE_PREFIX_RE.sub("", cleaned)
    cleaned = _PAGE_MARKER_RE.sub("", cleaned)
    cleaned = _CHUNK_MARKER_RE.sub("", cleaned)
    lines = [line.strip() for line in cleaned.splitlines()]
    result = "\n".join(lines).strip()
    if strip_boilerplate and not result:
        raise ValueError("Vision output contains no facts after removing wrappers")
    return result


__all__ = ["build_plain_text", "clean_text", "sanitize_vision_text"]
