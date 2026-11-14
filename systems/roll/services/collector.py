from __future__ import annotations

import asyncio
import logging

from config.settings import Settings
from shared.http_source import DataSourceError
from systems.roll.data_sources.csgoroll_source import CSGORollSource
from shared.normalizer import build_normalized_compare_key
from shared.repository import Repository
from shared.utils import now_ts

logger = logging.getLogger(__name__)


class RollCollector:
    def __init__(self, settings: Settings, repo: Repository, source: CSGORollSource):
        self.settings = settings
        self.repo = repo
        self.source = source

    async def run_once(self) -> dict:
        watchlist_id = (self.repo.get_state("watchlist_active_id") or "default").strip() or "default"
        active_batch_id = (self.repo.get_state("watchlist_active_batch_id") or "").strip()
        if not active_batch_id:
            active_batch_id = self.repo.create_batch_id()
            self.repo.set_state("watchlist_active_batch_id", active_batch_id)
        page_size = max(1, int(self.settings.roll_page_size))
        max_pages = max(1, int(self.settings.roll_pages_per_loop))
        after: str | None = None

        total_trades = 0
        pages = 0
        errors: list[str] = []
        multi_seen = 0
        multi_split = 0
        multi_skipped = 0

        for index in range(max_pages):
            try:
                page = await self.source.fetch_trade_page(
                    after=after,
                    first=page_size,
                    status=self.settings.roll_status,
                )
            except DataSourceError as exc:
                errors.append(str(exc))
                break
            except Exception as exc:  # noqa: BLE001
                errors.append(f"unexpected: {exc}")
                break

            pages += 1
            multi_seen += int(page.multi_item_trades_seen)
            multi_split += int(page.multi_item_trades_split)
            multi_skipped += int(page.multi_item_trades_skipped)

            trades = page.trades or []
            if trades:
                for trade in trades:
                    trade["normalized_compare_key"] = build_normalized_compare_key(
                        item_type=trade.get("base_name"),
                        skin_name=trade.get("market_name"),
                        wear=trade.get("wear_label"),
                        special_variant=trade.get("special_variant"),
                    )
                self.repo.upsert_roll_trades(
                    trades,
                    now_ts=now_ts(),
                    source_mode="watchlist",
                    batch_id=active_batch_id,
                    watchlist_id=watchlist_id,
                )
                total_trades += len(trades)

            after = page.end_cursor
            if not page.has_next_page or not after:
                break
            if index < (max_pages - 1):
                await asyncio.sleep(max(0.0, float(self.settings.roll_intra_page_delay_seconds)))

        expire_before = now_ts() - int(self.settings.roll_trade_ttl_seconds)
        removed = self.repo.cleanup_roll_trades(expire_before)
        return {
            "ok": not errors,
            "watchlist_id": watchlist_id,
            "batch_id": active_batch_id,
            "pages": pages,
            "trades_upserted": total_trades,
            "expired_removed": int(removed),
            "multi_item_seen": multi_seen,
            "multi_item_split": multi_split,
            "multi_item_skipped": multi_skipped,
            "errors": errors,
        }
