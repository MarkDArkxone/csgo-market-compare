const state = {
  rows: [],
  runtime: null,
};

function fmtTs(ts) {
  const n = Number(ts);
  if (!Number.isFinite(n) || n <= 0) return "--";
  return new Date(n * 1000).toLocaleString();
}

function fmtMoney(v) {
  const n = Number(v);
  return Number.isFinite(n) ? n.toFixed(2) : "--";
}

function fmtRate(v) {
  const n = Number(v);
  return Number.isFinite(n) ? `${n.toFixed(2)}%` : "--";
}

function secondsAgo(ts) {
  const n = Number(ts);
  if (!Number.isFinite(n) || n <= 0) return "--";
  return `${Math.max(0, Math.floor(Date.now() / 1000 - n))}秒前`;
}

async function fetchJson(url, options = {}) {
  const resp = await fetch(url, { cache: "no-store", ...options });
  if (!resp.ok) throw new Error(`${url} ${resp.status}`);
  return resp.json();
}

function setText(id, value) {
  const el = document.getElementById(id);
  if (el) el.textContent = value;
}

function renderRuntime(data) {
  state.runtime = data;
  state.rows = Array.isArray(data.rows) ? data.rows : [];
  setText("serverTime", fmtTs(data.server_time));
  setText("monitorStatus", data.monitor_running ? "RUNNING" : "STOPPED");
  setText("lastBuildAgo", secondsAgo(data.state?.last_build_at));
  setText("rollRate", String(data.state?.roll_rate ?? "--"));
  setText("rollService", data.roll_service?.ok ? "正常" : "未连接");
  setText("youpinService", data.youpin_service?.ok ? "正常" : "未连接");
  setText("servingWarehouse", `${data.state?.serving || 0} / ${data.state?.comparisons || 0}`);
  const fresh = Number(data.state?.fresh_pairs || 0);
  const live = Number(data.state?.live_verified || 0);
  const total = Math.max(1, Number(data.state?.serving || 0));
  setText("freshLive", `${((fresh / total) * 100).toFixed(1)}% / ${((live / total) * 100).toFixed(1)}%`);
  renderRows();
}

function renderRows() {
  const tbody = document.getElementById("resultsBody");
  if (!tbody) return;
  const search = (document.getElementById("searchInput")?.value || "").trim().toLowerCase();
  const profitOnly = Boolean(document.getElementById("profitOnly")?.checked);
  const priceOnly = Boolean(document.getElementById("priceOnly")?.checked);
  let rows = state.rows.slice();
  rows = rows.filter((row) => {
    const name = String(row.display_name_final || row.market_name || "").toLowerCase();
    if (search && !name.includes(search)) return false;
    if (profitOnly && !(Number(row.net_profit_rate) > 0)) return false;
    if (priceOnly && !(Number(row.youpin_sell_price) > 0)) return false;
    return true;
  });
  setText("resultsMeta", `当前展示 ${rows.length} 条`);
  if (!rows.length) {
    tbody.innerHTML = '<tr><td colspan="5">没有符合条件的数据</td></tr>';
    return;
  }
  tbody.innerHTML = rows.slice(0, 300).map((row) => `
    <tr>
      <td>${row.display_name_final || row.market_name || "--"}</td>
      <td>${fmtMoney(row.roll_converted_cost)}</td>
      <td>${fmtMoney(row.youpin_sell_price)}</td>
      <td>${fmtRate(row.net_profit_rate)}</td>
      <td>${row.verify_status || "--"}</td>
    </tr>
  `).join("");
}

async function loadRuntime() {
  const data = await fetchJson("/api/control-deck/runtime");
  renderRuntime(data);
}

async function invoke(action, id) {
  const data = await fetchJson(`/api/control-deck/actions/${action}`, { method: "POST" });
  const out = document.getElementById("receiptBox");
  if (out) out.textContent = JSON.stringify(data, null, 2);
  if (id === "status") window.open("/status", "_blank", "noopener");
  await loadRuntime();
}

document.addEventListener("DOMContentLoaded", () => {
  document.title = "ROLL / YOUPIN CONTROL DECK";
  document.getElementById("btnRefresh")?.addEventListener("click", () => invoke("refresh"));
  document.getElementById("btnStartLoops")?.addEventListener("click", () => invoke("start-loops"));
  document.getElementById("btnYoupinFill")?.addEventListener("click", () => invoke("youpin-fill"));
  document.getElementById("btnFullScan")?.addEventListener("click", () => invoke("full-scan"));
  document.getElementById("btnStatus")?.addEventListener("click", () => window.open("/status", "_blank", "noopener"));
  document.getElementById("btnApply")?.addEventListener("click", renderRows);
  loadRuntime().catch((e) => setText("resultsMeta", String(e.message || e)));
  setInterval(() => { loadRuntime().catch(() => {}); }, 1000);
});
