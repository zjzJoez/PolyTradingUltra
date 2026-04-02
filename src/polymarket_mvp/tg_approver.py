from __future__ import annotations

import argparse
import json
import os
import time
from datetime import timedelta
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
      --bg: #0c1118;
      --panel: #121925;
      --panel-2: #182131;
      --border: #273247;
      --text: #e7edf7;
      --muted: #92a1b8;
      --accent: #8dd3ff;
      --good: #1fb877;
      --warn: #f4b942;
      --bad: #ef6b73;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: radial-gradient(circle at top, #152033 0%, var(--bg) 55%);
      color: var(--text);
      font: 14px/1.45 Menlo, Monaco, Consolas, monospace;
    }
    a { color: var(--accent); text-decoration: none; }
    .page { max-width: 1500px; margin: 0 auto; padding: 24px; }
    .header {
      display: flex; justify-content: space-between; gap: 16px; align-items: flex-start;
      margin-bottom: 20px;
    }
    .title h1 { margin: 0 0 6px; font-size: 26px; }
    .title p, .meta { margin: 0; color: var(--muted); }
    .grid { display: grid; grid-template-columns: repeat(12, 1fr); gap: 16px; }
    .panel {
      background: linear-gradient(180deg, rgba(255,255,255,0.02), rgba(255,255,255,0.00)), var(--panel);
      border: 1px solid var(--border);
      border-radius: 14px;
      padding: 14px;
      min-height: 120px;
      box-shadow: 0 12px 24px rgba(0,0,0,0.22);
    }
    .panel h2 { margin: 0 0 12px; font-size: 15px; }
    .span-12 { grid-column: span 12; }
    .span-8 { grid-column: span 8; }
    .span-6 { grid-column: span 6; }
    .span-4 { grid-column: span 4; }
    .span-3 { grid-column: span 3; }
    .cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(165px, 1fr)); gap: 10px; }
    .card {
      background: var(--panel-2);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 10px;
    }
    .card .name { color: var(--muted); font-size: 12px; margin-bottom: 6px; }
    .badge {
      display: inline-block; border-radius: 999px; padding: 2px 8px; font-size: 12px;
      border: 1px solid currentColor;
    }
    .green { color: var(--good); }
    .yellow { color: var(--warn); }
    .red { color: var(--bad); }
    .metric { font-size: 20px; font-weight: 700; }
    table { width: 100%; border-collapse: collapse; }
    th, td { text-align: left; padding: 8px 6px; border-top: 1px solid var(--border); vertical-align: top; }
    th { color: var(--muted); font-weight: 600; font-size: 12px; }
    .empty { color: var(--muted); padding: 10px 0; }
    .attention-item {
      padding: 10px 0;
      border-top: 1px solid var(--border);
    }
    .attention-item:first-child { border-top: 0; padding-top: 0; }
    .attention-title { font-weight: 700; margin-bottom: 4px; }
    .small { font-size: 12px; color: var(--muted); }
    .pill-row { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 8px; }
    .pill {
      border: 1px solid var(--border);
      border-radius: 999px;
      padding: 4px 8px;
      color: var(--muted);
      background: rgba(255,255,255,0.02);
    }
    @media (max-width: 1100px) {
      .span-8, .span-6, .span-4, .span-3 { grid-column: span 12; }
      .header { display: block; }
      .meta { margin-top: 10px; }
    }
  </style>
