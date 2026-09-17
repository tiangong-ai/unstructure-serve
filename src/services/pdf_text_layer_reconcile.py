import html
import os
import re
import subprocess
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

from loguru import logger

CHECKBOX_CHARS = "☐☑□■"
SELECTED_CHECKBOX_CHARS = {"☑", "■"}
UNCHECKED_CHECKBOX_CHARS = {"☐", "□"}
_CHECKBOX_RE = re.compile(f"[{re.escape(CHECKBOX_CHARS)}]")
_TABLE_ROW_RE = re.compile(r"<tr\b[^>]*>.*?</tr>", re.IGNORECASE | re.DOTALL)
_TABLE_CELL_RE = re.compile(r"<t[dh]\b[^>]*>(.*?)</t[dh]>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_DEFAULT_TIMEOUT_SECONDS = 30
_ROW_Y_TOLERANCE = 3.0


@dataclass(frozen=True)
class _Word:
    page_idx: int
    x_min: float
    y_min: float
    text: str


@dataclass(frozen=True)
class _CheckboxEntry:
    label: str
    selected: bool

    @property
    def normalized_label(self) -> str:
        return _normalize_label(self.label)


@dataclass(frozen=True)
class _CheckboxRow:
    page_idx: int
    entries: tuple[_CheckboxEntry, ...]


def _env_enabled() -> bool:
    raw_value = os.getenv("MINERU_TEXT_LAYER_CHECKBOX_RECONCILE")
    if raw_value is None:
        return True
    return raw_value.strip().lower() not in {"0", "false", "no", "off"}


def _env_timeout_seconds() -> int:
    raw_value = os.getenv("MINERU_TEXT_LAYER_TIMEOUT_SECONDS")
    if raw_value is None:
        return _DEFAULT_TIMEOUT_SECONDS
    try:
        return max(int(raw_value), 1)
    except (TypeError, ValueError):
        logger.warning(
            "Invalid MINERU_TEXT_LAYER_TIMEOUT_SECONDS={}, falling back to {}s.",
            raw_value,
            _DEFAULT_TIMEOUT_SECONDS,
        )
        return _DEFAULT_TIMEOUT_SECONDS


def _content_list_has_checkbox(content_list: list[dict]) -> bool:
    for item in content_list:
        for key in (
            "text",
            "table_body",
            "list_items",
            "table_caption",
            "table_footnote",
            "img_caption",
            "img_footnote",
        ):
            value = item.get(key)
            if isinstance(value, str) and _CHECKBOX_RE.search(value):
                return True
            if isinstance(value, list) and any(
                isinstance(part, str) and _CHECKBOX_RE.search(part) for part in value
            ):
                return True
    return False


def _run_pdftotext_bbox(pdf_path: Path) -> Optional[str]:
    try:
        completed = subprocess.run(
            ["pdftotext", "-bbox", str(pdf_path), "-"],
            check=False,
            capture_output=True,
            text=True,
            timeout=_env_timeout_seconds(),
        )
    except FileNotFoundError:
        logger.debug("pdftotext not found; skipping PDF text-layer checkbox reconciliation.")
        return None
    except subprocess.TimeoutExpired:
        logger.warning(
            "pdftotext -bbox timed out for {}; skipping checkbox reconciliation.",
            pdf_path,
        )
        return None

    if completed.returncode != 0:
        logger.debug(
            "pdftotext -bbox failed for {} with exit code {}: {}",
            pdf_path,
            completed.returncode,
            completed.stderr.strip(),
        )
        return None
    return completed.stdout


def _tag_name(element: ET.Element) -> str:
    return element.tag.rsplit("}", maxsplit=1)[-1]


def _parse_float(value: Optional[str]) -> float:
    try:
        return float(value or "0")
    except ValueError:
        return 0.0


def _parse_bbox_words(payload: str) -> list[_Word]:
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as exc:
        logger.debug("Unable to parse pdftotext -bbox XML: {}", exc)
        return []

    words: list[_Word] = []
    page_idx = -1
    for page in root.iter():
        if _tag_name(page) != "page":
            continue
        page_idx += 1
        for word in page.iter():
            if _tag_name(word) != "word":
                continue
            text = "".join(word.itertext()).strip()
            if not text:
                continue
            words.append(
                _Word(
                    page_idx=page_idx,
                    x_min=_parse_float(word.attrib.get("xMin")),
                    y_min=_parse_float(word.attrib.get("yMin")),
                    text=text,
                )
            )
    return words


def _group_words_by_visual_row(words: list[_Word]) -> list[list[_Word]]:
    rows: list[list[_Word]] = []
    for word in sorted(words, key=lambda item: (item.page_idx, item.y_min, item.x_min)):
        if not rows:
            rows.append([word])
            continue
        prev = rows[-1][0]
        if prev.page_idx == word.page_idx and abs(prev.y_min - word.y_min) <= _ROW_Y_TOLERANCE:
            rows[-1].append(word)
        else:
            rows.append([word])

    for row in rows:
        row.sort(key=lambda item: item.x_min)
    return rows


def _normalize_label(value: str) -> str:
    value = html.unescape(value or "")
    value = _TAG_RE.sub("", value)
    value = re.sub(r"\s+", "", value)
    return value.strip(" :：;；,，")


def _label_matches(source_label: str, target_label: str) -> bool:
    source = _normalize_label(source_label)
    target = _normalize_label(target_label)
    if not source or not target:
        return False
    if source == target:
        return True
    if len(source) >= 4 and target.startswith(source):
        return True
    return len(target) >= 4 and source.startswith(target)


def _extract_source_rows_from_words(words: list[_Word]) -> dict[int, list[_CheckboxRow]]:
    page_rows: dict[int, list[_CheckboxRow]] = {}
    for visual_row in _group_words_by_visual_row(words):
        entries: list[_CheckboxEntry] = []
        idx = 0
        while idx < len(visual_row):
            word = visual_row[idx]
            marker = word.text[:1]
            if marker not in CHECKBOX_CHARS:
                idx += 1
                continue

            label = word.text[1:].strip()
            if not label and idx + 1 < len(visual_row):
                next_word = visual_row[idx + 1].text
                if next_word[:1] not in CHECKBOX_CHARS:
                    label = next_word.strip()

            normalized_label = _normalize_label(label)
            if normalized_label:
                entries.append(
                    _CheckboxEntry(
                        label=label,
                        selected=marker in SELECTED_CHECKBOX_CHARS,
                    )
                )
            idx += 1

        if entries:
            page_rows.setdefault(visual_row[0].page_idx, []).append(
                _CheckboxRow(page_idx=visual_row[0].page_idx, entries=tuple(entries))
            )
    return page_rows


def _strip_tags(value: str) -> str:
    return html.unescape(_TAG_RE.sub(" ", value or ""))


def _trim_plain_label(value: str) -> str:
    value = html.unescape(value or "").strip()
    value = re.sub(r"\s+", " ", value)
    if not value:
        return ""
    parts = value.split(" ")
    if len(parts) > 1 and len(parts[0]) <= 4:
        return parts[0]
    return value


def _extract_entries_from_text(value: str) -> list[_CheckboxEntry]:
    entries: list[_CheckboxEntry] = []
    matches = list(_CHECKBOX_RE.finditer(value or ""))
    for idx, match in enumerate(matches):
        start = match.end()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(value)
        label = _trim_plain_label(value[start:end])
        if _normalize_label(label):
            entries.append(
                _CheckboxEntry(
                    label=label,
                    selected=match.group(0) in SELECTED_CHECKBOX_CHARS,
                )
            )
    return entries


def _extract_entries_from_html_fragment(fragment: str) -> list[_CheckboxEntry]:
    cells = _TABLE_CELL_RE.findall(fragment or "")
    if not cells:
        return _extract_entries_from_text(_strip_tags(fragment))

    entries: list[_CheckboxEntry] = []
    for cell in cells:
        entries.extend(_extract_entries_from_text(_strip_tags(cell)))
    return entries


def _source_for_target_row(
    target_entries: list[_CheckboxEntry],
    source_rows: list[_CheckboxRow],
) -> Optional[_CheckboxRow]:
    if not target_entries:
        return None

    best_row: Optional[_CheckboxRow] = None
    best_score = 0
    for source_row in source_rows:
        score = 0
        for target_entry in target_entries:
            if any(
                _label_matches(source_entry.label, target_entry.label)
                for source_entry in source_row.entries
            ):
                score += 1
        if score > best_score:
            best_score = score
            best_row = source_row

    threshold = min(2, len(target_entries))
    if best_score >= threshold:
        return best_row
    return None


def _source_entry_for_target(
    target_entry: _CheckboxEntry,
    source_entries: Iterable[_CheckboxEntry],
) -> Optional[_CheckboxEntry]:
    for source_entry in source_entries:
        if _label_matches(source_entry.label, target_entry.label):
            return source_entry
    return None


def _replace_marker_for_label(fragment: str, label: str, *, selected: bool) -> tuple[str, int]:
    desired = "☑" if selected else "☐"
    current_selected = SELECTED_CHECKBOX_CHARS if selected else UNCHECKED_CHECKBOX_CHARS
    pattern = re.compile(
        rf"(?P<marker>[{re.escape(CHECKBOX_CHARS)}])(?P<gap>\s*){re.escape(label)}"
    )
    changes = 0

    def _replace(match: re.Match[str]) -> str:
        nonlocal changes
        marker = match.group("marker")
        if marker in current_selected:
            return match.group(0)
        changes += 1
        return f"{desired}{match.group('gap')}{label}"

    return pattern.sub(_replace, fragment), changes


def _restore_leading_cell_marker(fragment: str, source_rows: list[_CheckboxRow]) -> tuple[str, int]:
    """Restore one omitted leading glyph only for an exact, unambiguous option group."""
    changes = 0

    def _replace_cell(match: re.Match[str]) -> str:
        nonlocal changes
        cell = match.group(1)
        candidates = []
        for row in source_rows:
            # Require two surviving marked options and a distinctive first label.
            if len(row.entries) < 3 or len(row.entries[0].normalized_label) < 4:
                continue
            pattern = r"\s*" + re.escape(row.entries[0].label)
            for entry in row.entries[1:]:
                pattern += rf"\s*[{re.escape(CHECKBOX_CHARS)}]\s*{re.escape(entry.label)}"
            if re.fullmatch(pattern + r"\s*", cell):
                candidates.append(row)
        if len(candidates) != 1:
            return match.group(0)
        marker = "☑" if candidates[0].entries[0].selected else "☐"
        offset = match.start(1) - match.start() + len(cell) - len(cell.lstrip())
        changes += 1
        return match.group(0)[:offset] + marker + match.group(0)[offset:]

    return _TABLE_CELL_RE.sub(_replace_cell, fragment), changes


def _reconcile_fragment(fragment: str, source_rows: list[_CheckboxRow]) -> tuple[str, int]:
    fragment, changes = _restore_leading_cell_marker(fragment, source_rows)
    target_entries = _extract_entries_from_html_fragment(fragment)
    source_row = _source_for_target_row(target_entries, source_rows)
    if source_row is None:
        return fragment, changes

    updated = fragment
    for target_entry in target_entries:
        source_entry = _source_entry_for_target(target_entry, source_row.entries)
        if source_entry is None:
            continue
        updated, entry_changes = _replace_marker_for_label(
            updated,
            target_entry.label,
            selected=source_entry.selected,
        )
        changes += entry_changes
    return updated, changes


def _reconcile_html_rows(value: str, source_rows: list[_CheckboxRow]) -> tuple[str, int]:
    changes = 0

    def _replace_row(match: re.Match[str]) -> str:
        nonlocal changes
        updated, row_changes = _reconcile_fragment(match.group(0), source_rows)
        changes += row_changes
        return updated

    updated = _TABLE_ROW_RE.sub(_replace_row, value)
    if updated == value:
        updated, changes = _reconcile_fragment(value, source_rows)
    return updated, changes


def _reconcile_text_value(value: str, source_rows: list[_CheckboxRow]) -> tuple[str, int]:
    if "<tr" in value.lower():
        return _reconcile_html_rows(value, source_rows)
    return _reconcile_fragment(value, source_rows)


def reconcile_content_list_checkboxes(content_list: list[dict], pdf_path: str | Path) -> int:
    """Repair MinerU checkbox states using the original PDF text layer.

    MinerU can occasionally convert selected text-layer checkboxes into unchecked
    table symbols. This postprocess is intentionally narrow: it only runs when
    MinerU output already contains checkbox symbols, reads Poppler's bbox text
    layer, and updates matching rows on the same page.
    A missing leading cell glyph also requires a unique, exact option group with
    at least two surviving markers and a first label of four or more characters.
    """

    if not _env_enabled():
        return 0
    if not content_list or not _content_list_has_checkbox(content_list):
        return 0

    source_path = Path(pdf_path)
    if source_path.suffix.lower() != ".pdf":
        return 0

    bbox_payload = _run_pdftotext_bbox(source_path)
    if not bbox_payload:
        return 0

    source_rows_by_page = _extract_source_rows_from_words(_parse_bbox_words(bbox_payload))
    if not source_rows_by_page:
        return 0

    total_changes = 0
    for item in content_list:
        source_rows = source_rows_by_page.get(int(item.get("page_idx", 0)))
        if not source_rows:
            continue

        for key in ("table_body", "text"):
            value = item.get(key)
            if not isinstance(value, str) or not _CHECKBOX_RE.search(value):
                continue
            updated, changes = _reconcile_text_value(value, source_rows)
            if changes:
                item[key] = updated
                total_changes += changes

        for key in ("list_items", "table_caption", "table_footnote"):
            value = item.get(key)
            if not isinstance(value, list):
                continue
            updated_parts: list[object] = []
            for part in value:
                if not isinstance(part, str) or not _CHECKBOX_RE.search(part):
                    updated_parts.append(part)
                    continue
                updated, changes = _reconcile_text_value(part, source_rows)
                updated_parts.append(updated)
                total_changes += changes
            if updated_parts != value:
                item[key] = updated_parts

    if total_changes:
        logger.info(
            "Reconciled {} checkbox states from PDF text layer for {}.",
            total_changes,
            source_path,
        )
    return total_changes
