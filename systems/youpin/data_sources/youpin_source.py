from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
from typing import Callable
from typing import Any
from urllib.parse import urlparse

from config.settings import Settings
from shared.http_source import BaseHTTPSource, DataSourceError, RateLimitedError
from providers.youpin898 import YoupinAdapter
from providers.youpin898 import YoupinConfig
from providers.youpin898 import YoupinError
from shared.utils import safe_float

YOUPIN_COMMODITY_URL = (
    "https://api.youpin898.com/api/homepage/es/commodity/GetCsGoPagedList"
)
YOUPIN_TEMPLATE_DETAIL_URL = (
    "https://api.youpin898.com/api/homepage/pc/goods/market/queryTemplateDetail"
)
YOUPIN_SALE_LIST_URL = (
    "https://api.youpin898.com/api/homepage/pc/goods/market/queryOnSaleCommodityList"
)
YOUPIN_LEASE_LIST_URL = (
    "https://api.youpin898.com/api/homepage/pc/goods/market/queryOnLeaseCommodityList"
)
YOUPIN_TEMPLATE_SEARCH_URL = (
    "https://api.youpin898.com/api/homepage/es/template/GetCsGoPagedList"
)
YOUPIN_ACCOUNT_INFO_URL = "https://api.youpin898.com/api/user/Account/GetUserInfo"
YOUPIN_PURCHASE_FILTER_CONFIG_URL = (
    "https://api.youpin898.com/api/youpin/bff/trade/purchase/order/getTemplateFilterConfigV2"
)
YOUPIN_PURCHASE_ORDER_LIST_URL = (
    "https://api.youpin898.com/api/youpin/bff/trade/purchase/order/getTemplatePurchaseOrderListPC"
)
YOUPIN_PRICE_TREND_FILTER_INFO_URL = (
    "https://api.youpin898.com/api/youpin/price/trend/filter/info"
)
YOUPIN_PRICE_TREND_DATA_LIST_URL = (
    "https://api.youpin898.com/api/youpin/price/trend/dataList"
)

logger = logging.getLogger(__name__)

_EXACT_NAME_WHITESPACE_RE = re.compile(r"\s+")
_HEADER_CANONICAL_CASE = {
    "accept": "Accept",
    "accept-language": "Accept-Language",
    "authorization": "authorization",
    "content-type": "Content-Type",
    "cookie": "Cookie",
    "deviceid": "deviceId",
    "deviceuk": "deviceUk",
    "origin": "Origin",
    "referer": "Referer",
    "uk": "uk",
    "user-agent": "User-Agent",
}
_EXACT_NAME_REPLACEMENTS = (
    ("бя", "★"),
    ("â", "★"),
    ("鈽?", "★"),
    ("鈽?", "★"),
    ("(★)", "★"),
    ("（★）", "★"),
    ("StatTrakâ¢", "StatTrak™"),
    ("â¢", "™"),
)


# Re-declare the normalization table with ASCII-safe unicode escapes so
# exact-name fallback keeps working even if a prior source edit introduced
# mojibake literals into the file.
_EXACT_NAME_REPLACEMENTS = (
    ("\u659c\u891f", "\u2605"),
    ("\u8292\u8075\u805f", "\u2605"),
    ("\u95b3?", "\u2605"),
    ("(\u2605", "\u2605"),
    ("\u951b\u581a\u2026\u69c4\u951b?, ", "\u2605"),
    ("StatTrak\u8292\u805e\u5784", "StatTrak\u2122"),
    ("\u8292\u805e\u5784", "\u2122"),
)


