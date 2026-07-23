"use strict";

const REFRESH_MS = (window.APP_CONFIG.refreshSeconds || 10) * 1000;
const MAX_POINTS = 30;

// Selected LPAR (when the selector is present); empty = server default.
const lparSelect = document.getElementById("lpar-select");
let selectedLpar = lparSelect ? lparSelect.value : "";

function lparQuery() {
  return selectedLpar ? `?lpar=${encodeURIComponent(selectedLpar)}` : "";
}

// ----- CPU line chart -----
const cpuCtx = document.getElementById("cpu-chart").getContext("2d");
const cpuChart = new Chart(cpuCtx, {
  type: "line",
  data: {
    labels: [],
    datasets: [{
      label: "CPU %",
      data: [],
      borderColor: "#38bdf8",
      backgroundColor: "rgba(56,189,248,0.15)",
      fill: true,
      tension: 0.3,
      pointRadius: 0,
    }],
  },
  options: {
    animation: false,
    scales: {
      y: { min: 0, max: 100, ticks: { color: "#94a3b8" }, grid: { color: "#334155" } },
      x: { ticks: { color: "#94a3b8", maxTicksLimit: 6 }, grid: { display: false } },
    },
    plugins: { legend: { display: false } },
  },
});

function pushCpuPoint(value) {
  const now = new Date().toLocaleTimeString();
  cpuChart.data.labels.push(now);
  cpuChart.data.datasets[0].data.push(value);
  if (cpuChart.data.labels.length > MAX_POINTS) {
    cpuChart.data.labels.shift();
    cpuChart.data.datasets[0].data.shift();
  }
  cpuChart.update();
}

// ----- Total-jobs doughnut (share of the configured maximum) -----
const MAX_JOBS = 600000;
const jobsCtx = document.getElementById("jobs-chart").getContext("2d");
const jobsChart = new Chart(jobsCtx, {
  type: "doughnut",
  data: {
    labels: ["Total jobs", "Headroom"],
    datasets: [{
      data: [0, MAX_JOBS],
      backgroundColor: ["#38bdf8", "#334155"],
      borderColor: "#1e293b",
      borderWidth: 2,
    }],
  },
  options: {
    responsive: true,
    maintainAspectRatio: false,
    cutout: "70%",
    animation: { duration: 400 },
    plugins: {
      legend: { display: false },
      tooltip: {
        callbacks: {
          label: (ctx) => {
            const v = ctx.parsed || 0;
            const pct = ((v / MAX_JOBS) * 100).toFixed(1);
            return `${ctx.label}: ${v.toLocaleString()} (${pct}%)`;
          },
        },
      },
    },
  },
});

function renderJobsGauge(totalJobs) {
  const total = Math.max(0, Number(totalJobs) || 0);
  const used = Math.min(total, MAX_JOBS);
  const free = Math.max(0, MAX_JOBS - used);
  const pct = (used / MAX_JOBS) * 100;

  jobsChart.data.datasets[0].data = [used, free];
  // Colour the used slice by how close we are to the maximum.
  const usedColor = pct >= 90 ? "#ef4444" : pct >= 75 ? "#f59e0b" : "#38bdf8";
  jobsChart.data.datasets[0].backgroundColor = [usedColor, "#334155"];
  jobsChart.update();

  document.getElementById("jobs-pct").textContent = pct.toFixed(1) + "%";
  document.getElementById("jobs-used").textContent = total.toLocaleString();
  document.getElementById("jobs-free").textContent = free.toLocaleString();
}

const MAX_ASP = 100;
const aspCtx = document.getElementById("asp-chart").getContext("2d");
const aspChart = new Chart(aspCtx, {
  type: "doughnut",
  data: {
    labels: ["Used", "Free"],
    datasets: [{
      data: [0, MAX_ASP],
      backgroundColor: ["#38bdf8", "#334155"],
      borderColor: "#1e293b",
      borderWidth: 2,
    }],
  },
  options: {
    responsive: true,
    maintainAspectRatio: false,
    cutout: "70%",
    animation: { duration: 400 },
    plugins: {
      legend: { display: false },
      tooltip: {
        callbacks: {
          label: (ctx) => `${ctx.label}: ${(ctx.parsed || 0).toFixed(1)}%`,
        },
      },
    },
  },
});

function renderAspGauge(aspPct) {
  const pct = Math.min(Math.max(0, Number(aspPct) || 0), MAX_ASP);
  const free = Math.max(0, MAX_ASP - pct);

  aspChart.data.datasets[0].data = [pct, free];
  // Colour the used slice by how close we are to the maximum.
  const usedColor = pct >= 90 ? "#ef4444" : pct >= 80 ? "#f59e0b" : "#38bdf8";
  aspChart.data.datasets[0].backgroundColor = [usedColor, "#334155"];
  aspChart.update();

  document.getElementById("asp-pct").textContent = pct.toFixed(1) + "%";
  document.getElementById("asp-used").textContent = pct.toFixed(1) + "%";
  document.getElementById("asp-free").textContent = free.toFixed(1) + "%";
}