</head>
<body>
  <div class="page">
    <div class="header">
      <div class="title">
        <h1>Polymarket Ops</h1>
        <p>Internal dashboard for autopilot, Telegram approvals, live orders, positions, and recent failures.</p>
      </div>
      <div class="meta" id="header-meta">Loading...</div>
    </div>

    <div class="grid">
      <section class="panel span-12">
        <h2>System Health</h2>
        <div id="system-health" class="cards"></div>
      </section>

      <section class="panel span-4">
        <h2>Needs Attention</h2>
        <div id="needs-attention"></div>
      </section>

      <section class="panel span-8">
        <h2>Control State</h2>
        <div id="control-state"></div>
      </section>

      <section class="panel span-6">
        <h2>Pending Approvals</h2>
        <div id="pending-approvals"></div>
      </section>

      <section class="panel span-6">
        <h2>Live Orders</h2>
        <div id="live-orders"></div>
      </section>

      <section class="panel span-12">
        <h2>Open Positions</h2>
        <div id="open-positions"></div>
      </section>

      <section class="panel span-6">
        <h2>Recent Decisions</h2>
        <div id="recent-decisions"></div>
      </section>

      <section class="panel span-6">
        <h2>Recent Failures</h2>
        <div id="recent-failures"></div>
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

    function escapeHtml(value) {
      return String(value ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;");
    }

    function emptyState(message) {
      return `<div class="empty">${escapeHtml(message)}</div>`;
    }

    function renderTable(columns, rows) {
      if (!rows.length) return emptyState("No rows.");
      const head = `<tr>${columns.map(c => `<th>${escapeHtml(c.label)}</th>`).join("")}</tr>`;
      const body = rows.map(row => `<tr>${columns.map(c => `<td>${c.render(row)}</td>`).join("")}</tr>`).join("");
      return `<table><thead>${head}</thead><tbody>${body}</tbody></table>`;
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
          ${item.last_error_text ? `<div class="small red">${escapeHtml(item.last_error_text.slice(0, 220))}</div>` : ""}
        </div>
      `).join("");
    }

    function renderAttention(items) {
      if (!items.length) return emptyState("Nothing urgent.");
      return items.slice(0, 10).map(item => `
        <div class="attention-item">
          <div class="attention-title ${item.severity === "high" ? "red" : "yellow"}">${escapeHtml(item.title || item.kind || "attention")}</div>
          <div class="small">${escapeHtml(item.detail || item.message || "")}</div>
        </div>
      `).join("");
    }

    function renderControlState(control) {
      const kill = (control.kill_switches || []).length
        ? renderTable(
            [
              { label: "Scope", render: row => escapeHtml(`${row.scope_type}:${row.scope_key}`) },
              { label: "Reason", render: row => escapeHtml(row.reason) },
              { label: "Created", render: row => escapeHtml(row.created_at) },
            ],
            control.kill_switches
          )
        : emptyState("No active kill switches.");
      const intervals = Object.entries(control.loop_intervals_seconds || {})
        .map(([key, value]) => `<span class="pill">${escapeHtml(key)} ${fmtRelative(value)}</span>`)
        .join("");
      return `
        <div class="small">OpenClaw agent: ${escapeHtml(control.openclaw_agent_id || "n/a")} | auto execute mode: ${escapeHtml(control.tg_auto_execute_mode || "n/a")}</div>
        <div class="pill-row">${intervals}</div>
        <div style="margin-top: 12px">${kill}</div>
      `;
    }

    function renderSnapshot(data) {
      document.getElementById("header-meta").textContent =
        `updated ${data.timestamp} | pending ${data.pending_count} | live orders ${data.live_order_count} | open positions ${data.open_position_count}`;

      document.getElementById("system-health").innerHTML = renderHealth(data.system_health || []);
      document.getElementById("needs-attention").innerHTML = renderAttention(data.needs_attention || []);
      document.getElementById("control-state").innerHTML = renderControlState(data.control_state || {});

      document.getElementById("pending-approvals").innerHTML = renderTable(
        [
          { label: "Proposal", render: row => `<div>${escapeHtml(row.proposal_id)}</div><div class="small">${escapeHtml(row.proposal_kind || "entry")}</div>` },
          { label: "Market", render: row => row.market_url ? `<a href="${escapeHtml(row.market_url)}" target="_blank">${escapeHtml(row.market)}</a>` : escapeHtml(row.market) },
          { label: "Trade", render: row => `${escapeHtml(row.outcome)} · $${Number(row.size_usdc || 0).toFixed(2)} · ${(Number(row.confidence_score || 0)).toFixed(2)}` },
          { label: "Expires", render: row => `<div>${escapeHtml(row.approval_expires_at || "n/a")}</div><div class="small ${row.seconds_remaining != null && row.seconds_remaining <= 60 ? "red" : ""}">${fmtRelative(row.seconds_remaining)}</div>` },
        ],
        data.pending_approvals || []
      );

      document.getElementById("live-orders").innerHTML = renderTable(
        [
          { label: "Order", render: row => `<div>${escapeHtml(row.order_id || row.proposal_id)}</div><div class="small">${escapeHtml(row.status)}</div>` },
          { label: "Market", render: row => row.market_url ? `<a href="${escapeHtml(row.market_url)}" target="_blank">${escapeHtml(row.market)}</a>` : escapeHtml(row.market) },
          { label: "Request", render: row => `${escapeHtml(row.outcome || "")} · $${Number(row.requested_size_usdc || 0).toFixed(2)} @ ${row.requested_price ?? "n/a"}` },
          { label: "Age / TTL", render: row => `<div>${fmtRelative(row.age_seconds)}</div><div class="small ${row.seconds_remaining != null && row.seconds_remaining <= 60 ? "red" : ""}">${row.seconds_remaining != null ? fmtRelative(row.seconds_remaining) + " left" : "n/a"}</div>` },
        ],
        data.live_orders || []
      );

      document.getElementById("open-positions").innerHTML = renderTable(
        [
          { label: "Position", render: row => `<div>${escapeHtml(row.market)}</div><div class="small">${escapeHtml(row.outcome)}</div>` },
          { label: "Status", render: row => escapeHtml(row.status) },
          { label: "Entry / Mark", render: row => `${row.entry_price ?? "n/a"} / ${row.last_mark_price ?? "n/a"}` },
          { label: "Size", render: row => `$${Number(row.size_usdc || 0).toFixed(2)}` },
          { label: "PnL", render: row => `<div>U ${Number(row.unrealized_pnl || 0).toFixed(2)}</div><div class="small">R ${Number(row.realized_pnl || 0).toFixed(2)}</div>` },
        ],
        data.open_positions || []
      );

      document.getElementById("recent-decisions").innerHTML = renderTable(
        [
          { label: "Proposal", render: row => `<div>${escapeHtml(row.proposal_id)}</div><div class="small">${escapeHtml(row.status)}</div>` },
          { label: "Market", render: row => row.market_url ? `<a href="${escapeHtml(row.market_url)}" target="_blank">${escapeHtml(row.market)}</a>` : escapeHtml(row.market) },
          { label: "Trade", render: row => `${escapeHtml(row.outcome)} · $${Number(row.size_usdc || 0).toFixed(2)} · ${(Number(row.confidence_score || 0)).toFixed(2)}` },
          { label: "Reason", render: row => `<span class="small">${escapeHtml(row.reason || "")}</span>` },
        ],
        data.recent_decisions || []
      );

      document.getElementById("recent-failures").innerHTML = renderTable(
        [
          { label: "Kind", render: row => `<div>${escapeHtml(row.kind)}</div><div class="small">${escapeHtml(row.category)}</div>` },
          { label: "Target", render: row => escapeHtml(row.market || row.loop || row.proposal_id || "") },
          { label: "When", render: row => `<div>${escapeHtml(row.timestamp || "")}</div><div class="small">${escapeHtml(row.status || "")}</div>` },
          { label: "Message", render: row => `<span class="small">${escapeHtml((row.message || "").slice(0, 220))}</span>` },
        ],
        data.recent_failures || []
      );
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

    renderSnapshot(initial);
    window.setInterval(refresh, 5000);
  </script>
</body>
</html>
"""


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


def send_proposals(proposal_ids: List[str], chat_id: str, dry_run: bool = False) -> Dict[str, Any]:
    results = []
    with connect_db() as conn:
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

    @app.get("/ops")
    def ops_dashboard():
        with connect_db() as conn:
            snapshot = build_ops_snapshot(conn)
        return render_template_string(
            OPS_DASHBOARD_TEMPLATE,
            initial_json=json.dumps(snapshot, sort_keys=False),
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
            tg_post(
                "answerCallbackQuery",
                {
                    "callback_query_id": callback_query["id"],
                    "text": f"Decision recorded: {result['status']}",
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
        except Exception:
            append_jsonl(debug_events_path("approvals"), {"timestamp": utc_now_iso(), "type": "telegram_followup_failed", "proposal_id": proposal_id})
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
