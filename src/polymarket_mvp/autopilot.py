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


# Wall-clock timestamp of the most recent LLM-backed propose cycle. Used by
# _loop_propose() to throttle LLM invocations independently of the loop
# cadence (so the autopilot thread can keep waking up fast to service expiry
# and execute, without hammering Claude Max).
_LAST_LLM_PROPOSE_AT: float = 0.0

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
from .services.authorization_service import safe_authorization_status


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
        self._startup_checks()
        if self.max_iterations is not None:
            self._run_sequential()
        else:
            self._run_threaded()

    def _startup_checks(self) -> None:
        """Warn loudly about missing optional dependencies so operators notice immediately."""
        try:
            from .services.redeemer import redeem_resolved_positions  # noqa: F401
        except ImportError:
            _log(
                "WARNING: web3 not installed — redeemer is DISABLED. "
                "Winning positions will NOT be auto-redeemed; USDC stays locked in "
                "conditional tokens until manually redeemed. "
                "Fix: pip install 'polymarket-mvp[real-exec]'"
            )

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
            (get_env_int("POLY_AUTOPILOT_CONTEXT_MAX_CANDIDATES", 25),),
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

    def _sweep_proposed_records(self, conn) -> None:
        """Evaluate any proposals sitting at status='proposed' through the risk engine.

        Runs unconditionally — does NOT require the LLM gate to be open.  This
        ensures that exit proposals (written by _loop_exit) and imported alpha
        signals are never blocked behind the LLM min-interval throttle.
        """
        from .risk_engine import evaluate_full_record

        proposed = list_proposals_by_status(conn, ["proposed"])
        for item in proposed:
            pid = item["proposal_id"]
            record = proposal_record(conn, pid)
            if record is None:
                continue
            try:
                result = evaluate_full_record(conn, record)
                update_proposal_workflow_fields(
                    conn,
                    pid,
                    authorization_status=safe_authorization_status(
                        (result.get("authorization") or {}).get("authorization_status")
                    ),
                    status=result["next_status"],
                )
            except Exception:
                _log(f"risk eval failed for {pid}: {traceback.format_exc()}")

    def _loop_propose(self, conn) -> int:
        from .proposer import run_proposal_pipeline
        from .risk_engine import evaluate_full_record
        from .services.openclaw_adapter import (
            LLMRateLimitError,
            llm_cooldown_remaining_sec,
        )

        # Always sweep imported/exit proposals — these don't need the LLM so
        # they must not be blocked behind the rate-limit gate below.
        self._sweep_proposed_records(conn)
        if os.getenv("MVP_SHADOW_MODE") == "1":
            self._shadow_auto_approve_pending(conn)

        current_pending = list_proposals_by_status(conn, ["pending_approval"])
        if len(current_pending) >= self._max_pending_approvals():
            return 0

        engine = (os.getenv("POLY_PROPOSER_ENGINE") or "openclaw_llm").strip() or "openclaw_llm"
        if engine not in {"heuristic", "openclaw_llm"}:
            engine = "openclaw_llm"

        # LLM rate-limit gate (claude-backed engines only). Two interlocks:
        #  (1) Adapter cooldown after a rate-limit hit — honor it.
        #  (2) Minimum wall-clock interval between LLM-backed propose cycles.
        #      Autopilot may wake up every 30s, but we don't actually want to
        #      invoke the LLM that often when the Max subscription is shared
        #      with interactive sessions.
        global _LAST_LLM_PROPOSE_AT
        if engine == "openclaw_llm":
            remaining = llm_cooldown_remaining_sec()
            if remaining > 0:
                _log(f"propose skipped: LLM cooldown {int(remaining)}s remaining")
                return 0
            min_interval = get_env_int("POLY_LLM_MIN_INTERVAL_SECONDS", 600)
            if time.time() - _LAST_LLM_PROPOSE_AT < min_interval:
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

        try:
            proposals = run_proposal_pipeline(
                conn, markets, engine=engine,
                size_usdc=get_env_float("POLY_RISK_MAX_ORDER_USDC", 10.0),
                top=get_env_int("POLY_AUTOPILOT_MAX_PROPOSALS_PER_LOOP", 3),
            )
        except LLMRateLimitError as exc:
            _log(
                f"propose rate-limited: consecutive={exc.consecutive_count} "
                f"cooldown={exc.cooldown_sec}s — skipping cycle"
            )
            try:
                conn.execute(
                    """
                    INSERT INTO llm_rate_limit_events
                      (hit_at, stderr_snippet, cooldown_applied_sec, consecutive_count)
                    VALUES (?, ?, ?, ?)
                    """,
                    (utc_now_iso(), exc.stderr_snippet, int(exc.cooldown_sec), int(exc.consecutive_count)),
                )
                conn.commit()
            except Exception:
                _log(f"llm_rate_limit_events insert failed: {traceback.format_exc()}")
            return 0
        if engine == "openclaw_llm":
            _LAST_LLM_PROPOSE_AT = time.time()
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
                    authorization_status=safe_authorization_status(
                        (result.get("authorization") or {}).get("authorization_status")
                    ),
                    status=result["next_status"],
                )
            except Exception:
                _log(f"risk eval failed for {p['proposal_id']}: {traceback.format_exc()}")

        # Sweep again: LLM proposals were just risk-evaluated inline above;
        # this second pass handles any residual 'proposed' rows that were
        # created concurrently (e.g. exit proposals written during this tick).
        self._sweep_proposed_records(conn)
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
                # Skip if a non-terminal exit proposal already exists for this position.
                # Without this guard, each 30s tick creates a new proposal_id (if size
                # or confidence changes) and multiple racing exit orders pile up.
                existing = conn.execute(
                    """SELECT 1 FROM proposals
                       WHERE target_position_id = ?
                         AND proposal_kind = 'exit'
                         AND status NOT IN ('risk_blocked', 'expired', 'cancelled', 'executed')
                       LIMIT 1""",
                    (position["id"],),
                ).fetchone()
                if existing:
                    continue

                rec = run_exit_agent(conn, position, use_llm=True)
                recommendation = rec.get("recommendation")
                if recommendation in ("close", "reduce") and float(rec.get("confidence_score", 0)) >= 0.7:
                    target_pct = float(rec.get("target_reduce_pct") or 1.0)
                    exit_size = round(float(position["size_usdc"]) * target_pct, 4)
                    # If the reduce size is too small to meet Polymarket's 5-share
                    # minimum, promote to a full close rather than letting the risk
                    # engine block it silently.
                    if recommendation == "reduce":
                        entry_price = float(position.get("entry_price") or 0)
                        if entry_price > 0 and (exit_size / entry_price) < 5.0:
                            exit_size = round(float(position["size_usdc"]), 4)
                    exit_proposal = normalize_proposal({
                        "market_id": position["market_id"],
                        "outcome": position["outcome"],
                        "confidence_score": rec["confidence_score"],
                        "recommended_size_usdc": exit_size,
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
