from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager
import httpx
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from config.logging_config import setup_logging
from config.settings import Settings
from shared.market_watchlist import load_market_watch_targets
from shared.normalizer import build_normalized_compare_key, normalize_special_variant, normalize_wear
from shared.repository import Repository
from shared.utils import now_ts

settings = Settings.load()
setup_logging(settings.log_path)
repo = Repository(settings.db_path)

NO_STORE_HEADERS = {
    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    "Pragma": "no-cache",
    "Expires": "0",
}

YOUPIN_PUBLIC_ONLY_REASON = "Compare service is read-only; collection features are disabled."
ROLL_SERVICE_BASE_URL = str(os.getenv("ROLL_SERVICE_BASE_URL", "http://127.0.0.1:8101")).rstrip("/")
YOUPIN_SERVICE_BASE_URL = str(os.getenv("YOUPIN_SERVICE_BASE_URL", "http://127.0.0.1:8102")).rstrip("/")

@asynccontextmanager
async def lifespan(_app: FastAPI):
    try:
        yield
    finally:
        repo.close()


app = FastAPI(title="Compare Display Service", version="1.0.0", lifespan=lifespan)

client_sessions: dict[str, int] = {}
client_session_stale_seconds = 20


class ClientSessionPayload(BaseModel):
    session_id: str = Field(..., min_length=8, max_length=128)


class RollRatePayload(BaseModel):
    value: float = Field(..., gt=0.0, description="ROLL to Youpin rate")


class WatchlistImportPayload(BaseModel):
    watchlist_id: str = Field(default="default", min_length=1, max_length=64)
    path: str | None = None
    items: list[dict] | None = None


def _get_runtime_roll_rate() -> tuple[float, str, int | None]:
    raw = (repo.get_state("roll_to_youpin_rate") or "").strip()
    if raw:
        try:
            value = float(raw)
            if value > 0:
                source = (repo.get_state("roll_to_youpin_rate_source") or "").strip() or "runtime"
                updated_raw = (repo.get_state("roll_to_youpin_rate_updated_at") or "").strip()
                updated_at = int(updated_raw) if updated_raw.isdigit() else None
                return value, source, updated_at
        except ValueError:
            pass
    return float(settings.roll_to_youpin_rate), "default", None


def _comparison_data_version() -> dict[str, int | str]:
    repo_version = repo.comparison_data_version()
    token = ":".join(
        [
            str(repo_version.get("latest_update_at", 0) or 0),
            str(repo_version.get("comparison_count", 0) or 0),
            str(repo_version.get("serving_count", 0) or 0),
        ]
    )
    return {**repo_version, "token": token}


def _session_status_payload() -> dict:
    return {
        "mode": "public_only",
        "configured": False,
        "valid": None,
        "updated_at": None,
        "last_check_at": None,
        "source": "public_direct",
        "cookie_preview": None,
        "cookie_names": [],
        "profile_header_names": [],
        "last_error": None,
        "disabled_reason": YOUPIN_PUBLIC_ONLY_REASON,
    }


def _safe_int(value: object, default: int = 0) -> int:
    try:
        return int(value or 0)
    except Exception:
        return default


async def _call_service(
    *,
    service: str,
    method: str,
    path: str,
    payload: dict | None = None,
    timeout_seconds: float = 4.0,
) -> dict:
    base_url = ROLL_SERVICE_BASE_URL if service == "roll" else YOUPIN_SERVICE_BASE_URL
    url = f"{base_url}{path}"
    try:
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            if method.upper() == "GET":
                response = await client.get(url)
            else:
                response = await client.post(url, json=payload)
        data: dict
        try:
            data = response.json()
        except Exception:
            data = {"raw": response.text}
        return {
            "ok": bool(response.status_code < 400),
            "status_code": int(response.status_code),
            "service": service,
            "url": url,
            "data": data,
        }
    except Exception as exc:
        return {
            "ok": False,
            "status_code": 0,
            "service": service,
            "url": url,
            "error": str(exc),
        }


def _safe_path_name(path: Path | None) -> str | None:
    if path is None:
        return None
    try:
        return path.name
    except Exception:
        return None


