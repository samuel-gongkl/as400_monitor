"use strict";

const REFRESH_MS = (window.APP_CONFIG.refreshSeconds || 15) * 1000;

// Plugin: draw dashed horizontal threshold lines across a chart.
const thresholdLines = {
  id: "thresholdLines",
  afterDatasetsDraw(chart, args, opts) {
    const lines = (opts && opts.lines) || [];
    const { ctx, chartArea, scales } = chart;
    const y = scales.y;
    lines.forEach((l) => {
      const yPos = y.getPixelForValue(l.value);
      if (yPos < chartArea.top || yPos > chartArea.bottom) return;
      ctx.save();
      ctx.beginPath();
      ctx.setLineDash(l.dash || [6, 4]);
      ctx.lineWidth = 1;
      ctx.strokeStyle = l.color;
      ctx.moveTo(chartArea.left, yPos);
      ctx.lineTo(chartArea.right, yPos);
      ctx.stroke();
      ctx.restore();
    });
  },
};
Chart.register(thresholdLines);

// One set of Chart instances + DOM refs per LPAR, keyed by LPAR key.
const panels = {};

function fmtTime(ms) {
  const d = new Date(ms);
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function barColor(label, warn, crit) {
  const lo = parseInt(label.split("-")[0], 10);
  if (lo >= crit) return "rgba(239,68,68,0.85)";
  if (lo >= warn) return "rgba(245,158,11,0.85)";
  return "rgba(56,189,248,0.7)";
}

function escapeHtml(v) {
  return String(v)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;")
    .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

function row(label, value, cls) {
  return `<div class="srow"><span class="slabel">${label}</span>` +
         `<span class="sval ${cls || ""}">${value}</span></div>`;
}

// Build the DOM + charts for one LPAR panel and cache them.
function createPanel(lp) {
  const container = document.getElementById("lpar-panels");
  const section = document.createElement("section");
  section.className = "card lpar-panel";
  section.id = "panel-" + lp.key;
  section.innerHTML = `
    <div class="panel-head">
      <h2 class="panel-title">${escapeHtml(lp.name)}</h2>
      <span class="panel-host">${escapeHtml(lp.host || "")}</span>
      <span class="panel-status" id="status-${lp.key}"></span>
    </div>
    <div class="panel-body">
      <div class="panel-trend">
        <div class="chart-box"><canvas id="trend-${lp.key}"></canvas></div>
        <div class="legend" id="legend-${lp.key}"></div>
      </div>
      <div class="panel-side">
        <div class="chart-box small"><canvas id="hist-${lp.key}"></canvas></div>
        <div class="summary" id="summary-${lp.key}"></div>
      </div>
    </div>`;
  container.appendChild(section);

  const trendChart = new Chart(document.getElementById("trend-" + lp.key), {
    type: "line",
    data: {
      labels: [],
      datasets: [{
        label: "CPU %", data: [],
        borderColor: "#38bdf8", backgroundColor: "rgba(56,189,248,0.18)",
        fill: true, tension: 0.25, pointRadius: 0, borderWidth: 1.5,
      }],
    },
    options: {
      animation: false, maintainAspectRatio: false,
      interaction: { mode: "index", intersect: false },
      scales: {
        y: {
          min: 0, suggestedMax: 120,
          ticks: { color: "#94a3b8" }, grid: { color: "#334155" },
          title: { display: true, text: "CPU Usage (% of EC)", color: "#94a3b8" },
        },
        x: {
          ticks: { color: "#94a3b8", maxTicksLimit: 12, autoSkip: true },
          grid: { display: false },
        },
      },
      plugins: {
        legend: { display: false },
        thresholdLines: { lines: [] },
        tooltip: { callbacks: { label: (c) => ` ${c.parsed.y}% CPU` } },
      },
    },
  });

  const histChart = new Chart(document.getElementById("hist-" + lp.key), {
    type: "bar",
    data: { labels: [], datasets: [{ label: "Frequency", data: [], backgroundColor: [] }] },
    options: {
      animation: false, maintainAspectRatio: false,
      scales: {
        y: { beginAtZero: true, ticks: { color: "#94a3b8" }, grid: { color: "#334155" },
             title: { display: true, text: "Frequency", color: "#94a3b8" } },
        x: { ticks: { color: "#94a3b8", maxTicksLimit: 12 }, grid: { display: false },
             title: { display: true, text: "CPU Usage (%)", color: "#94a3b8" } },
      },
      plugins: { legend: { display: false } },
    },
  });

  const panel = { section, trendChart, histChart };
  panels[lp.key] = panel;
  return panel;
}

function renderTrend(panel, lp, thresholds) {
  const th = thresholds || {};
  const tc = panel.trendChart;
  tc.data.labels = lp.samples.map((s) => fmtTime(s.t));
  tc.data.datasets[0].data = lp.samples.map((s) => s.cpu);
  tc.options.plugins.thresholdLines.lines = [
    { value: th.warning, color: "#f59e0b", dash: [6, 4] },
    { value: th.critical, color: "#ef4444", dash: [6, 4] },
    { value: th.cap, color: "#a855f7", dash: [2, 3] },
  ];
  tc.update();
  document.getElementById("legend-" + lp.key).innerHTML =
    `<span class="lg lg-cpu">CPU %</span>` +
    `<span class="lg lg-warn">Warning (${th.warning}%)</span>` +
    `<span class="lg lg-crit">Critical (${th.critical}%)</span>` +
    `<span class="lg lg-cap">Entitled Cap (${th.cap}%)</span>`;
}

function renderHist(panel, lp, thresholds) {
  const th = thresholds || {};
  const h = lp.histogram || { labels: [], counts: [] };
  const hc = panel.histChart;
  hc.data.labels = h.labels;
  hc.data.datasets[0].data = h.counts;
  hc.data.datasets[0].backgroundColor =
    h.labels.map((l) => barColor(l, th.warning, th.critical));
  hc.update();
}

function renderSummary(lp, thresholds) {
  const s = lp.summary || {};
  const st = lp.stats || {};
  const th = thresholds || {};
  const peakCls = st.peak >= th.critical ? "crit" : st.peak >= th.warning ? "warn" : "";
  const html = [
    `<div class="sgroup">`,
    row("Host", s.host || "–"),
    row("OS", s.os || "–"),
    row("Partition", `${s.partition_id} of ${s.partitions}`),
    row("Machine", `${s.machine || "–"} · ${s.serial || ""}`),
    row("Entitled / Max", `${s.entitled_capacity} / ${s.max_capacity} cores`),
    row("Virtual Procs", `${s.virtual_processors} (max ${s.max_virtual_processors})`),
    `</div>`,
    `<div class="sgroup">`,
    row("Samples", st.count),
    `<div class="srow"><span class="slabel">Avg / Peak</span>` +
      `<span class="sval">${st.avg}% / ` +
      `<span class="${peakCls}">${st.peak}%</span></span></div>`,
    row("P95 / P99", `${st.p95}% / ${st.p99}%`),
    row(`Warn (≥${th.warning}%)`, `${st.warn_alerts} (${st.minutes_over_warn}m)`,
        st.warn_alerts > 0 ? "warn" : ""),
    row(`Crit (≥${th.critical}%)`, `${st.crit_alerts} (${st.minutes_over_crit}m)`,
        st.crit_alerts > 0 ? "crit" : ""),
    `</div>`,
  ].join("");
  document.getElementById("summary-" + lp.key).innerHTML = html;
}

function renderStatus(lp) {
  const el = document.getElementById("status-" + lp.key);
  if (!el) return;
  if (lp.ok === false) {
    el.textContent = "error";
    el.className = "panel-status bad";
    el.title = lp.error || "";
  } else {
    const peak = (lp.stats && lp.stats.peak) || 0;
    el.textContent = `avg ${(lp.stats && lp.stats.avg) || 0}% · peak ${peak}%`;
    el.className = "panel-status ok";
    el.title = "";
  }
}

function renderDashboard(data) {
  const th = data.thresholds || {};
  for (const lp of data.lpars) {
    const panel = panels[lp.key] || createPanel(lp);
    renderTrend(panel, lp, th);
    renderHist(panel, lp, th);
    renderSummary(lp, th);
    renderStatus(lp);
  }
}

async function refresh() {
  try {
    const res = await fetch("/api/lpar");
    const data = await res.json();
    if (!data.ok) {
      document.getElementById("updated").textContent = "error: " + (data.error || "");
      return;
    }
    renderDashboard(data);
    document.getElementById("sample-int").textContent = data.sample_seconds;
    document.getElementById("hist-hours").textContent = data.history_hours;
    document.getElementById("updated").textContent =
      "updated " + new Date().toLocaleTimeString();
  } catch (e) {
    document.getElementById("updated").textContent = "fetch failed";
  }
}

async function checkHealth() {
  try {
    const res = await fetch("/api/health");
    const h = await res.json();
    const el = document.getElementById("health");
    if (h.ok) {
      el.textContent = h.mode === "mock" ? "mock data" : "connected";
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

checkHealth();
refresh();
setInterval(refresh, REFRESH_MS);
setInterval(checkHealth, REFRESH_MS * 3);
