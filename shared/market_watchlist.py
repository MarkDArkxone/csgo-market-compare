from __future__ import annotations

from dataclasses import dataclass
import json
import os
from functools import lru_cache
from pathlib import Path
import re
from urllib.parse import parse_qs, urlparse
import zipfile
import xml.etree.ElementTree as ET

from shared.utils import build_comparison_market_key


_XLSX_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_XLSX_REL_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_GOODS_ID_PRIMARY_COLUMNS = ("A", "C", "F", "H", "J", "L", "N", "P", "R", "T")
_WORD_NS = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
_DOCX_STAR_AFTER_TYPE = re.compile(r"^(?P<weapon>.+?)\s+[★☆]\s+\|\s+(?P<rest>.+)$")
_DOCX_ENGLISH_WEAR_SUFFIX = re.compile(
    r"\((Factory New|Minimal Wear|Field-Tested|Well-Worn|Battle-Scarred)\)$"
)
_DOCX_ENGLISH_PREFIX = re.compile(r"^[A-Za-z0-9'()./\- +★☆]+?\s+\|")
_SPECIAL_VARIANT_LOCALIZED_MAP = {
    "P1": "Phase 1",
    "P2": "Phase 2",
    "P3": "Phase 3",
    "P4": "Phase 4",
    "红宝石": "Ruby",
    "蓝宝石": "Sapphire",
    "黑珍珠": "Black Pearl",
    "绿宝石": "Emerald",
}
_WATCH_ALIAS_KEY_PATTERN = re.compile(r"[\s\-]+")
_WATCH_PREFIX_ALIASES = {
    "M4A1消音版": "M4A1 消音型",
    "M4A1消音版（StatTrak™）": "M4A1 消音型（StatTrak™）",
    "M4A1消音版（纪念品）": "M4A1 消音型（纪念品）",
}
_WATCH_BASE_ALIASES = {
    "AWP | 灵缇": "AWP | LongDog",
    "AWP | 一击碧命": "AWP | Green Energy",
    "M4A1 消音型 | 平流层巡航": "M4A1-S | Stratosphere",
    "M4A1消音版 | 平流层巡航": "M4A1-S | Stratosphere",
    "M4A4 | 炼狱之火": "M4A4 | Hellish",
    "专业手套（★） | 羽袭": "★ Specialist Gloves | Pillow Punchers",
    "运动手套（★） | 紫外狂潮": "★ Sport Gloves | Ultra Violent",
}
_WATCH_CN_WEAR_TO_EN = {
    "崭新出厂": "Factory New",
    "略有磨损": "Minimal Wear",
    "久经沙场": "Field-Tested",
    "破损不堪": "Well-Worn",
    "战痕累累": "Battle-Scarred",
}
_ITEM_MASTER_TEMPLATE_ID_HEADERS = (
    "悠悠有品templateId",
    "悠悠有品商品ID_templateId",
    "悠悠有品商品IDtemplateId",
    "商品ID_templateId",
    "templateId",
)
_ITEM_MASTER_ENGLISH_NAME_HEADERS = (
    "英文名_原始",
    "英文名原始",
    "english_name_raw",
)
_ITEM_MASTER_SEARCH_NAME_HEADERS = (
    "CSGORoll搜索词",
    "csgorollMarketName",
    "最终搜索词",
    "悠悠有品搜索名",
)
_ITEM_MASTER_WEAR_HEADERS = (
    "磨损英文",
    "磨损",
    "wear",
    "wearName",
)
_ITEM_MASTER_SPECIAL_HEADERS = (
    "特殊款英文",
    "特殊款",
    "特殊标注",
    "special",
)
_ITEM_MASTER_STATTRAK_HEADERS = (
    "是否StatTrak",
    "StatTrak",
)
_ITEM_MASTER_SOUVENIR_HEADERS = (
    "是否纪念品",
    "纪念品",
)
_ITEM_MASTER_HAS_YOUPIN_ID_HEADERS = (
    "是否有YoupinID",
    "是否有youpinid",
    "hasYoupinId",
)
_ITEM_MASTER_ROLL_FOUND_HEADERS = (
    "是否在Roll找到样例",
    "是否在roll找到样例",
    "hasRollSample",
)
_ITEM_MASTER_ROLL_KEY_HEADERS = (
    "CSGORoll价格系统主键",
    "CSGORoll搜索词",
    "csgorollMarketName",
    "最终搜索词",
    "悠悠有品搜索名",
)
_ITEM_MASTER_LINK_HEADERS = (
    "悠悠有品商品链接",
    "商品链接",
    "link",
    "url",
)


