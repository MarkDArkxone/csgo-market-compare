from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
import re
import zipfile
import xml.etree.ElementTree as ET

import httpx

from config.settings import Settings
from shared.utils import CODE_TO_CN_DISPLAY, normalize_name_identity

logger = logging.getLogger(__name__)
_WORD_NS = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
_DOCX_STAR_AFTER_TYPE = re.compile(r"^(?P<weapon>.+?)\s+[★☆]\s+\|\s+(?P<rest>.+)$")

_DOCX_STAR_AFTER_TYPE = re.compile(r"^(?P<weapon>.+?)\s+\(?[★☆]\)?\s+\|\s+(?P<rest>.+)$")
_DOCX_STAR_TOKEN_PATTERN = re.compile(r"[★☆]|[（(]\s*[★☆]\s*[）)]")
_DOCX_MULTI_SPACE_PATTERN = re.compile(r"\s+")


@dataclass(frozen=True)
class MappingEntry:
    market_name: str
    youpin_template_id: int
    en_name: str
    cn_name: str | None


@dataclass(frozen=True)
class MappingMatch:
    entry: MappingEntry | None
    match_level: str
    confidence: float


class NameMapper:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._loaded = False
        self._lock = asyncio.Lock()
        self._entry_count = 0
        self._by_market_name: dict[str, MappingEntry] = {}
        self._by_base_wear_variant: dict[tuple[str, str, str], MappingEntry] = {}
        self._by_base_wear: dict[tuple[str, str], MappingEntry] = {}
        self._by_base: dict[str, MappingEntry] = {}

    async def ensure_loaded(self) -> None:
        if self._loaded:
            return

        async with self._lock:
            if self._loaded:
                return
            await asyncio.to_thread(self._load_sync)
            self._loaded = True

    def get_entry(
        self,
        market_name: str,
        *,
        base_name: str | None = None,
        wear_code: str | None = None,
        variant_label: str | None = None,
    ) -> MappingEntry | None:
        return self.get_entry_with_trace(
            market_name,
            base_name=base_name,
            wear_code=wear_code,
            variant_label=variant_label,
        ).entry

    def get_entry_with_trace(
        self,
        market_name: str,
        *,
        base_name: str | None = None,
        wear_code: str | None = None,
        variant_label: str | None = None,
    ) -> MappingMatch:
        market = (market_name or "").strip()
        if market in self._by_market_name:
            return MappingMatch(self._by_market_name[market], "exact_market_name", 1.0)

        identity = normalize_name_identity(market_name, variant_hint=variant_label)
        if market:
            base = identity.base_name.strip().lower()
            wear = identity.wear_code.strip().upper()
            variant = identity.variant_label.strip()
        else:
            base = (base_name or identity.base_name).strip().lower()
            wear = (wear_code or identity.wear_code or "NA").strip().upper()
            variant = (variant_label or identity.variant_label or "NA").strip()

        # Priority 1: base_name + wear_code + variant_label
        key1 = (base, wear, variant)
        if key1 in self._by_base_wear_variant:
            return MappingMatch(self._by_base_wear_variant[key1], "base+wear+variant", 0.95)

        # Special variants must not fall back to generic Doppler/Gamma Doppler mappings.
        if variant and variant != "NA":
            return MappingMatch(None, "missing_variant_mapping", 0.0)

        # Priority 2: base_name + wear_code
        key2 = (base, wear)
        if key2 in self._by_base_wear:
            return MappingMatch(self._by_base_wear[key2], "base+wear", 0.85)

        # Priority 3: base_name
        entry = self._by_base.get(base)
        if entry:
            return MappingMatch(entry, "base_only", 0.70)
        return MappingMatch(None, "missing", 0.0)

    def get_variant_parent_entry(
        self,
        market_name: str,
        *,
        base_name: str | None = None,
        wear_code: str | None = None,
        variant_label: str | None = None,
    ) -> MappingMatch:
        """
        Resolve the parent Youpin template for special variants.

        Example:
        "★ Butterfly Knife | Gamma Doppler Phase 2 (Factory New)"
        -> generic parent
        "★ Butterfly Knife | Gamma Doppler (Factory New)"

        This is only for data-source querying and must not be treated as
        a direct special-variant mapping.
        """
        identity = normalize_name_identity(market_name, variant_hint=variant_label)
        market = (market_name or "").strip()
        if market:
            base = identity.base_name.strip().lower()
            wear = identity.wear_code.strip().upper()
            variant = identity.variant_label.strip()
        else:
            base = (base_name or identity.base_name).strip().lower()
            wear = (wear_code or identity.wear_code or "NA").strip().upper()
            variant = (variant_label or identity.variant_label or "NA").strip()

        candidate_bases: list[str] = [base]
        if variant in {"Ruby", "Sapphire", "Black Pearl"}:
            candidate_bases.extend(
                self._build_parent_base_candidates(
                    base,
                    english_suffix="doppler",
                    chinese_suffix="多普勒",
                )
            )
        elif variant == "Emerald":
            candidate_bases.extend(
                self._build_parent_base_candidates(
                    base,
                    english_suffix="gamma doppler",
                    chinese_suffix="伽玛多普勒",
                )
            )

        seen: set[str] = set()
        deduped_bases: list[str] = []
        for candidate in candidate_bases:
            key = candidate.strip().lower()
            if not key or key in seen:
                continue
            seen.add(key)
            deduped_bases.append(key)

        for candidate in deduped_bases:
            key = (candidate, wear)
            entry = self._by_base_wear.get(key)
            if entry:
                return MappingMatch(entry, "base+wear_parent", 0.90)

        for candidate in deduped_bases:
            entry = self._by_base.get(candidate)
            if entry:
                return MappingMatch(entry, "base_parent", 0.60)

        return MappingMatch(None, "missing_parent_mapping", 0.0)

    @staticmethod
    def _build_parent_base_candidates(
        base: str,
        *,
        english_suffix: str,
        chinese_suffix: str,
    ) -> list[str]:
        text = str(base or "").strip()
        if not text:
            return []

        lowered = text.lower()
        out: list[str] = []
        suffix_pairs = (
            (english_suffix, english_suffix.lower()),
            (chinese_suffix, chinese_suffix.lower()),
        )
        for suffix, lowered_suffix in suffix_pairs:
            if lowered_suffix in lowered:
                continue
            if text.endswith("|"):
                out.append(f"{text} {suffix}")
            else:
                out.append(f"{text} | {suffix}")
        return out

    def stats(self) -> dict:
        return {"loaded": self._loaded, "count": self._entry_count}

    def register_entry(
        self,
        market_name: str,
        template_id: int,
        *,
        en_name: str | None = None,
        cn_name: str | None = None,
    ) -> MappingEntry | None:
        market = str(market_name or "").strip()
        if not market:
            return None
        try:
            youpin_template_id = int(template_id)
        except (TypeError, ValueError):
            return None
        if youpin_template_id <= 0:
            return None

        existing = self._by_market_name.get(market)
        if existing:
            return existing

        entry = MappingEntry(
            market_name=market,
            youpin_template_id=youpin_template_id,
            en_name=str(en_name or market).strip() or market,
            cn_name=str(cn_name or "").strip() or None,
        )
        if self._index_entry(entry, replace=False):
            self._entry_count += 1
        return entry

    def _index_entry(self, entry: MappingEntry, *, replace: bool) -> bool:
        exact_added = entry.market_name not in self._by_market_name
        for lookup_name in self._iter_lookup_names(entry):
            self._index_lookup_name(lookup_name, entry, replace=replace)
        return exact_added

    def _index_lookup_name(self, lookup_name: str, entry: MappingEntry, *, replace: bool) -> None:
        key_name = str(lookup_name or "").strip()
        if not key_name:
            return

        if replace or key_name not in self._by_market_name:
            self._by_market_name[key_name] = entry

        ident = normalize_name_identity(key_name)
        base_key = ident.base_name.strip().lower()
        wear_key = ident.wear_code.strip().upper()
        variant_key = ident.variant_label.strip()

        if not base_key:
            return

        key1 = (base_key, wear_key, variant_key)
        if replace or key1 not in self._by_base_wear_variant:
            self._by_base_wear_variant[key1] = entry

        key2 = (base_key, wear_key)
        if replace or key2 not in self._by_base_wear:
            self._by_base_wear[key2] = entry

        if replace or base_key not in self._by_base:
            self._by_base[base_key] = entry

    def _load_sync(self) -> None:
        uuyp_path = self.settings.mapper_uuyp_file
        steam_path = self.settings.mapper_steam_file

        if self.settings.mapper_auto_download:
            try:
                self._download_if_needed(uuyp_path, self.settings.mapper_uuyp_url)
            except Exception as exc:  # noqa: BLE001
                logger.warning("UUYP mapping download failed: %s", exc)
            try:
                self._download_if_needed(steam_path, self.settings.mapper_steam_url)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Steam mapping download failed: %s", exc)

        if not uuyp_path.exists():
            logger.warning(
                "NameMapper degraded: no local UUYP mapping cache found at %s",
                uuyp_path,
            )
            self._entry_count = 0
            self._by_market_name = {}
            self._by_base_wear_variant = {}
            self._by_base_wear = {}
            self._by_base = {}
            return

        try:
            uuyp_data = self._read_json(uuyp_path)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "NameMapper degraded: local UUYP mapping cache unreadable (%s): %s",
                uuyp_path,
                exc,
            )
            self._entry_count = 0
            self._by_market_name = {}
            self._by_base_wear_variant = {}
            self._by_base_wear = {}
            self._by_base = {}
            return

        steam_data = {}
        if steam_path.exists():
            try:
                steam_data = self._read_json(steam_path)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Steam mapping cache unreadable (%s), continue with EN-only names: %s",
                    steam_path,
                    exc,
                )

        self._by_market_name = {}
        self._by_base_wear_variant = {}
        self._by_base_wear = {}
        self._by_base = {}
        entry_count = 0
        for market_name, raw_id in uuyp_data.items():
            try:
                template_id = int(raw_id)
            except (TypeError, ValueError):
                continue

            if template_id <= 0:
                continue

            steam_info = steam_data.get(market_name, {})
            entry = MappingEntry(
                market_name=market_name,
                youpin_template_id=template_id,
                en_name=str(steam_info.get("en_name") or market_name),
                cn_name=(
                    str(steam_info["cn_name"])
                    if isinstance(steam_info, dict) and steam_info.get("cn_name")
                    else None
                ),
            )
            if self._index_entry(entry, replace=False):
                entry_count += 1

        self._load_docx_aliases()

        self._entry_count = entry_count
        logger.info("NameMapper loaded %d mappings", entry_count)

    @staticmethod
    def _read_json(path: Path) -> dict:
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)

    def _iter_lookup_names(self, entry: MappingEntry) -> list[str]:
        names: list[str] = []
        for item in (
            entry.market_name,
            entry.en_name,
            entry.cn_name,
            *self._build_alias_names(entry),
        ):
            text = str(item or "").strip()
            if text and text not in names:
                names.append(text)
        return names

    def _build_alias_names(self, entry: MappingEntry) -> list[str]:
        ident = normalize_name_identity(entry.en_name)
        base_name = ident.base_name.strip()
        wear_code = ident.wear_code.strip().upper()
        zh_wear = CODE_TO_CN_DISPLAY.get(wear_code)
        if not base_name or not zh_wear:
            return []

        alias_bases = {
            "AWP | LongDog": ["AWP | 灵缇"],
            "AWP | Green Energy": ["AWP | 一击碧命"],
            "M4A1-S | Stratosphere": [
                "M4A1 消音型 | 平流层巡航",
                "M4A1消音版 | 平流层巡航",
            ],
            "M4A4 | Hellish": ["M4A4 | 炼狱之火"],
            "Specialist Gloves | Pillow Punchers": ["专业手套（★） | 羽袭"],
            "Sport Gloves | Ultra Violent": ["运动手套（★） | 紫外狂潮"],
        }.get(base_name, [])
        if not alias_bases:
            return []

        if wear_code == "NA":
            return alias_bases
        return [f"{alias} ({zh_wear})" for alias in alias_bases]

    def _load_docx_aliases(self) -> None:
        docx_path = getattr(self.settings, "mapper_docx_file", None)
        if not docx_path or not Path(docx_path).exists():
            return
        try:
            alias_pairs = self._read_docx_alias_pairs(Path(docx_path))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Mapper docx alias file unreadable (%s): %s", docx_path, exc)
            return

        alias_count = 0
        for cn_name, en_name in alias_pairs:
            entry = self._resolve_docx_entry(en_name)
            if entry is None:
                continue
            before = self._by_market_name.get(cn_name)
            self._index_lookup_name(cn_name, entry, replace=False)
            if before is None and self._by_market_name.get(cn_name) is entry:
                alias_count += 1
        if alias_count:
            logger.info("NameMapper loaded %d aliases from %s", alias_count, docx_path)

    def _resolve_docx_entry(self, english_name: str) -> MappingEntry | None:
        candidates = [self._normalize_docx_english_name(english_name), english_name.strip()]
        seen: set[str] = set()
        for candidate in candidates:
            key = str(candidate or "").strip()
            if not key or key in seen:
                continue
            seen.add(key)
            match = self.get_entry_with_trace(key)
            if match.entry is not None:
                return match.entry
        return None

    @staticmethod
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

    @staticmethod
    def _normalize_docx_english_name(english_name: str) -> str:
        text = str(english_name or "").strip()
        if not text:
            return ""
        if "|" in text:
            left, right = text.split("|", 1)
            left_clean = _DOCX_MULTI_SPACE_PATTERN.sub(
                " ",
                _DOCX_STAR_TOKEN_PATTERN.sub(" ", left),
            ).strip()
            right_clean = _DOCX_MULTI_SPACE_PATTERN.sub(" ", right).strip()
            if left_clean and right_clean:
                return f"{left_clean} | {right_clean}"
        match = _DOCX_STAR_AFTER_TYPE.match(text)
        if match:
            weapon = match.group("weapon").strip()
            rest = match.group("rest").strip()
            return f"{weapon} | {rest}"
        return text

    @staticmethod
    def _read_docx_alias_pairs(path: Path) -> list[tuple[str, str]]:
        with zipfile.ZipFile(path) as archive:
            root = ET.fromstring(archive.read("word/document.xml"))
        paragraphs: list[str] = []
        for para in root.findall(".//w:p", _WORD_NS):
            texts = [node.text or "" for node in para.findall(".//w:t", _WORD_NS)]
            text = "".join(texts).strip()
            if text:
                paragraphs.append(text)

        out: list[tuple[str, str]] = []
        pending_cn: str | None = None
        for text in paragraphs:
            if " | " not in text or "(" not in text or ")" not in text:
                continue
            if pending_cn is None:
                pending_cn = text
                continue
            out.append((pending_cn, text))
            pending_cn = None
        return out

    def _download_if_needed(self, path: Path, url: str) -> None:
        refresh_seconds = max(0, int(self.settings.mapper_refresh_seconds))
        if path.exists() and refresh_seconds > 0:
            age_seconds = max(0.0, time.time() - path.stat().st_mtime)
            if age_seconds < refresh_seconds:
                return
        elif path.exists():
            return

        path.parent.mkdir(parents=True, exist_ok=True)
        logger.info("Downloading mapping file: %s", url)
        with httpx.Client(timeout=30.0, follow_redirects=True) as client:
            response = client.get(url)
            response.raise_for_status()
            tmp_path = path.with_suffix(f"{path.suffix}.tmp")
            tmp_path.write_bytes(response.content)
            tmp_path.replace(path)
