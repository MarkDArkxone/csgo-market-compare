from __future__ import annotations

import os
import time
from typing import Any

import httpx


SORT_CHOICES = {
    "1": ("roll_converted_cost", "Roll Cost"),
    "2": ("net_profit_rate", "Profit Rate"),
    "3": ("youpin_sell_price", "Youpin Price"),
}


def _base_url() -> str:
    return str(os.getenv("COMPARE_TUI_BASE_URL", "http://127.0.0.1:8000")).rstrip("/")


def _safe_float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except Exception:
        return 0.0


def _safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except Exception:
        return 0


def _fmt_ts(ts: int) -> str:
    if ts <= 0:
        return "--"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


def _clear() -> None:
    os.system("cls" if os.name == "nt" else "clear")


def _sort_rows(rows: list[dict[str, Any]], sort_key: str, desc: bool) -> list[dict[str, Any]]:
    def key_fn(row: dict[str, Any]) -> float:
        return _safe_float(row.get(sort_key))

    return sorted(rows, key=key_fn, reverse=desc)


def _format_runtime_lines(
    runtime: dict[str, Any],
    receipt_text: str,
    help_text: str,
    *,
    page: int,
    page_size: int,
    sort_choice: str,
    sort_desc: bool,
) -> list[str]:
    state = runtime.get("state", {}) if isinstance(runtime, dict) else {}
    rows = runtime.get("rows", []) if isinstance(runtime, dict) else []
    roll_service = runtime.get("roll_service", {}) if isinstance(runtime, dict) else {}
    youpin_service = runtime.get("youpin_service", {}) if isinstance(runtime, dict) else {}
    serving = _safe_int(state.get("serving"))
    fresh = _safe_int(state.get("fresh_pairs"))
    live = _safe_int(state.get("live_verified"))
    fresh_pct = (fresh / max(1, serving)) * 100.0
    live_pct = (live / max(1, serving)) * 100.0

    sort_key, sort_label = SORT_CHOICES.get(sort_choice, SORT_CHOICES["1"])
    ordered_rows = _sort_rows(list(rows), sort_key, sort_desc)
    total_rows = len(ordered_rows)
    total_pages = max(1, (total_rows + page_size - 1) // page_size)
    safe_page = min(max(1, page), total_pages)
    start = (safe_page - 1) * page_size
    end = min(total_rows, start + page_size)
    page_rows = ordered_rows[start:end]

    lines: list[str] = []
    lines.append("ROLL / YOUPIN CONTROL DECK (Terminal)")
    lines.append("CSGORoll vs Youpin898")
    lines.append("=" * 108)
    lines.append(
        "Server Time: "
        f"{_fmt_ts(_safe_int(runtime.get('server_time')))} | "
        f"Monitor: {'RUNNING' if runtime.get('monitor_running') else 'STOPPED'}"
    )
    lines.append(
        "Refresh: results 1s / aux 15s | "
        f"Roll Service: {'OK' if roll_service.get('ok') else 'DOWN'} | "
        f"Youpin Service: {'OK' if youpin_service.get('ok') else 'DOWN'}"
    )
    lines.append(
        "Serving / Warehouse: "
        f"{serving} / {_safe_int(state.get('comparisons'))} | "
        f"Fresh {fresh_pct:.1f}% | Live {live_pct:.1f}%"
    )
    lines.append(
        "Roll Rate: "
        f"{_safe_float(state.get('roll_rate')):.4f} "
        f"({state.get('roll_rate_source') or 'unknown'})"
    )
    lines.append(
        f"Last Build: {_fmt_ts(_safe_int(state.get('last_build_at')))} | "
        f"Last Verify: {_fmt_ts(_safe_int(state.get('last_verify_at')))}"
    )
    lines.append("-" * 108)
    lines.append(
        f"Results Page {safe_page}/{total_pages} | rows {start + 1}-{max(start + len(page_rows), start)} / {total_rows} | "
        f"Sort: {sort_label} {'DESC' if sort_desc else 'ASC'}"
    )
    lines.append("name                     | roll_cost | youpin_price | profit_rate | status")
    for row in page_rows:
        name = str(row.get("display_name_final") or row.get("market_name") or "--")
        roll_cost = _safe_float(row.get("roll_converted_cost"))
        yp_price = _safe_float(row.get("youpin_sell_price"))
        rate = _safe_float(row.get("net_profit_rate"))
        status = str(row.get("verify_status") or "--")
        lines.append(f"{name[:24]:24} | {roll_cost:8.2f} | {yp_price:11.2f} | {rate:8.2f}% | {status}")
    lines.append("-" * 108)
    lines.append(f"Receipt: {receipt_text}")
    lines.append(f"Keys: {help_text}")
    return lines


def _post_action(client: httpx.Client, action: str) -> dict[str, Any]:
    resp = client.post(f"/api/control-deck/actions/{action}")
    resp.raise_for_status()
    return resp.json()


def _poll_key_nonblocking() -> str:
    if os.name == "nt":
        try:
            import msvcrt  # type: ignore

            if msvcrt.kbhit():
                key = msvcrt.getwch()
                if key:
                    return key.lower()
        except Exception:
            return ""
        return ""
    return ""


def run_tui() -> None:
    base_url = _base_url()
    receipt_text = "waiting for action..."
    help_text = "r refresh | l loops | y youpin-fill | f full-scan | n/p page | 1/2/3 sort | o order | s status | q quit"
    page = 1
    page_size = 12
    sort_choice = "1"
    sort_desc = True
    with httpx.Client(base_url=base_url, timeout=6.0) as client:
        while True:
            try:
                runtime_resp = client.get("/api/control-deck/runtime", params={"limit": 1000})
                runtime_resp.raise_for_status()
                runtime = runtime_resp.json()
            except Exception as exc:
                _clear()
                print("Cannot connect compare service.")
                print(f"BASE_URL={base_url}")
                print(f"Error: {exc}")
                print("Start compare service first. Press Enter to retry, or q to quit.")
                cmd = input("> ").strip().lower()
                if cmd == "q":
                    return
                continue

            _clear()
            for line in _format_runtime_lines(
                runtime,
                receipt_text,
                help_text,
                page=page,
                page_size=page_size,
                sort_choice=sort_choice,
                sort_desc=sort_desc,
            ):
                print(line)

            deadline = time.time() + 1.0
            handled = False
            while time.time() < deadline:
                key = _poll_key_nonblocking()
                if not key:
                    time.sleep(0.05)
                    continue
                handled = True
                if key == "q":
                    return
                if key == "s":
                    try:
                        os.startfile(f"{base_url}/status")  # type: ignore[attr-defined]
                        receipt_text = "opened status window"
                    except Exception as exc:
                        receipt_text = f"open status failed: {exc}"
                    break
                if key in {"n", "p", "o", "1", "2", "3"}:
                    if key == "n":
                        page += 1
                    elif key == "p":
                        page = max(1, page - 1)
                    elif key == "o":
                        sort_desc = not sort_desc
                    else:
                        sort_choice = key
                        page = 1
                    receipt_text = f"view updated: page={page}, sort={SORT_CHOICES[sort_choice][1]}, desc={sort_desc}"
                    break
                if key in {"r", "l", "y", "f"}:
                    mapping = {
                        "r": "refresh",
                        "l": "start-loops",
                        "y": "youpin-fill",
                        "f": "full-scan",
                    }
                    action = mapping[key]
                    try:
                        receipt = _post_action(client, action)
                        receipt_text = f"action={receipt.get('action')} ok={receipt.get('ok')}"
                    except Exception as exc:
                        receipt_text = f"action={action} failed: {exc}"
                    break
                receipt_text = f"unknown key: {key}"
                break

            if os.name != "nt" and not handled:
                cmd = input("> ").strip().lower()
                if cmd == "q":
                    return
                if cmd == "s":
                    receipt_text = "non-windows: open /status manually"
                elif cmd in {"n", "p", "o", "1", "2", "3"}:
                    if cmd == "n":
                        page += 1
                    elif cmd == "p":
                        page = max(1, page - 1)
                    elif cmd == "o":
                        sort_desc = not sort_desc
                    else:
                        sort_choice = cmd
                        page = 1
                    receipt_text = f"view updated: page={page}, sort={SORT_CHOICES[sort_choice][1]}, desc={sort_desc}"
                elif cmd in {"r", "l", "y", "f"}:
                    mapping = {"r": "refresh", "l": "start-loops", "y": "youpin-fill", "f": "full-scan"}
                    action = mapping[cmd]
                    try:
                        receipt = _post_action(client, action)
                        receipt_text = f"action={receipt.get('action')} ok={receipt.get('ok')}"
                    except Exception as exc:
                        receipt_text = f"action={action} failed: {exc}"


if __name__ == "__main__":
    run_tui()