// ----- Renderers -----
// KPI threshold colouring: returns "" | "warn" | "alert".
function levelFor(value, warn, alert) {
  if (value >= alert) return "alert";
  if (value >= warn) return "warn";
  return "";
}

function setKpi(id, value, level) {
  const el = document.getElementById(id);
  el.textContent = value;
  const kpi = el.closest(".kpi");
  kpi.classList.remove("warn", "alert");
  if (level) kpi.classList.add(level);
}

function renderSystem(metric) {
  const card = document.getElementById("card-system");
  if (!metric.ok) { markError(card, metric.error); return; }
  clearError(card);
  const d = metric.data;
  setKpi("kpi-cpu", d.cpu_pct, levelFor(d.cpu_pct, 75, 90));
  setKpi("kpi-active", d.active_jobs, "");
  setKpi("kpi-uptime", d.uptime, "");
  renderJobsGauge(d.total_jobs);
  renderAspGauge(d.asp_used_pct);
  pushCpuPoint(d.cpu_pct);
}

function renderTable(metric) {
  const card = document.getElementById("card-" + metric.id);
  if (!card) return;
  if (!metric.ok) { markError(card, metric.error); return; }
  clearError(card);
  const table = document.getElementById("table-" + metric.id);
  const thead = table.querySelector("thead");
  const tbody = table.querySelector("tbody");
  const levels = metric.data.levels || [];

  thead.innerHTML = "<tr>" +
    metric.data.columns.map((c) => `<th>${escapeHtml(c)}</th>`).join("") +
    "</tr>";
  tbody.innerHTML = metric.data.rows.map((row, i) => {
    const cls = levels[i] ? ` class="row-${levels[i]}"` : "";
    return `<tr${cls}>` +
      row.map((cell) => `<td>${escapeHtml(cell)}</td>`).join("") + "</tr>";
  }).join("");

  if (metric.data.rows.length === 0) {
    tbody.innerHTML =
      `<tr><td colspan="${metric.data.columns.length}" class="empty">— none —</td></tr>`;
  }

  // Alert summary in the header note.
  const alertCount = levels.filter((l) => l === "alert").length;
  const warnCount = levels.filter((l) => l === "warn").length;

  // Optional freshness/status note shown under the table.
  let note = card.querySelector(".card-note");
  let noteText = metric.data.note || "";
  if (alertCount || warnCount) {
    const parts = [];
    if (alertCount) parts.push(`${alertCount} alert`);
    if (warnCount) parts.push(`${warnCount} warn`);
    noteText = parts.join(", ") + (noteText ? " · " + noteText : "");
  }
  card.classList.toggle("has-alert", alertCount > 0);
  if (noteText) {
    if (!note) {
      note = document.createElement("div");
      note.className = "card-note";
      card.appendChild(note);
    }
    note.textContent = noteText;
  } else if (note) {
    note.remove();
  }
}

function markError(card, msg) {
  card.classList.add("error");
  let el = card.querySelector(".card-error-msg");
  if (!el) {
    el = document.createElement("div");
    el.className = "card-error-msg";
    card.appendChild(el);
  }
  el.textContent = "⚠ " + (msg || "collector error");
}

function clearError(card) {
  card.classList.remove("error");
  const el = card.querySelector(".card-error-msg");
  if (el) el.remove();
}

function escapeHtml(v) {
  return String(v)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;")
    .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

function resetCpuChart() {
  cpuChart.data.labels = [];
  cpuChart.data.datasets[0].data = [];
  cpuChart.update();
}

// ----- Polling -----
async function refresh() {
  try {
    const res = await fetch("/api/metrics" + lparQuery());
    const payload = await res.json();
    for (const metric of payload.metrics) {
      if (metric.id === "system") renderSystem(metric);
      else if (metric.kind === "table") renderTable(metric);
    }
    document.getElementById("updated").textContent =
      "updated " + new Date().toLocaleTimeString();
  } catch (err) {
    document.getElementById("updated").textContent = "fetch failed";
  }
}

async function checkHealth() {
  try {
    const res = await fetch("/api/health" + lparQuery());
    const h = await res.json();
    const el = document.getElementById("health");
    if (h.ok) {
      el.textContent = h.mode === "mock"
        ? "mock data" : "connected " + (h.name || h.host || "");
      el.className = "health ok";
    } else {
      el.textContent = "disconnected";
      el.className = "health bad";
      el.title = h.error || "";
    }
  } catch (e) {
    const el = document.getElementById("health");
    el.textContent = "unreachable";
    el.className = "health bad";
  }
}

if (lparSelect) {
  lparSelect.addEventListener("change", () => {
    selectedLpar = lparSelect.value;
    resetCpuChart();
    refresh();
    checkHealth();
  });
}

checkHealth();
refresh();
setInterval(refresh, REFRESH_MS);
setInterval(checkHealth, REFRESH_MS * 3);
