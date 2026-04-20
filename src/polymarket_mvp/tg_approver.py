from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from datetime import timedelta
from pathlib import Path
from typing import Any, Dict, List

import requests
from flask import Flask, jsonify, render_template_string, request

from .common import (
    append_jsonl,
    clamp_approval_ttl,
    debug_events_path,
    dump_json,
    get_env_bool,
    get_env_float,
    get_env_int,
    load_repo_env,
    parse_iso8601,
    proposal_id_for,
    read_proposals,
    utc_now_iso,
)
from .db import (
    approval_by_callback,
    connect_db,
    decision_status_for,
    init_db,
    latest_execution,
    list_expired_pending_proposals,
    proposal_record,
    record_approval,
    record_execution,
    update_proposal_status,
    update_proposal_workflow_fields,
)
from .ops_snapshot import build_ops_snapshot
from .poly_executor import execute_record

load_repo_env()


OPS_DASHBOARD_TEMPLATE = """

<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Polymarket OS · Trading Terminal</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;700&display=swap" rel="stylesheet">
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
  <style>
    :root {
      --bg: #0d1117;
      --surface: #161b22;
      --surface-2: #21262d;
      --surface-3: #2d333b;
      --border: #30363d;
      --border-strong: #484f58;
      --green: #00ff88;
      --green-dim: #0f8a5f;
      --red: #ff4444;
      --red-dim: #c53c4c;
      --blue: #58a6ff;
      --amber: #f0a832;
      --purple: #bc8cff;
      --text: #e6edf3;
      --muted: #7d8590;
      --muted-2: #545b64;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    html, body {
      background: var(--bg);
      color: var(--text);
      font: 13px/1.5 "Inter", -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      -webkit-font-smoothing: antialiased;
      -moz-osx-font-smoothing: grayscale;
    }
    body {
      background:
        radial-gradient(ellipse at top left, rgba(88,166,255,0.04), transparent 45%),
        radial-gradient(ellipse at top right, rgba(0,255,136,0.03), transparent 45%),
        var(--bg);
      min-height: 100vh;
    }
    .mono { font-family: "JetBrains Mono", Menlo, Consolas, monospace; font-variant-numeric: tabular-nums; }
    .muted { color: var(--muted); }
    .muted-2 { color: var(--muted-2); }
    .green { color: var(--green); }
    .red { color: var(--red); }
    .blue { color: var(--blue); }
    .amber { color: var(--amber); }
    a { color: var(--blue); text-decoration: none; }
    a:hover { text-decoration: underline; }
    .page { max-width: 1800px; margin: 0 auto; padding: 14px 18px 28px; }

    /* HEADER */
    .header {
      display: flex; align-items: center; gap: 14px;
      padding: 10px 16px;
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 10px;
      margin-bottom: 12px;
    }
    .brand { display: flex; align-items: center; gap: 10px; font-weight: 700; letter-spacing: 0.04em; }
    .brand .logo {
      width: 26px; height: 26px; border-radius: 7px;
      background: linear-gradient(135deg, var(--green), var(--blue));
      display: grid; place-items: center; color: #000; font-weight: 800; font-size: 13px;
    }
    .pulse-dot {
      width: 8px; height: 8px; border-radius: 50%; background: var(--green);
      animation: pulse 2s infinite;
      box-shadow: 0 0 8px var(--green);
    }
    .pulse-dot.red { background: var(--red); box-shadow: 0 0 8px var(--red); }
    .pulse-dot.amber { background: var(--amber); box-shadow: 0 0 8px var(--amber); }
    @keyframes pulse {
      0%, 100% { opacity: 1; }
      50% { opacity: 0.35; }
    }
    .header .sep { color: var(--muted-2); }
    .header .clock { font-family: "JetBrains Mono", monospace; color: var(--muted); font-size: 12px; }
    .header .grow { flex: 1; }
    .badge {
      padding: 4px 9px; border-radius: 6px;
      font-size: 11px; font-weight: 700; letter-spacing: 0.06em;
      border: 1px solid var(--border);
      background: var(--surface-2);
      text-transform: uppercase;
    }
    .badge.live { background: rgba(255,68,68,0.12); border-color: rgba(255,68,68,0.4); color: var(--red); }
    .badge.mock { background: rgba(88,166,255,0.12); border-color: rgba(88,166,255,0.4); color: var(--blue); }
    .badge.kill { background: rgba(240,168,50,0.14); border-color: rgba(240,168,50,0.5); color: var(--amber); }
    .btn {
      padding: 6px 12px; border-radius: 6px;
      border: 1px solid var(--border); background: var(--surface-2);
      color: var(--text); font: 500 12px Inter, sans-serif;
      cursor: pointer; transition: all 0.15s;
    }
    .btn:hover { border-color: var(--border-strong); background: var(--surface-3); }
    .btn.primary { background: var(--green); color: #000; border-color: var(--green); }
    .btn.primary:hover { background: #4dffaa; }
    .btn.danger { background: rgba(255,68,68,0.14); color: var(--red); border-color: rgba(255,68,68,0.4); }

    /* ALERTS */
    .alerts { display: none; margin-bottom: 12px; }
    .alerts.has { display: block; }
    .alert {
      padding: 10px 14px; margin-bottom: 6px;
      border-left: 3px solid var(--amber);
      background: rgba(240,168,50,0.08);
      border-radius: 0 8px 8px 0;
      display: flex; gap: 10px; align-items: center;
      font-size: 12px;
    }
    .alert.high { border-left-color: var(--red); background: rgba(255,68,68,0.08); }

    /* KPI STRIP */
    .kpi-strip {
      display: grid;
      grid-template-columns: repeat(6, 1fr);
      gap: 10px; margin-bottom: 12px;
    }
    .kpi {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 12px 14px;
      position: relative; overflow: hidden;
    }
    .kpi::before {
      content: ""; position: absolute; top: 0; left: 0; right: 0; height: 2px;
      background: var(--blue); opacity: 0.5;
    }
    .kpi.pnl-pos::before { background: var(--green); }
    .kpi.pnl-neg::before { background: var(--red); }
    .kpi .label {
      font-size: 10px; color: var(--muted); letter-spacing: 0.1em;
      text-transform: uppercase; font-weight: 600; margin-bottom: 6px;
    }
    .kpi .value {
      font-family: "JetBrains Mono", monospace;
      font-size: 22px; font-weight: 700; letter-spacing: -0.02em;
      line-height: 1.1;
    }
    .kpi .sub {
      font-size: 11px; color: var(--muted); margin-top: 4px;
    }
    .kpi .bar {
      height: 3px; border-radius: 2px; background: var(--surface-2);
      margin-top: 6px; overflow: hidden;
    }
    .kpi .bar .fill {
      height: 100%; background: var(--green); transition: width 0.5s ease;
    }
    @keyframes flashbg {
      0% { background-color: rgba(88,166,255,0.25); }
      100% { background-color: transparent; }
    }
    .flash { animation: flashbg 0.6s ease-out; }

    /* CHARTS ROW */
    .charts-row {
      display: grid;
      grid-template-columns: 1.4fr 1fr;
      gap: 10px; margin-bottom: 12px;
    }
    .panel {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 12px 14px;
    }
    .panel-title {
      display: flex; align-items: center; justify-content: space-between;
      font-size: 11px; color: var(--muted); letter-spacing: 0.08em;
      text-transform: uppercase; font-weight: 600;
      margin-bottom: 10px;
    }
    .panel-title .count {
      background: var(--surface-2); color: var(--text);
      padding: 2px 8px; border-radius: 10px; font-size: 11px;
      letter-spacing: 0; text-transform: none;
    }
    .chart-wrap { position: relative; height: 200px; }

    /* HEALTH MATRIX */
    .health-matrix {
      display: grid;
      grid-template-columns: repeat(8, 1fr);
      gap: 8px; margin-bottom: 12px;
    }
    .loop-card {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 10px 12px;
      position: relative;
    }
    .loop-card.green { border-color: rgba(0,255,136,0.3); }
    .loop-card.amber { border-color: rgba(240,168,50,0.4); }
    .loop-card.red { border-color: rgba(255,68,68,0.4); background: rgba(255,68,68,0.03); }
    .loop-card .top {
      display: flex; align-items: center; justify-content: space-between;
      margin-bottom: 4px;
    }
    .loop-card .name {
      font-size: 11px; font-weight: 600; letter-spacing: 0.04em;
      text-transform: uppercase;
    }
    .loop-card .age { font-family: "JetBrains Mono", monospace; font-size: 10px; color: var(--muted); }
    .loop-card .items { font-family: "JetBrains Mono", monospace; font-size: 15px; font-weight: 700; }
    .loop-card canvas { width: 100% !important; height: 24px !important; margin-top: 4px; }

    /* LIVE PANELS */
    .live-row {
      display: grid;
      grid-template-columns: repeat(3, 1fr);
      gap: 10px; margin-bottom: 12px;
    }
    .recent-row {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 10px; margin-bottom: 12px;
    }
    table {
      width: 100%;
      border-collapse: collapse;
      font-size: 12px;
    }
    th {
      text-align: left; padding: 6px 8px;
      font-size: 10px; color: var(--muted); letter-spacing: 0.06em;
      text-transform: uppercase; font-weight: 600;
      border-bottom: 1px solid var(--border);
    }
    td {
      padding: 8px; border-bottom: 1px solid var(--surface-2);
      vertical-align: top;
    }
    tr:hover td { background: rgba(255,255,255,0.02); }
    .market-cell { max-width: 260px; }
    .market-cell .q {
      display: block;
      overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
      font-weight: 500;
    }
    .market-cell .sub {
      font-size: 10px; color: var(--muted); font-family: "JetBrains Mono", monospace;
    }
    .chip {
      display: inline-block;
      padding: 2px 7px; border-radius: 4px;
      font-size: 10px; font-weight: 600; letter-spacing: 0.04em;
      background: var(--surface-2); border: 1px solid var(--border);
      text-transform: uppercase;
    }
    .chip.green { color: var(--green); border-color: rgba(0,255,136,0.3); background: rgba(0,255,136,0.08); }
    .chip.red { color: var(--red); border-color: rgba(255,68,68,0.3); background: rgba(255,68,68,0.08); }
    .chip.amber { color: var(--amber); border-color: rgba(240,168,50,0.3); background: rgba(240,168,50,0.08); }
    .chip.blue { color: var(--blue); border-color: rgba(88,166,255,0.3); background: rgba(88,166,255,0.08); }
    .countdown { font-family: "JetBrains Mono", monospace; font-weight: 600; }
    .countdown.warn { color: var(--amber); }
    .countdown.urgent { color: var(--red); }
    .empty {
      padding: 24px; text-align: center; color: var(--muted-2);
      font-size: 12px;
    }

    /* CONTROLS DRAWER */
    .drawer {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 14px; margin-bottom: 12px;
      display: none;
    }
    .drawer.open { display: block; }
    .drawer .actions { display: flex; gap: 8px; margin-bottom: 10px; flex-wrap: wrap; }

    /* FOOTER */
    footer {
      padding: 12px 0; font-size: 11px; color: var(--muted);
      display: flex; justify-content: space-between; align-items: center;
    }

    /* SCROLLBAR */
    ::-webkit-scrollbar { width: 8px; height: 8px; }
    ::-webkit-scrollbar-track { background: var(--bg); }
    ::-webkit-scrollbar-thumb { background: var(--border); border-radius: 4px; }
    ::-webkit-scrollbar-thumb:hover { background: var(--border-strong); }

    @media (max-width: 1400px) {
      .kpi-strip { grid-template-columns: repeat(3, 1fr); }
      .health-matrix { grid-template-columns: repeat(4, 1fr); }
      .charts-row { grid-template-columns: 1fr; }
      .live-row { grid-template-columns: 1fr; }
      .recent-row { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <div class="page">
    <!-- HEADER -->
    <div class="header">
      <div class="brand">
        <div class="logo">P</div>
        <span>POLYMARKET OS</span>
      </div>
      <span class="pulse-dot" id="status-dot"></span>
      <span class="muted mono" id="status-text">connecting</span>
      <span class="sep">·</span>
      <span class="clock mono" id="utc-clock">--:--:-- UTC</span>
      <span class="grow"></span>
      <span class="badge mock" id="mode-badge">MOCK</span>
      <span class="badge kill" id="kill-badge" style="display:none">⛔ KILL SWITCH</span>
      <button class="btn" onclick="toggleDrawer()" id="drawer-btn">⚙ Controls</button>
    </div>

    <!-- ALERTS -->
    <div class="alerts" id="alerts"></div>

    <!-- KPI STRIP -->
    <div class="kpi-strip" id="kpi-strip">
      <div class="kpi" id="kpi-pnl">
        <div class="label">Net P&L</div>
        <div class="value mono" id="kpi-pnl-val">$0.00</div>
        <div class="sub"><span id="kpi-pnl-realized">+$0</span> realized · <span id="kpi-pnl-unrealized">+$0</span> unrealized</div>
      </div>
      <div class="kpi">
        <div class="label">Win Rate</div>
        <div class="value mono" id="kpi-winrate">—</div>
        <div class="sub"><span id="kpi-wins">0</span>W / <span id="kpi-losses">0</span>L · <span id="kpi-resolved">0</span> resolved</div>
        <div class="bar"><div class="fill" id="kpi-winrate-bar" style="width: 0%"></div></div>
      </div>
      <div class="kpi">
        <div class="label">Open Positions</div>
        <div class="value mono" id="kpi-open">0</div>
        <div class="sub"><span id="kpi-pending">0</span> pending · <span id="kpi-live">0</span> live orders</div>
      </div>
      <div class="kpi">
        <div class="label">USDC Balance</div>
        <div class="value mono" id="kpi-balance">—</div>
        <div class="sub">NAV <span id="kpi-nav" class="mono">—</span></div>
      </div>
      <div class="kpi">
        <div class="label">Exposure</div>
        <div class="value mono" id="kpi-exposure">$0.00</div>
        <div class="sub"><span id="kpi-unredeemed">$0</span> unredeemed</div>
      </div>
      <div class="kpi">
        <div class="label">Guardrails</div>
        <div class="value mono" id="kpi-kill">0</div>
        <div class="sub"><span id="kpi-failures">0</span> recent failures</div>
      </div>
    </div>

    <!-- CHARTS ROW -->
    <div class="charts-row">
      <div class="panel">
        <div class="panel-title">
          <span>📈 Cumulative PnL</span>
          <span class="count mono" id="pnl-chart-hdr">$0.00</span>
        </div>
        <div class="chart-wrap"><canvas id="pnl-chart"></canvas></div>
      </div>
      <div class="panel">
        <div class="panel-title">
          <span>🎯 Proposal Funnel</span>
          <span class="count mono" id="funnel-total">0</span>
        </div>
        <div class="chart-wrap"><canvas id="funnel-chart"></canvas></div>
      </div>
    </div>

    <!-- HEALTH MATRIX -->
    <div class="section-header"><span class="section-title">⚙ System Health</span></div>
    <div class="health-matrix" id="health-matrix"></div>

    <!-- LIVE PANELS -->
    <div class="live-row">
      <div class="panel">
        <div class="panel-title"><span>⏳ Pending Approvals</span><span class="count" id="pending-count">0</span></div>
        <div id="pending-body"></div>
      </div>
      <div class="panel">
        <div class="panel-title"><span>⚡ Live Orders</span><span class="count" id="live-count">0</span></div>
        <div id="live-body"></div>
      </div>
      <div class="panel">
        <div class="panel-title"><span>📊 Open Positions</span><span class="count" id="open-count">0</span></div>
        <div id="open-body"></div>
      </div>
    </div>

    <!-- RECENT ROW -->
    <div class="recent-row">
      <div class="panel">
        <div class="panel-title"><span>✅ Resolved Positions</span><span class="count" id="resolved-count">0</span></div>
        <div id="resolved-body"></div>
      </div>
      <div class="panel">
        <div class="panel-title"><span>⚠ Recent Failures</span><span class="count" id="failures-count">0</span></div>
        <div id="failures-body"></div>
      </div>
    </div>

    <!-- CONTROLS DRAWER -->
    <div class="drawer" id="drawer">
      <div class="panel-title"><span>⚙ System Controls</span><span class="muted mono" id="drawer-mode">mode: mock</span></div>
      <div class="actions">
        <button class="btn primary" onclick="sysCtl('start')">▶ Start</button>
        <button class="btn" onclick="sysCtl('restart')">↻ Restart</button>
        <button class="btn danger" onclick="sysCtl('stop')">⏹ Stop</button>
        <a class="btn" href="/api/ops/status" target="_blank">📄 Raw Snapshot</a>
        <a class="btn" href="http://localhost:8788/" target="_blank">🎛 Control Plane</a>
      </div>
      <div class="mono muted-2" id="drawer-log" style="font-size:11px"></div>
    </div>

    <footer>
      <span>Last refresh: <span class="mono" id="last-refresh">—</span> · 3s cadence</span>
      <span class="muted-2">Polymarket OS v0.6 · <span id="uptime-label">uptime —</span></span>
    </footer>
  </div>

  <script id="initial-ops-data" type="application/json">{{ initial_json|safe }}</script>
  <script>
    // ============================================================
    // STATE
    // ============================================================
    let lastSnapshot = null;
    let pnlChart = null, funnelChart = null;
    let sparklineCharts = {};
    let countdownState = {}; // key -> {expiresAt: ms-epoch, el: element, urgent: bool}
    const LOOP_NAMES = ["scan","context","propose","expiry","execute","reconcile","exit","review"];

    // ============================================================
    // FORMATTERS
    // ============================================================
    const fmtMoney = (v) => {
      if (v == null || isNaN(v)) return "—";
      const n = Number(v);
      const sign = n < 0 ? "-" : "";
      const abs = Math.abs(n);
      return sign + "$" + abs.toLocaleString("en-US", {minimumFractionDigits: 2, maximumFractionDigits: 2});
    };
    const fmtSigned = (v) => {
      if (v == null || isNaN(v)) return "—";
      const n = Number(v);
      const sign = n >= 0 ? "+" : "-";
      const abs = Math.abs(n);
      return sign + "$" + abs.toLocaleString("en-US", {minimumFractionDigits: 2, maximumFractionDigits: 2});
    };
    const fmtPct = (v, digits=1) => {
      if (v == null || isNaN(v)) return "—";
      return Number(v).toFixed(digits) + "%";
    };
    const fmtAge = (s) => {
      if (s == null) return "—";
      s = Math.max(0, Math.floor(s));
      if (s < 60) return s + "s";
      if (s < 3600) return Math.floor(s/60) + "m";
      if (s < 86400) return Math.floor(s/3600) + "h";
      return Math.floor(s/86400) + "d";
    };
    const fmtCountdown = (s) => {
      if (s == null) return "—";
      const neg = s < 0;
      s = Math.abs(Math.floor(s));
      const m = Math.floor(s/60);
      const sec = s % 60;
      return (neg?"-":"") + m + ":" + String(sec).padStart(2,"0");
    };
    const escHtml = (s) => {
      if (s == null) return "";
      return String(s)
        .replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;")
        .replace(/"/g,"&quot;").replace(/'/g,"&#39;");
    };
    const pnlClass = (v) => v == null ? "" : (v > 0.005 ? "green" : (v < -0.005 ? "red" : "muted"));

    // ============================================================
    // CLOCK
    // ============================================================
    function tickClock() {
      const d = new Date();
      const s = d.toISOString().substr(11,8);
      document.getElementById("utc-clock").textContent = s + " UTC";
    }
    setInterval(tickClock, 1000); tickClock();

    // ============================================================
    // COUNTDOWN TIMERS (local tick)
    // ============================================================
    function tickCountdowns() {
      const now = Date.now();
      Object.values(countdownState).forEach(c => {
        if (!c.el || !document.body.contains(c.el)) return;
        const remaining = Math.floor((c.expiresAt - now) / 1000);
        c.el.textContent = fmtCountdown(remaining);
        c.el.classList.remove("warn","urgent");
        if (remaining < 0) c.el.classList.add("urgent");
        else if (remaining < 30) c.el.classList.add("warn");
      });
    }
    setInterval(tickCountdowns, 1000);

    // ============================================================
    // CHART INIT
    // ============================================================
    function createGradient(ctx, color) {
      const h = ctx.canvas.parentNode.clientHeight || 200;
      const gr = ctx.createLinearGradient(0, 0, 0, h);
      gr.addColorStop(0, color + "44");
      gr.addColorStop(1, color + "00");
      return gr;
    }

    function initCharts() {
      Chart.defaults.font.family = "'Inter', sans-serif";
      Chart.defaults.color = "#7d8590";
      Chart.defaults.borderColor = "#30363d";

      // PnL
      const pnlCtx = document.getElementById("pnl-chart").getContext("2d");
      pnlChart = new Chart(pnlCtx, {
        type: "line",
        data: { labels: [], datasets: [{
          label: "Cumulative PnL",
          data: [],
          borderColor: "#00ff88",
          backgroundColor: createGradient(pnlCtx, "#00ff88"),
          fill: true,
          tension: 0.35,
          pointRadius: 0,
          pointHoverRadius: 4,
          pointHoverBackgroundColor: "#00ff88",
          borderWidth: 2,
        }]},
        options: {
          responsive: true, maintainAspectRatio: false,
          animation: false,
          interaction: { mode: "index", intersect: false },
          scales: {
            x: { grid: { display: false }, ticks: { maxTicksLimit: 7, font: {size: 10}, color: "#7d8590" } },
            y: {
              grid: { color: "rgba(255,255,255,0.04)" },
              ticks: {
                font: {family: "JetBrains Mono", size: 10},
                color: "#7d8590",
                callback: (v) => "$" + Number(v).toFixed(0)
              }
            }
          },
          plugins: {
            legend: { display: false },
            tooltip: {
              backgroundColor: "#21262d",
              borderColor: "#30363d", borderWidth: 1,
              titleColor: "#e6edf3", bodyColor: "#e6edf3",
              titleFont: {family: "JetBrains Mono", size: 11},
              bodyFont: {family: "JetBrains Mono", size: 12},
              padding: 10,
              callbacks: {
                label: (ctx) => "  " + fmtSigned(ctx.parsed.y)
              }
            }
          }
        }
      });

      // Funnel
      const funnelCtx = document.getElementById("funnel-chart").getContext("2d");
      funnelChart = new Chart(funnelCtx, {
        type: "bar",
        data: {
          labels: ["Proposed","Risk Blocked","Pending/Approved","Executed"],
          datasets: [{
            data: [0,0,0,0],
            backgroundColor: ["#58a6ff","#ff4444","#f0a832","#00ff88"],
            borderRadius: 4,
            barThickness: 22,
          }]
        },
        options: {
          indexAxis: "y",
          responsive: true, maintainAspectRatio: false,
          animation: false,
          scales: {
            x: {
              grid: { color: "rgba(255,255,255,0.04)" },
              ticks: { font: {family: "JetBrains Mono", size: 10}, color: "#7d8590" }
            },
            y: {
              grid: { display: false },
              ticks: { font: {size: 11, weight: "600"}, color: "#e6edf3" }
            }
          },
          plugins: {
            legend: { display: false },
            tooltip: {
              backgroundColor: "#21262d",
              borderColor: "#30363d", borderWidth: 1,
              callbacks: {
                label: (ctx) => {
                  const total = ctx.chart.data.datasets[0].data[0] || 1;
                  const v = ctx.parsed.x;
                  return "  " + v + " (" + ((v/total)*100).toFixed(1) + "%)";
                }
              }
            }
          }
        }
      });

      // Sparklines
      LOOP_NAMES.forEach(name => {
        const canvas = document.getElementById("spark-"+name);
        if (!canvas) return;
        sparklineCharts[name] = new Chart(canvas.getContext("2d"), {
          type: "line",
          data: { labels: Array(20).fill(""), datasets: [{
            data: Array(20).fill(0),
            borderColor: "#58a6ff",
            borderWidth: 1.5,
            fill: false,
            pointRadius: 0,
            tension: 0.3,
          }]},
          options: {
            responsive: false, maintainAspectRatio: false,
            animation: false,
            scales: { x: { display: false }, y: { display: false } },
            plugins: { legend: { display: false }, tooltip: { enabled: false } }
          }
        });
      });
    }

    // ============================================================
    // RENDERERS
    // ============================================================
    function renderSnapshot(snap) {
      if (!snap) return;
      lastSnapshot = snap;
      renderHeader(snap);
      renderAlerts(snap);
      renderKPIs(snap);
      renderHealth(snap);
      renderPending(snap);
      renderLiveOrders(snap);
      renderOpen(snap);
      renderResolved(snap);
      renderFailures(snap);
      const ts = snap.timestamp || new Date().toISOString();
      document.getElementById("last-refresh").textContent = ts.substr(11,8);
    }

    function renderHeader(snap) {
      const health = snap.system_health || [];
      const redCount = health.filter(h => h.health === "red").length;
      const amberCount = health.filter(h => h.health === "yellow" || h.health === "amber").length;
      const dot = document.getElementById("status-dot");
      const txt = document.getElementById("status-text");
      dot.classList.remove("red","amber");
      if (redCount > 0) { dot.classList.add("red"); txt.textContent = redCount + " loops down"; }
      else if (amberCount > 0) { dot.classList.add("amber"); txt.textContent = amberCount + " loops lagging"; }
      else { txt.textContent = "all systems operational"; }

      const mode = (snap.control_state && snap.control_state.tg_auto_execute_mode) || "mock";
      const modeBadge = document.getElementById("mode-badge");
      modeBadge.textContent = mode.toUpperCase();
      modeBadge.classList.remove("live","mock");
      modeBadge.classList.add(mode === "real" ? "live" : "mock");
      document.getElementById("drawer-mode").textContent = "mode: " + mode;

      const kills = (snap.control_state && snap.control_state.kill_switches) || [];
      const killBadge = document.getElementById("kill-badge");
      if (kills.length > 0) { killBadge.style.display = ""; killBadge.textContent = "⛔ " + kills.length + " KILL"; }
      else killBadge.style.display = "none";
    }

    function renderAlerts(snap) {
      const items = snap.needs_attention || [];
      const box = document.getElementById("alerts");
      if (items.length === 0) { box.classList.remove("has"); box.innerHTML = ""; return; }
      box.classList.add("has");
      box.innerHTML = items.slice(0, 4).map(a => `
        <div class="alert ${a.severity === "high" ? "high" : ""}">
          <span>${a.severity === "high" ? "🔴" : "⚠"}</span>
          <strong>${escHtml(a.title || a.kind)}</strong>
          <span class="muted">${escHtml(a.detail || "")}</span>
        </div>
      `).join("");
    }

    function renderKPIs(snap) {
      const p = snap.portfolio || {};
      const pnl = Number(p.total_pnl || 0);
      const pnlEl = document.getElementById("kpi-pnl");
      pnlEl.classList.remove("pnl-pos","pnl-neg");
      pnlEl.classList.add(pnl >= 0 ? "pnl-pos" : "pnl-neg");
      const pnlVal = document.getElementById("kpi-pnl-val");
      pnlVal.textContent = fmtSigned(pnl);
      pnlVal.className = "value mono " + pnlClass(pnl);
      document.getElementById("kpi-pnl-realized").textContent = fmtSigned(p.total_realized_pnl);
      document.getElementById("kpi-pnl-unrealized").textContent = fmtSigned(p.total_unrealized_pnl);

      const wr = p.win_rate;
      document.getElementById("kpi-winrate").textContent = wr != null ? fmtPct(wr, 1) : "—";
      document.getElementById("kpi-wins").textContent = p.wins || 0;
      document.getElementById("kpi-losses").textContent = p.losses || 0;
      document.getElementById("kpi-resolved").textContent = p.resolved_count || 0;
      document.getElementById("kpi-winrate-bar").style.width = Math.max(0, Math.min(100, wr || 0)) + "%";

      document.getElementById("kpi-open").textContent = p.open_count || 0;
      document.getElementById("kpi-pending").textContent = snap.pending_count || 0;
      document.getElementById("kpi-live").textContent = snap.live_order_count || 0;

      document.getElementById("kpi-balance").textContent = p.usdc_balance != null ? fmtMoney(p.usdc_balance) : "—";
      document.getElementById("kpi-nav").textContent = p.net_asset_value != null ? fmtMoney(p.net_asset_value) : "—";

      document.getElementById("kpi-exposure").textContent = fmtMoney(p.open_exposure_usdc);
      document.getElementById("kpi-unredeemed").textContent = fmtMoney(p.neg_risk_unredeemed_usdc || 0);

      const kills = (snap.control_state && snap.control_state.kill_switches) || [];
      document.getElementById("kpi-kill").textContent = kills.length;
      document.getElementById("kpi-failures").textContent = (snap.recent_failures || []).length;
    }

    function renderHealth(snap) {
      const container = document.getElementById("health-matrix");
      const existing = new Set(LOOP_NAMES);
      // Build once
      if (container.children.length === 0) {
        container.innerHTML = LOOP_NAMES.map(n => `
          <div class="loop-card" id="loop-${n}">
            <div class="top">
              <span class="name">${n}</span>
              <span class="pulse-dot" id="dot-${n}"></span>
            </div>
            <div class="items mono" id="items-${n}">—</div>
            <div class="age mono" id="age-${n}">— ago</div>
            <canvas id="spark-${n}" width="120" height="24"></canvas>
          </div>
        `).join("");
      }
      const health = snap.system_health || [];
      health.forEach(h => {
        const card = document.getElementById("loop-"+h.loop);
        if (!card) return;
        card.classList.remove("green","amber","red");
        const hh = h.health === "yellow" ? "amber" : h.health;
        card.classList.add(hh || "green");
        const dot = document.getElementById("dot-"+h.loop);
        dot.classList.remove("red","amber");
        if (hh === "red") dot.classList.add("red");
        else if (hh === "amber") dot.classList.add("amber");
        document.getElementById("items-"+h.loop).textContent = (h.items_processed != null ? h.items_processed : "—");
        document.getElementById("age-"+h.loop).textContent = fmtAge(h.age_seconds) + " ago";
      });
    }

    function renderPending(snap) {
      const items = snap.pending_approvals || [];
      document.getElementById("pending-count").textContent = items.length;
      const body = document.getElementById("pending-body");
      if (items.length === 0) { body.innerHTML = '<div class="empty">No pending approvals</div>'; return; }
      body.innerHTML = `<table><thead><tr>
        <th>Market</th><th>Side</th><th>Size</th><th>Conf</th><th>Expires</th>
      </tr></thead><tbody>${items.map(p => {
        const cd = p.approval_expires_at ? `cd-pending-${escHtml(p.proposal_id)}` : "";
        return `<tr>
          <td class="market-cell">
            <span class="q">${escHtml(p.market || p.market_id)}</span>
            <span class="sub">${escHtml(p.proposal_id)}</span>
          </td>
          <td><span class="chip ${p.outcome && /yes|over|up/i.test(p.outcome) ? "green" : "red"}">${escHtml(p.outcome)}</span></td>
          <td class="mono">${fmtMoney(p.size_usdc)}</td>
          <td class="mono">${p.confidence_score != null ? (p.confidence_score*100).toFixed(0)+"%" : "—"}</td>
          <td class="mono"><span class="countdown" id="${cd}">—</span></td>
        </tr>`;
      }).join("")}</tbody></table>`;
      items.forEach(p => {
        if (p.approval_expires_at) {
          countdownState["pending-"+p.proposal_id] = {
            expiresAt: new Date(p.approval_expires_at).getTime(),
            el: document.getElementById(`cd-pending-${p.proposal_id}`),
          };
        }
      });
    }

    function renderLiveOrders(snap) {
      const items = snap.live_orders || [];
      document.getElementById("live-count").textContent = items.length;
      const body = document.getElementById("live-body");
      if (items.length === 0) { body.innerHTML = '<div class="empty">No live orders</div>'; return; }
      body.innerHTML = `<table><thead><tr>
        <th>Market</th><th>Side</th><th>Size</th><th>Status</th><th>TTL</th>
      </tr></thead><tbody>${items.map(o => {
        const cd = `cd-live-${o.execution_id}`;
        const expEpoch = o.order_posted_at && o.order_live_ttl_seconds
          ? new Date(o.order_posted_at).getTime() + (o.order_live_ttl_seconds * 1000) : null;
        return `<tr>
          <td class="market-cell">
            <span class="q">${escHtml(o.market || o.market_id)}</span>
            <span class="sub">#${o.execution_id}</span>
          </td>
          <td><span class="chip blue">${escHtml(o.outcome)}</span></td>
          <td class="mono">${fmtMoney(o.requested_size_usdc)}</td>
          <td><span class="chip amber">${escHtml(o.status)}</span></td>
          <td class="mono"><span class="countdown" id="${cd}">—</span></td>
        </tr>`;
      }).join("")}</tbody></table>`;
      items.forEach(o => {
        if (o.order_posted_at && o.order_live_ttl_seconds) {
          const exp = new Date(o.order_posted_at).getTime() + (o.order_live_ttl_seconds * 1000);
          countdownState["live-"+o.execution_id] = {
            expiresAt: exp,
            el: document.getElementById(`cd-live-${o.execution_id}`),
          };
        }
      });
    }

    function renderOpen(snap) {
      const items = snap.open_positions || [];
      document.getElementById("open-count").textContent = items.length;
      const body = document.getElementById("open-body");
      if (items.length === 0) { body.innerHTML = '<div class="empty">No open positions</div>'; return; }
      body.innerHTML = `<table><thead><tr>
        <th>Market</th><th>Side</th><th>Entry</th><th>Mark</th><th>P&amp;L</th>
      </tr></thead><tbody>${items.map(pos => `
        <tr>
          <td class="market-cell">
            <span class="q">${escHtml(pos.market || pos.market_id)}</span>
            <span class="sub">${fmtMoney(pos.size_usdc)}</span>
          </td>
          <td><span class="chip blue">${escHtml(pos.outcome)}</span></td>
          <td class="mono muted">${pos.entry_price != null ? pos.entry_price.toFixed(3) : "—"}</td>
          <td class="mono">${pos.last_mark_price != null ? pos.last_mark_price.toFixed(3) : "—"}</td>
          <td class="mono ${pnlClass(pos.unrealized_pnl)}">${fmtSigned(pos.unrealized_pnl)}</td>
        </tr>
      `).join("")}</tbody></table>`;
    }

    function renderResolved(snap) {
      const items = (snap.resolved_positions || []).slice(0, 12);
      document.getElementById("resolved-count").textContent = (snap.resolved_positions || []).length;
      const body = document.getElementById("resolved-body");
      if (items.length === 0) { body.innerHTML = '<div class="empty">No resolved positions</div>'; return; }
      body.innerHTML = `<table><thead><tr>
        <th>Market</th><th>Side</th><th>Size</th><th>Realized P&amp;L</th><th>When</th>
      </tr></thead><tbody>${items.map(pos => `
        <tr>
          <td class="market-cell">
            <span class="q">${escHtml(pos.market || pos.market_id)}</span>
          </td>
          <td><span class="chip ${Number(pos.realized_pnl||0) >= 0 ? "green" : "red"}">${escHtml(pos.outcome)}</span></td>
          <td class="mono muted">${fmtMoney(pos.size_usdc)}</td>
          <td class="mono ${pnlClass(pos.realized_pnl)}"><strong>${fmtSigned(pos.realized_pnl)}</strong></td>
          <td class="mono muted-2">${(pos.updated_at || "").substr(5, 11).replace("T"," ")}</td>
        </tr>
      `).join("")}</tbody></table>`;
    }

    function renderFailures(snap) {
      const items = (snap.recent_failures || []).slice(0, 12);
      document.getElementById("failures-count").textContent = (snap.recent_failures || []).length;
      const body = document.getElementById("failures-body");
      if (items.length === 0) { body.innerHTML = '<div class="empty">No recent failures ✓</div>'; return; }
      body.innerHTML = `<table><thead><tr>
        <th>Kind</th><th>Category</th><th>Detail</th><th>When</th>
      </tr></thead><tbody>${items.map(f => `
        <tr>
          <td><span class="chip red">${escHtml(f.kind)}</span></td>
          <td class="mono amber">${escHtml(f.category || f.status || "")}</td>
          <td class="market-cell"><span class="q muted">${escHtml(f.message || "")}</span></td>
          <td class="mono muted-2">${(f.timestamp || "").substr(5, 11).replace("T"," ")}</td>
        </tr>
      `).join("")}</tbody></table>`;
    }

    // ============================================================
    // CHART UPDATES (slower cadence)
    // ============================================================
    function updatePnlChart(data) {
      if (!pnlChart || !data) return;
      const points = data.points || [];
      const finalVal = points.length ? points[points.length-1].cumulative_pnl : 0;
      const color = finalVal >= 0 ? "#00ff88" : "#ff4444";
      pnlChart.data.labels = points.map(p => (p.ts || "").substr(5, 5));
      pnlChart.data.datasets[0].data = points.map(p => p.cumulative_pnl);
      pnlChart.data.datasets[0].borderColor = color;
      const ctx = pnlChart.ctx;
      pnlChart.data.datasets[0].backgroundColor = createGradient(ctx, color);
      pnlChart.update("none");
      const hdr = document.getElementById("pnl-chart-hdr");
      hdr.textContent = fmtSigned(finalVal);
      hdr.className = "count mono " + pnlClass(finalVal);
    }

    function updateFunnelChart(stats) {
      if (!funnelChart || !stats) return;
      const p = stats.proposals_by_status || {};
      const total = Object.values(p).reduce((a,b) => a + Number(b||0), 0);
      const blocked = Number(p.risk_blocked || 0);
      const pending = Number(p.pending_approval || 0) + Number(p.approved || 0) + Number(p.authorized_for_execution || 0);
      const executed = Number(p.executed || 0);
      funnelChart.data.datasets[0].data = [total, blocked, pending, executed];
      funnelChart.update("none");
      document.getElementById("funnel-total").textContent = total + " total";
    }

    function updateSparklines(data) {
      if (!data || !data.loops) return;
      LOOP_NAMES.forEach(name => {
        const arr = data.loops[name] || [];
        const chart = sparklineCharts[name];
        if (!chart) return;
        const values = arr.map(h => h.items || 0);
        const anyErr = arr.some(h => h.error);
        chart.data.labels = values.map((_, i) => i);
        chart.data.datasets[0].data = values;
        chart.data.datasets[0].borderColor = anyErr ? "#ff4444" : "#58a6ff";
        chart.update("none");
      });
    }

    // ============================================================
    // POLLING
    // ============================================================
    async function refresh() {
      try {
        const [statusRes, hbRes] = await Promise.all([
          fetch("/api/ops/status"),
          fetch("/api/ops/heartbeat-history")
        ]);
        if (statusRes.ok) {
          const snap = await statusRes.json();
          renderSnapshot(snap);
        }
        if (hbRes.ok) {
          const hb = await hbRes.json();
          updateSparklines(hb);
        }
      } catch(e) { console.error("refresh error", e); }
    }

    async function refreshCharts() {
      try {
        const [pnlRes, statsRes] = await Promise.all([
          fetch("/api/ops/pnl-history"),
          fetch("/api/ops/stats")
        ]);
        if (pnlRes.ok) updatePnlChart(await pnlRes.json());
        if (statsRes.ok) updateFunnelChart(await statsRes.json());
      } catch(e) { console.error("chart refresh error", e); }
    }

    // ============================================================
    // CONTROLS
    // ============================================================
    function toggleDrawer() {
      document.getElementById("drawer").classList.toggle("open");
    }
    async function sysCtl(action) {
      const log = document.getElementById("drawer-log");
      log.textContent = `⏳ sending ${action}...`;
      try {
        const r = await fetch("/api/ops/system-control", {
          method: "POST", headers: {"Content-Type": "application/json"},
          body: JSON.stringify({action})
        });
        const data = await r.json();
        log.textContent = (data.ok ? "✓ " : "✗ ") + (data.message || data.error || JSON.stringify(data));
      } catch(e) { log.textContent = "✗ " + e.message; }
    }

    // ============================================================
    // BOOT
    // ============================================================
    initCharts();
    const initialNode = document.getElementById("initial-ops-data");
    try {
      const initial = JSON.parse(initialNode ? initialNode.textContent : "{}");
      renderSnapshot(initial);
    } catch(e) { console.error("initial parse", e); }

    refresh();
    refreshCharts();
    setInterval(refresh, 3000);
    setInterval(refreshCharts, 30000);
  </script>
</body>
</html>
"""


