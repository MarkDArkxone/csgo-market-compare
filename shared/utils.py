from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any

WEAPON_TYPE_EN_TO_CN = {
    "Bayonet": "刺刀",
    "Bowie Knife": "鲍伊猎刀",
    "Butterfly Knife": "蝴蝶刀",
    "Classic Knife": "海豹短刀",
    "Falchion Knife": "弯刀",
    "Flip Knife": "折叠刀",
    "Gut Knife": "穿肠刀",
    "Huntsman Knife": "猎杀者匕首",
    "Karambit": "爪子刀",
    "Kukri Knife": "廓尔喀刀",
    "M9 Bayonet": "M9 刺刀",
    "Navaja Knife": "折刀",
    "Nomad Knife": "流浪者匕首",
    "Paracord Knife": "系绳匕首",
    "Shadow Daggers": "暗影双匕",
    "Skeleton Knife": "骷髅匕首",
    "Stiletto Knife": "短剑",
    "Survival Knife": "求生匕首",
    "Talon Knife": "锯齿爪刀",
    "Ursus Knife": "熊刀",
    "Vanilla Knife": "普通匕首",
    "Bloodhound Gloves": "血猎手套",
    "Broken Fang Gloves": "狂牙手套",
    "Driver Gloves": "驾驶手套",
    "Hand Wraps": "裹手",
    "Hydra Gloves": "九头蛇手套",
    "Moto Gloves": "摩托手套",
    "Specialist Gloves": "专业手套",
    "Sport Gloves": "运动手套",
    "AK-47": "AK-47",
    "AUG": "AUG",
    "AWP": "AWP",
    "FAMAS": "法玛斯",
    "G3SG1": "G3SG1",
    "Galil AR": "加利尔 AR",
    "M4A1-S": "M4A1 消音型",
    "M4A4": "M4A4",
    "SCAR-20": "SCAR-20",
    "SG 553": "SG553",
    "SSG 08": "SSG08",
    "CZ75-Auto": "CZ75 自动手枪",
    "Desert Eagle": "沙漠之鹰",
    "Dual Berettas": "双持贝瑞塔",
    "Five-SeveN": "FN57",
    "Glock-18": "格洛克 18 型",
    "P2000": "P2000",
    "P250": "P250",
    "R8 Revolver": "R8 左轮手枪",
    "Tec-9": "Tec-9",
    "USP-S": "USP 消音版",
    "Zeus x27": "电击枪",
    "MAC-10": "MAC-10",
    "MP5-SD": "MP5-SD",
    "MP7": "MP7",
    "MP9": "MP9",
    "P90": "P90",
    "PP-Bizon": "PP - 野牛",
    "UMP-45": "UMP-45",
    "CT": "反恐精英",
    "T": "恐怖分子",
}

WEAR_TO_CODE = {
    "Factory New": "FN",
    "Minimal Wear": "MW",
    "Field-Tested": "FT",
    "Well-Worn": "WW",
    "Battle-Scarred": "BS",
    "崭新出厂": "FN",
    "略有磨损": "MW",
    "久经沙场": "FT",
    "破损不堪": "WW",
    "战痕累累": "BS",
}

CODE_TO_DISPLAY = {
    "FN": "Factory New",
    "MW": "Minimal Wear",
    "FT": "Field-Tested",
    "WW": "Well-Worn",
    "BS": "Battle-Scarred",
    "NA": "N/A",
}

CODE_TO_CN_DISPLAY = {
    "FN": "崭新出厂",
    "MW": "略有磨损",
    "FT": "久经沙场",
    "WW": "破损不堪",
    "BS": "战痕累累",
    "NA": "无磨损",
}

