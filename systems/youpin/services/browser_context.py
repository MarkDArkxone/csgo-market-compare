from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import Any

from config.settings import Settings
from shared.utils import now_ts

logger = logging.getLogger(__name__)


class YoupinBrowserContextError(RuntimeError):
    pass


class YoupinBrowserContextClient:
    _RUNTIME_REQUIRED_KEYS = ("authorization", "deviceUk", "deviceId")
    _RUNTIME_CONTROLLED_HEADER_KEYS = frozenset(
        {
            "accept-language",
            "app-version",
            "appversion",
            "apptype",
            "authorization",
            "deviceid",
            "deviceuk",
            "origin",
            "platform",
            "referer",
            "secret-v",
            "uk",
            "user-agent",
        }
    )
    _RUNTIME_CACHE_TTL_SECONDS = 15

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._lock = asyncio.Lock()
        self._playwright: Any | None = None
        self._browser: Any | None = None
        self._context: Any | None = None
        self._page: Any | None = None
        self._last_error: str | None = None
        self._last_connected_at: int | None = None
        self._last_ok_at: int | None = None
        self._site_runtime_headers: dict[str, str] | None = None
        self._site_runtime_headers_at: int | None = None

    def is_enabled(self) -> bool:
        return bool(
            self.settings.youpin_browser_context_enabled
            and self.settings.youpin_browser_context_cdp_url
        )

    def status(self) -> dict[str, Any]:
        connected = bool(self._browser is not None and self._context is not None)
        page_url = None
        page = self._pick_context_page()
        try:
            if page is not None and not page.is_closed():
                page_url = str(page.url or "")
        except Exception:
            page_url = None
        return {
            "enabled": bool(self.settings.youpin_browser_context_enabled),
            "configured": bool(self.settings.youpin_browser_context_cdp_url),
            "connected": connected,
            "cdp_url": self.settings.youpin_browser_context_cdp_url,
            "target_page_url": self.settings.youpin_browser_context_page_url,
            "current_page_url": page_url or None,
            "last_connected_at": self._last_connected_at,
            "last_ok_at": self._last_ok_at,
            "last_error": self._last_error,
        }

    async def close(self) -> None:
        async with self._lock:
            await self._disconnect_locked()

    async def request_json(
        self,
        *,
        method: str = "POST",
        url: str,
        params: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        if not self.is_enabled():
            raise YoupinBrowserContextError(
                "Youpin browser-context mode is disabled or CDP URL is missing"
            )

        timeout_ms = int(
            max(
                1000.0,
                float(timeout or self.settings.youpin_browser_context_timeout_seconds)
                * 1000.0,
            )
        )

        async with self._lock:
            attempts = 2
            last_error: Exception | None = None
            for _ in range(attempts):
                response = None
                try:
                    context = await self._ensure_context_locked()
                    requester = getattr(context, "request", None)
                    if requester is None:
                        raise YoupinBrowserContextError(
                            "connected browser context does not expose request API"
                        )
                    request_method = str(method or "POST").strip().upper() or "POST"
                    request_headers = await self._build_request_headers_locked(
                        headers=headers,
                        timeout_ms=timeout_ms,
                    )
                    request_kwargs = {
                        "headers": request_headers,
                        "timeout": timeout_ms,
                        "fail_on_status_code": False,
                    }
                    if params:
                        request_kwargs["params"] = dict(params)

                    if request_method == "GET":
                        response = await requester.get(url, **request_kwargs)
                    elif request_method == "POST":
                        response = await requester.post(
                            url,
                            data=payload or {},
                            **request_kwargs,
                        )
                    else:
                        fetcher = getattr(requester, "fetch", None)
                        if not callable(fetcher):
                            raise YoupinBrowserContextError(
                                f"browser-context request API does not support {request_method}"
                            )
                        if payload is not None:
                            request_kwargs["data"] = payload
                        response = await fetcher(
                            url,
                            method=request_method,
                            **request_kwargs,
                        )
                    data = await self._normalize_response(url, response)
                    self._last_error = None
                    self._last_ok_at = now_ts()
                    self._page = self._pick_context_page(context)
                    return data
                except Exception as exc:  # noqa: BLE001
                    last_error = exc
                    self._last_error = str(exc)
                    if self._looks_like_site_version_error(exc):
                        self._site_runtime_headers = None
                        self._site_runtime_headers_at = None
                        continue
                    if self._should_reconnect(exc):
                        await self._disconnect_locked()
                        continue
                    break
                finally:
                    if response is not None:
                        with contextlib.suppress(Exception):
                            await response.dispose()

            if last_error:
                raise YoupinBrowserContextError(str(last_error)) from last_error
            raise YoupinBrowserContextError("unknown browser-context failure")

    async def collect_runtime_context(
        self,
        *,
        timeout: float | None = None,
    ) -> dict[str, str]:
        if not self.is_enabled():
            raise YoupinBrowserContextError(
                "Youpin browser-context mode is disabled or CDP URL is missing"
            )

        timeout_ms = int(
            max(
                1000.0,
                float(timeout or self.settings.youpin_browser_context_timeout_seconds)
                * 1000.0,
            )
        )

        async with self._lock:
            attempts = 2
            last_error: Exception | None = None
            for _ in range(attempts):
                try:
                    headers = await self._build_request_headers_locked(
                        headers=None,
                        timeout_ms=timeout_ms,
                    )
                    cookie_map = await self._read_context_cookie_map_locked()
                    cookie_header = "; ".join(
                        f"{key}={value}"
                        for key, value in cookie_map.items()
                        if str(key).strip() and str(value).strip()
                    )
                    self._last_error = None
                    self._last_ok_at = now_ts()
                    return {
                        "authorization": str(headers.get("authorization") or "").strip(),
                        "token": str(headers.get("authorization") or "").strip(),
                        "uk": str(headers.get("uk") or "").strip(),
                        "device_token": str(headers.get("deviceUk") or "").strip(),
                        "device_id": str(headers.get("deviceId") or "").strip(),
                        "app_version": str(
                            headers.get("AppVersion") or headers.get("App-Version") or ""
                        ).strip(),
                        "user_agent": str(headers.get("user-agent") or "").strip(),
                        "origin": str(headers.get("origin") or "").strip(),
                        "referer": str(headers.get("referer") or "").strip(),
                        "cookie_header": cookie_header,
                        "cookie_names": sorted(cookie_map.keys()),
                        "profile_headers": dict(headers),
                    }
                except Exception as exc:  # noqa: BLE001
                    last_error = exc
                    self._last_error = str(exc)
                    if self._should_reconnect(exc):
                        await self._disconnect_locked()
                        continue
                    break

            if last_error:
                raise YoupinBrowserContextError(str(last_error)) from last_error
            raise YoupinBrowserContextError("unknown browser-context failure")

    async def _ensure_context_locked(self):
        context = self._context
        browser = self._browser
        if context is not None and browser is not None:
            self._page = self._pick_context_page(context)
            return context
        return await self._connect_context_locked()

    async def _connect_context_locked(self):
        try:
            from playwright.async_api import async_playwright
        except Exception as exc:
            raise YoupinBrowserContextError(
                "playwright not installed; run `pip install playwright` first"
            ) from exc

        self._playwright = await async_playwright().start()
        try:
            self._browser = await self._playwright.chromium.connect_over_cdp(
                self.settings.youpin_browser_context_cdp_url
            )
            contexts = list(getattr(self._browser, "contexts", []) or [])
            if not contexts:
                raise YoupinBrowserContextError(
                    "connected to browser, but no reusable Chromium context was found"
                )
            selected_context = self._select_context(contexts)
            self._context = selected_context
            self._page = self._pick_context_page(selected_context)
            self._site_runtime_headers = None
            self._last_connected_at = now_ts()
            return selected_context
        except Exception:
            await self._disconnect_locked()
            raise

    def _select_context(self, contexts: list[Any]) -> Any:
        for context in contexts:
            page = self._pick_context_page(context)
            if page is None:
                continue
            with contextlib.suppress(Exception):
                if "youpin898.com" in str(page.url or ""):
                    return context
        return contexts[0]

    def _pick_context_page(self, context: Any | None = None):
        selected_context = context or self._context
        if selected_context is None:
            return None
        pages = list(getattr(selected_context, "pages", []) or [])
        current = self._page
        if current is not None:
            with contextlib.suppress(Exception):
                if current in pages and not current.is_closed():
                    return current
        fallback = None
        for page in pages:
            try:
                if page.is_closed():
                    continue
                if fallback is None:
                    fallback = page
                if "youpin898.com" in str(page.url or ""):
                    return page
            except Exception:
                continue
        return fallback

    async def _build_request_headers_locked(
        self,
        *,
        headers: dict[str, str] | None,
        timeout_ms: int,
    ) -> dict[str, str]:
        merged = dict(self._site_runtime_headers or {})
        cache_age = None
        if self._site_runtime_headers_at:
            cache_age = max(0, now_ts() - self._site_runtime_headers_at)
        if (
            not merged
            or not self._has_complete_runtime_headers(merged)
            or cache_age is None
            or cache_age >= self._RUNTIME_CACHE_TTL_SECONDS
        ):
            refreshed = await self._load_site_runtime_headers_locked(timeout_ms)
            if refreshed:
                merged = dict(refreshed)
                self._site_runtime_headers = dict(refreshed)
                self._site_runtime_headers_at = now_ts()
        merged = self._merge_headers(
            merged,
            self._filter_non_runtime_headers(headers or {}),
        )
        if not any(str(key).strip().lower() == "content-type" for key in merged):
            merged["content-type"] = "application/json;charset=UTF-8"
        if not any(str(key).strip().lower() == "accept" for key in merged):
            merged["accept"] = "application/json, text/plain, */*"
        return merged

    async def _load_site_runtime_headers_locked(self, timeout_ms: int) -> dict[str, str]:
        await self._ensure_context_locked()
        page = await self._ensure_runtime_page_locked(timeout_ms)
        if page is None:
            return {}
        with contextlib.suppress(Exception):
            await page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
        with contextlib.suppress(Exception):
            await page.wait_for_timeout(min(timeout_ms, 2000))
        try:
            runtime = await page.evaluate(
                """
                async () => {
                  const trimText = (value) => String(value || '').trim();
                  return {
                    authorization: trimText(globalThis.uu_token || globalThis.token || ''),
                    uk: trimText(globalThis.WEB_UK || ''),
                    device_uk: trimText(globalThis.WEB_DEVICE_UK || ''),
                    device_id: trimText(globalThis.WEB_PC_UUID || ''),
                    app_version: '',
                    secret_v: '',
                    origin: String(location.origin || '').trim(),
                    referer: String(location.href || '').trim(),
                    user_agent: String(navigator.userAgent || '').trim(),
                    accept_language: Array.isArray(navigator.languages) && navigator.languages.length
                      ? navigator.languages.join(',')
                      : String(navigator.language || '').trim(),
                  };
                }
                """
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("Failed to collect Youpin browser runtime headers from page: %s", exc)
            return {}
        if not isinstance(runtime, dict):
            return {}
        context_cookie_map = await self._read_context_cookie_map_locked()
        authorization = self._first_non_empty_runtime_value(
            runtime.get("authorization"),
            context_cookie_map.get("uu_token"),
            context_cookie_map.get("authorization"),
            context_cookie_map.get("Authorization"),
            context_cookie_map.get("token"),
        )
        device_uk = self._first_non_empty_runtime_value(
            runtime.get("device_uk"),
            context_cookie_map.get("WEB_DEVICE_UK"),
            context_cookie_map.get("deviceUk"),
            context_cookie_map.get("deviceToken"),
            context_cookie_map.get("DeviceToken"),
        )
        device_id = self._first_non_empty_runtime_value(
            runtime.get("device_id"),
            context_cookie_map.get("WEB_PC_UUID"),
            context_cookie_map.get("deviceId"),
            context_cookie_map.get("DeviceId"),
        )
        app_version = (
            str(runtime.get("app_version") or "").strip()
            or str(self.settings.youpin898_app_version or "").strip()
            or "5.28.3"
        )
        secret_v = str(runtime.get("secret_v") or "").strip() or "h5_v1"
        origin = str(runtime.get("origin") or "").strip() or "https://www.youpin898.com"
        referer = (
            str(runtime.get("referer") or "").strip()
            or self.settings.youpin_browser_context_page_url
            or "https://www.youpin898.com/"
        )
        out = {
            "accept": "application/json, text/plain, */*",
            "content-type": "application/json;charset=UTF-8",
            "origin": origin,
            "referer": referer,
            "user-agent": str(runtime.get("user_agent") or "").strip(),
            "accept-language": str(runtime.get("accept_language") or "").strip(),
            "authorization": authorization,
            "uk": str(runtime.get("uk") or "").strip(),
            "deviceUk": device_uk,
            "appType": "1",
            "platform": "pc",
            "AppVersion": app_version,
            "App-Version": app_version,
            "secret-v": secret_v,
            "deviceId": device_id,
        }
        return {
            key: value
            for key, value in out.items()
            if isinstance(key, str) and isinstance(value, str) and value.strip()
        }

    @classmethod
    def _filter_non_runtime_headers(cls, headers: dict[str, str]) -> dict[str, str]:
        filtered: dict[str, str] = {}
        for key, value in headers.items():
            actual_key = str(key or "").strip()
            if not actual_key or value is None:
                continue
            if actual_key.lower() in cls._RUNTIME_CONTROLLED_HEADER_KEYS:
                continue
            filtered[actual_key] = str(value)
        return filtered

    async def _read_context_cookie_map_locked(self) -> dict[str, str]:
        context = self._context
        if context is None:
            return {}
        cookies_fn = getattr(context, "cookies", None)
        if not callable(cookies_fn):
            return {}
        try:
            cookie_entries = await cookies_fn()
        except Exception as exc:  # noqa: BLE001
            logger.debug("Failed to read Youpin browser context cookies: %s", exc)
            return {}
        out: dict[str, str] = {}
        if not isinstance(cookie_entries, list):
            return out
        for entry in cookie_entries:
            if not isinstance(entry, dict):
                continue
            key = str(entry.get("name") or "").strip()
            value = str(entry.get("value") or "").strip()
            if key and value and key not in out:
                out[key] = value
        return out

    async def _ensure_runtime_page_locked(self, timeout_ms: int):
        context = self._context
        if context is None:
            context = await self._ensure_context_locked()
        if context is None:
            return None
        page = self._pick_context_page(context)
        if page is not None:
            with contextlib.suppress(Exception):
                if "youpin898.com" in str(page.url or ""):
                    self._page = page
                    return page
        new_page = getattr(context, "new_page", None)
        if not callable(new_page):
            return page
        created_page = await new_page()
        target_url = self.settings.youpin_browser_context_page_url or "https://www.youpin898.com/"
        with contextlib.suppress(Exception):
            await created_page.goto(target_url, wait_until="domcontentloaded", timeout=timeout_ms)
        with contextlib.suppress(Exception):
            await created_page.wait_for_timeout(min(timeout_ms, 2000))
        self._page = created_page
        return created_page

    @staticmethod
    def _merge_headers(*header_maps: dict[str, str]) -> dict[str, str]:
        out: dict[str, str] = {}
        key_lookup: dict[str, str] = {}
        for header_map in header_maps:
            for key, value in header_map.items():
                if value is None:
                    continue
                actual_key = str(key or "").strip()
                if not actual_key:
                    continue
                lowered = actual_key.lower()
                previous_key = key_lookup.get(lowered)
                if previous_key and previous_key != actual_key:
                    out.pop(previous_key, None)
                key_lookup[lowered] = actual_key
                out[actual_key] = str(value)
        return out

    @classmethod
    def _has_complete_runtime_headers(cls, headers: dict[str, str]) -> bool:
        normalized = {
            str(key or "").strip().lower(): str(value or "").strip()
            for key, value in headers.items()
        }
        return all(normalized.get(key.lower()) for key in cls._RUNTIME_REQUIRED_KEYS)

    @staticmethod
    def _first_non_empty_runtime_value(*values: Any) -> str:
        for value in values:
            text = str(value or "").strip()
            if text:
                return text
        return ""

    async def _disconnect_locked(self) -> None:
        self._page = None
        self._context = None
        self._browser = None
        self._site_runtime_headers = None
        self._site_runtime_headers_at = None
        playwright = self._playwright
        self._playwright = None
        if playwright is not None:
            with contextlib.suppress(Exception):
                await playwright.stop()

    @staticmethod
    async def _normalize_response(url: str, response: Any) -> dict[str, Any]:
        status = int(getattr(response, "status", 0) or 0)
        try:
            text = str(await response.text())
        except Exception as exc:  # noqa: BLE001
            raise YoupinBrowserContextError(
                f"browser-context could not read response body for {url}"
            ) from exc
        if status >= 400:
            raise YoupinBrowserContextError(
                f"browser-context returned {status} for {url}: {text[:160]}"
            )
        try:
            data = json.loads(text)
        except Exception as exc:  # noqa: BLE001
            raise YoupinBrowserContextError(
                f"browser-context returned non-json response for {url}"
            ) from exc
        if not isinstance(data, dict):
            raise YoupinBrowserContextError(
                f"browser-context returned unsupported JSON payload for {url}"
            )
        return data

    @staticmethod
    def _should_reconnect(exc: Exception) -> bool:
        text = str(exc or "").lower()
        reconnect_markers = (
            "target page, context or browser has been closed",
            "browser has been closed",
            "browser closed",
            "connection closed",
            "browser disconnected",
            "target closed",
            "session closed",
            "ecconnrefused",
            "connection refused",
            "cannot connect to browser",
            "no reusable chromium context",
        )
        return any(marker in text for marker in reconnect_markers)

    @staticmethod
    def _looks_like_site_version_error(exc: Exception) -> bool:
        text = str(exc or "").strip()
        if not text:
            return False
        lowered = text.lower()
        return "85100" in lowered or "鐗堟湰杩囦綆" in text or "app鐗堟湰杩囦綆" in text
