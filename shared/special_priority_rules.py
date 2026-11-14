from __future__ import annotations

from dataclasses import dataclass

from shared.utils import normalize_weapon_type_name


@dataclass(frozen=True)
class PrioritySpecialRule:
    weapon_type: str
    finish_family: str
    wear_codes: frozenset[str]
    allowed_variants: frozenset[str]


@dataclass(frozen=True)
class PrioritySpecialMatch:
    is_priority_target: bool
    weapon_type: str | None
    finish_family: str | None
    wear_code: str | None
    allowed_variants: frozenset[str]


@dataclass(frozen=True)
class PrioritySpecialValidation:
    is_priority_target: bool
    is_valid: bool
    reason: str
    finish_family: str | None
    allowed_variants: tuple[str, ...]


_PHASES = frozenset({"Phase 1", "Phase 2", "Phase 3", "Phase 4"})
_PHASES_234 = frozenset({"Phase 2", "Phase 3", "Phase 4"})
_DOPPLER_GEMS = frozenset({"Ruby", "Sapphire", "Black Pearl"})
_GAMMA_ALLOWED = frozenset({"Phase 1", "Phase 2", "Phase 3", "Phase 4", "Emerald"})
_DOPPLER_ALLOWED_ALL = _DOPPLER_GEMS | _PHASES
_DOPPLER_ALLOWED_234 = _DOPPLER_GEMS | _PHASES_234
_DOPPLER_ALLOWED_234_NO_BLACK = frozenset(
    {"Phase 2", "Phase 3", "Phase 4", "Ruby", "Sapphire"}
)

# Conservative whitelist derived from the user-provided screenshots only.
_PRIORITY_SPECIAL_RULES: tuple[PrioritySpecialRule, ...] = (
    PrioritySpecialRule("Butterfly Knife", "Doppler", frozenset({"FN"}), _DOPPLER_ALLOWED_234),
    PrioritySpecialRule("Butterfly Knife", "Gamma Doppler", frozenset({"FN"}), _GAMMA_ALLOWED),
    PrioritySpecialRule("Glock-18", "Gamma Doppler", frozenset({"FN"}), _GAMMA_ALLOWED),
    PrioritySpecialRule("Glock-18", "Gamma Doppler", frozenset({"MW", "FT"}), frozenset({"Emerald"})),
    PrioritySpecialRule("Karambit", "Doppler", frozenset({"FN"}), _DOPPLER_ALLOWED_234),
    PrioritySpecialRule("Karambit", "Gamma Doppler", frozenset({"FN"}), _GAMMA_ALLOWED),
    PrioritySpecialRule("M9 Bayonet", "Doppler", frozenset({"FN"}), _DOPPLER_ALLOWED_234),
    PrioritySpecialRule("M9 Bayonet", "Gamma Doppler", frozenset({"FN"}), _GAMMA_ALLOWED),
    PrioritySpecialRule("Skeleton Knife", "Doppler", frozenset({"FN"}), _DOPPLER_ALLOWED_234),
    PrioritySpecialRule("Talon Knife", "Doppler", frozenset({"FN"}), _DOPPLER_ALLOWED_234),
    PrioritySpecialRule("Bayonet", "Doppler", frozenset({"FN"}), _DOPPLER_ALLOWED_234_NO_BLACK),
    PrioritySpecialRule("Bayonet", "Gamma Doppler", frozenset({"FN"}), _GAMMA_ALLOWED),
    PrioritySpecialRule("Nomad Knife", "Doppler", frozenset({"FN"}), _DOPPLER_ALLOWED_234_NO_BLACK),
    PrioritySpecialRule("Flip Knife", "Doppler", frozenset({"FN"}), _DOPPLER_ALLOWED_234),
    PrioritySpecialRule("Flip Knife", "Gamma Doppler", frozenset({"FN"}), _GAMMA_ALLOWED),
    PrioritySpecialRule("Stiletto Knife", "Doppler", frozenset({"FN"}), _DOPPLER_ALLOWED_234),
    PrioritySpecialRule("Ursus Knife", "Doppler", frozenset({"FN"}), _DOPPLER_ALLOWED_ALL),
    PrioritySpecialRule("Bowie Knife", "Doppler", frozenset({"FN"}), _DOPPLER_ALLOWED_ALL),
    PrioritySpecialRule("Bowie Knife", "Gamma Doppler", frozenset({"FN"}), _GAMMA_ALLOWED),
    PrioritySpecialRule("Huntsman Knife", "Doppler", frozenset({"FN"}), _DOPPLER_ALLOWED_234),
    PrioritySpecialRule("Huntsman Knife", "Gamma Doppler", frozenset({"FN"}), _GAMMA_ALLOWED),
    PrioritySpecialRule("Survival Knife", "Doppler", frozenset({"FN"}), _DOPPLER_ALLOWED_ALL),
    PrioritySpecialRule("Shadow Daggers", "Doppler", frozenset({"FN"}), _DOPPLER_ALLOWED_ALL),
    PrioritySpecialRule("Shadow Daggers", "Gamma Doppler", frozenset({"FN"}), _GAMMA_ALLOWED),
    PrioritySpecialRule("Gut Knife", "Doppler", frozenset({"FN"}), _DOPPLER_ALLOWED_ALL),
    PrioritySpecialRule("Gut Knife", "Gamma Doppler", frozenset({"FN"}), _GAMMA_ALLOWED),
    PrioritySpecialRule("Paracord Knife", "Doppler", frozenset({"FN"}), _DOPPLER_ALLOWED_ALL),
    PrioritySpecialRule("Falchion Knife", "Doppler", frozenset({"FN"}), _DOPPLER_ALLOWED_ALL),
    PrioritySpecialRule("Falchion Knife", "Gamma Doppler", frozenset({"FN"}), _GAMMA_ALLOWED),
)


