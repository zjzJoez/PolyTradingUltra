"""24/7 Polymarket autopilot supervisor.

Single long-running process that continuously scans markets, generates proposals,
manages approval deadlines, executes authorized orders, reconciles live orders,
and generates exit recommendations.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import threading
import time
import traceback
from typing import Any, Dict, List


# Process-wide write coordination: serializes DB write transactions across all
# autopilot loop threads to avoid SQLite "database is locked" under contention.
# Held only for quick-write loops; slow loops (propose/context/review) do their
# LLM work outside the lock and acquire it opportunistically via retry.
_DB_WRITE_LOCK = threading.Lock()

from .common import (
    blocked_market_reason,
    get_env_float,
    get_env_int,
    normalize_proposal,
    utc_now_iso,
)
from .db import (
    connect_db,
    init_db,
    list_expired_pending_proposals,
    list_kill_switches,
    list_positions,
    list_proposals_by_status,
    proposal_record,
    record_approval,
    record_execution,
    record_heartbeat,
    update_proposal_status,
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

    def _max_pending_approvals(self) -> int:
        return get_env_int("POLY_AUTOPILOT_MAX_PENDING_APPROVALS", 5)

    def should_run(self, loop_name: str) -> bool:
        last = self.last_run.get(loop_name, 0)
        return (time.time() - last) >= self.cadences[loop_name]

    LOOP_NAMES = ["scan", "context", "propose", "expiry", "execute", "reconcile", "exit", "review"]

    def run_forever(self) -> None:
        init_db()
        if self.max_iterations is not None:
            self._run_sequential()
        else:
            self._run_threaded()

    def _run_sequential(self) -> None:
        """Single-threaded pass used by tests with max_iterations set."""
        _log("autopilot started (sequential)")
        iteration = 0
        while True:
            if self.max_iterations is not None and iteration >= self.max_iterations:
                _log(f"max_iterations={self.max_iterations} reached, stopping")
                break
            try:
                for name in self.LOOP_NAMES:
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

    def _run_threaded(self) -> None:
        """Production path: one daemon thread per loop so slow loops cannot starve fast ones."""
        _log("autopilot started (threaded, one worker per loop)")
        stop = threading.Event()
        threads: List[threading.Thread] = []
        for name in self.LOOP_NAMES:
            t = threading.Thread(
                target=self._loop_worker,
                args=(name, stop),
                name=f"autopilot-{name}",
                daemon=True,
            )
            t.start()
            threads.append(t)
        try:
            while not stop.is_set():
                stop.wait(1.0)
        except KeyboardInterrupt:
            _log("received SIGINT, stopping")
            stop.set()
        for t in threads:
            t.join(timeout=5)

    def _loop_worker(self, name: str, stop: threading.Event) -> None:
        cadence = max(1, int(self.cadences.get(name, 30)))
        # Stagger initial starts so we don't hammer the DB with 8 simultaneous writes.
        stop.wait(0.25 * self.LOOP_NAMES.index(name))
        while not stop.is_set():
            started = time.time()
            try:
                with connect_db() as conn:
                    self._tick(conn, name)
                    conn.commit()
            except Exception:
                _log(f"{name} top-level error: {traceback.format_exc()}")
            elapsed = time.time() - started
            # Sleep the remainder of the cadence; never sleep less than 1s.
            stop.wait(max(1.0, cadence - elapsed))

    def _tick(self, conn, name: str) -> None:
        # Kill switch check inside _tick using the SAME connection (fixes race condition)
        if _global_kill_switch_active(conn):
            _log(f"{name} skipped: global kill switch active")
            return

        # Loop lag alert: warn if loop is overdue by 3x its cadence
        lag = time.time() - self.last_run.get(name, 0)
        if self.last_run.get(name) and lag > 3 * self.cadences[name]:
            _log(f"CRITICAL: {name} loop lagging {lag:.0f}s (cadence={self.cadences[name]}s)")

        # No Python-level write lock: each loop owns its own connection and
        # SQLite (WAL + busy_timeout) serializes writes at the DB layer. The
        # propose/context/review loops make multi-second LLM subprocess calls,
        # and a global lock across those calls starves fast loops (expiry 5s,
        # execute 10s) and caused a 10+ min stall in practice.
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
            if "outcomes" not in m and m.get("outcomes_json"):
                m["outcomes"] = m["outcomes_json"]
        markets = [m for m in markets if not blocked_market_reason(m)]
        if not markets:
            return 0
        results = fetch_and_persist_contexts(conn, markets)
        return len(results)

    def _loop_propose(self, conn) -> int:
        from .proposer import run_proposal_pipeline
        from .risk_engine import evaluate_full_record

        current_pending = list_proposals_by_status(conn, ["pending_approval"])
        if len(current_pending) >= self._max_pending_approvals():
            return 0

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
        markets = [m for m in markets if not blocked_market_reason(m)]
        if not markets:
            return 0

        proposals = run_proposal_pipeline(conn, markets, engine="heuristic",
                                          size_usdc=get_env_float("POLY_RISK_MAX_ORDER_USDC", 10.0),
                                          top=get_env_int("POLY_AUTOPILOT_MAX_PROPOSALS_PER_LOOP", 3))
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

        # Also evaluate any `proposed`-status records that bypassed run_proposal_pipeline
        # (e.g. alpha_lab signals imported via `import-alpha-signals`). Without this
        # sweep they stay at `proposed` forever and never reach the execute loop.
        other_proposed = list_proposals_by_status(conn, ["proposed"])
        for item in other_proposed:
            pid = item["proposal_id"]
            record = proposal_record(conn, pid)
            if record is None:
                continue
            try:
                result = evaluate_full_record(conn, record)
                update_proposal_workflow_fields(
                    conn,
                    pid,
                    authorization_status=(result.get("authorization") or {}).get("authorization_status", "none"),
                    status=result["next_status"],
                )
            except Exception:
                _log(f"risk eval failed for imported {pid}: {traceback.format_exc()}")

        # Shadow-mode auto-approval: proposals that passed risk but lack matching
        # auto-execute authorization would normally sit in `pending_approval`
        # waiting for a human. In shadow mode we auto-record an approval so they
        # flow through to `_loop_execute`, which is in turn gated by
        # MVP_SHADOW_MODE at the top of `poly_executor.execute_record`.
        if os.getenv("MVP_SHADOW_MODE") == "1":
            self._shadow_auto_approve_pending(conn)
        return len(proposals)

    def _shadow_auto_approve_pending(self, conn) -> None:
        pending = list_proposals_by_status(conn, ["pending_approval"])
        for item in pending:
            pid = item["proposal_id"]
            if (item.get("approval") or {}).get("decision") == "approved":
                # already has a shadow-auto approval row; just ensure status advances
                if item.get("status") == "pending_approval":
                    update_proposal_status(conn, pid, "authorized_for_execution")
                continue
            try:
                record_approval(
                    conn,
                    proposal_id=pid,
                    decision="approved",
                    decided_at=utc_now_iso(),
                    callback_query_id=f"shadow:{pid}",
                    raw_callback_json={"source": "shadow_auto"},
                )
                # record_approval transitions pending_approval -> approved;
                # bump to authorized_for_execution so the execute loop picks it up.
                update_proposal_status(conn, pid, "authorized_for_execution")
            except Exception:
                _log(f"shadow auto-approve failed for {pid}: {traceback.format_exc()}")

    def _loop_expiry(self, conn) -> int:
        """Sweep pending_approval proposals past their deadline to `expired`."""
        expired_records = list_expired_pending_proposals(conn)
        for record in expired_records:
            try:
                update_proposal_status(conn, record["proposal_id"], "expired")
            except Exception:
                _log(f"expire sweep failed for {record['proposal_id']}: {traceback.format_exc()}")
        return len(expired_records)

    def _loop_execute(self, conn) -> int:
        from .poly_executor import execute_record

        authorized = list_proposals_by_status(conn, ["authorized_for_execution"])
        mode = (os.getenv("POLY_AUTOPILOT_EXECUTE_MODE") or "real").strip().lower()
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
        from .services.reconciler import assert_position_consistency, cancel_orphaned_positions, cancel_stale_orders, check_and_backfill_resolutions, reconcile_live_orders
        from .services.position_manager import sync_all_positions, update_position_marks
        # Redeemer pulls in web3 for on-chain claim txs; on shadow-mode deployments
        # without web3 installed, skip redemption rather than crashing the loop.
        try:
            from .services.redeemer import redeem_resolved_positions
        except ImportError:
            redeem_resolved_positions = None

        cancel_orphaned_positions(conn)
        assert_position_consistency(conn)
        conn.commit()
        cancelled = cancel_stale_orders(conn)
        reconciled = reconcile_live_orders(conn)
        newly_resolved = check_and_backfill_resolutions(conn)
        if newly_resolved:
            for item in newly_resolved:
                _log(f"market resolved: {item['market_id']} → {item['resolved_outcome']}")
        sync_all_positions(conn)
        update_position_marks(conn)
        conn.commit()

        # Auto-redeem winning conditional tokens after resolution
        if redeem_resolved_positions is None:
            return 0
        try:
            redeemed = redeem_resolved_positions(conn)
            for r in redeemed:
                if r.get("success"):
                    _log(f"redeemed: market {r['market_id']} tx={r['tx_hash'][:16]}… balances={r.get('balances_before')}")
                else:
                    _log(f"redeem failed: market {r.get('market_id')} error={r.get('error', 'unknown')}")
            conn.commit()
        except Exception as exc:
            _log(f"redeem error: {exc}")

        return len(cancelled) + len(reconciled) + len(newly_resolved)

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