def _safe_path_size(path: Path | None) -> int | None:
    if path is None:
        return None
    try:
        return int(path.stat().st_size)
    except Exception:
        return None


def _load_compare_watch_targets():
    target_path = settings.market_watchlist_file or settings.mapper_docx_file
    return target_path, load_market_watch_targets(target_path)


def _query_dashboard_top_rows(limit: int = 12) -> tuple[str, list[dict]]:
    rows = repo.query_serving_comparisons(
        search="",
        sort_by="net_profit_rate",
        order="desc",
        sort_by_secondary="roll_cost",
        order_secondary="asc",
        wear_filter="",
        variant_filter="",
        weapon_filter="",
        min_roll_price=0.0,
        only_fresh=False,
        only_verified=False,
        limit=limit,
    )
    if rows:
        return "serving", rows
    return (
        "warehouse",
        repo.query_comparisons(
            search="",
            sort_by="net_profit_rate",
            order="desc",
            limit=limit,
            wear_filter="",
            variant_filter="",
            weapon_filter="",
            min_available=0,
            min_roll_price=0.0,
            only_fresh=False,
            min_decision_grade="",
            only_verified=False,
            only_profitable=False,
            sort_by_secondary="roll_cost",
            order_secondary="asc",
            priced_only=False,
        ),
    )


@app.get("/")
async def index() -> dict:
    return {"ok": True, "service": "compare_display", "ui": "disabled", "server_time": now_ts()}


@app.get("/status")
async def status_page() -> dict:
    return {"ok": True, "service": "compare_display", "status": "running", "server_time": now_ts()}


@app.post("/api/client-session/open")
async def client_session_open(payload: ClientSessionPayload = Body(...)) -> dict:
    client_sessions[payload.session_id] = now_ts()
    return {"ok": True}


@app.post("/api/client-session/heartbeat")
async def client_session_heartbeat(payload: ClientSessionPayload = Body(...)) -> dict:
    client_sessions[payload.session_id] = now_ts()
    return {"ok": True}


@app.post("/api/client-session/close")
async def client_session_close(payload: ClientSessionPayload = Body(...)) -> dict:
    client_sessions.pop(payload.session_id, None)
    return {"ok": True}


@app.get("/api/client-session/status")
async def client_session_status(session_id: str = Query(..., min_length=8, max_length=128)) -> dict:
    ts = now_ts()
    last_seen = client_sessions.get(session_id)
    active = bool(last_seen and ts - last_seen <= client_session_stale_seconds)
    return {
        "session_id": session_id,
        "active": active,
        "last_seen_at": int(last_seen or 0),
        "stale_after_seconds": client_session_stale_seconds,
    }


@app.get("/api/youpin-session/status")
async def youpin_session_status() -> dict:
    return _session_status_payload()


@app.post("/api/youpin898/runtime/import")
async def import_youpin_runtime() -> dict:
    raise HTTPException(status_code=410, detail=YOUPIN_PUBLIC_ONLY_REASON)


@app.get("/api/health")
async def health() -> dict:
    stats = repo.stats()
    return {
        "ok": True,
        "monitor_running": False,
        "roll_ok": True,
        "youpin_ok": True,
        "db_path": str(settings.db_path),
        "comparisons": _safe_int(stats.get("comparisons")),
        "server_time": now_ts(),
    }