_WEAR_PATTERN = re.compile(r"[\(（]([^()（）]+)[\)）]\s*$")
_PHASE_PATTERN = re.compile(r"\bphase\s*([1-4])\b", re.IGNORECASE)
_P_SHORT_PATTERN = re.compile(r"\bp\s*([1-4])\b", re.IGNORECASE)
_PHASE_CN_PATTERN = re.compile(r"阶段\s*([1-4])")
_DISPLAY_VARIANT_SUFFIX_PATTERN = re.compile(r"\s*\[(?P<tag>[^\[\]]+)\]\s*$")
_SPACES_PATTERN = re.compile(r"\s+")
_WEAPON_TYPE_KEY_STRIP = re.compile(r"[\s\-_]+")
_STAR_MARKER_PATTERN = re.compile(r"[★☆]|\(\s*[★☆]\s*\)|（\s*[★☆]\s*）|鈽\?|\u923d\ufffd")
_LEADING_BROKEN_STAR_PATTERN = re.compile(r"^(?:\?\s*)+")
_VARIANT_STRIP_PATTERN = re.compile(
    r"\b(?:phase\s*[1-4]|p\s*[1-4]|ruby|sapphire|black\s*pearl|emerald)\b|(?:阶段\s*[1-4]|红宝石|蓝宝石|黑珍珠|绿宝石)",
    re.IGNORECASE,
)
_STAR_MARKERS = (
    "★",
    "☆",
    "(★)",
    "（★）",
    "鈽?",
    "鈽�",
    "бя",
    "â˜…",
)
_SPECIAL_FINISH_WEAPON_HINTS = (
    "bayonet",
    "knife",
    "karambit",
    "daggers",
    "talon",
    "ursus",
    "kukri",
    "stiletto",
    "skeleton",
    "navaja",
    "bowie",
    "falchion",
    "butterfly",
    "huntsman",
    "survival",
    "nomad",
    "paracord",
    "刺刀",
    "猎刀",
    "蝴蝶刀",
    "爪子刀",
    "匕首",
    "双匕",
    "熊刀",
    "廓尔喀刀",
    "短剑",
    "骷髅匕首",
    "折刀",
    "鲍伊猎刀",
    "弯刀",
    "求生匕首",
    "流浪者匕首",
    "系绳匕首",
)
_EXACT_BASE_ALIASES = {
    "AWP | 灵缇": "AWP | LongDog",
    "AWP | 一击碧命": "AWP | Green Energy",
    "M4A1 消音型 | 平流层巡航": "M4A1 消音型 | Stratosphere",
    "M4A4 | 炼狱之火": "M4A4 | Hellish",
    "专业手套（★） | 羽袭": "专业手套（★） | Pillow Punchers",
    "运动手套（★） | 紫外狂潮": "运动手套（★） | Ultra Violent",
}
_PREFIX_BASE_ALIASES = {
    "M4A1消音版": "M4A1 消音型",
    "M4A1 消音版": "M4A1 消音型",
    "USP消音版": "USP 消音版",
    "格洛克18型": "格洛克 18 型",
}


_WEAPON_TYPE_KEY_TO_EN = {
    **{
        re.sub(r"[\s\-_]+", "", key).casefold(): key
        for key in WEAPON_TYPE_EN_TO_CN
    },
    **{
        re.sub(r"[\s\-_]+", "", value).casefold(): key
        for key, value in WEAPON_TYPE_EN_TO_CN.items()
    },
    "sg553": "SG 553",
    "ssg08": "SSG 08",
    "fn57": "Five-SeveN",
    "m4a1消音型": "M4A1-S",
    "usp消音版": "USP-S",
    "pp野牛": "PP-Bizon",
    "双持贝瑞塔": "Dual Berettas",
    "格洛克18型": "Glock-18",
    "cz75自动手枪": "CZ75-Auto",
    "r8左轮手枪": "R8 Revolver",
}


@dataclass(frozen=True)
class NameIdentity:
    base_name: str
    wear_code: str
    wear_label: str
    variant_group: str
    variant_label: str


def now_ts() -> int:
    return int(time.time())


def safe_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def extract_variant_fields(*texts: str | None) -> tuple[str, str]:
    merged = " | ".join(str(t or "") for t in texts).strip()
    if not merged:
        return "none", "NA"

    lowered = merged.lower()
    has_special_finish_weapon = any(
        token in lowered for token in _SPECIAL_FINISH_WEAPON_HINTS
    )
    has_doppler_family = any(
        token in lowered
        for token in ("doppler", "gamma doppler", "多普勒", "伽玛多普勒", "伽馬多普勒")
    )
    can_be_special_finish = has_special_finish_weapon or has_doppler_family

    if can_be_special_finish and ("ruby" in lowered or "红宝石" in merged):
        return "gem", "Ruby"
    if can_be_special_finish and ("sapphire" in lowered or "蓝宝石" in merged):
        return "gem", "Sapphire"
    if can_be_special_finish and (
        "black pearl" in lowered or "blackpearl" in lowered or "黑珍珠" in merged
    ):
        return "gem", "Black Pearl"
    if can_be_special_finish and ("emerald" in lowered or "绿宝石" in merged):
        return "gem", "Emerald"

    phase_match = _PHASE_PATTERN.search(merged)
    if phase_match and can_be_special_finish:
        return "phase", f"Phase {phase_match.group(1)}"
    p_short = _P_SHORT_PATTERN.search(merged)
    if p_short and can_be_special_finish:
        return "phase", f"Phase {p_short.group(1)}"
    phase_cn = _PHASE_CN_PATTERN.search(merged)
    if phase_cn and can_be_special_finish:
        return "phase", f"Phase {phase_cn.group(1)}"

    return "none", "NA"