@dataclass(frozen=True)
class WatchlistTargets:
    names: list[str]
    template_ids: list[int]
    paired_template_ids: bool = False
    source_row_count: int = 0


def load_market_watchlist(path: Path | None) -> list[str]:
    return load_market_watch_targets(path).names


def load_market_watch_targets(path: Path | None) -> WatchlistTargets:
    file_path = Path(path) if path else None
    if file_path is None or not file_path.exists():
        return WatchlistTargets(names=[], template_ids=[], paired_template_ids=False)

    suffix = file_path.suffix.lower()
    template_ids: list[int] = []
    paired_template_ids = False
    source_row_count = 0
    if suffix == ".xlsx":
        names = _read_xlsx_watch_names(file_path)
        numeric_ids, paired_template_ids = _read_xlsx_numeric_ids_with_mode(file_path)
        source_row_count = _count_xlsx_item_master_effective_rows(file_path)
        if paired_template_ids:
            # Item-master style workbooks already provide authoritative names.
            # Keep every template id so monitor can prewarm Youpin prices directly.
            template_ids = numeric_ids
        else:
            resolved_names, resolved_ids = _resolve_template_ids_to_market_names(numeric_ids)
            template_ids = [item_id for item_id in numeric_ids if item_id not in resolved_ids]
            names = _merge_names(names, resolved_names)
    elif suffix == ".docx":
        names = _read_docx_watchlist(file_path)
    elif suffix == ".json":
        names = _read_json_list(file_path)
    else:
        names = _read_text_lines(file_path)

    out_names: list[str] = []
    seen: set[str] = set()
    for raw in names:
        value = str(raw or "").strip()
        if not value or value in seen:
            continue
        seen.add(value)
        out_names.append(value)

    out_ids: list[int] = []
    seen_ids: set[int] = set()
    for raw_id in template_ids:
        try:
            template_id = int(raw_id)
        except (TypeError, ValueError):
            continue
        if template_id < 1000 or template_id in seen_ids:
            continue
        seen_ids.add(template_id)
        out_ids.append(template_id)
    if source_row_count <= 0 and suffix == ".xlsx":
        source_row_count = max(len(out_names), len(out_ids))
    return WatchlistTargets(
        names=out_names,
        template_ids=out_ids,
        paired_template_ids=paired_template_ids,
        source_row_count=max(0, int(source_row_count)),
    )


def _count_xlsx_item_master_effective_rows(path: Path) -> int:
    with zipfile.ZipFile(path) as archive:
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        rels = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        rel_map = {rel.attrib["Id"]: rel.attrib["Target"] for rel in rels}
        shared_strings = _read_shared_strings(archive)
        sheets = workbook.find(f"{_XLSX_NS}sheets")
        if sheets is None:
            return 0
        for sheet in sheets:
            sheet_name = str(sheet.attrib.get("name") or "").strip()
            if sheet_name.casefold() != "item_master_full":
                continue
            rel_id = sheet.attrib.get(f"{_XLSX_REL_NS}id")
            if not rel_id:
                continue
            sheet_path = _resolve_sheet_path(rel_map.get(rel_id))
            if not sheet_path:
                continue
            row_cells_all = list(_iter_sheet_row_cells(archive, sheet_path, shared_strings))
            if not row_cells_all:
                return 0
            header_map = _build_header_map(row_cells_all[0])
            wear_col = _header_pick(header_map, _ITEM_MASTER_WEAR_HEADERS)
            roll_key_col = _header_pick(header_map, _ITEM_MASTER_ROLL_KEY_HEADERS)
            if not wear_col:
                return 0
            count = 0
            for row_cells in row_cells_all[1:]:
                by_col = {
                    _column_letters(ref): str(value or "").strip()
                    for ref, value in row_cells
                }
                if not str(by_col.get(wear_col, "") or "").strip():
                    continue
                if roll_key_col and not str(by_col.get(roll_key_col, "") or "").strip():
                    continue
                count += 1
            return count
    return 0


