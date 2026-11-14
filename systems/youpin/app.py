from __future__ import annotations

import asyncio
import logging

from fastapi import FastAPI

from config.logging_config import setup_logging
from config.settings import Settings
from systems.youpin.data_sources.youpin_source import YoupinSource
from shared.market_watchlist import load_market_watch_targets
from shared.name_mapper import NameMapper
from shared.repository import Repository
from systems.youpin.services.browser_context import YoupinBrowserContextClient
from systems.youpin.services.collector import YoupinCollector
from shared.utils import now_ts

settings = Settings.load()
setup_logging(settings.log_path)
logger = logging.getLogger(__name__)

repo = Repository(settings.db_path)
mapper = NameMapper(settings)
youpin_browser_context = YoupinBrowserContextClient(settings)
youpin_source = YoupinSource(
    settings,
    browser_context_requester=youpin_browser_context,
)
collector = YoupinCollector(settings, repo, mapper, youpin_source)

app = FastAPI(title="Youpin Collector Service", version="0.1.0")

_loop_task: asyncio.Task | None = None
_loop_stop_event = asyncio.Event()
_last_run: dict = {"ok": True, "message": "never started", "updated_at": 0}


@app.get("/")
async def index() -> dict:
    return {"ok": True, "service": "youpin_collector", "ui": "disabled", "server_time": now_ts()}


@app.get("/health")
async def health() -> dict:
    return {
        "ok": True,
        "service": "youpin_collector",
        "running_loop": bool(_loop_task and not _loop_task.done()),
        "last_run": _last_run,
        "server_time": now_ts(),
    }


@app.post("/run-once")
async def run_once() -> dict:
    global _last_run
    result = await collector.run_once()
    _last_run = {**result, "updated_at": now_ts()}
    return _last_run


async def _run_loop(interval_seconds: float) -> None:
    global _last_run
    while not _loop_stop_event.is_set():
        try:
            result = await collector.run_once()
            _last_run = {**result, "updated_at": now_ts()}
        except Exception as exc:  # noqa: BLE001
            _last_run = {
                "ok": False,
                "message": str(exc),
                "updated_at": now_ts(),
            }
            logger.exception("youpin collector loop error")
        try:
            await asyncio.wait_for(_loop_stop_event.wait(), timeout=interval_seconds)
        except asyncio.TimeoutError:
            continue


@app.post("/loop/start")
async def start_loop() -> dict:
    global _loop_task
    if _loop_task and not _loop_task.done():
        return {"ok": True, "message": "already running"}
    _loop_stop_event.clear()
    _loop_task = asyncio.create_task(
        _run_loop(max(1.0, float(settings.youpin_cycle_time_budget_seconds))),
        name="youpin-collector-loop",
    )
    return {"ok": True, "message": "started"}


@app.post("/loop/stop")
async def stop_loop() -> dict:
    global _loop_task
    _loop_stop_event.set()
    if _loop_task and not _loop_task.done():
        await asyncio.wait_for(_loop_task, timeout=5.0)
    _loop_task = None
    return {"ok": True, "message": "stopped"}


@app.get("/api/dashboard")
async def dashboard() -> dict:
    stats = repo.stats()
    watch_targets = await asyncio.to_thread(load_market_watch_targets, settings.market_watchlist_file)
    browser_context_status = youpin_browser_context.status()
    special_runtime_status = youpin_source.special_item_runtime_status()
    return {
        "service": "youpin_collector",
        "running_loop": bool(_loop_task and not _loop_task.done()),
        "last_run": _last_run,
        "interval_seconds": max(1.0, float(settings.youpin_cycle_time_budget_seconds)),
        "per_request_delay_seconds": float(settings.youpin_per_request_delay_seconds),
        "watchlist_template_id_count": len(watch_targets.template_ids),
        "watchlist_name_count": len(watch_targets.names),
        "youpin_prices": int(stats.get("youpin_prices", 0) or 0),
        "last_comparison_update": int(stats.get("last_comparison_update", 0) or 0),
        "browser_context": browser_context_status,
        "special_runtime": special_runtime_status,
        "server_time": now_ts(),
    }
