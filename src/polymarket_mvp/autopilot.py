"""24/7 Polymarket autopilot supervisor.

Single long-running process that continuously scans markets, generates proposals,
manages approval deadlines, executes authorized orders, reconciles live orders,
and generates exit recommendations.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
import traceback
from typing import Any, Dict, List

from .common import (
    get_env_float,
    get_env_int,
    normalize_proposal,
    utc_now_iso,
)
from .db import (
    connect_db,
    init_db,
    list_kill_switches,
    list_positions,
    list_proposals_by_status,
    proposal_record,
    record_execution,
    record_heartbeat,
    update_proposal_workflow_fields,
)


def _log(msg: str) -> None:
    print(f"[autopilot {utc_now_iso()}] {msg}", file=sys.stderr, flush=True)


def _global_kill_switch_active(conn) -> bool:
    active = list_kill_switches(conn, active_only=True)
    return any(item["scope_type"] == "global" for item in active)


class Autopilot:
    def __init__(self, *, max_iterations: int | None = None):
        self.max_iterations = max_iterations
        self.cadences: Dict[str, int] = {
            "scan": get_env_int("POLY_SCAN_INTERVAL_SECONDS", 30),
            "context": get_env_int("POLY_CONTEXT_INTERVAL_SECONDS", 60),
            "propose": get_env_int("POLY_DECISION_INTERVAL_SECONDS", 30),
            "expiry": 5,
            "execute": 10,
            "reconcile": get_env_int("POLY_RECONCILE_INTERVAL_SECONDS", 10),
            "exit": get_env_int("POLY_EXIT_INTERVAL_SECONDS", 30),
            "review": 60,
        }
        self.last_run: Dict[str, float] = {}

    def should_run(self, loop_name: str) -> bool:
        last = self.last_run.get(loop_name, 0)
        return (time.time() - last) >= self.cadences[loop_name]

    def run_forever(self) -> None:
        init_db()
        _log("autopilot started")
        iteration = 0
        while True:
            if self.max_iterations is not None and iteration >= self.max_iterations:
                _log(f"max_iterations={self.max_iterations} reached, stopping")
                break
            try:
                with connect_db() as conn:
                    if _global_kill_switch_active(conn):
                        _log("global kill switch active, sleeping")
                        time.sleep(10)
                        iteration += 1
                        continue

                for name in ["scan", "context", "propose", "expiry", "execute", "reconcile", "exit", "review"]:
                    if self.should_run(name):
                        try:
                            with connect_db() as conn:
                                self._tick(conn, name)
                                conn.commit()
                        except Exception:
                            _log(f"{name} top-level error: {traceback.format_exc()}")
            except Exception:
                _log(f"top-level error: {traceback.format_exc()}")
            iteration += 1
            time.sleep(1)

    def _tick(self, conn, name: str) -> None:
        started = utc_now_iso()
        count = 0
        error_msg = None
        try:
            fn = getattr(self, f"_loop_{name}")
            count = fn(conn) or 0
            if count:
                _log(f"{name}: processed {count} items")
        except Exception:
            error_msg = traceback.format_exc()
            _log(f"{name} error: {error_msg}")
        try:
            record_heartbeat(conn, name, started, utc_now_iso(), count, error_msg)
        except Exception:
            pass
        self.last_run[name] = time.time()

    # --- Loop implementations ---

    def _loop_scan(self, conn) -> int:
        from .poly_scanner import scan_and_persist

        markets = scan_and_persist(
            conn,
            min_liquidity=get_env_float("POLY_SCAN_MIN_LIQUIDITY", 10000),
            max_expiry_days=get_env_int("POLY_SCAN_MAX_EXPIRY_DAYS", 7),
        )
        return len(markets)

    def _loop_context(self, conn) -> int:
        from .event_fetcher import fetch_and_persist_contexts

        # Fetch contexts for markets scanned recently (from DB)
        rows = conn.execute(
            "SELECT * FROM market_snapshots WHERE active = 1 AND accepting_orders = 1 ORDER BY last_scanned_at DESC LIMIT ?",
            (get_env_int("POLY_AUTOPILOT_MAX_CANDIDATES_PER_LOOP", 25),),
        ).fetchall()
        if not rows:
            return 0
        from .common import rows_to_dicts
        markets = rows_to_dicts(rows)
        for m in markets:
            if m.get("market_json") and isinstance(m["market_json"], str):
                import json
                try:
                    m["market_json"] = json.loads(m["market_json"])
                except Exception:
                    pass
            if m.get("outcomes_json") and isinstance(m["outcomes_json"], str):
                import json
                try:
                    m["outcomes_json"] = json.loads(m["outcomes_json"])
                except Exception:
                    pass
        results = fetch_and_persist_contexts(conn, markets)
        return len(results)

    def _loop_propose(self, conn) -> int:
        from .proposer import run_proposal_pipeline
        from .risk_engine import evaluate_full_record

        rows = conn.execute(
            "SELECT * FROM market_snapshots WHERE active = 1 AND accepting_orders = 1 ORDER BY last_scanned_at DESC LIMIT ?",
            (get_env_int("POLY_AUTOPILOT_MAX_CANDIDATES_PER_LOOP", 25),),
        ).fetchall()
        if not rows:
            return 0
        from .common import rows_to_dicts
        import json
        markets = rows_to_dicts(rows)
        for m in markets:
            for field in ("market_json", "outcomes_json"):
                if m.get(field) and isinstance(m[field], str):
                    try:
                        m[field] = json.loads(m[field])
                    except Exception:
                        pass
            if "outcomes" not in m and m.get("outcomes_json"):
                m["outcomes"] = m["outcomes_json"]

        proposals = run_proposal_pipeline(conn, markets, engine="openclaw_llm",
                                          size_usdc=get_env_float("POLY_RISK_MAX_ORDER_USDC", 10.0),
                                          top=get_env_int("POLY_AUTOPILOT_MAX_CANDIDATES_PER_LOOP", 25))
        # Run risk engine on each
        for p in proposals:
            record = proposal_record(conn, p["proposal_id"])
            if record is None:
                continue
            try:
                result = evaluate_full_record(conn, record)
                update_proposal_workflow_fields(
                    conn,
                    p["proposal_id"],
                    authorization_status=(result.get("authorization") or {}).get("authorization_status", "none"),
                    status=result["next_status"],
                )
            except Exception:
                _log(f"risk eval failed for {p['proposal_id']}: {traceback.format_exc()}")

        # Send unsent pending_approval proposals to Telegram
        self._send_unsent_to_telegram(conn)
        return len(proposals)

    def _send_unsent_to_telegram(self, conn) -> None:
        chat_id = os.getenv("TG_CHAT_ID")
        if not chat_id:
            return
        pending = list_proposals_by_status(conn, ["pending_approval"])
        unsent = [p for p in pending if not p.get("telegram_message_id")]
        if not unsent:
            return
        try:
            from .tg_approver import send_proposals
            send_proposals([p["proposal_id"] for p in unsent], chat_id=chat_id, conn=conn)
        except Exception:
            _log(f"telegram send failed: {traceback.format_exc()}")

    def _loop_expiry(self, conn) -> int:
        from .tg_approver import expire_stale_proposals
        expired = expire_stale_proposals(conn)
        return len(expired)

    def _loop_execute(self, conn) -> int:
        from .poly_executor import execute_record

        authorized = list_proposals_by_status(conn, ["authorized_for_execution"])
        mode = (os.getenv("TG_AUTO_EXECUTE_MODE") or "real").strip().lower()
        if mode not in ("mock", "real"):
            mode = "real"
        count = 0
        session_state: Dict[str, float] = {"cumulative_spend_usdc": 0.0}
        for record in authorized:
            try:
                execution = execute_record(conn, record, mode=mode, session_state=session_state)
                record_execution(conn, execution)
                count += 1
            except Exception:
                _log(f"execution failed for {record['proposal_id']}: {traceback.format_exc()}")
        return count

    def _loop_reconcile(self, conn) -> int:
        from .services.reconciler import cancel_stale_orders, reconcile_live_orders
        from .services.position_manager import sync_all_positions, update_position_marks

        cancelled = cancel_stale_orders(conn)
        reconciled = reconcile_live_orders(conn)
        sync_all_positions(conn)
        update_position_marks(conn)
        return len(cancelled) + len(reconciled)

    def _loop_exit(self, conn) -> int:
        from .agents.exit_agent import run_exit_agent
        from .db import upsert_proposal

        positions = list_positions(conn, statuses=["open", "partially_filled"])
        max_exits = get_env_int("POLY_AUTOPILOT_MAX_EXIT_PROPOSALS_PER_LOOP", 5)
        count = 0
        for position in positions[:max_exits]:
            try:
                rec = run_exit_agent(conn, position, use_llm=True)
                if rec.get("recommendation") == "close" and float(rec.get("confidence_score", 0)) >= 0.7:
                    exit_proposal = normalize_proposal({
                        "market_id": position["market_id"],
                        "outcome": position["outcome"],
                        "confidence_score": rec["confidence_score"],
                        "recommended_size_usdc": position["size_usdc"],
                        "reasoning": rec.get("reasoning", "exit recommendation"),
                        "max_slippage_bps": get_env_int("POLY_RISK_MAX_SLIPPAGE_BPS", 500),
                    })
                    upsert_proposal(
                        conn,
                        exit_proposal,
                        decision_engine="openclaw_llm",
                        status="proposed",
                        context_payload={},
                        proposal_kind="exit",
                        target_position_id=position["id"],
                    )
                    count += 1
            except Exception:
                _log(f"exit agent failed for position {position['id']}: {traceback.format_exc()}")
        return count

    def _loop_review(self, conn) -> int:
        from .agents.review_agent import run_review_agent

        resolved = list_positions(conn, statuses=["resolved"])
        count = 0
        for position in resolved[:10]:
            try:
                run_review_agent(conn, position)
                count += 1
            except Exception:
                pass
        return count


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="24/7 Polymarket autopilot supervisor.")
    parser.add_argument("--max-iterations", type=int, help="Stop after N ticks (for testing).")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    pilot = Autopilot(max_iterations=args.max_iterations)
    pilot.run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