def _merge_names(*groups: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for group in groups:
        for raw in group:
            value = str(raw or "").strip()
            if not value or value in seen:
                continue
            seen.add(value)
            out.append(value)
    return out


def _read_text_lines(path: Path) -> list[str]:
    return [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _read_json_list(path: Path) -> list[str]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, list):
        return [str(item or "").strip() for item in payload if str(item or "").strip()]
    if isinstance(payload, dict):
        return [str(key or "").strip() for key in payload.keys() if str(key or "").strip()]
    return []


def _read_docx_watchlist(path: Path) -> list[str]:
    names: list[str] = []
    for text in _read_docx_paragraphs(path):
        if not _looks_like_docx_english_market_name(text):
            continue
        normalized = _normalize_docx_english_name(text)
        if normalized:
            names.append(normalized)
    return names


def _read_xlsx_watch_names(path: Path) -> list[str]:
    with zipfile.ZipFile(path) as archive:
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        rels = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        rel_map = {rel.attrib["Id"]: rel.attrib["Target"] for rel in rels}
        shared_strings = _read_shared_strings(archive)
        values: list[str] = []
        sheets = workbook.find(f"{_XLSX_NS}sheets")
        sheet_entries: list[tuple[str, str]] = []
        for sheet in sheets if sheets is not None else ():
            sheet_name = str(sheet.attrib.get("name") or "").strip()
            rel_id = sheet.attrib.get(f"{_XLSX_REL_NS}id")
            if not rel_id:
                continue
            sheet_path = _resolve_sheet_path(rel_map.get(rel_id))
            if not sheet_path:
                continue
            sheet_entries.append((sheet_name, sheet_path))
        preferred = [
            sheet_path
            for sheet_name, sheet_path in sheet_entries
            if sheet_name.casefold() == "item_master_full"
        ]
        target_paths = preferred or [sheet_path for _, sheet_path in sheet_entries]
        for sheet_path in target_paths:
            values.extend(_read_sheet_watch_names(archive, sheet_path, shared_strings))
        return values


def _read_xlsx_first_column(path: Path) -> list[str]:
    with zipfile.ZipFile(path) as archive:
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        rels = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        rel_map = {rel.attrib["Id"]: rel.attrib["Target"] for rel in rels}
        shared_strings = _read_shared_strings(archive)
        values: list[str] = []
        sheets = workbook.find(f"{_XLSX_NS}sheets")
        for sheet in sheets if sheets is not None else ():
            rel_id = sheet.attrib.get(f"{_XLSX_REL_NS}id")
            if not rel_id:
                continue
            sheet_path = _resolve_sheet_path(rel_map.get(rel_id))
            if not sheet_path:
                continue
            values.extend(_read_sheet_first_column(archive, sheet_path, shared_strings))
        return [
            value
            for value in values
            if _looks_like_market_name(value)
        ]


def _read_xlsx_numeric_ids(path: Path) -> list[int]:
    return _read_xlsx_numeric_ids_with_mode(path)[0]


def _read_xlsx_numeric_ids_with_mode(path: Path) -> tuple[list[int], bool]:
    with zipfile.ZipFile(path) as archive:
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        rels = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        rel_map = {rel.attrib["Id"]: rel.attrib["Target"] for rel in rels}
        shared_strings = _read_shared_strings(archive)
        resolved_ids: list[int] = []
        seen_ids: set[int] = set()
        paired_template_ids = False
        sheets = workbook.find(f"{_XLSX_NS}sheets")
        sheet_entries: list[tuple[str, str]] = []
        for sheet in sheets if sheets is not None else ():
            sheet_name = str(sheet.attrib.get("name") or "").strip()
            rel_id = sheet.attrib.get(f"{_XLSX_REL_NS}id")
            if not rel_id:
                continue
            sheet_path = _resolve_sheet_path(rel_map.get(rel_id))
            if not sheet_path:
                continue
            sheet_entries.append((sheet_name, sheet_path))
        preferred = [
            sheet_path
            for sheet_name, sheet_path in sheet_entries
            if sheet_name.casefold() == "item_master_full"
        ]
        target_paths = preferred or [sheet_path for _, sheet_path in sheet_entries]
        for sheet_path in target_paths:
            row_cells_all = list(_iter_sheet_row_cells(archive, sheet_path, shared_strings))
            if not row_cells_all:
                continue
            header_map = _build_header_map(row_cells_all[0])
            if _is_item_master_sheet(header_map):
                paired_template_ids = True
                template_col = _header_pick(header_map, _ITEM_MASTER_TEMPLATE_ID_HEADERS)
                link_col = _header_pick(header_map, _ITEM_MASTER_LINK_HEADERS)
                has_youpin_col = _header_pick(header_map, _ITEM_MASTER_HAS_YOUPIN_ID_HEADERS)
                roll_key_col = _header_pick(header_map, _ITEM_MASTER_ROLL_KEY_HEADERS)
                roll_found_col = _header_pick(header_map, _ITEM_MASTER_ROLL_FOUND_HEADERS)
                if not template_col and not link_col:
                    continue
                for row_cells in row_cells_all[1:]:
                    by_col = {
                        _column_letters(ref): str(value or "").strip()
                        for ref, value in row_cells
                    }
                    if not _is_item_master_target_row(
                        by_col,
                        template_col=template_col,
                        link_col=link_col,
                        has_youpin_col=has_youpin_col,
                        roll_key_col=roll_key_col,
                        roll_found_col=roll_found_col,
                    ):
                        continue
                    numeric = _resolve_item_master_template_id(
                        by_col,
                        template_col=template_col,
                        link_col=link_col,
                    )
                    if numeric is None:
                        continue
                    if numeric in seen_ids:
                        continue
                    seen_ids.add(numeric)
                    resolved_ids.append(numeric)
                continue
            for row_cells in row_cells_all:
                for ref, value in row_cells:
                    if _column_letters(ref) not in _GOODS_ID_PRIMARY_COLUMNS:
                        continue
                    text = str(value or "").strip()
                    if not text.isdigit():
                        continue
                    numeric = int(text)
                    if numeric < 1000 or numeric in seen_ids:
                        continue
                    seen_ids.add(numeric)
                    resolved_ids.append(numeric)
        return resolved_ids, paired_template_ids


def _read_xlsx_goods_id_watchlist(path: Path) -> list[str]:
    resolved_names, _ = _resolve_template_ids_to_market_names(_read_xlsx_numeric_ids(path))
    return resolved_names


def _normalize_watch_variant_label(raw_variant: str) -> str:
    value = str(raw_variant or "").strip()
    if not value:
        return ""
    compact = value.upper().replace(" ", "")
    if compact in _SPECIAL_VARIANT_LOCALIZED_MAP:
        return _SPECIAL_VARIANT_LOCALIZED_MAP[compact]
    return _SPECIAL_VARIANT_LOCALIZED_MAP.get(value, value)


def _normalize_header_key(raw_header: str) -> str:
    text = str(raw_header or "").strip()
    if not text:
        return ""
    text = text.replace(" ", "")
    text = text.replace("_", "")
    return text.casefold()


def _build_header_map(cells: list[tuple[str, str]]) -> dict[str, str]:
    out: dict[str, str] = {}
    for ref, value in cells:
        key = _normalize_header_key(value)
        if not key:
            continue
        out[key] = _column_letters(ref)
    return out


def _header_pick(header_map: dict[str, str], aliases: tuple[str, ...]) -> str | None:
    for alias in aliases:
        key = _normalize_header_key(alias)
        if key and key in header_map:
            return header_map[key]
    return None


def _is_item_master_sheet(header_map: dict[str, str]) -> bool:
    has_search = _header_pick(header_map, _ITEM_MASTER_SEARCH_NAME_HEADERS) is not None
    has_wear = _header_pick(header_map, _ITEM_MASTER_WEAR_HEADERS) is not None
    has_template = _header_pick(header_map, _ITEM_MASTER_TEMPLATE_ID_HEADERS) is not None
    has_link = _header_pick(header_map, _ITEM_MASTER_LINK_HEADERS) is not None
    return (has_search and has_wear) or (has_search and (has_template or has_link))


def _is_truthy_text(raw_value: str) -> bool:
    value = str(raw_value or "").strip().casefold()
    if not value:
        return False
    return value in {"1", "true", "yes", "y", "是"}


def _is_item_master_target_row(
    by_col: dict[str, str],
    *,
    template_col: str | None,
    link_col: str | None,
    has_youpin_col: str | None,
    roll_key_col: str | None,
    roll_found_col: str | None,
) -> bool:
    template_id = _resolve_item_master_template_id(
        by_col,
        template_col=template_col,
        link_col=link_col,
    )
    if template_id is None:
        return False

    if has_youpin_col:
        has_youpin_value = str(by_col.get(has_youpin_col, "") or "").strip()
        if has_youpin_value and not _is_truthy_text(has_youpin_value):
            return False

    if roll_key_col:
        roll_key_value = str(by_col.get(roll_key_col, "") or "").strip()
        if not roll_key_value:
            return False

    if roll_found_col:
        roll_found_value = str(by_col.get(roll_found_col, "") or "").strip()
        if roll_found_value and not _is_truthy_text(roll_found_value):
            return False

    return True


def _extract_template_id_from_link(raw_link: str) -> int | None:
    link = str(raw_link or "").strip()
    if not link:
        return None
    try:
        parsed = urlparse(link)
        query = parse_qs(parsed.query or "")
        values = query.get("templateId") or query.get("templateid") or []
        for value in values:
            text = str(value or "").strip()
            if text.isdigit():
                numeric = int(text)
                if numeric >= 1000:
                    return numeric
    except Exception:
        pass
    match = re.search(r"(?:templateId|templateid)\s*=\s*(\d{4,})", link)
    if match:
        return int(match.group(1))
    return None


def _resolve_item_master_template_id(
    by_col: dict[str, str],
    *,
    template_col: str | None,
    link_col: str | None,
) -> int | None:
    if template_col:
        template_value = str(by_col.get(template_col, "") or "").strip()
        if template_value.isdigit():
            numeric = int(template_value)
            if numeric >= 1000:
                return numeric
    if link_col:
        return _extract_template_id_from_link(by_col.get(link_col, ""))
    return None


def _normalize_item_master_english_name(raw_name: str) -> str:
    name = str(raw_name or "").strip()
    if not name:
        return ""
    # e.g. "Sport Gloves (★) | Hedge Maze (Factory New)" -> "★ Sport Gloves | Hedge Maze (Factory New)"
    match = re.match(r"^(?P<left>.+?)\s*[\(（]\s*[★☆]\s*[\)）]\s*(?P<right>\|.+)$", name)
    if not match:
        return name
    return f"★ {match.group('left').strip()} {match.group('right').strip()}".strip()


def _build_item_master_market_name(
    *,
    english_name: str,
    search_name: str,
    wear_name: str,
    stattrak: bool,
    souvenir: bool,
) -> str:
    normalized_english = _normalize_item_master_english_name(english_name)
    if normalized_english:
        return normalized_english

    source = str(search_name or "").strip()
    if not source:
        return ""
    wear = str(wear_name or "").strip()
    market_name = f"{source} ({wear})".strip() if wear else source
    if stattrak and "stattrak" not in market_name.casefold():
        market_name = f"StatTrak™ {market_name}"
    if souvenir and "souvenir" not in market_name.casefold():
        market_name = f"Souvenir {market_name}"
    return market_name


def _resolve_watch_market_name(raw_name: str) -> str:
    value = str(raw_name or "").strip()
    if not value:
        return ""
    aliases = _load_market_name_aliases(str(_default_steam_mapping_path()))
    def resolve_alias(candidate: str) -> str:
        text = str(candidate or "").strip()
        if not text:
            return ""
        return str(
            aliases.get(text)
            or aliases.get(_normalize_watch_alias_key(text))
            or text
        ).strip()

    normalized_value = _normalize_watch_name_for_lookup(value)
    resolved = resolve_alias(value)
    if resolved == value and normalized_value != value:
        resolved = resolve_alias(normalized_value)
    if not resolved and normalized_value != value:
        resolved = resolve_alias(_apply_watch_base_alias(normalized_value))
    if not resolved or resolved == value:
        resolved = resolve_alias(_apply_watch_base_alias(value))
    return str(resolved or value).strip()


def _sheet_looks_like_goods_id_sheet(rows: list[list[str]]) -> bool:
    for row in rows[:3]:
        for value in row:
            if str(value or "").strip().lower() == "goods_id":
                return True
    return False


def _normalize_watch_alias_key(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    text = text.replace("（", "(").replace("）", ")")
    text = text.replace("｜", "|").replace("　", " ")
    return _WATCH_ALIAS_KEY_PATTERN.sub("", text).casefold()


def _normalize_watch_name_for_lookup(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    for src, dst in _WATCH_PREFIX_ALIASES.items():
        if text.startswith(src):
            return dst + text[len(src):]
    return text


def _apply_watch_base_alias(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    wear_match = re.search(r"\s*\([^()]+\)\s*$", text)
    wear_suffix = wear_match.group(0) if wear_match else ""
    base_name = text[: wear_match.start()].strip() if wear_match else text
    aliased_base = _WATCH_BASE_ALIASES.get(base_name)
    if aliased_base:
        return f"{aliased_base}{_normalize_watch_wear_suffix(wear_suffix)}".strip()
    return text


def _normalize_watch_wear_suffix(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    match = re.fullmatch(r"\(([^()]+)\)", text)
    if not match:
        return value
    wear = match.group(1).strip()
    normalized = _WATCH_CN_WEAR_TO_EN.get(wear)
    if not normalized:
        return value
    return f" ({normalized})"


def _looks_like_market_name(value: str) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    if text.lower().startswith("goods_id"):
        return False
    return "|" in text or "(" in text or "（" in text


def _read_docx_paragraphs(path: Path) -> list[str]:
    with zipfile.ZipFile(path) as archive:
        root = ET.fromstring(archive.read("word/document.xml"))
    paragraphs: list[str] = []
    for para in root.findall(".//w:p", _WORD_NS):
        texts = [node.text or "" for node in para.findall(".//w:t", _WORD_NS)]
        text = "".join(texts).strip()
        if text:
            paragraphs.append(text)
    return paragraphs


def _normalize_docx_english_name(english_name: str) -> str:
    text = str(english_name or "").strip()
    if not text:
        return ""
    match = _DOCX_STAR_AFTER_TYPE.match(text)
    if match:
        weapon = match.group("weapon").strip()
        rest = match.group("rest").strip()
        return f"★ {weapon} | {rest}"
    return text


def _looks_like_docx_english_market_name(value: str) -> bool:
    text = str(value or "").strip()
    if "|" not in text or not _DOCX_ENGLISH_WEAR_SUFFIX.search(text):
        return False
    return _DOCX_ENGLISH_PREFIX.search(text) is not None


def _read_shared_strings(archive: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in archive.namelist():
        return []
    root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
    out: list[str] = []
    for item in root:
        out.append("".join(text.text or "" for text in item.iter(f"{_XLSX_NS}t")))
    return out


def _read_sheet_first_column(
    archive: zipfile.ZipFile,
    sheet_path: str,
    shared_strings: list[str],
) -> list[str]:
    out: list[str] = []
    for row_index, row_values in enumerate(
        _iter_sheet_rows(archive, sheet_path, shared_strings),
        start=1,
    ):
        if row_index == 1 and any(value.lower() == "goods_id" for value in row_values):
            return []
        first_value = ""
        for value in row_values:
            if value:
                first_value = value
                break
        if first_value:
            out.append(first_value)
    return out


def _read_sheet_watch_names(
    archive: zipfile.ZipFile,
    sheet_path: str,
    shared_strings: list[str],
) -> list[str]:
    row_cells = list(_iter_sheet_row_cells(archive, sheet_path, shared_strings))
    if not row_cells:
        return []
    preview_rows = [
        [str(value or "").strip() for _, value in cells]
        for cells in row_cells[:3]
    ]
    if _sheet_looks_like_goods_id_sheet(preview_rows):
        return []
    header_map = _build_header_map(row_cells[0])
    if _is_item_master_sheet(header_map):
        out: list[str] = []
        english_col = _header_pick(header_map, _ITEM_MASTER_ENGLISH_NAME_HEADERS)
        search_col = _header_pick(header_map, _ITEM_MASTER_SEARCH_NAME_HEADERS)
        wear_col = _header_pick(header_map, _ITEM_MASTER_WEAR_HEADERS)
        special_col = _header_pick(header_map, _ITEM_MASTER_SPECIAL_HEADERS)
        stattrak_col = _header_pick(header_map, _ITEM_MASTER_STATTRAK_HEADERS)
        souvenir_col = _header_pick(header_map, _ITEM_MASTER_SOUVENIR_HEADERS)
        template_col = _header_pick(header_map, _ITEM_MASTER_TEMPLATE_ID_HEADERS)
        link_col = _header_pick(header_map, _ITEM_MASTER_LINK_HEADERS)
        has_youpin_col = _header_pick(header_map, _ITEM_MASTER_HAS_YOUPIN_ID_HEADERS)
        roll_key_col = _header_pick(header_map, _ITEM_MASTER_ROLL_KEY_HEADERS)
        roll_found_col = _header_pick(header_map, _ITEM_MASTER_ROLL_FOUND_HEADERS)
        for cells in row_cells[1:]:
            by_col = {
                _column_letters(ref): str(value or "").strip()
                for ref, value in cells
            }
            if not _is_item_master_target_row(
                by_col,
                template_col=template_col,
                link_col=link_col,
                has_youpin_col=has_youpin_col,
                roll_key_col=roll_key_col,
                roll_found_col=roll_found_col,
            ):
                continue
            market_name = _build_item_master_market_name(
                english_name=by_col.get(english_col or "", ""),
                search_name=by_col.get(search_col or "", ""),
                wear_name=by_col.get(wear_col or "", ""),
                stattrak=_is_truthy_text(by_col.get(stattrak_col or "", "")),
                souvenir=_is_truthy_text(by_col.get(souvenir_col or "", "")),
            )
            normalized_name = _resolve_watch_market_name(market_name)
            if not normalized_name:
                continue
            special_variant = _normalize_watch_variant_label(by_col.get(special_col or "", ""))
            if special_variant:
                out.append(build_comparison_market_key(normalized_name, special_variant))
            else:
                out.append(normalized_name)
        return out

    out: list[str] = []
    for cells in row_cells:
        by_col = {
            _column_letters(ref): str(value or "").strip()
            for ref, value in cells
        }
        name = str(by_col.get("A") or "").strip()
        if not name:
            continue
        normalized_name = _resolve_watch_market_name(name)
        if not normalized_name:
            continue
        variant_value = str(by_col.get("B") or "").strip()
        normalized_variant = _normalize_watch_variant_label(variant_value)
        if normalized_variant:
            out.append(build_comparison_market_key(normalized_name, normalized_variant))
        else:
            out.append(normalized_name)
    return out


def _iter_sheet_rows(
    archive: zipfile.ZipFile,
    sheet_path: str,
    shared_strings: list[str],
):
    root = ET.fromstring(archive.read(sheet_path))
    for row in root.iter(f"{_XLSX_NS}row"):
        row_values: list[str] = []
        for _, value in _iter_row_cell_values(row, shared_strings):
            row_values.append(value)
        if row_values:
            yield row_values


def _iter_sheet_row_cells(
    archive: zipfile.ZipFile,
    sheet_path: str,
    shared_strings: list[str],
):
    root = ET.fromstring(archive.read(sheet_path))
    for row in root.iter(f"{_XLSX_NS}row"):
        row_cells = list(_iter_row_cell_values(row, shared_strings))
        if row_cells:
            yield row_cells


def _iter_row_cell_values(row: ET.Element, shared_strings: list[str]):
    for cell in row.iter(f"{_XLSX_NS}c"):
        ref = str(cell.attrib.get("r") or "").strip()
        cell_type = cell.attrib.get("t")
        value_node = cell.find(f"{_XLSX_NS}v")
        if cell_type == "inlineStr":
            inline_text = "".join(
                text.text or "" for text in cell.iter(f"{_XLSX_NS}t")
            )
            value = inline_text
        else:
            value = value_node.text if value_node is not None else ""
            if cell_type == "s" and value:
                value = shared_strings[int(value)]
        yield ref, str(value or "").strip()


def _column_letters(cell_ref: str) -> str:
    out: list[str] = []
    for char in str(cell_ref or "").strip():
        if char.isalpha():
            out.append(char.upper())
        else:
            break
    return "".join(out)


def _resolve_sheet_path(target: str | None) -> str | None:
    text = str(target or "").strip()
    if not text:
        return None
    text = text.lstrip("/")
    if text.startswith("xl/"):
        return text
    return "xl/" + text


def _default_uuyp_mapping_path() -> Path:
    project_root = Path(
        os.getenv("PROJECT_ROOT", str(Path(__file__).resolve().parent.parent))
    ).resolve()
    return project_root / "data" / "mappings" / "uuyp_730.json"


def _default_steam_mapping_path() -> Path:
    project_root = Path(
        os.getenv("PROJECT_ROOT", str(Path(__file__).resolve().parent.parent))
    ).resolve()
    return project_root / "data" / "mappings" / "steam_730.json"


def _resolve_template_ids_to_market_names(
    template_ids: list[int],
) -> tuple[list[str], set[int]]:
    reverse = _load_reverse_uuyp_mapping(str(_default_uuyp_mapping_path()))
    names: list[str] = []
    resolved_ids: set[int] = set()
    for item_id in template_ids:
        if item_id not in reverse:
            continue
        resolved_ids.add(item_id)
        names.append(reverse[item_id])
    return names, resolved_ids


@lru_cache(maxsize=8)
def _load_reverse_uuyp_mapping(mapping_path: str) -> dict[int, str]:
    path = Path(mapping_path)
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    out: dict[int, str] = {}
    if not isinstance(payload, dict):
        return out
    for market_name, raw_id in payload.items():
        try:
            item_id = int(raw_id)
        except (TypeError, ValueError):
            continue
        name = str(market_name or "").strip()
        if item_id < 1000 or not name:
            continue
        out.setdefault(item_id, name)
    return out


@lru_cache(maxsize=8)
def _load_market_name_aliases(mapping_path: str) -> dict[str, str]:
    path = Path(mapping_path)
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    out: dict[str, str] = {}
    if not isinstance(payload, dict):
        return out
    for market_name, raw in payload.items():
        if not isinstance(raw, dict):
            continue
        market_value = str(market_name or "").strip()
        cn_name = str(raw.get("cn_name") or "").strip()
        en_name = str(raw.get("en_name") or market_value).strip()
        if not market_value:
            continue
        out.setdefault(market_value, market_value)
        normalized_market = _normalize_watch_alias_key(market_value)
        if normalized_market:
            out.setdefault(normalized_market, market_value)
        if en_name:
            out.setdefault(en_name, market_value)
            normalized_en = _normalize_watch_alias_key(en_name)
            if normalized_en:
                out.setdefault(normalized_en, market_value)
        if cn_name:
            out.setdefault(cn_name, market_value)
            normalized_cn = _normalize_watch_alias_key(cn_name)
            if normalized_cn:
                out.setdefault(normalized_cn, market_value)
    return out
