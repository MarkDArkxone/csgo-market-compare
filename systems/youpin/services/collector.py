from __future__ import annotations

import asyncio
import logging

from config.settings import Settings
from shared.http_source import DataSourceError, RateLimitedError
from systems.youpin.data_sources.youpin_source import YoupinSource
from shared.market_watchlist import load_market_watch_targets
from shared.name_mapper import NameMapper
from shared.normalizer import build_normalized_compare_key
from shared.repository import Repository
from shared.utils import now_ts

logger = logging.getLogger(__name__)


class YoupinCollector:
    def __init__(
        self,
        settings: Settings,
        repo: Repository,
        mapper: NameMapper,
        source: YoupinSource,
    ):
        self.settings = settings
        self.repo = repo
        self.mapper = mapper
        self.source = source

    async def run_once(self) -> dict:
        watchlist_id = (self.repo.get_state("watchlist_active_id") or "default").strip() or "default"
        active_batch_id = (self.repo.get_state("watchlist_active_batch_id") or "").strip()
        if not active_batch_id:
            active_batch_id = self.repo.create_batch_id()
            self.repo.set_state("watchlist_active_batch_id", active_batch_id)
        await self.mapper.ensure_loaded()
        watch_targets = load_market_watch_targets(self.settings.market_watchlist_file)
        template_ids = [int(item) for item in watch_targets.template_ids if int(item) > 0]

        attempted = 0
        success = 0
        failed = 0
        rate_limited = 0
        empty_price = 0

        for template_id in template_ids:
            attempted += 1
            market_name = f"template:{template_id}"
            cn_name = None
            normalized_key = build_normalized_compare_key(
                item_type="template",
                skin_name=str(template_id),
                wear=None,
                special_variant=None,
            )
            try:
                result = await self.source.fetch_template_price(template_id, market_name=market_name)
                price = result.get("price")
                if price is None:
                    empty_price += 1
                    self.repo.mark_youpin_failure(
                        template_id=template_id,
                        market_name=market_name,
                        cn_name=cn_name,
                        now_ts=now_ts(),
                        retry_after_seconds=60,
                        error_kind="empty_price",
                        error_message="empty price",
                        source_mode="watchlist",
                        batch_id=active_batch_id,
                        watchlist_id=watchlist_id,
                        normalized_compare_key=normalized_key,
                        status="price_missing",
                    )
                else:
                    success += 1
                    self.repo.upsert_youpin_price(
                        template_id=template_id,
                        market_name=market_name,
                        cn_name=cn_name,
                        commodity_name=result.get("commodity_name"),
                        price=float(price),
                        updated_at=now_ts(),
                        raw_json=result.get("raw_json"),
                        source_mode="watchlist",
                        batch_id=active_batch_id,
                        watchlist_id=watchlist_id,
                        normalized_compare_key=normalized_key,
                        status="ok",
                    )
            except RateLimitedError as exc:
                rate_limited += 1
                failed += 1
                self.repo.mark_youpin_failure(
                    template_id=template_id,
                    market_name=market_name,
                    cn_name=cn_name,
                    now_ts=now_ts(),
                    retry_after_seconds=120,
                    error_kind="rate_limited",
                    error_message=str(exc),
                    source_mode="watchlist",
                    batch_id=active_batch_id,
                    watchlist_id=watchlist_id,
                    normalized_compare_key=normalized_key,
                    status="request_failed",
                )
            except DataSourceError as exc:
                failed += 1
                err = str(exc).lower()
                if "cookie" in err and ("expired" in err or "invalid" in err):
                    mapped_status = "cookie_expired"
                elif "login" in err or "token" in err or "auth" in err:
                    mapped_status = "not_logged_in"
                elif "special" in err and "match" in err:
                    mapped_status = "special_variant_not_matched"
                elif "not found" in err:
                    mapped_status = "not_found"
                else:
                    mapped_status = "request_failed"
                self.repo.mark_youpin_failure(
                    template_id=template_id,
                    market_name=market_name,
                    cn_name=cn_name,
                    now_ts=now_ts(),
                    retry_after_seconds=60,
                    error_kind="data_source",
                    error_message=str(exc),
                    source_mode="watchlist",
                    batch_id=active_batch_id,
                    watchlist_id=watchlist_id,
                    normalized_compare_key=normalized_key,
                    status=mapped_status,
                )
            except Exception as exc:  # noqa: BLE001
                failed += 1
                self.repo.mark_youpin_failure(
                    template_id=template_id,
                    market_name=market_name,
                    cn_name=cn_name,
                    now_ts=now_ts(),
                    retry_after_seconds=60,
                    error_kind="unexpected",
                    error_message=str(exc),
                    source_mode="watchlist",
                    batch_id=active_batch_id,
                    watchlist_id=watchlist_id,
                    normalized_compare_key=normalized_key,
                    status="request_failed",
                )

            await asyncio.sleep(max(0.0, float(self.settings.youpin_per_request_delay_seconds)))

        return {
            "ok": failed == 0,
            "watchlist_id": watchlist_id,
            "batch_id": active_batch_id,
            "target_total": len(template_ids),
            "attempted": attempted,
            "success": success,
            "failed": failed,
            "rate_limited": rate_limited,
            "empty_price": empty_price,
        }