def _dashboard_initial_json(payload: Dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=False).replace("</", "<\\/")


def _launch_agent_specs() -> List[Dict[str, str]]:
    home = os.path.expanduser("~")
    launch_agents = os.path.join(home, "Library", "LaunchAgents")
    return [
        {
            "name": "autopilot",
            "label": "com.polymarket.autopilot",
            "plist": os.path.join(launch_agents, "com.polymarket.autopilot.plist"),
        },
        {
            "name": "tg-webhook",
            "label": "com.polymarket.tg-webhook",
            "plist": os.path.join(launch_agents, "com.polymarket.tg-webhook.plist"),
        },
    ]


def _launchctl_domain() -> str:
    return f"gui/{os.getuid()}"


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _manual_service_specs() -> List[Dict[str, Any]]:
    root = _repo_root()
    venv_bin = root / ".venv311" / "bin"
    return [
        {
            "name": "autopilot",
            "kind": "process_match",
            "argv": [str(venv_bin / "python"), "-m", "polymarket_mvp.autopilot"],
            "markers": [
                "polymarket_mvp.autopilot",
                "src.polymarket_mvp.autopilot",
                "/polymarket-autopilot",
            ],
            "log": root / "var" / "log" / "autopilot.dashboard.log",
        },
        {
            "name": "tg-webhook",
            "kind": "port_listener",
            "port": 8787,
            "argv": [str(venv_bin / "tg-approver"), "serve", "--port", "8787"],
            "markers": ["tg-approver", "polymarket_mvp.tg_approver", "src.polymarket_mvp.tg_approver"],
            "log": root / "var" / "log" / "tg-webhook.dashboard.log",
        },
    ]


