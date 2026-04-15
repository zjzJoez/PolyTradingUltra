"""Tests for v0.5 autopilot features: TTL clamping, approval expiry,
stale order cancellation, exit proposals, and supervisor loop."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from polymarket_mvp.common import (
    blocked_market_reason,
    clamp_approval_ttl,
    clamp_order_live_ttl,
    parse_iso8601,
    proposal_id_for,
    utc_now_iso,
)
from polymarket_mvp.db import (
    connect_db,
    init_db,
    list_expired_pending_proposals,
    list_positions,
    proposal_record,
    record_execution,
    record_heartbeat,
    record_position,
    record_position_event,
    update_proposal_status,
    update_proposal_workflow_fields,
    upsert_market_snapshot,
    upsert_proposal,
)


def sample_market() -> dict:
    return {
        "market_id": "m1",
        "question": "Will BTC be up in the next hour?",
        "slug": "btc-next-hour",
        "market_url": "https://polymarket.com/event/btc-next-hour",
        "condition_id": "cond-1",
        "active": True,
        "closed": False,
        "accepting_orders": True,
        "end_date": "2026-03-22T12:00:00Z",
        "seconds_to_expiry": 3600,
        "days_to_expiry": 0.041,
        "liquidity_usdc": 100000,
        "volume_usdc": 50000,
        "volume_24h_usdc": 10000,
        "outcomes": [
            {"name": "Yes", "price": 0.62, "token_id": "tok_yes"},
            {"name": "No", "price": 0.38, "token_id": "tok_no"},
        ],
    }


def sample_proposal() -> dict:
    return {
        "market_id": "m1",
        "outcome": "Yes",
        "confidence_score": 0.62,
        "recommended_size_usdc": 5.0,
        "reasoning": "Test proposal",
        "max_slippage_bps": 500,
    }


class AutopilotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "test.sqlite3"
        os.environ["POLYMARKET_MVP_DB_PATH"] = str(self.db_path)
        os.environ["POLYMARKET_AVAILABLE_BALANCE_U"] = "1000"
        os.environ["POLY_RISK_REQUIRE_EXECUTABLE_MARKET"] = "false"
        os.environ["POLY_RISK_MAX_ORDER_USDC"] = "10"
        os.environ["POLY_RISK_MIN_CONFIDENCE"] = "0.5"
        os.environ["POLY_RISK_MAX_SLIPPAGE_BPS"] = "600"
        os.environ["POLY_BLOCK_CRYPTO_DIRECTIONAL_MARKETS"] = "false"
        os.environ["SIGNATURE_TYPE"] = ""
        os.environ["TG_WEBHOOK_SECRET"] = ""

    def tearDown(self) -> None:
        self.tmpdir.cleanup()
        os.environ.pop("POLYMARKET_MVP_DB_PATH", None)
        os.environ.pop("POLY_BLOCK_CRYPTO_DIRECTIONAL_MARKETS", None)
        os.environ.pop("SIGNATURE_TYPE", None)

    # --- TTL clamping tests ---

    def test_approval_ttl_clamping_defaults(self) -> None:
        """Default TTL with no agent suggestion and no market expiry."""
        os.environ["POLY_APPROVAL_MAX_TTL_SECONDS"] = "300"
        ttl = clamp_approval_ttl(None, None)
        self.assertEqual(ttl, 300)

    def test_approval_ttl_agent_lower_than_system(self) -> None:
        """Agent suggests shorter TTL than system max."""
        os.environ["POLY_APPROVAL_MAX_TTL_SECONDS"] = "300"
        ttl = clamp_approval_ttl(120, None)
        self.assertEqual(ttl, 120)

    def test_approval_ttl_agent_clamped_to_system_max(self) -> None:
        """Agent suggests longer TTL, clamped to system max."""
        os.environ["POLY_APPROVAL_MAX_TTL_SECONDS"] = "300"
        ttl = clamp_approval_ttl(600, None)
        self.assertEqual(ttl, 300)

    def test_approval_ttl_respects_market_expiry(self) -> None:
        """TTL shortened when market expires soon (e.g. 200s away)."""
        os.environ["POLY_APPROVAL_MAX_TTL_SECONDS"] = "300"
        os.environ["POLY_APPROVAL_EXPIRY_BUFFER_SECONDS"] = "120"
        ttl = clamp_approval_ttl(None, 200.0)
        # min(300, max(30, 0.25*200)=50, max(30, 200-120)=80) = 50
        self.assertEqual(ttl, 50)

    def test_approval_ttl_floor_enforced(self) -> None:
        """TTL never drops below 30s floor."""
        os.environ["POLY_APPROVAL_MAX_TTL_SECONDS"] = "10"
        ttl = clamp_approval_ttl(5, None)
        self.assertEqual(ttl, 30)

    def test_order_live_ttl_clamping(self) -> None:
        os.environ["POLY_ORDER_MAX_LIVE_TTL_SECONDS"] = "300"
        self.assertEqual(clamp_order_live_ttl(None), 300)
        self.assertEqual(clamp_order_live_ttl(100), 100)
        self.assertEqual(clamp_order_live_ttl(600), 300)
        self.assertEqual(clamp_order_live_ttl(5), 15)  # floor

    # --- Approval expiry tests ---

    def test_expire_stale_proposals(self) -> None:
        """Proposal past its approval_expires_at gets expired."""
        init_db(self.db_path)
        with connect_db(self.db_path) as conn:
            upsert_market_snapshot(conn, sample_market())
            proposal = sample_proposal()
            upsert_proposal(
                conn, proposal, decision_engine="heuristic",
                status="proposed", context_payload={},
            )
            pid = proposal_id_for(proposal)
            update_proposal_status(conn, pid, "pending_approval")
            past_time = (parse_iso8601(utc_now_iso()) - timedelta(seconds=60)).strftime("%Y-%m-%dT%H:%M:%SZ")
            update_proposal_workflow_fields(
                conn, pid,
                approval_requested_at=past_time,
                approval_expires_at=past_time,
                telegram_message_id="123",
                telegram_chat_id="456",
            )
            conn.commit()

            expired = list_expired_pending_proposals(conn)
            self.assertEqual(len(expired), 1)
            self.assertEqual(expired[0]["proposal_id"], pid)

            # Call expire sweep (mock Telegram)
            from polymarket_mvp.tg_approver import expire_stale_proposals
            with patch("polymarket_mvp.tg_approver.tg_post"):
                results = expire_stale_proposals(conn)
            conn.commit()

            self.assertEqual(len(results), 1)
            self.assertEqual(results[0]["action"], "expired")
            record = proposal_record(conn, pid)
            self.assertEqual(record["status"], "expired")

    def test_expire_stale_proposals_commits_before_telegram_edit(self) -> None:
        """Expiry sweep should release SQLite write lock before Telegram calls."""
        init_db(self.db_path)
        with connect_db(self.db_path) as conn:
            upsert_market_snapshot(conn, sample_market())
            proposal = sample_proposal()
            upsert_proposal(
                conn, proposal, decision_engine="heuristic",
                status="proposed", context_payload={},
            )
            pid = proposal_id_for(proposal)
            update_proposal_status(conn, pid, "pending_approval")
            past_time = (parse_iso8601(utc_now_iso()) - timedelta(seconds=60)).strftime("%Y-%m-%dT%H:%M:%SZ")
            update_proposal_workflow_fields(
                conn, pid,
                approval_requested_at=past_time,
                approval_expires_at=past_time,
                telegram_message_id="123",
                telegram_chat_id="456",
            )
            conn.commit()

            from polymarket_mvp.tg_approver import expire_stale_proposals

            def fake_tg_post(method, payload):
                del method, payload
                with connect_db(self.db_path) as verify_conn:
                    record = proposal_record(verify_conn, pid)
                    self.assertIsNotNone(record)
                    self.assertEqual(record["status"], "expired")
                return {"ok": True}

            with patch("polymarket_mvp.tg_approver.tg_post", side_effect=fake_tg_post):
                results = expire_stale_proposals(conn)

            self.assertEqual(len(results), 1)
            record = proposal_record(conn, pid)
            self.assertEqual(record["status"], "expired")

    def test_late_telegram_callback_rejected(self) -> None:
        """Expired proposal callback is rejected, no approval recorded."""
        init_db(self.db_path)
        with connect_db(self.db_path) as conn:
            upsert_market_snapshot(conn, sample_market())
            proposal = sample_proposal()
            upsert_proposal(
                conn, proposal, decision_engine="heuristic",
                status="proposed", context_payload={},
            )
            pid = proposal_id_for(proposal)
            update_proposal_status(conn, pid, "pending_approval")
            past_time = (parse_iso8601(utc_now_iso()) - timedelta(seconds=60)).strftime("%Y-%m-%dT%H:%M:%SZ")
            update_proposal_workflow_fields(
                conn, pid,
                approval_requested_at=past_time,
                approval_expires_at=past_time,
            )
            conn.commit()

        # Simulate a late callback via the Flask app
        from polymarket_mvp.tg_approver import create_app
        with patch("polymarket_mvp.tg_approver.tg_post"):
            app = create_app()
            client = app.test_client()
            resp = client.post("/telegram/webhook", json={
                "callback_query": {
                    "id": "cb_late_1",
                    "data": f"approve:{pid}",
                    "from": {"id": 1, "username": "tester"},
                    "message": {"message_id": 123, "chat": {"id": 456}},
                }
            })
        data = resp.get_json()
        self.assertEqual(data.get("reason"), "expired")

        # Verify no approval was recorded
        with connect_db(self.db_path) as conn:
            record = proposal_record(conn, pid)
            self.assertEqual(record["status"], "pending_approval")
            self.assertIsNone(record.get("approval"))

    def test_autopilot_propose_loop_skips_blocked_crypto_directional_markets(self) -> None:
        os.environ["POLY_BLOCK_CRYPTO_DIRECTIONAL_MARKETS"] = "true"
        self.assertEqual(blocked_market_reason(sample_market()), "blocked_crypto_short_term_directional_market")
        init_db(self.db_path)
        with connect_db(self.db_path) as conn:
            upsert_market_snapshot(conn, sample_market())
            conn.commit()
            from polymarket_mvp.autopilot import Autopilot

            pilot = Autopilot(max_iterations=1)
            with patch("polymarket_mvp.proposer.run_proposal_pipeline") as mocked:
                count = pilot._loop_propose(conn)
        self.assertEqual(count, 0)
        mocked.assert_not_called()

    # --- Stale order cancellation ---

    def test_stale_order_auto_cancelled(self) -> None:
        """Submitted execution with expired TTL is auto-cancelled."""
        init_db(self.db_path)
        with connect_db(self.db_path) as conn:
            upsert_market_snapshot(conn, sample_market())
            proposal = sample_proposal()
            upsert_proposal(
                conn, proposal, decision_engine="heuristic",
                status="executed", context_payload={},
            )
            pid = proposal_id_for(proposal)
            past_time = (parse_iso8601(utc_now_iso()) - timedelta(seconds=600)).strftime("%Y-%m-%dT%H:%M:%SZ")
            execution = record_execution(conn, {
                "proposal_id": pid,
                "mode": "real",
                "client_order_id": f"{pid}-real",
                "order_intent_json": {
                    "order_live_ttl_seconds": 60,
                    "order_posted_at": past_time,
                },
                "requested_price": 0.62,
                "requested_size_usdc": 5.0,
                "max_slippage_bps": 500,
                "observed_worst_price": 0.62,
                "slippage_check_status": "passed",
                "status": "submitted",
                "filled_size_usdc": None,
                "avg_fill_price": None,
                "txhash_or_order_id": "order_stale_123",
                "slippage_bps": 0,
                "error_message": None,
                "created_at": past_time,
                "updated_at": past_time,
            })
            conn.commit()

            # Verify position was created as open_requested
            positions = list_positions(conn)
            self.assertTrue(any(p["status"] == "open_requested" for p in positions))

            # Run cancel_stale_orders (mock CLOB client)
            from polymarket_mvp.services.reconciler import cancel_stale_orders
            mock_client = Mock()
            with patch("polymarket_mvp.services.reconciler._build_clob_client", return_value=mock_client):
                cancelled = cancel_stale_orders(conn)
            conn.commit()

            self.assertEqual(len(cancelled), 1)
            self.assertEqual(cancelled[0]["action"], "auto_cancelled")

            # Verify position is now cancelled
            positions = list_positions(conn, statuses=["cancelled"])
            self.assertEqual(len(positions), 1)

    # --- Exit proposal tests ---

    def test_exit_proposal_creation(self) -> None:
        """OpenClaw close recommendation creates exit proposal."""
        init_db(self.db_path)
        with connect_db(self.db_path) as conn:
            # Use far-future end_date so deterministic rules don't fire
            far_future_market = sample_market()
            far_future_market["end_date"] = "2027-12-31T23:59:59Z"
            far_future_market["seconds_to_expiry"] = 60000000
            upsert_market_snapshot(conn, far_future_market)
            proposal = sample_proposal()
            upsert_proposal(
                conn, proposal, decision_engine="heuristic",
                status="executed", context_payload={},
            )
            pid = proposal_id_for(proposal)
            execution = record_execution(conn, {
                "proposal_id": pid,
                "mode": "mock",
                "client_order_id": f"{pid}-mock",
                "order_intent_json": {},
                "requested_price": 0.62,
                "requested_size_usdc": 5.0,
                "max_slippage_bps": 500,
                "observed_worst_price": 0.62,
                "slippage_check_status": "passed",
                "status": "filled",
                "filled_size_usdc": 5.0,
                "avg_fill_price": 0.62,
                "txhash_or_order_id": None,
                "slippage_bps": 0,
                "error_message": None,
                "created_at": utc_now_iso(),
                "updated_at": utc_now_iso(),
            })
            conn.commit()

            # Get the position
            positions = list_positions(conn, statuses=["open_requested", "open"])
            self.assertTrue(len(positions) >= 1)
            position = positions[0]

            # Mock OpenClaw returning a close recommendation
            mock_exit = [{"position_id": position["id"], "recommendation": "close",
                          "confidence_score": 0.85, "reasoning": "Market sentiment shifted"}]
            with patch("polymarket_mvp.services.openclaw_adapter.is_enabled", return_value=True), \
                 patch("polymarket_mvp.services.openclaw_adapter.maybe_generate_exit_proposals", return_value=mock_exit):
                from polymarket_mvp.agents.exit_agent import run_exit_agent
                rec = run_exit_agent(conn, position, use_llm=True)

            self.assertEqual(rec["recommendation"], "close")
            self.assertAlmostEqual(rec["confidence_score"], 0.85)

            # Create the exit proposal (as run_exit_agent.py --create-proposals would)
            from polymarket_mvp.common import normalize_proposal
            exit_proposal = normalize_proposal({
                "market_id": position["market_id"],
                "outcome": position["outcome"],
                "confidence_score": rec["confidence_score"],
                "recommended_size_usdc": position["size_usdc"],
                "reasoning": rec["reasoning"],
                "max_slippage_bps": 500,
            })
            stored = upsert_proposal(
                conn, exit_proposal, decision_engine="openclaw_llm",
                status="proposed", context_payload={},
                proposal_kind="exit", target_position_id=position["id"],
            )
            conn.commit()

            # Verify exit proposal was created correctly
            exit_record = proposal_record(conn, stored["proposal_id"])
            self.assertIsNotNone(exit_record)
            self.assertEqual(exit_record["proposal_kind"], "exit")
            self.assertEqual(exit_record["target_position_id"], position["id"])

    def test_exit_proposal_deterministic_fallback(self) -> None:
        """When OpenClaw is unavailable, deterministic exit returns hold for far-future market."""
        init_db(self.db_path)
        with connect_db(self.db_path) as conn:
            # Use far-future end_date so deterministic returns "hold"
            far_future_market = sample_market()
            far_future_market["end_date"] = "2027-12-31T23:59:59Z"
            far_future_market["seconds_to_expiry"] = 60000000
            upsert_market_snapshot(conn, far_future_market)
            proposal = sample_proposal()
            upsert_proposal(
                conn, proposal, decision_engine="heuristic",
                status="executed", context_payload={},
            )
            pid = proposal_id_for(proposal)
            record_execution(conn, {
                "proposal_id": pid,
                "mode": "mock",
                "client_order_id": f"{pid}-mock",
                "order_intent_json": {},
                "requested_price": 0.62,
                "requested_size_usdc": 5.0,
                "max_slippage_bps": 500,
                "observed_worst_price": 0.62,
                "slippage_check_status": "passed",
                "status": "filled",
                "filled_size_usdc": 5.0,
                "avg_fill_price": 0.62,
                "txhash_or_order_id": None,
                "slippage_bps": 0,
                "error_message": None,
                "created_at": utc_now_iso(),
                "updated_at": utc_now_iso(),
            })
            conn.commit()

            positions = list_positions(conn, statuses=["open_requested", "open"])
            self.assertTrue(len(positions) >= 1)
            position = positions[0]

            # OpenClaw unavailable
            with patch("polymarket_mvp.services.openclaw_adapter.is_enabled", return_value=False):
                from polymarket_mvp.agents.exit_agent import run_exit_agent
                rec = run_exit_agent(conn, position, use_llm=True)

            # Should fall back to deterministic "hold" (market not near expiry in test)
            self.assertEqual(rec["recommendation"], "hold")

    # --- Autopilot supervisor tests ---

    def test_autopilot_single_tick(self) -> None:
        """Autopilot with max_iterations=1 runs without crashing and records heartbeats."""
        init_db(self.db_path)
        from polymarket_mvp.autopilot import Autopilot

        pilot = Autopilot(max_iterations=1)
        # Mock all external calls to avoid real network requests
        with patch("polymarket_mvp.autopilot.Autopilot._loop_scan", return_value=0), \
             patch("polymarket_mvp.autopilot.Autopilot._loop_context", return_value=0), \
             patch("polymarket_mvp.autopilot.Autopilot._loop_propose", return_value=0), \
             patch("polymarket_mvp.autopilot.Autopilot._loop_expiry", return_value=0), \
             patch("polymarket_mvp.autopilot.Autopilot._loop_execute", return_value=0), \
             patch("polymarket_mvp.autopilot.Autopilot._loop_reconcile", return_value=0), \
             patch("polymarket_mvp.autopilot.Autopilot._loop_exit", return_value=0), \
             patch("polymarket_mvp.autopilot.Autopilot._loop_review", return_value=0):
            pilot.run_forever()

        # Verify heartbeats were recorded
        with connect_db(self.db_path) as conn:
            heartbeats = conn.execute("SELECT DISTINCT loop_name FROM autopilot_heartbeats").fetchall()
            loop_names = {row["loop_name"] for row in heartbeats}
        # All 8 loops should have at least one heartbeat
        self.assertEqual(loop_names, {"scan", "context", "propose", "expiry", "execute", "reconcile", "exit", "review"})

    def test_autopilot_idempotency_no_duplicate_sends(self) -> None:
        """Running two ticks does not duplicate Telegram sends for already-sent proposals."""
        init_db(self.db_path)
        with connect_db(self.db_path) as conn:
            upsert_market_snapshot(conn, sample_market())
            proposal = sample_proposal()
            upsert_proposal(
                conn, proposal, decision_engine="heuristic",
                status="proposed", context_payload={},
            )
            pid = proposal_id_for(proposal)
            update_proposal_status(conn, pid, "pending_approval")
            # Simulate already-sent: set telegram_message_id
            update_proposal_workflow_fields(
                conn, pid,
                telegram_message_id="already_sent_123",
                telegram_chat_id="456",
            )
            conn.commit()

        from polymarket_mvp.autopilot import Autopilot
        pilot = Autopilot(max_iterations=1)

        send_calls = []
        original_send = None

        def mock_send(proposal_ids, **kwargs):
            send_calls.extend(proposal_ids)
            return {"sent_count": len(proposal_ids), "events": []}

        # Only mock the propose loop to test send behavior
        with patch("polymarket_mvp.autopilot.Autopilot._loop_scan", return_value=0), \
             patch("polymarket_mvp.autopilot.Autopilot._loop_context", return_value=0), \
             patch("polymarket_mvp.autopilot.Autopilot._loop_propose", return_value=0), \
             patch("polymarket_mvp.autopilot.Autopilot._loop_expiry", return_value=0), \
             patch("polymarket_mvp.autopilot.Autopilot._loop_execute", return_value=0), \
             patch("polymarket_mvp.autopilot.Autopilot._loop_reconcile", return_value=0), \
             patch("polymarket_mvp.autopilot.Autopilot._loop_exit", return_value=0), \
             patch("polymarket_mvp.autopilot.Autopilot._loop_review", return_value=0):
            pilot.run_forever()

        # No proposals should have been sent (already has telegram_message_id)
        self.assertEqual(send_calls, [])

    # --- Schema migration test ---

    def test_v05_migration_adds_new_columns(self) -> None:
        """Fresh init_db creates proposals table with v0.5 columns."""
        init_db(self.db_path)
        with connect_db(self.db_path) as conn:
            columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(proposals)").fetchall()
            }
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        self.assertIn("proposal_kind", columns)
        self.assertIn("target_position_id", columns)
        self.assertIn("approval_ttl_seconds", columns)
        self.assertIn("order_live_ttl_seconds", columns)
        self.assertIn("approval_requested_at", columns)
        self.assertIn("approval_expires_at", columns)
        self.assertIn("telegram_message_id", columns)
        self.assertIn("telegram_chat_id", columns)
        self.assertIn("autopilot_heartbeats", tables)


if __name__ == "__main__":
    unittest.main()