def priority_special_match(
    *,
    market_name: str | None,
    wear_code: str | None,
    item_variant_name: str | None = None,
    detail_title: str | None = None,
    special_variant: str | None = None,
) -> PrioritySpecialMatch:
    weapon_type = normalize_weapon_type_name(market_name or detail_title or item_variant_name or "")
    wear = str(wear_code or "").strip().upper() or "NA"
    finish_family = resolve_special_finish_family(
        detail_title,
        item_variant_name,
        market_name,
        special_variant,
    )
    if not weapon_type or weapon_type == "Unknown" or not finish_family:
        return PrioritySpecialMatch(False, None, finish_family, wear, frozenset())

    for rule in _PRIORITY_SPECIAL_RULES:
        if (
            rule.weapon_type == weapon_type
            and rule.finish_family == finish_family
            and wear in rule.wear_codes
        ):
            return PrioritySpecialMatch(
                True,
                weapon_type,
                finish_family,
                wear,
                rule.allowed_variants,
            )
    return PrioritySpecialMatch(False, weapon_type, finish_family, wear, frozenset())


def validate_priority_special_confirmation(
    *,
    market_name: str | None,
    wear_code: str | None,
    item_variant_name: str | None = None,
    detail_title: str | None = None,
    special_variant: str | None = None,
) -> PrioritySpecialValidation:
    match = priority_special_match(
        market_name=market_name,
        wear_code=wear_code,
        item_variant_name=item_variant_name,
        detail_title=detail_title,
        special_variant=special_variant,
    )
    if not match.is_priority_target:
        return PrioritySpecialValidation(False, True, "not_priority_target", match.finish_family, ())

    variant_value = _normalize_variant_value(special_variant)
    if not variant_value:
        return PrioritySpecialValidation(
            True,
            False,
            "priority target missing confirmed special variant",
            match.finish_family,
            tuple(sorted(match.allowed_variants)),
        )
    if variant_value not in match.allowed_variants:
        return PrioritySpecialValidation(
            True,
            False,
            f"priority target variant {variant_value} not in whitelist",
            match.finish_family,
            tuple(sorted(match.allowed_variants)),
        )
    return PrioritySpecialValidation(
        True,
        True,
        "priority target whitelist matched",
        match.finish_family,
        tuple(sorted(match.allowed_variants)),
    )


def priority_special_score_boost(
    *,
    market_name: str | None,
    wear_code: str | None,
    item_variant_name: str | None = None,
    detail_title: str | None = None,
    special_variant: str | None = None,
) -> float:
    match = priority_special_match(
        market_name=market_name,
        wear_code=wear_code,
        item_variant_name=item_variant_name,
        detail_title=detail_title,
        special_variant=special_variant,
    )
    if not match.is_priority_target:
        return 0.0
    if match.finish_family == "Gamma Doppler":
        return 120.0
    return 100.0


def resolve_special_finish_family(*texts: str | None) -> str | None:
    merged = " | ".join(str(text or "").strip() for text in texts if str(text or "").strip())
    if not merged:
        return None
    lowered = merged.lower()
    if any(token in lowered for token in ("gamma doppler", "伽玛多普勒", "伽馬多普勒")):
        return "Gamma Doppler"
    if "emerald" in lowered or "绿宝石" in merged or "綠寶石" in merged:
        return "Gamma Doppler"
    if any(token in lowered for token in ("doppler", "多普勒")):
        return "Doppler"
    if any(token in lowered for token in ("ruby", "sapphire", "black pearl")) or any(
        token in merged for token in ("红宝石", "紅寶石", "蓝宝石", "藍寶石", "黑珍珠")
    ):
        return "Doppler"
    return None


def _normalize_variant_value(raw_value: str | None) -> str | None:
    text = str(raw_value or "").strip()
    if not text:
        return None
    lowered = text.casefold()
    compact = lowered.replace(" ", "")
    if compact in {"p1", "phase1", "阶段1", "第1阶段", "第一阶段"}:
        return "Phase 1"
    if compact in {"p2", "phase2", "阶段2", "第2阶段", "第二阶段"}:
        return "Phase 2"
    if compact in {"p3", "phase3", "阶段3", "第3阶段", "第三阶段"}:
        return "Phase 3"
    if compact in {"p4", "phase4", "阶段4", "第4阶段", "第四阶段"}:
        return "Phase 4"
    if compact in {"blackpearl", "黑珍珠"}:
        return "Black Pearl"
    if compact in {"ruby", "红宝石", "紅寶石"}:
        return "Ruby"
    if compact in {"sapphire", "蓝宝石", "藍寶石"}:
        return "Sapphire"
    if compact in {"emerald", "绿宝石", "綠寶石"}:
        return "Emerald"
    return text
