function fmtTs(ts) {
  const n = Number(ts);
  if (!Number.isFinite(n) || n <= 0) return "--";
  return new Date(n * 1000).toLocaleString();
}

function fmtNum(v) {
  const n = Number(v);
  if (!Number.isFinite(n)) return "--";
  return String(n);
}

function row(label, value) {
  return `<tr><td>${label}</td><td>${value}</td></tr>`;
}

async function loadStatus() {
  const res = await fetch("/api/system-status", { cache: "no-store" });
  if (!res.ok) throw new Error(`/api/system-status ${res.status}`);
  return res.json();
}

function renderCards(data) {
  const monitorOk = Boolean(data.monitor_running);
  document.getElementById("cMonitor").innerHTML = monitorOk ? '<span class="ok">RUNNING</span>' : '<span class="bad">STOPPED</span>';
  document.getElementById("cServing").textContent = fmtNum(data.serving_count);
  document.getElementById("cFresh").textContent = fmtNum(data.fresh_pair_count);
  document.getElementById("cLive").textContent = fmtNum(data.verified_live_count);
  document.getElementById("cFx").textContent = `${data.exchange_rate_source || "--"} / ${fmtNum(data.exchange_rate_age_seconds)}s`;
  const session = data.youpin_session || {};
  const sessionText = session.mode === "public_only" ? "PUBLIC ONLY" : "SESSION";
  document.getElementById("cError").innerHTML = data.last_error ? `<span class="bad">${String(data.last_error)}</span>` : `<span class="ok">${sessionText}</span>`;
  document.getElementById("timeHint").textContent = `服务器时间: ${fmtTs(data.server_time)} | 2秒自动刷新`;
}

function renderTable(data) {
  const youpinSession = data.youpin_session || {};
  const html = [
    row("server_time", `${fmtTs(data.server_time)} (${data.server_time || "--"})`),
    row("roll_refresh_mode", String(data.roll_refresh_mode || "--")),
    row("roll_daily_refresh_schedule", `${fmtNum(data.roll_daily_refresh_hour)}:${fmtNum(data.roll_daily_refresh_minute)}`),
    row("roll_last_daily_refresh_at", fmtTs(data.roll_last_daily_refresh_at)),
    row("roll_daily_snapshot_rows", fmtNum(data.roll_daily_snapshot_rows)),
    row("roll_daily_snapshot_at", fmtTs(data.roll_daily_snapshot_at)),
    row("full_scan_active", String(Boolean(data.full_scan_active))),
    row("full_scan_requested_at", fmtTs(data.full_scan_requested_at)),
    row("full_scan_started_at", fmtTs(data.full_scan_started_at)),
    row("full_scan_completed_at", fmtTs(data.full_scan_completed_at)),
    row("last_monitor_cycle_at", fmtTs(data.last_monitor_cycle_at)),
    row("last_serving_build_at", fmtTs(data.last_serving_build_at)),
    row("last_live_verify_at", fmtTs(data.last_live_verify_at)),
    row("warehouse_count", fmtNum(data.warehouse_count)),
    row("serving_count", fmtNum(data.serving_count)),
    row("fresh_pair_count", fmtNum(data.fresh_pair_count)),
    row("verified_live_count", fmtNum(data.verified_live_count)),
    row("stale_count", fmtNum(data.stale_count)),
    row("null_price_count", fmtNum(data.null_price_count)),
    row("anomaly_count", fmtNum(data.anomaly_count)),
    row("roll_ok / youpin_ok", `${data.roll_ok} / ${data.youpin_ok}`),
    row("exchange_rate_source", String(data.exchange_rate_source || "--")),
    row("exchange_rate_age_seconds", fmtNum(data.exchange_rate_age_seconds)),
    row("youpin_session.mode", String(youpinSession.mode || "--")),
    row("youpin_session.source", String(youpinSession.source || "--")),
    row("youpin_session.disabled_reason", String(youpinSession.disabled_reason || "--")),
    row("special_detail_backlog", fmtNum(data.special_detail_backlog)),
    row("special_detail_confirmed", fmtNum(data.special_detail_confirmed)),
    row("special_detail_candidate_only", fmtNum(data.special_detail_candidate_only)),
    row("special_detail_failed", fmtNum(data.special_detail_failed)),
    row("verify_queue_size", fmtNum(data.verify_queue_size)),
    row("verify_queue_trend", String(data.verify_queue_trend || "--")),
    row("youpin_429_rate", fmtNum(data.youpin_429_rate)),
    row("refresh_queue_size", fmtNum(data.refresh_queue_size)),
    row("slowdown", `roll=${data?.slowdown?.roll ?? "--"} / youpin=${data?.slowdown?.youpin ?? "--"} / max=${data?.slowdown?.max ?? "--"}`),
    row("last_error", String(data.last_error || "--")),
  ].join("");
  document.getElementById("statusTable").innerHTML = html;
}

function renderLogs(data) {
  const rows = Array.isArray(data.recent_logs) ? data.recent_logs : [];
  const html = rows
    .slice(-50)
    .map((x) => `<div class="log-row">[${fmtTs(x.ts)}] ${x.event || "event"} - ${x.message || ""}</div>`)
    .join("");
  const box = document.getElementById("logsBox");
  box.innerHTML = html || '<div class="log-row">暂无日志</div>';
  box.scrollTop = box.scrollHeight;
}

async function refreshStatus() {
  try {
    const data = await loadStatus();
    renderCards(data);
    renderTable(data);
    renderLogs(data);
  } catch (err) {
    document.getElementById("timeHint").textContent = `状态加载失败: ${err.message}`;
  }
}

document.addEventListener("DOMContentLoaded", () => {
  document.getElementById("refreshNowBtn").addEventListener("click", () => {
    refreshStatus();
  });
  refreshStatus();
  setInterval(refreshStatus, 2000);
});
