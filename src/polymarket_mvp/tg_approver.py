from __future__ import annotations

import argparse
import json
import os
import time
from typing import Any, Dict, List

import requests
from flask import Flask, jsonify, request

from .common import (
    append_jsonl,
    debug_events_path,
    dump_json,
    get_env_bool,
    get_env_float,
    get_env_int,
    load_repo_env,
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
    proposal_record,
    record_approval,
    record_execution,
)
from .poly_executor import execute_record

load_repo_env()


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
            message_text = format_message(record)
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
            event = {
                "timestamp": utc_now_iso(),
                "type": "proposal_sent",
                "proposal_id": proposal_id,
                "status": record["status"],
                "chat_id": str(chat_id),
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