def _run_launchctl(*args: str) -> Dict[str, Any]:
    command = ["launchctl", *args]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    return {
        "command": command,
        "returncode": result.returncode,
        "stdout": (result.stdout or "").strip(),
        "stderr": (result.stderr or "").strip(),
    }


def _ps_processes() -> List[Dict[str, str]]:
    try:
        result = subprocess.run(["ps", "-axo", "pid=,command="], capture_output=True, text=True, check=False)
    except Exception:
        return []
    if result.returncode != 0:
        return []
    items: List[Dict[str, str]] = []
    for raw in (result.stdout or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        parts = line.split(None, 1)
        if not parts:
            continue
        pid = parts[0]
        command = parts[1] if len(parts) > 1 else ""
        if pid.isdigit():
            items.append({"pid": pid, "command": command})
    return items


def _matching_pids(markers: List[str]) -> List[int]:
    current_pid = os.getpid()
    matched: List[int] = []
    for item in _ps_processes():
        pid = int(item["pid"])
        command = item["command"]
        if pid == current_pid:
            continue
        if any(marker in command for marker in markers):
            matched.append(pid)
    return matched


def _listener_pids(port: int) -> List[int]:
    try:
        result = subprocess.run(
            ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
            capture_output=True,
            text=True,
            check=False,
        )
    except Exception:
        return []
    if result.returncode != 0:
        return []
    pids: List[int] = []
    for line in (result.stdout or "").splitlines():
        line = line.strip()
        if line.isdigit():
            pids.append(int(line))
    return pids


def _manual_running(spec: Dict[str, Any]) -> bool:
    if spec["kind"] == "port_listener":
        return bool(_listener_pids(int(spec["port"])))
    return bool(_matching_pids(list(spec["markers"])))


def _start_manual_service(spec: Dict[str, Any]) -> Dict[str, Any]:
    if _manual_running(spec):
        if spec["name"] == "autopilot":
            time.sleep(1.0)
            if _manual_running(spec):
                return {"name": spec["name"], "mode": "manual", "status": "already_running"}
        else:
            return {"name": spec["name"], "mode": "manual", "status": "already_running"}
    log_path = Path(spec["log"])
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as handle:
        subprocess.Popen(
            list(spec["argv"]),
            cwd=str(_repo_root()),
            stdout=handle,
            stderr=handle,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    return {"name": spec["name"], "mode": "manual", "status": "started", "log": str(log_path)}


def _stop_manual_service(spec: Dict[str, Any], *, include_self: bool = False) -> Dict[str, Any]:
    if spec["kind"] == "port_listener":
        pids = _listener_pids(int(spec["port"]))
    else:
        pids = _matching_pids(list(spec["markers"]))
    if not include_self:
        pids = [pid for pid in pids if pid != os.getpid()]
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            continue
    return {"name": spec["name"], "mode": "manual", "status": "stopped" if pids else "not_running", "pids": pids}


def _restart_manual_service(spec: Dict[str, Any]) -> Dict[str, Any]:
    stop_result = _stop_manual_service(spec, include_self=False)
    time.sleep(0.2)
    start_result = _start_manual_service(spec)
    return {"name": spec["name"], "mode": "manual", "status": "restarted", "stop": stop_result, "start": start_result}


def _launchctl_service_loaded(label: str) -> bool:
    result = _run_launchctl("print", f"{_launchctl_domain()}/{label}")
    return result["returncode"] == 0


def _schedule_tg_webhook_control(action: str, delay_seconds: float = 1.0) -> None:
    agent_specs = [item for item in _launch_agent_specs() if item["name"] == "tg-webhook"]
    manual_specs = [item for item in _manual_service_specs() if item["name"] == "tg-webhook"]
    payload = {
        "action": action,
        "delay": delay_seconds,
        "domain": _launchctl_domain(),
        "repo_root": str(_repo_root()),
        "agents": agent_specs,
        "manual": [
            {
                "name": item["name"],
                "kind": item["kind"],
                "argv": list(item["argv"]),
                "markers": list(item["markers"]),
                "port": item.get("port"),
                "log": str(item["log"]),
            }
            for item in manual_specs
        ],
    }
    script = """
import json
import os
import signal
import subprocess
import sys
import time

payload = json.loads(sys.argv[1])
time.sleep(float(payload["delay"]))
domain = payload["domain"]
repo_root = payload["repo_root"]
agents = payload["agents"]
action = payload["action"]
manual = payload["manual"]

def run(*args):
    subprocess.run(["launchctl", *args], capture_output=True, text=True, check=False)

def listener_pids(port):
    result = subprocess.run(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"], capture_output=True, text=True, check=False)
    return [int(line.strip()) for line in (result.stdout or "").splitlines() if line.strip().isdigit()]

def matching_pids(markers):
    result = subprocess.run(["ps", "-axo", "pid=,command="], capture_output=True, text=True, check=False)
    items = []
    for raw in (result.stdout or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        parts = line.split(None, 1)
        if not parts or not parts[0].isdigit():
            continue
        pid = int(parts[0])
        command = parts[1] if len(parts) > 1 else ""
        if any(marker in command for marker in markers):
            items.append(pid)
    return items

def stop_manual(item):
    pids = listener_pids(int(item["port"])) if item["kind"] == "port_listener" else matching_pids(item["markers"])
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass

def start_manual(item):
    log_path = item["log"]
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    with open(log_path, "a", encoding="utf-8") as handle:
        subprocess.Popen(item["argv"], cwd=repo_root, stdout=handle, stderr=handle, stdin=subprocess.DEVNULL, start_new_session=True)

if action == "stop_system":
    for item in reversed(agents):
        run("bootout", domain, f"{domain}/{item['label']}")
    for item in reversed(manual):
        stop_manual(item)
elif action == "restart_system":
    for item in agents:
        run("kickstart", "-k", f"{domain}/{item['label']}")
    for item in manual:
        stop_manual(item)
    time.sleep(0.5)
    for item in manual:
        start_manual(item)
"""
    subprocess.Popen(
        [sys.executable, "-c", script, json.dumps(payload, sort_keys=False)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )


def control_system(action: str) -> Dict[str, Any]:
    allowed = {"start_system", "stop_system", "restart_system"}
    if action not in allowed:
        raise ValueError(f"unsupported action: {action}")
    domain = _launchctl_domain()
    agents = _launch_agent_specs()
    manual_specs = _manual_service_specs()
    autopilot_agent = next((item for item in agents if item["name"] == "autopilot"), None)
    tg_agent = next((item for item in agents if item["name"] == "tg-webhook"), None)
    autopilot_manual = next((item for item in manual_specs if item["name"] == "autopilot"), None)
    tg_manual = next((item for item in manual_specs if item["name"] == "tg-webhook"), None)
    if action == "start_system":
        results = []
        for item in agents:
            if os.path.exists(item["plist"]):
                results.append(_run_launchctl("bootstrap", domain, item["plist"]))
            if _launchctl_service_loaded(item["label"]):
                results.append(_run_launchctl("kickstart", "-k", f"{domain}/{item['label']}"))
        if autopilot_manual and not (autopilot_agent and _launchctl_service_loaded(autopilot_agent["label"])):
            results.append(_start_manual_service(autopilot_manual))
        if tg_manual and not (tg_agent and _launchctl_service_loaded(tg_agent["label"])):
            results.append(_start_manual_service(tg_manual))
        return {
            "ok": True,
            "action": action,
            "message": "Start requested for autopilot and tg-webhook.",
            "results": results,
        }
    results = []
    if autopilot_agent and _launchctl_service_loaded(autopilot_agent["label"]):
        if action == "stop_system":
            results.append(_run_launchctl("bootout", domain, f"{domain}/{autopilot_agent['label']}"))
        else:
            results.append(_run_launchctl("kickstart", "-k", f"{domain}/{autopilot_agent['label']}"))
    elif autopilot_manual:
        if action == "stop_system":
            results.append(_stop_manual_service(autopilot_manual, include_self=False))
        else:
            results.append(_restart_manual_service(autopilot_manual))

    if action == "stop_system":
        _schedule_tg_webhook_control(action)
        message = "Stop requested for autopilot and tg-webhook."
    else:
        _schedule_tg_webhook_control(action)
        message = "Restart requested for autopilot and tg-webhook."
    return {"ok": True, "action": action, "message": message, "results": results}


def system_control_status() -> Dict[str, Any]:
    domain = _launchctl_domain()
    agents = _launch_agent_specs()
    manual_specs = _manual_service_specs()
    items: List[Dict[str, Any]] = []
    for item in agents:
        loaded = _launchctl_service_loaded(item["label"])
        items.append(
            {
                "name": item["name"],
                "manager": "launchd",
                "loaded": loaded,
                "label": item["label"],
                "target": f"{domain}/{item['label']}",
            }
        )
    for item in manual_specs:
        if item["kind"] == "port_listener":
            pids = _listener_pids(int(item["port"]))
        else:
            pids = _matching_pids(list(item["markers"]))
        items.append(
            {
                "name": item["name"],
                "manager": "manual",
                "running": bool(pids),
                "pids": pids,
                "port": item.get("port"),
                "log": str(item["log"]),
            }
        )
    return {"timestamp": utc_now_iso(), "services": items}


def tg_base_url() -> str:
    base = os.getenv("TG_BASE_URL", "https://api.telegram.org").rstrip("/")
    token = os.getenv("TG_BOT_TOKEN")
    if not token:
        raise RuntimeError("TG_BOT_TOKEN is required for Telegram operations.")
    return f"{base}/bot{token}"


def tg_post(method: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    response = requests.post(f"{tg_base_url()}/{method}", json=payload, timeout=20)
    response.raise_for_status()
    data = response.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram API returned error for {method}: {data}")
    return data


def tg_get(method: str, params: Dict[str, Any] | None = None) -> Dict[str, Any]:
    response = requests.get(f"{tg_base_url()}/{method}", params=params, timeout=20)
    response.raise_for_status()
    data = response.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram API returned error for {method}: {data}")
    return data


def callback_data_for(action: str, proposal_id: str) -> str:
    return f"{action}:{proposal_id}"


def risk_summary_for(record: Dict[str, Any]) -> str:
    proposal = record["proposal_json"]
    max_order_usdc = get_env_float("POLY_RISK_MAX_ORDER_USDC", 10.0)
    min_confidence = get_env_float("POLY_RISK_MIN_CONFIDENCE", 0.6)
    max_slippage_bps = get_env_int("POLY_RISK_MAX_SLIPPAGE_BPS", 500)
    return (
        f"Risk gate: size<=${max_order_usdc:.2f}, confidence>={min_confidence:.2f}, "
        f"slippage<={max_slippage_bps}bps, proposal_slippage={proposal['max_slippage_bps']}bps"
    )


def format_message(record: Dict[str, Any]) -> str:
    proposal = record["proposal_json"]
    market = record.get("market") or {}
    context_payload = record.get("context_payload_json") or {}
    summary = (context_payload.get("assembled_text") or "").strip()
    summary_text = summary[:350] + ("..." if len(summary) > 350 else "")
    lines = [
        "Polymarket trade proposal",
        f"Proposal ID: {record['proposal_id']}",
        f"Market: {market.get('question') or proposal['market_id']}",
        f"Link: {market.get('market_url') or 'n/a'}",
        f"Outcome: {proposal['outcome']}",
        f"Confidence: {proposal['confidence_score']:.2f}",
        f"Size (USDC): {proposal['recommended_size_usdc']:.2f}",
        f"Max slippage: {proposal['max_slippage_bps']} bps",
        f"Reasoning: {proposal['reasoning']}",
        risk_summary_for(record),
    ]
    if summary_text:
        lines.append(f"Context: {summary_text}")
    lines.append("Decision required: explicit Approve or Reject.")
    return "\n".join(lines)


def send_proposals(proposal_ids: List[str], chat_id: str, dry_run: bool = False, conn=None) -> Dict[str, Any]:
    results = []
    _own_conn = conn is None
    if _own_conn:
        conn = connect_db()
    try:
        for proposal_id in proposal_ids:
            record = proposal_record(conn, proposal_id)
            if record is None:
                raise RuntimeError(f"Proposal {proposal_id} not found.")
            if record["status"] not in {"pending_approval", "approved", "rejected"}:
                raise RuntimeError(f"Proposal {proposal_id} is in status '{record['status']}', expected pending_approval.")
            # Compute approval deadline
            market = record.get("market") or {}
            seconds_to_expiry = market.get("seconds_to_expiry")
            approval_ttl = clamp_approval_ttl(record.get("approval_ttl_seconds"), seconds_to_expiry)
            now = utc_now_iso()
            now_dt = parse_iso8601(now)
            expires_at = (now_dt + timedelta(seconds=approval_ttl)).strftime("%Y-%m-%dT%H:%M:%SZ")
            message_text = format_message(record) + f"\n\nExpires in {approval_ttl}s — approve before {expires_at} UTC"
            if dry_run:
                telegram_result = {"message_id": None, "dry_run": True}
            else:
                payload = {
                    "chat_id": chat_id,
                    "text": message_text,
                    "reply_markup": {
                        "inline_keyboard": [
                            [
                                {"text": "Approve", "callback_data": callback_data_for("approve", proposal_id)},
                                {"text": "Reject", "callback_data": callback_data_for("reject", proposal_id)},
                            ]
                        ]
                    },
                }
                telegram_result = tg_post("sendMessage", payload)["result"]
            msg_id = str(telegram_result.get("message_id") or "") if not dry_run else None
            update_proposal_workflow_fields(
                conn,
                proposal_id,
                approval_requested_at=now,
                approval_expires_at=expires_at,
                telegram_message_id=msg_id,
                telegram_chat_id=str(chat_id),
            )
            conn.commit()
            event = {
                "timestamp": now,
                "type": "proposal_sent",
                "proposal_id": proposal_id,
                "status": record["status"],
                "chat_id": str(chat_id),
                "approval_ttl_seconds": approval_ttl,
                "approval_expires_at": expires_at,
                "telegram": telegram_result,
            }
            append_jsonl(debug_events_path("approvals"), event)
            results.append(event)
    finally:
        if _own_conn:
            conn.close()
    return {"timestamp": utc_now_iso(), "sent_count": len(results), "events": results}


def update_decision(action: str, proposal_id: str, callback_query: Dict[str, Any]) -> Dict[str, Any]:
    callback_id = str(callback_query.get("id") or "")
    with connect_db() as conn:
        existing = approval_by_callback(conn, callback_id)
        if existing is not None:
            record = proposal_record(conn, proposal_id)
            return {"proposal_id": proposal_id, "status": record["status"], "approval": existing}
        record = proposal_record(conn, proposal_id)
        if record is None:
            raise KeyError(f"Unknown proposal_id: {proposal_id}")
        if record["status"] in {"approved", "rejected"} and record.get("approval"):
            return {"proposal_id": proposal_id, "status": record["status"], "approval": record["approval"]}
        user = callback_query.get("from", {})
        message = callback_query.get("message", {})
        status = "approved" if action == "approve" else "rejected"
        approval = record_approval(
            conn,
            proposal_id=proposal_id,
            decision=status,
            decided_at=utc_now_iso(),
            telegram_user_id=str(user.get("id")) if user.get("id") is not None else None,
            telegram_username=user.get("username"),
            callback_query_id=callback_id,
            telegram_message_id=str(message.get("message_id")) if message.get("message_id") is not None else None,
            raw_callback_json=callback_query,
        )
        conn.commit()
        event = {
            "timestamp": utc_now_iso(),
            "type": "decision_recorded",
            "proposal_id": proposal_id,
            "status": status,
            "callback_query_id": callback_id,
        }
        append_jsonl(debug_events_path("approvals"), event)
        return {"proposal_id": proposal_id, "status": status, "approval": approval}


def _auto_execute_mode() -> str:
    mode = (os.getenv("TG_AUTO_EXECUTE_MODE", "real") or "").strip().lower()
    if mode not in {"mock", "real"}:
        raise RuntimeError(f"TG_AUTO_EXECUTE_MODE must be 'mock' or 'real', got: {mode!r}")
    return mode


def auto_execute_approved_proposal(proposal_id: str) -> Dict[str, Any]:
    if not get_env_bool("TG_AUTO_EXECUTE_ON_APPROVE", True):
        return {"enabled": False, "executed": False, "reason": "disabled_by_env"}
    mode = _auto_execute_mode()
    with connect_db() as conn:
        record = proposal_record(conn, proposal_id)
        if record is None:
            raise RuntimeError(f"proposal not found: {proposal_id}")
        if (record.get("approval") or {}).get("decision") != "approved":
            return {"enabled": True, "executed": False, "reason": "not_approved", "mode": mode}

        # Webhook callbacks can be retried by Telegram; avoid duplicate real orders.
        previous = latest_execution(conn, proposal_id, mode=mode)
        if previous and previous.get("status") in {"filled", "submitted", "live"}:
            return {"enabled": True, "executed": False, "reason": "already_executed", "mode": mode, "existing_execution_id": previous["id"]}

        execution = execute_record(conn, record, mode=mode, session_state={"cumulative_spend_usdc": 0.0})
        stored = record_execution(conn, execution)
        conn.commit()
        return {
            "enabled": True,
            "executed": True,
            "mode": mode,
            "execution_id": stored.get("id"),
            "execution_status": stored.get("status"),
            "order_id": stored.get("txhash_or_order_id"),
            "error_message": stored.get("error_message"),
        }


def expire_stale_proposals(conn=None) -> List[Dict[str, Any]]:
    """Sweep pending_approval proposals past their deadline. Returns list of expired entries."""
    own_conn = conn is None
    if own_conn:
        conn = connect_db()
    try:
        expired = list_expired_pending_proposals(conn)
        results = []
        for record in expired:
            update_proposal_status(conn, record["proposal_id"], "expired")
            append_jsonl(debug_events_path("approvals"), {
                "timestamp": utc_now_iso(),
                "type": "proposal_expired",
                "proposal_id": record["proposal_id"],
                "approval_expires_at": record.get("approval_expires_at"),
            })
            # Release the SQLite write lock before making Telegram API calls.
            conn.commit()
            if record.get("telegram_message_id") and record.get("telegram_chat_id"):
                try:
                    tg_post("editMessageText", {
                        "chat_id": record["telegram_chat_id"],
                        "message_id": record["telegram_message_id"],
                        "text": format_message(record) + "\n\n--- EXPIRED ---",
                        "reply_markup": {"inline_keyboard": []},
                    })
                except Exception:
                    pass
            results.append({"proposal_id": record["proposal_id"], "action": "expired"})
        return results
    finally:
        if own_conn:
            conn.close()


def create_app() -> Flask:
    init_db()
    app = Flask(__name__)

    @app.get("/healthz")
    def healthz():
        return jsonify({"ok": True, "timestamp": utc_now_iso()})

    @app.get("/decisions/<proposal_id>")
    def get_decision(proposal_id: str):
        with connect_db() as conn:
            record = proposal_record(conn, proposal_id)
        if record is None:
            return jsonify({"proposal_id": proposal_id, "status": "missing"}), 404
        return jsonify({"proposal_id": proposal_id, "status": record["status"], "approval": record["approval"]})

    @app.get("/api/ops/status")
    def ops_status():
        with connect_db() as conn:
            return jsonify(build_ops_snapshot(conn))

    @app.get("/api/ops/proposals")
    def ops_proposals():
        with connect_db() as conn:
            snapshot = build_ops_snapshot(conn)
        return jsonify(
            {
                "timestamp": snapshot["timestamp"],
                "pending_count": snapshot["pending_count"],
                "pending_approvals": snapshot["pending_approvals"],
                "recent_decisions": snapshot["recent_decisions"],
            }
        )

    @app.get("/api/ops/failures")
    def ops_failures():
        with connect_db() as conn:
            snapshot = build_ops_snapshot(conn)
        return jsonify({"timestamp": snapshot["timestamp"], "recent_failures": snapshot["recent_failures"]})

    @app.get("/api/ops/events")
    def ops_events():
        with connect_db() as conn:
            snapshot = build_ops_snapshot(conn)
        return jsonify({"timestamp": snapshot["timestamp"], "recent_events": snapshot["recent_events"]})

    @app.post("/api/ops/system-control")
    def ops_system_control():
        payload = request.get_json(force=True, silent=True) or {}
        action = str(payload.get("action") or "").strip()
        try:
            result = control_system(action)
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 500
        return jsonify(result)

    @app.get("/api/ops/pnl-history")
    def ops_pnl_history():
        with connect_db() as conn:
            rows = conn.execute(
                """
                SELECT updated_at AS ts, realized_pnl
                FROM positions
                WHERE status = 'resolved' AND mode = 'real' AND realized_pnl IS NOT NULL
                ORDER BY updated_at ASC
                """
            ).fetchall()
        points = []
        cumulative = 0.0
        for r in rows:
            try:
                pnl = float(r["realized_pnl"] or 0.0)
            except Exception:
                pnl = 0.0
            cumulative += pnl
            points.append({"ts": r["ts"], "realized_pnl": pnl, "cumulative_pnl": cumulative})
        return jsonify({"points": points})

    @app.get("/api/ops/heartbeat-history")
    def ops_heartbeat_history():
        with connect_db() as conn:
            rows = conn.execute(
                """
                SELECT loop_name, started_at, items_processed, error_message
                FROM (
                    SELECT loop_name, started_at, items_processed, error_message,
                           ROW_NUMBER() OVER (PARTITION BY loop_name ORDER BY id DESC) AS rn
                    FROM autopilot_heartbeats
                ) ranked
                WHERE rn <= 20
                ORDER BY loop_name ASC, started_at ASC
                """
            ).fetchall()
        loops: Dict[str, List[Dict[str, Any]]] = {}
        for r in rows:
            loops.setdefault(r["loop_name"], []).append({
                "ts": r["started_at"],
                "items": int(r["items_processed"] or 0),
                "error": bool(r["error_message"]),
            })
        return jsonify({"loops": loops})

    @app.get("/api/ops/stats")
    def ops_stats():
        with connect_db() as conn:
            prop = {r["status"]: int(r["c"]) for r in conn.execute(
                "SELECT status, COUNT(*) AS c FROM proposals GROUP BY status"
            ).fetchall()}
            execs = {r["status"]: int(r["c"]) for r in conn.execute(
                "SELECT status, COUNT(*) AS c FROM executions GROUP BY status"
            ).fetchall()}
            pos = {r["status"]: int(r["c"]) for r in conn.execute(
                "SELECT status, COUNT(*) AS c FROM positions GROUP BY status"
            ).fetchall()}
            try:
                failures = {r["verdict"]: int(r["c"]) for r in conn.execute(
                    "SELECT verdict, COUNT(*) AS c FROM agent_reviews GROUP BY verdict"
                ).fetchall()}
            except Exception:
                failures = {}
        return jsonify({
            "proposals_by_status": prop,
            "executions_by_status": execs,
            "positions_by_status": pos,
            "failure_buckets": failures,
        })

    @app.get("/ops")
    def ops_dashboard():
        with connect_db() as conn:
            snapshot = build_ops_snapshot(conn)
        return render_template_string(
            OPS_DASHBOARD_TEMPLATE,
            initial_json=_dashboard_initial_json(snapshot),
        )

    @app.post("/telegram/webhook")
    def telegram_webhook():
        secret_header = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
        expected_secret = os.getenv("TG_WEBHOOK_SECRET")
        if expected_secret and secret_header != expected_secret:
            return jsonify({"ok": False, "error": "invalid webhook secret"}), 403

        update = request.get_json(force=True, silent=False)
        callback_query = update.get("callback_query")
        if not callback_query:
            append_jsonl(debug_events_path("approvals"), {"timestamp": utc_now_iso(), "type": "webhook_update_ignored", "update": update})
            return jsonify({"ok": True, "ignored": True})

        data = callback_query.get("data", "")
        if ":" not in data:
            return jsonify({"ok": False, "error": "invalid callback payload"}), 400
        action, proposal_id = data.split(":", 1)
        if action not in {"approve", "reject"}:
            return jsonify({"ok": False, "error": "invalid action"}), 400

        # Reject late callbacks for expired proposals
        with connect_db() as conn:
            pre_record = proposal_record(conn, proposal_id)
        if pre_record and pre_record.get("approval_expires_at"):
            if utc_now_iso() > pre_record["approval_expires_at"]:
                try:
                    tg_post("answerCallbackQuery", {
                        "callback_query_id": callback_query["id"],
                        "text": "This proposal has expired.",
                    })
                except Exception:
                    pass
                append_jsonl(debug_events_path("approvals"), {
                    "timestamp": utc_now_iso(),
                    "type": "late_callback_rejected",
                    "proposal_id": proposal_id,
                    "approval_expires_at": pre_record["approval_expires_at"],
                })
                return jsonify({"ok": False, "reason": "expired", "proposal_id": proposal_id})

        result = update_decision(action, proposal_id, callback_query)
        auto_exec = None
        if result.get("status") == "approved":
            try:
                auto_exec = auto_execute_approved_proposal(proposal_id)
                append_jsonl(
                    debug_events_path("approvals"),
                    {
                        "timestamp": utc_now_iso(),
                        "type": "auto_execute_attempt",
                        "proposal_id": proposal_id,
                        "auto_execute": auto_exec,
                    },
                )
            except Exception as exc:
                auto_exec = {"enabled": True, "executed": False, "reason": "exception", "error": str(exc)}
                append_jsonl(
                    debug_events_path("approvals"),
                    {
                        "timestamp": utc_now_iso(),
                        "type": "auto_execute_failed",
                        "proposal_id": proposal_id,
                        "error": str(exc),
                    },
                )
        try:
            callback_text = f"Decision recorded: {result['status']}"
            if result["status"] == "approved" and isinstance(auto_exec, dict):
                if auto_exec.get("executed") and auto_exec.get("execution_status") in {"submitted", "live", "filled"}:
                    callback_text = f"Approved. Execution status: {auto_exec.get('execution_status')}"
                elif auto_exec.get("error_message"):
                    callback_text = f"Approved, but execution failed: {auto_exec['error_message'][:160]}"
                elif auto_exec.get("reason"):
                    callback_text = f"Approved, but execution skipped: {auto_exec['reason']}"
            tg_post(
                "answerCallbackQuery",
                {
                    "callback_query_id": callback_query["id"],
                    "text": callback_text,
                },
            )
            message = callback_query.get("message", {})
            if message:
                tg_post(
                    "editMessageReplyMarkup",
                    {
                        "chat_id": message["chat"]["id"],
                        "message_id": message["message_id"],
                        "reply_markup": {"inline_keyboard": []},
                    },
                )
        except Exception as exc:
            append_jsonl(
                debug_events_path("approvals"),
                {
                    "timestamp": utc_now_iso(),
                    "type": "telegram_followup_failed",
                    "proposal_id": proposal_id,
                    "error": str(exc),
                },
            )
        return jsonify({"ok": True, "proposal_id": proposal_id, "status": result["status"], "auto_execute": auto_exec})

    return app


def wait_for_decisions(proposal_ids: List[str], timeout_seconds: int, poll_interval: int) -> Dict[str, Any]:
    deadline = time.time() + timeout_seconds
    while True:
        with connect_db() as conn:
            states = decision_status_for(conn, proposal_ids)
        statuses = {state["proposal_id"]: state.get("status", "missing") for state in states}
        all_approved = all(status == "approved" for status in statuses.values()) and bool(statuses)
        any_rejected = any(status == "rejected" for status in statuses.values())
        if all_approved or any_rejected or time.time() >= deadline:
            summary_status = "approved" if all_approved else "rejected" if any_rejected else "pending"
            return {
                "timestamp": utc_now_iso(),
                "approved": all_approved,
                "status": summary_status,
                "proposal_ids": proposal_ids,
                "decisions": states,
            }
        time.sleep(poll_interval)


def normalize_webhook_url(raw_url: str) -> str:
    return raw_url.rstrip("/") + "/telegram/webhook"


def set_webhook(webhook_url: str, drop_pending_updates: bool = False) -> Dict[str, Any]:
    payload: Dict[str, Any] = {"url": normalize_webhook_url(webhook_url)}
    secret = os.getenv("TG_WEBHOOK_SECRET")
    if secret:
        payload["secret_token"] = secret
    if drop_pending_updates:
        payload["drop_pending_updates"] = True
    result = tg_post("setWebhook", payload)
    return {
        "timestamp": utc_now_iso(),
        "webhook_url": payload["url"],
        "secret_configured": bool(secret),
        "drop_pending_updates": drop_pending_updates,
        "telegram": result.get("result", result),
    }


def get_webhook_info() -> Dict[str, Any]:
    result = tg_get("getWebhookInfo")
    return {"timestamp": utc_now_iso(), "telegram": result.get("result", result)}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Telegram approval gate backed by SQLite.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    send_parser = subparsers.add_parser("send", help="Send proposals to Telegram.")
    send_parser.add_argument("--proposal-file", required=True, help="Proposal JSON file.")
    send_parser.add_argument("--chat-id", help="Telegram chat id. Defaults to TG_CHAT_ID.")
    send_parser.add_argument("--output", help="Optional file path for send summary.")
    send_parser.add_argument("--dry-run", action="store_true", help="Skip Telegram API calls.")

    await_parser = subparsers.add_parser("await", help="Wait for approval or rejection.")
    await_parser.add_argument("--proposal-file", required=True, help="Proposal JSON file.")
    await_parser.add_argument("--timeout-seconds", type=int, default=1800, help="Maximum wait time.")
    await_parser.add_argument("--poll-interval", type=int, default=5, help="Polling interval.")
    await_parser.add_argument("--output", help="Optional file path for decision summary.")

    status_parser = subparsers.add_parser("status", help="Show current decision state for proposals.")
    status_parser.add_argument("--proposal-file", required=True, help="Proposal JSON file.")
    status_parser.add_argument("--output", help="Optional file path for decision summary.")

    serve_parser = subparsers.add_parser("serve", help="Serve webhook callback handler.")
    serve_parser.add_argument("--host", default="0.0.0.0", help="Bind address.")
    serve_parser.add_argument("--port", type=int, default=8787, help="Bind port.")

    set_webhook_parser = subparsers.add_parser("set-webhook", help="Register the Telegram webhook URL.")
    set_webhook_parser.add_argument("--webhook-url", required=True, help="Public base URL, for example https://abc.ngrok.app")
    set_webhook_parser.add_argument("--drop-pending-updates", action="store_true")
    set_webhook_parser.add_argument("--output", help="Optional file path for webhook registration summary.")

    webhook_info_parser = subparsers.add_parser("webhook-info", help="Show Telegram webhook configuration.")
    webhook_info_parser.add_argument("--output", help="Optional file path for webhook info.")

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    init_db()

    if args.command == "serve":
        app = create_app()
        app.run(host=args.host, port=args.port)
        return 0

    if args.command == "set-webhook":
        result = set_webhook(args.webhook_url, drop_pending_updates=args.drop_pending_updates)
        print(dump_json(result, path=args.output))
        return 0

    if args.command == "webhook-info":
        result = get_webhook_info()
        print(dump_json(result, path=args.output))
        return 0

    proposal_ids = [proposal_id_for(proposal) for proposal in read_proposals(args.proposal_file)]

    if args.command == "send":
        chat_id = args.chat_id or os.getenv("TG_CHAT_ID")
        if not chat_id:
            raise RuntimeError("A Telegram chat id is required via --chat-id or TG_CHAT_ID.")
        result = send_proposals(proposal_ids, chat_id=str(chat_id), dry_run=args.dry_run)
        print(dump_json(result, path=args.output))
        return 0

    if args.command == "await":
        result = wait_for_decisions(proposal_ids, args.timeout_seconds, args.poll_interval)
        print(dump_json(result, path=args.output))
        return 0

    if args.command == "status":
        with connect_db() as conn:
            result = {"timestamp": utc_now_iso(), "decisions": decision_status_for(conn, proposal_ids)}
        print(dump_json(result, path=args.output))
        return 0

    raise RuntimeError(f"Unhandled command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
