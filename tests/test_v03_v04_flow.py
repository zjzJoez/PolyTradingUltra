from __future__ import annotations

import os
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from polymarket_mvp.common import parse_iso8601, proposal_id_for, utc_now_iso
from polymarket_mvp.db import (
    connect_db,
    create_strategy_authorization,
    init_db,
    list_positions,
    market_snapshot,
    proposal_record,
    record_execution,
    set_kill_switch,
    upsert_market_snapshot,
    upsert_proposal,
    upsert_market_resolution,
)
from polymarket_mvp.poly_executor import execute_record
from polymarket_mvp.proposer import build_openclaw_proposals
from polymarket_mvp.risk_engine import evaluate_full_record
from polymarket_mvp.agents.supervisor_agent import supervise_record
from polymarket_mvp.services.position_manager import sync_all_positions, update_position_marks
from polymarket_mvp.services.openclaw_adapter import chat_json


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

    def test_build_openclaw_proposals_generates_from_adapter(self) -> None:
        markets = [sample_market()]
        context_file = {
            "markets": [
                {
                    "market_id": "m1",
                    "context_payload": {
                        "topic": "BTC",
                        "assembled_text": "ETF chatter and near-term momentum.",
                        "sources": [{"source_type": "news", "display_text": "ETF chatter"}],
                    },
                }
            ]
        }
        with patch("polymarket_mvp.proposer.maybe_generate_trade_proposals") as mocked:
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
            proposals = build_openclaw_proposals(
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
        markets = [sample_updown_market()]
        with patch("polymarket_mvp.proposer.maybe_generate_trade_proposals") as mocked:
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


if __name__ == "__main__":
    unittest.main()
