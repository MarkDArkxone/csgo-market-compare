from __future__ import annotations

import json
import logging
import math
import re
import sqlite3
import threading
from datetime import datetime
from functools import cmp_to_key
from pathlib import Path
from uuid import uuid4

from shared.normalizer import normalize_special_variant, normalize_wear
from shared.special_priority_rules import priority_special_score_boost
from shared.special_items import compute_special_priority_score
from shared.utils import (
    extract_variant_fields,
    normalize_base_name,
    normalize_name_identity,
    normalize_weapon_type_name,
)

logger = logging.getLogger(__name__)


class Repository:
    _PRIMARY_SORT_WEIGHT = 0.6
    _SECONDARY_SORT_WEIGHT = 0.4
    _DISPLAY_ROUNDED_FIELDS = {
        "roll_raw_price",
        "roll_reference_price",
        "roll_converted_cost",
        "youpin_sell_price",
        "youpin_live_price",
        "gross_profit",
        "gross_profit_rate",
        "net_profit",
        "net_profit_rate",
        "score",
    }
    _PRICE_LIKE_FIELDS = {
        "roll_raw_price",
        "roll_reference_price",
        "roll_converted_cost",
        "youpin_sell_price",
        "youpin_live_price",
        "gross_profit",
        "net_profit",
    }
    _RANK_MAPS = {
        "decision_grade": {"A": 5, "B": 4, "C": 3, "D": 2, "X": 1},
        "bucket_risk_level": {"low": 3, "medium": 2, "high": 1},
        "verify_status": {
            "verified_live": 4,
            "verified_cache": 3,
            "unverified": 2,
            "verify_failed": 1,
        },
        "data_confidence": {"high": 3, "medium": 2, "low": 1},
        "price_status": {"ok": 4, "cache": 3, "stale": 2, "missing": 1},
    }
    _ALLOWED_SPECIAL_MAPPING_LEVELS = {
        "exact_market_name",
        "base+wear+variant",
        "base+wear+special-style",
    }

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        # A larger busy timeout makes cross-process contention survivable when
        # another reader/writer temporarily holds the DB lock.
        self._conn = sqlite3.connect(
            str(self.db_path),
            check_same_thread=False,
            timeout=30.0,
        )
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @staticmethod
    def _compute_opportunity_score(row: dict) -> float | None:
        if row.get("youpin_sell_price") is None or row.get("roll_converted_cost") is None:
            return None

        net_profit_rate = row.get("net_profit_rate")
        try:
            net_profit_rate = float(net_profit_rate) if net_profit_rate is not None else 0.0
        except (TypeError, ValueError):
            net_profit_rate = 0.0

        if bool(row.get("is_fresh_pair")):
            freshness_score = 1.0
        elif bool(row.get("is_roll_fresh")):
            freshness_score = 0.5
        else:
            freshness_score = 0.2

        verify_status = str(row.get("verify_status") or "").strip()
        if verify_status == "verified_live":
            verification_score = 1.0
        elif verify_status == "verified_cache":
            verification_score = 0.6
        else:
            verification_score = 0.2

        bucket_risk_level = str(row.get("bucket_risk_level") or "").strip().lower()
        if bucket_risk_level == "low":
            risk_score = 1.0
        elif bucket_risk_level == "medium":
            risk_score = 0.6
        else:
            risk_score = 0.2

        try:
            liquidity_score = math.log(float(row.get("roll_bucket_count") or 0) + 1.0)
        except (TypeError, ValueError):
            liquidity_score = 0.0

        return (
            net_profit_rate * 0.4
            + freshness_score * 0.2
            + verification_score * 0.2
            + risk_score * 0.1
            + liquidity_score * 0.1
        )

    @classmethod
    def _display_bucket_number(cls, key: str, number: float):
        if key in {"gross_profit_rate", "net_profit_rate", "score"}:
            return round(number, 2)

        if key in cls._PRICE_LIKE_FIELDS:
            absolute = abs(number)
            if absolute >= 10000:
                return round(number / 100.0) * 100.0
            if absolute >= 1000:
                return round(number / 10.0) * 10.0
            if absolute >= 100:
                return round(number)
            return round(number, 1)

        if key in cls._DISPLAY_ROUNDED_FIELDS:
            return round(number, 2)
        return number

    @classmethod
    def _resolve_sort_value(
        cls,
        row: dict,
        key: str,
        *,
        display_bucket: bool = False,
    ):
        value = row.get(key)
        if value is None:
            return None

        if key in cls._RANK_MAPS:
            return cls._RANK_MAPS[key].get(str(value).strip().lower(), 0)

        if key in {"is_fresh_pair", "is_live_verified"}:
            return 1 if bool(value) else 0

        if isinstance(value, str):
            return value.casefold()

        try:
            number = float(value)
        except (TypeError, ValueError):
            return str(value).casefold()

        if display_bucket and key in cls._DISPLAY_ROUNDED_FIELDS:
            return cls._display_bucket_number(key, number)
        return number

    @classmethod
    def _compare_sort_values(
        cls,
        left,
        right,
        *,
        descending: bool,
    ) -> int:
        left_missing = left is None
        right_missing = right is None
        if left_missing and right_missing:
            return 0
        if left_missing:
            return 1
        if right_missing:
            return -1
        if left < right:
            return 1 if descending else -1
        if left > right:
            return -1 if descending else 1
        return 0

    @classmethod
    def _build_rank_lookup(
        cls,
        rows: list[dict],
        key: str,
        *,
        descending: bool,
    ) -> dict[int, float]:
        resolved = [
            (index, cls._resolve_sort_value(row, key, display_bucket=True))
            for index, row in enumerate(rows)
        ]
        non_missing = [(index, value) for index, value in resolved if value is not None]
        if not non_missing:
            return {index: -1.0 for index in range(len(rows))}

        ordered = sorted(
            non_missing,
            key=cmp_to_key(
                lambda left, right: cls._compare_sort_values(
                    left[1],
                    right[1],
                    descending=descending,
                )
            ),
        )
        group_ids: list[int] = []
        current_group = -1
        previous_value = None
        for _index, value in ordered:
            if previous_value is None or cls._compare_sort_values(
                previous_value,
                value,
                descending=descending,
            ) != 0:
                current_group += 1
                previous_value = value
            group_ids.append(current_group)

        if current_group <= 0:
            lookup = {index: 1.0 for index, _value in ordered}
        else:
            lookup = {
                index: 1.0 - (group_id / current_group)
                for (index, _value), group_id in zip(ordered, group_ids)
            }

        for index, value in resolved:
            if value is None:
                lookup[index] = -1.0
        return lookup

    @classmethod
    def _sort_rows_in_memory(
        cls,
        rows: list[dict],
        *,
        primary_key: str,
        primary_desc: bool,
        secondary_key: str = "",
        secondary_desc: bool = True,
        tertiary_name_key: str = "market_name",
    ) -> list[dict]:
        use_joint_sort = bool(secondary_key and secondary_key != primary_key)
        row_index_lookup = {id(row): index for index, row in enumerate(rows)}
        primary_rank_lookup = (
            cls._build_rank_lookup(rows, primary_key, descending=primary_desc)
            if use_joint_sort
            else {}
        )
        secondary_rank_lookup = (
            cls._build_rank_lookup(rows, secondary_key, descending=secondary_desc)
            if use_joint_sort
            else {}
        )

        def compare_rows(left: dict, right: dict) -> int:
            if use_joint_sort:
                left_index = row_index_lookup[id(left)]
                right_index = row_index_lookup[id(right)]
                left_joint = (
                    primary_rank_lookup.get(left_index, -1.0) * cls._PRIMARY_SORT_WEIGHT
                    + secondary_rank_lookup.get(left_index, -1.0) * cls._SECONDARY_SORT_WEIGHT
                )
                right_joint = (
                    primary_rank_lookup.get(right_index, -1.0) * cls._PRIMARY_SORT_WEIGHT
                    + secondary_rank_lookup.get(right_index, -1.0) * cls._SECONDARY_SORT_WEIGHT
                )
                if left_joint > right_joint:
                    return -1
                if left_joint < right_joint:
                    return 1

            primary_cmp = cls._compare_sort_values(
                cls._resolve_sort_value(left, primary_key, display_bucket=True),
                cls._resolve_sort_value(right, primary_key, display_bucket=True),
                descending=primary_desc,
            )
            if primary_cmp != 0:
                return primary_cmp

            if secondary_key and secondary_key != primary_key:
                secondary_cmp = cls._compare_sort_values(
                    cls._resolve_sort_value(left, secondary_key, display_bucket=True),
                    cls._resolve_sort_value(right, secondary_key, display_bucket=True),
                    descending=secondary_desc,
                )
                if secondary_cmp != 0:
                    return secondary_cmp

            left_name = str(left.get(tertiary_name_key) or left.get("market_name") or left.get("market_key") or "").casefold()
            right_name = str(right.get(tertiary_name_key) or right.get("market_name") or right.get("market_key") or "").casefold()
            if left_name < right_name:
                return -1
            if left_name > right_name:
                return 1
            return 0

        return sorted(rows, key=cmp_to_key(compare_rows))

    @classmethod
    def _is_special_variant_row_trusted(cls, row: dict) -> bool:
        variant_label = str(row.get("variant_label") or row.get("variant_tag") or "NA").strip()
        if not variant_label or variant_label == "NA":
            return True
        mapping_match_level = str(row.get("mapping_match_level") or "").strip()
        verify_status = str(row.get("verify_status") or "").strip()
        verify_source = str(row.get("verify_source") or "").strip()
        if (
            mapping_match_level == "watchlist_template_id"
            and verify_status == "verified_cache"
            and verify_source == "watchlist_template_direct"
        ):
            return True
        if (
            mapping_match_level in cls._ALLOWED_SPECIAL_MAPPING_LEVELS
            and verify_status == "verified_cache"
            and verify_source == "watchlist_template_direct"
        ):
            return True
        if (
            mapping_match_level in cls._ALLOWED_SPECIAL_MAPPING_LEVELS
            and verify_status == "verified_cache"
            and verify_source == "youpin_exact_name_backfill"
        ):
            return True
        return (
            mapping_match_level in cls._ALLOWED_SPECIAL_MAPPING_LEVELS
            and verify_status == "verified_live"
            and verify_source.startswith("live_youpin")
        )

    @classmethod
    def _sanitize_special_variant_row(cls, row: dict) -> dict:
        source_name = str(row.get("source_market_name") or row.get("market_name") or row.get("market_key") or "")
        recalculated_group, recalculated_label = extract_variant_fields(source_name, row.get("base_name"))
        stored_variant = str(row.get("variant_label") or row.get("variant_tag") or "NA").strip()
        if stored_variant != "NA" and recalculated_label == "NA":
            sanitized = dict(row)
            sanitized["variant_group"] = recalculated_group
            sanitized["variant_label"] = "NA"
            sanitized["variant_tag"] = "NA"
            sanitized["display_variant"] = ""
            return sanitized
        if cls._is_special_variant_row_trusted(row):
            return row
        sanitized = dict(row)
        for key in (
            "youpin_sell_price",
            "youpin_price",
            "youpin_live_price",
            "gross_profit",
            "gross_profit_rate",
            "net_sell_price",
            "net_profit",
            "net_profit_rate",
            "price_diff",
            "profit_rate",
            "live_verify_price",
            "live_verify_time",
            "price_reference_median",
            "price_mad",
            "price_iqr",
            "price_anomaly_ratio",
            "price_std_dev",
            "price_trend",
        ):
            if key in sanitized:
                sanitized[key] = None
        sanitized["price_status"] = "verify_failed"
        sanitized["price_status_note"] = "Special variant price not live-verified"
        sanitized["verify_note"] = "Special variant requires live-verified specialStyle price"
        sanitized["score"] = None
        return sanitized

    def find_trusted_special_variant_price(
        self,
        *,
        source_market_name: str,
        base_name: str | None = None,
        wear_code: str | None = None,
        variant_label: str | None = None,
    ) -> dict | None:
        variant = str(variant_label or "NA").strip()
        if not variant or variant == "NA":
            return None

        source_name = str(source_market_name or "").strip()
        normalized_target = self._normalize_special_overlay_name(source_name, variant)
        restored_target = self._restore_special_finish_parent_name(source_name, variant)
        target_identity = normalize_name_identity(restored_target or source_name, variant)
        target_base = normalize_base_name(
            str(target_identity.base_name or base_name or ""),
            strip_variant=False,
        )
        target_wear = str(target_identity.wear_code or wear_code or "NA").strip().upper()

        with self._lock:
            comparison_rows = [
                dict(row)
                for row in self._conn.execute(
                    """
                    SELECT *
                    FROM comparisons
                    WHERE COALESCE(variant_label, variant_tag, 'NA') = ?
                      AND COALESCE(youpin_sell_price, 0) > 0
                    ORDER BY updated_at DESC
                    LIMIT 500
                    """,
                    (variant,),
                ).fetchall()
            ]
            serving_rows = [
                dict(row)
                for row in self._conn.execute(
                    """
                    SELECT *
                    FROM serving_comparisons
                    WHERE COALESCE(variant_label, 'NA') = ?
                      AND COALESCE(youpin_live_price, 0) > 0
                    ORDER BY is_current DESC, built_at DESC
                    LIMIT 500
                    """,
                    (variant,),
                ).fetchall()
            ]

        candidates: list[dict] = []
        for row in serving_rows:
            candidates.append(
                {
                    **row,
                    "market_name": row.get("market_key"),
                    "youpin_sell_price": row.get("youpin_live_price"),
                    "youpin_price_updated_at": row.get("youpin_live_time") or row.get("built_at"),
                }
            )
        candidates.extend(comparison_rows)

        for row in candidates:
            if not self._is_special_variant_row_trusted(row):
                continue
            try:
                price = float(row.get("youpin_sell_price"))
            except (TypeError, ValueError):
                continue
            if price <= 0:
                continue

            row_names = [
                str(row.get("market_name") or row.get("market_key") or "").strip(),
                str(row.get("source_market_name") or "").strip(),
                str(row.get("display_name_final") or "").strip(),
            ]
            if source_name and source_name in row_names:
                return {**row, "price": price}

            for row_name in row_names:
                if (
                    row_name
                    and normalized_target
                    and self._normalize_special_overlay_name(row_name, variant)
                    == normalized_target
                ):
                    return {**row, "price": price}

            row_identity_source = self._restore_special_finish_parent_name(
                next((name for name in row_names if name), ""),
                variant,
            )
            row_identity = normalize_name_identity(row_identity_source, variant)
            row_base = normalize_base_name(
                str(row_identity.base_name or row.get("base_name") or ""),
                strip_variant=False,
            )
            row_wear = str(row_identity.wear_code or row.get("wear_code") or "NA").strip().upper()
            if row_base == target_base and row_wear == target_wear:
                return {**row, "price": price}

        return None

    @staticmethod
    def _normalize_special_overlay_name(name: str, variant_label: str) -> str:
        normalized = str(name or "").strip()
        if not normalized:
            return ""
        normalized = re.sub(r"^[^A-Za-z0-9]+", "", normalized)
        if variant_label and variant_label != "NA":
            variant_pattern = re.escape(str(variant_label).strip())
            normalized = re.sub(rf"\s*\[{variant_pattern}\]", "", normalized, flags=re.IGNORECASE)
            normalized = re.sub(rf"\b{variant_pattern}\b", "", normalized, flags=re.IGNORECASE)
        normalized = re.sub(r"\s+", " ", normalized)
        normalized = normalized.replace(" ()", "()")
        return normalized.strip().casefold()

    @staticmethod
    def _restore_special_finish_parent_name(name: str, variant_label: str) -> str:
        text = str(name or "").strip()
        variant = str(variant_label or "").strip()
        if not text or variant not in {"Ruby", "Sapphire", "Black Pearl", "Emerald"}:
            return text

        parent_finish = "Gamma Doppler" if variant == "Emerald" else "Doppler"
        if "|" not in text:
            return text

        left, right = text.split("|", 1)
        right = right.strip()
        pattern = re.compile(rf"^{re.escape(variant)}(\s*\()", re.IGNORECASE)
        replaced = pattern.sub(f"{parent_finish}\\1", right, count=1)
        if replaced != right:
            return f"{left.strip()} | {replaced}".strip()
        return text

    def backfill_profit_direction(
        self,
        *,
        platform_fee_rate: float,
        withdraw_loss_rate: float,
    ) -> None:
        fee_rate = max(0.0, float(platform_fee_rate)) + max(0.0, float(withdraw_loss_rate))
        fee_rate = min(fee_rate, 0.99)
        with self._lock:
            self._conn.execute(
                """
                UPDATE comparisons
                SET
                    gross_profit = CASE
                        WHEN roll_converted_cost IS NOT NULL AND youpin_sell_price IS NOT NULL
                        THEN roll_converted_cost - youpin_sell_price
                        ELSE gross_profit
                    END,
                    gross_profit_rate = CASE
                        WHEN roll_converted_cost IS NOT NULL AND youpin_sell_price IS NOT NULL AND youpin_sell_price > 0
                        THEN (roll_converted_cost - youpin_sell_price) / youpin_sell_price * 100.0
                        ELSE gross_profit_rate
                    END,
                    net_sell_price = CASE
                        WHEN youpin_sell_price IS NOT NULL
                        THEN youpin_sell_price * (1.0 + ?)
                        ELSE net_sell_price
                    END,
                    net_profit = CASE
                        WHEN roll_converted_cost IS NOT NULL AND youpin_sell_price IS NOT NULL
                        THEN roll_converted_cost - (youpin_sell_price * (1.0 + ?))
                        ELSE net_profit
                    END,
                    net_profit_rate = CASE
                        WHEN roll_converted_cost IS NOT NULL AND youpin_sell_price IS NOT NULL AND (youpin_sell_price * (1.0 + ?)) > 0
                        THEN (roll_converted_cost - (youpin_sell_price * (1.0 + ?))) / (youpin_sell_price * (1.0 + ?)) * 100.0
                        ELSE net_profit_rate
                    END,
                    price_diff = CASE
                        WHEN roll_converted_cost IS NOT NULL AND youpin_sell_price IS NOT NULL
                        THEN roll_converted_cost - youpin_sell_price
                        ELSE price_diff
                    END,
                    profit_rate = CASE
                        WHEN roll_converted_cost IS NOT NULL AND youpin_sell_price IS NOT NULL AND youpin_sell_price > 0
                        THEN (roll_converted_cost - youpin_sell_price) / youpin_sell_price * 100.0
                        ELSE profit_rate
                    END
                WHERE roll_converted_cost IS NOT NULL AND youpin_sell_price IS NOT NULL
                """,
                (fee_rate, fee_rate, fee_rate, fee_rate, fee_rate),
            )
            self._conn.execute(
                """
                UPDATE serving_comparisons
                SET
                    net_profit = CASE
                        WHEN roll_converted_cost IS NOT NULL AND youpin_live_price IS NOT NULL
                        THEN roll_converted_cost - (youpin_live_price * (1.0 + ?))
                        ELSE net_profit
                    END,
                    net_profit_rate = CASE
                        WHEN roll_converted_cost IS NOT NULL AND youpin_live_price IS NOT NULL AND (youpin_live_price * (1.0 + ?)) > 0
                        THEN (roll_converted_cost - (youpin_live_price * (1.0 + ?))) / (youpin_live_price * (1.0 + ?)) * 100.0
                        ELSE net_profit_rate
                    END
                WHERE roll_converted_cost IS NOT NULL AND youpin_live_price IS NOT NULL
                """,
                (fee_rate, fee_rate, fee_rate, fee_rate),
            )
            self._conn.commit()

    def recompute_roll_rate_outputs(
        self,
        *,
        roll_to_youpin_rate: float,
        platform_fee_rate: float,
        withdraw_loss_rate: float,
    ) -> None:
        rate = max(0.0, float(roll_to_youpin_rate))
        if rate <= 0:
            return
        fee_rate = max(0.0, float(platform_fee_rate)) + max(0.0, float(withdraw_loss_rate))
        fee_rate = min(fee_rate, 0.99)
        with self._lock:
            self._conn.execute(
                """
                UPDATE comparisons
                SET
                    roll_converted_cost = CASE
                        WHEN roll_raw_price IS NOT NULL AND roll_raw_price > 0
                        THEN ROUND(roll_raw_price * ?, 4)
                        ELSE NULL
                    END,
                    gross_profit = CASE
                        WHEN roll_raw_price IS NOT NULL AND roll_raw_price > 0
                         AND youpin_sell_price IS NOT NULL AND youpin_sell_price > 0
                        THEN ROUND((roll_raw_price * ?) - youpin_sell_price, 4)
                        ELSE NULL
                    END,
                    gross_profit_rate = CASE
                        WHEN roll_raw_price IS NOT NULL AND roll_raw_price > 0
                         AND youpin_sell_price IS NOT NULL AND youpin_sell_price > 0
                        THEN ROUND((((roll_raw_price * ?) - youpin_sell_price) / youpin_sell_price) * 100.0, 4)
                        ELSE NULL
                    END,
                    net_sell_price = CASE
                        WHEN youpin_sell_price IS NOT NULL AND youpin_sell_price > 0
                        THEN ROUND(youpin_sell_price * (1.0 + ?), 4)
                        ELSE NULL
                    END,
                    net_profit = CASE
                        WHEN roll_raw_price IS NOT NULL AND roll_raw_price > 0
                         AND youpin_sell_price IS NOT NULL AND youpin_sell_price > 0
                        THEN ROUND((roll_raw_price * ?) - (youpin_sell_price * (1.0 + ?)), 4)
                        ELSE NULL
                    END,
                    net_profit_rate = CASE
                        WHEN roll_raw_price IS NOT NULL AND roll_raw_price > 0
                         AND youpin_sell_price IS NOT NULL AND youpin_sell_price > 0
                        THEN ROUND((((roll_raw_price * ?) - (youpin_sell_price * (1.0 + ?))) / (youpin_sell_price * (1.0 + ?))) * 100.0, 4)
                        ELSE NULL
                    END,
                    price_diff = CASE
                        WHEN roll_raw_price IS NOT NULL AND roll_raw_price > 0
                         AND youpin_sell_price IS NOT NULL AND youpin_sell_price > 0
                        THEN ROUND((roll_raw_price * ?) - youpin_sell_price, 4)
                        ELSE NULL
                    END,
                    profit_rate = CASE
                        WHEN roll_raw_price IS NOT NULL AND roll_raw_price > 0
                         AND youpin_sell_price IS NOT NULL AND youpin_sell_price > 0
                        THEN ROUND((((roll_raw_price * ?) - youpin_sell_price) / youpin_sell_price) * 100.0, 4)
                        ELSE NULL
                    END
                """,
                (
                    rate,
                    rate,
                    rate,
                    fee_rate,
                    rate,
                    fee_rate,
                    rate,
                    fee_rate,
                    fee_rate,
                    rate,
                    rate,
                ),
            )
            self._conn.execute(
                """
                UPDATE serving_comparisons
                SET
                    roll_converted_cost = CASE
                        WHEN roll_reference_price IS NOT NULL AND roll_reference_price > 0
                        THEN ROUND(roll_reference_price * ?, 4)
                        ELSE NULL
                    END,
                    net_profit = CASE
                        WHEN roll_reference_price IS NOT NULL AND roll_reference_price > 0
                         AND youpin_live_price IS NOT NULL AND youpin_live_price > 0
                        THEN ROUND((roll_reference_price * ?) - (youpin_live_price * (1.0 + ?)), 4)
                        ELSE NULL
                    END,
                    net_profit_rate = CASE
                        WHEN roll_reference_price IS NOT NULL AND roll_reference_price > 0
                         AND youpin_live_price IS NOT NULL AND youpin_live_price > 0
                        THEN ROUND((((roll_reference_price * ?) - (youpin_live_price * (1.0 + ?))) / (youpin_live_price * (1.0 + ?))) * 100.0, 4)
                        ELSE NULL
                    END
                """,
                (
                    rate,
                    rate,
                    fee_rate,
                    rate,
                    fee_rate,
                    fee_rate,
                ),
            )
            self._conn.commit()

    def _init_schema(self) -> None:
        with self._lock:
            cur = self._conn.cursor()
            cur.executescript(
                """
                PRAGMA journal_mode=WAL;
                PRAGMA synchronous=NORMAL;
                PRAGMA busy_timeout=30000;
                PRAGMA wal_autocheckpoint=1000;

                CREATE TABLE IF NOT EXISTS app_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS roll_trades (
                    trade_id TEXT PRIMARY KEY,
                    market_name TEXT NOT NULL,
                    base_name TEXT NOT NULL,
                    wear_code TEXT NOT NULL,
                    wear_label TEXT NOT NULL,
                    variant_group TEXT NOT NULL DEFAULT 'none',
                    variant_label TEXT NOT NULL DEFAULT 'NA',
                    variant_tag TEXT NOT NULL DEFAULT 'NA',
                    is_special_candidate INTEGER NOT NULL DEFAULT 0,
                    is_special_confirmed INTEGER NOT NULL DEFAULT 0,
                    special_variant TEXT,
                    special_variant_type TEXT,
                    priority_score REAL NOT NULL DEFAULT 0,
                    detail_last_fetched_at INTEGER NOT NULL DEFAULT 0,
                    item_external_id TEXT,
                    item_variant_name TEXT,
                    item_category_names TEXT,
                    detail_title TEXT,
                    float_value REAL,
                    pattern_seed INTEGER,
                    paint_index INTEGER,
                    detail_price REAL,
                    sale_status TEXT,
                    detail_url TEXT,
                    detail_error TEXT,
                    price REAL NOT NULL,
                    markup_percent REAL NOT NULL,
                    status TEXT NOT NULL,
                    first_seen_at INTEGER NOT NULL,
                    last_seen_at INTEGER NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_roll_market_last_seen
                ON roll_trades (market_name, last_seen_at DESC);

                CREATE INDEX IF NOT EXISTS idx_roll_status_last_seen
                ON roll_trades (status, last_seen_at DESC);

                CREATE TABLE IF NOT EXISTS product_catalog (
                    sku_key TEXT PRIMARY KEY,
                    source_market_name TEXT NOT NULL,
                    base_name TEXT NOT NULL,
                    weapon_type TEXT,
                    wear_code TEXT NOT NULL,
                    wear_label TEXT NOT NULL,
                    variant_group TEXT NOT NULL DEFAULT 'none',
                    variant_label TEXT NOT NULL DEFAULT 'NA',
                    first_seen_at INTEGER NOT NULL,
                    last_seen_at INTEGER NOT NULL,
                    last_roll_price REAL,
                    last_markup_percent REAL,
                    seen_count INTEGER NOT NULL DEFAULT 1,
                    is_active INTEGER NOT NULL DEFAULT 1
                );

                CREATE INDEX IF NOT EXISTS idx_product_catalog_last_seen
                ON product_catalog (last_seen_at DESC);

                CREATE INDEX IF NOT EXISTS idx_product_catalog_market
                ON product_catalog (source_market_name, wear_code, variant_label);

                CREATE TABLE IF NOT EXISTS roll_daily_snapshot (
                    source_market_name TEXT NOT NULL,
                    base_name TEXT NOT NULL,
                    wear_code TEXT NOT NULL,
                    wear_label TEXT NOT NULL,
                    variant_group TEXT NOT NULL DEFAULT 'none',
                    variant_label TEXT NOT NULL DEFAULT 'NA',
                    is_special_candidate INTEGER NOT NULL DEFAULT 0,
                    is_special_confirmed INTEGER NOT NULL DEFAULT 0,
                    special_variant TEXT,
                    special_variant_type TEXT,
                    priority_score REAL NOT NULL DEFAULT 0,
                    detail_last_fetched_at INTEGER NOT NULL DEFAULT 0,
                    detail_title TEXT,
                    float_value REAL,
                    pattern_seed INTEGER,
                    paint_index INTEGER,
                    detail_price REAL,
                    sale_status TEXT,
                    roll_reference_price REAL,
                    roll_max_markup REAL,
                    roll_bucket_count INTEGER NOT NULL DEFAULT 0,
                    roll_available_count INTEGER NOT NULL DEFAULT 0,
                    roll_listing_count INTEGER NOT NULL DEFAULT 0,
                    roll_last_seen_at INTEGER NOT NULL DEFAULT 0,
                    captured_at INTEGER NOT NULL,
                    PRIMARY KEY (source_market_name, variant_label)
                );

                CREATE INDEX IF NOT EXISTS idx_roll_daily_snapshot_captured
                ON roll_daily_snapshot (captured_at DESC);

                CREATE INDEX IF NOT EXISTS idx_roll_daily_snapshot_price
                ON roll_daily_snapshot (roll_reference_price DESC, roll_last_seen_at DESC);

                CREATE TABLE IF NOT EXISTS youpin_prices (
                    template_id INTEGER PRIMARY KEY,
                    market_name TEXT NOT NULL,
                    cn_name TEXT,
                    commodity_name TEXT,
                    price REAL,
                    price_updated_at INTEGER NOT NULL DEFAULT 0,
                    updated_at INTEGER NOT NULL,
                    fail_count INTEGER NOT NULL DEFAULT 0,
                    next_retry_at INTEGER NOT NULL DEFAULT 0,
                    last_error_kind TEXT,
                    last_error_message TEXT,
                    raw_json TEXT
                );

                CREATE TABLE IF NOT EXISTS youpin_price_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    template_id INTEGER NOT NULL,
                    price REAL NOT NULL,
                    observed_at INTEGER NOT NULL,
                    source TEXT NOT NULL DEFAULT 'cache',
                    raw_json TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_youpin_price_history_template_time
                ON youpin_price_history (template_id, observed_at DESC);

                CREATE TABLE IF NOT EXISTS comparisons (
                    market_name TEXT PRIMARY KEY,
                    source_market_name TEXT,
                    base_name TEXT NOT NULL,
                    weapon_type TEXT,
                    wear_code TEXT NOT NULL,
                    wear_label TEXT NOT NULL,
                    variant_group TEXT,
                    variant_label TEXT,
                    variant_tag TEXT,
                    is_special_candidate INTEGER NOT NULL DEFAULT 0,
                    is_special_confirmed INTEGER NOT NULL DEFAULT 0,
                    special_variant TEXT,
                    special_variant_type TEXT,
                    detail_last_fetched_at INTEGER NOT NULL DEFAULT 0,
                    detail_title TEXT,
                    float_value REAL,
                    pattern_seed INTEGER,
                    paint_index INTEGER,
                    detail_price REAL,
                    sale_status TEXT,
                    display_name_cn TEXT,
                    display_name_en TEXT,
                    display_name_final TEXT,
                    display_variant TEXT,
                    cn_name TEXT,
                    roll_raw_price REAL,
                    roll_converted_cost REAL,
                    youpin_sell_price REAL,
                    gross_profit REAL,
                    gross_profit_rate REAL,
                    net_sell_price REAL,
                    net_profit REAL,
                    net_profit_rate REAL,
                    bucket_risk_level TEXT,
                    priority_score REAL,
                    roll_reference_price REAL,
                    roll_max_markup REAL,
                    roll_bucket_count INTEGER NOT NULL,
                    roll_available_count INTEGER NOT NULL DEFAULT 0,
                    roll_listing_count INTEGER NOT NULL,
                    price_status TEXT,
                    price_status_note TEXT,
                    roll_last_seen_at INTEGER NOT NULL DEFAULT 0,
                    youpin_price_updated_at INTEGER NOT NULL DEFAULT 0,
                    youpin_age_seconds INTEGER,
                    roll_age_seconds INTEGER,
                    is_youpin_fresh INTEGER NOT NULL DEFAULT 0,
                    is_roll_fresh INTEGER NOT NULL DEFAULT 0,
                    is_fresh_pair INTEGER NOT NULL DEFAULT 0,
                    is_live_verified INTEGER NOT NULL DEFAULT 0,
                    live_verify_price REAL,
                    live_verify_time INTEGER,
                    verify_status TEXT NOT NULL DEFAULT 'unverified',
                    verify_source TEXT NOT NULL DEFAULT 'none',
                    verify_time INTEGER,
                    verify_age_seconds INTEGER,
                    verified_fresh INTEGER NOT NULL DEFAULT 0,
                    verify_note TEXT,
                    mapping_match_level TEXT,
                    mapping_confidence REAL,
                    decision_grade TEXT NOT NULL DEFAULT 'X',
                    decision_note TEXT,
                    data_confidence TEXT NOT NULL DEFAULT 'low',
                    stale_reason TEXT,
                    reference_reason TEXT,
                    bucket_markup REAL,
                    target_markup REAL,
                    price_anomaly INTEGER NOT NULL DEFAULT 0,
                    price_anomaly_ratio REAL,
                    price_reference_median REAL,
                    price_mad REAL,
                    price_iqr REAL,
                    price_std_dev REAL,
                    price_trend REAL,
                    price_anomaly_note TEXT,
                    youpin_price REAL,
                    price_diff REAL,
                    profit_rate REAL,
                    is_valid INTEGER NOT NULL DEFAULT 1,
                    error_type TEXT,
                    last_checked_at INTEGER,
                    data_source TEXT NOT NULL DEFAULT 'comparisons',
                    confidence_score REAL NOT NULL DEFAULT 1.0,
                    health_level TEXT NOT NULL DEFAULT 'healthy',
                    data_status TEXT NOT NULL DEFAULT 'ok',
                    audit_note TEXT,
                    calc_note TEXT NOT NULL,
                    updated_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS serving_runs (
                    run_id TEXT PRIMARY KEY,
                    started_at INTEGER NOT NULL,
                    finished_at INTEGER,
                    config_json TEXT NOT NULL,
                    stats_json TEXT
                );
                CREATE TABLE IF NOT EXISTS serving_comparisons (
                    run_id TEXT NOT NULL,
                    is_current INTEGER NOT NULL DEFAULT 1,
                    market_key TEXT NOT NULL,
                    display_name_final TEXT,
                    weapon_type TEXT,
                    wear_code TEXT,
                    wear_label TEXT,
                    variant_label TEXT,
                    is_special_candidate INTEGER NOT NULL DEFAULT 0,
                    is_special_confirmed INTEGER NOT NULL DEFAULT 0,
                    special_variant TEXT,
                    special_variant_type TEXT,
                    detail_last_fetched_at INTEGER NOT NULL DEFAULT 0,
                    float_value REAL,
                    pattern_seed INTEGER,
                    paint_index INTEGER,
                    detail_price REAL,
                    sale_status TEXT,
                    roll_reference_price REAL,
                    roll_converted_cost REAL,
                    roll_last_seen_at INTEGER,
                    roll_age_seconds INTEGER,
                    roll_listing_count INTEGER,
                    roll_bucket_count INTEGER,
                    bucket_markup REAL,
                    target_markup REAL,
                    reference_reason TEXT,
                    youpin_template_id INTEGER,
                    youpin_live_price REAL,
                    youpin_live_time INTEGER,
                    youpin_age_seconds INTEGER,
                    is_fresh_pair INTEGER NOT NULL DEFAULT 0,
                    is_live_verified INTEGER NOT NULL DEFAULT 0,
                    verify_status TEXT,
                    verify_source TEXT,
                    verify_note TEXT,
                    mapping_match_level TEXT,
                    mapping_confidence REAL,
                    mapping_source TEXT,
                    mapping_version TEXT,
                    price_reference_median REAL,
                    price_mad REAL,
                    price_iqr REAL,
                    price_anomaly INTEGER NOT NULL DEFAULT 0,
                    price_anomaly_ratio REAL,
                    price_anomaly_note TEXT,
                    price_std_dev REAL,
                    price_trend REAL,
                    net_profit REAL,
                    net_profit_rate REAL,
                    is_valid INTEGER NOT NULL DEFAULT 1,
                    error_type TEXT,
                    last_checked_at INTEGER,
                    data_source TEXT NOT NULL DEFAULT 'serving',
                    confidence_score REAL NOT NULL DEFAULT 1.0,
                    calc_note TEXT,
                    built_at INTEGER NOT NULL,
                    PRIMARY KEY (run_id, market_key)
                );
                CREATE INDEX IF NOT EXISTS idx_serving_current_profit
                ON serving_comparisons (is_current, net_profit_rate DESC);
                CREATE TABLE IF NOT EXISTS comparison_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    snapshot_at INTEGER NOT NULL,
                    market_name TEXT NOT NULL,
                    source_market_name TEXT,
                    base_name TEXT NOT NULL,
                    wear_code TEXT NOT NULL,
                    variant_tag TEXT,
                    roll_raw_price REAL,
                    roll_converted_cost REAL,
                    youpin_sell_price REAL,
                    gross_profit_rate REAL,
                    net_profit_rate REAL,
                    roll_available_count INTEGER NOT NULL DEFAULT 0,
                    updated_at INTEGER NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_snap_time
                ON comparison_snapshots (snapshot_at DESC);

                CREATE INDEX IF NOT EXISTS idx_snap_market
                ON comparison_snapshots (market_name, snapshot_at DESC);

                CREATE TABLE IF NOT EXISTS verification_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_at INTEGER NOT NULL,
                    checked INTEGER NOT NULL,
                    ok_count INTEGER NOT NULL,
                    failed_count INTEGER NOT NULL,
                    details_json TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_verify_time
                ON verification_runs (run_at DESC);

                CREATE TABLE IF NOT EXISTS watchlist_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    watchlist_id TEXT NOT NULL,
                    raw_name TEXT NOT NULL,
                    cn_name TEXT,
                    en_name TEXT,
                    roll_market_name TEXT,
                    youpin_search_name TEXT,
                    wear_cn TEXT,
                    wear_en TEXT,
                    wear_abbr TEXT,
                    special_variant TEXT,
                    normalized_compare_key TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at INTEGER NOT NULL DEFAULT 0,
                    updated_at INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_watchlist_items_watchlist
                ON watchlist_items (watchlist_id, enabled, normalized_compare_key);
                """
            )
            self._ensure_roll_columns(cur)
            self._ensure_roll_daily_snapshot_schema(cur)
            self._ensure_youpin_columns(cur)
            self._ensure_comparison_columns(cur)
            self._ensure_serving_columns(cur)
            self._ensure_watchlist_schema(cur)
            try:
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_comp_profit ON comparisons (profit_rate DESC)"
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("Migration skipped for comparisons profit index: %s", exc)
            self._conn.commit()

    @staticmethod
    def _try_add_column(cur: sqlite3.Cursor, table: str, col: str, ddl: str) -> None:
        try:
            cur.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
        except Exception as exc:  # noqa: BLE001
            logger.warning("Migration skipped for %s.%s: %s", table, col, exc)

    def _ensure_roll_columns(self, cur: sqlite3.Cursor) -> None:
        existing = {row[1] for row in cur.execute("PRAGMA table_info(roll_trades)").fetchall()}
        if "variant_group" not in existing:
            self._try_add_column(
                cur, "roll_trades", "variant_group", "TEXT NOT NULL DEFAULT 'none'"
            )
        if "variant_label" not in existing:
            self._try_add_column(
                cur, "roll_trades", "variant_label", "TEXT NOT NULL DEFAULT 'NA'"
            )
        if "variant_tag" not in existing:
            self._try_add_column(
                cur, "roll_trades", "variant_tag", "TEXT NOT NULL DEFAULT 'NA'"
            )
        desired: dict[str, str] = {
            "is_special_candidate": "INTEGER NOT NULL DEFAULT 0",
            "is_special_confirmed": "INTEGER NOT NULL DEFAULT 0",
            "special_variant": "TEXT",
            "special_variant_type": "TEXT",
            "priority_score": "REAL NOT NULL DEFAULT 0",
            "detail_last_fetched_at": "INTEGER NOT NULL DEFAULT 0",
            "item_external_id": "TEXT",
            "item_variant_name": "TEXT",
            "item_category_names": "TEXT",
            "detail_title": "TEXT",
            "float_value": "REAL",
            "pattern_seed": "INTEGER",
            "paint_index": "INTEGER",
            "detail_price": "REAL",
            "sale_status": "TEXT",
            "detail_url": "TEXT",
            "detail_error": "TEXT",
        }
        for col, ddl in desired.items():
            if col not in existing:
                self._try_add_column(cur, "roll_trades", col, ddl)
        watchlist_cols: dict[str, str] = {
            "source_mode": "TEXT NOT NULL DEFAULT 'legacy'",
            "batch_id": "TEXT",
            "watchlist_id": "TEXT",
            "normalized_compare_key": "TEXT",
        }
        for col, ddl in watchlist_cols.items():
            if col not in existing:
                self._try_add_column(cur, "roll_trades", col, ddl)

    def _ensure_roll_daily_snapshot_schema(self, cur: sqlite3.Cursor) -> None:
        existing_info = cur.execute("PRAGMA table_info(roll_daily_snapshot)").fetchall()
        if not existing_info:
            return
        pk_cols = [
            str(row[1])
            for row in sorted(existing_info, key=lambda item: int(item[5] or 0))
            if int(row[5] or 0) > 0
        ]
        needs_recreate = pk_cols != ["source_market_name", "variant_label"]
        if needs_recreate:
            cur.execute("ALTER TABLE roll_daily_snapshot RENAME TO roll_daily_snapshot_legacy")
            cur.executescript(
                """
                CREATE TABLE roll_daily_snapshot (
                    source_market_name TEXT NOT NULL,
                    base_name TEXT NOT NULL,
                    wear_code TEXT NOT NULL,
                    wear_label TEXT NOT NULL,
                    variant_group TEXT NOT NULL DEFAULT 'none',
                    variant_label TEXT NOT NULL DEFAULT 'NA',
                    is_special_candidate INTEGER NOT NULL DEFAULT 0,
                    is_special_confirmed INTEGER NOT NULL DEFAULT 0,
                    special_variant TEXT,
                    special_variant_type TEXT,
                    priority_score REAL NOT NULL DEFAULT 0,
                    detail_last_fetched_at INTEGER NOT NULL DEFAULT 0,
                    detail_title TEXT,
                    float_value REAL,
                    pattern_seed INTEGER,
                    paint_index INTEGER,
                    detail_price REAL,
                    sale_status TEXT,
                    roll_reference_price REAL,
                    roll_max_markup REAL,
                    roll_bucket_count INTEGER NOT NULL DEFAULT 0,
                    roll_available_count INTEGER NOT NULL DEFAULT 0,
                    roll_listing_count INTEGER NOT NULL DEFAULT 0,
                    roll_last_seen_at INTEGER NOT NULL DEFAULT 0,
                    captured_at INTEGER NOT NULL,
                    PRIMARY KEY (source_market_name, variant_label)
                );
                """
            )
            cur.execute(
                """
                INSERT INTO roll_daily_snapshot (
                    source_market_name, base_name, wear_code, wear_label,
                    variant_group, variant_label,
                    is_special_candidate, is_special_confirmed, special_variant, special_variant_type,
                    priority_score, detail_last_fetched_at,
                    detail_title, float_value, pattern_seed, paint_index, detail_price, sale_status,
                    roll_reference_price, roll_max_markup, roll_bucket_count,
                    roll_available_count, roll_listing_count, roll_last_seen_at, captured_at
                )
                SELECT
                    source_market_name, base_name, wear_code, wear_label,
                    COALESCE(variant_group, 'none'),
                    COALESCE(variant_label, 'NA'),
                    0,
                    CASE WHEN COALESCE(variant_label, 'NA') != 'NA' THEN 1 ELSE 0 END,
                    CASE WHEN COALESCE(variant_label, 'NA') != 'NA' THEN variant_label ELSE NULL END,
                    CASE
                        WHEN COALESCE(variant_group, 'none') IN ('gem', 'phase')
                            THEN variant_group
                        WHEN COALESCE(variant_label, 'NA') != 'NA'
                            THEN 'finish_family'
                        ELSE NULL
                    END,
                    0,
                    0,
                    NULL,
                    NULL,
                    NULL,
                    NULL,
                    NULL,
                    NULL,
                    roll_reference_price, roll_max_markup, roll_bucket_count,
                    roll_available_count, roll_listing_count, roll_last_seen_at, captured_at
                FROM roll_daily_snapshot_legacy
                """
            )
            cur.execute("DROP TABLE roll_daily_snapshot_legacy")
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_roll_daily_snapshot_captured
                ON roll_daily_snapshot (captured_at DESC)
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_roll_daily_snapshot_price
                ON roll_daily_snapshot (roll_reference_price DESC, roll_last_seen_at DESC)
                """
            )
            existing_info = cur.execute("PRAGMA table_info(roll_daily_snapshot)").fetchall()
        existing = {row[1] for row in existing_info}
        desired: dict[str, str] = {
            "is_special_candidate": "INTEGER NOT NULL DEFAULT 0",
            "is_special_confirmed": "INTEGER NOT NULL DEFAULT 0",
            "special_variant": "TEXT",
            "special_variant_type": "TEXT",
            "priority_score": "REAL NOT NULL DEFAULT 0",
            "detail_last_fetched_at": "INTEGER NOT NULL DEFAULT 0",
            "detail_title": "TEXT",
            "float_value": "REAL",
            "pattern_seed": "INTEGER",
            "paint_index": "INTEGER",
            "detail_price": "REAL",
            "sale_status": "TEXT",
        }
        for col, ddl in desired.items():
            if col not in existing:
                self._try_add_column(cur, "roll_daily_snapshot", col, ddl)

    def _ensure_comparison_columns(self, cur: sqlite3.Cursor) -> None:
        existing = {
            row[1]
            for row in cur.execute("PRAGMA table_info(comparisons)").fetchall()
        }
        desired: dict[str, str] = {
            "source_market_name": "TEXT",
            "weapon_type": "TEXT",
            "is_special_candidate": "INTEGER NOT NULL DEFAULT 0",
            "is_special_confirmed": "INTEGER NOT NULL DEFAULT 0",
            "special_variant": "TEXT",
            "special_variant_type": "TEXT",
            "detail_last_fetched_at": "INTEGER NOT NULL DEFAULT 0",
            "detail_title": "TEXT",
            "float_value": "REAL",
            "pattern_seed": "INTEGER",
            "paint_index": "INTEGER",
            "detail_price": "REAL",
            "sale_status": "TEXT",
            "display_name_cn": "TEXT",
            "display_name_en": "TEXT",
            "display_name_final": "TEXT",
            "display_variant": "TEXT",
            "cn_name": "TEXT",
            "roll_raw_price": "REAL",
            "roll_converted_cost": "REAL",
            "youpin_sell_price": "REAL",
            "gross_profit": "REAL",
            "gross_profit_rate": "REAL",
            "net_sell_price": "REAL",
            "net_profit": "REAL",
            "net_profit_rate": "REAL",
            "variant_group": "TEXT",
            "variant_label": "TEXT",
            "variant_tag": "TEXT",
            "bucket_risk_level": "TEXT",
            "priority_score": "REAL",
            "roll_reference_price": "REAL",
            "roll_max_markup": "REAL",
            "roll_bucket_count": "INTEGER NOT NULL DEFAULT 0",
            "roll_available_count": "INTEGER NOT NULL DEFAULT 0",
            "roll_listing_count": "INTEGER NOT NULL DEFAULT 0",
            "price_status": "TEXT",
            "price_status_note": "TEXT",
            "roll_last_seen_at": "INTEGER NOT NULL DEFAULT 0",
            "youpin_price_updated_at": "INTEGER NOT NULL DEFAULT 0",
            "youpin_age_seconds": "INTEGER",
            "roll_age_seconds": "INTEGER",
            "is_youpin_fresh": "INTEGER NOT NULL DEFAULT 0",
            "is_roll_fresh": "INTEGER NOT NULL DEFAULT 0",
            "is_fresh_pair": "INTEGER NOT NULL DEFAULT 0",
            "is_live_verified": "INTEGER NOT NULL DEFAULT 0",
            "live_verify_price": "REAL",
            "live_verify_time": "INTEGER",
            "verify_status": "TEXT NOT NULL DEFAULT 'unverified'",
            "verify_source": "TEXT NOT NULL DEFAULT 'none'",
            "verify_time": "INTEGER",
            "verify_age_seconds": "INTEGER",
            "verified_fresh": "INTEGER NOT NULL DEFAULT 0",
            "verify_note": "TEXT",
            "mapping_match_level": "TEXT",
            "mapping_confidence": "REAL",
            "decision_grade": "TEXT NOT NULL DEFAULT 'X'",
            "decision_note": "TEXT",
            "data_confidence": "TEXT NOT NULL DEFAULT 'low'",
            "stale_reason": "TEXT",
            "reference_reason": "TEXT",
            "bucket_markup": "REAL",
            "target_markup": "REAL",
            "price_anomaly": "INTEGER NOT NULL DEFAULT 0",
            "price_anomaly_ratio": "REAL",
            "price_reference_median": "REAL",
            "price_mad": "REAL",
            "price_iqr": "REAL",
            "price_std_dev": "REAL",
            "price_trend": "REAL",
            "price_anomaly_note": "TEXT",
            "youpin_price": "REAL",
            "price_diff": "REAL",
            "profit_rate": "REAL",
            "is_valid": "INTEGER NOT NULL DEFAULT 1",
            "error_type": "TEXT",
            "last_checked_at": "INTEGER",
            "data_source": "TEXT NOT NULL DEFAULT 'comparisons'",
            "confidence_score": "REAL NOT NULL DEFAULT 1.0",
            "health_level": "TEXT NOT NULL DEFAULT 'healthy'",
            "data_status": "TEXT NOT NULL DEFAULT 'ok'",
            "audit_note": "TEXT",
            "calc_note": "TEXT NOT NULL DEFAULT ''",
            "updated_at": "INTEGER NOT NULL DEFAULT 0",
        }
        for col, ddl in desired.items():
            if col in existing:
                continue
            self._try_add_column(cur, "comparisons", col, ddl)
        # Smoothly upgrade legacy rows so variant text is part of display names.
        cur.execute(
            """
            UPDATE comparisons
            SET display_name_cn = CASE
                WHEN COALESCE(display_name_cn, '') != ''
                 AND COALESCE(display_variant, '') != ''
                 AND INSTR(display_name_cn, display_variant) = 0
                THEN display_name_cn || '（' || display_variant || '）'
                ELSE display_name_cn
            END
            """
        )
        cur.execute(
            """
            UPDATE comparisons
            SET display_name_en = CASE
                WHEN COALESCE(display_name_en, '') != ''
                 AND COALESCE(display_variant, '') != ''
                 AND INSTR(LOWER(display_name_en), LOWER(display_variant)) = 0
                THEN display_name_en || ' (' || display_variant || ')'
                ELSE display_name_en
            END
            """
        )
        # Keep backward compatibility for rows created before display_name_final existed.
        cur.execute(
            """
            UPDATE comparisons
            SET display_name_final = COALESCE(
                NULLIF(display_name_final, ''),
                NULLIF(display_name_cn, ''),
                NULLIF(display_name_en, ''),
                NULLIF(cn_name, ''),
                NULLIF(source_market_name, ''),
                market_name
            )
            WHERE display_name_final IS NULL OR display_name_final = ''
            """
        )
        cur.execute(
            """
            UPDATE comparisons
            SET weapon_type = CASE
                WHEN COALESCE(weapon_type, '') != '' THEN weapon_type
                WHEN INSTR(base_name, ' | ') > 0
                    THEN TRIM(REPLACE(SUBSTR(base_name, 1, INSTR(base_name, ' | ') - 1), '★', ''))
                ELSE TRIM(REPLACE(base_name, '★', ''))
            END
            WHERE weapon_type IS NULL OR weapon_type = ''
            """
        )
        cur.execute(
            """
            UPDATE comparisons
            SET price_status = CASE
                WHEN COALESCE(price_status, '') != '' THEN price_status
                WHEN youpin_sell_price IS NOT NULL THEN 'ok'
                ELSE 'fetch_failed'
            END
            WHERE price_status IS NULL OR price_status = ''
            """
        )
        cur.execute(
            """
            UPDATE comparisons
            SET price_status_note = CASE
                WHEN COALESCE(price_status_note, '') != '' THEN price_status_note
                WHEN youpin_sell_price IS NOT NULL THEN 'Price available'
                ELSE 'Status pending rebuild'
            END
            WHERE price_status_note IS NULL OR price_status_note = ''
            """
        )

    def _ensure_serving_columns(self, cur: sqlite3.Cursor) -> None:
        existing = {
            row[1]
            for row in cur.execute("PRAGMA table_info(serving_comparisons)").fetchall()
        }
        desired: dict[str, str] = {
            "is_special_candidate": "INTEGER NOT NULL DEFAULT 0",
            "is_special_confirmed": "INTEGER NOT NULL DEFAULT 0",
            "special_variant": "TEXT",
            "special_variant_type": "TEXT",
            "detail_last_fetched_at": "INTEGER NOT NULL DEFAULT 0",
            "float_value": "REAL",
            "pattern_seed": "INTEGER",
            "paint_index": "INTEGER",
            "detail_price": "REAL",
            "sale_status": "TEXT",
            "is_valid": "INTEGER NOT NULL DEFAULT 1",
            "error_type": "TEXT",
            "last_checked_at": "INTEGER",
            "data_source": "TEXT NOT NULL DEFAULT 'serving'",
            "confidence_score": "REAL NOT NULL DEFAULT 1.0",
        }
        for col, ddl in desired.items():
            if col not in existing:
                self._try_add_column(cur, "serving_comparisons", col, ddl)
        cur.execute(
            """
            UPDATE comparisons
            SET verify_status = COALESCE(NULLIF(verify_status, ''), 'unverified'),
                verify_source = COALESCE(NULLIF(verify_source, ''), 'none'),
                decision_grade = COALESCE(NULLIF(decision_grade, ''), 'X'),
                data_confidence = CASE
                    WHEN bucket_risk_level = 'low' THEN 'high'
                    WHEN bucket_risk_level = 'medium' THEN 'medium'
                    ELSE 'low'
                END
            WHERE verify_status IS NULL OR verify_status = ''
               OR verify_source IS NULL OR verify_source = ''
               OR decision_grade IS NULL OR decision_grade = ''
               OR data_confidence IS NULL OR data_confidence = ''
            """
        )
        cur.execute(
            """
            UPDATE comparisons
            SET price_anomaly = COALESCE(price_anomaly, 0)
            WHERE price_anomaly IS NULL
            """
        )

    def _ensure_youpin_columns(self, cur: sqlite3.Cursor) -> None:
        existing = {
            row[1]
            for row in cur.execute("PRAGMA table_info(youpin_prices)").fetchall()
        }
        desired: dict[str, str] = {
            "price_updated_at": "INTEGER NOT NULL DEFAULT 0",
            "last_error_kind": "TEXT",
            "last_error_message": "TEXT",
        }
        for col, ddl in desired.items():
            if col in existing:
                continue
            self._try_add_column(cur, "youpin_prices", col, ddl)
        watchlist_cols: dict[str, str] = {
            "source_mode": "TEXT NOT NULL DEFAULT 'legacy'",
            "batch_id": "TEXT",
            "watchlist_id": "TEXT",
            "normalized_compare_key": "TEXT",
            "status": "TEXT NOT NULL DEFAULT 'pending'",
        }
        for col, ddl in watchlist_cols.items():
            if col in existing:
                continue
            self._try_add_column(cur, "youpin_prices", col, ddl)

        cur.execute(
            """
            UPDATE youpin_prices
            SET price_updated_at = CASE
                WHEN price IS NOT NULL AND COALESCE(price_updated_at, 0) <= 0
                THEN COALESCE(updated_at, 0)
                ELSE COALESCE(price_updated_at, 0)
            END
            """
        )

    def _ensure_watchlist_schema(self, cur: sqlite3.Cursor) -> None:
        existing = {row[1] for row in cur.execute("PRAGMA table_info(comparisons)").fetchall()}
        watchlist_cols: dict[str, str] = {
            "source_mode": "TEXT NOT NULL DEFAULT 'legacy'",
            "batch_id": "TEXT",
            "watchlist_id": "TEXT",
            "normalized_compare_key": "TEXT",
        }
        for col, ddl in watchlist_cols.items():
            if col not in existing:
                self._try_add_column(cur, "comparisons", col, ddl)

    def get_state(self, key: str, default: str | None = None) -> str | None:
        with self._lock:
            cur = self._conn.execute("SELECT value FROM app_state WHERE key = ?", (key,))
            row = cur.fetchone()
            return row["value"] if row else default

    def set_state(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO app_state (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, value),
            )
            self._conn.commit()

    def upsert_roll_trades(
        self,
        trades: list[dict],
        now_ts: int,
        *,
        source_mode: str = "legacy",
        batch_id: str | None = None,
        watchlist_id: str | None = None,
    ) -> None:
        if not trades:
            return

        rows = []
        catalog_rows: dict[str, tuple] = {}
        catalog_seen_counts: dict[str, int] = {}
        trade_ids: list[str] = []
        seen_trade_ids: set[str] = set()
        for trade in trades:
            trade_id = str(trade.get("trade_id") or "").strip()
            if not trade_id or trade_id in seen_trade_ids:
                continue
            seen_trade_ids.add(trade_id)
            trade_ids.append(trade_id)
        existing_by_id: dict[str, dict] = {}
        if trade_ids:
            placeholders = ",".join("?" for _ in trade_ids)
            sql = f"SELECT * FROM roll_trades WHERE trade_id IN ({placeholders})"
            with self._lock:
                cur = self._conn.execute(sql, trade_ids)
                existing_by_id = {
                    str(row["trade_id"]): dict(row) for row in cur.fetchall()
                }
        for t in trades:
            existing = existing_by_id.get(str(t["trade_id"]))
            existing_confirmed = bool(existing and existing.get("is_special_confirmed"))
            existing_candidate = bool(existing and existing.get("is_special_candidate"))
            existing_variant_label = (
                str(existing.get("variant_label") or "NA").strip() if existing else "NA"
            )
            variant_group = (
                str(existing.get("variant_group") or "none").strip()
                if existing_confirmed and existing_variant_label != "NA"
                else str(t.get("variant_group") or "none").strip()
            )
            variant_label = (
                existing_variant_label
                if existing_confirmed and existing_variant_label != "NA"
                else str(t.get("variant_label") or "NA").strip()
            )
            variant_tag = (
                str(existing.get("variant_tag") or variant_label).strip()
                if existing_confirmed and existing_variant_label != "NA"
                else str(t.get("variant_tag") or variant_label).strip()
            )
            special_variant = (
                str(existing.get("special_variant") or "").strip()
                if existing and str(existing.get("special_variant") or "").strip()
                else str(t.get("special_variant") or "").strip()
            )
            special_variant_type = (
                str(existing.get("special_variant_type") or "").strip()
                if existing and str(existing.get("special_variant_type") or "").strip()
                else str(t.get("special_variant_type") or "").strip()
            )
            is_special_candidate = bool(
                t.get("is_special_candidate") or existing_candidate or existing_confirmed
            )
            is_special_confirmed = bool(existing_confirmed)
            detail_last_fetched_at = int(existing.get("detail_last_fetched_at") or 0) if existing else int(
                t.get("detail_last_fetched_at") or 0
            )
            old_price = None
            if existing and existing.get("price") is not None:
                try:
                    old_price = float(existing.get("price"))
                except (TypeError, ValueError):
                    old_price = None
            new_price = float(t["price"])
            price_change_ratio = 0.0
            if old_price and old_price > 0:
                price_change_ratio = abs(new_price - old_price) / old_price
            priority_score = 0.0
            if is_special_candidate:
                priority_score = compute_special_priority_score(
                    special_variant_type=special_variant_type,
                    never_fetched=detail_last_fetched_at <= 0,
                    price_change_ratio=price_change_ratio,
                    has_opportunity=False,
                    current_price=new_price,
                    markup_percent=float(t["markup_percent"]),
                )
            rows.append(
                (
                    t["trade_id"],
                    t["market_name"],
                    t["base_name"],
                    t["wear_code"],
                    t["wear_label"],
                    variant_group or "none",
                    variant_label or "NA",
                    variant_tag or "NA",
                    int(is_special_candidate),
                    int(is_special_confirmed),
                    special_variant or None,
                    special_variant_type or None,
                    float(priority_score),
                    detail_last_fetched_at,
                    str(
                        t.get("item_external_id")
                        or (existing.get("item_external_id") if existing else "")
                        or ""
                    ).strip()
                    or None,
                    str(
                        t.get("item_variant_name")
                        or (existing.get("item_variant_name") if existing else "")
                        or ""
                    ).strip()
                    or None,
                    str(
                        t.get("item_category_names")
                        or (existing.get("item_category_names") if existing else "")
                        or ""
                    ).strip()
                    or None,
                    new_price,
                    float(t["markup_percent"]),
                    t.get("status", "LISTED"),
                    source_mode,
                    batch_id,
                    watchlist_id,
                    str(t.get("normalized_compare_key") or "").strip() or None,
                    now_ts,
                    now_ts,
                )
            )
            sku_key = "|".join(
                [
                    str(t["market_name"]),
                    str(t["wear_code"]),
                    str(variant_label or "NA"),
                ]
            )
            catalog_seen_counts[sku_key] = catalog_seen_counts.get(sku_key, 0) + 1
            catalog_rows[sku_key] = (
                sku_key,
                t["market_name"],
                t["base_name"],
                t.get("weapon_type"),
                t["wear_code"],
                t["wear_label"],
                variant_group or "none",
                variant_label or "NA",
                now_ts,
                now_ts,
                new_price,
                float(t["markup_percent"]),
            )

        with self._lock:
            self._conn.executemany(
                """
                INSERT INTO roll_trades (
                    trade_id, market_name, base_name, wear_code, wear_label,
                    variant_group, variant_label, variant_tag,
                    is_special_candidate, is_special_confirmed, special_variant, special_variant_type,
                    priority_score, detail_last_fetched_at,
                    item_external_id, item_variant_name, item_category_names,
                    price, markup_percent, status,
                    source_mode, batch_id, watchlist_id, normalized_compare_key,
                    first_seen_at, last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(trade_id) DO UPDATE SET
                    market_name = excluded.market_name,
                    base_name = excluded.base_name,
                    wear_code = excluded.wear_code,
                    wear_label = excluded.wear_label,
                    variant_group = excluded.variant_group,
                    variant_label = excluded.variant_label,
                    variant_tag = excluded.variant_tag,
                    is_special_candidate = excluded.is_special_candidate,
                    is_special_confirmed = CASE
                        WHEN roll_trades.is_special_confirmed = 1 THEN 1
                        ELSE excluded.is_special_confirmed
                    END,
                    special_variant = COALESCE(excluded.special_variant, roll_trades.special_variant),
                    special_variant_type = COALESCE(excluded.special_variant_type, roll_trades.special_variant_type),
                    priority_score = excluded.priority_score,
                    detail_last_fetched_at = COALESCE(roll_trades.detail_last_fetched_at, excluded.detail_last_fetched_at),
                    item_external_id = COALESCE(excluded.item_external_id, roll_trades.item_external_id),
                    item_variant_name = COALESCE(excluded.item_variant_name, roll_trades.item_variant_name),
                    item_category_names = COALESCE(excluded.item_category_names, roll_trades.item_category_names),
                    price = excluded.price,
                    markup_percent = excluded.markup_percent,
                    status = excluded.status,
                    source_mode = excluded.source_mode,
                    batch_id = COALESCE(excluded.batch_id, roll_trades.batch_id),
                    watchlist_id = COALESCE(excluded.watchlist_id, roll_trades.watchlist_id),
                    normalized_compare_key = COALESCE(excluded.normalized_compare_key, roll_trades.normalized_compare_key),
                    last_seen_at = excluded.last_seen_at
                """,
                rows,
            )
            self._conn.executemany(
                """
                INSERT INTO product_catalog (
                sku_key, source_market_name, base_name, weapon_type, wear_code, wear_label,
                    variant_group, variant_label, first_seen_at, last_seen_at,
                    last_roll_price, last_markup_percent, seen_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(sku_key) DO UPDATE SET
                    source_market_name = excluded.source_market_name,
                    base_name = excluded.base_name,
                    weapon_type = COALESCE(excluded.weapon_type, product_catalog.weapon_type),
                    wear_code = excluded.wear_code,
                    wear_label = excluded.wear_label,
                    variant_group = excluded.variant_group,
                    variant_label = excluded.variant_label,
                    last_seen_at = excluded.last_seen_at,
                    last_roll_price = excluded.last_roll_price,
                    last_markup_percent = excluded.last_markup_percent,
                    seen_count = product_catalog.seen_count + excluded.seen_count,
                    is_active = 1
                """,
                [
                    (*row, catalog_seen_counts[sku_key])
                    for sku_key, row in catalog_rows.items()
                ],
            )
            self._conn.commit()

    def cleanup_roll_trades(self, expire_before_ts: int) -> int:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM roll_trades WHERE last_seen_at < ?",
                (expire_before_ts,),
            )
            self._conn.execute(
                """
                UPDATE product_catalog
                SET is_active = CASE
                    WHEN last_seen_at < ? THEN 0
                    ELSE is_active
                END
                """,
                (expire_before_ts,),
            )
            self._conn.commit()
            return cur.rowcount

    def get_roll_rows_by_market_names(
        self, market_names: list[str], min_seen_ts: int
    ) -> list[dict]:
        if not market_names:
            return []
        placeholders = ",".join("?" for _ in market_names)
        sql = (
            f"SELECT * FROM roll_trades WHERE status = 'LISTED' "
            f"AND last_seen_at >= ? AND market_name IN ({placeholders}) "
            f"ORDER BY price DESC, last_seen_at DESC, market_name ASC"
        )
        params = [min_seen_ts, *market_names]
        with self._lock:
            cur = self._conn.execute(sql, params)
            return [dict(row) for row in cur.fetchall()]

    def sort_market_names_by_roll_price(
        self, market_names: list[str], min_seen_ts: int
    ) -> list[str]:
        if not market_names:
            return []
        deduped: list[str] = []
        seen: set[str] = set()
        for name in market_names:
            key = str(name or "").strip()
            if not key or key in seen:
                continue
            seen.add(key)
            deduped.append(key)
        if not deduped:
            return []
        placeholders = ",".join("?" for _ in deduped)
        sql = f"""
            SELECT market_name, MAX(price) AS max_price, MAX(last_seen_at) AS last_seen_at
            FROM roll_trades
            WHERE status = 'LISTED'
              AND last_seen_at >= ?
              AND market_name IN ({placeholders})
            GROUP BY market_name
            ORDER BY max_price DESC, last_seen_at DESC, market_name ASC
        """
        params = [min_seen_ts, *deduped]
        with self._lock:
            cur = self._conn.execute(sql, params)
            ordered = [str(row["market_name"]) for row in cur.fetchall()]
        missing = [name for name in deduped if name not in set(ordered)]
        return [*ordered, *missing]

    def get_active_market_names(self, min_seen_ts: int, limit: int) -> list[str]:
        with self._lock:
            cur = self._conn.execute(
                """
                SELECT market_name
                FROM roll_trades
                WHERE status = 'LISTED' AND last_seen_at >= ?
                GROUP BY market_name
                ORDER BY MAX(price) DESC, MAX(last_seen_at) DESC, market_name ASC
                LIMIT ?
                """,
                (min_seen_ts, limit),
            )
            return [row["market_name"] for row in cur.fetchall()]

    def list_priority_market_names(self, limit: int) -> list[tuple[str, float]]:
        safe_limit = max(1, min(5000, int(limit)))
        with self._lock:
            cur = self._conn.execute(
                """
                SELECT
                    source_market_name,
                    MAX(COALESCE(priority_score, 0)) AS ps
                FROM comparisons
                WHERE source_market_name IS NOT NULL
                GROUP BY source_market_name
                ORDER BY ps DESC, MAX(updated_at) DESC
                LIMIT ?
                """,
                (safe_limit,),
            )
            return [(str(row["source_market_name"]), float(row["ps"] or 0.0)) for row in cur.fetchall()]

    def list_unpriced_comparison_market_names(self, limit: int) -> list[tuple[str, float]]:
        safe_limit = max(1, min(5000, int(limit)))
        with self._lock:
            cur = self._conn.execute(
                """
                SELECT
                    source_market_name,
                    MAX(COALESCE(priority_score, 0)) AS ps
                FROM comparisons
                WHERE source_market_name IS NOT NULL
                  AND source_market_name != ''
                  AND (
                    youpin_sell_price IS NULL
                    OR COALESCE(price_status, '') IN ('no_cached_price', 'verify_failed')
                  )
                GROUP BY source_market_name
                ORDER BY
                    MAX(COALESCE(is_roll_fresh, 0)) DESC,
                    ps DESC,
                    MAX(COALESCE(roll_last_seen_at, 0)) DESC,
                    MAX(COALESCE(updated_at, 0)) DESC,
                    MAX(COALESCE(roll_reference_price, 0)) DESC,
                    source_market_name ASC
                LIMIT ?
                """,
                (safe_limit,),
            )
            return [
                (str(row["source_market_name"]), float(row["ps"] or 0.0))
                for row in cur.fetchall()
            ]

    def list_comparison_market_names(self, limit: int) -> list[str]:
        safe_limit = max(1, min(500000, int(limit)))
        with self._lock:
            cur = self._conn.execute(
                """
                SELECT COALESCE(NULLIF(source_market_name, ''), market_name) AS compare_name
                FROM comparisons
                WHERE COALESCE(NULLIF(source_market_name, ''), market_name) IS NOT NULL
                GROUP BY compare_name
                ORDER BY MAX(updated_at) DESC, compare_name ASC
                LIMIT ?
                """,
                (safe_limit,),
            )
            return [str(row["compare_name"]) for row in cur.fetchall()]

    def list_recent_special_market_names(self, limit: int) -> list[str]:
        safe_limit = max(1, min(5000, int(limit)))
        with self._lock:
            cur = self._conn.execute(
                """
                SELECT market_name
                FROM (
                    SELECT source_market_name AS market_name, updated_at AS sort_ts
                    FROM comparisons
                    WHERE source_market_name IS NOT NULL
                      AND (
                        COALESCE(variant_label, variant_tag, 'NA') != 'NA'
                        OR COALESCE(is_special_candidate, 0) = 1
                      )
                    UNION ALL
                    SELECT market_name, last_seen_at AS sort_ts
                    FROM roll_trades
                    WHERE status = 'LISTED'
                      AND COALESCE(is_special_candidate, 0) = 1
                )
                WHERE market_name IS NOT NULL AND market_name != ''
                GROUP BY market_name
                ORDER BY MAX(sort_ts) DESC
                LIMIT ?
                """,
                (safe_limit,),
            )
            return [str(row["market_name"]) for row in cur.fetchall()]

    def list_special_detail_candidates(
        self,
        *,
        now_ts: int,
        limit: int,
        candidate_ttl_seconds: int,
        confirmed_ttl_seconds: int,
        price_change_ratio: float,
        opportunity_profit_threshold: float,
    ) -> list[dict]:
        safe_limit = max(1, min(5000, int(limit)))
        fetch_limit = max(safe_limit * 8, 200)
        with self._lock:
            cur = self._conn.execute(
                """
                SELECT
                    rt.*,
                    MAX(COALESCE(c.priority_score, 0)) AS comparison_priority_score,
                    MAX(COALESCE(c.net_profit_rate, -999999)) AS comparison_net_profit_rate
                FROM roll_trades rt
                LEFT JOIN comparisons c
                  ON c.source_market_name = rt.market_name
                WHERE rt.status = 'LISTED'
                  AND COALESCE(rt.is_special_candidate, 0) = 1
                GROUP BY rt.trade_id
                ORDER BY rt.last_seen_at DESC, rt.price DESC, rt.market_name ASC
                LIMIT ?
                """,
                (fetch_limit,),
            )
            rows = [dict(row) for row in cur.fetchall()]

        out: list[dict] = []
        grouped_rows: dict[str, list[dict]] = {}
        for row in rows:
            market_name = str(row.get("market_name") or "").strip()
            if not market_name:
                continue
            grouped_rows.setdefault(market_name, []).append(row)

        for market_name, listings in grouped_rows.items():
            sample = max(
                listings,
                key=lambda row: (
                    float(row.get("comparison_priority_score") or 0.0),
                    float(row.get("priority_score") or 0.0),
                    int(row.get("last_seen_at") or 0),
                    float(row.get("price") or 0.0),
                    str(row.get("trade_id") or ""),
                ),
            )
            group_detail_last_fetched_at = max(
                int(row.get("detail_last_fetched_at") or 0) for row in listings
            )
            group_is_confirmed = any(bool(row.get("is_special_confirmed")) for row in listings)
            ttl_seconds = (
                int(confirmed_ttl_seconds) if group_is_confirmed else int(candidate_ttl_seconds)
            )
            opportunity_refresh_floor = min(1800, max(300, ttl_seconds // 6))
            detail_prices: list[float] = []
            current_prices: list[float] = []
            for row in listings:
                detail_price = row.get("detail_price")
                current_price = row.get("price")
                try:
                    detail_price_value = (
                        float(detail_price) if detail_price is not None else None
                    )
                except (TypeError, ValueError):
                    detail_price_value = None
                try:
                    current_price_value = (
                        float(current_price) if current_price is not None else None
                    )
                except (TypeError, ValueError):
                    current_price_value = None
                if detail_price_value is not None and detail_price_value > 0:
                    detail_prices.append(detail_price_value)
                if current_price_value is not None:
                    current_prices.append(current_price_value)

            ratio = 0.0
            if detail_prices and current_prices:
                detail_price_value = min(detail_prices)
                current_price_value = max(current_prices)
                ratio = abs(current_price_value - detail_price_value) / detail_price_value
            else:
                current_price_value = max(current_prices) if current_prices else None

            comparison_opportunity = any(
                float(row.get("comparison_net_profit_rate") or -999999)
                >= float(opportunity_profit_threshold)
                for row in listings
            )
            is_due = (
                group_detail_last_fetched_at <= 0
                or group_detail_last_fetched_at <= int(now_ts) - ttl_seconds
                or ratio >= float(price_change_ratio)
                or (
                    comparison_opportunity
                    and group_detail_last_fetched_at
                    <= int(now_ts) - opportunity_refresh_floor
                )
            )
            if not is_due:
                continue

            runtime_priority = compute_special_priority_score(
                special_variant_type=sample.get("special_variant_type"),
                never_fetched=group_detail_last_fetched_at <= 0,
                price_change_ratio=ratio,
                has_opportunity=comparison_opportunity,
                current_price=current_price_value,
                markup_percent=sample.get("markup_percent"),
            ) + (max(float(row.get("comparison_priority_score") or 0.0) for row in listings) * 0.25)
            priority_target_boost = priority_special_score_boost(
                market_name=sample.get("market_name"),
                wear_code=sample.get("wear_code"),
                item_variant_name=sample.get("item_variant_name"),
                detail_title=sample.get("detail_title"),
                special_variant=sample.get("special_variant"),
            )
            runtime_priority += priority_target_boost
            if group_is_confirmed:
                runtime_priority += 8.0
            sample["is_priority_special_target"] = bool(priority_target_boost > 0)
            sample["runtime_priority_score"] = round(runtime_priority, 4)
            sample["comparison_opportunity"] = comparison_opportunity
            sample["price_change_ratio"] = round(ratio, 6)
            sample["detail_last_fetched_at"] = group_detail_last_fetched_at
            sample["is_special_confirmed"] = int(group_is_confirmed)
            sample["special_group_size"] = len(listings)
            out.append(sample)

        out.sort(
            key=lambda row: (
                float(row.get("runtime_priority_score") or 0.0),
                int(row.get("last_seen_at") or 0),
                float(row.get("price") or 0.0),
                str(row.get("trade_id") or ""),
            ),
            reverse=True,
        )
        return out[:safe_limit]

    def update_special_trade_detail(
        self,
        *,
        trade_id: str,
        fetched_at: int,
        is_confirmed: bool,
        special_variant: str | None,
        special_variant_type: str | None,
        detail_title: str | None,
        float_value: float | None,
        pattern_seed: int | None,
        paint_index: int | None,
        detail_price: float | None,
        sale_status: str | None,
        detail_url: str | None,
        detail_error: str | None,
        priority_score: float | None = None,
    ) -> None:
        variant_value = str(special_variant or "").strip()
        variant_type_value = str(special_variant_type or "").strip()
        confirmed_group = "finish_family"
        if variant_type_value in {"gem", "phase"}:
            confirmed_group = variant_type_value
        confirmed_label = variant_value or "NA"
        with self._lock:
            self._conn.execute(
                """
                UPDATE roll_trades
                SET
                    is_special_candidate = 1,
                    is_special_confirmed = CASE
                        WHEN ? = 1 THEN 1
                        ELSE COALESCE(is_special_confirmed, 0)
                    END,
                    special_variant = COALESCE(?, special_variant),
                    special_variant_type = COALESCE(?, special_variant_type),
                    variant_group = CASE
                        WHEN ? = 1 THEN ?
                        ELSE variant_group
                    END,
                    variant_label = CASE
                        WHEN ? = 1 THEN ?
                        ELSE variant_label
                    END,
                    variant_tag = CASE
                        WHEN ? = 1 THEN ?
                        ELSE variant_tag
                    END,
                    priority_score = COALESCE(?, priority_score),
                    detail_last_fetched_at = ?,
                    detail_title = COALESCE(?, detail_title),
                    float_value = COALESCE(?, float_value),
                    pattern_seed = COALESCE(?, pattern_seed),
                    paint_index = COALESCE(?, paint_index),
                    detail_price = COALESCE(?, detail_price),
                    sale_status = COALESCE(?, sale_status),
                    detail_url = COALESCE(?, detail_url),
                    detail_error = ?
                WHERE trade_id = ?
                """,
                (
                    int(bool(is_confirmed)),
                    variant_value or None,
                    variant_type_value or None,
                    int(bool(is_confirmed)),
                    confirmed_group,
                    int(bool(is_confirmed)),
                    confirmed_label,
                    int(bool(is_confirmed)),
                    confirmed_label,
                    priority_score,
                    int(fetched_at),
                    detail_title,
                    float_value,
                    pattern_seed,
                    paint_index,
                    detail_price,
                    sale_status,
                    detail_url,
                    detail_error,
                    trade_id,
                ),
            )
            self._conn.commit()

    def create_batch_id(self) -> str:
        return f"batch_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}_{uuid4().hex[:8]}"

    def replace_watchlist_items(self, watchlist_id: str, items: list[dict], now_ts: int) -> int:
        with self._lock:
            self._conn.execute("DELETE FROM watchlist_items WHERE watchlist_id = ?", (watchlist_id,))
            payload = []
            for item in items:
                payload.append(
                    (
                        watchlist_id,
                        str(item.get("raw_name") or "").strip(),
                        item.get("cn_name"),
                        item.get("en_name"),
                        item.get("roll_market_name"),
                        item.get("youpin_search_name"),
                        item.get("wear_cn"),
                        item.get("wear_en"),
                        item.get("wear_abbr"),
                        normalize_special_variant(item.get("special_variant")),
                        str(item.get("normalized_compare_key") or "").strip(),
                        1 if item.get("enabled", True) else 0,
                        now_ts,
                        now_ts,
                    )
                )
            self._conn.executemany(
                """
                INSERT INTO watchlist_items (
                    watchlist_id, raw_name, cn_name, en_name, roll_market_name, youpin_search_name,
                    wear_cn, wear_en, wear_abbr, special_variant, normalized_compare_key, enabled, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                payload,
            )
            self._conn.commit()
            return len(payload)

    def list_watchlist_items(self, watchlist_id: str, enabled_only: bool = True) -> list[dict]:
        sql = "SELECT * FROM watchlist_items WHERE watchlist_id = ?"
        params: list = [watchlist_id]
        if enabled_only:
            sql += " AND enabled = 1"
        sql += " ORDER BY id ASC"
        with self._lock:
            cur = self._conn.execute(sql, tuple(params))
            return [dict(row) for row in cur.fetchall()]

    def clear_watchlist_results(self, watchlist_id: str) -> dict:
        with self._lock:
            roll_deleted = self._conn.execute(
                "DELETE FROM roll_trades WHERE source_mode = 'watchlist' AND COALESCE(watchlist_id,'') = ?",
                (watchlist_id,),
            ).rowcount
            yp_deleted = self._conn.execute(
                "DELETE FROM youpin_prices WHERE source_mode = 'watchlist' AND COALESCE(watchlist_id,'') = ?",
                (watchlist_id,),
            ).rowcount
            comp_deleted = self._conn.execute(
                "DELETE FROM comparisons WHERE source_mode = 'watchlist' AND COALESCE(watchlist_id,'') = ?",
                (watchlist_id,),
            ).rowcount
            self._conn.commit()
        return {"roll_trades": int(roll_deleted), "youpin_prices": int(yp_deleted), "comparisons": int(comp_deleted)}

    def watchlist_progress_stats(self, watchlist_id: str, batch_id: str | None = None) -> dict:
        with self._lock:
            total = int(
                self._conn.execute(
                    "SELECT COUNT(*) FROM watchlist_items WHERE watchlist_id = ? AND enabled = 1",
                    (watchlist_id,),
                ).fetchone()[0]
                or 0
            )
            roll_sql = (
                "SELECT COUNT(DISTINCT normalized_compare_key) FROM roll_trades "
                "WHERE source_mode = 'watchlist' AND COALESCE(watchlist_id,'') = ? "
                "AND COALESCE(normalized_compare_key,'') != ''"
            )
            yp_sql = (
                "SELECT COUNT(DISTINCT normalized_compare_key) FROM youpin_prices "
                "WHERE source_mode = 'watchlist' AND COALESCE(watchlist_id,'') = ? "
                "AND COALESCE(normalized_compare_key,'') != '' AND price IS NOT NULL AND price > 0"
            )
            fail_sql = (
                "SELECT COUNT(*) FROM youpin_prices "
                "WHERE source_mode = 'watchlist' AND COALESCE(watchlist_id,'') = ? "
                "AND COALESCE(status,'') NOT IN ('ok','pending','')"
            )
            params: list = [watchlist_id]
            if batch_id:
                roll_sql += " AND COALESCE(batch_id,'') = ?"
                yp_sql += " AND COALESCE(batch_id,'') = ?"
                fail_sql += " AND COALESCE(batch_id,'') = ?"
                params.append(batch_id)
            roll_done = int(self._conn.execute(roll_sql, tuple(params)).fetchone()[0] or 0)
            yp_done = int(self._conn.execute(yp_sql, tuple(params)).fetchone()[0] or 0)
            failed = int(self._conn.execute(fail_sql, tuple(params)).fetchone()[0] or 0)
            compare_ok = int(
                self._conn.execute(
                    "SELECT COUNT(*) FROM comparisons WHERE source_mode = 'watchlist' AND COALESCE(watchlist_id,'') = ?",
                    (watchlist_id,),
                ).fetchone()[0]
                or 0
            )
        pending = max(0, total - yp_done)
        return {
            "watchlist_total": total,
            "roll_done_count": roll_done,
            "youpin_done_count": yp_done,
            "compare_ok_count": compare_ok,
            "failed_count": failed,
            "pending_count": pending,
        }

    def list_recent_verified_success_market_names(self, limit: int) -> list[str]:
        safe_limit = max(1, min(5000, int(limit)))
        latest = self.get_latest_verification_run()
        if not latest:
            return []
        raw = latest.get("details_json")
        if not raw:
            return []
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return []
        names: list[str] = []
        for item in parsed:
            if not isinstance(item, dict):
                continue
            if not item.get("ok"):
                continue
            source_name = str(item.get("source_market_name") or "").strip()
            if source_name:
                names.append(source_name)
                continue
            market_name = str(item.get("market_name") or "").strip()
            if market_name:
                names.append(market_name)
        out: list[str] = []
        seen: set[str] = set()
        for name in names:
            if name in seen:
                continue
            seen.add(name)
            out.append(name)
            if len(out) >= safe_limit:
                break
        return out

    def list_catalog_backfill_market_names(self, limit: int) -> list[str]:
        safe_limit = max(1, min(5000, int(limit)))
        with self._lock:
            cur = self._conn.execute(
                """
                SELECT source_market_name
                FROM product_catalog
                WHERE source_market_name IS NOT NULL
                GROUP BY source_market_name
                ORDER BY
                    MAX(COALESCE(is_active, 0)) DESC,
                    MIN(COALESCE(last_seen_at, 0)) ASC,
                    MAX(COALESCE(seen_count, 0)) DESC,
                    MAX(COALESCE(last_roll_price, 0)) DESC,
                    source_market_name ASC
                LIMIT ?
                """,
                (safe_limit,),
            )
            return [str(row["source_market_name"]) for row in cur.fetchall()]

    def replace_roll_daily_snapshot(self, rows: list[dict], *, captured_at: int) -> int:
        payload = [
            (
                str(row["source_market_name"]),
                str(row["base_name"]),
                str(row["wear_code"]),
                str(row["wear_label"]),
                str(row.get("variant_group") or "none"),
                str(row.get("variant_label") or "NA"),
                int(bool(row.get("is_special_candidate"))),
                int(bool(row.get("is_special_confirmed"))),
                row.get("special_variant"),
                row.get("special_variant_type"),
                float(row.get("priority_score") or 0.0),
                int(row.get("detail_last_fetched_at") or 0),
                row.get("detail_title"),
                row.get("float_value"),
                row.get("pattern_seed"),
                row.get("paint_index"),
                row.get("detail_price"),
                row.get("sale_status"),
                row.get("roll_reference_price"),
                row.get("roll_max_markup"),
                int(row.get("roll_bucket_count") or 0),
                int(row.get("roll_available_count", row.get("roll_listing_count", 0)) or 0),
                int(row.get("roll_listing_count") or 0),
                int(row.get("roll_last_seen_at") or 0),
                int(captured_at),
            )
            for row in rows
            if str(row.get("source_market_name") or "").strip()
        ]
        with self._lock:
            self._conn.execute("DELETE FROM roll_daily_snapshot")
            if payload:
                self._conn.executemany(
                    """
                    INSERT INTO roll_daily_snapshot (
                        source_market_name, base_name, wear_code, wear_label,
                        variant_group, variant_label,
                        is_special_candidate, is_special_confirmed, special_variant, special_variant_type,
                        priority_score, detail_last_fetched_at,
                        detail_title, float_value, pattern_seed, paint_index, detail_price, sale_status,
                        roll_reference_price, roll_max_markup, roll_bucket_count,
                        roll_available_count, roll_listing_count, roll_last_seen_at,
                        captured_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    payload,
                )
            self._conn.commit()
            return len(payload)

    def get_roll_daily_snapshot_rows_by_market_names(
        self, market_names: list[str]
    ) -> list[dict]:
        if not market_names:
            return []
        deduped: list[str] = []
        seen: set[str] = set()
        for name in market_names:
            key = str(name or "").strip()
            if not key or key in seen:
                continue
            seen.add(key)
            deduped.append(key)
        if not deduped:
            return []
        placeholders = ",".join("?" for _ in deduped)
        sql = (
            "SELECT * FROM roll_daily_snapshot "
            f"WHERE source_market_name IN ({placeholders}) "
            "ORDER BY roll_reference_price DESC, roll_last_seen_at DESC, source_market_name ASC"
        )
        with self._lock:
            cur = self._conn.execute(sql, deduped)
            return [dict(row) for row in cur.fetchall()]

    def list_roll_daily_market_names(self, limit: int) -> list[str]:
        safe_limit = max(1, min(500000, int(limit)))
        with self._lock:
            cur = self._conn.execute(
                """
                SELECT source_market_name
                FROM roll_daily_snapshot
                GROUP BY source_market_name
                ORDER BY MAX(roll_reference_price) DESC, MAX(roll_last_seen_at) DESC, source_market_name ASC
                LIMIT ?
                """,
                (safe_limit,),
            )
            return [str(row["source_market_name"]) for row in cur.fetchall()]

    def sort_roll_daily_market_names_by_price(self, market_names: list[str]) -> list[str]:
        if not market_names:
            return []
        deduped: list[str] = []
        seen: set[str] = set()
        for name in market_names:
            key = str(name or "").strip()
            if not key or key in seen:
                continue
            seen.add(key)
            deduped.append(key)
        if not deduped:
            return []
        placeholders = ",".join("?" for _ in deduped)
        sql = f"""
            SELECT source_market_name,
                   MAX(roll_reference_price) AS max_price,
                   MAX(roll_last_seen_at) AS last_seen_at
            FROM roll_daily_snapshot
            WHERE source_market_name IN ({placeholders})
            GROUP BY source_market_name
            ORDER BY max_price DESC, last_seen_at DESC, source_market_name ASC
        """
        with self._lock:
            cur = self._conn.execute(sql, deduped)
            ordered = [str(row["source_market_name"]) for row in cur.fetchall()]
        missing = [name for name in deduped if name not in set(ordered)]
        return [*ordered, *missing]

    def get_roll_daily_snapshot_count(self) -> int:
        with self._lock:
            cur = self._conn.execute("SELECT COUNT(*) FROM roll_daily_snapshot")
            row = cur.fetchone()
            return int(row[0] if row else 0)

    def get_roll_daily_snapshot_last_captured_at(self) -> int:
        with self._lock:
            cur = self._conn.execute("SELECT MAX(captured_at) FROM roll_daily_snapshot")
            row = cur.fetchone()
            return int((row[0] if row else 0) or 0)

    def get_youpin_record(self, template_id: int) -> dict | None:
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM youpin_prices WHERE template_id = ?",
                (template_id,),
            )
            row = cur.fetchone()
            return dict(row) if row else None

    def upsert_youpin_price(
        self,
        template_id: int,
        market_name: str,
        cn_name: str | None,
        commodity_name: str | None,
        price: float | None,
        updated_at: int,
        raw_json: str | None,
        *,
        source_mode: str = "legacy",
        batch_id: str | None = None,
        watchlist_id: str | None = None,
        normalized_compare_key: str | None = None,
        status: str = "ok",
    ) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO youpin_prices (
                    template_id, market_name, cn_name, commodity_name,
                    price, price_updated_at, updated_at, fail_count, next_retry_at,
                    last_error_kind, last_error_message, raw_json,
                    source_mode, batch_id, watchlist_id, normalized_compare_key, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, 0, NULL, NULL, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(template_id) DO UPDATE SET
                    market_name = excluded.market_name,
                    cn_name = excluded.cn_name,
                    commodity_name = excluded.commodity_name,
                    price = excluded.price,
                    price_updated_at = excluded.price_updated_at,
                    updated_at = excluded.updated_at,
                    fail_count = 0,
                    next_retry_at = 0,
                    last_error_kind = NULL,
                    last_error_message = NULL,
                    raw_json = excluded.raw_json,
                    source_mode = excluded.source_mode,
                    batch_id = excluded.batch_id,
                    watchlist_id = excluded.watchlist_id,
                    normalized_compare_key = excluded.normalized_compare_key,
                    status = excluded.status
                """,
                (
                    template_id,
                    market_name,
                    cn_name,
                    commodity_name,
                    price,
                    updated_at,
                    updated_at,
                    raw_json,
                    source_mode,
                    batch_id,
                    watchlist_id,
                    normalized_compare_key,
                    status,
                ),
            )
            if price is not None and float(price) > 0:
                self._conn.execute(
                    """
                    INSERT INTO youpin_price_history (
                        template_id, price, observed_at, source, raw_json
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        template_id,
                        float(price),
                        int(updated_at),
                        "cache",
                        raw_json,
                    ),
                )
            self._conn.commit()

    def record_youpin_price_history(
        self,
        *,
        template_id: int,
        price: float | None,
        observed_at: int,
        source: str,
        raw_json: str | None = None,
    ) -> None:
        if price is None or float(price) <= 0:
            return
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO youpin_price_history (
                    template_id, price, observed_at, source, raw_json
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    int(template_id),
                    float(price),
                    int(observed_at),
                    str(source),
                    raw_json,
                ),
            )
            self._conn.commit()

    def get_recent_youpin_prices(self, template_id: int, limit: int) -> list[float]:
        safe_limit = max(1, min(500, int(limit)))
        with self._lock:
            cur = self._conn.execute(
                """
                SELECT price
                FROM youpin_price_history
                WHERE template_id = ? AND price > 0
                ORDER BY observed_at DESC, id DESC
                LIMIT ?
                """,
                (int(template_id), safe_limit),
            )
            return [float(row["price"]) for row in cur.fetchall()]

    def mark_youpin_failure(
        self,
        template_id: int,
        market_name: str,
        cn_name: str | None,
        now_ts: int,
        retry_after_seconds: int,
        *,
        error_kind: str = "error",
        error_message: str | None = None,
        source_mode: str = "legacy",
        batch_id: str | None = None,
        watchlist_id: str | None = None,
        normalized_compare_key: str | None = None,
        status: str = "request_failed",
    ) -> None:
        existing = self.get_youpin_record(template_id)
        fail_count = int(existing["fail_count"]) + 1 if existing else 1
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO youpin_prices (
                    template_id, market_name, cn_name, commodity_name,
                    price, price_updated_at, updated_at, fail_count, next_retry_at,
                    last_error_kind, last_error_message, raw_json,
                    source_mode, batch_id, watchlist_id, normalized_compare_key, status
                ) VALUES (?, ?, ?, NULL, NULL, 0, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?)
                ON CONFLICT(template_id) DO UPDATE SET
                    market_name = excluded.market_name,
                    cn_name = excluded.cn_name,
                    fail_count = ?,
                    next_retry_at = ?,
                    updated_at = ?,
                    last_error_kind = excluded.last_error_kind,
                    last_error_message = excluded.last_error_message,
                    source_mode = excluded.source_mode,
                    batch_id = excluded.batch_id,
                    watchlist_id = excluded.watchlist_id,
                    normalized_compare_key = excluded.normalized_compare_key,
                    status = excluded.status
                """,
                (
                    template_id,
                    market_name,
                    cn_name,
                    now_ts,
                    fail_count,
                    now_ts + retry_after_seconds,
                    error_kind,
                    error_message,
                    source_mode,
                    batch_id,
                    watchlist_id,
                    normalized_compare_key,
                    status,
                    fail_count,
                    now_ts + retry_after_seconds,
                    now_ts,
                ),
            )
            self._conn.commit()

    def upsert_comparisons(self, rows: list[dict]) -> None:
        if not rows:
            return
        payload = []
        for r in rows:
            payload.append(
                (
                    r["market_name"],
                    r.get("source_market_name"),
                    r["base_name"],
                    r.get("weapon_type"),
                    r["wear_code"],
                    r["wear_label"],
                    r.get("variant_group"),
                    r.get("variant_label"),
                    r.get("variant_tag"),
                    int(bool(r.get("is_special_candidate", False))),
                    int(bool(r.get("is_special_confirmed", False))),
                    r.get("special_variant"),
                    r.get("special_variant_type"),
                    int(r.get("detail_last_fetched_at") or 0),
                    r.get("detail_title"),
                    r.get("float_value"),
                    r.get("pattern_seed"),
                    r.get("paint_index"),
                    r.get("detail_price"),
                    r.get("sale_status"),
                    r.get("display_name_cn"),
                    r.get("display_name_en"),
                    r.get("display_name_final"),
                    r.get("display_variant"),
                    r.get("cn_name"),
                    r.get("roll_raw_price"),
                    r.get("roll_converted_cost"),
                    r.get("youpin_sell_price"),
                    r.get("gross_profit"),
                    r.get("gross_profit_rate"),
                    r.get("net_sell_price"),
                    r.get("net_profit"),
                    r.get("net_profit_rate"),
                    r.get("bucket_risk_level"),
                    r.get("priority_score"),
                    r.get("roll_reference_price"),
                    r.get("roll_max_markup"),
                    r.get("roll_bucket_count", 0),
                    r.get("roll_available_count", r.get("roll_listing_count", 0)),
                    r.get("roll_listing_count", 0),
                    r.get("price_status"),
                    r.get("price_status_note"),
                    r.get("roll_last_seen_at", 0),
                    r.get("youpin_price_updated_at", 0),
                    r.get("youpin_age_seconds"),
                    r.get("roll_age_seconds"),
                    int(bool(r.get("is_youpin_fresh", False))),
                    int(bool(r.get("is_roll_fresh", False))),
                    int(bool(r.get("is_fresh_pair", False))),
                    int(bool(r.get("is_live_verified", False))),
                    r.get("live_verify_price"),
                    r.get("live_verify_time"),
                    r.get("verify_status", "unverified"),
                    r.get("verify_source", "none"),
                    r.get("verify_time"),
                    r.get("verify_age_seconds"),
                    int(bool(r.get("verified_fresh", False))),
                    r.get("verify_note"),
                    r.get("mapping_match_level"),
                    r.get("mapping_confidence"),
                    r.get("decision_grade", "X"),
                    r.get("decision_note"),
                    r.get("data_confidence", "low"),
                    r.get("stale_reason"),
                    r.get("reference_reason"),
                    r.get("bucket_markup"),
                    r.get("target_markup"),
                    int(bool(r.get("price_anomaly", False))),
                    r.get("price_anomaly_ratio"),
                    r.get("price_reference_median"),
                    r.get("price_mad"),
                    r.get("price_iqr"),
                    r.get("price_std_dev"),
                    r.get("price_trend"),
                    r.get("price_anomaly_note"),
                    r.get("youpin_price"),
                    r.get("price_diff"),
                    r.get("profit_rate"),
                    r.get("calc_note", ""),
                    r["updated_at"],
                )
            )

        with self._lock:
            placeholders = ", ".join("?" for _ in range(len(payload[0])))
            sql = (
                """
                INSERT INTO comparisons (
                    market_name, source_market_name, base_name, weapon_type, wear_code, wear_label,
                    variant_group, variant_label, variant_tag,
                    is_special_candidate, is_special_confirmed, special_variant, special_variant_type,
                    detail_last_fetched_at, detail_title, float_value, pattern_seed, paint_index, detail_price, sale_status,
                    display_name_cn, display_name_en, display_name_final, display_variant, cn_name,
                    roll_raw_price, roll_converted_cost, youpin_sell_price,
                    gross_profit, gross_profit_rate, net_sell_price, net_profit, net_profit_rate,
                    bucket_risk_level, priority_score,
                    roll_reference_price, roll_max_markup, roll_bucket_count, roll_available_count, roll_listing_count,
                    price_status, price_status_note,
                    roll_last_seen_at, youpin_price_updated_at, youpin_age_seconds, roll_age_seconds,
                    is_youpin_fresh, is_roll_fresh, is_fresh_pair, is_live_verified, live_verify_price, live_verify_time,
                    verify_status, verify_source, verify_time, verify_age_seconds, verified_fresh, verify_note,
                    mapping_match_level, mapping_confidence,
                    decision_grade, decision_note, data_confidence, stale_reason,
                    reference_reason, bucket_markup, target_markup,
                    price_anomaly, price_anomaly_ratio, price_reference_median, price_mad, price_iqr, price_std_dev, price_trend, price_anomaly_note,
                    youpin_price, price_diff, profit_rate, calc_note, updated_at
                ) VALUES ("""
                + placeholders
                + """)
                ON CONFLICT(market_name) DO UPDATE SET
                    source_market_name = excluded.source_market_name,
                    base_name = excluded.base_name,
                    weapon_type = excluded.weapon_type,
                    wear_code = excluded.wear_code,
                    wear_label = excluded.wear_label,
                    variant_group = excluded.variant_group,
                    variant_label = excluded.variant_label,
                    variant_tag = excluded.variant_tag,
                    is_special_candidate = excluded.is_special_candidate,
                    is_special_confirmed = excluded.is_special_confirmed,
                    special_variant = excluded.special_variant,
                    special_variant_type = excluded.special_variant_type,
                    detail_last_fetched_at = excluded.detail_last_fetched_at,
                    detail_title = excluded.detail_title,
                    float_value = excluded.float_value,
                    pattern_seed = excluded.pattern_seed,
                    paint_index = excluded.paint_index,
                    detail_price = excluded.detail_price,
                    sale_status = excluded.sale_status,
                    display_name_cn = excluded.display_name_cn,
                    display_name_en = excluded.display_name_en,
                    display_name_final = excluded.display_name_final,
                    display_variant = excluded.display_variant,
                    cn_name = excluded.cn_name,
                    roll_raw_price = excluded.roll_raw_price,
                    roll_converted_cost = excluded.roll_converted_cost,
                    youpin_sell_price = excluded.youpin_sell_price,
                    gross_profit = excluded.gross_profit,
                    gross_profit_rate = excluded.gross_profit_rate,
                    net_sell_price = excluded.net_sell_price,
                    net_profit = excluded.net_profit,
                    net_profit_rate = excluded.net_profit_rate,
                    bucket_risk_level = excluded.bucket_risk_level,
                    priority_score = excluded.priority_score,
                    roll_reference_price = excluded.roll_reference_price,
                    roll_max_markup = excluded.roll_max_markup,
                    roll_bucket_count = excluded.roll_bucket_count,
                    roll_available_count = excluded.roll_available_count,
                    roll_listing_count = excluded.roll_listing_count,
                    price_status = excluded.price_status,
                    price_status_note = excluded.price_status_note,
                    roll_last_seen_at = excluded.roll_last_seen_at,
                    youpin_price_updated_at = excluded.youpin_price_updated_at,
                    youpin_age_seconds = excluded.youpin_age_seconds,
                    roll_age_seconds = excluded.roll_age_seconds,
                    is_youpin_fresh = excluded.is_youpin_fresh,
                    is_roll_fresh = excluded.is_roll_fresh,
                    is_fresh_pair = excluded.is_fresh_pair,
                    is_live_verified = excluded.is_live_verified,
                    live_verify_price = excluded.live_verify_price,
                    live_verify_time = excluded.live_verify_time,
                    verify_status = excluded.verify_status,
                    verify_source = excluded.verify_source,
                    verify_time = excluded.verify_time,
                    verify_age_seconds = excluded.verify_age_seconds,
                    verified_fresh = excluded.verified_fresh,
                    verify_note = excluded.verify_note,
                    mapping_match_level = excluded.mapping_match_level,
                    mapping_confidence = excluded.mapping_confidence,
                    decision_grade = excluded.decision_grade,
                    decision_note = excluded.decision_note,
                    data_confidence = excluded.data_confidence,
                    stale_reason = excluded.stale_reason,
                    reference_reason = excluded.reference_reason,
                    bucket_markup = excluded.bucket_markup,
                    target_markup = excluded.target_markup,
                    price_anomaly = excluded.price_anomaly,
                    price_anomaly_ratio = excluded.price_anomaly_ratio,
                    price_reference_median = excluded.price_reference_median,
                    price_mad = excluded.price_mad,
                    price_iqr = excluded.price_iqr,
                    price_std_dev = excluded.price_std_dev,
                    price_trend = excluded.price_trend,
                    price_anomaly_note = excluded.price_anomaly_note,
                    youpin_price = excluded.youpin_price,
                    price_diff = excluded.price_diff,
                    profit_rate = excluded.profit_rate,
                    calc_note = excluded.calc_note,
                    updated_at = excluded.updated_at
                """
            )
            self._conn.executemany(sql, payload)
            self._conn.commit()

    def prune_stale_comparisons(self, min_seen_ts: int) -> int:
        with self._lock:
            cur = self._conn.execute(
                """
                DELETE FROM comparisons
                WHERE COALESCE(source_market_name, market_name) NOT IN (
                    SELECT DISTINCT market_name
                    FROM roll_trades
                    WHERE status = 'LISTED' AND last_seen_at >= ?
                )
                """,
                (min_seen_ts,),
            )
            self._conn.commit()
            return cur.rowcount

    def prune_comparisons_to_roll_daily_snapshot(self) -> int:
        with self._lock:
            cur = self._conn.execute(
                """
                DELETE FROM comparisons
                WHERE COALESCE(source_market_name, market_name) NOT IN (
                    SELECT source_market_name
                    FROM roll_daily_snapshot
                )
                """
            )
            self._conn.commit()
            return cur.rowcount

    def prune_comparisons_to_market_names(self, market_names: list[str]) -> int:
        deduped: list[str] = []
        seen: set[str] = set()
        for name in market_names:
            key = str(name or "").strip()
            if not key or key in seen:
                continue
            seen.add(key)
            deduped.append(key)
        with self._lock:
            if not deduped:
                cur = self._conn.execute("DELETE FROM comparisons")
                self._conn.commit()
                return cur.rowcount
            placeholders = ",".join("?" for _ in deduped)
            cur = self._conn.execute(
                f"""
                DELETE FROM comparisons
                WHERE COALESCE(source_market_name, market_name) NOT IN ({placeholders})
                """,
                deduped,
            )
            self._conn.commit()
            return cur.rowcount

    def prune_watchlist_placeholder_comparisons(self) -> int:
        with self._lock:
            cur = self._conn.execute(
                """
                DELETE FROM comparisons
                WHERE COALESCE(reference_reason, '') = 'watchlist_target_placeholder'
                """
            )
            self._conn.commit()
            return cur.rowcount

    def prune_current_serving_to_market_names(self, market_names: list[str]) -> int:
        deduped: list[str] = []
        seen: set[str] = set()
        for name in market_names:
            key = str(name or "").strip()
            if not key or key in seen:
                continue
            seen.add(key)
            deduped.append(key)
        with self._lock:
            if not deduped:
                cur = self._conn.execute(
                    "UPDATE serving_comparisons SET is_current = 0 WHERE is_current = 1"
                )
                self._conn.commit()
                return cur.rowcount
            placeholders = ",".join("?" for _ in deduped)
            cur = self._conn.execute(
                f"""
                UPDATE serving_comparisons
                SET is_current = 0
                WHERE is_current = 1
                  AND (
                    CASE
                      WHEN INSTR(COALESCE(market_key, ''), ' [') > 0
                        THEN SUBSTR(market_key, 1, INSTR(market_key, ' [') - 1)
                      ELSE COALESCE(market_key, '')
                    END
                  ) NOT IN ({placeholders})
                """,
                deduped,
            )
            self._conn.commit()
            return cur.rowcount

    def snapshot_comparisons(self, snapshot_at: int) -> int:
        with self._lock:
            cur = self._conn.execute(
                """
                INSERT INTO comparison_snapshots (
                    snapshot_at, market_name, source_market_name, base_name,
                    wear_code, variant_tag, roll_raw_price, roll_converted_cost,
                    youpin_sell_price, gross_profit_rate, net_profit_rate,
                    roll_available_count, updated_at
                )
                SELECT
                    ?, market_name, source_market_name, base_name,
                    wear_code, COALESCE(variant_tag, 'NA'), roll_raw_price, roll_converted_cost,
                    youpin_sell_price, gross_profit_rate, net_profit_rate,
                    COALESCE(roll_available_count, roll_listing_count, 0), updated_at
                FROM comparisons
                """,
                (int(snapshot_at),),
            )
            self._conn.commit()
            return cur.rowcount

    def cleanup_comparison_snapshots(self, expire_before_ts: int) -> int:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM comparison_snapshots WHERE snapshot_at < ?",
                (int(expire_before_ts),),
            )
            self._conn.commit()
            return cur.rowcount

    def insert_verification_run(
        self,
        *,
        run_at: int,
        checked: int,
        ok_count: int,
        failed_count: int,
        details_json: str | None,
    ) -> int:
        with self._lock:
            cur = self._conn.execute(
                """
                INSERT INTO verification_runs (
                    run_at, checked, ok_count, failed_count, details_json
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    int(run_at),
                    int(checked),
                    int(ok_count),
                    int(failed_count),
                    details_json,
                ),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def get_latest_verification_run(self) -> dict | None:
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM verification_runs ORDER BY run_at DESC LIMIT 1"
            )
            row = cur.fetchone()
            return dict(row) if row else None

    def query_comparisons(
        self,
        search: str,
        sort_by: str,
        order: str,
        limit: int,
        wear_filter: str = "",
        variant_filter: str = "",
        weapon_filter: str = "",
        min_available: int = 0,
        min_roll_price: float = 0.0,
        only_fresh: bool = False,
        min_decision_grade: str = "",
        only_verified: bool = False,
        only_profitable: bool = False,
        sort_by_secondary: str = "",
        order_secondary: str = "desc",
        priced_only: bool = False,
    ) -> list[dict]:
        allowed_sort = {
            "market_name": "market_name",
            "display_name_final": "display_name_final",
            "display_name_cn": "display_name_cn",
            "display_name_en": "display_name_en",
            "weapon_type": "weapon_type",
            "wear": "wear_code",
            "variant": "variant_label",
            "variant_label": "variant_label",
            "roll_raw_price": "roll_raw_price",
            "roll_cost": "roll_converted_cost",
            "youpin_sell_price": "youpin_sell_price",
            "gross_profit": "gross_profit",
            "gross_profit_rate": "gross_profit_rate",
            "net_profit": "net_profit",
            "net_profit_rate": "net_profit_rate",
            "roll_available_count": "roll_available_count",
            "available_count": "roll_available_count",
            "roll_bucket_count": "roll_bucket_count",
            "priority_score": "priority_score",
            "bucket_risk_level": "bucket_risk_level",
            "decision_grade": "decision_grade",
            "verify_status": "verify_status",
            "data_confidence": "data_confidence",
            "is_fresh_pair": "is_fresh_pair",
            "price_status": "price_status",
            "roll_price": "roll_raw_price",
            "youpin_price": "youpin_sell_price",
            "price_diff": "gross_profit",
            "profit_rate": "gross_profit_rate",
            "updated_at": "updated_at",
            "score": "score",
        }
        sort_col = allowed_sort.get(sort_by, "roll_converted_cost")
        sort_dir = "DESC" if order.lower() == "desc" else "ASC"
        sort_col_secondary = allowed_sort.get(sort_by_secondary or "", "")
        sort_dir_secondary = "DESC" if order_secondary.lower() == "desc" else "ASC"
        safe_limit = max(1, min(10000, int(limit)))

        where_clauses: list[str] = []
        params: list = []
        q = search.strip()
        if q:
            where_clauses.append(
                "("
                "market_name LIKE ? OR source_market_name LIKE ? OR cn_name LIKE ? OR base_name LIKE ? "
                "OR display_name_final LIKE ? OR display_name_cn LIKE ? OR display_name_en LIKE ? "
                "OR weapon_type LIKE ?"
                ")"
            )
            like = f"%{q}%"
            params.extend([like, like, like, like, like, like, like, like])

        wear = wear_filter.strip().upper()
        if wear and wear != "ALL":
            where_clauses.append("wear_code = ?")
            params.append(wear)

        variant = variant_filter.strip()
        if variant and variant.upper() != "ALL":
            where_clauses.append("COALESCE(variant_label, variant_tag, 'NA') = ?")
            params.append(variant)

        weapon = weapon_filter.strip()
        if weapon and weapon.upper() != "ALL":
            normalized_weapon = normalize_weapon_type_name(weapon)
            weapon_candidates: list[str] = []
            for candidate in (normalized_weapon, weapon):
                key = str(candidate or "").strip()
                if key and key not in weapon_candidates:
                    weapon_candidates.append(key)
            placeholders = ", ".join("?" for _ in weapon_candidates)
            where_clauses.append(f"COALESCE(weapon_type, 'Unknown') IN ({placeholders})")
            params.extend(weapon_candidates)

        if int(min_available or 0) > 0:
            where_clauses.append(
                "COALESCE(roll_available_count, roll_listing_count, 0) >= ?"
            )
            params.append(int(min_available))

        if float(min_roll_price or 0.0) > 0:
            where_clauses.append("COALESCE(roll_raw_price, 0) >= ?")
            params.append(float(min_roll_price))

        if priced_only:
            where_clauses.append("youpin_sell_price IS NOT NULL")
            where_clauses.append("roll_converted_cost IS NOT NULL")

        if only_fresh:
            where_clauses.append("COALESCE(is_fresh_pair, 0) = 1")

        grade = min_decision_grade.strip().upper()
        if grade in {"A", "B", "C", "D", "X"}:
            threshold = {
                "A": ("A",),
                "B": ("A", "B"),
                "C": ("A", "B", "C"),
                "D": ("A", "B", "C", "D"),
                "X": ("A", "B", "C", "D", "X"),
            }[grade]
            placeholders = ", ".join("?" for _ in threshold)
            where_clauses.append(f"COALESCE(decision_grade, 'X') IN ({placeholders})")
            params.extend(threshold)

        if only_verified:
            where_clauses.append("COALESCE(verify_status, 'unverified') = 'verified_live'")

        if only_profitable:
            where_clauses.append("COALESCE(net_profit_rate, -999999) >= 0")

        where = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""

        sql = (
            "SELECT * FROM comparisons "
            f"{where} "
        )

        with self._lock:
            cur = self._conn.execute(sql, params)
            rows = [dict(row) for row in cur.fetchall()]

        rows = self._overlay_current_serving_special_rows(rows)

        sanitized_rows: list[dict] = []
        for row in rows:
            row = self._sanitize_special_variant_row(row)
            if variant and variant.upper() != "ALL":
                if str(row.get("variant_label") or row.get("variant_tag") or "NA") != variant:
                    continue
            row["score"] = self._compute_opportunity_score(row)
            sanitized_rows.append(row)
        rows = sanitized_rows
        rows = self._sort_rows_in_memory(
            rows,
            primary_key=sort_col,
            primary_desc=(sort_dir == "DESC"),
            secondary_key=sort_col_secondary,
            secondary_desc=(sort_dir_secondary == "DESC"),
            tertiary_name_key="market_name",
        )
        return rows[:safe_limit]

    def _overlay_current_serving_special_rows(self, rows: list[dict]) -> list[dict]:
        if not rows:
            return rows

        target_keys: list[str] = []
        seen_keys: set[str] = set()
        target_display_names: list[str] = []
        seen_display_names: set[tuple[str, str]] = set()
        target_variants: list[str] = []
        seen_variants: set[str] = set()
        for row in rows:
            variant_label = str(row.get("variant_label") or row.get("variant_tag") or "NA").strip()
            market_key = str(row.get("market_name") or "").strip()
            display_name = str(
                row.get("display_name_final")
                or row.get("source_market_name")
                or row.get("market_name")
                or ""
            ).strip()
            if not market_key or market_key in seen_keys:
                continue
            if variant_label == "NA":
                continue
            if self._is_special_variant_row_trusted(row):
                continue
            seen_keys.add(market_key)
            target_keys.append(market_key)
            display_key = (display_name, variant_label)
            if display_name and display_key not in seen_display_names:
                seen_display_names.add(display_key)
                target_display_names.append(display_name)
            if variant_label not in seen_variants:
                seen_variants.add(variant_label)
                target_variants.append(variant_label)

        if not target_keys and not target_display_names and not target_variants:
            return rows

        key_placeholders = ", ".join("?" for _ in target_keys) if target_keys else "''"
        name_placeholders = (
            ", ".join("?" for _ in target_display_names) if target_display_names else "''"
        )
        variant_placeholders = (
            ", ".join("?" for _ in target_variants) if target_variants else "''"
        )
        query_params = [*target_keys, *target_display_names, *target_variants]
        with self._lock:
            cur = self._conn.execute(
                f"""
                SELECT *
                FROM serving_comparisons
                WHERE (
                    market_key IN ({key_placeholders})
                    OR display_name_final IN ({name_placeholders})
                    OR variant_label IN ({variant_placeholders})
                )
                  AND COALESCE(youpin_live_price, 0) > 0
                ORDER BY is_current DESC, built_at DESC
                """,
                query_params,
            )
            serving_rows: dict[str, dict] = {}
            serving_rows_by_display: dict[tuple[str, str], dict] = {}
            serving_rows_by_normalized: dict[tuple[str, str], dict] = {}
            serving_rows_by_identity: dict[tuple[str, str, str], dict] = {}
            for row in cur.fetchall():
                market_key = str(row["market_key"])
                display_name = str(row["display_name_final"] or "").strip()
                variant_label = str(row["variant_label"] or "NA").strip()
                row_dict = dict(row)
                if market_key not in serving_rows:
                    serving_rows[market_key] = row_dict
                display_key = (display_name, variant_label)
                if display_name and display_key not in serving_rows_by_display:
                    serving_rows_by_display[display_key] = row_dict
                normalized_name = self._normalize_special_overlay_name(
                    market_key or display_name,
                    variant_label,
                )
                normalized_key = (normalized_name, variant_label)
                if normalized_name and normalized_key not in serving_rows_by_normalized:
                    serving_rows_by_normalized[normalized_key] = row_dict
                identity_source = self._restore_special_finish_parent_name(
                    market_key or display_name,
                    variant_label,
                )
                identity = normalize_name_identity(identity_source, variant_label)
                identity_key = (
                    normalize_base_name(identity.base_name, strip_variant=False),
                    str(identity.wear_code or "NA").strip().upper(),
                    variant_label,
                )
                if identity_key not in serving_rows_by_identity:
                    serving_rows_by_identity[identity_key] = row_dict

        if not serving_rows:
            return rows

        merged_rows: list[dict] = []
        for row in rows:
            market_key = str(row.get("market_name") or "").strip()
            display_name = str(
                row.get("display_name_final")
                or row.get("source_market_name")
                or row.get("market_name")
                or ""
            ).strip()
            variant_label = str(row.get("variant_label") or row.get("variant_tag") or "NA").strip()
            serving_row = serving_rows.get(market_key)
            if serving_row is None and display_name:
                serving_row = serving_rows_by_display.get((display_name, variant_label))
            if serving_row is None:
                normalized_name = self._normalize_special_overlay_name(
                    str(row.get("source_market_name") or display_name or market_key),
                    variant_label,
                )
                serving_row = serving_rows_by_normalized.get((normalized_name, variant_label))
            if serving_row is None:
                identity_source = self._restore_special_finish_parent_name(
                    str(row.get("source_market_name") or display_name or market_key),
                    variant_label,
                )
                row_identity = normalize_name_identity(identity_source, variant_label)
                row_identity_key = (
                    normalize_base_name(
                        str(row_identity.base_name or row.get("base_name") or ""),
                        strip_variant=False,
                    ),
                    str(row_identity.wear_code or row.get("wear_code") or "NA").strip().upper(),
                    variant_label,
                )
                serving_row = serving_rows_by_identity.get(row_identity_key)
            if not serving_row or not self._is_special_variant_row_trusted(serving_row):
                merged_rows.append(row)
                continue

            merged = dict(row)
            merged.update(
                {
                    "youpin_sell_price": serving_row.get("youpin_live_price"),
                    "youpin_price": serving_row.get("youpin_live_price"),
                    "gross_profit": serving_row.get("gross_profit"),
                    "gross_profit_rate": serving_row.get("gross_profit_rate"),
                    "net_profit": serving_row.get("net_profit"),
                    "net_profit_rate": serving_row.get("net_profit_rate"),
                    "live_verify_price": serving_row.get("youpin_live_price"),
                    "live_verify_time": serving_row.get("youpin_live_time"),
                    "verify_status": serving_row.get("verify_status"),
                    "verify_source": serving_row.get("verify_source"),
                    "verify_note": serving_row.get("verify_note"),
                    "verify_time": serving_row.get("youpin_live_time"),
                    "youpin_price_updated_at": serving_row.get("youpin_live_time"),
                    "youpin_age_seconds": serving_row.get("youpin_age_seconds"),
                    "is_live_verified": serving_row.get("is_live_verified"),
                    "is_fresh_pair": serving_row.get("is_fresh_pair"),
                    "mapping_match_level": serving_row.get("mapping_match_level"),
                    "mapping_confidence": serving_row.get("mapping_confidence"),
                    "price_reference_median": serving_row.get("price_reference_median"),
                    "price_mad": serving_row.get("price_mad"),
                    "price_iqr": serving_row.get("price_iqr"),
                    "price_anomaly": serving_row.get("price_anomaly"),
                    "price_anomaly_ratio": serving_row.get("price_anomaly_ratio"),
                    "price_anomaly_note": serving_row.get("price_anomaly_note"),
                    "price_std_dev": serving_row.get("price_std_dev"),
                    "price_trend": serving_row.get("price_trend"),
                    "data_source": "comparisons+serving_overlay",
                }
            )
            merged_rows.append(merged)

        return merged_rows

    def upsert_serving_rows(
        self,
        *,
        run_id: str,
        rows: list[dict],
        built_at: int,
        replaced_market_keys: list[str] | None = None,
    ) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO serving_runs (run_id, started_at, finished_at, config_json, stats_json)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                  finished_at = excluded.finished_at,
                  stats_json = excluded.stats_json
                """,
                (run_id, built_at, built_at, "{}", None),
            )
            market_keys: list[str] = []
            if replaced_market_keys:
                seen_keys: set[str] = set()
                for raw_key in replaced_market_keys:
                    key = str(raw_key or "").strip()
                    if not key or key in seen_keys:
                        continue
                    seen_keys.add(key)
                    market_keys.append(key)
            if market_keys:
                placeholders = ",".join("?" for _ in market_keys)
                self._conn.execute(
                    f"UPDATE serving_comparisons SET is_current = 0 WHERE is_current = 1 AND market_key IN ({placeholders})",
                    market_keys,
                )
            if rows:
                deduped_rows: list[dict] = []
                seen_payload_keys: set[str] = set()
                for row in rows:
                    market_key = str(row.get("market_name") or "").strip()
                    if not market_key or market_key in seen_payload_keys:
                        continue
                    seen_payload_keys.add(market_key)
                    deduped_rows.append(row)
                payload = []
                for r in deduped_rows:
                    youpin_live_price = r.get("live_verify_price")
                    if youpin_live_price is None:
                        youpin_live_price = r.get("youpin_sell_price")
                    youpin_live_time = r.get("live_verify_time")
                    if youpin_live_time is None:
                        youpin_live_time = (
                            r.get("verify_time")
                            or r.get("youpin_price_updated_at")
                            or r.get("updated_at")
                        )
                    payload.append(
                        (
                            run_id,
                            1,
                            r.get("market_name"),
                            r.get("display_name_final"),
                            r.get("weapon_type"),
                            r.get("wear_code"),
                            r.get("wear_label"),
                            r.get("variant_label"),
                            int(bool(r.get("is_special_candidate"))),
                            int(bool(r.get("is_special_confirmed"))),
                            r.get("special_variant"),
                            r.get("special_variant_type"),
                            int(r.get("detail_last_fetched_at") or 0),
                            r.get("float_value"),
                            r.get("pattern_seed"),
                            r.get("paint_index"),
                            r.get("detail_price"),
                            r.get("sale_status"),
                            r.get("roll_reference_price"),
                            r.get("roll_converted_cost"),
                            r.get("roll_last_seen_at"),
                            r.get("roll_age_seconds"),
                            r.get("roll_listing_count"),
                            r.get("roll_bucket_count"),
                            r.get("bucket_markup"),
                            r.get("target_markup"),
                            r.get("reference_reason"),
                            r.get("youpin_template_id"),
                            youpin_live_price,
                            youpin_live_time,
                            r.get("youpin_age_seconds"),
                            int(bool(r.get("is_fresh_pair"))),
                            int(bool(r.get("is_live_verified"))),
                            r.get("verify_status"),
                            r.get("verify_source"),
                            r.get("verify_note"),
                            r.get("mapping_match_level"),
                            r.get("mapping_confidence"),
                            "name_mapper",
                            None,
                            r.get("price_reference_median"),
                            r.get("price_mad"),
                            r.get("price_iqr"),
                            int(bool(r.get("price_anomaly"))),
                            r.get("price_anomaly_ratio"),
                            r.get("price_anomaly_note"),
                            r.get("price_std_dev"),
                            r.get("price_trend"),
                            r.get("net_profit"),
                            r.get("net_profit_rate"),
                            r.get("calc_note"),
                            int(built_at),
                        )
                    )
                self._conn.executemany(
                    """
                    INSERT INTO serving_comparisons (
                      run_id, is_current, market_key, display_name_final, weapon_type, wear_code, wear_label, variant_label,
                      is_special_candidate, is_special_confirmed, special_variant, special_variant_type,
                      detail_last_fetched_at, float_value, pattern_seed, paint_index, detail_price, sale_status,
                      roll_reference_price, roll_converted_cost, roll_last_seen_at, roll_age_seconds, roll_listing_count, roll_bucket_count,
                      bucket_markup, target_markup, reference_reason,
                      youpin_template_id, youpin_live_price, youpin_live_time, youpin_age_seconds,
                      is_fresh_pair, is_live_verified, verify_status, verify_source, verify_note,
                      mapping_match_level, mapping_confidence, mapping_source, mapping_version,
                      price_reference_median, price_mad, price_iqr, price_anomaly, price_anomaly_ratio, price_anomaly_note,
                      price_std_dev, price_trend, net_profit, net_profit_rate, calc_note, built_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    payload,
                )
            self._conn.commit()

    def query_serving_comparisons(
        self,
        *,
        search: str,
        sort_by: str,
        order: str,
        sort_by_secondary: str = "",
        order_secondary: str = "desc",
        limit: int,
        wear_filter: str = "",
        variant_filter: str = "",
        weapon_filter: str = "",
        min_roll_price: float = 0.0,
        only_fresh: bool = True,
        only_verified: bool = True,
    ) -> list[dict]:
        allowed_sort = {
            "roll_cost": "roll_converted_cost",
            "roll_raw_price": "roll_reference_price",
            "youpin_sell_price": "youpin_live_price",
            "gross_profit": "gross_profit",
            "gross_profit_rate": "gross_profit_rate",
            "net_profit_rate": "net_profit_rate",
            "net_profit": "net_profit",
            "roll_available_count": "roll_listing_count",
            "updated_at": "built_at",
            "market_name": "market_key",
            "score": "score",
        }
        sort_col = allowed_sort.get(sort_by, "roll_converted_cost")
        sort_dir = "DESC" if order.lower() == "desc" else "ASC"
        sort_col_secondary = allowed_sort.get(sort_by_secondary or "", "")
        sort_dir_secondary = "DESC" if order_secondary.lower() == "desc" else "ASC"
        safe_limit = max(1, min(10000, int(limit)))
        where_clauses = ["is_current = 1"]
        params: list = []
        q = search.strip()
        if q:
            where_clauses.append("(market_key LIKE ? OR display_name_final LIKE ? OR weapon_type LIKE ?)")
            like = f"%{q}%"
            params.extend([like, like, like])
        wear = wear_filter.strip().upper()
        if wear and wear != "ALL":
            where_clauses.append("wear_code = ?")
            params.append(wear)
        variant = variant_filter.strip()
        if variant and variant.upper() != "ALL":
            where_clauses.append("COALESCE(variant_label, 'NA') = ?")
            params.append(variant)
        weapon = weapon_filter.strip()
        if weapon and weapon.upper() != "ALL":
            normalized_weapon = normalize_weapon_type_name(weapon)
            weapon_candidates: list[str] = []
            for candidate in (normalized_weapon, weapon):
                key = str(candidate or "").strip()
                if key and key not in weapon_candidates:
                    weapon_candidates.append(key)
            placeholders = ", ".join("?" for _ in weapon_candidates)
            where_clauses.append(f"COALESCE(weapon_type, 'Unknown') IN ({placeholders})")
            params.extend(weapon_candidates)
        if float(min_roll_price or 0.0) > 0:
            where_clauses.append("COALESCE(roll_reference_price, 0) >= ?")
            params.append(float(min_roll_price))
        if only_fresh:
            where_clauses.append("COALESCE(is_fresh_pair, 0) = 1")
        if only_verified:
            where_clauses.append(
                "COALESCE(verify_status, '') IN ('verified_live', 'verified_cache')"
            )
        sql = (
            "SELECT * FROM serving_comparisons "
            f"WHERE {' AND '.join(where_clauses)} "
        )
        with self._lock:
            cur = self._conn.execute(sql, params)
            rows = [dict(row) for row in cur.fetchall()]

        sanitized_rows: list[dict] = []
        for row in rows:
            row = self._sanitize_special_variant_row(row)
            if variant and variant.upper() != "ALL":
                if str(row.get("variant_label") or row.get("variant_tag") or "NA") != variant:
                    continue
            if not self._is_special_variant_row_trusted(row):
                continue
            try:
                roll_cost = float(row.get("roll_converted_cost"))
                youpin_price = float(row.get("youpin_live_price"))
                if roll_cost > 0 and youpin_price > 0:
                    gross_profit = roll_cost - youpin_price
                    row["gross_profit"] = gross_profit
                    row["gross_profit_rate"] = gross_profit / max(youpin_price, 1e-9) * 100.0
                else:
                    row["gross_profit"] = None
                    row["gross_profit_rate"] = None
            except (TypeError, ValueError):
                row["gross_profit"] = None
                row["gross_profit_rate"] = None
            score_row = dict(row)
            score_row["youpin_sell_price"] = row.get("youpin_live_price")
            row["score"] = self._compute_opportunity_score(score_row)
            sanitized_rows.append(row)

        rows = sanitized_rows

        rows = self._sort_rows_in_memory(
            rows,
            primary_key=sort_col,
            primary_desc=(sort_dir == "DESC"),
            secondary_key=sort_col_secondary,
            secondary_desc=(sort_dir_secondary == "DESC"),
            tertiary_name_key="market_key",
        )
        return rows[:safe_limit]

    def get_comparison_by_market_name(self, market_name: str) -> dict | None:
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM comparisons WHERE market_name = ?",
                (market_name,),
            )
            row = cur.fetchone()
            return dict(row) if row else None

    def update_comparison_verification(
        self,
        *,
        market_name: str,
        verify_status: str,
        verify_source: str,
        verify_time: int | None,
        verify_age_seconds: int | None,
        verified_fresh: bool,
        verify_note: str,
        youpin_sell_price: float | None = None,
        gross_profit: float | None = None,
        gross_profit_rate: float | None = None,
        net_sell_price: float | None = None,
        net_profit: float | None = None,
        net_profit_rate: float | None = None,
        youpin_price_updated_at: int | None = None,
        youpin_age_seconds: int | None = None,
        is_youpin_fresh: bool | None = None,
        is_fresh_pair: bool | None = None,
        decision_grade: str | None = None,
        decision_note: str | None = None,
        stale_reason: str | None = None,
        price_anomaly: bool | None = None,
        price_anomaly_ratio: float | None = None,
        price_reference_median: float | None = None,
        price_anomaly_note: str | None = None,
    ) -> None:
        with self._lock:
            self._conn.execute(
                """
                UPDATE comparisons
                SET verify_status = ?,
                    verify_source = ?,
                    verify_time = ?,
                    is_live_verified = CASE WHEN ? = 'verified_live' THEN 1 ELSE 0 END,
                    live_verify_price = CASE WHEN ? = 'verified_live' THEN COALESCE(?, live_verify_price) ELSE live_verify_price END,
                    live_verify_time = CASE WHEN ? = 'verified_live' THEN COALESCE(?, live_verify_time) ELSE live_verify_time END,
                    verify_age_seconds = ?,
                    verified_fresh = ?,
                    verify_note = ?,
                    youpin_sell_price = COALESCE(?, youpin_sell_price),
                    youpin_price = COALESCE(?, youpin_price),
                    gross_profit = COALESCE(?, gross_profit),
                    gross_profit_rate = COALESCE(?, gross_profit_rate),
                    net_sell_price = COALESCE(?, net_sell_price),
                    net_profit = COALESCE(?, net_profit),
                    net_profit_rate = COALESCE(?, net_profit_rate),
                    youpin_price_updated_at = COALESCE(?, youpin_price_updated_at),
                    youpin_age_seconds = COALESCE(?, youpin_age_seconds),
                    is_youpin_fresh = COALESCE(?, is_youpin_fresh),
                    is_fresh_pair = COALESCE(?, is_fresh_pair),
                    decision_grade = COALESCE(?, decision_grade),
                    decision_note = COALESCE(?, decision_note),
                    stale_reason = COALESCE(?, stale_reason),
                    price_anomaly = COALESCE(?, price_anomaly),
                    price_anomaly_ratio = COALESCE(?, price_anomaly_ratio),
                    price_reference_median = COALESCE(?, price_reference_median),
                    price_anomaly_note = COALESCE(?, price_anomaly_note),
                    updated_at = CASE WHEN ? IS NOT NULL THEN ? ELSE updated_at END
                WHERE market_name = ?
                """,
                (
                    verify_status,
                    verify_source,
                    verify_time,
                    verify_status,
                    verify_status,
                    youpin_sell_price,
                    verify_status,
                    verify_time,
                    verify_age_seconds,
                    int(bool(verified_fresh)),
                    verify_note,
                    youpin_sell_price,
                    youpin_sell_price,
                    gross_profit,
                    gross_profit_rate,
                    net_sell_price,
                    net_profit,
                    net_profit_rate,
                    youpin_price_updated_at,
                    youpin_age_seconds,
                    None if is_youpin_fresh is None else int(bool(is_youpin_fresh)),
                    None if is_fresh_pair is None else int(bool(is_fresh_pair)),
                    decision_grade,
                    decision_note,
                    stale_reason,
                    None if price_anomaly is None else int(bool(price_anomaly)),
                    price_anomaly_ratio,
                    price_reference_median,
                    price_anomaly_note,
                    verify_time,
                    verify_time,
                    market_name,
                ),
            )
            self._conn.commit()

    def backfill_comparison_youpin_cache(
        self,
        *,
        market_name: str,
        price: float,
        price_updated_at: int,
        scan_time: int,
        is_youpin_fresh: bool,
        verify_source: str,
        verify_note: str,
    ) -> int:
        name = str(market_name or "").strip()
        if not name:
            return 0
        try:
            price_value = float(price)
        except (TypeError, ValueError):
            return 0
        if price_value <= 0:
            return 0
        price_time = int(price_updated_at or scan_time or 0)
        age_seconds = max(0, int(scan_time) - price_time) if price_time else None
        with self._lock:
            cur = self._conn.execute(
                """
                UPDATE comparisons
                SET youpin_sell_price = ?,
                    youpin_price = ?,
                    youpin_price_updated_at = ?,
                    youpin_age_seconds = ?,
                    is_youpin_fresh = ?,
                    verify_status = CASE
                        WHEN COALESCE(verify_status, '') = 'verified_live'
                        THEN verify_status ELSE 'verified_cache' END,
                    verify_source = CASE
                        WHEN COALESCE(verify_status, '') = 'verified_live'
                        THEN verify_source ELSE ? END,
                    verify_time = CASE
                        WHEN COALESCE(verify_status, '') = 'verified_live'
                        THEN verify_time ELSE ? END,
                    verify_age_seconds = ?,
                    verified_fresh = ?,
                    verify_note = CASE
                        WHEN COALESCE(verify_status, '') = 'verified_live'
                        THEN verify_note ELSE ? END,
                    price_status = CASE
                        WHEN roll_raw_price IS NOT NULL AND roll_raw_price > 0 THEN 'ok'
                        ELSE price_status END,
                    price_status_note = CASE
                        WHEN roll_raw_price IS NOT NULL AND roll_raw_price > 0 THEN 'Price available'
                        ELSE 'Youpin price cached; waiting for active Roll listing' END,
                    mapping_match_level = CASE
                        WHEN COALESCE(mapping_match_level, '') = 'watchlist_target'
                        THEN 'exact_market_name' ELSE mapping_match_level END,
                    mapping_confidence = CASE
                        WHEN COALESCE(mapping_match_level, '') = 'watchlist_target'
                        THEN MAX(COALESCE(mapping_confidence, 0), 0.95)
                        ELSE mapping_confidence END,
                    reference_reason = CASE
                        WHEN COALESCE(reference_reason, '') = 'watchlist_target_placeholder'
                        THEN 'youpin_cache_refresh' ELSE reference_reason END,
                    calc_note = CASE
                        WHEN COALESCE(calc_note, '') = 'watchlist_target_placeholder'
                        THEN 'youpin_cache_refresh' ELSE calc_note END,
                    updated_at = MAX(COALESCE(updated_at, 0), ?)
                WHERE market_name = ? OR source_market_name = ?
                """,
                (
                    price_value,
                    price_value,
                    price_time,
                    age_seconds,
                    int(bool(is_youpin_fresh)),
                    verify_source,
                    price_time,
                    age_seconds,
                    int(bool(is_youpin_fresh)),
                    verify_note,
                    int(scan_time),
                    name,
                    name,
                ),
            )
            self._conn.commit()
            return int(cur.rowcount or 0)

    def list_auto_verify_candidates(
        self,
        *,
        min_profit_rate: float,
        limit: int,
    ) -> list[dict]:
        safe_limit = max(1, min(200, int(limit)))
        with self._lock:
            cur = self._conn.execute(
                """
                SELECT *
                FROM comparisons
                WHERE youpin_sell_price IS NOT NULL
                  AND (
                    COALESCE(net_profit_rate, -999999) >= ?
                    OR COALESCE(variant_label, 'NA') != 'NA'
                    OR COALESCE(priority_score, 0) >= 35
                    OR COALESCE(decision_grade, 'X') IN ('A', 'B')
                  )
                ORDER BY
                  CASE COALESCE(decision_grade, 'X')
                    WHEN 'A' THEN 5
                    WHEN 'B' THEN 4
                    WHEN 'C' THEN 3
                    WHEN 'D' THEN 2
                    ELSE 1
                  END DESC,
                  COALESCE(net_profit_rate, -999999) DESC,
                  COALESCE(priority_score, 0) DESC,
                  COALESCE(roll_bucket_count, 0) DESC,
                  market_name ASC
                LIMIT ?
                """,
                (float(min_profit_rate), safe_limit),
            )
            return [dict(row) for row in cur.fetchall()]

    def list_recent_comparison_market_names(
        self,
        limit: int,
        *,
        require_youpin_price: bool = True,
    ) -> list[str]:
        safe_limit = max(1, min(1000, int(limit)))
        where = "WHERE youpin_sell_price IS NOT NULL" if require_youpin_price else ""
        sql = (
            "SELECT market_name FROM comparisons "
            f"{where} "
            "ORDER BY updated_at DESC "
            "LIMIT ?"
        )
        with self._lock:
            cur = self._conn.execute(sql, (safe_limit,))
            return [str(row["market_name"]) for row in cur.fetchall()]

    def comparison_status_counts(self) -> dict:
        with self._lock:
            cur = self._conn.cursor()
            total = int(cur.execute("SELECT COUNT(*) FROM comparisons").fetchone()[0])
            fresh = int(
                cur.execute(
                    "SELECT COUNT(*) FROM comparisons WHERE COALESCE(is_fresh_pair, 0) = 1"
                ).fetchone()[0]
            )
            verified_live = int(
                cur.execute(
                    "SELECT COUNT(*) FROM comparisons WHERE COALESCE(verify_status, '') = 'verified_live'"
                ).fetchone()[0]
            )
            stale = total - fresh
            return {
                "total": total,
                "fresh": fresh,
                "verified_live": verified_live,
                "stale": stale,
            }

    def serving_status_counts(self) -> dict:
        with self._lock:
            cur = self._conn.cursor()
            total = int(
                cur.execute(
                    "SELECT COUNT(*) FROM serving_comparisons WHERE is_current = 1"
                ).fetchone()[0]
            )
            fresh = int(
                cur.execute(
                    "SELECT COUNT(*) FROM serving_comparisons WHERE is_current = 1 AND COALESCE(is_fresh_pair, 0) = 1"
                ).fetchone()[0]
            )
            live = int(
                cur.execute(
                    "SELECT COUNT(*) FROM serving_comparisons WHERE is_current = 1 AND COALESCE(is_live_verified, 0) = 1"
                ).fetchone()[0]
            )
            return {
                "total": total,
                "fresh": fresh,
                "live_verified": live,
            }

    def comparison_data_version(self) -> dict[str, int]:
        with self._lock:
            cur = self._conn.cursor()
            comparison_columns = {
                str(row["name"])
                for row in cur.execute("PRAGMA table_info(comparisons)").fetchall()
            }
            serving_columns = {
                str(row["name"])
                for row in cur.execute("PRAGMA table_info(serving_comparisons)").fetchall()
            }

            def _max_ts(table: str, columns: set[str], candidates: tuple[str, ...], where: str = "") -> int:
                parts = [
                    f"COALESCE(MAX({column}), 0)"
                    for column in candidates
                    if column in columns
                ]
                if not parts:
                    return 0
                sql = f"SELECT MAX({', '.join(parts)}) FROM {table} {where}".strip()
                return int(cur.execute(sql).fetchone()[0] or 0)

            comparison_count = int(
                cur.execute("SELECT COUNT(*) FROM comparisons").fetchone()[0] or 0
            )
            serving_count = int(
                cur.execute(
                    "SELECT COUNT(*) FROM serving_comparisons WHERE is_current = 1"
                ).fetchone()[0]
                or 0
            )
            comparison_updated_at = _max_ts(
                "comparisons",
                comparison_columns,
                ("updated_at", "verify_time", "live_verify_time", "youpin_price_updated_at", "roll_last_seen_at"),
            )
            serving_updated_at = _max_ts(
                "serving_comparisons",
                serving_columns,
                ("updated_at", "verify_time", "live_verify_time", "youpin_price_updated_at", "youpin_live_time", "roll_last_seen_at"),
                "WHERE is_current = 1",
            )
        latest_update_at = max(
            comparison_updated_at,
            serving_updated_at,
        )
        return {
            "comparison_count": comparison_count,
            "serving_count": serving_count,
            "latest_update_at": latest_update_at,
            "comparison_updated_at": comparison_updated_at,
            "serving_updated_at": serving_updated_at,
        }

    def watchlist_compare_snapshot(
        self,
        *,
        market_names: list[str],
        template_ids: list[int],
        min_roll_seen_ts: int,
    ) -> dict:
        ordered_names: list[str] = []
        seen_names: set[str] = set()
        for raw in market_names:
            name = str(raw or "").strip()
            if not name or name in seen_names:
                continue
            seen_names.add(name)
            ordered_names.append(name)

        ordered_template_ids: list[int] = []
        seen_template_ids: set[int] = set()
        for raw in template_ids:
            try:
                template_id = int(raw)
            except (TypeError, ValueError):
                continue
            if template_id <= 0 or template_id in seen_template_ids:
                continue
            seen_template_ids.add(template_id)
            ordered_template_ids.append(template_id)

        with self._lock:
            cur = self._conn.cursor()
            active_roll_names = {
                str(row["market_name"]).strip()
                for row in cur.execute(
                    """
                    SELECT DISTINCT market_name
                    FROM roll_trades
                    WHERE status = 'LISTED' AND last_seen_at >= ?
                    """,
                    (int(min_roll_seen_ts),),
                ).fetchall()
                if str(row["market_name"] or "").strip()
            }

            comparison_names_all: set[str] = set()
            comparison_names_with_price: set[str] = set()
            comparison_names_fresh: set[str] = set()
            comparison_names_live: set[str] = set()
            for row in cur.execute(
                """
                SELECT
                    COALESCE(NULLIF(source_market_name, ''), market_name) AS compare_name,
                    youpin_sell_price,
                    COALESCE(is_fresh_pair, 0) AS is_fresh_pair,
                    COALESCE(verify_status, '') AS verify_status
                FROM comparisons
                """
            ).fetchall():
                compare_name = str(row["compare_name"] or "").strip()
                if not compare_name:
                    continue
                comparison_names_all.add(compare_name)
                if row["youpin_sell_price"] is not None:
                    comparison_names_with_price.add(compare_name)
                    if int(row["is_fresh_pair"] or 0) == 1:
                        comparison_names_fresh.add(compare_name)
                if str(row["verify_status"] or "").strip() == "verified_live":
                    comparison_names_live.add(compare_name)

            template_ids_all: set[int] = set()
            template_ids_with_price: set[int] = set()
            for row in cur.execute(
                "SELECT template_id, price FROM youpin_prices"
            ).fetchall():
                try:
                    template_id = int(row["template_id"])
                except (TypeError, ValueError):
                    continue
                template_ids_all.add(template_id)
                if row["price"] is not None:
                    template_ids_with_price.add(template_id)

        def _ordered_hits(source: list, hit_set: set) -> list:
            return [item for item in source if item in hit_set]

        name_roll_hits = _ordered_hits(ordered_names, active_roll_names)
        name_comparison_hits = _ordered_hits(ordered_names, comparison_names_all)
        name_price_hits = _ordered_hits(ordered_names, comparison_names_with_price)
        name_fresh_hits = _ordered_hits(ordered_names, comparison_names_fresh)
        name_live_hits = _ordered_hits(ordered_names, comparison_names_live)
        template_all_hits = _ordered_hits(ordered_template_ids, template_ids_all)
        template_price_hits = _ordered_hits(ordered_template_ids, template_ids_with_price)

        return {
            "name_targets": ordered_names,
            "template_id_targets": ordered_template_ids,
            "roll_name_hits": name_roll_hits,
            "comparison_name_hits": name_comparison_hits,
            "priced_name_hits": name_price_hits,
            "fresh_name_hits": name_fresh_hits,
            "live_name_hits": name_live_hits,
            "template_id_hits": template_all_hits,
            "priced_template_id_hits": template_price_hits,
            "missing_names": [name for name in ordered_names if name not in comparison_names_all],
            "missing_priced_names": [
                name for name in ordered_names if name not in comparison_names_with_price
            ],
            "missing_fresh_names": [
                name for name in ordered_names if name not in comparison_names_fresh
            ],
            "missing_template_ids": [
                item_id for item_id in ordered_template_ids if item_id not in template_ids_all
            ],
            "missing_priced_template_ids": [
                item_id
                for item_id in ordered_template_ids
                if item_id not in template_ids_with_price
            ],
        }

    def query_watchlist_compare(
        self,
        *,
        watchlist_id: str,
        coin_rate: float,
        batch_id: str | None = None,
        keyword: str = "",
        item_type: str = "",
        wear: str = "",
        special_variant: str = "",
        status: str = "",
        min_available: int | None = None,
        min_roll_coin_price: float | None = None,
        max_roll_coin_price: float | None = None,
        min_youpin_price: float | None = None,
        max_youpin_price: float | None = None,
        min_profit: float | None = None,
        min_profit_rate: float | None = None,
        only_profitable: bool = False,
        sort_by: str = "roll_rmb_price",
        sort_order: str = "desc",
        secondary_sort_by: str = "profit_rate",
        secondary_sort_order: str = "desc",
    ) -> list[dict]:
        rows = self.list_watchlist_items(watchlist_id=watchlist_id, enabled_only=True)
        roll_bucket_by_key: dict[str, list[dict]] = {}
        yp_by_key: dict[str, dict] = {}
        with self._lock:
            roll_sql = "SELECT * FROM roll_trades WHERE source_mode = 'watchlist' AND COALESCE(watchlist_id,'') = ?"
            roll_params: list = [watchlist_id]
            if batch_id:
                roll_sql += " AND COALESCE(batch_id,'') = ?"
                roll_params.append(batch_id)
            for row in self._conn.execute(roll_sql, tuple(roll_params)).fetchall():
                key = str(row["normalized_compare_key"] or "").strip()
                if not key:
                    continue
                roll_bucket_by_key.setdefault(key, []).append(dict(row))

            yp_sql = "SELECT * FROM youpin_prices WHERE source_mode = 'watchlist' AND COALESCE(watchlist_id,'') = ?"
            yp_params: list = [watchlist_id]
            if batch_id:
                yp_sql += " AND COALESCE(batch_id,'') = ?"
                yp_params.append(batch_id)
            for row in self._conn.execute(yp_sql, tuple(yp_params)).fetchall():
                key = str(row["normalized_compare_key"] or "").strip()
                if key and key not in yp_by_key:
                    yp_by_key[key] = dict(row)

        out: list[dict] = []
        keyword_l = keyword.strip().lower()
        item_type_l = item_type.strip().lower()
        status_l = status.strip().lower()
        wear_filter = normalize_wear(wear) if wear else ""
        special_filter = normalize_special_variant(special_variant) if special_variant else ""

        def _pick_roll_row(candidates: list[dict]) -> tuple[dict | None, str]:
            if not candidates:
                return None, "roll_no_valid_11_5"
            # Rule: prefer exact 11.5%; fallback to >=9% nearest to 11.5; otherwise nearest overall.
            with_markup: list[tuple[dict, float]] = []
            for row in candidates:
                try:
                    markup = float(row.get("markup_percent") or 0.0)
                except Exception:
                    markup = 0.0
                with_markup.append((row, markup))
            exact = [row for row, markup in with_markup if abs(markup - 11.5) <= 1e-9]
            if exact:
                return exact[0], "ok"
            above9 = [(row, abs(markup - 11.5)) for row, markup in with_markup if markup >= 9.0]
            if above9:
                above9.sort(key=lambda x: x[1])
                return above9[0][0], "roll_fallback_nearest_9_plus"
            with_markup.sort(key=lambda x: abs(x[1] - 11.5))
            return with_markup[0][0], "roll_fallback_non_target"

        for item in rows:
            key = str(item.get("normalized_compare_key") or "").strip()
            if not key:
                continue
            raw_name = str(item.get("raw_name") or "")
            if keyword_l:
                text = " ".join([raw_name, str(item.get("cn_name") or ""), str(item.get("en_name") or ""), key]).lower()
                if keyword_l not in text:
                    continue
            item_wear = normalize_wear(item.get("wear_en") or item.get("wear_cn") or item.get("wear_abbr"))
            item_special = normalize_special_variant(item.get("special_variant"))
            row_item_type = str(item.get("en_name") or item.get("raw_name") or "").split("|")[0].strip()
            if item_type_l and item_type_l not in row_item_type.lower() and item_type_l not in raw_name.lower():
                continue
            if wear_filter and item_wear != wear_filter:
                continue
            if special_filter and item_special != special_filter:
                continue
            roll, roll_status = _pick_roll_row(roll_bucket_by_key.get(key, []))
            yp = yp_by_key.get(key)
            roll_coin_price = float(roll["price"]) if roll and roll.get("price") is not None else None
            roll_available_count = int(roll.get("roll_listing_count") or 0) if roll else 0
            yp_status = str(yp.get("status") or "price_missing") if yp else "price_missing"
            youpin_price = float(yp["price"]) if yp and yp.get("price") is not None and float(yp["price"]) > 0 else None
            roll_rmb_price = (roll_coin_price * coin_rate) if roll_coin_price is not None else None
            profit = None
            profit_rate = None
            compare_status = "compare_incomplete"
            if roll_rmb_price is not None and youpin_price is not None and youpin_price > 0 and yp_status == "ok":
                profit = roll_rmb_price - youpin_price
                profit_rate = profit / youpin_price
                compare_status = "ok"
            if status_l and compare_status.lower() != status_l and yp_status.lower() != status_l and roll_status.lower() != status_l:
                continue
            if only_profitable and not (profit is not None and profit > 0):
                continue
            if min_roll_coin_price is not None and (roll_coin_price is None or roll_coin_price < min_roll_coin_price):
                continue
            if min_available is not None and roll_available_count < int(min_available):
                continue
            if max_roll_coin_price is not None and (roll_coin_price is None or roll_coin_price > max_roll_coin_price):
                continue
            if min_youpin_price is not None and (youpin_price is None or youpin_price < min_youpin_price):
                continue
            if max_youpin_price is not None and (youpin_price is None or youpin_price > max_youpin_price):
                continue
            if min_profit is not None and (profit is None or profit < min_profit):
                continue
            if min_profit_rate is not None and (profit_rate is None or profit_rate < min_profit_rate):
                continue
            out.append(
                {
                    "watchlist_id": watchlist_id,
                    "batch_id": batch_id or (roll.get("batch_id") if roll else yp.get("batch_id") if yp else None),
                    "source_mode": "watchlist",
                    "normalized_compare_key": key,
                    "raw_name": raw_name,
                    "roll_coin_price": roll_coin_price,
                    "roll_available_count": roll_available_count,
                    "coin_rate": coin_rate,
                    "roll_rmb_price": roll_rmb_price,
                    "youpin_price": youpin_price,
                    "profit": profit,
                    "profit_rate": profit_rate,
                    "roll_status": roll_status if roll_coin_price is not None else "roll_no_valid_11_5",
                    "youpin_status": yp_status,
                    "compare_status": compare_status,
                    "last_roll_update": int(roll.get("last_seen_at") or 0) if roll else 0,
                    "last_youpin_update": int(yp.get("updated_at") or 0) if yp else 0,
                    "item_type": row_item_type,
                    "wear": item_wear,
                    "special_variant": item_special,
                }
            )

        def _key(row: dict, name: str):
            val = row.get(name)
            if val is None:
                return (1, 0)
            return (0, val)

        out.sort(key=lambda row: _key(row, secondary_sort_by), reverse=str(secondary_sort_order).lower() == "desc")
        out.sort(key=lambda row: _key(row, sort_by), reverse=str(sort_order).lower() == "desc")
        return out

    def strict_rejection_counts(self) -> dict:
        with self._lock:
            cur = self._conn.cursor()
            not_verified = int(
                cur.execute(
                    """
                    SELECT COUNT(*) FROM comparisons
                    WHERE COALESCE(is_live_verified, 0) = 0
                       OR COALESCE(verify_status, '') != 'verified_live'
                       OR COALESCE(price_status, '') IN ('no_mapping', 'no_cached_price', 'verify_failed')
                    """
                ).fetchone()[0]
            )
            stale = int(
                cur.execute(
                    "SELECT COUNT(*) FROM comparisons WHERE COALESCE(is_fresh_pair, 0) = 0"
                ).fetchone()[0]
            )
            null_price = int(
                cur.execute(
                    """
                    SELECT COUNT(*) FROM comparisons
                    WHERE youpin_sell_price IS NULL
                       OR roll_converted_cost IS NULL
                       OR net_profit_rate IS NULL
                    """
                ).fetchone()[0]
            )
            anomaly = int(
                cur.execute(
                    "SELECT COUNT(*) FROM comparisons WHERE COALESCE(price_anomaly, 0) = 1"
                ).fetchone()[0]
            )
            return {
                "rejected_not_verified_count": not_verified,
                "rejected_stale_count": stale,
                "rejected_null_price_count": null_price,
                "rejected_anomaly_count": anomaly,
            }

    def latest_serving_live_verify_time(self) -> int:
        with self._lock:
            cur = self._conn.cursor()
            latest = cur.execute(
                """
                SELECT MAX(COALESCE(youpin_live_time, 0))
                FROM serving_comparisons
                WHERE is_current = 1 AND COALESCE(is_live_verified, 0) = 1
                """
            ).fetchone()[0]
            return int(latest or 0)

    def backfill_comparison_metadata(
        self,
        *,
        now_ts: int,
        youpin_fresh_seconds: int,
        roll_fresh_seconds: int,
    ) -> None:
        with self._lock:
            self._conn.execute(
                """
                UPDATE comparisons
                SET youpin_age_seconds = CASE
                        WHEN COALESCE(youpin_price_updated_at, 0) > 0 THEN MAX(0, ? - youpin_price_updated_at)
                        WHEN youpin_sell_price IS NOT NULL THEN MAX(0, ? - updated_at)
                        ELSE NULL
                    END,
                    roll_age_seconds = CASE
                        WHEN COALESCE(roll_last_seen_at, 0) > 0 THEN MAX(0, ? - roll_last_seen_at)
                        ELSE MAX(0, ? - updated_at)
                    END,
                    is_youpin_fresh = CASE
                        WHEN COALESCE(youpin_price_updated_at, 0) > 0 AND (? - youpin_price_updated_at) <= ? THEN 1
                        ELSE 0
                    END,
                    is_roll_fresh = CASE
                        WHEN COALESCE(roll_last_seen_at, 0) > 0 AND (? - roll_last_seen_at) <= ? THEN 1
                        ELSE CASE WHEN (? - updated_at) <= ? THEN 1 ELSE 0 END
                    END,
                    verify_status = COALESCE(NULLIF(verify_status, ''), CASE WHEN price_status = 'ok' THEN 'verified_cache' ELSE 'unverified' END),
                    verify_source = COALESCE(NULLIF(verify_source, ''), CASE WHEN price_status = 'ok' THEN 'cache' ELSE 'none' END),
                    is_live_verified = CASE
                        WHEN COALESCE(verify_status, '') = 'verified_live' THEN 1
                        ELSE COALESCE(is_live_verified, 0)
                    END,
                    live_verify_price = CASE
                        WHEN COALESCE(verify_status, '') = 'verified_live' THEN COALESCE(live_verify_price, youpin_sell_price)
                        ELSE live_verify_price
                    END,
                    live_verify_time = CASE
                        WHEN COALESCE(verify_status, '') = 'verified_live' THEN COALESCE(live_verify_time, verify_time, youpin_price_updated_at)
                        ELSE live_verify_time
                    END,
                    verify_note = COALESCE(NULLIF(verify_note, ''), price_status_note),
                    data_confidence = CASE
                        WHEN bucket_risk_level = 'low' THEN 'high'
                        WHEN bucket_risk_level = 'medium' THEN 'medium'
                        ELSE 'low'
                    END
                """,
                (
                    now_ts,
                    now_ts,
                    now_ts,
                    now_ts,
                    now_ts,
                    youpin_fresh_seconds,
                    now_ts,
                    roll_fresh_seconds,
                    now_ts,
                    roll_fresh_seconds,
                ),
            )
            self._conn.execute(
                """
                UPDATE comparisons
                SET is_fresh_pair = CASE
                        WHEN COALESCE(is_youpin_fresh, 0) = 1 AND COALESCE(is_roll_fresh, 0) = 1 THEN 1
                        ELSE 0
                    END,
                    verified_fresh = CASE
                        WHEN COALESCE(verify_status, 'unverified') IN ('verified_cache', 'verified_live')
                         AND COALESCE(is_youpin_fresh, 0) = 1 THEN 1
                        ELSE 0
                    END,
                    stale_reason = CASE
                        WHEN youpin_sell_price IS NULL THEN 'missing_youpin_price'
                        WHEN COALESCE(is_youpin_fresh, 0) = 0 AND COALESCE(is_roll_fresh, 0) = 0 THEN 'stale_youpin,stale_roll'
                        WHEN COALESCE(is_youpin_fresh, 0) = 0 THEN 'stale_youpin'
                        WHEN COALESCE(is_roll_fresh, 0) = 0 THEN 'stale_roll'
                        ELSE ''
                    END,
                    decision_grade = CASE
                        WHEN youpin_sell_price IS NULL OR price_status = 'no_mapping' THEN 'X'
                        WHEN verify_status = 'verify_failed' THEN 'D'
                        WHEN COALESCE(is_fresh_pair, 0) = 1
                         AND verify_status = 'verified_live'
                         AND COALESCE(verified_fresh, 0) = 1
                         AND COALESCE(net_profit_rate, -999999) >= 10
                         AND COALESCE(bucket_risk_level, 'high') IN ('low', 'medium') THEN 'A'
                        WHEN COALESCE(is_fresh_pair, 0) = 1
                         AND verify_status IN ('verified_cache', 'unverified')
                         AND COALESCE(net_profit_rate, -999999) >= 8
                         AND COALESCE(bucket_risk_level, 'high') != 'high' THEN 'B'
                        WHEN COALESCE(is_youpin_fresh, 0) = 1 OR COALESCE(is_roll_fresh, 0) = 1 THEN 'C'
                        ELSE 'D'
                    END,
                    decision_note = CASE
                        WHEN youpin_sell_price IS NULL OR price_status = 'no_mapping' THEN 'Missing price or mapping'
                        WHEN verify_status = 'verify_failed' THEN 'Live verification failed'
                        WHEN COALESCE(is_fresh_pair, 0) = 1
                         AND verify_status = 'verified_live'
                         AND COALESCE(verified_fresh, 0) = 1
                         AND COALESCE(net_profit_rate, -999999) >= 10
                         AND COALESCE(bucket_risk_level, 'high') IN ('low', 'medium') THEN 'Fresh pair with live verification'
                        WHEN COALESCE(is_fresh_pair, 0) = 1
                         AND verify_status IN ('verified_cache', 'unverified')
                         AND COALESCE(net_profit_rate, -999999) >= 8
                         AND COALESCE(bucket_risk_level, 'high') != 'high' THEN 'Fresh pair with usable cached verification'
                        WHEN COALESCE(is_youpin_fresh, 0) = 1 OR COALESCE(is_roll_fresh, 0) = 1 THEN 'Partially fresh or medium confidence'
                        ELSE 'Stale or high-risk data'
                    END
                """
            )
            self._conn.commit()

    def stats(self) -> dict:
        with self._lock:
            cur = self._conn.cursor()
            roll_count = cur.execute("SELECT COUNT(*) FROM roll_trades").fetchone()[0]
            catalog_count = cur.execute("SELECT COUNT(*) FROM product_catalog").fetchone()[0]
            active_catalog_count = cur.execute(
                "SELECT COUNT(*) FROM product_catalog WHERE COALESCE(is_active, 0) = 1"
            ).fetchone()[0]
            comp_count = cur.execute("SELECT COUNT(*) FROM comparisons").fetchone()[0]
            yp_count = cur.execute("SELECT COUNT(*) FROM youpin_prices").fetchone()[0]
            last_comp = cur.execute("SELECT MAX(updated_at) FROM comparisons").fetchone()[0]
            snap_count = cur.execute(
                "SELECT COUNT(*) FROM comparison_snapshots"
            ).fetchone()[0]
            last_snap = cur.execute(
                "SELECT MAX(snapshot_at) FROM comparison_snapshots"
            ).fetchone()[0]
            roll_daily_count = cur.execute(
                "SELECT COUNT(*) FROM roll_daily_snapshot"
            ).fetchone()[0]
            roll_daily_last = cur.execute(
                "SELECT MAX(captured_at) FROM roll_daily_snapshot"
            ).fetchone()[0]
            last_verify = cur.execute(
                "SELECT MAX(run_at) FROM verification_runs"
            ).fetchone()[0]
            return {
                "roll_trades": int(roll_count),
                "product_catalog": int(catalog_count),
                "active_product_catalog": int(active_catalog_count),
                "comparisons": int(comp_count),
                "youpin_prices": int(yp_count),
                "last_comparison_update": int(last_comp or 0),
                "comparison_snapshots": int(snap_count),
                "last_snapshot_at": int(last_snap or 0),
                "roll_daily_snapshot_rows": int(roll_daily_count or 0),
                "roll_daily_snapshot_at": int(roll_daily_last or 0),
                "last_verification_at": int(last_verify or 0),
            }