@app.get("/api/system-status")
async def system_status() -> dict:
    stats = repo.stats()
    comparison_counts = repo.comparison_status_counts()
    serving_counts = repo.serving_status_counts()
    rejection_counts = repo.strict_rejection_counts()
    rate_value, rate_source, rate_updated_at = _get_runtime_roll_rate()
    exchange_updated_raw = (repo.get_state("exchange_rate_updated_at") or "").strip()
    exchange_updated_at = int(exchange_updated_raw) if exchange_updated_raw.isdigit() else None
    exchange_age_seconds = max(0, now_ts() - exchange_updated_at) if exchange_updated_at else None
    return {
        "server_time": now_ts(),
        "monitor_running": False,
        "roll_refresh_mode": "externalized",
        "roll_daily_refresh_hour": settings.roll_daily_refresh_hour,
        "roll_daily_refresh_minute": settings.roll_daily_refresh_minute,
        "roll_last_daily_refresh_at": _safe_int(repo.get_state("roll_last_daily_refresh_at")),
        "roll_daily_snapshot_rows": _safe_int(stats.get("roll_daily_snapshot_rows")),
        "roll_daily_snapshot_at": _safe_int(stats.get("roll_daily_snapshot_at")),
        "full_scan_active": False,
        "full_scan_requested_at": 0,
        "full_scan_started_at": 0,
        "full_scan_completed_at": 0,
        "last_monitor_cycle_at": 0,
        "last_serving_build_at": _safe_int(stats.get("last_comparison_update")),
        "last_live_verify_at": _safe_int(repo.latest_serving_live_verify_time()),
        "warehouse_count": _safe_int(comparison_counts.get("total")),
        "product_catalog_count": _safe_int(stats.get("product_catalog")),
        "active_product_catalog_count": _safe_int(stats.get("active_product_catalog")),
        "serving_count": _safe_int(serving_counts.get("total")),
        "fresh_pair_count": _safe_int(serving_counts.get("fresh")),
        "verified_live_count": _safe_int(serving_counts.get("live_verified")),
        "stale_count": _safe_int(comparison_counts.get("stale")),
        "null_price_count": _safe_int(rejection_counts.get("rejected_null_price_count")),
        "anomaly_count": _safe_int(rejection_counts.get("rejected_anomaly_count")),
        "roll_ok": True,
        "youpin_ok": True,
        "roll_rate_value": rate_value,
        "roll_rate_source": rate_source,
        "roll_rate_updated_at": rate_updated_at,
        "exchange_rate_source": (repo.get_state("exchange_rate_source") or "").strip() or "unknown",
        "exchange_rate_age_seconds": exchange_age_seconds,
        "youpin_session": _session_status_payload(),
        "special_detail_backlog": 0,
        "special_detail_processed": 0,
        "special_detail_confirmed": 0,
        "special_detail_candidate_only": 0,
        "special_detail_failed": 0,
        "special_detail_disabled": True,
        "watchlist_template_id_total": 0,
        "watchlist_template_id_queued": 0,
        "watchlist_template_id_attempted": 0,
        "watchlist_template_id_success": 0,
        "watchlist_template_id_resolved_names": 0,
        "watchlist_template_id_rate_limited": 0,
        "watchlist_template_id_empty_price": 0,
        "verify_queue_size": 0,
        "verify_queue_trend": "flat",
        "youpin_429_rate": 0.0,
        "refresh_queue_size": 0,
        "slowdown": {"roll": 1.0, "youpin": 1.0, "max": 1.0},
        "last_error": None,
        "recent_logs": [],
    }


@app.get("/api/dashboard")
async def dashboard() -> dict:
    stats = repo.stats()
    comparison_counts = repo.comparison_status_counts()
    serving_counts = repo.serving_status_counts()
    target_path, targets = _load_compare_watch_targets()
    min_roll_seen_ts = now_ts() - int(settings.roll_trade_ttl_seconds)
    snapshot = repo.watchlist_compare_snapshot(
        market_names=targets.names,
        template_ids=targets.template_ids,
        min_roll_seen_ts=min_roll_seen_ts,
    )
    top_rows_mode, top_rows = _query_dashboard_top_rows(limit=12)
    _, rate_source, _ = _get_runtime_roll_rate()
    return {
        "server_time": now_ts(),
        "data_version": _comparison_data_version(),
        "import_status": {
            "watchlist_file_name": _safe_path_name(target_path),
            "watchlist_path": str(target_path) if target_path else None,
            "template_target_count": len(targets.template_ids),
            "name_target_count": len(targets.names),
            "source_row_count": int(getattr(targets, "source_row_count", 0) or 0),
            "mapper_docx_name": _safe_path_name(settings.mapper_docx_file),
            "mapper_docx_path": str(settings.mapper_docx_file) if settings.mapper_docx_file else None,
        },
        "roll_status": {
            "roll_trades": _safe_int(stats.get("roll_trades")),
            "product_catalog": _safe_int(stats.get("product_catalog")),
            "active_product_catalog": _safe_int(stats.get("active_product_catalog")),
            "watchlist_roll_hits": len(snapshot["roll_name_hits"]),
        },
        "youpin_status": {
            "youpin_prices": _safe_int(stats.get("youpin_prices")),
            "template_hit_count": len(snapshot["template_id_hits"]),
            "template_priced_count": len(snapshot["priced_template_id_hits"]),
            "session_mode": _session_status_payload()["mode"],
        },
        "compare_status": {
            "comparison_total": _safe_int(comparison_counts.get("total")),
            "serving_total": _safe_int(serving_counts.get("total")),
            "fresh_pair_count": _safe_int(serving_counts.get("fresh")),
            "live_verified_count": _safe_int(serving_counts.get("live_verified")),
        },
        "db_status": {
            "db_path": str(settings.db_path),
            "db_size_bytes": _safe_path_size(settings.db_path),
            "last_comparison_update": _safe_int(stats.get("last_comparison_update")),
            "comparison_snapshots": _safe_int(stats.get("comparison_snapshots")),
            "last_snapshot_at": _safe_int(stats.get("last_snapshot_at")),
            "exchange_rate_source": (repo.get_state("exchange_rate_source") or "").strip() or rate_source,
        },
        "top_rows_mode": top_rows_mode,
        "top_rows": top_rows,
    }


