const state = {
  dashboard: null,
};

function fmtTs(ts) {
  const n = Number(ts);
  if (!Number.isFinite(n) || n <= 0) return "--";
  return new Date(n * 1000).toLocaleString();
}

async function fetchJson(url, options = {}) {
  const response = await fetch(url, { cache: "no-store", ...options });
  if (!response.ok) {
    throw new Error(`${url} ${response.status}`);
  }
  return response.json();
}

function renderDashboard(data) {
  state.dashboard = data;
  document.getElementById("loopState").textContent = data.running_loop ? "运行中" : "已停止";
  document.getElementById("loopMeta").textContent = `服务时间 ${fmtTs(data.server_time)}`;
  document.getElementById("lastRunAt").textContent = fmtTs(data.last_run?.updated_at);
  document.getElementById("lastRunMessage").textContent = data.last_run?.message || "最近一次执行完成";
  document.getElementById("rollTrades").textContent = String(data.roll_trades ?? "--");
  document.getElementById("productCatalog").textContent = String(data.product_catalog ?? "--");
  document.getElementById("catalogMeta").textContent = `活跃 ${data.active_product_catalog ?? "--"}`;
  document.getElementById("pageSize").textContent = String(data.roll_page_size ?? "--");
  document.getElementById("pagesPerLoop").textContent = String(data.roll_pages_per_loop ?? "--");
  document.getElementById("intervalSeconds").textContent = `${Number(data.interval_seconds || 0).toFixed(1)}s`;
  document.getElementById("ttlSeconds").textContent = `${data.roll_trade_ttl_seconds ?? "--"}s`;
  document.getElementById("resultBox").textContent = JSON.stringify(data.last_run || {}, null, 2);
}

async function refreshDashboard() {
  const data = await fetchJson("/api/dashboard");
  renderDashboard(data);
}

async function invoke(url) {
  await fetchJson(url, { method: "POST" });
  await refreshDashboard();
}

document.addEventListener("DOMContentLoaded", () => {
  document.getElementById("refreshBtn").addEventListener("click", () => {
    refreshDashboard().catch((error) => {
      document.getElementById("resultBox").textContent = String(error.message || error);
    });
  });
  document.getElementById("runOnceBtn").addEventListener("click", () => {
    invoke("/run-once").catch((error) => {
      document.getElementById("resultBox").textContent = String(error.message || error);
    });
  });
  document.getElementById("startLoopBtn").addEventListener("click", () => {
    invoke("/loop/start").catch((error) => {
      document.getElementById("resultBox").textContent = String(error.message || error);
    });
  });
  document.getElementById("stopLoopBtn").addEventListener("click", () => {
    invoke("/loop/stop").catch((error) => {
      document.getElementById("resultBox").textContent = String(error.message || error);
    });
  });
  refreshDashboard().catch((error) => {
    document.getElementById("resultBox").textContent = String(error.message || error);
  });
  setInterval(() => {
    refreshDashboard().catch(() => {});
  }, 5000);
});

