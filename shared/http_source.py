from __future__ import annotations

import asyncio
import json
import logging
import random
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from config.settings import Settings

logger = logging.getLogger(__name__)


class DataSourceError(Exception):
    pass


class RateLimitedError(DataSourceError):
    pass


class BaseHTTPSource:
    def __init__(
        self,
        settings: Settings,
        source_name: str,
        default_headers: dict[str, str] | None = None,
        max_retries: int = 3,
    ):
        self.settings = settings
        self.source_name = source_name
        self.max_retries = max_retries
        self.default_headers = default_headers or {}
        self._slowdown = settings.source_min_slowdown
        self._client = httpx.AsyncClient(
            timeout=20.0,
            follow_redirects=True,
            proxy=settings.proxy_url,
            limits=httpx.Limits(max_connections=200, max_keepalive_connections=100),
        )

    async def close(self) -> None:
        await self._client.aclose()

    @property
    def slowdown(self) -> float:
        return self._slowdown

    def register_success(self) -> None:
        decay = 0.75 if self._slowdown > 2.0 else 0.88
        self._slowdown = max(
            self.settings.source_min_slowdown,
            round(self._slowdown * decay, 4),
        )

    def register_rate_limit(self) -> None:
        self._slowdown = min(
            self.settings.source_max_slowdown,
            round(self._slowdown * 1.2 + 0.1, 4),
        )

    async def request_json(
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

        last_error: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                response = await self._client.request(
                    method=method,
                    url=url,
                    headers=merged_headers,
                    params=params,
                    json=payload,
                    timeout=timeout,
                )

                text = response.text

                if response.status_code == 429:
                    self.register_rate_limit()
                    retry_after = self._parse_retry_after_seconds(
                        response.headers.get("Retry-After")
                    )
                    if retry_after is not None:
                        await asyncio.sleep(retry_after)
                    raise RateLimitedError(f"{self.source_name} returned 429")

                if response.status_code in {403, 503} and "challenge" in text.lower():
                    self.register_rate_limit()
                    raise RateLimitedError(
                        f"{self.source_name} returned anti-bot challenge ({response.status_code})"
                    )

                if response.status_code in {401, 403}:
                    self.register_rate_limit()
                    raise RateLimitedError(
                        f"{self.source_name} returned {response.status_code}"
                    )

                if response.status_code >= 500:
                    raise DataSourceError(
                        f"{self.source_name} returned {response.status_code}"
                    )

                if response.status_code >= 400:
                    raise DataSourceError(
                        f"{self.source_name} returned {response.status_code}: {text[:160]}"
                    )

                try:
                    data = response.json()
                except json.JSONDecodeError as exc:
                    raise DataSourceError(
                        f"{self.source_name} returned non-json response"
                    ) from exc

                if not isinstance(data, dict):
                    raise DataSourceError(
                        f"{self.source_name} returned unsupported JSON payload"
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
            except RateLimitedError as exc:
                last_error = exc
                if attempt < self.max_retries:
                    await asyncio.sleep(self._backoff_with_jitter(attempt))
                continue
            except (httpx.TimeoutException, httpx.NetworkError, DataSourceError) as exc:
                last_error = exc
                if attempt < self.max_retries:
                    await asyncio.sleep(self._backoff_with_jitter(attempt))
                continue

        if last_error:
            raise last_error
        raise DataSourceError(f"{self.source_name} request failed")

    def detect_semantic_failure(self, data: dict) -> tuple[bool, str] | None:
        return None

    def _backoff_with_jitter(self, attempt: int) -> float:
        base = float(getattr(self.settings, "backoff_base_seconds", 0.4))
        cap = float(getattr(self.settings, "backoff_max_seconds", 8.0))
        jitter_ratio = float(getattr(self.settings, "backoff_jitter_ratio", 0.2))
        raw = min(cap, base * (2 ** max(0, attempt - 1))) * self.slowdown
        jitter = raw * random.uniform(-jitter_ratio, jitter_ratio)
        return max(0.05, raw + jitter)

    @staticmethod
    def _parse_retry_after_seconds(value: str | None) -> float | None:
        if not value:
            return None
        raw = str(value).strip()
        if not raw:
            return None
        if raw.isdigit():
            return max(0.0, float(raw))
        try:
            dt = parsedate_to_datetime(raw)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            now = datetime.now(timezone.utc)
            delta = (dt - now).total_seconds()
            return max(0.0, delta)
        except Exception:
            return None

