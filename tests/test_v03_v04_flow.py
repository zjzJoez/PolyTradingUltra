from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from polymarket_mvp.common import append_jsonl, blocked_market_reason, debug_events_path, parse_iso8601, proposal_id_for, utc_now_iso
from polymarket_mvp.autopilot import Autopilot
from polymarket_mvp.autopilot_status import main as autopilot_status_main
from polymarket_mvp.db import (
    connect_db,
    create_strategy_authorization,
    init_db,
    list_positions,
    market_snapshot,
    proposal_record,
    record_heartbeat,
    record_execution,
    set_kill_switch,
    update_proposal_workflow_fields,
    upsert_market_snapshot,
    upsert_proposal,
    upsert_market_resolution,
)
from polymarket_mvp.ops_snapshot import build_ops_snapshot
from polymarket_mvp.event_fetcher import fetch_contexts_for_market
from polymarket_mvp.poly_executor import execute_record
from polymarket_mvp.proposer import build_openclaw_proposals
from polymarket_mvp.risk_engine import evaluate_full_record, evaluate_proposal
from polymarket_mvp.agents.supervisor_agent import supervise_record
from polymarket_mvp.services.position_manager import sync_all_positions, update_position_marks
from polymarket_mvp.services.openclaw_adapter import chat_json
from polymarket_mvp.services.reconciler import reconcile_live_orders


