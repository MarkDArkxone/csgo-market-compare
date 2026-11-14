from __future__ import annotations

import re
from dataclasses import dataclass

from shared.utils import extract_variant_fields

_SPECIAL_FINISH_PATTERNS: tuple[tuple[re.Pattern[str], str, str], ...] = (
    (
        re.compile(r"(?:\bgamma\s*doppler\b|伽玛多普勒|伽馬多普勒)", re.IGNORECASE),
        "Gamma Doppler",
        "finish_family",
    ),
    (
        re.compile(r"\bmarble\s*fade\b", re.IGNORECASE),
        "Marble Fade",
        "finish_family",
    ),
    (
        re.compile(r"\bcase\s*hardened\b", re.IGNORECASE),
        "Case Hardened",
        "finish_family",
    ),
    (
        re.compile(r"(?:\bdoppler\b|多普勒)", re.IGNORECASE),
        "Doppler",
        "finish_family",
    ),
    (
        re.compile(r"\bfade\b", re.IGNORECASE),
        "Fade",
        "finish_family",
    ),
)

_FLOAT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bfloat(?:\s*value)?\s*[:#]?\s*(0\.\d+)\b", re.IGNORECASE),
    re.compile(r"\bwear(?:\s*rating)?\s*[:#]?\s*(0\.\d+)\b", re.IGNORECASE),
)
_PATTERN_SEED_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bpattern\s*seed\s*[:#]?\s*(\d{1,5})\b", re.IGNORECASE),
    re.compile(r"\bseed\s*[:#]?\s*(\d{1,5})\b", re.IGNORECASE),
)
_PAINT_INDEX_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bpaint\s*index\s*[:#]?\s*(\d{1,5})\b", re.IGNORECASE),
    re.compile(r"\bfinish\s*catalog\s*[:#]?\s*(\d{1,5})\b", re.IGNORECASE),
)
_PRICE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?:price|listed|listing)\s*[:#]?\s*\$?\s*([0-9]+(?:\.[0-9]+)?)", re.IGNORECASE),
    re.compile(r"¥\s*([0-9]+(?:\.[0-9]+)?)", re.IGNORECASE),
)


@dataclass(frozen=True)
class SpecialItemSignal:
    is_special_candidate: bool
    special_variant: str | None
    special_variant_type: str | None
    matched_keyword: str | None


@dataclass(frozen=True)
class ParsedDetailMetrics:
    special_variant: str | None
    special_variant_type: str | None
    float_value: float | None
    pattern_seed: int | None
    paint_index: int | None
    detail_price: float | None
    sale_status: str | None


def detect_special_candidate(*texts: str | None) -> SpecialItemSignal:
    merged = " | ".join(str(text or "").strip() for text in texts if str(text or "").strip())
    if not merged:
        return SpecialItemSignal(False, None, None, None)

    variant_group, variant_label = extract_variant_fields(merged)
    if variant_label != "NA":
        return SpecialItemSignal(
            True,
            variant_label,
            _normalize_variant_type(variant_group),
            variant_label,
        )

    for pattern, variant, variant_type in _SPECIAL_FINISH_PATTERNS:
        if pattern.search(merged):
            return SpecialItemSignal(True, variant, variant_type, variant)
    return SpecialItemSignal(False, None, None, None)


def parse_special_detail_metrics(*texts: str | None) -> ParsedDetailMetrics:
    merged = "\n".join(str(text or "") for text in texts if text is not None)
    signal = detect_special_candidate(merged)
    lowered = merged.lower()
    return ParsedDetailMetrics(
        special_variant=signal.special_variant,
        special_variant_type=signal.special_variant_type,
        float_value=_extract_float(merged, _FLOAT_PATTERNS),
        pattern_seed=_extract_int(merged, _PATTERN_SEED_PATTERNS),
        paint_index=_extract_int(merged, _PAINT_INDEX_PATTERNS),
        detail_price=_extract_float(merged, _PRICE_PATTERNS),
        sale_status=_infer_sale_status(lowered),
    )


def compute_special_priority_score(
    *,
    special_variant_type: str | None,
    never_fetched: bool,
    price_change_ratio: float,
    has_opportunity: bool,
    current_price: float | None,
    markup_percent: float | None,
) -> float:
    score = 0.0
    variant_type = str(special_variant_type or "").strip().lower()
    if variant_type == "gem":
        score += 82.0
    elif variant_type == "phase":
        score += 74.0
    elif variant_type:
        score += 60.0

    if never_fetched:
        score += 22.0
    if has_opportunity:
        score += 35.0
    if price_change_ratio >= 0.15:
        score += 24.0
    elif price_change_ratio >= 0.08:
        score += 16.0
    elif price_change_ratio >= 0.03:
        score += 8.0

    if current_price is not None and current_price >= 200:
        score += 6.0
    if markup_percent is not None and markup_percent <= 5.0:
        score += 6.0
    return round(score, 4)


def _normalize_variant_type(value: str | None) -> str | None:
    key = str(value or "").strip().lower()
    if key in {"gem", "phase", "finish_family"}:
        return key
    return key or None


def _extract_float(text: str, patterns: tuple[re.Pattern[str], ...]) -> float | None:
    for pattern in patterns:
        match = pattern.search(text)
        if not match:
            continue
        try:
            return float(match.group(1))
        except (TypeError, ValueError):
            continue
    return None


def _extract_int(text: str, patterns: tuple[re.Pattern[str], ...]) -> int | None:
    for pattern in patterns:
        match = pattern.search(text)
        if not match:
            continue
        try:
            return int(match.group(1))
        except (TypeError, ValueError):
            continue
    return None


def _infer_sale_status(lowered_text: str) -> str | None:
    if not lowered_text:
        return None
    if any(token in lowered_text for token in ("sold", "not available", "unavailable", "withdrawn")):
        return "inactive"
    if any(token in lowered_text for token in ("available", "listed", "on sale", "for sale")):
        return "listed"
    return None
