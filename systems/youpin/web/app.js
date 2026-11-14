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
  document.getElementById("loopState").textContent = data.running_loop ? "运行中" : "已停止";
  document.getElementById("loopMeta").textContent = `服务时间 ${fmtTs(data.server_time)}`;
  document.getElementById("watchlistTemplateIds").textContent = String(data.watchlist_template_id_count ?? "--");
  document.getElementById("watchlistNames").textContent = `名称池 ${data.watchlist_name_count ?? "--"}`;
  document.getElementById("youpinPrices").textContent = String(data.youpin_prices ?? "--");
  document.getElementById("lastRunAt").textContent = fmtTs(data.last_run?.updated_at);
  document.getElementById("lastRunMessage").textContent = data.last_run?.message || "最近一次执行完成";
  document.getElementById("browserContextEnabled").textContent = data.browser_context?.enabled ? "已启用" : "未启用";
  document.getElementById("browserContextConnected").textContent = data.browser_context?.connected ? "已连接" : "未连接";
  document.getElementById("specialRuntimeActive").textContent = data.special_runtime?.active ? "已激活" : "未激活";
  document.getElementById("requestDelay").textContent = `${Number(data.per_request_delay_seconds || 0).toFixed(2)}s`;
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
