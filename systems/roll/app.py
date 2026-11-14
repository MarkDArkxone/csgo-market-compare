from __future__ import annotations

import asyncio
import logging

from fastapi import FastAPI

from config.logging_config import setup_logging
from config.settings import Settings
from systems.roll.data_sources.csgoroll_source import CSGORollSource
from shared.repository import Repository
from systems.roll.services.collector import RollCollector
from shared.utils import now_ts

settings = Settings.load()
setup_logging(settings.log_path)
logger = logging.getLogger(__name__)

repo = Repository(settings.db_path)
roll_source = CSGORollSource(settings)
collector = RollCollector(settings, repo, roll_source)

app = FastAPI(title="Roll Collector Service", version="0.1.0")

_loop_task: asyncio.Task | None = None
_loop_stop_event = asyncio.Event()
_last_run: dict = {"ok": True, "message": "never started", "updated_at": 0}


@app.get("/")
async def index() -> dict:
    return {"ok": True, "service": "roll_collector", "ui": "disabled", "server_time": now_ts()}


@app.get("/health")
async def health() -> dict:
    return {
        "ok": True,
        "service": "roll_collector",
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
            logger.exception("roll collector loop error")
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
        _run_loop(max(1.0, float(settings.roll_base_interval_seconds))),
        name="roll-collector-loop",
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
    return {
        "service": "roll_collector",
        "running_loop": bool(_loop_task and not _loop_task.done()),
        "last_run": _last_run,
        "interval_seconds": max(1.0, float(settings.roll_base_interval_seconds)),
        "roll_page_size": int(settings.roll_page_size),
        "roll_pages_per_loop": int(settings.roll_pages_per_loop),
        "roll_trade_ttl_seconds": int(settings.roll_trade_ttl_seconds),
        "roll_trades": int(stats.get("roll_trades", 0) or 0),
        "product_catalog": int(stats.get("product_catalog", 0) or 0),
        "active_product_catalog": int(stats.get("active_product_catalog", 0) or 0),
        "last_comparison_update": int(stats.get("last_comparison_update", 0) or 0),
        "server_time": now_ts(),
    }