def normalize_base_name(raw_base_name: str, *, strip_variant: bool = True) -> str:
    text = (raw_base_name or "").strip()
    if not text:
        return ""

    # Some Windows / console code paths collapse the leading star marker into '?'.
    text = _LEADING_BROKEN_STAR_PATTERN.sub("", text).strip()
    text = _SPACES_PATTERN.sub(" ", _STAR_MARKER_PATTERN.sub(" ", text)).strip()
    stripped = _VARIANT_STRIP_PATTERN.sub(" ", text) if strip_variant else text
    normalized = _SPACES_PATTERN.sub(" ", stripped).strip()
    if normalized in _EXACT_BASE_ALIASES:
        return _EXACT_BASE_ALIASES[normalized]
    for prefix, replacement in _PREFIX_BASE_ALIASES.items():
        if normalized.startswith(prefix):
            return f"{replacement}{normalized[len(prefix):]}"
    if normalized:
        return normalized
    # If stripping removed everything unexpectedly, keep original text.
    return text


def _normalize_special_finish_parent_base(
    raw_base_name: str,
    *,
    variant_label: str,
) -> str:
    text = str(raw_base_name or "").strip()
    if not text:
        return text

    patterns = {
        "Ruby": r"\bruby\b",
        "Sapphire": r"\bsapphire\b",
        "Black Pearl": r"\bblack\s*pearl\b",
        "Emerald": r"\bemerald\b",
    }
    parent_families = {
        "Ruby": "Doppler",
        "Sapphire": "Doppler",
        "Black Pearl": "Doppler",
        "Emerald": "Gamma Doppler",
    }
    pattern = patterns.get(str(variant_label or "").strip())
    if not pattern:
        return text
    parent_family = parent_families.get(str(variant_label or "").strip(), "").strip()
    replaced = re.sub(pattern, parent_family or " ", text, flags=re.IGNORECASE)
    replaced = _SPACES_PATTERN.sub(" ", replaced).strip()
    return replaced or text


def normalize_name_identity(
    market_name: str,
    variant_hint: str | None = None,
) -> NameIdentity:
    text = (market_name or "").strip()
    if not text:
        return NameIdentity(
            base_name="",
            wear_code="NA",
            wear_label=CODE_TO_DISPLAY["NA"],
            variant_group="none",
            variant_label="NA",
        )

    suffix_match = _DISPLAY_VARIANT_SUFFIX_PATTERN.search(text)
    if suffix_match:
        suffix_variant = str(suffix_match.group("tag") or "").strip()
        if suffix_variant and not variant_hint:
            variant_hint = suffix_variant
        text = text[: suffix_match.start()].strip()

    match = _WEAR_PATTERN.search(text)
    if not match:
        base_part = text
        wear_code = "NA"
    else:
        wear_raw = match.group(1).strip()
        wear_code = WEAR_TO_CODE.get(wear_raw, "NA")
        base_part = text[: match.start()].strip()

    wear_label = CODE_TO_DISPLAY.get(wear_code, CODE_TO_DISPLAY["NA"])
    variant_group, variant_label = extract_variant_fields(text, variant_hint)
    if variant_label in {"Ruby", "Sapphire", "Black Pearl", "Emerald"}:
        base_part = _normalize_special_finish_parent_base(
            base_part,
            variant_label=variant_label,
        )
    base_name = normalize_base_name(
        base_part,
        strip_variant=(variant_label != "NA"),
    )
    return NameIdentity(
        base_name=base_name,
        wear_code=wear_code,
        wear_label=wear_label,
        variant_group=variant_group,
        variant_label=variant_label,
    )


def build_comparison_market_key(source_market_name: str, variant_tag: str) -> str:
    name = (source_market_name or "").strip()
    tag = (variant_tag or "NA").strip()
    if not name:
        return name
    if not tag or tag == "NA":
        return name
    return f"{name} [{tag}]"


def normalize_weapon_type_name(raw_name: str | None) -> str:
    text = (raw_name or "").strip()
    if not text:
        return "Unknown"

    if " | " in text:
        text = text.split(" | ", 1)[0].strip()
    text = _SPACES_PATTERN.sub(" ", _STAR_MARKER_PATTERN.sub(" ", text)).strip()

    key = _WEAPON_TYPE_KEY_STRIP.sub("", text).casefold()
    return _WEAPON_TYPE_KEY_TO_EN.get(key, text)


def weapon_type_to_cn(raw_name: str | None) -> str | None:
    canonical = normalize_weapon_type_name(raw_name)
    return WEAPON_TYPE_EN_TO_CN.get(canonical)


def extract_weapon_type(base_name: str, fallback_name: str | None = None) -> str:
    text = (base_name or "").strip()
    if not text:
        text = (fallback_name or "").strip()
    if not text:
        return "Unknown"

    if " | " in text:
        text = text.split(" | ", 1)[0].strip()
    text = _SPACES_PATTERN.sub(" ", _STAR_MARKER_PATTERN.sub(" ", text)).strip()

    return normalize_weapon_type_name(text)
