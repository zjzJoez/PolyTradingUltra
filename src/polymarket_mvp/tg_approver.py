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
  <title>Polymarket Ops</title>
  <style>
    :root {
      --bg: #f3efe6;
      --bg-accent: #e6dfd2;
      --panel: rgba(255, 252, 247, 0.92);
      --panel-2: #f7f1e8;
      --border: #d7cbb8;
      --border-strong: #bfae95;
      --text: #1f1b16;
      --muted: #6b6256;
      --accent: #0b5fff;
      --good: #0f8a5f;
      --warn: #b57617;
      --bad: #c53c4c;
      --shadow: 0 18px 40px rgba(81, 57, 24, 0.08);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background:
        radial-gradient(circle at top left, rgba(255,255,255,0.8), transparent 28%),
        radial-gradient(circle at top right, rgba(214,194,160,0.35), transparent 32%),
        linear-gradient(180deg, var(--bg) 0%, var(--bg-accent) 100%);
      color: var(--text);
      font: 14px/1.5 "SF Mono", "IBM Plex Mono", Menlo, Monaco, Consolas, monospace;
    }
    a { color: var(--accent); text-decoration: none; }
    a:hover { text-decoration: underline; }
    .page { max-width: 1580px; margin: 0 auto; padding: 24px; }
    .header {
      display: grid;
      grid-template-columns: minmax(0, 1.5fr) minmax(260px, 0.8fr);
      gap: 16px;
      margin-bottom: 18px;
    }
    .hero, .meta-card {
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 20px;
      box-shadow: var(--shadow);
    }
    .hero {
      padding: 22px;
      background:
        linear-gradient(135deg, rgba(255,255,255,0.88), rgba(246,239,228,0.92)),
        var(--panel);
    }
    .meta-card {
      padding: 18px;
      display: flex;
      flex-direction: column;
      justify-content: space-between;
      gap: 14px;
    }
    .eyebrow {
      text-transform: uppercase;
      letter-spacing: 0.12em;
      font-size: 11px;
      color: var(--muted);
      margin-bottom: 10px;
    }
    .title h1 { margin: 0 0 8px; font-size: 34px; line-height: 1.1; }
    .title p, .meta { margin: 0; color: var(--muted); }
    .hero-metrics {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 10px;
      margin-top: 18px;
    }
    .grid { display: grid; grid-template-columns: repeat(12, 1fr); gap: 16px; }
    .panel {
      background: linear-gradient(180deg, rgba(255,255,255,0.68), rgba(255,255,255,0.52)), var(--panel);
      border: 1px solid var(--border);
      border-radius: 18px;
      padding: 16px;
      min-height: 120px;
      box-shadow: var(--shadow);
    }
    .panel-head {
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      gap: 12px;
      margin-bottom: 12px;
    }
    .panel h2 { margin: 0; font-size: 15px; }
    .panel-copy { margin: 4px 0 0; color: var(--muted); font-size: 12px; }
    .span-12 { grid-column: span 12; }
    .span-8 { grid-column: span 8; }
    .span-6 { grid-column: span 6; }
    .span-4 { grid-column: span 4; }
    .span-3 { grid-column: span 3; }
    .cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 10px; }
    .card {
      background: var(--panel-2);
      border: 1px solid var(--border);
      border-radius: 14px;
      padding: 12px;
      min-width: 0;
    }
    .card .name {
      color: var(--muted);
      font-size: 11px;
      margin-bottom: 8px;
      text-transform: uppercase;
      letter-spacing: 0.08em;
    }
    .badge {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      border-radius: 999px;
      padding: 3px 9px;
      font-size: 11px;
      border: 1px solid currentColor;
      background: rgba(255,255,255,0.55);
      text-transform: uppercase;
      letter-spacing: 0.04em;
    }
    .green { color: var(--good); }
    .yellow { color: var(--warn); }
    .red { color: var(--bad); }
    .metric { font-size: 24px; font-weight: 700; line-height: 1.1; }
    .metric.small-metric { font-size: 18px; }
    .metric-label { color: var(--muted); font-size: 12px; }
    .table-shell {
      overflow-x: auto;
      border: 1px solid var(--border);
      border-radius: 14px;
      background: rgba(255,255,255,0.42);
    }
    table { width: 100%; border-collapse: collapse; min-width: 760px; }
    th, td {
      text-align: left;
      padding: 10px 10px;
      border-top: 1px solid var(--border);
      vertical-align: top;
    }
    th {
      color: var(--muted);
      font-weight: 600;
      font-size: 11px;
      text-transform: uppercase;
      letter-spacing: 0.06em;
      background: rgba(247,241,232,0.7);
      position: sticky;
      top: 0;
      z-index: 1;
    }
    tbody tr:hover { background: rgba(255,255,255,0.38); }
    .empty { color: var(--muted); padding: 10px 0; }
    .attention-item {
      padding: 10px 0;
      border-top: 1px solid var(--border);
    }
    .attention-item:first-child { border-top: 0; padding-top: 0; }
    .attention-title { font-weight: 700; margin-bottom: 4px; }
    .small { font-size: 12px; color: var(--muted); }
    .tiny { font-size: 11px; color: var(--muted); }
    .mono { font-variant-ligatures: none; }
    .mono-wrap {
      font-variant-ligatures: none;
      overflow-wrap: anywhere;
      word-break: break-word;
    }
    .truncate-2 {
      display: -webkit-box;
      -webkit-line-clamp: 2;
      -webkit-box-orient: vertical;
      overflow: hidden;
    }
    .cell-stack { display: flex; flex-direction: column; gap: 4px; min-width: 0; }
    .cell-main { font-weight: 600; overflow-wrap: anywhere; }
    .cell-sub { color: var(--muted); font-size: 12px; overflow-wrap: anywhere; }
    .table-meta {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin-bottom: 10px;
    }
    .pill-row { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 8px; }
    .pill {
      border: 1px solid var(--border);
      border-radius: 999px;
      padding: 4px 8px;
      color: var(--muted);
      background: rgba(255,255,255,0.6);
      min-width: 0;
      overflow-wrap: anywhere;
    }
    .status-chip {
      display: inline-flex;
      align-items: center;
      border: 1px solid var(--border-strong);
      border-radius: 999px;
      padding: 4px 9px;
      font-size: 11px;
      background: rgba(255,255,255,0.58);
      color: var(--text);
    }
    .muted-block {
      border: 1px dashed var(--border-strong);
      border-radius: 14px;
      padding: 12px;
      background: rgba(255,255,255,0.45);
    }
    .control-actions {
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      margin-top: 12px;
    }
    .control-button {
      appearance: none;
      border: 1px solid var(--border-strong);
      border-radius: 12px;
      padding: 10px 14px;
      background: rgba(255,255,255,0.72);
      color: var(--text);
      font: inherit;
      cursor: pointer;
    }
    .control-button:hover { background: rgba(255,255,255,0.92); }
    .control-button.red {
      border-color: rgba(197, 60, 76, 0.45);
      color: var(--bad);
    }
    .control-button.yellow {
      border-color: rgba(181, 118, 23, 0.45);
      color: var(--warn);
    }
    .control-log {
      margin-top: 10px;
      padding: 10px 12px;
      border-radius: 12px;
      border: 1px solid var(--border);
      background: rgba(255,255,255,0.48);
      min-height: 42px;
    }
    .section-kpis {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
      gap: 10px;
      margin-bottom: 12px;
    }
    @media (max-width: 1100px) {
      .span-8, .span-6, .span-4, .span-3 { grid-column: span 12; }
      .header { grid-template-columns: 1fr; }
      .hero-metrics { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .meta { margin-top: 10px; }
    }
    @media (max-width: 720px) {
      .page { padding: 16px; }
      .hero { padding: 18px; }
      .hero-metrics { grid-template-columns: 1fr; }
      table { min-width: 640px; }
    }
  </style>
</head>
<body>
  <div class="page">
    <div class="header">
      <div class="hero">
        <div class="eyebrow">Ops Console</div>
        <div class="title">
        <h1>Polymarket Ops</h1>
          <p>Readable at a glance: approvals, live execution, open positions, and the few things that actually need intervention.</p>
        </div>
        <div id="hero-metrics" class="hero-metrics"></div>
      </div>
      <div class="meta-card">
        <div>
          <div class="eyebrow">Snapshot</div>
          <div class="metric small-metric" id="snapshot-status">Loading...</div>
          <p class="meta" id="header-meta"></p>
        </div>
        <div class="muted-block">
          <div class="tiny">Dashboard</div>
          <div class="small">Auto-refreshes every 5 seconds. Long text is clamped and IDs wrap instead of blowing out the layout.</div>
        </div>
      </div>
    </div>

    <div class="grid">
      <section class="panel span-4">
        <div class="panel-head">
          <div>
            <h2>Needs Attention</h2>
            <p class="panel-copy">Most urgent operational items.</p>
          </div>
        </div>
        <div id="needs-attention"></div>
      </section>

      <section class="panel span-8">
        <div class="panel-head">
          <div>
            <h2>Control State</h2>
            <p class="panel-copy">Current agent binding, loop cadence, and kill switches.</p>
          </div>
        </div>
        <div id="control-state"></div>
        <div class="control-actions">
          <button class="control-button" data-action="start_system">Start System</button>
          <button class="control-button yellow" data-action="restart_system">Restart System</button>
          <button class="control-button red" data-action="stop_system">Stop System</button>
        </div>
        <div id="control-log" class="control-log small">Launchd control for `autopilot` and `tg-webhook`.</div>
      </section>

      <section class="panel span-12">
        <div class="panel-head">
          <div>
            <h2>System Health</h2>
            <p class="panel-copy">Heartbeat age, loop duration, and latest top-level errors.</p>
          </div>
        </div>
        <div id="system-health" class="cards"></div>
      </section>

      <section class="panel span-12">
        <div class="panel-head">
          <div>
            <h2>Pending Approvals</h2>
            <p class="panel-copy">Human decisions waiting in Telegram, sorted by urgency.</p>
          </div>
        </div>
        <div id="pending-approvals"></div>
      </section>

      <section class="panel span-6">
        <div class="panel-head">
          <div>
            <h2>Live Orders</h2>
            <p class="panel-copy">Submitted or working orders with age and remaining TTL.</p>
          </div>
        </div>
        <div id="live-orders"></div>
      </section>

      <section class="panel span-6">
        <div class="panel-head">
          <div>
            <h2>Recent Decisions</h2>
            <p class="panel-copy">Latest proposal outcomes and why they landed there.</p>
          </div>
        </div>
        <div id="recent-decisions"></div>
      </section>

      <section class="panel span-12">
        <div class="panel-head">
          <div>
            <h2>Open Positions</h2>
            <p class="panel-copy">Current inventory with entry, mark, and PnL.</p>
          </div>
        </div>
        <div id="open-positions"></div>
      </section>

      <section class="panel span-6">
        <div class="panel-head">
          <div>
            <h2>Recent Failures</h2>
            <p class="panel-copy">Recent execution, reconcile, risk, heartbeat, or Telegram failures.</p>
          </div>
        </div>
        <div id="recent-failures"></div>
      </section>

      <section class="panel span-6">
        <div class="panel-head">
          <div>
            <h2>Recent Events</h2>
            <p class="panel-copy">Raw operational events for quick spot checks.</p>
          </div>
        </div>
        <div id="recent-events"></div>
      </section>
    </div>
  </div>

  <script id="initial-ops-data" type="application/json">{{ initial_json|safe }}</script>
  <script>
    const initial = JSON.parse(document.getElementById("initial-ops-data").textContent);

    function fmtRelative(seconds) {
      if (seconds === null || seconds === undefined) return "n/a";
      const abs = Math.abs(seconds);
      if (abs < 60) return `${seconds}s`;
      if (abs < 3600) return `${Math.round(seconds / 60)}m`;
      if (abs < 86400) return `${Math.round(seconds / 3600)}h`;
      return `${Math.round(seconds / 86400)}d`;
    }

    function fmtTimestamp(value) {
      if (!value) return "n/a";
      const parsed = new Date(value);
      if (Number.isNaN(parsed.getTime())) return escapeHtml(String(value));
      return escapeHtml(parsed.toLocaleString());
    }

    function escapeHtml(value) {
      return String(value ?? "")
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#39;");
    }

    function escapeAttr(value) {
      return escapeHtml(value);
    }

    function toNumberOrNull(value) {
      if (value === null || value === undefined || value === "") return null;
      const parsed = Number(value);
      return Number.isFinite(parsed) ? parsed : null;
    }

    function fmtMoney(value) {
      const parsed = toNumberOrNull(value);
      return parsed === null ? "n/a" : `$${parsed.toFixed(2)}`;
    }

    function fmtSignedMoney(value) {
      const parsed = toNumberOrNull(value);
      if (parsed === null) return "n/a";
      const sign = parsed > 0 ? "+" : "";
      return `${sign}${parsed.toFixed(2)}`;
    }

    function fmtDecimal(value, digits = 2) {
      const parsed = toNumberOrNull(value);
      return parsed === null ? "n/a" : parsed.toFixed(digits);
    }

    function fmtPrice(value) {
      const parsed = toNumberOrNull(value);
      return parsed === null ? "n/a" : parsed.toFixed(3);
    }

    function toneClass(value, positiveGood = false) {
      const parsed = toNumberOrNull(value);
      if (parsed === null || parsed === 0) return "";
      if (positiveGood) return parsed > 0 ? "green" : "red";
      return parsed < 0 ? "red" : "yellow";
    }

    function statusTone(value) {
      const normalized = String(value || "").toLowerCase();
      if (!normalized) return "";
      if (["approved", "filled", "open", "live", "submitted", "healthy", "green"].includes(normalized)) return "green";
      if (["pending_approval", "partial_fill", "yellow"].includes(normalized)) return "yellow";
      if (["failed", "error", "rejected", "expired", "risk_blocked", "red"].includes(normalized)) return "red";
      return "";
    }

    function safeLink(url, label) {
      if (!url) return escapeHtml(label);
      return `<a href="${escapeAttr(url)}" target="_blank" rel="noreferrer">${escapeHtml(label)}</a>`;
    }

    function emptyState(message) {
      return `<div class="empty">${escapeHtml(message)}</div>`;
    }

    function renderTable(columns, rows, emptyMessage = "No rows.") {
      if (!rows.length) return emptyState(emptyMessage);
      const head = `<tr>${columns.map(c => `<th>${escapeHtml(c.label)}</th>`).join("")}</tr>`;
      const body = rows.map(row => `<tr>${columns.map(c => `<td>${c.render(row)}</td>`).join("")}</tr>`).join("");
      return `<div class="table-shell"><table><thead>${head}</thead><tbody>${body}</tbody></table></div>`;
    }

    function renderHeroMetrics(data) {
      const metrics = [
        { label: "Pending", value: data.pending_count || 0, tone: data.pending_count ? "yellow" : "green" },
        { label: "Live Orders", value: data.live_order_count || 0, tone: data.live_order_count ? "yellow" : "green" },
        { label: "Open Positions", value: data.open_position_count || 0, tone: data.open_position_count ? "yellow" : "green" },
        { label: "Failures", value: (data.recent_failures || []).length, tone: (data.recent_failures || []).length ? "red" : "green" },
      ];
      return metrics.map(item => `
        <div class="card">
          <div class="name">${escapeHtml(item.label)}</div>
          <div class="metric ${item.tone === "red" ? "red" : item.tone === "yellow" ? "yellow" : item.tone === "green" ? "green" : ""}">${escapeHtml(item.value)}</div>
        </div>
      `).join("");
    }

    function renderHealth(items) {
      if (!items.length) return emptyState("No heartbeats yet.");
      return items.map(item => `
        <div class="card">
          <div class="name">${escapeHtml(item.loop)}</div>
          <div><span class="badge ${item.health}">${escapeHtml(item.health)}</span></div>
          <div class="metric">${fmtRelative(item.age_seconds)}</div>
          <div class="small">cadence ${fmtRelative(item.cadence_seconds)} | processed ${item.items_processed ?? 0}</div>
          <div class="small">duration ${item.duration_seconds != null ? item.duration_seconds.toFixed(1) + "s" : "n/a"}</div>
          ${item.last_error_text ? `<div class="small red truncate-2 mono-wrap">${escapeHtml(item.last_error_text.slice(0, 220))}</div>` : ""}
        </div>
      `).join("");
    }

    function renderAttention(items) {
      if (!items.length) return emptyState("Nothing urgent.");
      return items.slice(0, 10).map(item => `
        <div class="attention-item">
          <div class="attention-title ${item.severity === "high" ? "red" : "yellow"}">${escapeHtml(item.title || item.kind || "attention")}</div>
          <div class="small">${escapeHtml(item.detail || item.message || "")}</div>
          ${item.timestamp ? `<div class="small">${fmtTimestamp(item.timestamp)}</div>` : ""}
        </div>
      `).join("");
    }

    function renderControlState(control) {
      const kill = (control.kill_switches || []).length
        ? renderTable(
            [
              { label: "Scope", render: row => `<div class="cell-stack"><div class="cell-main mono-wrap">${escapeHtml(`${row.scope_type}:${row.scope_key}`)}</div></div>` },
              { label: "Reason", render: row => `<div class="cell-stack"><div class="cell-sub truncate-2">${escapeHtml(row.reason)}</div></div>` },
              { label: "Created", render: row => `<div class="cell-stack"><div class="cell-sub">${fmtTimestamp(row.created_at)}</div></div>` },
            ],
            control.kill_switches,
            "No active kill switches."
          )
        : emptyState("No active kill switches.");
      const intervals = Object.entries(control.loop_intervals_seconds || {})
        .map(([key, value]) => `<span class="pill">${escapeHtml(key)} ${fmtRelative(value)}</span>`)
        .join("");
      return `
        <div class="table-meta">
          <span class="status-chip">OpenClaw agent ${escapeHtml(control.openclaw_agent_id || "n/a")}</span>
          <span class="status-chip">auto execute ${escapeHtml(control.tg_auto_execute_mode || "n/a")}</span>
        </div>
        <div class="pill-row">${intervals}</div>
        <div style="margin-top: 12px">${kill}</div>
      `;
    }

    function summarizeEvent(row) {
      const parts = [];
      if (row.status) parts.push(`status ${row.status}`);
      if (row.market) parts.push(String(row.market));
      if (row.outcome) parts.push(`outcome ${row.outcome}`);
      if (row.error) parts.push(`error ${row.error}`);
      if (row.approval_expires_at) parts.push(`expires ${row.approval_expires_at}`);
      if (row.callback_query_id) parts.push(`callback ${row.callback_query_id}`);
      if (row.chat_id) parts.push(`chat ${row.chat_id}`);
      if (!parts.length && row.telegram && row.telegram.text) {
        parts.push(String(row.telegram.text).replace(/\s+/g, " ").slice(0, 180));
      }
      if (!parts.length) parts.push(JSON.stringify(row).slice(0, 180));
      return escapeHtml(parts.join(" | "));
    }

    function renderRecentEvents(items) {
      return renderTable(
        [
          { label: "Type", render: row => `<div class="cell-stack"><div class="cell-main">${escapeHtml(row.type || "event")}</div><div class="cell-sub">${escapeHtml(row.proposal_id || row.chat_id || "")}</div></div>` },
          { label: "When", render: row => `<div class="cell-stack"><div class="cell-main">${fmtTimestamp(row.timestamp)}</div><div class="cell-sub">${row.approval_expires_at ? `expires ${escapeHtml(row.approval_expires_at)}` : ""}</div></div>` },
          { label: "Summary", render: row => `<div class="cell-stack"><div class="cell-sub truncate-2 mono-wrap">${summarizeEvent(row)}</div></div>` },
        ],
        items || [],
        "No recent events."
      );
    }

    function renderSnapshot(data) {
      document.getElementById("snapshot-status").textContent = "Live snapshot";
      document.getElementById("header-meta").textContent =
        `updated ${new Date(data.timestamp).toLocaleString()} | pending ${data.pending_count || 0} | live orders ${data.live_order_count || 0} | open positions ${data.open_position_count || 0}`;
      document.getElementById("hero-metrics").innerHTML = renderHeroMetrics(data);

      document.getElementById("system-health").innerHTML = renderHealth(data.system_health || []);
      document.getElementById("needs-attention").innerHTML = renderAttention(data.needs_attention || []);
      document.getElementById("control-state").innerHTML = renderControlState(data.control_state || {});

      document.getElementById("pending-approvals").innerHTML = renderTable(
        [
          { label: "Proposal", render: row => `<div class="cell-stack"><div class="cell-main mono-wrap">${escapeHtml(row.proposal_id)}</div><div class="cell-sub">${escapeHtml(row.proposal_kind || "entry")}</div></div>` },
          { label: "Market", render: row => `<div class="cell-stack"><div class="cell-main">${safeLink(row.market_url, row.market || "n/a")}</div><div class="cell-sub">${escapeHtml(row.topic || "")}</div></div>` },
          { label: "Trade", render: row => `<div class="cell-stack"><div class="cell-main">${escapeHtml(row.outcome || "n/a")}</div><div class="cell-sub">${fmtMoney(row.size_usdc)} size | conf ${fmtDecimal(row.confidence_score)}</div></div>` },
          { label: "Expires", render: row => `<div class="cell-stack"><div class="cell-main">${fmtTimestamp(row.approval_expires_at)}</div><div class="cell-sub ${row.seconds_remaining != null && row.seconds_remaining <= 60 ? "red" : ""}">${fmtRelative(row.seconds_remaining)}</div></div>` },
        ],
        data.pending_approvals || [],
        "No proposals waiting for approval."
      );

      document.getElementById("live-orders").innerHTML = renderTable(
        [
          { label: "Order", render: row => `<div class="cell-stack"><div class="cell-main mono-wrap">${escapeHtml(row.order_id || row.proposal_id || "n/a")}</div><div class="cell-sub">${escapeHtml(row.status || "n/a")}</div></div>` },
          { label: "Market", render: row => `<div class="cell-stack"><div class="cell-main">${safeLink(row.market_url, row.market || "n/a")}</div><div class="cell-sub">${escapeHtml(row.outcome || "")}</div></div>` },
          { label: "Request", render: row => `<div class="cell-stack"><div class="cell-main">${fmtMoney(row.requested_size_usdc)}</div><div class="cell-sub">@ ${fmtPrice(row.requested_price)}</div></div>` },
          { label: "Age / TTL", render: row => `<div class="cell-stack"><div class="cell-main">${fmtRelative(row.age_seconds)}</div><div class="cell-sub ${row.seconds_remaining != null && row.seconds_remaining <= 60 ? "red" : ""}">${row.seconds_remaining != null ? fmtRelative(row.seconds_remaining) + " left" : "n/a"}</div></div>` },
        ],
        data.live_orders || [],
        "No live orders."
      );

      document.getElementById("open-positions").innerHTML = renderTable(
        [
          { label: "Position", render: row => `<div class="cell-stack"><div class="cell-main">${safeLink(row.market_url, row.market || "n/a")}</div><div class="cell-sub">${escapeHtml(row.outcome || "")}</div></div>` },
          { label: "Status", render: row => `<span class="status-chip ${statusTone(row.status)}">${escapeHtml(row.status || "n/a")}</span>` },
          { label: "Entry / Mark", render: row => `<div class="cell-stack"><div class="cell-main">${fmtPrice(row.entry_price)} / ${fmtPrice(row.last_mark_price)}</div><div class="cell-sub">mark age ${fmtRelative(row.mark_age_seconds)}</div></div>` },
          { label: "Size", render: row => `<div class="cell-stack"><div class="cell-main">${fmtMoney(row.size_usdc)}</div></div>` },
          { label: "PnL", render: row => `<div class="cell-stack"><div class="cell-main ${toneClass(row.unrealized_pnl, true)}">U ${fmtSignedMoney(row.unrealized_pnl)}</div><div class="cell-sub ${toneClass(row.realized_pnl, true)}">R ${fmtSignedMoney(row.realized_pnl)}</div></div>` },
        ],
        data.open_positions || [],
        "No open positions."
      );

      document.getElementById("recent-decisions").innerHTML = renderTable(
        [
          { label: "Proposal", render: row => `<div class="cell-stack"><div class="cell-main mono-wrap">${escapeHtml(row.proposal_id)}</div><div class="cell-sub ${statusTone(row.status)}">${escapeHtml(row.status || "n/a")}</div></div>` },
          { label: "Market", render: row => `<div class="cell-stack"><div class="cell-main">${safeLink(row.market_url, row.market || "n/a")}</div><div class="cell-sub">${escapeHtml(row.outcome || "")} | ${fmtMoney(row.size_usdc)} | conf ${fmtDecimal(row.confidence_score)}</div></div>` },
          { label: "Reason", render: row => `<div class="cell-stack"><div class="cell-sub truncate-2">${escapeHtml(row.reason || "")}</div><div class="cell-sub">${fmtTimestamp(row.updated_at)}</div></div>` },
        ],
        data.recent_decisions || [],
        "No recent decisions."
      );

      document.getElementById("recent-failures").innerHTML = renderTable(
        [
          { label: "Kind", render: row => `<div class="cell-stack"><div class="cell-main">${escapeHtml(row.kind || "failure")}</div><div class="cell-sub">${escapeHtml(row.category || "")}</div></div>` },
          { label: "Target", render: row => `<div class="cell-stack"><div class="cell-sub mono-wrap">${escapeHtml(row.market || row.loop || row.proposal_id || "")}</div></div>` },
          { label: "When", render: row => `<div class="cell-stack"><div class="cell-main">${fmtTimestamp(row.timestamp)}</div><div class="cell-sub ${statusTone(row.status)}">${escapeHtml(row.status || "")}</div></div>` },
          { label: "Message", render: row => `<div class="cell-stack"><div class="cell-sub truncate-2 mono-wrap">${escapeHtml((row.message || "").slice(0, 220))}</div></div>` },
        ],
        data.recent_failures || [],
        "No recent failures."
      );

      document.getElementById("recent-events").innerHTML = renderRecentEvents(data.recent_events || []);
    }

    async function refresh() {
      try {
        const response = await fetch("/api/ops/status", { headers: { "Accept": "application/json" } });
        if (!response.ok) throw new Error(`status ${response.status}`);
        const data = await response.json();
        renderSnapshot(data);
      } catch (error) {
        document.getElementById("header-meta").textContent = `dashboard refresh failed: ${error}`;
      }
    }

    async function controlSystem(action) {
      const log = document.getElementById("control-log");
      log.textContent = `sending ${action}...`;
      try {
        const response = await fetch("/api/ops/system-control", {
          method: "POST",
          headers: { "Content-Type": "application/json", "Accept": "application/json" },
          body: JSON.stringify({ action }),
        });
        const payload = await response.json();
        if (!response.ok || !payload.ok) {
          throw new Error(payload.error || `status ${response.status}`);
        }
        log.textContent = payload.message || `${action} accepted`;
        window.setTimeout(refresh, 1500);
      } catch (error) {
        log.textContent = `control failed: ${error}`;
      }
    }

    document.querySelectorAll("[data-action]").forEach(button => {
      button.addEventListener("click", () => {
        const action = button.getAttribute("data-action");
        if (!action) return;
        controlSystem(action);
      });
    });

    renderSnapshot(initial);
    window.setInterval(refresh, 5000);
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
            "markers": [" -m polymarket_mvp.autopilot", "/polymarket-autopilot"],
            "log": root / "var" / "log" / "autopilot.dashboard.log",
        },
        {
            "name": "tg-webhook",
            "kind": "port_listener",
            "port": 8787,
            "argv": [str(venv_bin / "tg-approver"), "serve", "--port", "8787"],
            "markers": ["tg-approver", "polymarket_mvp.tg_approver"],
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
        conn = connect_db().__enter__()
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
            append_jsonl(debug_events_path("approvals"), {
                "timestamp": utc_now_iso(),
                "type": "proposal_expired",
                "proposal_id": record["proposal_id"],
                "approval_expires_at": record.get("approval_expires_at"),
            })
            results.append({"proposal_id": record["proposal_id"], "action": "expired"})
        if own_conn:
            conn.commit()
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