@app.get("/api/control-deck/runtime")
async def control_deck_runtime(limit: int = Query(200, ge=10, le=1000)) -> dict:
    stats = repo.stats()
    comparison_counts = repo.comparison_status_counts()
    serving_counts = repo.serving_status_counts()
    rate_value, rate_source, _ = _get_runtime_roll_rate()
    top_rows = repo.query_comparisons(
        search="",
        sort_by="roll_cost",
        order="desc",
        limit=limit,
        wear_filter="",
        variant_filter="",
        weapon_filter="",
        min_available=1,
        min_roll_price=0.0,
        only_fresh=False,
        min_decision_grade="",
        only_verified=False,
        only_profitable=False,
        sort_by_secondary="",
        order_secondary="desc",
        priced_only=False,
    )
    roll_dash, youpin_dash = await _call_service(service="roll", method="GET", path="/api/dashboard"), await _call_service(
        service="youpin",
        method="GET",
        path="/api/dashboard",
    )
    active_watchlist_id = (repo.get_state("watchlist_active_id") or "default").strip() or "default"
    active_batch_id = (repo.get_state("watchlist_active_batch_id") or "").strip()
    progress = repo.watchlist_progress_stats(active_watchlist_id, active_batch_id or None)
    wl_rows = repo.query_watchlist_compare(
        watchlist_id=active_watchlist_id,
        batch_id=active_batch_id or None,
        coin_rate=rate_value,
        sort_by="roll_rmb_price",
        sort_order="desc",
        secondary_sort_by="profit_rate",
        secondary_sort_order="desc",
    )
    compare_ok_count = int(progress.get("compare_ok_count") or 0)
    pending_count = int(progress.get("pending_count") or 0)
    return {
        "server_time": now_ts(),
        "refresh_policy": {
            "result_refresh_seconds": 1,
            "aux_refresh_seconds": 15,
        },
        "monitor_running": bool(roll_dash.get("ok")) and bool(youpin_dash.get("ok")),
        "roll_service": roll_dash,
        "youpin_service": youpin_dash,
        "state": {
            "comparisons": _safe_int(comparison_counts.get("total")),
            "serving": _safe_int(serving_counts.get("total")),
            "fresh_pairs": _safe_int(serving_counts.get("fresh")),
            "live_verified": _safe_int(serving_counts.get("live_verified")),
            "roll_rate": rate_value,
            "roll_rate_source": rate_source,
            "db_path": str(settings.db_path),
            "last_build_at": _safe_int(stats.get("last_comparison_update")),
            "last_verify_at": _safe_int(stats.get("last_verification_at")),
            "watchlist_total": int(progress.get("watchlist_total") or 0),
            "roll_done_count": int(progress.get("roll_done_count") or 0),
            "youpin_done_count": int(progress.get("youpin_done_count") or 0),
            "compare_ok_count": compare_ok_count,
            "failed_count": int(progress.get("failed_count") or 0),
            "pending_count": pending_count,
            "current_batch_id": active_batch_id or None,
            "compare_read_only": True,
        },
        "rows": top_rows,
        "watchlist_rows": wl_rows,
    }


