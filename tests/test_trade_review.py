from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from polymarket_mvp.db import (
    connect_db,
    init_db,
    record_agent_review,
    record_approval,
    record_execution,
    record_heartbeat,
    record_order_reconciliation,
    record_position,
    record_position_event,
    record_shadow_execution,
    upsert_market_resolution,
    upsert_market_snapshot,
    upsert_proposal,
)
from polymarket_mvp.trade_review import ISSUE_TAXONOMY, classify_market, generate_trade_review, main


def winner_market() -> dict:
    return {
        "market_id": "m-win",
        "question": "Will Shandong Taishan FC win on 2026-04-04?",
        "slug": "shandong-win-2026-04-04",
        "market_url": "https://polymarket.com/event/shandong-win-2026-04-04",
        "condition_id": "cond-win",
        "active": False,
        "closed": True,
        "accepting_orders": False,
        "end_date": "2026-04-04T14:00:00Z",
        "seconds_to_expiry": 600,
        "days_to_expiry": 0.01,
        "liquidity_usdc": 25000,
        "volume_usdc": 12000,
        "volume_24h_usdc": 8000,
        "outcomes": [
            {"name": "Yes", "price": 0.54, "token_id": "tok-win-yes"},
            {"name": "No", "price": 0.46, "token_id": "tok-win-no"},
        ],
    }


def totals_market() -> dict:
    return {
        "market_id": "m-total",
        "question": "Games Total: O/U 2.5",
        "slug": "games-total-ou-2-5",
        "market_url": "https://polymarket.com/event/games-total-ou-2-5",
        "condition_id": "cond-total",
        "active": False,
        "closed": True,
        "accepting_orders": False,
        "end_date": "2026-04-14T10:30:00Z",
        "seconds_to_expiry": 900,
        "days_to_expiry": 0.01,
        "liquidity_usdc": 30000,
        "volume_usdc": 18000,
        "volume_24h_usdc": 6000,
        "outcomes": [
            {"name": "Over", "price": 0.48, "token_id": "tok-total-over"},
            {"name": "Under", "price": 0.52, "token_id": "tok-total-under"},
        ],
        "sportsMarketType": "totals",
    }


def crypto_market() -> dict:
    return {
        "market_id": "m-crypto",
        "question": "BNB Up or Down - April 14, 10:15-10:30 UTC",
        "slug": "bnb-up-or-down-2026-04-14-1015",
        "market_url": "https://polymarket.com/event/bnb-up-or-down-2026-04-14-1015",
        "condition_id": "cond-crypto",
        "active": False,
        "closed": True,
        "accepting_orders": False,
        "end_date": "2026-04-14T10:30:00Z",
        "seconds_to_expiry": 900,
        "days_to_expiry": 0.01,
        "liquidity_usdc": 45000,
        "volume_usdc": 22000,
        "volume_24h_usdc": 9000,
        "outcomes": [
            {"name": "Up", "price": 0.49, "token_id": "tok-crypto-up"},
            {"name": "Down", "price": 0.51, "token_id": "tok-crypto-down"},
        ],
        "resolutionSource": "data.chain.link/streams",
    }


def proposal_payload(market_id: str, outcome: str, confidence: float, size_usdc: float, reasoning: str) -> dict:
    return {
        "market_id": market_id,
        "outcome": outcome,
        "confidence_score": confidence,
        "recommended_size_usdc": size_usdc,
        "reasoning": reasoning,
        "max_slippage_bps": 500,
    }


def execution_payload(proposal_id: str, *, status: str, requested_size_usdc: float, requested_price: float, created_at: str, error_message: str | None = None) -> dict:
    return {
        "proposal_id": proposal_id,
        "mode": "real",
        "client_order_id": f"client-{proposal_id}-{status}",
        "order_intent_json": {"proposal_id": proposal_id},
        "requested_price": requested_price,
        "requested_size_usdc": requested_size_usdc,
        "max_slippage_bps": 500,
        "observed_worst_price": requested_price,
        "slippage_check_status": "passed",
        "status": status,
        "filled_size_usdc": requested_size_usdc if status == "filled" else 0.0,
        "avg_fill_price": requested_price if status == "filled" else None,
        "txhash_or_order_id": f"order-{proposal_id}" if status == "filled" else None,
        "slippage_bps": 0.0 if status == "filled" else None,
        "error_message": error_message,
        "created_at": created_at,
        "updated_at": created_at,
    }


class TradeReviewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "review.sqlite3"
        self.output_dir = Path(self.tmpdir.name) / "review-output"
        os.environ["POLYMARKET_MVP_DB_PATH"] = str(self.db_path)
        os.environ["POLYMARKET_MVP_STATE_DIR"] = self.tmpdir.name

    def tearDown(self) -> None:
        self.tmpdir.cleanup()
        os.environ.pop("POLYMARKET_MVP_DB_PATH", None)
        os.environ.pop("POLYMARKET_MVP_STATE_DIR", None)

    def _seed_review_history(self) -> None:
        init_db(self.db_path)
        with connect_db(self.db_path) as conn:
            upsert_market_snapshot(conn, winner_market())
            upsert_market_snapshot(conn, totals_market())
            upsert_market_snapshot(conn, crypto_market())

            duplicate_one = upsert_proposal(
                conn,
                proposal_payload("m-win", "Yes", 0.64, 10.0, "Team form looks strong."),
                decision_engine="openclaw_llm",
                status="proposed",
                context_payload={"topic": "football"},
                strategy_name="near_expiry_conviction",
                topic="football",
            )
            duplicate_two = upsert_proposal(
                conn,
                proposal_payload("m-win", "Yes", 0.61, 10.0, "Momentum still points the same way."),
                decision_engine="openclaw_llm",
                status="proposed",
                context_payload={"topic": "football"},
                strategy_name="near_expiry_conviction",
                topic="football",
            )
            split_trade = upsert_proposal(
                conn,
                proposal_payload("m-total", "Over", 0.57, 10.0, "Totals market mispriced near close."),
                decision_engine="openclaw_llm",
                status="pending_approval",
                context_payload={"topic": "esports"},
                strategy_name="totals_overlay",
                topic="esports",
                authorization_status="matched_manual_only",
            )
            risk_blocked = upsert_proposal(
                conn,
                proposal_payload("m-total", "Under", 0.43, 5.0, "Weak edge, should not pass risk."),
                decision_engine="openclaw_llm",
                status="risk_blocked",
                context_payload={"topic": "esports"},
                strategy_name="near_expiry_conviction",
                topic="esports",
            )
            failed_trade = upsert_proposal(
                conn,
                proposal_payload("m-crypto", "Up", 0.55, 5.0, "Short-horizon momentum attempt."),
                decision_engine="openclaw_llm",
                status="authorized_for_execution",
                context_payload={"topic": "crypto"},
                strategy_name="near_expiry_conviction",
                topic="crypto",
                authorization_status="matched_auto_execute",
            )

            conn.execute("UPDATE proposals SET created_at = ?, updated_at = ? WHERE proposal_id = ?", ("2026-04-04T09:00:00Z", "2026-04-04T09:00:00Z", duplicate_one["proposal_id"]))
            conn.execute("UPDATE proposals SET created_at = ?, updated_at = ? WHERE proposal_id = ?", ("2026-04-04T09:15:00Z", "2026-04-04T09:15:00Z", duplicate_two["proposal_id"]))
            conn.execute("UPDATE proposals SET created_at = ?, updated_at = ? WHERE proposal_id = ?", ("2026-04-14T10:10:00Z", "2026-04-14T10:10:00Z", split_trade["proposal_id"]))
            conn.execute("UPDATE proposals SET created_at = ?, updated_at = ? WHERE proposal_id = ?", ("2026-04-14T10:11:00Z", "2026-04-14T10:11:00Z", risk_blocked["proposal_id"]))
            conn.execute("UPDATE proposals SET created_at = ?, updated_at = ? WHERE proposal_id = ?", ("2026-04-14T10:12:00Z", "2026-04-14T10:12:00Z", failed_trade["proposal_id"]))

            record_approval(
                conn,
                proposal_id=split_trade["proposal_id"],
                decision="approved",
                decided_at="2026-04-14T10:11:00Z",
                telegram_user_id="123",
                telegram_username="tester",
                callback_query_id="cb-1",
                telegram_message_id="msg-1",
                raw_callback_json={"decision": "approved"},
            )

            execution_one = record_execution(
                conn,
                execution_payload(
                    duplicate_one["proposal_id"],
                    status="filled",
                    requested_size_usdc=10.0,
                    requested_price=0.50,
                    created_at="2026-04-04T09:30:00Z",
                ),
            )
            execution_two = record_execution(
                conn,
                execution_payload(
                    duplicate_two["proposal_id"],
                    status="filled",
                    requested_size_usdc=10.0,
                    requested_price=0.50,
                    created_at="2026-04-04T09:35:00Z",
                ),
            )
            execution_three = record_execution(
                conn,
                execution_payload(
                    split_trade["proposal_id"],
                    status="filled",
                    requested_size_usdc=10.0,
                    requested_price=0.30,
                    created_at="2026-04-14T10:12:00Z",
                ),
            )
            record_execution(
                conn,
                execution_payload(
                    failed_trade["proposal_id"],
                    status="failed",
                    requested_size_usdc=5.0,
                    requested_price=0.49,
                    created_at="2026-04-14T10:13:00Z",
                    error_message="real_preflight_failed: PolyApiException Request exception!",
                ),
            )

            position_one = record_position(
                conn,
                {
                    "proposal_id": duplicate_one["proposal_id"],
                    "execution_id": execution_one["id"],
                    "market_id": "m-win",
                    "event_cluster_id": None,
                    "outcome": "Yes",
                    "entry_price": 0.50,
                    "size_usdc": 10.0,
                    "filled_qty": 20.0,
                    "status": "resolved",
                    "entry_time": "2026-04-04T09:30:00Z",
                    "last_mark_price": 0.0,
                    "unrealized_pnl": 0.0,
                    "realized_pnl": -10.0,
                    "strategy_name": "near_expiry_conviction",
                    "is_shadow": False,
                    "mode": "real",
                    "created_at": "2026-04-04T09:30:00Z",
                    "updated_at": "2026-04-04T14:30:00Z",
                },
            )
            position_two = record_position(
                conn,
                {
                    "proposal_id": duplicate_two["proposal_id"],
                    "execution_id": execution_two["id"],
                    "market_id": "m-win",
                    "event_cluster_id": None,
                    "outcome": "Yes",
                    "entry_price": 0.50,
                    "size_usdc": 10.0,
                    "filled_qty": 20.0,
                    "status": "resolved",
                    "entry_time": "2026-04-04T09:35:00Z",
                    "last_mark_price": 0.0,
                    "unrealized_pnl": 0.0,
                    "realized_pnl": -10.0,
                    "strategy_name": "near_expiry_conviction",
                    "is_shadow": False,
                    "mode": "real",
                    "created_at": "2026-04-04T09:35:00Z",
                    "updated_at": "2026-04-04T14:30:00Z",
                },
            )
            position_three = record_position(
                conn,
                {
                    "proposal_id": split_trade["proposal_id"],
                    "execution_id": execution_three["id"],
                    "market_id": "m-total",
                    "event_cluster_id": None,
                    "outcome": "Over",
                    "entry_price": 0.30,
                    "size_usdc": 10.0,
                    "filled_qty": 10.0,
                    "status": "resolved",
                    "entry_time": "2026-04-14T10:12:00Z",
                    "last_mark_price": 0.50,
                    "unrealized_pnl": 0.0,
                    "realized_pnl": 2.0,
                    "strategy_name": "totals_overlay",
                    "is_shadow": False,
                    "mode": "real",
                    "created_at": "2026-04-14T10:12:00Z",
                    "updated_at": "2026-04-14T10:40:00Z",
                },
            )

            record_position_event(conn, {"position_id": position_one["id"], "event_type": "resolve", "payload_json": {"realized_pnl": -10.0}, "created_at": "2026-04-04T14:30:00Z"})
            record_position_event(conn, {"position_id": position_three["id"], "event_type": "resolve", "payload_json": {"realized_pnl": 2.0}, "created_at": "2026-04-14T10:40:00Z"})
            record_order_reconciliation(
                conn,
                {
                    "execution_id": execution_three["id"],
                    "external_order_id": "order-split",
                    "observed_status": "filled",
                    "observed_fill_qty": 10.0,
                    "observed_fill_price": 0.30,
                    "reconciliation_result": "matched",
                    "payload_json": {"source": "test"},
                    "created_at": "2026-04-14T10:13:30Z",
                },
            )
            record_agent_review(
                conn,
                {
                    "position_id": position_one["id"],
                    "proposal_id": duplicate_one["proposal_id"],
                    "event_cluster_id": None,
                    "review_type": "post_trade",
                    "summary": "Duplicated thesis amplified downside.",
                    "what_worked": ["execution path filled"],
                    "what_failed": ["position sizing discipline"],
                    "failure_bucket": "risk",
                    "next_action": "block_duplicate_exposure",
                    "payload_json": {"source": "test"},
                    "created_at": "2026-04-04T15:00:00Z",
                },
            )
            record_shadow_execution(
                conn,
                {
                    "proposal_id": split_trade["proposal_id"],
                    "simulated_fill_price": 0.31,
                    "simulated_size": 10.0,
                    "simulated_notional": 10.0,
                    "simulated_status": "filled",
                    "context_json": {"mode": "shadow"},
                    "created_at": "2026-04-14T10:12:30Z",
                },
            )
            record_heartbeat(
                conn,
                "scan",
                "2026-04-14T10:00:00Z",
                "2026-04-14T10:00:04Z",
                500,
                None,
                {"source": "test"},
            )
            record_heartbeat(
                conn,
                "context",
                "2026-04-14T10:01:00Z",
                "2026-04-14T10:01:03Z",
                25,
                "sqlite3.OperationalError: database is locked",
                {"source": "test"},
            )
            upsert_market_resolution(conn, "m-win", "No", {"closedTime": "2026-04-04T14:30:00Z", "outcomes": ["Yes", "No"], "outcomePrices": ["0", "1"]})
            upsert_market_resolution(conn, "m-total", "50-50", {"closedTime": "2026-04-14T10:40:00Z", "outcomes": ["Over", "Under"], "outcomePrices": ["0.5", "0.5"]})
            conn.execute("UPDATE market_resolutions SET resolved_at = ? WHERE market_id = ?", ("2026-04-04T14:30:00Z", "m-win"))
            conn.execute("UPDATE market_resolutions SET resolved_at = ? WHERE market_id = ?", ("2026-04-14T10:40:00Z", "m-total"))
            conn.commit()

    def test_generate_trade_review_outputs_full_package(self) -> None:
        self._seed_review_history()
        result = generate_trade_review(db_path=self.db_path, output_dir=self.output_dir)

        self.assertEqual(result["row_counts"]["proposals"], 5)
        self.assertEqual(result["row_counts"]["executions"], 4)
        self.assertEqual(result["row_counts"]["positions"], 3)

        metrics = json.loads((self.output_dir / "metrics.json").read_text(encoding="utf-8"))
        self.assertEqual(metrics["headline"]["total_realized_pnl"], -18.0)
        self.assertEqual(metrics["headline"]["resolved_positions"], 3)
        self.assertEqual(metrics["row_counts"]["agent_reviews"], 1)

        incident_types = {row["issue_type"] for row in metrics["incidents"]}
        self.assertTrue({"duplicate_exposure", "split_resolution", "execution_failure", "ops_lock"}.issubset(incident_types))

        proposal_funnel = (self.output_dir / "facts" / "proposal_funnel.csv").read_text(encoding="utf-8")
        self.assertIn("near_expiry_conviction", proposal_funnel)
        self.assertIn("risk_blocked", proposal_funnel)

        position_facts = (self.output_dir / "facts" / "position_facts.csv").read_text(encoding="utf-8")
        self.assertIn("is_duplicate_entry", position_facts)
        self.assertIn("m-total", position_facts)

        summary = (self.output_dir / "summary.md").read_text(encoding="utf-8")
        self.assertIn("# Polymarket Trading 全量复盘", summary)
        self.assertIn("最大亏损组", summary)
        self.assertIn("Known Unknowns", summary)
        self.assertTrue((self.output_dir / "plots" / "daily_realized_pnl.svg").exists())
        self.assertTrue((self.output_dir / "plots" / "strategy_realized_pnl.svg").exists())
        self.assertTrue((self.output_dir / "review_snapshot.sqlite3").exists())

    def test_cli_main_generates_outputs(self) -> None:
        self._seed_review_history()
        exit_code = main(["--db", str(self.db_path), "--output-dir", str(self.output_dir)])
        self.assertEqual(exit_code, 0)
        self.assertTrue((self.output_dir / "summary.md").exists())

    def test_classify_market_and_taxonomy_are_stable(self) -> None:
        self.assertEqual(classify_market(winner_market()), "sports_winner")
        self.assertEqual(classify_market(totals_market()), "sports_totals")
        self.assertEqual(classify_market(crypto_market()), "crypto_up_down")
        self.assertIn("duplicate_exposure", ISSUE_TAXONOMY)
        self.assertIn("ops_lock", ISSUE_TAXONOMY)

    def test_generate_trade_review_handles_missing_optional_tables(self) -> None:
        legacy_db = Path(self.tmpdir.name) / "legacy.sqlite3"
        with sqlite3.connect(legacy_db) as conn:
            conn.executescript(
                """
                CREATE TABLE market_snapshots (
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
                CREATE TABLE proposals (
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
            )
            conn.execute(
                """
                INSERT INTO market_snapshots (
                  market_id, question, slug, market_url, condition_id, active, closed, accepting_orders,
                  end_date, seconds_to_expiry, days_to_expiry, liquidity_usdc, volume_usdc, volume_24h_usdc,
                  outcomes_json, market_json, last_scanned_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "legacy-market",
                    "Will Legacy FC win?",
                    "legacy-fc-win",
                    "https://example.com/legacy",
                    "legacy-cond",
                    1,
                    0,
                    1,
                    "2026-04-02T12:00:00Z",
                    600,
                    0.01,
                    1000,
                    500,
                    100,
                    json.dumps([{"name": "Yes", "price": 0.55}, {"name": "No", "price": 0.45}]),
                    json.dumps({"market_id": "legacy-market", "question": "Will Legacy FC win?"}),
                    "2026-04-02T11:00:00Z",
                ),
            )
            conn.execute(
                """
                INSERT INTO proposals (
                  proposal_id, market_id, outcome, confidence_score, recommended_size_usdc, reasoning,
                  decision_engine, status, max_slippage_bps, proposal_json, context_payload_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "legacy-proposal",
                    "legacy-market",
                    "Yes",
                    0.55,
                    5.0,
                    "Legacy record",
                    "openclaw_llm",
                    "proposed",
                    500,
                    json.dumps({"market_id": "legacy-market", "outcome": "Yes"}),
                    json.dumps({"topic": "legacy"}),
                    "2026-04-02T11:00:00Z",
                    "2026-04-02T11:00:00Z",
                ),
            )
            conn.commit()

        legacy_output = Path(self.tmpdir.name) / "legacy-output"
        result = generate_trade_review(db_path=legacy_db, output_dir=legacy_output)
        self.assertEqual(result["row_counts"]["proposals"], 1)
        self.assertEqual(result["row_counts"]["executions"], 0)
        self.assertTrue((legacy_output / "summary.md").exists())

