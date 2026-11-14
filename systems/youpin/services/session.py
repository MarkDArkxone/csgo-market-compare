from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

from shared.repository import Repository
from shared.utils import now_ts

if TYPE_CHECKING:
    from systems.youpin.data_sources.youpin_source import YoupinSource


class YoupinSessionService:
    COOKIE_KEY = "youpin_session_cookie"
    COOKIE_HASH_KEY = "youpin_session_cookie_hash"
    PROFILE_HEADERS_KEY = "youpin_session_profile_headers"
    UPDATED_AT_KEY = "youpin_session_updated_at"
    LAST_CHECK_AT_KEY = "youpin_session_last_check_at"
    IS_VALID_KEY = "youpin_session_is_valid"
    LAST_ERROR_KEY = "youpin_session_last_error"
    SOURCE_KEY = "youpin_session_source"
    TARGET_DOMAIN = "youpin898.com"
    _COOKIE_ATTRS = {"path", "domain", "expires", "max-age", "secure", "httponly", "samesite"}
    _PROFILE_HEADER_ALLOWLIST = {
        "user-agent",
        "accept",
        "accept-language",
        "origin",
        "referer",
        "sec-ch-ua",
        "sec-ch-ua-mobile",
        "sec-ch-ua-platform",
        "sec-fetch-dest",
        "sec-fetch-mode",
        "sec-fetch-site",
        "priority",
    }

    def __init__(self, repo: Repository):
        self.repo = repo

    def get_cookie_header(self) -> str | None:
        raw = (self.repo.get_state(self.COOKIE_KEY) or "").strip()
        return raw or None

    def get_request_profile(self) -> dict[str, str] | None:
        raw = (self.repo.get_state(self.PROFILE_HEADERS_KEY) or "").strip()
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except Exception:
            return None
        if not isinstance(data, dict):
            return None
        out: dict[str, str] = {}
        for key, value in data.items():
            if not isinstance(key, str) or value is None:
                continue
            out[key] = str(value)
        return out or None

    def import_runtime_session(
        self,
        *,
        cookie_header: str | None,
        profile_headers: dict[str, str] | None,
        source: str = "browser_context",
    ) -> dict:
        normalized_cookie = self._pairs_to_cookie_header(
            self._extract_pairs_from_cookie_header(str(cookie_header or "").strip())
        )
        normalized_profile: dict[str, str] = {}
        for key, value in (profile_headers or {}).items():
            if value is None:
                continue
            actual_key = str(key or "").strip()
            actual_value = str(value).strip()
            if not actual_key or not actual_value:
                continue
            normalized_profile[actual_key] = actual_value

        ts = now_ts()
        cookie_hash = (
            hashlib.sha256(normalized_cookie.encode("utf-8")).hexdigest()
            if normalized_cookie
            else ""
        )
        self.repo.set_state(self.COOKIE_KEY, normalized_cookie)
        self.repo.set_state(self.COOKIE_HASH_KEY, cookie_hash)
        self.repo.set_state(
            self.PROFILE_HEADERS_KEY,
            json.dumps(normalized_profile, ensure_ascii=False)
            if normalized_profile
            else "",
        )
        self.repo.set_state(self.UPDATED_AT_KEY, str(ts))
        self.repo.set_state(self.LAST_CHECK_AT_KEY, "0")
        self.repo.set_state(self.IS_VALID_KEY, "")
        self.repo.set_state(self.LAST_ERROR_KEY, "")
        self.repo.set_state(self.SOURCE_KEY, source)
        return self.status()

    def import_cookie(self, cookie_text: str, *, source: str = "manual_import") -> dict:
        normalized = self._normalize_cookie(cookie_text)
        profile = self._extract_request_profile(cookie_text)
        ts = now_ts()
        cookie_hash = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        self.repo.set_state(self.COOKIE_KEY, normalized)
        self.repo.set_state(self.COOKIE_HASH_KEY, cookie_hash)
        self.repo.set_state(
            self.PROFILE_HEADERS_KEY,
            json.dumps(profile, ensure_ascii=False) if profile else "",
        )
        self.repo.set_state(self.UPDATED_AT_KEY, str(ts))
        self.repo.set_state(self.LAST_CHECK_AT_KEY, "0")
        self.repo.set_state(self.IS_VALID_KEY, "")
        self.repo.set_state(self.LAST_ERROR_KEY, "")
        self.repo.set_state(self.SOURCE_KEY, source)
        return self.status()

    def clear(self) -> dict:
        for key in (
            self.COOKIE_KEY,
            self.COOKIE_HASH_KEY,
            self.PROFILE_HEADERS_KEY,
            self.UPDATED_AT_KEY,
            self.LAST_CHECK_AT_KEY,
            self.IS_VALID_KEY,
            self.LAST_ERROR_KEY,
            self.SOURCE_KEY,
        ):
            self.repo.set_state(key, "")
        return self.status()

    async def validate(self, source: "YoupinSource") -> dict:
        cookie = self.get_cookie_header()
        profile = self.get_request_profile()
        ts = now_ts()
        self.repo.set_state(self.LAST_CHECK_AT_KEY, str(ts))
        has_browser_context = bool(
            callable(getattr(source, "browser_context_enabled", None))
            and source.browser_context_enabled()
        )
        if not cookie and not profile and not has_browser_context:
            self.repo.set_state(self.IS_VALID_KEY, "0")
            self.repo.set_state(self.LAST_ERROR_KEY, "No imported Youpin session")
            return self.status()

        result = await source.check_session_status()
        valid = result.get("valid")
        if valid is True:
            self.repo.set_state(self.IS_VALID_KEY, "1")
        elif valid is False:
            self.repo.set_state(self.IS_VALID_KEY, "0")
        else:
            self.repo.set_state(self.IS_VALID_KEY, "")
        self.repo.set_state(self.LAST_ERROR_KEY, str(result.get("error") or ""))
        return self.status()

    def status(self) -> dict:
        cookie = self.get_cookie_header()
        updated_at = self._int_state(self.UPDATED_AT_KEY)
        last_check_at = self._int_state(self.LAST_CHECK_AT_KEY)
        is_valid = (self.repo.get_state(self.IS_VALID_KEY) or "").strip()
        return {
            "configured": bool(cookie or self.get_request_profile()),
            "valid": True if is_valid == "1" else False if is_valid == "0" else None,
            "updated_at": updated_at,
            "last_check_at": last_check_at,
            "source": (self.repo.get_state(self.SOURCE_KEY) or "").strip() or None,
            "cookie_preview": self._mask_cookie(cookie),
            "cookie_names": self._cookie_names(cookie),
            "profile_header_names": sorted((self.get_request_profile() or {}).keys()),
            "last_error": (self.repo.get_state(self.LAST_ERROR_KEY) or "").strip() or None,
        }

    @classmethod
    def _normalize_cookie(cls, cookie_text: str) -> str:
        text = str(cookie_text or "").replace("\ufeff", "").strip()
        if not text:
            raise ValueError("Cookie 为空或格式不正确")
        pairs = cls._extract_pairs_from_request_headers(text)
        if not pairs:
            pairs = cls._extract_pairs_from_browser_table(text)
        if not pairs:
            pairs = cls._extract_pairs_from_json(text)
        if not pairs:
            pairs = cls._extract_pairs_from_netscape(text)
        if not pairs:
            pairs = cls._extract_pairs_from_cookie_header(text)
        normalized = cls._pairs_to_cookie_header(pairs)
        if not normalized:
            raise ValueError("Cookie 为空或格式不正确")
        return normalized

    @classmethod
    def _extract_pairs_from_request_headers(cls, text: str) -> list[tuple[str, str]]:
        lines = [line.rstrip() for line in text.splitlines() if line.strip()]
        for idx, raw_line in enumerate(lines):
            line = raw_line.strip()
            lower = line.lower()
            if lower.startswith("cookie:"):
                return cls._extract_pairs_from_cookie_header(line.split(":", 1)[1].strip())
            if lower == "cookie" and idx + 1 < len(lines):
                return cls._extract_pairs_from_cookie_header(lines[idx + 1].strip())
            if lower.startswith("cookie\t"):
                return cls._extract_pairs_from_cookie_header(line.split("\t", 1)[1].strip())
        return []

    @classmethod
    def _extract_request_profile(cls, text: str) -> dict[str, str]:
        profile: dict[str, str] = {}
        lines = [line.rstrip() for line in text.splitlines() if line.strip()]
        idx = 0
        while idx < len(lines):
            raw_line = lines[idx].strip()
            if not raw_line:
                idx += 1
                continue
            key: str | None = None
            value: str | None = None
            if raw_line.startswith(":"):
                idx += 1
                continue
            if ":" in raw_line and not raw_line.lower().startswith("cookie\t"):
                possible_key, possible_value = raw_line.split(":", 1)
                if possible_key.strip():
                    key = possible_key.strip().lower()
                    value = possible_value.strip()
            elif idx + 1 < len(lines):
                key = raw_line.strip().lower()
                value = lines[idx + 1].strip()
                idx += 1
            idx += 1
            if not key or value is None:
                continue
            if key == "cookie":
                continue
            if key in cls._PROFILE_HEADER_ALLOWLIST:
                profile[key] = value
        return profile

    @classmethod
    def _extract_pairs_from_cookie_header(cls, text: str) -> list[tuple[str, str]]:
        if not text:
            return []
        pairs: list[tuple[str, str]] = []
        for chunk in text.replace("\n", ";").split(";"):
            item = chunk.strip()
            if not item or "=" not in item:
                continue
            key, value = item.split("=", 1)
            key = key.strip()
            value = value.strip()
            if not key or key.lower() in cls._COOKIE_ATTRS:
                continue
            pairs.append((key, value))
        return pairs

    @classmethod
    def _extract_pairs_from_browser_table(cls, text: str) -> list[tuple[str, str]]:
        if "\t" not in text:
            return []
        pairs: list[tuple[str, str]] = []
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            parts = [part.strip() for part in raw_line.split("\t")]
            if len(parts) < 3:
                continue
            name, value, domain = parts[0], parts[1], parts[2]
            if not name or not value or cls.TARGET_DOMAIN not in domain:
                continue
            pairs.append((name, value))
        return pairs

    @classmethod
    def _extract_pairs_from_netscape(cls, text: str) -> list[tuple[str, str]]:
        if "\t" not in text:
            return []
        pairs: list[tuple[str, str]] = []
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            parts = raw_line.split("\t")
            if len(parts) < 7:
                continue
            domain = parts[0].strip()
            if cls.TARGET_DOMAIN not in domain:
                continue
            name = parts[-2].strip()
            value = parts[-1].strip()
            if not name:
                continue
            pairs.append((name, value))
        return pairs

    @classmethod
    def _extract_pairs_from_json(cls, text: str) -> list[tuple[str, str]]:
        stripped = text.lstrip()
        if not stripped.startswith("{") and not stripped.startswith("["):
            return []
        try:
            payload = json.loads(text)
        except Exception:
            return []
        pairs: list[tuple[str, str]] = []

        def visit(node):
            if isinstance(node, dict):
                name = node.get("name")
                value = node.get("value")
                domain = str(node.get("domain") or node.get("host") or "")
                if isinstance(name, str) and value is not None:
                    if not domain or cls.TARGET_DOMAIN in domain:
                        pairs.append((name.strip(), str(value).strip()))
                for child in node.values():
                    visit(child)
            elif isinstance(node, list):
                for child in node:
                    visit(child)

        visit(payload)
        return [(k, v) for k, v in pairs if k]

    @staticmethod
    def _pairs_to_cookie_header(pairs: list[tuple[str, str]]) -> str:
        ordered: dict[str, str] = {}
        for key, value in pairs:
            if not key:
                continue
            ordered[key] = value
        return "; ".join(f"{key}={value}" for key, value in ordered.items())

    @staticmethod
    def _mask_cookie(cookie: str | None) -> str | None:
        if not cookie:
            return None
        parts = []
        for chunk in cookie.split(";"):
            item = chunk.strip()
            if "=" not in item:
                continue
            key, value = item.split("=", 1)
            value = value.strip()
            if len(value) > 6:
                masked = f"{value[:3]}...{value[-3:]}"
            else:
                masked = "***"
            parts.append(f"{key.strip()}={masked}")
        return "; ".join(parts[:5])

    @staticmethod
    def _cookie_names(cookie: str | None) -> list[str]:
        if not cookie:
            return []
        names: list[str] = []
        seen: set[str] = set()
        for chunk in cookie.split(";"):
            item = chunk.strip()
            if "=" not in item:
                continue
            key = item.split("=", 1)[0].strip()
            if not key or key in seen:
                continue
            seen.add(key)
            names.append(key)
        return names

    def _int_state(self, key: str) -> int | None:
        raw = (self.repo.get_state(key) or "").strip()
        if not raw:
            return None
        try:
            value = int(raw)
        except ValueError:
            return None
        return value if value > 0 else None
