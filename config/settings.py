from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    return int(raw)


def _float_env(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    return float(raw)


def _candidate_project_roots() -> list[Path]:
    candidates: list[Path] = []

    project_root_raw = os.getenv("PROJECT_ROOT", "").strip()
    if project_root_raw:
        candidates.append(Path(project_root_raw))

    candidates.append(Path.cwd())

    executable_raw = str(getattr(sys, "executable", "") or "").strip()
    if executable_raw:
        executable_path = Path(executable_raw)
        candidates.append(executable_path.parent)
        candidates.append(executable_path.parent.parent)

    candidates.append(Path(__file__).resolve().parent.parent)

    out: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except Exception:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        out.append(resolved)
    return out


def _load_runtime_dotenv() -> Path | None:
    for root in _candidate_project_roots():
        env_path = root / ".env"
        if env_path.exists():
            load_dotenv(env_path, override=False)
            return env_path
    load_dotenv(override=False)
    return None


def _first_existing_path(candidates: list[Path]) -> Path | None:
    for candidate in candidates:
        try:
            if candidate.exists():
                return candidate
        except Exception:
            continue
    return None


@dataclass(frozen=True)
class Settings:
    app_host: str
    app_port: int

    db_path: Path
    log_path: Path

    proxy_url: str | None

    roll_page_size: int
    roll_pages_per_loop: int
    roll_intra_page_delay_seconds: float
    roll_multi_item_mode: str
    roll_refresh_mode: str
    roll_daily_refresh_hour: int
    roll_daily_refresh_minute: int
    roll_daily_fresh_seconds: int
    roll_status: str
    roll_order_by: tuple[str, ...]
    roll_base_interval_seconds: float
    roll_jitter_seconds: float
    roll_trade_ttl_seconds: int
    roll_max_retries: int
    roll_excluded_categories: set[str]
    special_detail_enabled: bool
    special_detail_batch_size: int
    special_detail_pause_normal_queue: bool
    special_detail_candidate_ttl_seconds: int
    special_detail_confirmed_ttl_seconds: int
    special_detail_timeout_seconds: float
    special_detail_headless: bool
    special_detail_browser_path: str | None
    special_detail_url_template: str

    youpin_ttl_seconds: int
    youpin_priority_ttl_seconds: int
    youpin_high_priority_ttl_seconds: int
    youpin_batch_size: int
    youpin_refresh_workers: int
    youpin_cycle_time_budget_seconds: float
    youpin_per_request_delay_seconds: float
    youpin_max_retries: int
    youpin_failure_retry_base_seconds: int
    youpin_failure_retry_max_seconds: int
    youpin_rate_limit_retry_base_seconds: int
    youpin_rate_limit_retry_max_seconds: int
    youpin_empty_price_retry_base_seconds: int
    youpin_empty_price_retry_max_seconds: int
    youpin_empty_warn_min_attempts: int
    youpin_empty_warn_rate: float
    youpin_browser_context_enabled: bool
    youpin_browser_context_cdp_url: str | None
    youpin_browser_context_page_url: str
    youpin_browser_context_timeout_seconds: float
    youpin898_detail_enabled: bool
    youpin898_base_url: str
    youpin898_timeout_seconds: float
    youpin898_token: str | None
    youpin898_device_token: str | None
    youpin898_device_id: str | None
    youpin898_app_version: str
    youpin898_user_agent: str

    csgoroll_user_agent: str

    strategy_top_markup_bucket: str
    roll_to_youpin_rate: float
    roll_target_markup_percent: float
    roll_target_markup_eps: float
    platform_fee_rate: float
    withdraw_loss_rate: float

    exchange_rate_enabled: bool
    exchange_rate_primary_source: str
    exchange_rate_backup_source: str
    exchange_rate_base: str
    exchange_rate_target: str
    exchange_rate_refresh_seconds: int
    exchange_rate_timeout_seconds: float
    roll_coin_to_usd_rate: float
    exchange_rate_max_backoff_seconds: int
    exchange_rate_manual_override_seconds: int

    mapper_auto_download: bool
    mapper_refresh_seconds: int
    mapper_uuyp_url: str
    mapper_steam_url: str
    mapper_uuyp_file: Path
    mapper_steam_file: Path
    mapper_docx_file: Path | None
    market_watchlist_file: Path | None
    market_template_library_file: Path | None

    ui_max_rows: int

    source_min_slowdown: float
    source_max_slowdown: float
    slowdown_warn_threshold: float
    roll_priority_queue_warn_size: int
    health_check_interval_seconds: int
    health_check_ttl_seconds: int
    health_check_price_deviation_threshold: float
    health_check_timestamp_skew_seconds: int
    health_check_duplicate_window_seconds: int

    verify_enabled: bool
    verify_interval_seconds: int
    verify_sample_size: int
    verify_check_live_youpin: bool
    youpin_fresh_seconds: int
    roll_fresh_seconds: int
    auto_verify_profit_threshold: float
    auto_verify_max_batch: int
    show_only_fresh_by_default: bool
    default_api_only_verified: bool
    serving_fresh_seconds: int
    ui_refresh_seconds: int
    youpin_live_verify_rps: float
    youpin_live_verify_burst: int
    youpin_live_verify_workers: int
    youpin_live_timeout_seconds: float
    youpin_live_max_retries: int
    backoff_base_seconds: float
    backoff_max_seconds: float
    backoff_jitter_ratio: float
    youpin_anomaly_lookback: int
    youpin_anomaly_min_history: int
    youpin_anomaly_multiplier: float
    anomaly_mad_multiplier: float
    anomaly_iqr_multiplier: float

    snapshot_on_cycle_complete: bool
    snapshot_retention_days: int

    @staticmethod
    def load() -> "Settings":
        _load_runtime_dotenv()
        root = Path(os.getenv("PROJECT_ROOT", ".")).resolve()
        data_dir = root / "data"
        logs_dir = root / "logs"
        mapping_dir = data_dir / "mappings"

        data_dir.mkdir(parents=True, exist_ok=True)
        logs_dir.mkdir(parents=True, exist_ok=True)
        mapping_dir.mkdir(parents=True, exist_ok=True)

        page_size = _int_env("ROLL_PAGE_SIZE", 250)
        page_size = max(1, min(250, page_size))
        pages_per_loop = _int_env("ROLL_PAGES_PER_LOOP", 3)
        pages_per_loop = max(1, min(20, pages_per_loop))
        multi_item_mode = os.getenv("ROLL_MULTI_ITEM_MODE", "split_items").strip().lower()
        if multi_item_mode not in {"skip", "split_items"}:
            multi_item_mode = "split_items"
        roll_refresh_mode = os.getenv("ROLL_REFRESH_MODE", "continuous").strip().lower()
        if roll_refresh_mode not in {"continuous", "daily"}:
            roll_refresh_mode = "continuous"
        roll_daily_refresh_hour = max(0, min(23, _int_env("ROLL_DAILY_REFRESH_HOUR", 9)))
        roll_daily_refresh_minute = max(
            0, min(59, _int_env("ROLL_DAILY_REFRESH_MINUTE", 0))
        )

        strategy = os.getenv("TOP_BUCKET_STRATEGY", "min").strip().lower()
        if strategy not in {"min", "avg", "median"}:
            strategy = "min"

        market_watchlist_raw = os.getenv("MARKET_WATCHLIST_FILE", "").strip()
        default_market_watchlist = _first_existing_path(
            [
                root / "工作簿1.xlsx",
                Path.home() / "Downloads" / "uu-roll.xlsx",
            ]
        )
        if market_watchlist_raw:
            market_watchlist_file: Path | None = Path(market_watchlist_raw)
        elif default_market_watchlist is not None:
            market_watchlist_file = default_market_watchlist
        else:
            market_watchlist_file = None
        mapper_docx_raw = os.getenv("MAPPER_DOCX_FILE", "").strip()
        if mapper_docx_raw:
            mapper_docx_file: Path | None = Path(mapper_docx_raw)
        else:
            mapper_docx_file = _first_existing_path(
                [
                    root / "中英文映射.docx",
                    root / "崭新出厂.docx",
                    Path.home() / "Documents" / "中英文映射.docx",
                    Path.home() / "Documents" / "崭新出厂.docx",
                ]
            )
        market_template_library_raw = os.getenv("MARKET_TEMPLATE_LIBRARY_FILE", "").strip()
        default_market_template_library = root / "services" / "id库.xlsx"
        if market_template_library_raw:
            market_template_library_file: Path | None = Path(market_template_library_raw)
        elif default_market_template_library.exists():
            market_template_library_file = default_market_template_library
        else:
            market_template_library_file = None

        if mapper_docx_file is None:
            mapper_docx_file = _first_existing_path(
                [
                    root / "中英文映射.docx",
                    root / "崭新出厂.docx",
                    Path.home() / "Documents" / "中英文映射.docx",
                    Path.home() / "Documents" / "崭新出厂.docx",
                    root / "data" / "docx_exports" / "zhangxinchuchang_dedup.json",
                ]
            )
        if market_template_library_file is None:
            market_template_library_file = _first_existing_path(
                [
                    root / "services" / "id库.xlsx",
                    root / "services" / "id.xlsx",
                ]
            )

        return Settings(
            app_host=os.getenv("APP_HOST", "127.0.0.1"),
            app_port=_int_env("APP_PORT", 8000),
            db_path=Path(os.getenv("DB_PATH", str(data_dir / "app.db"))),
            log_path=Path(os.getenv("LOG_PATH", str(logs_dir / "app.log"))),
            proxy_url=os.getenv("HTTP_PROXY_URL") or None,
            roll_page_size=page_size,
            roll_pages_per_loop=pages_per_loop,
            roll_intra_page_delay_seconds=_float_env(
                "ROLL_INTRA_PAGE_DELAY_SECONDS", 0.2
            ),
            roll_multi_item_mode=multi_item_mode,
            roll_refresh_mode=roll_refresh_mode,
            roll_daily_refresh_hour=roll_daily_refresh_hour,
            roll_daily_refresh_minute=roll_daily_refresh_minute,
            roll_daily_fresh_seconds=max(
                300, _int_env("ROLL_DAILY_FRESH_SECONDS", 93600)
            ),
            roll_status=os.getenv("ROLL_STATUS", "LISTED"),
            roll_order_by=tuple(
                part.strip()
                for part in os.getenv("ROLL_ORDER_BY", "TOTAL_VALUE_DESC").split(",")
                if part.strip()
            ),
            roll_base_interval_seconds=_float_env("ROLL_BASE_INTERVAL_SECONDS", 1.0),
            roll_jitter_seconds=_float_env("ROLL_JITTER_SECONDS", 0.4),
            roll_trade_ttl_seconds=_int_env("ROLL_TRADE_TTL_SECONDS", 1800),
            roll_max_retries=_int_env("ROLL_MAX_RETRIES", 3),
            roll_excluded_categories={
                part.strip().lower()
                for part in os.getenv(
                    "ROLL_EXCLUDED_CATEGORIES",
                    "shotgun,shotguns,machinegun,machineguns,stickers,others",
                ).split(",")
                if part.strip()
            },
            special_detail_enabled=_bool_env("SPECIAL_DETAIL_ENABLED", True),
            special_detail_batch_size=max(
                1, min(20, _int_env("SPECIAL_DETAIL_BATCH_SIZE", 12))
            ),
            special_detail_pause_normal_queue=_bool_env(
                "SPECIAL_DETAIL_PAUSE_NORMAL_QUEUE", False
            ),
            special_detail_candidate_ttl_seconds=max(
                1800, _int_env("SPECIAL_DETAIL_CANDIDATE_TTL_SECONDS", 10800)
            ),
            special_detail_confirmed_ttl_seconds=max(
                1800, _int_env("SPECIAL_DETAIL_CONFIRMED_TTL_SECONDS", 3600)
            ),
            special_detail_timeout_seconds=max(
                3.0, _float_env("SPECIAL_DETAIL_TIMEOUT_SECONDS", 20.0)
            ),
            special_detail_headless=_bool_env("SPECIAL_DETAIL_HEADLESS", True),
            special_detail_browser_path=(
                os.getenv("SPECIAL_DETAIL_BROWSER_PATH", "").strip() or None
            ),
            special_detail_url_template=(
                os.getenv("SPECIAL_DETAIL_URL_TEMPLATE", "").strip()
            ),
            youpin_ttl_seconds=max(10, _int_env("YOUPIN_TTL_SECONDS", 60)),
            youpin_priority_ttl_seconds=max(
                10, _int_env("YOUPIN_PRIORITY_TTL_SECONDS", 30)
            ),
            youpin_high_priority_ttl_seconds=max(
                5, _int_env("YOUPIN_HIGH_PRIORITY_TTL_SECONDS", 10)
            ),
            youpin_batch_size=_int_env("YOUPIN_BATCH_SIZE", 160),
            youpin_refresh_workers=max(
                1, _int_env("YOUPIN_REFRESH_WORKERS", 16)
            ),
            youpin_cycle_time_budget_seconds=max(
                5.0, _float_env("YOUPIN_CYCLE_TIME_BUDGET_SECONDS", 120.0)
            ),
            youpin_per_request_delay_seconds=_float_env(
                "YOUPIN_PER_REQUEST_DELAY_SECONDS", 0.01
            ),
            youpin_max_retries=_int_env("YOUPIN_MAX_RETRIES", 3),
            youpin_failure_retry_base_seconds=max(
                5, _int_env("YOUPIN_FAILURE_RETRY_BASE_SECONDS", 20)
            ),
            youpin_failure_retry_max_seconds=max(
                30, _int_env("YOUPIN_FAILURE_RETRY_MAX_SECONDS", 600)
            ),
            youpin_rate_limit_retry_base_seconds=max(
                10, _int_env("YOUPIN_RATE_LIMIT_RETRY_BASE_SECONDS", 30)
            ),
            youpin_rate_limit_retry_max_seconds=max(
                60, _int_env("YOUPIN_RATE_LIMIT_RETRY_MAX_SECONDS", 1200)
            ),
            youpin_empty_price_retry_base_seconds=max(
                20, _int_env("YOUPIN_EMPTY_PRICE_RETRY_BASE_SECONDS", 60)
            ),
            youpin_empty_price_retry_max_seconds=max(
                120, _int_env("YOUPIN_EMPTY_PRICE_RETRY_MAX_SECONDS", 1800)
            ),
            youpin_empty_warn_min_attempts=max(
                1, _int_env("YOUPIN_EMPTY_WARN_MIN_ATTEMPTS", 20)
            ),
            youpin_empty_warn_rate=min(
                1.0, max(0.0, _float_env("YOUPIN_EMPTY_WARN_RATE", 0.35))
            ),
            youpin_browser_context_enabled=_bool_env(
                "YOUPIN_BROWSER_CONTEXT_ENABLED", False
            ),
            youpin_browser_context_cdp_url=(
                os.getenv("YOUPIN_BROWSER_CONTEXT_CDP_URL", "").strip() or None
            ),
            youpin_browser_context_page_url=(
                os.getenv(
                    "YOUPIN_BROWSER_CONTEXT_PAGE_URL",
                    "https://www.youpin898.com/",
                ).strip()
                or "https://www.youpin898.com/"
            ),
            youpin_browser_context_timeout_seconds=max(
                2.0,
                _float_env("YOUPIN_BROWSER_CONTEXT_TIMEOUT_SECONDS", 20.0),
            ),
            youpin898_detail_enabled=_bool_env("YOUPIN898_DETAIL_ENABLED", True),
            youpin898_base_url=(
                os.getenv("YOUPIN898_BASE_URL", "https://api.youpin898.com").strip()
                or "https://api.youpin898.com"
            ),
            youpin898_timeout_seconds=max(
                3.0, _float_env("YOUPIN898_TIMEOUT_SECONDS", 20.0)
            ),
            youpin898_token=os.getenv("YOUPIN898_TOKEN", "").strip() or None,
            youpin898_device_token=(
                os.getenv("YOUPIN898_DEVICE_TOKEN", "").strip() or None
            ),
            youpin898_device_id=os.getenv("YOUPIN898_DEVICE_ID", "").strip() or None,
            youpin898_app_version=(
                os.getenv("YOUPIN898_APP_VERSION", "5.28.3").strip() or "5.28.3"
            ),
            youpin898_user_agent=(
                os.getenv("YOUPIN898_USER_AGENT", "okhttp/3.14.9").strip()
                or "okhttp/3.14.9"
            ),
            csgoroll_user_agent=os.getenv(
                "CSGOROLL_USER_AGENT",
                (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
            ),
            strategy_top_markup_bucket=strategy,
            roll_to_youpin_rate=_float_env("ROLL_TO_YOUPIN_RATE", 4.06),
            roll_target_markup_percent=_float_env("ROLL_TARGET_MARKUP_PERCENT", 11.5),
            roll_target_markup_eps=max(
                0.001, _float_env("ROLL_TARGET_MARKUP_EPS", 0.02)
            ),
            platform_fee_rate=_float_env("PLATFORM_FEE_RATE", 0.025),
            withdraw_loss_rate=_float_env("WITHDRAW_LOSS_RATE", 0.01),
            exchange_rate_enabled=_bool_env("EXCHANGE_RATE_ENABLED", True),
            exchange_rate_primary_source=os.getenv(
                "EXCHANGE_RATE_PRIMARY_SOURCE", "frankfurter"
            ).strip().lower(),
            exchange_rate_backup_source=os.getenv(
                "EXCHANGE_RATE_BACKUP_SOURCE", "open_er_api"
            ).strip().lower(),
            exchange_rate_base=os.getenv("EXCHANGE_RATE_BASE", "USD").strip().upper(),
            exchange_rate_target=os.getenv("EXCHANGE_RATE_TARGET", "CNY")
            .strip()
            .upper(),
            exchange_rate_refresh_seconds=max(
                30, _int_env("EXCHANGE_RATE_REFRESH_SECONDS", 120)
            ),
            exchange_rate_timeout_seconds=_float_env(
                "EXCHANGE_RATE_TIMEOUT_SECONDS", 8.0
            ),
            roll_coin_to_usd_rate=_float_env("ROLL_COIN_TO_USD_RATE", 1.0),
            exchange_rate_max_backoff_seconds=max(
                60, _int_env("EXCHANGE_RATE_MAX_BACKOFF_SECONDS", 900)
            ),
            exchange_rate_manual_override_seconds=max(
                0, _int_env("EXCHANGE_RATE_MANUAL_OVERRIDE_SECONDS", 0)
            ),
            mapper_auto_download=_bool_env("MAPPER_AUTO_DOWNLOAD", True),
            mapper_refresh_seconds=max(
                0, _int_env("MAPPER_REFRESH_SECONDS", 86400)
            ),
            mapper_uuyp_url=os.getenv(
                "MAPPER_UUYP_URL",
                "https://raw.githubusercontent.com/EricZhu-42/"
                "SteamTradingSite-ID-Mapper/main/uuyp/730.json",
            ),
            mapper_steam_url=os.getenv(
                "MAPPER_STEAM_URL",
                "https://raw.githubusercontent.com/EricZhu-42/"
                "SteamTradingSite-ID-Mapper/main/steam/730.json",
            ),
            mapper_uuyp_file=Path(
                os.getenv("MAPPER_UUYP_FILE", str(mapping_dir / "uuyp_730.json"))
            ),
            mapper_steam_file=Path(
                os.getenv("MAPPER_STEAM_FILE", str(mapping_dir / "steam_730.json"))
            ),
            mapper_docx_file=mapper_docx_file,
            market_watchlist_file=market_watchlist_file,
            market_template_library_file=market_template_library_file,
            ui_max_rows=_int_env("UI_MAX_ROWS", 3000),
            source_min_slowdown=_float_env("SOURCE_MIN_SLOWDOWN", 1.0),
            source_max_slowdown=_float_env("SOURCE_MAX_SLOWDOWN", 8.0),
            slowdown_warn_threshold=max(
                1.0, _float_env("SLOWDOWN_WARN_THRESHOLD", 2.5)
            ),
            roll_priority_queue_warn_size=max(
                1, _int_env("ROLL_PRIORITY_QUEUE_WARN_SIZE", 60)
            ),
            health_check_interval_seconds=max(
                10, _int_env("HEALTH_CHECK_INTERVAL_SECONDS", 60)
            ),
            health_check_ttl_seconds=max(
                30, _int_env("HEALTH_CHECK_TTL_SECONDS", 120)
            ),
            health_check_price_deviation_threshold=max(
                0.01, _float_env("HEALTH_CHECK_PRICE_DEVIATION_THRESHOLD", 0.5)
            ),
            health_check_timestamp_skew_seconds=max(
                60, _int_env("HEALTH_CHECK_TIMESTAMP_SKEW_SECONDS", 300)
            ),
            health_check_duplicate_window_seconds=max(
                10, _int_env("HEALTH_CHECK_DUPLICATE_WINDOW_SECONDS", 60)
            ),
            verify_enabled=_bool_env("VERIFY_ENABLED", True),
            verify_interval_seconds=max(30, _int_env("VERIFY_INTERVAL_SECONDS", 300)),
            verify_sample_size=max(1, min(20, _int_env("VERIFY_SAMPLE_SIZE", 5))),
            verify_check_live_youpin=_bool_env("VERIFY_CHECK_LIVE_YOUPIN", False),
            youpin_fresh_seconds=max(30, _int_env("YOUPIN_FRESH_SECONDS", 300)),
            roll_fresh_seconds=max(30, _int_env("ROLL_FRESH_SECONDS", 300)),
            auto_verify_profit_threshold=_float_env(
                "AUTO_VERIFY_PROFIT_THRESHOLD", 10.0
            ),
            auto_verify_max_batch=max(1, _int_env("AUTO_VERIFY_MAX_BATCH", 30)),
            show_only_fresh_by_default=_bool_env(
                "SHOW_ONLY_FRESH_BY_DEFAULT", True
            ),
            default_api_only_verified=_bool_env("DEFAULT_API_ONLY_VERIFIED", True),
            serving_fresh_seconds=max(30, _int_env("SERVING_FRESH_SECONDS", 120)),
            ui_refresh_seconds=max(1, _int_env("UI_REFRESH_SECONDS", 5)),
            youpin_live_verify_rps=max(
                0.1, _float_env("YOUPIN_LIVE_VERIFY_RPS", 1.5)
            ),
            youpin_live_verify_burst=max(
                1, _int_env("YOUPIN_LIVE_VERIFY_BURST", 3)
            ),
            youpin_live_verify_workers=max(
                1, _int_env("YOUPIN_LIVE_VERIFY_WORKERS", 10)
            ),
            youpin_live_timeout_seconds=max(
                0.5, _float_env("YOUPIN_LIVE_TIMEOUT_SECONDS", 2.5)
            ),
            youpin_live_max_retries=max(
                1, _int_env("YOUPIN_LIVE_MAX_RETRIES", 3)
            ),
            backoff_base_seconds=max(
                0.05, _float_env("BACKOFF_BASE_SECONDS", 0.4)
            ),
            backoff_max_seconds=max(
                0.2, _float_env("BACKOFF_MAX_SECONDS", 8.0)
            ),
            backoff_jitter_ratio=min(
                0.9, max(0.0, _float_env("BACKOFF_JITTER_RATIO", 0.2))
            ),
            youpin_anomaly_lookback=max(
                3, _int_env("YOUPIN_ANOMALY_LOOKBACK", 20)
            ),
            youpin_anomaly_min_history=max(
                3, _int_env("YOUPIN_ANOMALY_MIN_HISTORY", 5)
            ),
            youpin_anomaly_multiplier=max(
                1.1, _float_env("YOUPIN_ANOMALY_MULTIPLIER", 2.0)
            ),
            anomaly_mad_multiplier=max(
                1.0, _float_env("ANOMALY_MAD_MULTIPLIER", 6.0)
            ),
            anomaly_iqr_multiplier=max(
                0.5, _float_env("ANOMALY_IQR_MULTIPLIER", 2.5)
            ),
            snapshot_on_cycle_complete=_bool_env("SNAPSHOT_ON_CYCLE_COMPLETE", False),
            snapshot_retention_days=max(1, _int_env("SNAPSHOT_RETENTION_DAYS", 30)),
        )
