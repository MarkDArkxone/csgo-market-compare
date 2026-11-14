from __future__ import annotations

import re

WEAR_CN_TO_EN = {
    "崭新出厂": "Factory New",
    "略有磨损": "Minimal Wear",
    "久经沙场": "Field-Tested",
    "破损不堪": "Well-Worn",
    "战痕累累": "Battle-Scarred",
}

WEAR_ABBR_TO_EN = {
    "FN": "Factory New",
    "MW": "Minimal Wear",
    "FT": "Field-Tested",
    "WW": "Well-Worn",
    "BS": "Battle-Scarred",
}

SPECIAL_VARIANT_MAP = {
    "P1": "Phase 1",
    "PHASE 1": "Phase 1",
    "阶段1": "Phase 1",
    "一阶段": "Phase 1",
    "P2": "Phase 2",
    "PHASE 2": "Phase 2",
    "阶段2": "Phase 2",
    "二阶段": "Phase 2",
    "P3": "Phase 3",
    "PHASE 3": "Phase 3",
    "阶段3": "Phase 3",
    "三阶段": "Phase 3",
    "P4": "Phase 4",
    "PHASE 4": "Phase 4",
    "阶段4": "Phase 4",
    "四阶段": "Phase 4",
    "RUBY": "Ruby",
    "红宝石": "Ruby",
    "SAPPHIRE": "Sapphire",
    "蓝宝石": "Sapphire",
    "BLACK PEARL": "Black Pearl",
    "黑珍珠": "Black Pearl",
    "EMERALD": "Emerald",
    "绿宝石": "Emerald",
}


def normalize_wear(value: str | None) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    up = text.upper()
    if up in WEAR_ABBR_TO_EN:
        return WEAR_ABBR_TO_EN[up]
    if text in WEAR_CN_TO_EN:
        return WEAR_CN_TO_EN[text]
    return text


def normalize_special_variant(value: str | None) -> str | None:
    text = str(value or "").strip()
    if not text or text.upper() in {"NA", "NONE"}:
        return None
    key = re.sub(r"\s+", " ", text).strip().upper()
    if key in SPECIAL_VARIANT_MAP:
        return SPECIAL_VARIANT_MAP[key]
    return text


def build_normalized_compare_key(
    item_type: str | None,
    skin_name: str | None,
    wear: str | None,
    special_variant: str | None = None,
    stattrak: bool = False,
    souvenir: bool = False,
) -> str:
    weapon = str(item_type or "").strip()
    skin = str(skin_name or "").strip()
    wear_en = normalize_wear(wear) or ""
    special = normalize_special_variant(special_variant)
    segments = [segment for segment in [weapon, skin, wear_en] if segment]
    if stattrak:
        segments.append("StatTrak")
    if souvenir:
        segments.append("Souvenir")
    if special:
        segments.append(special)
    return " | ".join(segments).strip()