@app.post("/api/control-deck/actions/refresh")
async def control_deck_refresh_action(watchlist_id: str = Query("default")) -> dict:
    batch_id = repo.create_batch_id()
    repo.set_state("watchlist_active_batch_id", batch_id)
    repo.set_state("watchlist_active_id", watchlist_id)
    roll_run, youpin_run = await _call_service(service="roll", method="POST", path="/run-once"), await _call_service(
        service="youpin",
        method="POST",
        path="/run-once",
    )
    return {
        "ok": bool(roll_run.get("ok") and youpin_run.get("ok")),
        "action": "refresh",
        "watchlist_id": watchlist_id,
        "batch_id": batch_id,
        "results": [roll_run, youpin_run],
    }


@app.post("/api/control-deck/actions/start-loops")
async def control_deck_start_loops_action(watchlist_id: str = Query("default")) -> dict:
    repo.set_state("watchlist_active_id", watchlist_id)
    roll_start, youpin_start = await _call_service(service="roll", method="POST", path="/loop/start"), await _call_service(
        service="youpin",
        method="POST",
        path="/loop/start",
    )
    return {
        "ok": bool(roll_start.get("ok") and youpin_start.get("ok")),
        "action": "start-loops",
        "watchlist_id": watchlist_id,
        "results": [roll_start, youpin_start],
    }


@app.post("/api/control-deck/actions/youpin-fill")
async def control_deck_youpin_fill_action(watchlist_id: str = Query("default")) -> dict:
    batch_id = (repo.get_state("watchlist_active_batch_id") or "").strip() or repo.create_batch_id()
    repo.set_state("watchlist_active_batch_id", batch_id)
    repo.set_state("watchlist_active_id", watchlist_id)
    youpin_run = await _call_service(service="youpin", method="POST", path="/run-once")
    return {
        "ok": bool(youpin_run.get("ok")),
        "action": "youpin-fill",
        "watchlist_id": watchlist_id,
        "batch_id": batch_id,
        "results": [youpin_run],
    }


@app.post("/api/control-deck/actions/full-scan")
async def control_deck_full_scan_action(watchlist_id: str = Query("default")) -> dict:
    # In split architecture, full scan is orchestrated by collector loops.
    batch_id = repo.create_batch_id()
    repo.set_state("watchlist_active_batch_id", batch_id)
    repo.set_state("watchlist_active_id", watchlist_id)
    roll_start, youpin_start = await _call_service(service="roll", method="POST", path="/loop/start"), await _call_service(
        service="youpin",
        method="POST",
        path="/loop/start",
    )
    roll_run, youpin_run = await _call_service(service="roll", method="POST", path="/run-once"), await _call_service(
        service="youpin",
        method="POST",
        path="/run-once",
    )
    ok = all(bool(item.get("ok")) for item in (roll_start, youpin_start, roll_run, youpin_run))
    return {
        "ok": ok,
        "action": "full-scan",
        "watchlist_id": watchlist_id,
        "batch_id": batch_id,
        "note": "Split architecture executes full-scan intent through collector loops and immediate run-once actions.",
        "results": [roll_start, youpin_start, roll_run, youpin_run],
    }


@app.get("/api/config/roll-rate")
async def get_roll_rate() -> dict:
    value, source, updated_at = _get_runtime_roll_rate()
    return {"value": value, "source": source, "updated_at": updated_at}


@app.post("/api/config/roll-rate")
async def set_roll_rate(payload: RollRatePayload = Body(...)) -> dict:
    value = float(payload.value)
    ts = now_ts()
    repo.set_state("roll_to_youpin_rate", f"{value:.6f}")
    repo.set_state("roll_to_youpin_rate_source", "runtime_manual")
    repo.set_state("roll_to_youpin_rate_updated_at", str(ts))
    return {"value": value, "source": "runtime_manual", "updated_at": ts}