OLD_SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS market_snapshots (
  market_id TEXT PRIMARY KEY,
  question TEXT,
  slug TEXT,
  market_url TEXT,
  condition_id TEXT,
  active INTEGER NOT NULL DEFAULT 0,
  closed INTEGER NOT NULL DEFAULT 0,
  accepting_orders INTEGER NOT NULL DEFAULT 0,
  end_date TEXT,
  seconds_to_expiry INTEGER,
  days_to_expiry REAL,
  liquidity_usdc REAL,
  volume_usdc REAL,
  volume_24h_usdc REAL,
  outcomes_json TEXT NOT NULL,
  market_json TEXT NOT NULL,
  last_scanned_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS proposals (
  proposal_id TEXT PRIMARY KEY,
  market_id TEXT NOT NULL,
  outcome TEXT NOT NULL,
  confidence_score REAL NOT NULL,
  recommended_size_usdc REAL NOT NULL,
  reasoning TEXT NOT NULL,
  decision_engine TEXT NOT NULL,
  status TEXT NOT NULL,
  max_slippage_bps INTEGER NOT NULL DEFAULT 500,
  proposal_json TEXT NOT NULL,
  context_payload_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
"""


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
        "reasoning": "Deterministic thesis",
        "max_slippage_bps": 500,
    }


def sample_updown_market() -> dict:
    market = sample_market()
    market["market_id"] = "m2"
    market["question"] = "Will SOL be up in the next hour?"
    market["slug"] = "sol-next-hour"
    market["market_url"] = "https://polymarket.com/event/sol-next-hour"
    market["condition_id"] = "cond-2"
    market["outcomes"] = [
        {"name": "Up", "price": 0.44, "token_id": "tok_up"},
        {"name": "Down", "price": 0.56, "token_id": "tok_down"},
    ]
    return market


def sample_extreme_market() -> dict:
    market = sample_market()
    market["market_id"] = "m-extreme"
    market["slug"] = "extreme-market"
    market["condition_id"] = "cond-extreme"
    market["question"] = "Will ETH be above $2300 on April 2?"
    market["outcomes"] = [
        {"name": "Yes", "price": 0.0005, "token_id": "tok_extreme_yes"},
        {"name": "No", "price": 0.9995, "token_id": "tok_extreme_no"},
    ]
    return market


def active_authorization_window() -> tuple[str, str]:
    now = parse_iso8601(utc_now_iso())
    return (
        (now - timedelta(days=1)).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        (now + timedelta(days=30)).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    )


class TradingOSUpgradeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "test.sqlite3"
        os.environ["POLYMARKET_MVP_DB_PATH"] = str(self.db_path)
        os.environ["POLYMARKET_MVP_STATE_DIR"] = self.tmpdir.name
        os.environ["POLYMARKET_AVAILABLE_BALANCE_U"] = "1000"
        os.environ["POLY_RISK_REQUIRE_EXECUTABLE_MARKET"] = "false"
        os.environ["POLY_RISK_MAX_ORDER_USDC"] = "10"
        os.environ["POLY_RISK_MIN_CONFIDENCE"] = "0.5"
        os.environ["POLY_RISK_MAX_SLIPPAGE_BPS"] = "600"
        os.environ["POLY_RISK_MAX_TOPIC_EXPOSURE_USDC"] = "50"
        os.environ["POLY_RISK_MAX_CLUSTER_EXPOSURE_USDC"] = "50"
        os.environ["POLY_RISK_MAX_STRATEGY_DAILY_GROSS_USDC"] = "50"
        os.environ["POLY_BLOCK_CRYPTO_DIRECTIONAL_MARKETS"] = "false"
        os.environ["POLY_RISK_MARKET_CLASS_ENABLED"] = "false"
        os.environ["SIGNATURE_TYPE"] = ""

    def tearDown(self) -> None:
        self.tmpdir.cleanup()
        os.environ.pop("POLYMARKET_MVP_DB_PATH", None)
        os.environ.pop("POLYMARKET_MVP_STATE_DIR", None)
        os.environ.pop("POLY_BLOCK_CRYPTO_DIRECTIONAL_MARKETS", None)
        os.environ.pop("POLY_RISK_MARKET_CLASS_ENABLED", None)
        os.environ.pop("SIGNATURE_TYPE", None)
        os.environ.pop("POLY_AUTOPILOT_MAX_PENDING_APPROVALS", None)

    def test_init_db_contains_new_v03_v04_tables(self) -> None:
        init_db(self.db_path)
        with connect_db(self.db_path) as conn:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                ).fetchall()
            }
        self.assertIn("schema_migrations", tables)
        self.assertIn("event_clusters", tables)
        self.assertIn("research_memos", tables)
        self.assertIn("strategy_authorizations", tables)
        self.assertIn("positions", tables)
        self.assertIn("kill_switches", tables)

    def test_migration_upgrades_old_proposals_table(self) -> None:
        with connect_db(self.db_path) as conn:
            conn.executescript(OLD_SCHEMA)
            conn.commit()
        init_db(self.db_path)
        with connect_db(self.db_path) as conn:
            proposal_columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(proposals)").fetchall()
            }
        self.assertIn("authorization_status", proposal_columns)
        self.assertIn("strategy_name", proposal_columns)
        self.assertIn("source_memo_id", proposal_columns)

    def test_risk_engine_can_authorize_for_execution(self) -> None:
        init_db(self.db_path)
        valid_from, valid_until = active_authorization_window()
        with connect_db(self.db_path) as conn:
            upsert_market_snapshot(conn, sample_market())
            proposal = sample_proposal()
            proposal["recommended_size_usdc"] = 10.0
            upsert_proposal(
                conn,
                proposal,
                decision_engine="heuristic",
                status="proposed",
                context_payload={"topic": "BTC", "assembled_text": "summary"},
                strategy_name="near_expiry_conviction",
                topic="BTC",
                authorization_status="none",
            )
            create_strategy_authorization(
                conn,
                {
                    "strategy_name": "near_expiry_conviction",
                    "scope_topic": "BTC",
                    "scope_market_type": "binary",
                    "scope_event_cluster_id": None,
                    "max_order_usdc": 10,
                    "max_daily_gross_usdc": 50,
                    "max_open_positions": 10,
                    "max_daily_loss_usdc": 50,
                    "max_slippage_bps": 600,
                    "allow_auto_execute": True,
                    "requires_human_if_above_usdc": 10,
                    "valid_from": valid_from,
                    "valid_until": valid_until,
                    "status": "active",
                    "created_by": "test",
                },
            )
            conn.commit()
            record = proposal_record(conn, proposal_id_for(proposal))
            self.assertIsNotNone(record)
            result = evaluate_full_record(conn, record)
        self.assertEqual(result["next_status"], "authorized_for_execution")
        self.assertEqual(result["authorization"]["authorization_status"], "matched_auto_execute")

    def test_risk_engine_routes_limit_reached_to_risk_blocked(self) -> None:
        """Regression for the production CHECK-constraint blow-up:

        evaluate_authorization() can return authorization_status values like
        'daily_loss_limit_reached' / 'position_limit_reached' to signal a
        hard cap was hit. Before the fix, those leaked into
        update_proposal_workflow_fields() and crashed on the CHECK constraint
        every retry, freezing the propose loop on the offending proposal.

        After the fix: limit-reached cases route to next_status='risk_blocked'
        (with the reason persisted to risk_block_reasons_json), and the
        authorization_status that lands in the DB is the safe 'none'.
        """
        from unittest.mock import patch as _patch
        from polymarket_mvp.services.authorization_service import safe_authorization_status

        # The pure normalizer
        self.assertEqual(safe_authorization_status("daily_loss_limit_reached"), "none")
        self.assertEqual(safe_authorization_status("position_limit_reached"), "none")
        self.assertEqual(safe_authorization_status("matched_auto_execute"), "matched_auto_execute")
        self.assertEqual(safe_authorization_status("matched_manual_only"), "matched_manual_only")
        self.assertEqual(safe_authorization_status(None), "none")
        self.assertEqual(safe_authorization_status("garbage_value"), "none")

        # End-to-end: mock the authorization layer to return a limit-reached
        # outcome, verify evaluate_full_record routes to risk_blocked and the
        # DB write goes through cleanly (no CHECK violation).
        init_db(self.db_path)
        with connect_db(self.db_path) as conn:
            upsert_market_snapshot(conn, sample_market())
            proposal = sample_proposal()
            stored = upsert_proposal(
                conn, proposal, decision_engine="heuristic", status="proposed",
                context_payload={"topic": "BTC", "assembled_text": "summary"},
                strategy_name="near_expiry_conviction", topic="BTC",
            )
            conn.commit()
            record = proposal_record(conn, stored["proposal_id"])

            with _patch("polymarket_mvp.risk_engine.evaluate_authorization") as mock_auth:
                mock_auth.return_value = {
                    "authorization_status": "daily_loss_limit_reached",
                    "matched_authorization": {"strategy_name": "near_expiry_conviction"},
                    "reason": "daily_loss=60.00 >= max=50.00",
                }
                result = evaluate_full_record(conn, record)

            self.assertEqual(result["next_status"], "risk_blocked")
            # Persist through the same path the autopilot uses, with the new
            # safe wrapper — this is the call that used to raise IntegrityError.
            from polymarket_mvp.db import update_proposal_workflow_fields
            update_proposal_workflow_fields(
                conn,
                stored["proposal_id"],
                authorization_status=safe_authorization_status(
                    (result.get("authorization") or {}).get("authorization_status")
                ),
                status=result["next_status"],
            )
            conn.commit()

            saved = dict(conn.execute(
                "SELECT status, authorization_status, risk_block_reasons_json FROM proposals WHERE proposal_id = ?",
                (stored["proposal_id"],),
            ).fetchone())
            self.assertEqual(saved["status"], "risk_blocked")
            self.assertEqual(saved["authorization_status"], "none")
            self.assertIn("daily_loss", saved["risk_block_reasons_json"])

    def test_authorized_execution_creates_position_and_kill_switch_blocks(self) -> None:
        init_db(self.db_path)
        with connect_db(self.db_path) as conn:
            upsert_market_snapshot(conn, sample_market())
            proposal = sample_proposal()
            stored = upsert_proposal(
                conn,
                proposal,
                decision_engine="heuristic",
                status="authorized_for_execution",
                context_payload={"topic": "BTC", "assembled_text": "summary"},
                strategy_name="near_expiry_conviction",
                topic="BTC",
            )
            conn.commit()
            record = proposal_record(conn, stored["proposal_id"])
            self.assertIsNotNone(record)
            execution = execute_record(conn, record, mode="mock", session_state={"cumulative_spend_usdc": 0.0})
            saved = conn.execute(
                """
                SELECT *
                FROM executions
                WHERE proposal_id = ?
                """,
                (stored["proposal_id"],),
            ).fetchall()
            self.assertEqual(saved, [])
            from polymarket_mvp.db import record_execution

            record_execution(conn, execution)
            conn.commit()
            positions = list_positions(conn)
            self.assertEqual(len(positions), 1)
            set_kill_switch(conn, scope_type="market", scope_key="m1", reason="halt", created_by="test")
            conn.commit()
            blocked = execute_record(conn, record, mode="mock", session_state={"cumulative_spend_usdc": 0.0})
        self.assertEqual(blocked["status"], "failed")
        self.assertIn("blocked_by_kill_switch", blocked["error_message"])

    def test_risk_engine_falls_back_to_snapshot_price_when_clob_price_404(self) -> None:
        os.environ["POLY_RISK_REQUIRE_EXECUTABLE_MARKET"] = "true"
        init_db(self.db_path)
        with connect_db(self.db_path) as conn:
            upsert_market_snapshot(conn, sample_market())
            proposal = sample_proposal()
            proposal["recommended_size_usdc"] = 10.0
            upsert_proposal(
                conn,
                proposal,
                decision_engine="heuristic",
                status="proposed",
                context_payload={"topic": "BTC", "assembled_text": "summary"},
                strategy_name="near_expiry_conviction",
                topic="BTC",
            )
            conn.commit()
            record = proposal_record(conn, proposal_id_for(proposal))
            mocked = Mock()
            mocked.status_code = 404
            mocked.text = '{"error":"No orderbook exists for the requested token id"}'
            with patch("polymarket_mvp.risk_engine.requests.get", return_value=mocked):
                result = evaluate_full_record(conn, record)
        self.assertNotIn("selected_outcome_has_no_live_price", result["risk_summary"]["reasons"])

    def test_risk_engine_uses_real_balance_floor_when_available(self) -> None:
        init_db(self.db_path)
        with connect_db(self.db_path) as conn:
            upsert_market_snapshot(conn, sample_market())
            proposal = sample_proposal()
            proposal["recommended_size_usdc"] = 10.0
            upsert_proposal(
                conn,
                proposal,
                decision_engine="heuristic",
                status="proposed",
                context_payload={"topic": "BTC", "assembled_text": "summary"},
                strategy_name="near_expiry_conviction",
                topic="BTC",
            )
            conn.commit()
            record = proposal_record(conn, proposal_id_for(proposal))
            self.assertIsNotNone(record)
            with patch("polymarket_mvp.risk_engine._real_available_balance_usdc", return_value=8.52):
                result = evaluate_proposal(record)

        self.assertIn("insufficient_balance", result["risk_summary"]["reasons"])
        self.assertEqual(result["risk_summary"]["real_available_balance_usdc"], 8.52)
        self.assertEqual(result["risk_summary"]["configured_balance_usdc"], 1000.0)

    def test_risk_engine_rejects_extreme_price_market(self) -> None:
        init_db(self.db_path)
        with connect_db(self.db_path) as conn:
            upsert_market_snapshot(conn, sample_extreme_market())
            proposal = {
                "market_id": "m-extreme",
                "outcome": "No",
                "confidence_score": 0.9995,
                "recommended_size_usdc": 5.0,
                "reasoning": "Near-certain market.",
                "max_slippage_bps": 500,
            }
            upsert_proposal(
                conn,
                proposal,
                decision_engine="heuristic",
                status="proposed",
                context_payload={"topic": "ETH", "assembled_text": "summary"},
                strategy_name="near_expiry_conviction",
                topic="ETH",
            )
            conn.commit()
            record = proposal_record(conn, proposal_id_for(proposal))
            self.assertIsNotNone(record)
            result = evaluate_proposal(record)

        self.assertTrue(
            any(reason.startswith("selected_outcome_price_outside_tradable_band") for reason in result["risk_summary"]["reasons"])
        )

    def test_risk_engine_blocks_crypto_short_term_directional_market(self) -> None:
        os.environ["POLY_BLOCK_CRYPTO_DIRECTIONAL_MARKETS"] = "true"
        init_db(self.db_path)
        with connect_db(self.db_path) as conn:
            upsert_market_snapshot(conn, sample_market())
            upsert_proposal(
                conn,
                sample_proposal(),
                decision_engine="heuristic",
                status="proposed",
                context_payload={"topic": "BTC", "assembled_text": "summary"},
                strategy_name="near_expiry_conviction",
                topic="BTC",
            )
            conn.commit()
            record = proposal_record(conn, proposal_id_for(sample_proposal()))
            self.assertIsNotNone(record)
            result = evaluate_proposal(record)

        self.assertIn("blocked_crypto_short_term_directional_market", result["risk_summary"]["reasons"])
        self.assertFalse(result["approved_for_approval_gate"])

    def test_execute_record_blocks_crypto_short_term_directional_market(self) -> None:
        os.environ["POLY_BLOCK_CRYPTO_DIRECTIONAL_MARKETS"] = "true"
        init_db(self.db_path)
        with connect_db(self.db_path) as conn:
            upsert_market_snapshot(conn, sample_market())
            upsert_proposal(
                conn,
                sample_proposal(),
                decision_engine="heuristic",
                status="approved",
                context_payload={"topic": "BTC", "assembled_text": "summary"},
            )
            conn.commit()
            record = proposal_record(conn, proposal_id_for(sample_proposal()))
            self.assertIsNotNone(record)
            execution = execute_record(conn, record, mode="mock")

        self.assertEqual(execution["status"], "failed")
        self.assertEqual(execution["error_message"], "blocked_crypto_short_term_directional_market")

    def test_update_position_marks_skips_open_requested_positions(self) -> None:
        init_db(self.db_path)
        market = sample_market()
        market["outcomes"] = [
            {"name": "Yes", "price": 0.7, "token_id": "tok_yes"},
            {"name": "No", "price": 0.3, "token_id": "tok_no"},
        ]
        with connect_db(self.db_path) as conn:
            upsert_market_snapshot(conn, market)
            stored = upsert_proposal(
                conn,
                sample_proposal(),
                decision_engine="heuristic",
                status="authorized_for_execution",
                context_payload={"topic": "BTC", "assembled_text": "summary"},
                strategy_name="near_expiry_conviction",
                topic="BTC",
            )
            record_execution(
                conn,
                {
                    "proposal_id": stored["proposal_id"],
                    "mode": "mock",
                    "client_order_id": f"{stored['proposal_id']}-submitted",
                    "order_intent_json": {"request": {}},
                    "requested_price": 0.4,
                    "requested_size_usdc": 10.0,
                    "max_slippage_bps": 500,
                    "observed_worst_price": 0.4,
                    "slippage_check_status": "passed",
                    "status": "submitted",
                    "filled_size_usdc": None,
                    "avg_fill_price": None,
                    "txhash_or_order_id": "order-1",
                    "slippage_bps": None,
                    "error_message": None,
                    "created_at": utc_now_iso(),
                    "updated_at": utc_now_iso(),
                },
            )
            conn.commit()

            updated = update_position_marks(conn)
            position = list_positions(conn)[0]

        self.assertEqual(updated, [])
        self.assertEqual(position["status"], "open_requested")
        self.assertEqual(position["last_mark_price"], 0.4)
        self.assertEqual(position["unrealized_pnl"], 0.0)

    def test_update_position_marks_keeps_resolution_idempotent(self) -> None:
        init_db(self.db_path)
        with connect_db(self.db_path) as conn:
            upsert_market_snapshot(conn, sample_market())
            stored = upsert_proposal(
                conn,
                sample_proposal(),
                decision_engine="heuristic",
                status="authorized_for_execution",
                context_payload={"topic": "BTC", "assembled_text": "summary"},
                strategy_name="near_expiry_conviction",
                topic="BTC",
            )
            execution = execute_record(conn, proposal_record(conn, stored["proposal_id"]), mode="mock", session_state={"cumulative_spend_usdc": 0.0})
            record_execution(conn, execution)
            upsert_market_resolution(conn, "m1", "Yes", {"market_id": "m1", "resolved_outcome": "Yes"})
            conn.commit()

            first = update_position_marks(conn)
            sync_all_positions(conn)
            second = update_position_marks(conn)
            events = conn.execute(
                "SELECT event_type FROM position_events WHERE position_id = 1 ORDER BY id ASC"
            ).fetchall()
            position = list_positions(conn)[0]

        self.assertEqual(len(first), 1)
        self.assertEqual(second, [])
        self.assertEqual([row["event_type"] for row in events], ["open", "resolve"])
        self.assertEqual(position["status"], "resolved")
        self.assertEqual(position["last_mark_price"], 1.0)
        self.assertGreater(position["realized_pnl"], 0.0)

    def test_update_position_marks_handles_split_resolution(self) -> None:
        init_db(self.db_path)
        with connect_db(self.db_path) as conn:
            upsert_market_snapshot(conn, sample_market())
            stored = upsert_proposal(
                conn,
                sample_proposal(),
                decision_engine="heuristic",
                status="authorized_for_execution",
                context_payload={"topic": "BTC", "assembled_text": "summary"},
                strategy_name="near_expiry_conviction",
                topic="BTC",
            )
            execution = execute_record(conn, proposal_record(conn, stored["proposal_id"]), mode="mock", session_state={"cumulative_spend_usdc": 0.0})
            record_execution(conn, execution)
            upsert_market_resolution(
                conn,
                "m1",
                "50-50",
                {"market_id": "m1", "outcomes": ["Yes", "No"], "outcomePrices": ["0.5", "0.5"]},
            )
            conn.commit()

            sync_all_positions(conn)
            updated = update_position_marks(conn)
            position = list_positions(conn)[0]

        self.assertEqual(len(updated), 1)
        self.assertEqual(position["status"], "resolved")
        self.assertEqual(position["last_mark_price"], 0.5)
        expected_realized = round((0.5 - float(position["entry_price"])) * float(position["filled_qty"]), 6)
        self.assertAlmostEqual(position["realized_pnl"], expected_realized, places=6)

    def test_sync_all_positions_cancels_existing_open_requested_when_execution_fails(self) -> None:
        init_db(self.db_path)
        with connect_db(self.db_path) as conn:
            upsert_market_snapshot(conn, sample_market())
            stored = upsert_proposal(
                conn,
                sample_proposal(),
                decision_engine="heuristic",
                status="authorized_for_execution",
                context_payload={"topic": "BTC", "assembled_text": "summary"},
                strategy_name="near_expiry_conviction",
                topic="BTC",
            )
            execution = {
                "proposal_id": stored["proposal_id"],
                "mode": "real",
                "client_order_id": f"{stored['proposal_id']}-submitted",
                "order_intent_json": {"request": {}},
                "requested_price": 0.4,
                "requested_size_usdc": 10.0,
                "max_slippage_bps": 500,
                "observed_worst_price": 0.4,
                "slippage_check_status": "passed",
                "status": "submitted",
                "filled_size_usdc": None,
                "avg_fill_price": None,
                "txhash_or_order_id": "order-cancel-test",
                "slippage_bps": None,
                "error_message": None,
                "created_at": utc_now_iso(),
                "updated_at": utc_now_iso(),
            }
            record_execution(conn, execution)
            conn.commit()

            sync_all_positions(conn)
            conn.execute(
                "UPDATE executions SET status = ?, error_message = ?, updated_at = ? WHERE txhash_or_order_id = ?",
                ("failed", "order_not_live", utc_now_iso(), "order-cancel-test"),
            )
            conn.commit()

            sync_all_positions(conn)
            position = list_positions(conn)[0]
            events = conn.execute(
                "SELECT event_type FROM position_events WHERE position_id = 1 ORDER BY id ASC"
            ).fetchall()

        self.assertEqual(position["status"], "cancelled")
        self.assertEqual(events[0]["event_type"], "open")
        self.assertEqual(events[-1]["event_type"], "reconcile")

    def test_sync_all_positions_cancels_existing_open_requested_when_execution_is_canceled(self) -> None:
        init_db(self.db_path)
        with connect_db(self.db_path) as conn:
            upsert_market_snapshot(conn, sample_market())
            stored = upsert_proposal(
                conn,
                sample_proposal(),
                decision_engine="heuristic",
                status="authorized_for_execution",
                context_payload={"topic": "BTC", "assembled_text": "summary"},
                strategy_name="near_expiry_conviction",
                topic="BTC",
            )
            execution = {
                "proposal_id": stored["proposal_id"],
                "mode": "real",
                "client_order_id": f"{stored['proposal_id']}-submitted",
                "order_intent_json": {"request": {}},
                "requested_price": 0.4,
                "requested_size_usdc": 10.0,
                "max_slippage_bps": 500,
                "observed_worst_price": 0.4,
                "slippage_check_status": "passed",
                "status": "submitted",
                "filled_size_usdc": None,
                "avg_fill_price": None,
                "txhash_or_order_id": "order-cancel-market-resolved",
                "slippage_bps": None,
                "error_message": None,
                "created_at": utc_now_iso(),
                "updated_at": utc_now_iso(),
            }
            record_execution(conn, execution)
            conn.commit()

            sync_all_positions(conn)
            # Historical scenario was status='canceled_market_resolved'; that
            # value was removed from the executions.status enum by migration
            # v10 (it had zero code references and didn't fit the canonical
            # state machine). The cancel-on-failure path now flows through
            # the same 'failed' status with an explanatory error_message.
            conn.execute(
                "UPDATE executions SET status = ?, error_message = ?, updated_at = ? WHERE txhash_or_order_id = ?",
                ("failed", "canceled_market_resolved", utc_now_iso(), "order-cancel-market-resolved"),
            )
            conn.commit()

            sync_all_positions(conn)
            position = list_positions(conn)[0]
            events = conn.execute(
                "SELECT event_type FROM position_events WHERE position_id = 1 ORDER BY id ASC"
            ).fetchall()

        self.assertEqual(position["status"], "cancelled")
        self.assertEqual(events[0]["event_type"], "open")
        self.assertEqual(events[-1]["event_type"], "reconcile")

    def test_partial_fill_then_invalid_advances_position_to_resolved(self) -> None:
        """Regression for the production stuck-position-1593 bug:

        Order is placed, goes LIVE, fills partially (10 of 12.5 shares at $0.4),
        then the CLOB invalidates the rest. Market then resolves against us.

        Before the fix, _normalize_order_status leaked "INVALID" as the literal
        lowercase string into executions.status, the reconciler dropped the
        partial fill (only recorded fills on status='filled'), and the
        downstream sync never advanced the position past 'open_requested'.
        After the fix: INVALID → 'failed', partial fill is preserved, position
        becomes 'partially_filled' then 'resolved' with the correct loss.
        """
        init_db(self.db_path)
        with connect_db(self.db_path) as conn:
            upsert_market_snapshot(conn, sample_market())
            stored = upsert_proposal(
                conn,
                sample_proposal(),  # outcome="Yes"
                decision_engine="heuristic",
                status="authorized_for_execution",
                context_payload={"topic": "BTC", "assembled_text": "summary"},
                strategy_name="near_expiry_conviction",
                topic="BTC",
            )
            execution = {
                "proposal_id": stored["proposal_id"],
                "mode": "real",
                "client_order_id": f"{stored['proposal_id']}-submitted",
                "order_intent_json": {"request": {}},
                "requested_price": 0.4,
                "requested_size_usdc": 5.0,
                "max_slippage_bps": 500,
                "observed_worst_price": 0.4,
                "slippage_check_status": "passed",
                "status": "live",
                "filled_size_usdc": None,
                "avg_fill_price": None,
                "txhash_or_order_id": "order-invalid-partial",
                "slippage_bps": None,
                "error_message": None,
                "created_at": utc_now_iso(),
                "updated_at": utc_now_iso(),
            }
            record_execution(conn, execution)
            conn.commit()
            sync_all_positions(conn)  # creates the open_requested row

            # CLOB returns INVALID after a partial fill — mirrors the exact
            # production payload that froze position 1593.
            fake_client = Mock()
            fake_client.get_order.return_value = {
                "id": "order-invalid-partial",
                "status": "INVALID",
                "size_matched": "10.0",
                "price": "0.4",
                "original_size": "12.5",
            }
            with patch(
                "polymarket_mvp.services.reconciler._build_clob_client",
                return_value=fake_client,
            ):
                reconcile_live_orders(conn)
            conn.commit()

            # Verify execution captured the partial fill (was dropped before the fix)
            exec_row = dict(conn.execute(
                "SELECT status, filled_size_usdc, avg_fill_price FROM executions WHERE txhash_or_order_id = ?",
                ("order-invalid-partial",),
            ).fetchone())
            self.assertEqual(exec_row["status"], "failed")  # not the literal "invalid" anymore
            self.assertAlmostEqual(exec_row["filled_size_usdc"], 4.0, places=4)
            self.assertAlmostEqual(exec_row["avg_fill_price"], 0.4, places=4)

            # Sync should now advance the position to partially_filled
            sync_all_positions(conn)
            position = list_positions(conn)[0]
            self.assertEqual(position["status"], "partially_filled")
            self.assertAlmostEqual(float(position["filled_qty"]), 10.0, places=4)
            self.assertAlmostEqual(float(position["entry_price"]), 0.4, places=4)

            # Market resolves "No" — we bet "Yes" → full loss on the 10 filled shares
            upsert_market_resolution(
                conn,
                "m1",
                "No",
                {"outcomes": ["Yes", "No"], "outcomePrices": ["0", "1"]},
            )
            conn.commit()
            update_position_marks(conn)

            position = list_positions(conn)[0]
            self.assertEqual(position["status"], "resolved")
            self.assertAlmostEqual(float(position["realized_pnl"]), -4.0, places=4)

    def test_portfolio_risk_blocks_duplicate_market_outcome_entry_when_active_exposure_exists(self) -> None:
        init_db(self.db_path)
        with connect_db(self.db_path) as conn:
            upsert_market_snapshot(conn, sample_market())
            first = upsert_proposal(
                conn,
                sample_proposal(),
                decision_engine="heuristic",
                status="authorized_for_execution",
                context_payload={"topic": "BTC", "assembled_text": "summary"},
                strategy_name="near_expiry_conviction",
                topic="BTC",
            )
            record_execution(
                conn,
                {
                    "proposal_id": first["proposal_id"],
                    "mode": "real",
                    "client_order_id": f"{first['proposal_id']}-filled",
                    "order_intent_json": {"request": {}},
                    "requested_price": 0.62,
                    "requested_size_usdc": 5.0,
                    "max_slippage_bps": 500,
                    "observed_worst_price": 0.62,
                    "slippage_check_status": "passed",
                    "status": "filled",
                    "filled_size_usdc": 5.0,
                    "avg_fill_price": 0.62,
                    "txhash_or_order_id": "order-duplicate-risk",
                    "slippage_bps": 0,
                    "error_message": None,
                    "created_at": utc_now_iso(),
                    "updated_at": utc_now_iso(),
                },
            )
            second = upsert_proposal(
                conn,
                sample_proposal(),
                decision_engine="heuristic",
                status="proposed",
                context_payload={"topic": "BTC", "assembled_text": "summary"},
                strategy_name="near_expiry_conviction",
                topic="BTC",
            )
            conn.commit()

            result = evaluate_full_record(conn, proposal_record(conn, second["proposal_id"]))

        self.assertEqual(result["next_status"], "risk_blocked")
        self.assertIn("market_outcome_exposure_exists", result["portfolio_risk"]["reasons"])

    def test_portfolio_risk_blocks_duplicate_market_outcome_entry_when_pending_entry_exists(self) -> None:
        init_db(self.db_path)
        with connect_db(self.db_path) as conn:
            upsert_market_snapshot(conn, sample_market())
            upsert_proposal(
                conn,
                sample_proposal(),
                decision_engine="heuristic",
                status="pending_approval",
                context_payload={"topic": "BTC", "assembled_text": "summary"},
                strategy_name="near_expiry_conviction",
                topic="BTC",
            )
            duplicate = sample_proposal()
            duplicate["reasoning"] = "Second copy"
            stored = upsert_proposal(
                conn,
                duplicate,
                decision_engine="heuristic",
                status="proposed",
                context_payload={"topic": "BTC", "assembled_text": "summary"},
                strategy_name="near_expiry_conviction",
                topic="BTC",
            )
            conn.commit()

            result = evaluate_full_record(conn, proposal_record(conn, stored["proposal_id"]))

        self.assertEqual(result["next_status"], "risk_blocked")
        self.assertIn("market_outcome_entry_already_pending", result["portfolio_risk"]["reasons"])

    def test_build_openclaw_proposals_generates_from_adapter(self) -> None:
        market = sample_market()
        # Use a non-crypto question so category scoring doesn't deprioritise the
        # market to zero — this test exercises the proposal pipeline, not the
        # crypto-block path. Bump expiry past the directional cutoff for the
        # same reason.
        market["question"] = "Will the new bill be approved by the senate by next week?"
        market["slug"] = "senate-bill-approval"
        market["days_to_expiry"] = 5.0
        market["seconds_to_expiry"] = 432000
        markets = [market]
        context_file = {
            "markets": [
                {
                    "market_id": "m1",
                    "context_payload": {
                        "topic": "Senate",
                        "assembled_text": "Whip count tightening; vote scheduled this week.",
                        "sources": [{"source_type": "news", "display_text": "Whip count tightening"}],
                    },
                }
            ]
        }
        with patch("polymarket_mvp.proposer.poly_proposer_generate") as mocked, \
             patch.dict(os.environ, {"POLY_SIZING_MODE": "flat"}, clear=False):
            mocked.return_value = [
                {
                    "market_id": "m1",
                    "outcome": "Yes",
                    "confidence_score": 0.71,
                    "recommended_size_usdc": 7.5,
                    "reasoning": "Context points toward Yes in the near term.",
                    "max_slippage_bps": 400,
                }
            ]
            proposals, _meta, _conv = build_openclaw_proposals(
                markets,
                context_file=context_file,
                size_usdc=5.0,
                top=3,
                max_slippage_bps=500,
            )

        self.assertEqual(len(proposals), 1)
        self.assertEqual(proposals[0]["market_id"], "m1")
        self.assertEqual(proposals[0]["outcome"], "Yes")
        self.assertEqual(proposals[0]["recommended_size_usdc"], 7.5)

    def test_build_openclaw_proposals_rejects_invalid_market_outcome(self) -> None:
        # Use a non-crypto market so the new category multiplier doesn't zero
        # the score and skip the LLM call entirely (we need the LLM mock to
        # fire for the invalid-outcome assertion). Keep Up/Down outcome names
        # so the mocked LLM's "No" still trips the validator.
        market = sample_updown_market()
        market["question"] = "Will the bill pass the senate vote next week?"
        market["slug"] = "senate-vote-pass"
        market["days_to_expiry"] = 5.0
        market["seconds_to_expiry"] = 432000
        markets = [market]
        with patch("polymarket_mvp.proposer.poly_proposer_generate") as mocked:
            mocked.return_value = [
                {
                    "market_id": "m2",
                    "outcome": "No",
                    "confidence_score": 0.66,
                    "recommended_size_usdc": 1.0,
                    "reasoning": "Bearish setup.",
                    "max_slippage_bps": 500,
                }
            ]
            with self.assertRaises(RuntimeError) as exc:
                build_openclaw_proposals(
                    markets,
                    context_file=None,
                    size_usdc=1.0,
                    top=1,
                    max_slippage_bps=500,
                )

        self.assertIn("invalid outcomes", str(exc.exception))
        self.assertIn("allowed=['Up', 'Down']", str(exc.exception))

    def test_blocked_market_reason_targets_crypto_short_term_directional_only(self) -> None:
        os.environ["POLY_BLOCK_CRYPTO_DIRECTIONAL_MARKETS"] = "true"
        self.assertEqual(blocked_market_reason(sample_market()), "blocked_crypto_short_term_directional_market")
        self.assertIsNone(blocked_market_reason(sample_extreme_market()))

    def test_build_openclaw_proposals_filters_extreme_price_candidates(self) -> None:
        markets = [sample_extreme_market()]
        with patch("polymarket_mvp.proposer.poly_proposer_generate") as mocked:
            mocked.return_value = [
                {
                    "market_id": "m-extreme",
                    "outcome": "No",
                    "confidence_score": 0.9995,
                    "recommended_size_usdc": 5.0,
                    "reasoning": "Certain but low-upside.",
                    "max_slippage_bps": 500,
                }
            ]
            proposals, _meta, _conv = build_openclaw_proposals(
                markets,
                context_file=None,
                size_usdc=5.0,
                top=1,
                max_slippage_bps=500,
            )

        self.assertEqual(proposals, [])

    def test_openclaw_cli_json_is_parsed_from_wrapped_response(self) -> None:
        completed = Mock()
        completed.returncode = 0
        completed.stdout = (
            '{"result":{"messages":[{"role":"assistant","content":"'
            '{\\"thesis\\":\\"Alpha\\",\\"supporting_evidence\\":[\\"One\\"],'
            '\\"counter_evidence\\":[],\\"uncertainty_notes\\":\\"Low\\"}"}]}}'
        )
        completed.stderr = ""
        with patch.dict(
            os.environ,
            {
                "OPENCLAW_TRANSPORT": "cli",
                "OPENCLAW_CLI_PATH": "/usr/local/bin/openclaw",
            },
            clear=False,
        ):
            with patch("polymarket_mvp.services.openclaw_adapter._cli_path", return_value="/usr/local/bin/openclaw"):
                with patch("polymarket_mvp.services.openclaw_adapter.subprocess.run", return_value=completed):
                    payload = chat_json("Return JSON only.", '{"market_id":"m1"}')

        self.assertIsNotNone(payload)
        self.assertEqual(payload["thesis"], "Alpha")

    def test_openclaw_cli_json_is_parsed_from_payloads_response(self) -> None:
        completed = Mock()
        completed.returncode = 0
        completed.stdout = (
            '{"payloads":[{"text":"{\\"thesis\\":\\"Bravo\\",\\"supporting_evidence\\":[\\"Two\\"],'
            '\\"counter_evidence\\":[],\\"uncertainty_notes\\":\\"Medium\\"}","mediaUrl":null}],"meta":{"aborted":false}}'
        )
        completed.stderr = ""
        with patch.dict(
            os.environ,
            {
                "OPENCLAW_TRANSPORT": "cli",
                "OPENCLAW_CLI_PATH": "/usr/local/bin/openclaw",
            },
            clear=False,
        ):
            with patch("polymarket_mvp.services.openclaw_adapter._cli_path", return_value="/usr/local/bin/openclaw"):
                with patch("polymarket_mvp.services.openclaw_adapter.subprocess.run", return_value=completed):
                    payload = chat_json("Return JSON only.", '{"market_id":"m1"}')

        self.assertIsNotNone(payload)
        self.assertEqual(payload["thesis"], "Bravo")

    def test_openclaw_cli_json_can_be_read_from_stderr_logs(self) -> None:
        completed = Mock()
        completed.returncode = 0
        completed.stdout = ""
        completed.stderr = (
            '[agents/model-providers] bootstrap config fallback\\n'
            '{"payloads":[{"text":"{\\"thesis\\":\\"Charlie\\",\\"supporting_evidence\\":[\\"Three\\"],'
            '\\"counter_evidence\\":[],\\"uncertainty_notes\\":\\"High\\"}","mediaUrl":null}],"meta":{"aborted":false}}'
        )
        with patch.dict(
            os.environ,
            {
                "OPENCLAW_TRANSPORT": "cli",
                "OPENCLAW_CLI_PATH": "/usr/local/bin/openclaw",
            },
            clear=False,
        ):
            with patch("polymarket_mvp.services.openclaw_adapter._cli_path", return_value="/usr/local/bin/openclaw"):
                with patch("polymarket_mvp.services.openclaw_adapter.subprocess.run", return_value=completed):
                    payload = chat_json("Return JSON only.", '{"market_id":"m1"}')

        self.assertIsNotNone(payload)
        self.assertEqual(payload["thesis"], "Charlie")

    def test_supervisor_falls_back_when_openclaw_raises(self) -> None:
        record = {
            "proposal_json": sample_proposal(),
            "strategy_name": "near_expiry_conviction",
            "topic": "BTC",
            "event_cluster_id": 7,
            "context_payload_json": {"topic": "BTC"},
        }
        with patch("polymarket_mvp.agents.supervisor_agent.maybe_generate_supervisor_decision", side_effect=RuntimeError("boom")):
            supervisor = supervise_record(record)

        self.assertEqual(supervisor["strategy_name"], "near_expiry_conviction")
        self.assertEqual(supervisor["topic"], "BTC")
        self.assertEqual(supervisor["decision"], "promote")
        self.assertEqual(supervisor["priority_score"], 0.62)

    def test_fetch_contexts_for_market_soft_fails_cryptopanic(self) -> None:
        with patch("polymarket_mvp.event_fetcher.PerplexityAdapter.fetch", return_value=[]):
            with patch(
                "polymarket_mvp.event_fetcher.CryptoPanicAdapter.fetch",
                side_effect=RuntimeError("502 bad gateway"),
            ):
                with patch(
                    "polymarket_mvp.event_fetcher.ApifyTwitterAdapter.fetch",
                    return_value=[
                        {
                            "source_type": "apify_twitter",
                            "source_id": "tweet-1",
                            "title": None,
                            "published_at": utc_now_iso(),
                            "url": "https://x.com/example/status/1",
                            "raw_text": "tweet body",
                            "display_text": "X: market chatter",
                            "importance_weight": 0.5,
                            "normalized_payload_json": {"full_text": "market chatter"},
                        }
                    ],
                ):
                    result = fetch_contexts_for_market(
                        sample_market(),
                        providers=["perplexity", "cryptopanic", "apify_twitter"],
                        limit=3,
                        min_favorite_count=0,
                        budget_chars=1200,
                    )

        contexts = result["contexts"]
        self.assertEqual(len(contexts), 2)
        self.assertEqual(contexts[0]["source_type"], "cryptopanic")
        self.assertEqual(contexts[0]["source_id"], "soft_fail")
        self.assertTrue(contexts[0]["normalized_payload_json"]["soft_fail"])
        self.assertIn("temporarily unavailable", contexts[0]["display_text"])
        self.assertEqual(contexts[1]["source_type"], "apify_twitter")
        self.assertIn("market chatter", result["context_payload"]["assembled_text"])

    def test_reconciler_fills_live_order_and_position_updates(self) -> None:
        init_db(self.db_path)
        with connect_db(self.db_path) as conn:
            upsert_market_snapshot(conn, sample_market())
            stored = upsert_proposal(
                conn,
                sample_proposal(),
                decision_engine="heuristic",
                status="authorized_for_execution",
                context_payload={"topic": "BTC", "assembled_text": "summary"},
                strategy_name="near_expiry_conviction",
                topic="BTC",
            )
            record_execution(
                conn,
                {
                    "proposal_id": stored["proposal_id"],
                    "mode": "real",
                    "client_order_id": f"{stored['proposal_id']}-submitted",
                    "order_intent_json": {"request": {}},
                    "requested_price": 0.62,
                    "requested_size_usdc": 5.0,
                    "max_slippage_bps": 500,
                    "observed_worst_price": 0.62,
                    "slippage_check_status": "passed",
                    "status": "submitted",
                    "filled_size_usdc": None,
                    "avg_fill_price": None,
                    "txhash_or_order_id": "order-fill-lifecycle",
                    "slippage_bps": None,
                    "error_message": None,
                    "created_at": utc_now_iso(),
                    "updated_at": utc_now_iso(),
                },
            )
            conn.commit()

            sync_all_positions(conn)
            position_before = list_positions(conn)[0]
            self.assertEqual(position_before["status"], "open_requested")

            mock_client = Mock()
            mock_client.get_order.return_value = {
                "status": "MATCHED",
                "size_matched": "8.064516",
                "price": "0.62",
            }
            with patch("polymarket_mvp.services.reconciler._build_clob_client", return_value=mock_client):
                reconciliations = reconcile_live_orders(conn)
            conn.commit()

            self.assertEqual(len(reconciliations), 1)
            self.assertEqual(reconciliations[0]["observed_status"], "filled")

            sync_all_positions(conn)
            position_after = list_positions(conn)[0]
            self.assertEqual(position_after["status"], "open")
            self.assertIsNotNone(position_after["entry_price"])

    def test_reconciler_cancels_live_order_and_position_updates(self) -> None:
        init_db(self.db_path)
        with connect_db(self.db_path) as conn:
            upsert_market_snapshot(conn, sample_market())
            stored = upsert_proposal(
                conn,
                sample_proposal(),
                decision_engine="heuristic",
                status="authorized_for_execution",
                context_payload={"topic": "BTC", "assembled_text": "summary"},
                strategy_name="near_expiry_conviction",
                topic="BTC",
            )
            record_execution(
                conn,
                {
                    "proposal_id": stored["proposal_id"],
                    "mode": "real",
                    "client_order_id": f"{stored['proposal_id']}-submitted",
                    "order_intent_json": {"request": {}},
                    "requested_price": 0.62,
                    "requested_size_usdc": 5.0,
                    "max_slippage_bps": 500,
                    "observed_worst_price": 0.62,
                    "slippage_check_status": "passed",
                    "status": "submitted",
                    "filled_size_usdc": None,
                    "avg_fill_price": None,
                    "txhash_or_order_id": "order-cancel-lifecycle",
                    "slippage_bps": None,
                    "error_message": None,
                    "created_at": utc_now_iso(),
                    "updated_at": utc_now_iso(),
                },
            )
            conn.commit()

            sync_all_positions(conn)
            position_before = list_positions(conn)[0]
            self.assertEqual(position_before["status"], "open_requested")

            mock_client = Mock()
            mock_client.get_order.return_value = {
                "status": "CANCELED",
                "size_matched": "0",
                "price": "0.62",
            }
            with patch("polymarket_mvp.services.reconciler._build_clob_client", return_value=mock_client):
                reconciliations = reconcile_live_orders(conn)
            conn.commit()

            self.assertEqual(len(reconciliations), 1)
            self.assertEqual(reconciliations[0]["observed_status"], "failed")

            sync_all_positions(conn)
            position_after = list_positions(conn)[0]
            self.assertEqual(position_after["status"], "cancelled")

            events = conn.execute(
                "SELECT event_type FROM position_events WHERE position_id = ? ORDER BY id ASC",
                (position_after["id"],),
            ).fetchall()
            event_types = [e["event_type"] for e in events]
            self.assertIn("open", event_types)
            self.assertIn("reconcile", event_types)

    def test_risk_engine_blocks_on_gamma_clob_divergence(self) -> None:
        os.environ["POLY_RISK_REQUIRE_EXECUTABLE_MARKET"] = "true"
        os.environ["POLY_RISK_MAX_GAMMA_CLOB_DIVERGENCE_BPS"] = "500"
        init_db(self.db_path)
        with connect_db(self.db_path) as conn:
            upsert_market_snapshot(conn, sample_market())
            proposal = sample_proposal()
            upsert_proposal(
                conn,
                proposal,
                decision_engine="heuristic",
                status="proposed",
                context_payload={"topic": "BTC", "assembled_text": "summary"},
                strategy_name="near_expiry_conviction",
                topic="BTC",
            )
            conn.commit()
            record = proposal_record(conn, proposal_id_for(proposal))
            mocked = Mock()
            mocked.status_code = 200
            mocked.json.return_value = {"price": "0.999"}
            mocked.raise_for_status = Mock()
            with patch("polymarket_mvp.risk_engine.requests.get", return_value=mocked):
                result = evaluate_full_record(conn, record)
        self.assertIn("gamma_clob_price_divergence_exceeded", result["risk_summary"]["reasons"])
        self.assertEqual(result["next_status"], "risk_blocked")

    def test_risk_engine_passes_when_gamma_clob_divergence_within_bounds(self) -> None:
        os.environ["POLY_RISK_REQUIRE_EXECUTABLE_MARKET"] = "true"
        os.environ["POLY_RISK_MAX_GAMMA_CLOB_DIVERGENCE_BPS"] = "500"
        init_db(self.db_path)
        valid_from, valid_until = active_authorization_window()
        with connect_db(self.db_path) as conn:
            upsert_market_snapshot(conn, sample_market())
            proposal = sample_proposal()
            upsert_proposal(
                conn,
                proposal,
                decision_engine="heuristic",
                status="proposed",
                context_payload={"topic": "BTC", "assembled_text": "summary"},
                strategy_name="near_expiry_conviction",
                topic="BTC",
                authorization_status="none",
            )
            create_strategy_authorization(
                conn,
                {
                    "strategy_name": "near_expiry_conviction",
                    "scope_topic": "BTC",
                    "scope_market_type": "binary",
                    "scope_event_cluster_id": None,
                    "max_order_usdc": 10,
                    "max_daily_gross_usdc": 50,
                    "max_open_positions": 10,
                    "max_daily_loss_usdc": 50,
                    "max_slippage_bps": 600,
                    "allow_auto_execute": True,
                    "requires_human_if_above_usdc": 10,
                    "valid_from": valid_from,
                    "valid_until": valid_until,
                    "status": "active",
                    "created_by": "test",
                },
            )
            conn.commit()
            record = proposal_record(conn, proposal_id_for(proposal))
            mocked = Mock()
            mocked.status_code = 200
            mocked.json.return_value = {"price": "0.63"}
            mocked.raise_for_status = Mock()
            with patch("polymarket_mvp.risk_engine.requests.get", return_value=mocked):
                result = evaluate_full_record(conn, record)
        self.assertNotIn("gamma_clob_price_divergence_exceeded", result["risk_summary"]["reasons"])
        self.assertEqual(result["next_status"], "authorized_for_execution")

    def test_build_ops_snapshot_returns_health_orders_positions_and_attention(self) -> None:
        init_db(self.db_path)
        now = parse_iso8601(utc_now_iso())
        with connect_db(self.db_path) as conn:
            upsert_market_snapshot(conn, sample_market())
            pending_proposal = sample_proposal()
            pending = upsert_proposal(
                conn,
                pending_proposal,
                decision_engine="openclaw_llm",
                status="pending_approval",
                context_payload={"topic": "BTC", "assembled_text": "summary"},
                strategy_name="near_expiry_conviction",
                topic="BTC",
                approval_ttl_seconds=120,
                order_live_ttl_seconds=180,
            )
            update_proposal_workflow_fields(
                conn,
                pending["proposal_id"],
                approval_requested_at=utc_now_iso(),
                approval_expires_at=(now + timedelta(seconds=45)).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
                telegram_message_id="99",
                telegram_chat_id="123",
            )

            live_proposal = sample_proposal()
            live_proposal["outcome"] = "No"
            stored = upsert_proposal(
                conn,
                live_proposal,
                decision_engine="openclaw_llm",
                status="approved",
                context_payload={"topic": "BTC", "assembled_text": "summary"},
                strategy_name="near_expiry_conviction",
                topic="BTC",
                approval_ttl_seconds=120,
                order_live_ttl_seconds=180,
            )
            execution = {
                "proposal_id": stored["proposal_id"],
                "mode": "real",
                "client_order_id": f"{stored['proposal_id']}-real",
                "order_intent_json": {
                    "order_live_ttl_seconds": 180,
                    "order_posted_at": (now - timedelta(seconds=30)).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
                },
                "requested_price": 0.62,
                "requested_size_usdc": 5.0,
                "max_slippage_bps": 500,
                "observed_worst_price": 0.62,
                "slippage_check_status": "passed",
                "status": "live",
                "filled_size_usdc": None,
                "avg_fill_price": None,
                "txhash_or_order_id": "order-123",
                "slippage_bps": 0.0,
                "error_message": None,
                "created_at": (now - timedelta(seconds=30)).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
                "updated_at": utc_now_iso(),
            }
            record_execution(conn, execution)
            record_heartbeat(
                conn,
                "scan",
                (now - timedelta(seconds=5)).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
                utc_now_iso(),
                4,
                None,
            )
            record_heartbeat(
                conn,
                "execute",
                (now - timedelta(seconds=40)).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
                utc_now_iso(),
                0,
                "top-level error",
            )
            set_kill_switch(conn, scope_type="global", scope_key="*", reason="maintenance", created_by="test")
            conn.commit()

            snapshot = build_ops_snapshot(conn)

        self.assertEqual(snapshot["pending_count"], 1)
        self.assertEqual(snapshot["live_order_count"], 1)
        self.assertEqual(snapshot["open_position_count"], 1)
        self.assertTrue(any(item["loop"] == "scan" for item in snapshot["system_health"]))
        self.assertTrue(any(item["loop"] == "execute" and item["health"] != "green" for item in snapshot["system_health"]))
        self.assertIsNotNone(snapshot["pending_approvals"][0]["seconds_remaining"])
        self.assertEqual(snapshot["live_orders"][0]["order_id"], "order-123")
        self.assertTrue(snapshot["control_state"]["kill_switches"])
        self.assertTrue(snapshot["needs_attention"])

    def test_build_ops_snapshot_normalizes_failure_categories(self) -> None:
        init_db(self.db_path)
        with connect_db(self.db_path) as conn:
            upsert_market_snapshot(conn, sample_market())

            slippage = sample_proposal()
            slippage["outcome"] = "Yes"
            slippage_record = upsert_proposal(
                conn,
                slippage,
                decision_engine="openclaw_llm",
                status="failed",
                context_payload={"topic": "BTC"},
            )
            record_execution(
                conn,
                {
                    "proposal_id": slippage_record["proposal_id"],
                    "mode": "real",
                    "client_order_id": None,
                    "order_intent_json": {},
                    "requested_price": None,
                    "requested_size_usdc": 5.0,
                    "max_slippage_bps": 500,
                    "observed_worst_price": None,
                    "slippage_check_status": "failed",
                    "status": "failed",
                    "filled_size_usdc": 0.0,
                    "avg_fill_price": None,
                    "txhash_or_order_id": None,
                    "slippage_bps": None,
                    "error_message": "slippage_exceeded",
                    "created_at": utc_now_iso(),
                    "updated_at": utc_now_iso(),
                },
            )

            allowance = sample_proposal()
            allowance["outcome"] = "No"
            allowance_record = upsert_proposal(
                conn,
                allowance,
                decision_engine="openclaw_llm",
                status="failed",
                context_payload={"topic": "BTC"},
            )
            record_execution(
                conn,
                {
                    "proposal_id": allowance_record["proposal_id"],
                    "mode": "real",
                    "client_order_id": None,
                    "order_intent_json": {},
                    "requested_price": None,
                    "requested_size_usdc": 5.0,
                    "max_slippage_bps": 500,
                    "observed_worst_price": None,
                    "slippage_check_status": "skipped",
                    "status": "failed",
                    "filled_size_usdc": 0.0,
                    "avg_fill_price": None,
                    "txhash_or_order_id": None,
                    "slippage_bps": None,
                    "error_message": "order_submit_failed (likely missing spender approval): allowance: 0",
                    "created_at": utc_now_iso(),
                    "updated_at": utc_now_iso(),
                },
            )

            record_heartbeat(conn, "reconcile", utc_now_iso(), utc_now_iso(), 0, "boom")
            conn.commit()
            snapshot = build_ops_snapshot(conn)

        categories = {item["category"] for item in snapshot["recent_failures"]}
        self.assertIn("slippage_exceeded", categories)
        self.assertIn("allowance_missing", categories)
        self.assertIn("autopilot_loop_error", categories)

    def test_autopilot_status_cli_emits_shared_snapshot(self) -> None:
        """The `autopilot-status` CLI should emit a JSON snapshot with the same
        keys that the ops dashboard used to read (kept as a thin contract test
        after the Flask /ops route was retired with the Telegram surface)."""
        init_db(self.db_path)
        buffer = io.StringIO()
        with patch("sys.argv", ["autopilot-status"]):
            with redirect_stdout(buffer):
                rc = autopilot_status_main()
        self.assertEqual(rc, 0)
        cli_payload = json.loads(buffer.getvalue())
        self.assertIn("pending_approvals", cli_payload)
        self.assertIn("recent_events", cli_payload)

    def test_executor_blocks_duplicate_execution_for_same_proposal(self) -> None:
        """A proposal that already has a filled execution cannot be executed again."""
        init_db(self.db_path)
        with connect_db(self.db_path) as conn:
            upsert_market_snapshot(conn, sample_market())
            stored = upsert_proposal(
                conn,
                sample_proposal(),
                decision_engine="heuristic",
                status="authorized_for_execution",
                context_payload={"topic": "BTC", "assembled_text": "summary"},
                strategy_name="near_expiry_conviction",
                topic="BTC",
            )
            conn.commit()
            record = proposal_record(conn, stored["proposal_id"])
            first = execute_record(conn, record, mode="mock")
            record_execution(conn, first)
            conn.commit()
            self.assertEqual(first["status"], "filled")

            # Re-fetch the record (status may have changed to 'executed')
            record2 = proposal_record(conn, stored["proposal_id"])
            # Force status back so the "not authorized" check doesn't fire first
            record2["status"] = "authorized_for_execution"
            second = execute_record(conn, record2, mode="mock")

        self.assertEqual(second["status"], "failed")
        self.assertEqual(second["error_message"], "proposal_already_has_active_execution")

    def test_executor_blocks_duplicate_market_outcome_exposure(self) -> None:
        """A second entry proposal on the same market_id + outcome is blocked at execution."""
        init_db(self.db_path)
        with connect_db(self.db_path) as conn:
            upsert_market_snapshot(conn, sample_market())
            # First proposal: execute and fill
            p1 = sample_proposal()
            stored1 = upsert_proposal(
                conn,
                p1,
                decision_engine="heuristic",
                status="authorized_for_execution",
                context_payload={"topic": "BTC", "assembled_text": "summary"},
                strategy_name="near_expiry_conviction",
                topic="BTC",
            )
            conn.commit()
            rec1 = proposal_record(conn, stored1["proposal_id"])
            exec1 = execute_record(conn, rec1, mode="mock")
            record_execution(conn, exec1)
            conn.commit()
            self.assertEqual(exec1["status"], "filled")

            # Second proposal: different reasoning so different proposal_id, same market+outcome
            p2 = sample_proposal()
            p2["reasoning"] = "Different thesis, same direction"
            stored2 = upsert_proposal(
                conn,
                p2,
                decision_engine="heuristic",
                status="authorized_for_execution",
                context_payload={"topic": "BTC", "assembled_text": "summary v2"},
                strategy_name="near_expiry_conviction",
                topic="BTC",
            )
            conn.commit()
            rec2 = proposal_record(conn, stored2["proposal_id"])
            exec2 = execute_record(conn, rec2, mode="mock")

        self.assertEqual(exec2["status"], "failed")
        self.assertEqual(exec2["error_message"], "duplicate_market_outcome_exposure")

    def test_executor_allows_exit_despite_existing_exposure(self) -> None:
        """Exit proposals must not be blocked by the duplicate exposure guard."""
        init_db(self.db_path)
        with connect_db(self.db_path) as conn:
            upsert_market_snapshot(conn, sample_market())
            # Entry proposal: execute and fill
            stored1 = upsert_proposal(
                conn,
                sample_proposal(),
                decision_engine="heuristic",
                status="authorized_for_execution",
                context_payload={"topic": "BTC", "assembled_text": "summary"},
                strategy_name="near_expiry_conviction",
                topic="BTC",
            )
            conn.commit()
            rec1 = proposal_record(conn, stored1["proposal_id"])
            exec1 = execute_record(conn, rec1, mode="mock")
            record_execution(conn, exec1)
            conn.commit()

            # Exit proposal on the same market+outcome
            p_exit = sample_proposal()
            p_exit["reasoning"] = "Exit thesis"
            stored2 = upsert_proposal(
                conn,
                p_exit,
                decision_engine="heuristic",
                status="authorized_for_execution",
                context_payload={"topic": "BTC", "assembled_text": "exit"},
                strategy_name="near_expiry_conviction",
                topic="BTC",
                proposal_kind="exit",
            )
            conn.commit()
            rec2 = proposal_record(conn, stored2["proposal_id"])
            exec2 = execute_record(conn, rec2, mode="mock")

        # Exit should NOT be blocked by duplicate exposure guard
        self.assertNotEqual(exec2.get("error_message"), "duplicate_market_outcome_exposure")

    def test_executor_allows_same_market_different_outcome(self) -> None:
        """Entries on the same market but different outcomes should not block each other."""
        init_db(self.db_path)
        with connect_db(self.db_path) as conn:
            upsert_market_snapshot(conn, sample_market())
            # First proposal: Yes
            stored1 = upsert_proposal(
                conn,
                sample_proposal(),
                decision_engine="heuristic",
                status="authorized_for_execution",
                context_payload={"topic": "BTC", "assembled_text": "summary"},
                strategy_name="near_expiry_conviction",
                topic="BTC",
            )
            conn.commit()
            rec1 = proposal_record(conn, stored1["proposal_id"])
            exec1 = execute_record(conn, rec1, mode="mock")
            record_execution(conn, exec1)
            conn.commit()
            self.assertEqual(exec1["status"], "filled")

            # Second proposal: No (different outcome)
            p2 = sample_proposal()
            p2["outcome"] = "No"
            p2["confidence_score"] = 0.55
            stored2 = upsert_proposal(
                conn,
                p2,
                decision_engine="heuristic",
                status="authorized_for_execution",
                context_payload={"topic": "BTC", "assembled_text": "bearish"},
                strategy_name="near_expiry_conviction",
                topic="BTC",
            )
            conn.commit()
            rec2 = proposal_record(conn, stored2["proposal_id"])
            exec2 = execute_record(conn, rec2, mode="mock")

        self.assertNotEqual(exec2.get("error_message"), "duplicate_market_outcome_exposure")


class PreflightConditionalAllowanceTests(unittest.TestCase):
    """Regression: preflight must not raise on conditional_allowance=0 for
    BUY orders, but must raise for SELL (exit) orders. Production wallet has
    setApprovalForAll=False on both CTFExchange and NegRiskCTFExchange so the
    conditional allowance API returns 0 for every fresh outcome — before the
    fix, every first-touch BUY died at preflight with order_submit_failed."""

    def _mock_client_with_zero_conditional_allowance(self):
        from py_clob_client.clob_types import AssetType  # pyright: ignore[reportMissingImports]
        client = Mock()
        client.get_api_keys.return_value = [{"key": "x"}]
        def _balance_allowance(params):
            if params.asset_type == AssetType.COLLATERAL:
                return {"balance": "1000000000", "allowance": "1000000000"}
            return {"balance": "0", "allowance": 0.0}
        client.get_balance_allowance.side_effect = _balance_allowance
        client.get_address.return_value = "0xe65B947Ec589CFDB27292ac1da6eB58AfFE4BdE7"
        return client

    def test_buy_preflight_does_not_raise_on_zero_conditional_allowance(self):
        from polymarket_mvp.poly_executor import _real_preflight_check
        with patch.dict(os.environ, {
            "POLY_CLOB_FUNDER": "0xe65B947Ec589CFDB27292ac1da6eB58AfFE4BdE7",
            "SIGNATURE_TYPE": "0",
        }):
            client = self._mock_client_with_zero_conditional_allowance()
            preflight = _real_preflight_check(
                client,
                {"recommended_size_usdc": 5.0},
                token_id="123456789",
                is_sell=False,
            )
            self.assertEqual(preflight["conditional_allowance"]["allowance"], 0.0)
            self.assertGreater(preflight["collateral_balance_available"], 0)

    def test_sell_preflight_raises_on_zero_conditional_allowance(self):
        from polymarket_mvp.poly_executor import _real_preflight_check
        with patch.dict(os.environ, {
            "POLY_CLOB_FUNDER": "0xe65B947Ec589CFDB27292ac1da6eB58AfFE4BdE7",
            "SIGNATURE_TYPE": "0",
        }):
            client = self._mock_client_with_zero_conditional_allowance()
            with self.assertRaises(RuntimeError) as ctx:
                _real_preflight_check(
                    client,
                    {"recommended_size_usdc": 5.0},
                    token_id="123456789",
                    is_sell=True,
                )
            self.assertIn("Conditional token allowance is zero", str(ctx.exception))


class ClobSuccessFalseTests(unittest.TestCase):
    """Regression: HTTP 200 with {success: false} from post_order must NOT
    be recorded as status='submitted'. Before the fix, _normalize_order_status
    mapped the missing status field to "submitted", silently parking phantom
    orders that never reached the book."""

    def test_normalize_status_returns_submitted_for_none(self):
        from polymarket_mvp.poly_executor import _normalize_order_status
        self.assertEqual(_normalize_order_status(None), "submitted")

    def test_normalize_status_maps_unknown_to_failed(self):
        from polymarket_mvp.poly_executor import _normalize_order_status
        self.assertEqual(_normalize_order_status("INVALID"), "failed")
        self.assertEqual(_normalize_order_status("WHATEVER"), "failed")


if __name__ == "__main__":
    unittest.main()
