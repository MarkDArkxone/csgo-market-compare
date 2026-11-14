from __future__ import annotations

import logging
from dataclasses import dataclass
import json

from config.settings import Settings
from shared.http_source import BaseHTTPSource, DataSourceError
from shared.special_items import detect_special_candidate
from shared.utils import normalize_name_identity, safe_float

CSGOROLL_GRAPHQL_URL = "https://api-trader.csgoroll.com/graphql"

TRADES_QUERY = """
query ($first: PaginationAmount, $after: String, $status: TradeStatus, $orderBy: [TradeOrderBy]) {
  trades(first: $first, after: $after, status: $status, orderBy: $orderBy) {
    edges {
      node {
        id
        status
        totalValue
        markupPercent
        tradeItems {
          marketName
          value
          markupPercent
          itemVariant {
            externalId
            value
            name
            categories {
              edges {
                node {
                  name
                }
              }
            }
          }
        }
      }
    }
    pageInfo {
      hasNextPage
      endCursor
    }
  }
}
""".strip()


@dataclass
class TradePage:
    trades: list[dict]
    has_next_page: bool
    end_cursor: str | None
    multi_item_trades_seen: int
    multi_item_trades_split: int
    multi_item_trades_skipped: int


logger = logging.getLogger(__name__)


class CSGORollSource(BaseHTTPSource):
    def __init__(self, settings: Settings):
        super().__init__(
            settings=settings,
            source_name="csgoroll",
            max_retries=settings.roll_max_retries,
            default_headers={
                "Content-Type": "application/json",
                "User-Agent": settings.csgoroll_user_agent,
                "Origin": "https://www.csgoroll.com",
                "Referer": "https://www.csgoroll.com/",
            },
        )
        self.url = CSGOROLL_GRAPHQL_URL

    async def fetch_trade_page(
        self, *, after: str | None, first: int, status: str
    ) -> TradePage:
        payload = {
            "query": TRADES_QUERY,
            "variables": {
                "first": first,
                "after": after,
                "status": status,
                "orderBy": list(self.settings.roll_order_by),
            },
        }
        data = await self.request_json("POST", self.url, payload=payload)

        trades_obj = (data.get("data") or {}).get("trades")
        if not trades_obj:
            raise DataSourceError("csgoroll response missing trades")

        parsed: list[dict] = []
        multi_item_seen = 0
        multi_item_split = 0
        multi_item_skipped = 0
        for edge in trades_obj.get("edges") or []:
            node = (edge or {}).get("node") or {}
            trade_items = node.get("tradeItems") or []
            if not trade_items:
                continue

            if len(trade_items) > 1:
                multi_item_seen += 1
                if self.settings.roll_multi_item_mode == "skip":
                    multi_item_skipped += 1
                    logger.info(
                        "Skip CSGORoll multi-item trade (count=%d, trade_id=%s)",
                        len(trade_items),
                        str(node.get("id") or ""),
                    )
                    continue
                multi_item_split += 1

            for idx, item in enumerate(trade_items):
                parsed_row = self._parse_trade_item(
                    node=node,
                    item=item,
                    item_index=idx,
                    single_item_trade=(len(trade_items) == 1),
                )
                if parsed_row:
                    parsed.append(parsed_row)

        page_info = trades_obj.get("pageInfo") or {}
        return TradePage(
            trades=parsed,
            has_next_page=bool(page_info.get("hasNextPage")),
            end_cursor=page_info.get("endCursor"),
            multi_item_trades_seen=multi_item_seen,
            multi_item_trades_split=multi_item_split,
            multi_item_trades_skipped=multi_item_skipped,
        )

    def _parse_trade_item(
        self,
        *,
        node: dict,
        item: dict,
        item_index: int,
        single_item_trade: bool,
    ) -> dict | None:
        market_name = str(item.get("marketName") or "").strip()
        if not market_name:
            return None

        if self._should_exclude_by_category(item):
            return None

        price = safe_float(item.get("value"))
        if price is None:
            # For split mode, fallback to total value only when it's a real single-item listing.
            if single_item_trade:
                price = safe_float(node.get("totalValue"))
            if price is None:
                return None
        markup = safe_float(item.get("markupPercent"))
        if markup is None:
            markup = safe_float(node.get("markupPercent"))
        if markup is None:
            return None

        variant_name = str(((item.get("itemVariant") or {}).get("name") or "")).strip()
        category_names = self._collect_category_names(item)
        identity = normalize_name_identity(market_name)
        special_signal = detect_special_candidate(
            market_name,
            variant_name,
            " ".join(category_names),
        )
        trade_id_core = str(node.get("id") or "")
        if not trade_id_core:
            return None
        trade_id = trade_id_core if single_item_trade else f"{trade_id_core}#{item_index}"
        return {
            "trade_id": trade_id,
            "status": str(node.get("status") or "LISTED"),
            "market_name": market_name,
            "base_name": identity.base_name,
            "wear_code": identity.wear_code,
            "wear_label": identity.wear_label,
            "variant_group": identity.variant_group,
            "variant_label": identity.variant_label,
            "variant_tag": identity.variant_label,
            "item_variant_name": variant_name,
            "item_external_id": str(((item.get("itemVariant") or {}).get("externalId") or "")).strip(),
            "item_category_names": json.dumps(category_names, ensure_ascii=False),
            "is_special_candidate": special_signal.is_special_candidate,
            "is_special_confirmed": False,
            "special_variant": special_signal.special_variant,
            "special_variant_type": special_signal.special_variant_type,
            "priority_score": 0.0,
            "detail_last_fetched_at": 0,
            "price": price,
            "markup_percent": markup,
        }

    def _collect_category_names(self, trade_item: dict) -> list[str]:
        item_variant = trade_item.get("itemVariant") or {}
        categories_obj = item_variant.get("categories") or {}
        edges = categories_obj.get("edges") or []
        names: list[str] = []
        for edge in edges:
            node = (edge or {}).get("node") or {}
            raw_name = node.get("name")
            if isinstance(raw_name, str) and raw_name.strip():
                names.append(raw_name.strip())
        return names

    def _should_exclude_by_category(self, trade_item: dict) -> bool:
        names = self._collect_category_names(trade_item)
        normalized = {name.lower() for name in names}
        if normalized & self.settings.roll_excluded_categories:
            return True

        # Fallback: if category list is empty, still block obvious sticker names.
        market_name = str(trade_item.get("marketName") or "").strip().lower()
        if market_name.startswith("sticker |"):
            return True

        return False

    def detect_semantic_failure(self, data: dict) -> tuple[bool, str] | None:
        errors = data.get("errors")
        if not isinstance(errors, list) or not errors:
            return None

        first = errors[0] if isinstance(errors[0], dict) else {}
        message = str(first.get("message") or "csgoroll graphql error")
        # GraphQL errors (including auth/rate-limit semantics) should trigger backoff.
        return (True, f"csgoroll graphql error: {message}")
