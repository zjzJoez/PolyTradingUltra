from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from polymarket_mvp.db import (
    connect_db,
    create_strategy_authorization,
    init_db,
    list_positions,
    market_snapshot,
    proposal_record,
    set_kill_switch,
    upsert_market_snapshot,
    upsert_proposal,
)
from polymarket_mvp.poly_executor import execute_record
from polymarket_mvp.risk_engine import evaluate_full_record
from polymarket_mvp.common import proposal_id_for


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


class TradingOSUpgradeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "test.sqlite3"
        os.environ["POLYMARKET_MVP_DB_PATH"] = str(self.db_path)
        os.environ["POLYMARKET_AVAILABLE_BALANCE_U"] = "1000"
        os.environ["POLY_RISK_REQUIRE_EXECUTABLE_MARKET"] = "false"
        os.environ["POLY_RISK_MAX_ORDER_USDC"] = "10"
        os.environ["POLY_RISK_MIN_CONFIDENCE"] = "0.5"
        os.environ["POLY_RISK_MAX_SLIPPAGE_BPS"] = "600"
        os.environ["POLY_RISK_MAX_TOPIC_EXPOSURE_USDC"] = "50"
        os.environ["POLY_RISK_MAX_CLUSTER_EXPOSURE_USDC"] = "50"
        os.environ["POLY_RISK_MAX_STRATEGY_DAILY_GROSS_USDC"] = "50"

    def tearDown(self) -> None:
        self.tmpdir.cleanup()
        os.environ.pop("POLYMARKET_MVP_DB_PATH", None)

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
                    "valid_from": "2026-03-21T00:00:00Z",
                    "valid_until": "2026-03-23T00:00:00Z",
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


if __name__ == "__main__":
    unittest.main()