@app.get("/api/comparisons")
async def comparisons(
    search: str = Query(""),
    sort_by: str = Query("roll_cost"),
    order: str = Query("desc"),
    sort_by_secondary: str = Query("gross_profit_rate"),
    order_secondary: str = Query("desc"),
    wear: str = Query(""),
    variant: str = Query(""),
    weapon_type: str = Query(""),
    min_available: int = Query(0, ge=0),
    min_roll_price: float = Query(0.0, ge=0.0),
    mode: str = Query("serving"),
    only_fresh: bool | None = Query(None),
    only_verified: bool | None = Query(None),
    min_decision_grade: str = Query(""),
    only_profitable: bool = Query(False),
    priced_only: bool = Query(False),
    prefer_fresh: bool = Query(False),
    wait_timeout_ms: int = Query(0, ge=0, le=10000),
    limit: int = Query(2000, ge=1, le=10000),
) -> dict:
    _ = prefer_fresh
    _ = wait_timeout_ms
    safe_limit = min(limit, settings.ui_max_rows)
    safe_mode = mode.strip().lower()
    if safe_mode not in {"serving", "warehouse"}:
        safe_mode = "serving"
    effective_only_fresh = bool(only_fresh) if only_fresh is not None else bool(settings.show_only_fresh_by_default)
    effective_only_verified = bool(only_verified) if only_verified is not None else bool(settings.default_api_only_verified)
    if safe_mode == "serving":
        rows = repo.query_serving_comparisons(
            search=search,
            sort_by=sort_by,
            order=order,
            sort_by_secondary=sort_by_secondary,
            order_secondary=order_secondary,
            wear_filter=wear,
            variant_filter=variant,
            weapon_filter=weapon_type,
            min_roll_price=min_roll_price,
            only_fresh=effective_only_fresh,
            only_verified=effective_only_verified,
            limit=safe_limit,
        )
        if not rows:
            safe_mode = "warehouse"
    if safe_mode == "warehouse":
        rows = repo.query_comparisons(
            search=search,
            sort_by=sort_by,
            order=order,
            limit=safe_limit,
            wear_filter=wear,
            variant_filter=variant,
            weapon_filter=weapon_type,
            min_available=min_available,
            min_roll_price=min_roll_price,
            only_fresh=effective_only_fresh,
            min_decision_grade=min_decision_grade,
            only_verified=effective_only_verified,
            only_profitable=only_profitable,
            sort_by_secondary=sort_by_secondary,
            order_secondary=order_secondary,
            priced_only=priced_only,
        )
    return {
        "count": len(rows),
        "rows": rows,
        "sort_by": sort_by,
        "order": order,
        "sort_by_secondary": sort_by_secondary,
        "order_secondary": order_secondary,
        "wear": wear,
        "variant": variant,
        "weapon_type": weapon_type,
        "min_available": min_available,
        "min_roll_price": min_roll_price,
        "only_fresh": effective_only_fresh,
        "only_verified": effective_only_verified,
        "mode": safe_mode,
        "min_decision_grade": min_decision_grade.strip().upper(),
        "only_profitable": bool(only_profitable),
        "priced_only": bool(priced_only),
        "data_version": _comparison_data_version(),
        "server_time": now_ts(),
    }


def _build_watchlist_items_from_path(path: Path | None) -> list[dict]:
    targets = load_market_watch_targets(path)
    items: list[dict] = []
    special_tokens = ["P1", "P2", "P3", "P4", "Phase 1", "Phase 2", "Phase 3", "Phase 4", "Ruby", "Sapphire", "Black Pearl", "Emerald", "红宝石", "蓝宝石", "黑珍珠", "绿宝石"]
    wear_tokens = [
        "Factory New",
        "Minimal Wear",
        "Field-Tested",
        "Well-Worn",
        "Battle-Scarred",
        "崭新出厂",
        "略有磨损",
        "久经沙场",
        "破损不堪",
        "战痕累累",
        "FN",
        "MW",
        "FT",
        "WW",
        "BS",
    ]
    for raw in targets.names:
        raw_name = str(raw or "").strip()
        if not raw_name:
            continue
        special = None
        for token in special_tokens:
            if token.lower() in raw_name.lower():
                special = token
                break
        wear_raw = None
        for wear in wear_tokens:
            if wear.lower() in raw_name.lower():
                wear_raw = wear
                break
        wear_en = normalize_wear(wear_raw)
        clean_name = raw_name
        if special:
            clean_name = clean_name.replace(special, "").strip()
        if wear_raw:
            clean_name = clean_name.replace(wear_raw, "").strip()
        clean_name = clean_name.replace("()", "").replace("（ ）", "").strip()
        normalized_key = raw_name
        if " | " in raw_name:
            left, right = clean_name.split(" | ", 1) if " | " in clean_name else raw_name.split(" | ", 1)
            normalized_key = build_normalized_compare_key(
                item_type=left.strip(),
                skin_name=right.strip(),
                wear=wear_en,
                special_variant=special,
            ) or raw_name
        items.append(
            {
                "raw_name": raw_name,
                "cn_name": raw_name,
                "en_name": raw_name,
                "roll_market_name": clean_name or raw_name,
                "youpin_search_name": clean_name or raw_name,
                "wear_cn": None,
                "wear_en": wear_en,
                "wear_abbr": None,
                "special_variant": normalize_special_variant(special),
                "normalized_compare_key": normalized_key,
                "enabled": True,
            }
        )
    return items