class YoupinSource(BaseHTTPSource):
    def __init__(
        self,
        settings: Settings,
        cookie_provider: Callable[[], str | None] | None = None,
        request_profile_provider: Callable[[], dict[str, str] | None] | None = None,
        browser_context_requester: Any | None = None,
    ):
        super().__init__(
            settings=settings,
            source_name="youpin",
            max_retries=settings.youpin_max_retries,
            default_headers={
                "Content-Type": "application/json",
                "User-Agent": settings.csgoroll_user_agent,
                "Origin": "https://www.youpin898.com",
                "Referer": "https://www.youpin898.com/",
            },
        )
        self.url = YOUPIN_COMMODITY_URL
        self._detail_cache: dict[int, dict[str, Any]] = {}
        self._purchase_filter_cache: dict[int, dict[str, Any]] = {}
        self._cookie_provider = cookie_provider
        self._request_profile_provider = request_profile_provider
        self._browser_context_requester = browser_context_requester
        self._runtime_cookie_header: str | None = None
        self._runtime_request_profile: dict[str, str] | None = None
        self._special_item_runtime_meta: dict[str, Any] | None = None
        self._special_item_adapter = self._build_special_item_adapter()
        self._special_item_adapter_lock = threading.Lock()

    async def close(self) -> None:
        try:
            await super().close()
        finally:
            adapter = self._special_item_adapter
            self._special_item_adapter = None
            if adapter is not None:
                adapter.close()
            if self._browser_context_requester is not None:
                close_fn = getattr(self._browser_context_requester, "close", None)
                if callable(close_fn):
                    await close_fn()

    def browser_context_enabled(self) -> bool:
        requester = self._browser_context_requester
        return bool(
            requester is not None
            and callable(getattr(requester, "is_enabled", None))
            and requester.is_enabled()
        )

    def browser_context_status(self) -> dict[str, Any] | None:
        requester = self._browser_context_requester
        if requester is None:
            return None
        status_fn = getattr(requester, "status", None)
        if not callable(status_fn):
            return None
        try:
            status = status_fn()
        except Exception as exc:  # noqa: BLE001
            return {
                "enabled": self.browser_context_enabled(),
                "configured": False,
                "connected": False,
                "last_error": str(exc),
            }
        return status if isinstance(status, dict) else None

    def special_item_runtime_status(self) -> dict[str, Any]:
        runtime = self._resolve_special_item_runtime()
        token = runtime["token"]
        device_token = runtime["device_token"]
        device_id = runtime["device_id"]
        enabled = bool(self.settings.youpin898_detail_enabled)
        configured = bool(token and device_token and device_id)
        browser_status = self.browser_context_status() or {}
        reason = None
        if not enabled:
            reason = "detail_provider_disabled"
        elif not configured:
            reason = "missing_credentials"
        elif self._special_item_adapter is None:
            reason = "adapter_init_failed"
        return {
            "enabled": enabled,
            "configured": configured,
            "active": bool(self._special_item_adapter is not None),
            "source": runtime["source"],
            "updated_at": runtime["updated_at"],
            "token_preview": self._preview_secret(token),
            "device_token_preview": self._preview_secret(device_token),
            "device_id_preview": self._preview_secret(device_id),
            "app_version": runtime["app_version"] or None,
            "user_agent": runtime["user_agent"] or None,
            "browser_context_enabled": bool(browser_status.get("enabled")),
            "browser_context_configured": bool(browser_status.get("configured")),
            "browser_context_connected": bool(browser_status.get("connected")),
            "reason": reason,
        }

    def configure_special_item_runtime(
        self,
        *,
        token: str,
        device_token: str,
        device_id: str,
        app_version: str | None = None,
        user_agent: str | None = None,
        source: str = "runtime",
        updated_at: int | None = None,
    ) -> dict[str, Any]:
        self._special_item_runtime_meta = {
            "token": str(token or "").strip(),
            "device_token": str(device_token or "").strip(),
            "device_id": str(device_id or "").strip(),
            "app_version": str(app_version or "").strip(),
            "user_agent": str(user_agent or "").strip(),
            "source": str(source or "runtime").strip() or "runtime",
            "updated_at": int(updated_at or 0) or None,
        }
        self._rebuild_special_item_adapter()
        return self.special_item_runtime_status()

    def clear_special_item_runtime(self) -> dict[str, Any]:
        self._special_item_runtime_meta = None
        self._rebuild_special_item_adapter()
        return self.special_item_runtime_status()

    def configure_session_runtime(
        self,
        *,
        cookie_header: str | None,
        profile_headers: dict[str, str] | None,
    ) -> None:
        cookie_text = str(cookie_header or "").strip()
        self._runtime_cookie_header = cookie_text or None

        normalized_profile: dict[str, str] = {}
        for key, value in (profile_headers or {}).items():
            actual_key = str(key or "").strip()
            if not actual_key or value is None:
                continue
            actual_value = str(value).strip()
            if not actual_value:
                continue
            normalized_profile[actual_key] = actual_value
        self._runtime_request_profile = normalized_profile or None

    def clear_session_runtime(self) -> None:
        self._runtime_cookie_header = None
        self._runtime_request_profile = None

    def _build_special_item_adapter(self) -> YoupinAdapter | None:
        runtime = self._resolve_special_item_runtime()
        if not bool(self.settings.youpin898_detail_enabled):
            return None

        token = runtime["token"]
        device_token = runtime["device_token"]
        device_id = runtime["device_id"]
        if not token or not device_token or not device_id:
            return None

        config = YoupinConfig(
            base_url=runtime["base_url"],
            timeout=float(self.settings.youpin898_timeout_seconds),
            token=token,
            device_token=device_token,
            device_id=device_id,
            app_version=runtime["app_version"],
            user_agent=runtime["user_agent"],
        )
        try:
            return YoupinAdapter(config)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Youpin898 special-item adapter init failed: %s", exc)
            return None

    def _resolve_special_item_runtime(self) -> dict[str, Any]:
        runtime = dict(self._special_item_runtime_meta or {})
        default_app_version = str(self.settings.youpin898_app_version or "").strip()
        default_user_agent = str(self.settings.youpin898_user_agent or "").strip()
        runtime_app_version = str(runtime.get("app_version") or "").strip()
        resolved_app_version = self._pick_preferred_app_version(
            runtime_app_version,
            default_app_version,
        )
        return {
            "token": str(runtime.get("token") or self.settings.youpin898_token or "").strip(),
            "device_token": str(
                runtime.get("device_token") or self.settings.youpin898_device_token or ""
            ).strip(),
            "device_id": str(
                runtime.get("device_id") or self.settings.youpin898_device_id or ""
            ).strip(),
            "app_version": resolved_app_version,
            "user_agent": str(runtime.get("user_agent") or "").strip()
            or default_user_agent,
            "base_url": str(self.settings.youpin898_base_url or "").strip()
            or "https://api.youpin898.com",
            "source": str(runtime.get("source") or "").strip() or (
                "runtime" if runtime else "settings"
            ),
            "updated_at": int(runtime.get("updated_at") or 0) or None,
        }

    @staticmethod
    def _pick_preferred_app_version(runtime_version: str, default_version: str) -> str:
        runtime_text = str(runtime_version or "").strip()
        default_text = str(default_version or "").strip()
        if not runtime_text:
            return default_text
        if not default_text:
            return runtime_text
        runtime_parts = YoupinSource._parse_version_parts(runtime_text)
        default_parts = YoupinSource._parse_version_parts(default_text)
        if runtime_parts is None or default_parts is None:
            return runtime_text
        return runtime_text if runtime_parts >= default_parts else default_text

    @staticmethod
    def _parse_version_parts(version_text: str) -> tuple[int, ...] | None:
        text = str(version_text or "").strip()
        if not text:
            return None
        parts: list[int] = []
        for part in text.split("."):
            piece = part.strip()
            if not piece.isdigit():
                return None
            parts.append(int(piece))
        return tuple(parts)

    def _rebuild_special_item_adapter(self) -> None:
        with self._special_item_adapter_lock:
            previous_adapter = self._special_item_adapter
            self._special_item_adapter = self._build_special_item_adapter()
        if previous_adapter is not None and previous_adapter is not self._special_item_adapter:
            previous_adapter.close()

    async def _request_via_special_item_client(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        adapter = self._special_item_adapter
        if adapter is None:
            return None

        parsed = urlparse(str(url or "").strip())
        path = parsed.path or str(url or "").strip()
        if parsed.query:
            path = f"{path}?{parsed.query}"
        if not path:
            return None

        def _call() -> dict[str, Any]:
            assert self._special_item_adapter is not None
            with self._special_item_adapter_lock:
                client = getattr(self._special_item_adapter, "_client", None)
                if client is None:
                    raise RuntimeError("special item adapter client unavailable")
                return client.call_api(
                    method.upper(),
                    path,
                    params=params,
                    json=payload,
                )

        try:
            return await asyncio.to_thread(_call)
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "Youpin special-item client fallback failed method=%s path=%s error=%s",
                method,
                path,
                exc,
            )
            return None

    @staticmethod
    def _preview_secret(value: str | None) -> str | None:
        text = str(value or "").strip()
        if not text:
            return None
        if len(text) <= 10:
            return text
        return f"{text[:6]}...{text[-4:]}"

    async def _try_fetch_item_detail_for_search_result(
        self, item: dict[str, Any] | None
    ) -> dict[str, Any] | None:
        item_id = self._extract_item_id(item)
        adapter = self._special_item_adapter
        if adapter is None or not item_id:
            return None

        def _load_detail() -> dict[str, Any] | None:
            assert self._special_item_adapter is not None
            with self._special_item_adapter_lock:
                detail = self._special_item_adapter.get_item_detail(item_id)
            price = float(detail.price) if detail.price is not None else None
            steam_price = float(detail.steam_price) if detail.steam_price is not None else None
            return {
                "item_id": detail.item_id,
                "commodity_name": detail.commodity_name,
                "price": price,
                "steam_price": steam_price,
                "abrade": detail.abrade,
                "paint_seed": detail.paint_seed,
                "name_tags": list(detail.name_tags or []),
                "stickers": list(detail.stickers or []),
                "fetched_at": detail.fetched_at.isoformat(),
                "raw": dict(detail.raw or {}),
            }

        try:
            return await asyncio.to_thread(_load_detail)
        except YoupinError as exc:
            logger.warning(
                "Youpin898 detail enrich failed item_id=%s error=%s",
                item_id,
                exc,
            )
            return None
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Youpin898 detail enrich unexpected failure item_id=%s error=%s",
                item_id,
                exc,
            )
            return None

    @staticmethod
    def _extract_item_id(item: dict[str, Any] | None) -> str | None:
        if not isinstance(item, dict):
            return None
        for key in (
            "Id",
            "id",
            "CommodityId",
            "commodityId",
            "ItemId",
            "itemId",
            "SellNo",
            "sellNo",
            "CommodityNo",
            "commodityNo",
        ):
            value = str(item.get(key) or "").strip()
            if value:
                return value
        return None

    @staticmethod
    def _extract_template_id(item: dict[str, Any] | None) -> int | None:
        if not isinstance(item, dict):
            return None
        for key in (
            "TemplateId",
            "templateId",
            "template_id",
            "TemplateID",
            "CommodityTemplateId",
            "commodityTemplateId",
        ):
            raw = item.get(key)
            if raw is None:
                continue
            try:
                value = int(str(raw).strip())
            except (TypeError, ValueError):
                continue
            if value > 0:
                return value
        return None

    @staticmethod
    def _build_combined_raw_json(
        search_payload: dict[str, Any] | list[dict[str, Any]] | None,
        detail_payload: dict[str, Any] | None,
    ) -> str:
        payload: dict[str, Any] = {"search": search_payload or {}}
        if detail_payload:
            payload["detail"] = detail_payload
        return json.dumps(payload, ensure_ascii=False)

    @staticmethod
    def _variant_result_from_template_result(
        template_result: dict[str, Any],
        variant_label: str,
    ) -> dict[str, Any]:
        return {
            "template_id": template_result.get("template_id"),
            "price": template_result.get("price"),
            "commodity_name": template_result.get("commodity_name"),
            "variant_label": variant_label,
            "special_style_name": template_result.get("special_style_name"),
            "special_style_type": template_result.get("special_style_type"),
            "item_id": template_result.get("item_id"),
            "detail_price": template_result.get("detail_price"),
            "steam_price": template_result.get("steam_price"),
            "abrade": template_result.get("abrade"),
            "paint_seed": template_result.get("paint_seed"),
            "name_tags": list(template_result.get("name_tags") or []),
            "stickers": list(template_result.get("stickers") or []),
            "raw_json": str(template_result.get("raw_json") or "{}"),
        }

    async def fetch_template_price(
        self,
        template_id: int,
        market_name: str | None = None,
    ) -> dict:
        detail_payload: dict[str, Any] | None = None
        detail_error: Exception | None = None
        exact_error: Exception | None = None
        allow_special_template_fallback = False
        try:
            detail_payload = await self.fetch_template_detail(template_id)
            allow_special_template_fallback = self._has_special_style_filters(
                detail_payload
            )
            detail_price = self._extract_template_detail_price(detail_payload, template_id)
            if detail_price is not None and detail_price > 0:
                return {
                    "template_id": template_id,
                    "price": detail_price,
                    "commodity_name": self.extract_display_name(detail_payload),
                    "raw_json": json.dumps({"detail": detail_payload}, ensure_ascii=False),
                }
        except Exception as exc:  # noqa: BLE001
            detail_error = exc

        search_error: Exception | None = None
        try:
            payload = {
                "listSortType": 1,
                "listType": 10,
                "pageIndex": 1,
                "pageSize": 20,
                "sortType": 1,
                "templateId": str(template_id),
            }
            data = await self._request_sensitive_json(
                "POST",
                self.url,
                payload=payload,
                headers=self._auth_headers(),
            )
            commodity_list = self._extract_commodity_list(data)
            prices = []
            for item in commodity_list:
                value = self._extract_search_price(item)
                if value is not None and value > 0:
                    prices.append(value)

            price = min(prices) if prices else None
            if price is not None and price > 0:
                commodity_name = None
                if commodity_list:
                    commodity_name = self._extract_item_name(commodity_list[0])
                if not commodity_name:
                    commodity_name = self.extract_display_name(detail_payload)
                return {
                    "template_id": template_id,
                    "price": price,
                    "commodity_name": commodity_name,
                    "raw_json": json.dumps(
                        {"detail": detail_payload or {}, "search": data},
                        ensure_ascii=False,
                    ),
                }
            search_error = DataSourceError(
                f"youpin empty price list for template_id={template_id}"
            )
        except Exception as exc:  # noqa: BLE001
            search_error = exc

        if market_name:
            try:
                exact_result = await self.search_exact_name_price(market_name)
                return {
                    "template_id": template_id,
                    "price": exact_result.get("price"),
                    "commodity_name": exact_result.get("commodity_name"),
                    "item_id": exact_result.get("item_id"),
                    "detail_price": exact_result.get("detail_price"),
                    "steam_price": exact_result.get("steam_price"),
                    "abrade": exact_result.get("abrade"),
                    "paint_seed": exact_result.get("paint_seed"),
                    "name_tags": exact_result.get("name_tags"),
                    "stickers": exact_result.get("stickers"),
                    "raw_json": json.dumps(
                        {
                            "detail": detail_payload or {},
                            "exact_name": json.loads(
                                str(exact_result.get("raw_json") or "{}")
                            ),
                        },
                        ensure_ascii=False,
                    ),
                }
            except Exception as exc:  # noqa: BLE001
                exact_error = exc

        trend_result = await self._try_fetch_template_trend_price(
            template_id=template_id,
            detail=detail_payload,
        )
        if trend_result is not None:
            return trend_result

        lease_result = await self._try_fetch_template_lease_price(
            template_id=template_id,
            detail=detail_payload,
            allow_special_templates=allow_special_template_fallback,
        )
        if lease_result is not None:
            return lease_result

        purchase_result = await self._try_fetch_template_purchase_price(
            template_id=template_id,
            detail=detail_payload,
            allow_special_templates=allow_special_template_fallback,
        )
        if purchase_result is not None:
            return purchase_result

        if exact_error is not None:
            raise exact_error
        if search_error is not None:
            raise search_error
        if detail_error is not None:
            raise detail_error
        raise DataSourceError(f"youpin empty price list for template_id={template_id}")

    async def fetch_template_detail(self, template_id: int) -> dict:
        cached = self._detail_cache.get(int(template_id))
        if cached:
            return cached

        payload = {
            "gameId": "730",
            "listType": "10",
            "templateId": str(template_id),
        }
        try:
            data = await self._request_sensitive_json(
                "POST",
                YOUPIN_TEMPLATE_DETAIL_URL,
                payload=payload,
                headers=self._auth_headers(),
            )
        except Exception:
            data = await self._request_via_special_item_client(
                "POST",
                YOUPIN_TEMPLATE_DETAIL_URL,
                payload=payload,
            )
            if data is None:
                raise
        data_obj = data.get("Data") or {}
        self._detail_cache[int(template_id)] = data_obj
        return data_obj

    async def fetch_variant_price(
        self,
        template_id: int,
        variant_label: str,
        market_name: str | None = None,
    ) -> dict:
        detail: dict[str, Any] | None = None
        style: dict[str, Any] | None = None
        try:
            detail = await self.fetch_template_detail(template_id)
            style = self._resolve_special_style(detail, variant_label)
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "Youpin template detail unavailable for variant lookup template_id=%s variant=%s error=%s",
                template_id,
                variant_label,
                exc,
            )

        prices = []
        matched_items: list[dict[str, Any]] = []
        best_raw_data: dict[str, Any] | None = None
        max_pages = self._variant_scan_page_limit(variant_label)
        auth_headers = self._auth_headers()
        commodity_scan_errors: list[str] = []
        sale_list_errors: list[str] = []
        if auth_headers:
            for page_index in range(1, max_pages + 1):
                payload = {
                    "listSortType": 2,
                    "listType": 10,
                    "pageIndex": page_index,
                    "pageSize": 100,
                    "sortType": 0,
                    "templateId": str(template_id),
                }
                try:
                    data = await self.request_json(
                        "POST",
                        self.url,
                        payload=payload,
                        headers=auth_headers,
                    )
                except Exception as exc:  # noqa: BLE001
                    commodity_scan_errors.append(str(exc))
                    break
                commodity_list = self._extract_commodity_list(data)
                page_matches = [
                    item
                    for item in commodity_list
                    if self._item_matches_variant(item, variant_label)
                ]
                for item in page_matches:
                    value = self._extract_search_price(item)
                    if value is None or value <= 0:
                        continue
                    prices.append(value)
                    matched_items.append(item)
                    if best_raw_data is None or value <= min(prices):
                        best_raw_data = data
                if len(commodity_list) < 100:
                    break

        if not prices and not style:
            try:
                detail = await self.fetch_template_detail(template_id)
                style = self._resolve_special_style(detail, variant_label)
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    "Youpin template detail retry unavailable for special style template_id=%s variant=%s error=%s",
                    template_id,
                    variant_label,
                    exc,
                )

        if not prices and style:
            for page_index in range(1, max_pages + 1):
                payload = {
                    "gameId": "730",
                    "listSortType": 1,
                    "listType": 10,
                    "pageIndex": page_index,
                    "pageSize": 100,
                    "sortType": 0,
                    "templateId": str(template_id),
                    "keyWords": style["abbr_title"],
                }
                try:
                    data = await self._request_sensitive_json(
                        "POST",
                        YOUPIN_SALE_LIST_URL,
                        payload=payload,
                        headers=auth_headers,
                    )
                except Exception as exc:  # noqa: BLE001
                    sale_list_errors.append(str(exc))
                    break
                commodity_list = self._extract_commodity_list(data)
                page_matches = [
                    item
                    for item in commodity_list
                    if self._item_matches_variant(item, variant_label)
                ]
                for item in page_matches:
                    value = self._extract_search_price(item)
                    if value is None or value <= 0:
                        continue
                    prices.append(value)
                    matched_items.append(item)
                    if best_raw_data is None or value <= min(prices):
                        best_raw_data = data
                if len(commodity_list) < 100:
                    break

        if not prices:
            trend_result = await self._try_fetch_variant_trend_price(
                template_id=template_id,
                variant_label=variant_label,
                detail=detail,
                style=style,
            )
            if trend_result is not None:
                return trend_result

            lease_result = await self._try_fetch_variant_lease_price(
                template_id=template_id,
                variant_label=variant_label,
                detail=detail,
                style=style,
            )
            if lease_result is not None:
                return lease_result

            purchase_result = await self._try_fetch_variant_purchase_price(
                template_id=template_id,
                variant_label=variant_label,
                detail=detail,
                style=style,
            )
            if purchase_result is not None:
                return purchase_result

            if not style:
                template_result = await self._try_fetch_template_lease_price(
                    template_id=template_id,
                    detail=detail,
                    allow_special_templates=True,
                )
                if template_result is None:
                    template_result = await self._try_fetch_template_purchase_price(
                        template_id=template_id,
                        detail=detail,
                        allow_special_templates=True,
                    )
                if template_result is not None:
                    return self._variant_result_from_template_result(
                        template_result,
                        variant_label,
                    )

            if market_name:
                try:
                    exact_result = await self.search_exact_name_price(market_name)
                except Exception:  # noqa: BLE001
                    exact_result = None
                if exact_result is not None:
                    exact_name = str(exact_result.get("commodity_name") or "").strip()
                    exact_item = {
                        "CommodityName": exact_name,
                        "SpecialStyleName": exact_result.get("special_style_name"),
                    }
                    if self._item_matches_variant(exact_item, variant_label):
                        exact_price = safe_float(exact_result.get("price"))
                        if exact_price is not None and exact_price > 0:
                            return {
                                "template_id": template_id,
                                "price": exact_price,
                                "commodity_name": exact_name or market_name,
                                "variant_label": variant_label,
                                "special_style_name": (
                                    str(exact_result.get("special_style_name") or "").strip()
                                    or variant_label
                                ),
                                "special_style_type": (style or {}).get("abbr_type"),
                                "item_id": exact_result.get("item_id"),
                                "detail_price": exact_result.get("detail_price"),
                                "steam_price": exact_result.get("steam_price"),
                                "abrade": exact_result.get("abrade"),
                                "paint_seed": exact_result.get("paint_seed"),
                                "name_tags": list(exact_result.get("name_tags") or []),
                                "stickers": list(exact_result.get("stickers") or []),
                                "raw_json": str(exact_result.get("raw_json") or "{}"),
                            }

        price = min(prices) if prices else None
        if price is None:
            error_parts: list[str] = []
            if commodity_scan_errors:
                error_parts.append(
                    f"commodity_scan={' | '.join(commodity_scan_errors[:2])}"
                )
            if sale_list_errors:
                error_parts.append(f"sale_list={' | '.join(sale_list_errors[:2])}")
            if not style:
                error_parts.append("special_style_missing")
            suffix = f" {' '.join(error_parts)}" if error_parts else ""
            raise DataSourceError(
                f"youpin empty variant price list for template_id={template_id} variant={variant_label}{suffix}"
            )

        best_item = min(
            matched_items,
            key=lambda item: self._extract_search_price(item) or 10**18,
        ) if matched_items else None
        commodity_name = None
        if best_item:
            commodity_name = self._extract_item_name(best_item)

        detail_payload = await self._try_fetch_item_detail_for_search_result(best_item)
        if detail_payload:
            detail_price = detail_payload.get("price")
            if detail_price is not None and detail_price > 0:
                price = detail_price
            commodity_name = str(detail_payload.get("commodity_name") or commodity_name or "").strip() or commodity_name

        return {
            "template_id": template_id,
            "price": price,
            "commodity_name": commodity_name,
            "variant_label": variant_label,
            "special_style_name": self._extract_variant_style_name(best_item)
            or (style["abbr_title"] if style else variant_label),
            "special_style_type": style["abbr_type"] if style else None,
            "item_id": detail_payload.get("item_id") if detail_payload else self._extract_item_id(best_item),
            "detail_price": detail_payload.get("price") if detail_payload else None,
            "steam_price": detail_payload.get("steam_price") if detail_payload else None,
            "abrade": detail_payload.get("abrade") if detail_payload else None,
            "paint_seed": detail_payload.get("paint_seed") if detail_payload else None,
            "name_tags": detail_payload.get("name_tags") if detail_payload else [],
            "stickers": detail_payload.get("stickers") if detail_payload else [],
            "raw_json": self._build_combined_raw_json(best_raw_data or {}, detail_payload),
        }

    async def search_exact_name_price(self, market_name: str) -> dict:
        keyword = self._normalize_exact_match_text(market_name)
        if not keyword:
            raise DataSourceError("youpin exact-name search keyword is empty")

        last_error: Exception | None = None
        for list_type in ("30", "10"):
            payload = {
                "listType": list_type,
                "gameId": "730",
                "keyWords": keyword,
                "pageIndex": 1,
                "pageSize": 20,
                "sortType": "0",
                "listSortType": "2",
            }
            try:
                try:
                    data = await self._request_sensitive_json(
                        "POST",
                        YOUPIN_TEMPLATE_SEARCH_URL,
                        payload=payload,
                        headers=self._legacy_auth_headers(),
                    )
                except Exception:
                    data = await self._request_via_special_item_client(
                        "POST",
                        YOUPIN_TEMPLATE_SEARCH_URL,
                        payload=payload,
                    )
                    if data is None:
                        raise
                items = self._extract_search_items(data)
                best = self._select_best_exact_match(items, keyword)
                if best is None:
                    last_error = DataSourceError(
                        f"youpin exact-name search found no exact match for {keyword}"
                    )
                    continue
                detail_payload = await self._try_fetch_item_detail_for_search_result(best)
                price = self._extract_search_price(best)
                if detail_payload:
                    detail_price = detail_payload.get("price")
                    if detail_price is not None and detail_price > 0:
                        price = detail_price
                return {
                    "market_name": keyword,
                    "commodity_name": str(
                        detail_payload.get("commodity_name")
                        if detail_payload
                        else (
                            best.get("CommodityName")
                            or best.get("Name")
                            or best.get("MarketHashName")
                            or best.get("TemplateName")
                        )
                        or ""
                    ).strip()
                    or None,
                    "price": price,
                    "item_id": detail_payload.get("item_id") if detail_payload else self._extract_item_id(best),
                    "detail_price": detail_payload.get("price") if detail_payload else None,
                    "steam_price": detail_payload.get("steam_price") if detail_payload else None,
                    "abrade": detail_payload.get("abrade") if detail_payload else None,
                    "paint_seed": detail_payload.get("paint_seed") if detail_payload else None,
                    "name_tags": detail_payload.get("name_tags") if detail_payload else [],
                    "stickers": detail_payload.get("stickers") if detail_payload else [],
                    "template_id": self._extract_template_id(best),
                    "raw_json": self._build_combined_raw_json(data, detail_payload),
                    "search_list_type": list_type,
                }
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                continue

        if last_error:
            raise last_error
        raise DataSourceError(f"youpin exact-name search failed for {keyword}")

    def extract_display_name(self, payload: dict[str, Any] | None) -> str | None:
        if not isinstance(payload, dict):
            return None
        for key in (
            "commodityHashName",
            "CommodityHashName",
            "marketHashName",
            "MarketHashName",
        ):
            value = str(payload.get(key) or "").strip()
            if value:
                return value
        direct = self._extract_item_name(payload)
        if direct:
            return direct
        for key in (
            "TemplateInfo",
            "templateInfo",
            "CommodityInfo",
            "commodityInfo",
            "GoodsInfo",
            "goodsInfo",
            "Data",
            "data",
        ):
            value = payload.get(key)
            if not isinstance(value, dict):
                continue
            nested = self._extract_item_name(value)
            if nested:
                return nested
        return None

    async def check_session_status(self) -> dict:
        keyword = "AK-47 | Redline (Field-Tested)"
        payload = {
            "listType": "30",
            "gameId": "730",
            "keyWords": keyword,
            "pageIndex": 1,
            "pageSize": 5,
            "sortType": "0",
            "listSortType": "2",
        }
        if not self._auth_headers() and not self.browser_context_enabled():
            return {
                "valid": False,
                "error": "No imported Youpin cookie and browser-context mode is disabled",
                "code": None,
            }

        browser_context_result = await self._check_browser_context_session_status()
        if browser_context_result is not None:
            return browser_context_result

        try:
            data = await self._request_sensitive_raw_json(
                "POST",
                YOUPIN_TEMPLATE_SEARCH_URL,
                payload=payload,
                headers=self._legacy_auth_headers(),
            )
        except Exception as exc:  # noqa: BLE001
            return {
                "valid": False,
                "error": self._augment_session_error_with_browser_context(str(exc)),
                "code": None,
            }

        code, msg = self._response_code_and_msg(data)
        if code == 0:
            return {"valid": True, "error": None, "code": code}
        if code == 84103:
            browser_status = self.browser_context_status() or {}
            browser_error_kind = self._browser_context_error_kind(
                str(browser_status.get("last_error") or "")
            )
            base_error = (
                "浏览器上下文请求失败"
                if bool(browser_status.get("connected"))
                and browser_error_kind
                and browser_error_kind != "login"
                else "登录态无效或缺失"
            )
            return {
                "valid": False,
                "error": self._augment_session_error_with_browser_context(base_error),
                "code": code,
            }
        if code == 84104:
            return {
                "valid": None,
                "error": self._augment_session_error_with_browser_context(msg),
                "code": code,
            }
        return {
            "valid": False,
            "error": self._augment_session_error_with_browser_context(msg or f"Code={code}"),
            "code": code,
        }

    async def _check_browser_context_session_status(self) -> dict | None:
        if not self.browser_context_enabled():
            return None

        try:
            data = await self._browser_context_requester.request_json(
                method="GET",
                url=YOUPIN_ACCOUNT_INFO_URL,
                headers=self._browser_fetch_headers(self._auth_headers()),
                timeout=20.0,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Youpin browser-context account-session request failed for %s: %s",
                YOUPIN_ACCOUNT_INFO_URL,
                exc,
            )
            return None

        code, msg = self._response_code_and_msg(data)
        data_obj = data.get("Data") if isinstance(data, dict) else None
        if data_obj is None and isinstance(data, dict):
            data_obj = data.get("data")
        if code == 0:
            return {"valid": True, "error": None, "code": code}
        if code in {84101, 84103}:
            return {
                "valid": False,
                "error": self._augment_session_error_with_browser_context(
                    "登录态无效或缺失"
                ),
                "code": code,
            }
        if code in {84104}:
            return {
                "valid": None,
                "error": self._augment_session_error_with_browser_context(msg),
                "code": code,
            }
        if code is None and data_obj:
            return {"valid": True, "error": None, "code": code}
        return None

    def _augment_session_error_with_browser_context(self, base_error: str) -> str:
        message = str(base_error or "").strip() or "unknown error"
        if not self.browser_context_enabled():
            return message

        status = self.browser_context_status() or {}
        cdp_url = str(status.get("cdp_url") or "").strip() or "http://127.0.0.1:9222"
        last_error = str(status.get("last_error") or "").strip()
        connected = bool(status.get("connected"))
        error_kind = self._browser_context_error_kind(last_error)

        if connected and error_kind == "login":
            suffix = "已连接 CDP 浏览器上下文，但当前浏览器里的 Youpin 仍未登录"
        elif connected and error_kind == "site_version":
            suffix = f"已连接 CDP 浏览器上下文，但浏览器请求缺少或使用了过期站点版本头: {last_error}"
        elif connected and last_error:
            suffix = f"已连接 CDP 浏览器上下文，但最近一次站点请求失败: {last_error}"
        elif connected:
            suffix = "已连接 CDP 浏览器上下文，但最近一次会话校验未通过"
        elif last_error:
            suffix = f"CDP 浏览器上下文不可用（{cdp_url}）: {last_error}"
        else:
            suffix = f"CDP 浏览器上下文未连接（{cdp_url}）"

        if suffix in message:
            return message
        return f"{message} | {suffix}"

    @staticmethod
    def _browser_context_error_kind(last_error: str) -> str:
        text = str(last_error or "").strip()
        if not text:
            return ""
        lowered = text.lower()
        if "85100" in lowered or "版本过低" in text or "app版本过低" in text:
            return "site_version"
        if "84103" in lowered or "need login" in lowered or "登录" in text:
            return "login"
        return "other"

    @staticmethod
    def _is_browser_context_transport_error(last_error: str) -> bool:
        text = str(last_error or "").strip()
        if not text:
            return False
        lowered = text.lower()
        transport_markers = (
            "connect_over_cdp",
            "timeout",
            "timed out",
            "econnrefused",
            "connection refused",
            "browser disconnected",
            "websocket",
            "target page, context or browser has been closed",
            "net::err_",
            "playwright not installed",
            "no module named 'playwright'",
        )
        return any(marker in lowered for marker in transport_markers)

    def _should_skip_browser_context_request(self) -> bool:
        if not self.browser_context_enabled():
            return True
        status = self.browser_context_status() or {}
        if bool(status.get("connected")):
            return False
        last_error = str(status.get("last_error") or "").strip()
        return self._is_browser_context_transport_error(last_error)

    @staticmethod
    def _variant_aliases(variant_label: str) -> list[str]:
        label = str(variant_label or "").strip()
        if not label:
            return []
        lowered = label.lower()
        aliases = [label]
        if lowered == "black pearl":
            aliases.extend(["BlackPearl", "Black Pearl", "黑珍珠"])
        elif lowered == "phase 1":
            aliases.extend(["P1", "Phase1", "阶段1", "阶段 1"])
        elif lowered == "phase 2":
            aliases.extend(["P2", "Phase2", "阶段2", "阶段 2"])
        elif lowered == "phase 3":
            aliases.extend(["P3", "Phase3", "阶段3", "阶段 3"])
        elif lowered == "phase 4":
            aliases.extend(["P4", "Phase4", "阶段4", "阶段 4"])
        elif lowered == "sapphire":
            aliases.extend(["Sapphire", "蓝宝石"])
        elif lowered == "ruby":
            aliases.extend(["Ruby", "红宝石"])
        elif lowered == "emerald":
            aliases.extend(["Emerald", "绿宝石"])
        seen: set[str] = set()
        out: list[str] = []
        for item in aliases:
            key = YoupinSource._normalize_style_key(item)
            if key in seen:
                continue
            seen.add(key)
            out.append(item)
        return out

    def _resolve_special_style(
        self, detail: dict[str, Any], variant_label: str
    ) -> dict[str, Any] | None:
        style_list = detail.get("specialStyleList") or []
        aliases = {
            self._normalize_style_key(item) for item in self._variant_aliases(variant_label)
        }
        for item in style_list:
            if not isinstance(item, dict):
                continue
            for candidate_name in (
                item.get("Name"),
                item.get("Title"),
                item.get("AbbrTitle"),
            ):
                name = str(candidate_name or "").strip()
                if not name:
                    continue
                if self._normalize_style_key(name) in aliases:
                    return {
                        "abbr_title": name,
                        "abbr_type": item.get("SpecialType")
                        or item.get("AbbrType")
                        or item.get("Type"),
                    }
        return None

    @staticmethod
    def _normalize_style_key(value: str) -> str:
        return "".join(str(value or "").strip().casefold().split())

    @staticmethod
    def _variant_aliases(variant_label: str) -> list[str]:
        label = str(variant_label or "").strip()
        if not label:
            return []
        lowered = label.lower()
        aliases = [label]
        if lowered == "black pearl":
            aliases.extend(["BlackPearl", "Black Pearl", "\u9ed1\u73cd\u73e0"])
        elif lowered == "phase 1":
            aliases.extend(["P1", "Phase1", "\u9636\u6bb51", "\u9636\u6bb5 1"])
        elif lowered == "phase 2":
            aliases.extend(["P2", "Phase2", "\u9636\u6bb52", "\u9636\u6bb5 2"])
        elif lowered == "phase 3":
            aliases.extend(["P3", "Phase3", "\u9636\u6bb53", "\u9636\u6bb5 3"])
        elif lowered == "phase 4":
            aliases.extend(["P4", "Phase4", "\u9636\u6bb54", "\u9636\u6bb5 4"])
        elif lowered == "sapphire":
            aliases.extend(["Sapphire", "\u84dd\u5b9d\u77f3"])
        elif lowered == "ruby":
            aliases.extend(["Ruby", "\u7ea2\u5b9d\u77f3"])
        elif lowered == "emerald":
            aliases.extend(["Emerald", "\u7956\u6bcd\u7eff", "\u7eff\u5b9d\u77f3"])
        seen: set[str] = set()
        out: list[str] = []
        for item in aliases:
            key = YoupinSource._normalize_style_key(item)
            if key in seen:
                continue
            seen.add(key)
            out.append(item)
        return out

    @classmethod
    def _iter_filter_items(
        cls,
        detail: dict[str, Any] | None,
    ) -> list[tuple[dict[str, Any], dict[str, Any]]]:
        if not isinstance(detail, dict):
            return []
        pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for filter_group in detail.get("filters") or []:
            if not isinstance(filter_group, dict):
                continue
            for item in filter_group.get("Items") or []:
                if not isinstance(item, dict):
                    continue
                pairs.append((filter_group, item))
                for node in item.get("NodeItems") or []:
                    if isinstance(node, dict):
                        pairs.append((filter_group, node))
        return pairs

    @classmethod
    def _extract_template_detail_price(
        cls,
        detail: dict[str, Any] | None,
        template_id: int,
    ) -> float | None:
        template_key = str(int(template_id))
        selected_price: float | None = None
        fallback_price: float | None = None
        for filter_group, item in cls._iter_filter_items(detail):
            price = safe_float(item.get("SellPrice"))
            if price is None or price <= 0:
                continue
            fixed_val = str(item.get("FixedVal") or "").strip()
            if fixed_val and fixed_val == template_key:
                return price
            if item.get("IsSelected") is True and selected_price is None:
                selected_price = price
            if (
                selected_price is None
                and str(filter_group.get("Name") or "").strip() == "外观"
                and not str(item.get("QueryString") or "").strip()
            ):
                selected_price = price
            if fallback_price is None or price < fallback_price:
                fallback_price = price
        if selected_price is not None and selected_price > 0:
            return selected_price
        return fallback_price

    @classmethod
    def _extract_commodity_list(cls, data: dict[str, Any]) -> list[dict[str, Any]]:
        items = cls._extract_search_items(data)
        return items if items else []

    def detect_semantic_failure(self, data: dict) -> tuple[bool, str] | None:
        code, msg = self._response_code_and_msg(data)
        if code == 0:
            return None
        if code == 84103:
            return (False, f"youpin code={code}: {msg}")
        # Code != 0 is considered semantic failure and should trigger backoff.
        return (True, f"youpin code={code}: {msg}")

    @staticmethod
    def _response_code_and_msg(data: dict) -> tuple[int | None, str]:
        if not isinstance(data, dict):
            return None, "unknown error"
        raw_code = data.get("Code")
        if raw_code is None:
            raw_code = data.get("code")
        try:
            code = int(raw_code) if raw_code is not None else None
        except (TypeError, ValueError):
            code = None
        msg = str(data.get("Msg") or data.get("msg") or "").strip() or "unknown error"
        return code, msg

    async def _request_sensitive_json(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
        timeout: float = 20.0,
    ) -> dict[str, Any]:
        browser_error: Exception | None = None
        if self.browser_context_enabled() and not self._should_skip_browser_context_request():
            try:
                data = await self._browser_context_requester.request_json(
                    method=method,
                    url=url,
                    params=params,
                    payload=payload,
                    headers=self._browser_fetch_headers(headers),
                    timeout=timeout,
                )
                semantic = self.detect_semantic_failure(data)
                if semantic:
                    should_slowdown, message = semantic
                    if should_slowdown:
                        self.register_rate_limit()
                        raise RateLimitedError(message)
                    raise DataSourceError(message)
                self.register_success()
                return data
            except (RateLimitedError, DataSourceError) as exc:
                browser_error = exc
                logger.warning(
                    "Youpin browser-context semantic failure for %s: %s",
                    url,
                    exc,
                )
            except Exception as exc:  # noqa: BLE001
                browser_error = exc
                logger.warning(
                    "Youpin browser-context request failed for %s: %s",
                    url,
                    exc,
                )

        try:
            return await self.request_json(
                method,
                url,
                headers=headers,
                params=params,
                payload=payload,
                timeout=timeout,
            )
        except Exception as exc:
            if browser_error is not None and not self._auth_headers():
                raise DataSourceError(
                    f"{exc} | browser_context={browser_error}"
                ) from exc
            raise

    async def _request_sensitive_raw_json(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
        timeout: float = 20.0,
    ) -> dict[str, Any]:
        browser_error: Exception | None = None
        if self.browser_context_enabled() and not self._should_skip_browser_context_request():
            try:
                data = await self._browser_context_requester.request_json(
                    method=method,
                    url=url,
                    params=params,
                    payload=payload,
                    headers=self._browser_fetch_headers(headers),
                    timeout=timeout,
                )
                self.register_success()
                return data
            except Exception as exc:  # noqa: BLE001
                browser_error = exc
                logger.warning(
                    "Youpin browser-context raw request failed for %s: %s",
                    url,
                    exc,
                )

        try:
            return await self._raw_request_json(
                method,
                url,
                headers=headers,
                params=params,
                payload=payload,
                timeout=timeout,
            )
        except Exception as exc:
            if browser_error is not None and not self._auth_headers():
                raise DataSourceError(
                    f"{exc} | browser_context={browser_error}"
                ) from exc
            raise

    async def _raw_request_json(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
        timeout: float = 20.0,
    ) -> dict:
        merged_headers = dict(self.default_headers)
        if headers:
            merged_headers.update(headers)
        response = await self._client.request(
            method=method,
            url=url,
            headers=merged_headers,
            params=params,
            json=payload,
            timeout=timeout,
        )
        if response.status_code >= 400:
            raise DataSourceError(
                f"{self.source_name} returned {response.status_code}: {response.text[:160]}"
            )
        try:
            data = response.json()
        except json.JSONDecodeError as exc:
            raise DataSourceError(
                f"{self.source_name} returned non-json response"
            ) from exc
        if not isinstance(data, dict):
            raise DataSourceError(f"{self.source_name} returned unsupported JSON payload")
        return data

    def _auth_headers(self) -> dict[str, str]:
        headers: dict[str, str] = {}
        if self._request_profile_provider:
            profile = self._request_profile_provider() or {}
            headers = self._merge_header_maps(headers, profile)
        if self._runtime_request_profile:
            headers = self._merge_header_maps(headers, self._runtime_request_profile)
        cookie = self._cookie_provider() if self._cookie_provider else None
        if cookie:
            headers = self._merge_header_maps(headers, {"Cookie": cookie})
        if self._runtime_cookie_header:
            headers = self._merge_header_maps(
                headers,
                {"Cookie": self._runtime_cookie_header},
            )
        return headers

    def _legacy_auth_headers(self) -> dict[str, str]:
        headers = {
            "accept": "application/json, text/plain, */*",
            "accept-language": "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7",
            "apptype": "1",
            "content-type": "application/json;charset=UTF-8",
            "origin": "http://www.898yp.com",
            "referer": "http://www.898yp.com/",
            "user-agent": self.settings.csgoroll_user_agent,
        }
        if self._request_profile_provider:
            profile = self._request_profile_provider() or {}
            headers = self._merge_header_maps(headers, profile)
        auth = self._auth_headers()
        if auth:
            headers = self._merge_header_maps(headers, auth)
        return headers

    @staticmethod
    def _merge_header_maps(*header_maps: dict[str, str]) -> dict[str, str]:
        out: dict[str, str] = {}
        key_lookup: dict[str, str] = {}
        for header_map in header_maps:
            for key, value in header_map.items():
                if value is None:
                    continue
                raw_key = str(key or "").strip()
                if not raw_key:
                    continue
                lowered = raw_key.lower()
                actual_key = _HEADER_CANONICAL_CASE.get(lowered, raw_key)
                previous = key_lookup.get(lowered)
                if previous and previous != actual_key:
                    out.pop(previous, None)
                key_lookup[lowered] = actual_key
                out[actual_key] = str(value)
        return out

    @staticmethod
    def _browser_fetch_headers(
        headers: dict[str, str] | None,
    ) -> dict[str, str]:
        if not headers:
            return {
                "content-type": "application/json;charset=UTF-8",
                "accept": "application/json, text/plain, */*",
            }
        blocked = {"cookie", "host", "content-length"}
        out: dict[str, str] = {}
        for key, value in headers.items():
            actual_key = str(key or "").strip()
            lowered = actual_key.lower()
            if lowered in blocked or value is None:
                continue
            out[actual_key] = str(value)
        if "content-type" not in out:
            out["content-type"] = "application/json;charset=UTF-8"
        return out

    @staticmethod
    def _extract_search_items(data: dict) -> list[dict[str, Any]]:
        data_obj = data.get("Data")
        if isinstance(data_obj, list):
            return [item for item in data_obj if isinstance(item, dict)]
        if isinstance(data_obj, dict):
            for key in ("TemplateList", "CommodityList", "Items", "List"):
                items = data_obj.get(key)
                if isinstance(items, list):
                    return [item for item in items if isinstance(item, dict)]
        return []

    @staticmethod
    def _extract_search_price(item: dict[str, Any]) -> float | None:
        for key in (
            "Price",
            "price",
            "purchasePrice",
            "PurchasePrice",
            "MinPrice",
            "minPrice",
            "SellMinPrice",
            "sellMinPrice",
            "ReferencePrice",
            "referencePrice",
        ):
            value = safe_float(item.get(key))
            if value is not None and value > 0:
                return value
        return None

    @staticmethod
    def _extract_item_name(item: dict[str, Any]) -> str | None:
        for key in (
            "CommodityHashName",
            "commodityHashName",
            "MarketHashName",
            "marketHashName",
            "CommodityName",
            "commodityName",
            "Name",
            "name",
            "MarketHashName",
            "marketHashName",
            "TemplateName",
            "templateName",
        ):
            value = str(item.get(key) or "").strip()
            if value:
                return value
        return None

    @staticmethod
    def _extract_variant_style_name(item: dict[str, Any] | None) -> str | None:
        if not isinstance(item, dict):
            return None
        for key in (
            "DopplerName",
            "dopplerName",
            "FadeName",
            "fadeName",
            "HardenedName",
            "hardenedName",
            "SpecialStyle",
            "specialStyle",
            "SpecialStyleName",
            "specialStyleName",
            "AbbrTitle",
            "abbrTitle",
        ):
            value = str(item.get(key) or "").strip()
            if value:
                return value
        return None

    @classmethod
    def _select_best_exact_match(
        cls,
        items: list[dict[str, Any]],
        keyword: str,
    ) -> dict[str, Any] | None:
        normalized_keyword = cls._normalize_exact_match_text(keyword).casefold()
        if not normalized_keyword:
            return None

        exact_matches: list[dict[str, Any]] = []
        partial_matches: list[dict[str, Any]] = []
        for item in items:
            name = cls._normalize_exact_match_text(cls._extract_item_name(item) or "")
            if not name:
                continue
            normalized_name = name.casefold()
            if normalized_name == normalized_keyword:
                exact_matches.append(item)
            elif normalized_keyword in normalized_name:
                partial_matches.append(item)

        candidates = exact_matches or partial_matches
        if not candidates:
            return None
        priced = [
            item for item in candidates if cls._extract_search_price(item) is not None
        ]
        if not priced:
            return None
        return min(priced, key=lambda item: cls._extract_search_price(item) or 10**18)

    @classmethod
    def _item_matches_variant(cls, item: dict[str, Any], variant_label: str) -> bool:
        aliases = {cls._normalize_style_key(item) for item in cls._variant_aliases(variant_label)}
        candidates = (
            item.get("dopplerName"),
            item.get("DopplerName"),
            item.get("fadeName"),
            item.get("FadeName"),
            item.get("specialStyle"),
            item.get("SpecialStyle"),
            item.get("specialStyleName"),
            item.get("SpecialStyleName"),
            item.get("abbrTitle"),
            item.get("AbbrTitle"),
        )
        for value in candidates:
            text = str(value or "").strip()
            if text and cls._normalize_style_key(text) in aliases:
                return True
        title_candidates = (
            item.get("CommodityName"),
            item.get("commodityName"),
            item.get("Name"),
            item.get("name"),
            item.get("MarketHashName"),
            item.get("marketHashName"),
            item.get("TemplateName"),
            item.get("templateName"),
        )
        for value in title_candidates:
            text = cls._normalize_style_key(value)
            if not text:
                continue
            if any(alias and alias in text for alias in aliases):
                return True
        return False

    @staticmethod
    def _variant_scan_page_limit(variant_label: str) -> int:
        label = str(variant_label or "").strip().lower()
        if label in {"ruby", "sapphire", "black pearl", "emerald"}:
            return 6
        return 3

    @staticmethod
    def _normalize_exact_match_text(value: str | None) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        for src, dst in _EXACT_NAME_REPLACEMENTS:
            text = text.replace(src, dst)
        text = _EXACT_NAME_WHITESPACE_RE.sub(" ", text).strip()
        if text.startswith("★ "):
            return text
        if text.startswith("★"):
            return f"★ {text[1:].strip()}"
        return text

    @staticmethod
    def _normalize_exact_match_text(value: str | None) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        for src, dst in _EXACT_NAME_REPLACEMENTS:
            text = text.replace(src, dst)
        extra_replacements = (
            ("\u0431\u044f", "\u2605"),
            ("\u00e2\u0084\u00a2", "\u2122"),
            ("\u00e2\u02c6\u00a2", "\u2122"),
        )
        for src, dst in extra_replacements:
            text = text.replace(src, dst)
        text = _EXACT_NAME_WHITESPACE_RE.sub(" ", text).strip()
        if text.startswith("\u2605"):
            return text
        leading_markers = (
            "\u0431\u044f",
            "\u659c\u891f",
            "\u8292\u8075\u805f",
            "\u95b3?",
        )
        for marker in leading_markers:
            if text.startswith(marker):
                remainder = text[len(marker):].strip()
                return f"\u2605 {remainder}".strip()
        return text

    async def _try_fetch_variant_trend_price(
        self,
        *,
        template_id: int,
        variant_label: str,
        detail: dict[str, Any] | None,
        style: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        trend_payload: dict[str, Any] | None = None
        filter_payload: dict[str, Any] | None = None
        trend_entries: list[dict[str, Any]] = []
        trend_style_name = str((style or {}).get("abbr_title") or "").strip() or None

        try:
            trend_payload = await self._fetch_price_trend_data(template_id)
            trend_entries = self._extract_price_trend_entries(trend_payload)
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "Youpin trend lookup failed template_id=%s variant=%s error=%s",
                template_id,
                variant_label,
                exc,
            )

        latest_trade = self._select_latest_variant_trend_trade(
            trend_entries,
            variant_label=variant_label,
            style_name=trend_style_name,
        )
        if latest_trade is None:
            try:
                filter_payload = await self._fetch_price_trend_filter_info(template_id)
                filter_names = self._extract_price_trend_filter_names(filter_payload)
                if filter_names:
                    trend_payload = await self._fetch_price_trend_data(
                        template_id,
                        filter_template_type_names=filter_names,
                    )
                    trend_entries = self._extract_price_trend_entries(trend_payload)
                    latest_trade = self._select_latest_variant_trend_trade(
                        trend_entries,
                        variant_label=variant_label,
                        style_name=trend_style_name,
                    )
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    "Youpin trend fallback retry failed template_id=%s variant=%s error=%s",
                    template_id,
                    variant_label,
                    exc,
                )

        if latest_trade is None:
            return None

        raw_payload = {
            "detail": detail or {},
            "trend_filter": filter_payload or {},
            "trend": trend_payload or {},
        }
        return {
            "template_id": template_id,
            "price": float(latest_trade["price"]),
            "commodity_name": self.extract_display_name(detail),
            "variant_label": variant_label,
            "special_style_name": latest_trade.get("style_name")
            or trend_style_name
            or variant_label,
            "special_style_type": (style or {}).get("abbr_type"),
            "item_id": None,
            "detail_price": None,
            "steam_price": None,
            "abrade": None,
            "paint_seed": None,
            "name_tags": [],
            "stickers": [],
            "raw_json": json.dumps(raw_payload, ensure_ascii=False),
        }

    async def _try_fetch_template_trend_price(
        self,
        *,
        template_id: int,
        detail: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        trend_payload: dict[str, Any] | None = None
        try:
            trend_payload = await self._fetch_price_trend_data(template_id)
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "Youpin template trend lookup failed template_id=%s error=%s",
                template_id,
                exc,
            )
            return None

        latest_trade = self._select_latest_template_trend_trade(
            self._extract_price_trend_entries(trend_payload)
        )
        if latest_trade is None:
            return None

        return {
            "template_id": template_id,
            "price": float(latest_trade["price"]),
            "commodity_name": self.extract_display_name(detail),
            "raw_json": json.dumps(
                {"detail": detail or {}, "trend": trend_payload or {}},
                ensure_ascii=False,
            ),
        }

    @staticmethod
    def _has_special_style_filters(detail: dict[str, Any] | None) -> bool:
        if not isinstance(detail, dict):
            return False
        style_list = detail.get("specialStyleList")
        if not isinstance(style_list, list):
            return False
        return any(isinstance(item, dict) for item in style_list)

    @classmethod
    def _is_default_special_style(cls, value: Any) -> bool:
        normalized = cls._normalize_style_key(str(value or ""))
        return normalized in {"", "不限", "全部", "na", "n/a", "default", "normal", "-1"}

    async def _try_fetch_template_lease_price(
        self,
        *,
        template_id: int,
        detail: dict[str, Any] | None,
        allow_special_templates: bool = False,
    ) -> dict[str, Any] | None:
        if not allow_special_templates and self._has_special_style_filters(detail):
            return None

        try:
            data = await self._request_sensitive_json(
                "POST",
                YOUPIN_LEASE_LIST_URL,
                payload={
                    "gameId": "730",
                    "listSortType": 2,
                    "listType": 10,
                    "pageIndex": 1,
                    "pageSize": 100,
                    "sortType": 0,
                    "templateId": str(template_id),
                },
                headers=self._auth_headers(),
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "Youpin template lease lookup failed template_id=%s error=%s",
                template_id,
                exc,
            )
            return None

        lease_items = self._extract_commodity_list(data)
        priced_items = [
            item
            for item in lease_items
            if (self._extract_search_price(item) or 0) > 0
        ]
        if not priced_items:
            return None

        best_item = min(
            priced_items,
            key=lambda item: self._extract_search_price(item) or 10**18,
        )
        detail_payload = await self._try_fetch_item_detail_for_search_result(best_item)
        price = self._extract_search_price(best_item)
        if detail_payload:
            detail_price = detail_payload.get("price")
            if detail_price is not None and detail_price > 0:
                price = detail_price
        if price is None or price <= 0:
            return None

        commodity_name = str(
            (detail_payload or {}).get("commodity_name")
            or self._extract_item_name(best_item)
            or self.extract_display_name(detail)
            or ""
        ).strip() or None
        return {
            "template_id": template_id,
            "price": float(price),
            "commodity_name": commodity_name,
            "item_id": (detail_payload or {}).get("item_id") or self._extract_item_id(best_item),
            "detail_price": (detail_payload or {}).get("price"),
            "steam_price": (detail_payload or {}).get("steam_price"),
            "abrade": (detail_payload or {}).get("abrade"),
            "paint_seed": (detail_payload or {}).get("paint_seed"),
            "name_tags": list((detail_payload or {}).get("name_tags") or []),
            "stickers": list((detail_payload or {}).get("stickers") or []),
            "raw_json": self._build_combined_raw_json(data, detail_payload),
        }

    async def _try_fetch_variant_lease_price(
        self,
        *,
        template_id: int,
        variant_label: str,
        detail: dict[str, Any] | None,
        style: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        raw_payload: dict[str, Any] | None = None
        best_item: dict[str, Any] | None = None
        best_price: float | None = None
        seen_keywords: set[str] = set()
        keyword_candidates = self._variant_aliases(variant_label)
        if style and style.get("abbr_title"):
            keyword_candidates.append(str(style["abbr_title"]))

        for keyword in keyword_candidates:
            normalized_keyword = self._normalize_style_key(keyword)
            if not normalized_keyword or normalized_keyword in seen_keywords:
                continue
            seen_keywords.add(normalized_keyword)
            try:
                payload = {
                    "gameId": "730",
                    "listSortType": 2,
                    "listType": 10,
                    "pageIndex": 1,
                    "pageSize": 100,
                    "sortType": 0,
                    "templateId": str(template_id),
                    "keyWords": keyword,
                }
                try:
                    data = await self._request_sensitive_json(
                        "POST",
                        YOUPIN_LEASE_LIST_URL,
                        payload=payload,
                        headers=self._auth_headers(),
                    )
                except Exception:
                    data = await self._request_via_special_item_client(
                        "POST",
                        YOUPIN_LEASE_LIST_URL,
                        payload=payload,
                    )
                    if data is None:
                        raise
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    "Youpin variant lease lookup failed template_id=%s variant=%s keyword=%s error=%s",
                    template_id,
                    variant_label,
                    keyword,
                    exc,
                )
                continue

            lease_items = [
                item
                for item in self._extract_commodity_list(data)
                if self._item_matches_variant(item, variant_label)
            ]
            for item in lease_items:
                candidate_price = self._extract_search_price(item)
                if candidate_price is None or candidate_price <= 0:
                    continue
                if best_price is None or candidate_price < best_price:
                    best_price = candidate_price
                    best_item = item
                    raw_payload = data

        if best_item is None or best_price is None:
            return None

        detail_payload = await self._try_fetch_item_detail_for_search_result(best_item)
        if detail_payload:
            detail_price = detail_payload.get("price")
            if detail_price is not None and detail_price > 0:
                best_price = float(detail_price)

        commodity_name = str(
            (detail_payload or {}).get("commodity_name")
            or self._extract_item_name(best_item)
            or self.extract_display_name(detail)
            or ""
        ).strip() or None
        return {
            "template_id": template_id,
            "price": float(best_price),
            "commodity_name": commodity_name,
            "variant_label": variant_label,
            "special_style_name": self._extract_variant_style_name(best_item)
            or str((style or {}).get("abbr_title") or "").strip()
            or variant_label,
            "special_style_type": (style or {}).get("abbr_type"),
            "item_id": (detail_payload or {}).get("item_id") or self._extract_item_id(best_item),
            "detail_price": (detail_payload or {}).get("price"),
            "steam_price": (detail_payload or {}).get("steam_price"),
            "abrade": (detail_payload or {}).get("abrade"),
            "paint_seed": (detail_payload or {}).get("paint_seed"),
            "name_tags": list((detail_payload or {}).get("name_tags") or []),
            "stickers": list((detail_payload or {}).get("stickers") or []),
            "raw_json": self._build_combined_raw_json(raw_payload or {}, detail_payload),
        }

    async def _fetch_purchase_filter_config(self, template_id: int) -> dict[str, Any]:
        cached = self._purchase_filter_cache.get(int(template_id))
        if cached:
            return cached

        payload = {"templateId": str(template_id)}
        try:
            data = await self._request_sensitive_json(
                "POST",
                YOUPIN_PURCHASE_FILTER_CONFIG_URL,
                payload=payload,
                headers=self._auth_headers(),
            )
        except Exception:
            data = await self._request_via_special_item_client(
                "POST",
                YOUPIN_PURCHASE_FILTER_CONFIG_URL,
                payload=payload,
            )
            if data is None:
                raise
        self._purchase_filter_cache[int(template_id)] = data
        return data

    @classmethod
    def _extract_purchase_filter_options(
        cls,
        payload: dict[str, Any] | None,
    ) -> list[dict[str, Any]]:
        if not isinstance(payload, dict):
            return []
        data_obj = payload.get("Data")
        if data_obj is None:
            data_obj = payload.get("data")
        if not isinstance(data_obj, dict):
            return []
        for key in ("specialInfoList", "SpecialInfoList"):
            items = data_obj.get(key)
            if isinstance(items, list):
                return [item for item in items if isinstance(item, dict)]
        return []

    @classmethod
    def _resolve_special_purchase_type_id(
        cls,
        filter_payload: dict[str, Any] | None,
        variant_label: str,
        style_name: str | None = None,
    ) -> int | None:
        aliases = {
            cls._normalize_style_key(item)
            for item in cls._variant_aliases(variant_label)
            if str(item or "").strip()
        }
        if style_name:
            aliases.add(cls._normalize_style_key(style_name))

        for item in cls._extract_purchase_filter_options(filter_payload):
            candidate_names = (
                item.get("name"),
                item.get("Name"),
                item.get("title"),
                item.get("Title"),
                item.get("specialStyle"),
                item.get("SpecialStyle"),
                item.get("abbrTitle"),
                item.get("AbbrTitle"),
                item.get("value"),
                item.get("Value"),
            )
            for candidate_name in candidate_names:
                normalized_name = cls._normalize_style_key(str(candidate_name or ""))
                if not normalized_name or normalized_name not in aliases:
                    continue
                raw_type_id = item.get("typeId")
                if raw_type_id is None:
                    raw_type_id = item.get("TypeId")
                try:
                    return int(raw_type_id)
                except (TypeError, ValueError):
                    continue
        return None

    @classmethod
    def _extract_purchase_order_items(
        cls,
        payload: dict[str, Any] | None,
    ) -> list[dict[str, Any]]:
        if not isinstance(payload, dict):
            return []
        data_obj = payload.get("Data")
        if data_obj is None:
            data_obj = payload.get("data")
        if not isinstance(data_obj, dict):
            return []
        for key in ("purchaseOrderResponseList", "PurchaseOrderResponseList", "Items", "List"):
            items = data_obj.get(key)
            if isinstance(items, list):
                return [item for item in items if isinstance(item, dict)]
        return []

    async def _try_fetch_template_purchase_price(
        self,
        *,
        template_id: int,
        detail: dict[str, Any] | None,
        allow_special_templates: bool = False,
    ) -> dict[str, Any] | None:
        if not allow_special_templates and self._has_special_style_filters(detail):
            return None

        try:
            payload = {
                "templateId": str(template_id),
                "pageIndex": 1,
                "pageSize": 100,
            }
            try:
                data = await self._request_sensitive_json(
                    "POST",
                    YOUPIN_PURCHASE_ORDER_LIST_URL,
                    payload=payload,
                    headers=self._auth_headers(),
                )
            except Exception:
                data = await self._request_via_special_item_client(
                    "POST",
                    YOUPIN_PURCHASE_ORDER_LIST_URL,
                    payload=payload,
                )
                if data is None:
                    raise
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "Youpin template purchase lookup failed template_id=%s error=%s",
                template_id,
                exc,
            )
            return None

        purchase_items = self._extract_purchase_order_items(data)
        if not purchase_items:
            return None

        normal_items = [
            item
            for item in purchase_items
            if self._is_default_special_style(item.get("specialStyle"))
        ]
        priced_items = [
            item
            for item in (normal_items or purchase_items)
            if (self._extract_search_price(item) or 0) > 0
        ]
        if not priced_items:
            return None

        best_item = max(
            priced_items,
            key=lambda item: self._extract_search_price(item) or 0.0,
        )
        best_price = self._extract_search_price(best_item)
        if best_price is None or best_price <= 0:
            return None
        return {
            "template_id": template_id,
            "price": float(best_price),
            "commodity_name": self._extract_item_name(best_item)
            or self.extract_display_name(detail),
            "item_id": None,
            "detail_price": None,
            "steam_price": None,
            "abrade": None,
            "paint_seed": None,
            "name_tags": [],
            "stickers": [],
            "raw_json": json.dumps(
                {"detail": detail or {}, "purchase": data or {}},
                ensure_ascii=False,
            ),
        }

    async def _try_fetch_variant_purchase_price(
        self,
        *,
        template_id: int,
        variant_label: str,
        detail: dict[str, Any] | None,
        style: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        filter_payload: dict[str, Any] | None = None
        type_id: int | None = None
        try:
            filter_payload = await self._fetch_purchase_filter_config(template_id)
            type_id = self._resolve_special_purchase_type_id(
                filter_payload,
                variant_label,
                str((style or {}).get("abbr_title") or "").strip() or None,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "Youpin purchase filter lookup failed template_id=%s variant=%s error=%s",
                template_id,
                variant_label,
                exc,
            )

        payload: dict[str, Any] = {
            "templateId": str(template_id),
            "pageIndex": 1,
            "pageSize": 100,
        }
        if type_id is not None:
            payload["typeId"] = type_id

        try:
            try:
                data = await self._request_sensitive_json(
                    "POST",
                    YOUPIN_PURCHASE_ORDER_LIST_URL,
                    payload=payload,
                    headers=self._auth_headers(),
                )
            except Exception:
                data = await self._request_via_special_item_client(
                    "POST",
                    YOUPIN_PURCHASE_ORDER_LIST_URL,
                    payload=payload,
                )
                if data is None:
                    raise
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "Youpin variant purchase lookup failed template_id=%s variant=%s error=%s",
                template_id,
                variant_label,
                exc,
            )
            return None

        purchase_items = [
            item
            for item in self._extract_purchase_order_items(data)
            if self._item_matches_variant(item, variant_label)
        ]
        priced_items = [
            item
            for item in purchase_items
            if (self._extract_search_price(item) or 0) > 0
        ]
        if not priced_items:
            return None

        best_item = max(
            priced_items,
            key=lambda item: self._extract_search_price(item) or 0.0,
        )
        best_price = self._extract_search_price(best_item)
        if best_price is None or best_price <= 0:
            return None
        return {
            "template_id": template_id,
            "price": float(best_price),
            "commodity_name": self._extract_item_name(best_item)
            or self.extract_display_name(detail),
            "variant_label": variant_label,
            "special_style_name": self._extract_variant_style_name(best_item)
            or str((style or {}).get("abbr_title") or "").strip()
            or variant_label,
            "special_style_type": (style or {}).get("abbr_type"),
            "item_id": None,
            "detail_price": None,
            "steam_price": None,
            "abrade": None,
            "paint_seed": None,
            "name_tags": [],
            "stickers": [],
            "raw_json": json.dumps(
                {
                    "detail": detail or {},
                    "purchase_filter": filter_payload or {},
                    "purchase": data or {},
                },
                ensure_ascii=False,
            ),
        }

    async def _fetch_price_trend_filter_info(self, template_id: int) -> dict[str, Any]:
        payload = {"templateId": str(template_id)}
        try:
            return await self._request_sensitive_json(
                "POST",
                YOUPIN_PRICE_TREND_FILTER_INFO_URL,
                payload=payload,
                headers=self._auth_headers(),
            )
        except Exception:
            data = await self._request_via_special_item_client(
                "POST",
                YOUPIN_PRICE_TREND_FILTER_INFO_URL,
                payload=payload,
            )
            if data is None:
                raise
            return data

    async def _fetch_price_trend_data(
        self,
        template_id: int,
        *,
        filter_template_type_names: list[str] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "templateId": str(template_id),
            "orderType": "1",
            "day": "7",
            "templateTypeName": "",
            "customizeDay": False,
        }
        if filter_template_type_names:
            payload["filterTemplateTypeNames"] = filter_template_type_names
        try:
            return await self._request_sensitive_json(
                "POST",
                YOUPIN_PRICE_TREND_DATA_LIST_URL,
                payload=payload,
                headers=self._auth_headers(),
            )
        except Exception:
            data = await self._request_via_special_item_client(
                "POST",
                YOUPIN_PRICE_TREND_DATA_LIST_URL,
                payload=payload,
            )
            if data is None:
                raise
            return data

    @classmethod
    def _extract_price_trend_entries(
        cls,
        payload: dict[str, Any] | None,
    ) -> list[dict[str, Any]]:
        if not isinstance(payload, dict):
            return []
        data_obj = payload.get("Data")
        if data_obj is None:
            data_obj = payload.get("data")
        if not isinstance(data_obj, list):
            return []
        return [item for item in data_obj if isinstance(item, dict)]

    @classmethod
    def _extract_price_trend_filter_names(
        cls,
        payload: dict[str, Any] | None,
    ) -> list[str]:
        if not isinstance(payload, dict):
            return []
        data_obj = payload.get("Data")
        if data_obj is None:
            data_obj = payload.get("data")
        if not isinstance(data_obj, dict):
            return []
        raw_names = data_obj.get("templateTypeNameList")
        if raw_names is None:
            raw_names = data_obj.get("TemplateTypeNameList")
        if not isinstance(raw_names, list):
            return []
        out: list[str] = []
        seen: set[str] = set()
        for item in raw_names:
            name = str(item or "").strip()
            if not name:
                continue
            key = cls._normalize_style_key(name)
            if key in seen:
                continue
            seen.add(key)
            out.append(name)
        return out

    @classmethod
    def _select_latest_variant_trend_trade(
        cls,
        trend_entries: list[dict[str, Any]],
        *,
        variant_label: str,
        style_name: str | None = None,
    ) -> dict[str, Any] | None:
        aliases = {
            cls._normalize_style_key(item)
            for item in cls._variant_aliases(variant_label)
            if str(item or "").strip()
        }
        if style_name:
            aliases.add(cls._normalize_style_key(style_name))

        matched_trade: dict[str, Any] | None = None
        fallback_trade: dict[str, Any] | None = None
        for entry in trend_entries:
            latest_trade = cls._extract_latest_trade_from_trend_entry(entry)
            if latest_trade is None:
                continue

            raw_style_name = str(
                entry.get("templateTypeName") or entry.get("TemplateTypeName") or ""
            ).strip()
            candidate = {
                "style_name": raw_style_name or None,
                "price": latest_trade["price"],
                "time": latest_trade["time"],
            }
            normalized_style_name = cls._normalize_style_key(raw_style_name)
            if raw_style_name and normalized_style_name in aliases:
                if (
                    matched_trade is None
                    or int(candidate["time"]) >= int(matched_trade["time"])
                ):
                    matched_trade = candidate
                continue

            if len(trend_entries) == 1 and (
                fallback_trade is None
                or int(candidate["time"]) >= int(fallback_trade["time"])
            ):
                fallback_trade = candidate

        return matched_trade or fallback_trade

    @classmethod
    def _extract_latest_trade_from_trend_entry(
        cls,
        entry: dict[str, Any],
    ) -> dict[str, Any] | None:
        response = entry.get("templateTradeTrendResponse")
        if response is None:
            response = entry.get("TemplateTradeTrendResponse")
        if not isinstance(response, dict):
            response = entry

        latest_time = 0
        latest_price: float | None = None
        trade_data = response.get("tradeDataList")
        if trade_data is None:
            trade_data = response.get("TradeDataList")
        if isinstance(trade_data, list):
            for item in trade_data:
                if not isinstance(item, dict):
                    continue
                price = safe_float(item.get("price"))
                if price is None or price <= 0:
                    price = safe_float(item.get("Price"))
                if price is None or price <= 0:
                    continue
                try:
                    observed_at = int(item.get("time") or item.get("Time") or 0)
                except (TypeError, ValueError):
                    observed_at = 0
                if latest_price is None or observed_at >= latest_time:
                    latest_price = price
                    latest_time = observed_at

        if latest_price is None:
            trade_price_list = response.get("tradePriceList")
            if trade_price_list is None:
                trade_price_list = response.get("TradePriceList")
            if isinstance(trade_price_list, list):
                for item in trade_price_list:
                    price = safe_float(item)
                    if price is not None and price > 0:
                        latest_price = price

        if latest_price is None or latest_price <= 0:
            return None
        return {"price": latest_price, "time": latest_time}

    @classmethod
    def _select_latest_template_trend_trade(
        cls,
        trend_entries: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        latest_trade: dict[str, Any] | None = None
        for entry in trend_entries:
            candidate = cls._extract_latest_trade_from_trend_entry(entry)
            if candidate is None:
                continue
            if latest_trade is None or int(candidate["time"]) >= int(latest_trade["time"]):
                latest_trade = candidate
        return latest_trade