def _build_watchlist_items_from_payload(items: list[dict]) -> list[dict]:
    out: list[dict] = []
    for item in items:
        raw_name = str(item.get("raw_name") or "").strip()
        cn_name = str(item.get("cn_name") or raw_name).strip() or raw_name
        en_name = str(item.get("en_name") or raw_name).strip() or raw_name
        item_type = str(item.get("item_type") or "").strip()
        skin_name = str(item.get("skin_name") or "").strip()
        wear_en = normalize_wear(item.get("wear_en") or item.get("wear_cn") or item.get("wear_abbr"))
        special = normalize_special_variant(item.get("special_variant"))
        normalized_key = str(item.get("normalized_compare_key") or "").strip()
        if not normalized_key:
            normalized_key = build_normalized_compare_key(
                item_type=item_type or en_name.split("|")[0].strip(),
                skin_name=skin_name or ("|".join(en_name.split("|")[1:]).strip() if "|" in en_name else en_name),
                wear=wear_en,
                special_variant=special,
                stattrak=bool(item.get("stattrak", False)),
                souvenir=bool(item.get("souvenir", False)),
            )
        if not normalized_key:
            continue
        out.append(
            {
                "raw_name": raw_name or cn_name or en_name or normalized_key,
                "cn_name": cn_name or None,
                "en_name": en_name or None,
                "roll_market_name": str(item.get("roll_market_name") or en_name or cn_name or normalized_key).strip(),
                "youpin_search_name": str(item.get("youpin_search_name") or cn_name or en_name or normalized_key).strip(),
                "wear_cn": str(item.get("wear_cn") or "").strip() or None,
                "wear_en": wear_en,
                "wear_abbr": str(item.get("wear_abbr") or "").strip() or None,
                "special_variant": special,
                "normalized_compare_key": normalized_key,
                "enabled": bool(item.get("enabled", True)),
            }
        )
    return out


@app.post("/api/watchlist/import")
async def watchlist_import(payload: WatchlistImportPayload = Body(default=WatchlistImportPayload())) -> dict:
    watchlist_id = payload.watchlist_id.strip() or "default"
    file_path = Path(payload.path).resolve() if payload.path else (settings.market_watchlist_file or settings.mapper_docx_file)
    rows = _build_watchlist_items_from_path(file_path)
    count = repo.replace_watchlist_items(watchlist_id=watchlist_id, items=rows, now_ts=now_ts())
    repo.set_state("watchlist_active_id", watchlist_id)
    return {"ok": True, "watchlist_id": watchlist_id, "count": count, "path": str(file_path) if file_path else None}


@app.get("/api/watchlist/items")
async def watchlist_items(watchlist_id: str = Query("default")) -> dict:
    rows = repo.list_watchlist_items(watchlist_id=watchlist_id, enabled_only=False)
    return {"watchlist_id": watchlist_id, "count": len(rows), "rows": rows}


@app.post("/api/watchlist/clear-results")
async def watchlist_clear_results(watchlist_id: str = Query("default")) -> dict:
    deleted = repo.clear_watchlist_results(watchlist_id=watchlist_id)
    return {"ok": True, "watchlist_id": watchlist_id, "deleted": deleted}


@app.post("/api/control-deck/actions/create-batch")
async def control_deck_create_batch_action(watchlist_id: str = Query("default")) -> dict:
    batch_id = repo.create_batch_id()
    repo.set_state("watchlist_active_batch_id", batch_id)
    repo.set_state("watchlist_active_id", watchlist_id)
    return {"ok": True, "watchlist_id": watchlist_id, "batch_id": batch_id}


@app.post("/api/control-deck/actions/roll-watchlist-refresh")
async def control_deck_roll_watchlist_refresh(watchlist_id: str = Query("default")) -> dict:
    batch_id = repo.create_batch_id()
    repo.set_state("watchlist_active_batch_id", batch_id)
    repo.set_state("watchlist_active_id", watchlist_id)
    roll_run = await _call_service(service="roll", method="POST", path="/run-once")
    return {"ok": bool(roll_run.get("ok")), "watchlist_id": watchlist_id, "batch_id": batch_id, "results": [roll_run]}


@app.post("/api/control-deck/actions/youpin-watchlist-fill")
async def control_deck_youpin_watchlist_fill(watchlist_id: str = Query("default")) -> dict:
    batch_id = repo.get_state("watchlist_active_batch_id") or repo.create_batch_id()
    repo.set_state("watchlist_active_batch_id", str(batch_id))
    repo.set_state("watchlist_active_id", watchlist_id)
    yp_run = await _call_service(service="youpin", method="POST", path="/run-once")
    return {"ok": bool(yp_run.get("ok")), "watchlist_id": watchlist_id, "batch_id": str(batch_id), "results": [yp_run]}


@app.get("/api/watchlist-compare")
async def watchlist_compare(
    watchlist_id: str = Query("default"),
    batch_id: str | None = Query(None),
    coin_rate: float | None = Query(None),
    keyword: str = Query(""),
    item_type: str = Query(""),
    wear: str = Query(""),
    special_variant: str = Query(""),
    status: str = Query(""),
    min_roll_coin_price: float | None = Query(None),
    max_roll_coin_price: float | None = Query(None),
    min_youpin_price: float | None = Query(None),
    max_youpin_price: float | None = Query(None),
    min_profit: float | None = Query(None),
    min_profit_rate: float | None = Query(None),
    only_profitable: bool = Query(False),
    sort_by: str = Query("roll_rmb_price"),
    sort_order: str = Query("desc"),
    secondary_sort_by: str = Query("profit_rate"),
    secondary_sort_order: str = Query("desc"),
) -> dict:
    effective_coin_rate = float(coin_rate) if coin_rate and coin_rate > 0 else float(settings.roll_to_youpin_rate)
    rows = repo.query_watchlist_compare(
        watchlist_id=watchlist_id,
        batch_id=batch_id,
        coin_rate=effective_coin_rate,
        keyword=keyword,
        item_type=item_type,
        wear=wear,
        special_variant=special_variant,
        status=status,
        min_roll_coin_price=min_roll_coin_price,
        max_roll_coin_price=max_roll_coin_price,
        min_youpin_price=min_youpin_price,
        max_youpin_price=max_youpin_price,
        min_profit=min_profit,
        min_profit_rate=min_profit_rate,
        only_profitable=only_profitable,
        sort_by=sort_by,
        sort_order=sort_order,
        secondary_sort_by=secondary_sort_by,
        secondary_sort_order=secondary_sort_order,
    )
    return {
        "watchlist_id": watchlist_id,
        "batch_id": batch_id,
        "coin_rate": effective_coin_rate,
        "count": len(rows),
        "rows": rows,
    }


@app.post("/api/refresh/trigger")
async def trigger_refresh() -> dict:
    raise HTTPException(status_code=410, detail=YOUPIN_PUBLIC_ONLY_REASON)


@app.post("/api/full-scan/trigger")
async def trigger_full_scan() -> dict:
    raise HTTPException(status_code=410, detail=YOUPIN_PUBLIC_ONLY_REASON)


@app.post("/api/youpin/fill-all/trigger")
async def trigger_youpin_fill_all() -> dict:
    raise HTTPException(status_code=410, detail=YOUPIN_PUBLIC_ONLY_REASON)
