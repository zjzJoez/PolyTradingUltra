"""Tests for the Alpha Lab signal importer."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from polymarket_mvp.common import utc_now_iso
from polymarket_mvp.db import connect_db, init_db, upsert_market_snapshot, upsert_proposal
from polymarket_mvp.alpha_signal_importer import (
    already_imported_signal_ids,
    import_signals,
    is_signal_expired,
    list_importable_signals,
    mark_signal_imported,
    signal_context_payload,
    signal_to_proposal,
)


def _make_market(market_id: str = "mkt-001") -> dict:
    return {
        "market_id": market_id,
        "question": "Will Team A beat Team B?",
        "slug": "team-a-vs-team-b",
        "market_url": "https://polymarket.com/event/team-a-vs-team-b",
        "condition_id": "cond-001",
        "active": 1,
        "closed": 0,
        "accepting_orders": 1,
        "end_date": "2026-06-01T00:00:00Z",
        "seconds_to_expiry": 86400,
        "days_to_expiry": 1.0,
        "liquidity_usdc": 5000.0,
        "volume_usdc": 20000.0,
        "volume_24h_usdc": 1000.0,
        "outcomes": [
            {"name": "Yes", "price": 0.55, "token_id": "tok-yes"},
            {"name": "No", "price": 0.45, "token_id": "tok-no"},
        ],
        "market_json": {
            "market_id": market_id,
            "question": "Will Team A beat Team B?",
            "outcomes": [
                {"name": "Yes", "price": 0.55, "token_id": "tok-yes"},
                {"name": "No", "price": 0.45, "token_id": "tok-no"},
            ],
        },
    }


def _make_signal(
    signal_id: str = "sig-001",
    market_id: str = "mkt-001",
    status: str = "ready_for_import",
    net_edge_bps: float = 300.0,
    expires_minutes_from_now: int = 30,
) -> dict:
    now = datetime.now(timezone.utc)
    expires_at = (now + timedelta(minutes=expires_minutes_from_now)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    return {
        "signal_id": signal_id,
        "market_id": market_id,
        "fixture_id": "fixture-001",
        "strategy_name": "soccer_prematch_value_v1",
        "market_family": "1x2",
        "outcome": "Yes",
        "fair_probability": 0.62,
        "market_probability": 0.55,
        "gross_edge_bps": 700.0,
        "net_edge_bps": net_edge_bps,
        "recommended_size_usdc": 5.0,
        "max_entry_price": 0.60,
        "signal_expires_at": expires_at,
        "model_version": "v1",
        "mapping_confidence": 0.95,
        "feature_freshness_seconds": 120,
        "confidence_score": 0.72,
        "expected_clv_bps": 150.0,
        "selection_rank": 1,
        "status": status,
        "quality_flags_json": "[]",
        "source_summary_json": json.dumps({"source_count": 4, "quote_dispersion": 0.02}),
        "explanation_json": json.dumps({"summary": "Strong edge from consensus anchor"}),
        "created_at": utc_now_iso(),
        "updated_at": utc_now_iso(),
    }


def _insert_alpha_signal(conn, signal: dict) -> None:
    """Insert an alpha_signal directly into the shared DB for testing."""
    cols = list(signal.keys())
    placeholders = ", ".join(["?"] * len(cols))
    col_names = ", ".join(cols)
    conn.execute(
        f"INSERT OR REPLACE INTO alpha_signals ({col_names}) VALUES ({placeholders})",
        [signal[c] for c in cols],
    )


def _create_alpha_signals_table(conn) -> None:
    """Create alpha_signals table for testing (mimics Alpha Lab schema)."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS alpha_signals (
          signal_id TEXT PRIMARY KEY,
          market_id TEXT NOT NULL,
          fixture_id TEXT,
          strategy_name TEXT NOT NULL,
          market_family TEXT NOT NULL,
          outcome TEXT NOT NULL,
          fair_probability REAL NOT NULL,
          market_probability REAL NOT NULL,
          gross_edge_bps REAL NOT NULL,
          net_edge_bps REAL NOT NULL,
          recommended_size_usdc REAL NOT NULL,
          max_entry_price REAL NOT NULL,
          signal_expires_at TEXT NOT NULL,
          model_version TEXT NOT NULL,
          mapping_confidence REAL NOT NULL,
          feature_freshness_seconds INTEGER NOT NULL,
          confidence_score REAL NOT NULL DEFAULT 0.0,
          expected_clv_bps REAL NOT NULL DEFAULT 0.0,
          selection_rank INTEGER NOT NULL DEFAULT 0,
          status TEXT NOT NULL CHECK(status IN ('draft', 'ready_for_import', 'imported', 'expired', 'cancelled')),
          quality_flags_json TEXT NOT NULL DEFAULT '[]',
          source_summary_json TEXT NOT NULL DEFAULT '{}',
          explanation_json TEXT NOT NULL,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
    """)


class TestSignalToProposal(unittest.TestCase):
    def test_converts_signal_fields(self):
        signal = _make_signal()
        proposal = signal_to_proposal(signal)
        self.assertEqual(proposal["market_id"], "mkt-001")
        self.assertEqual(proposal["outcome"], "Yes")
        self.assertAlmostEqual(proposal["confidence_score"], 0.72)
        self.assertEqual(proposal["recommended_size_usdc"], 5.0)
        self.assertIn("net_edge=300bps", proposal["reasoning"])
        self.assertIn("Alpha Lab signal", proposal["reasoning"])
        self.assertGreater(proposal["max_slippage_bps"], 0)

    def test_confidence_clamped(self):
        signal = _make_signal()
        signal["confidence_score"] = 1.5
        proposal = signal_to_proposal(signal)
        self.assertEqual(proposal["confidence_score"], 1.0)

        signal["confidence_score"] = -0.2
        proposal = signal_to_proposal(signal)
        self.assertEqual(proposal["confidence_score"], 0.0)

    def test_context_payload_structure(self):
        signal = _make_signal()
        ctx = signal_context_payload(signal)
        self.assertEqual(ctx["source"], "alpha_lab")
        self.assertEqual(ctx["signal_id"], "sig-001")
        self.assertEqual(ctx["strategy_name"], "soccer_prematch_value_v1")
        self.assertAlmostEqual(ctx["fair_probability"], 0.62)
        self.assertIsInstance(ctx["explanation"], dict)
        self.assertIsInstance(ctx["source_summary"], dict)
        self.assertIsInstance(ctx["quality_flags"], list)


class TestIsSignalExpired(unittest.TestCase):
    def test_not_expired(self):
        signal = _make_signal(expires_minutes_from_now=30)
        self.assertFalse(is_signal_expired(signal))

    def test_expired(self):
        signal = _make_signal(expires_minutes_from_now=-5)
        self.assertTrue(is_signal_expired(signal))

    def test_no_expiry(self):
        signal = _make_signal()
        signal["signal_expires_at"] = None
        self.assertFalse(is_signal_expired(signal))


class TestImportSignals(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False)
        self.db_path = Path(self.tmp.name)
        self.tmp.close()
        init_db(self.db_path)
        self.conn = connect_db(self.db_path)
        _create_alpha_signals_table(self.conn)
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self.db_path.unlink(missing_ok=True)

    def _insert_market(self, market_id: str = "mkt-001"):
        mkt = _make_market(market_id)
        upsert_market_snapshot(self.conn, mkt)
        self.conn.commit()

    def test_import_single_signal(self):
        self._insert_market()
        sig = _make_signal()
        _insert_alpha_signal(self.conn, sig)
        self.conn.commit()

        results = import_signals(self.conn)
        self.conn.commit()

        imported = [r for r in results if r["action"] == "imported"]
        self.assertEqual(len(imported), 1)
        self.assertIn("proposal_id", imported[0])

        # Verify signal marked as imported
        row = self.conn.execute(
            "SELECT status FROM alpha_signals WHERE signal_id=?", ("sig-001",)
        ).fetchone()
        self.assertEqual(row[0], "imported")

        # Verify proposal has alpha metadata
        prop = self.conn.execute(
            "SELECT * FROM proposals WHERE alpha_signal_id=?", ("sig-001",)
        ).fetchone()
        self.assertIsNotNone(prop)
        prop_dict = dict(prop)
        self.assertEqual(prop_dict["decision_engine"], "alpha_lab")
        self.assertAlmostEqual(prop_dict["alpha_fair_probability"], 0.62)
        self.assertAlmostEqual(prop_dict["alpha_net_edge_bps"], 300.0)
        self.assertEqual(prop_dict["alpha_model_version"], "v1")
        self.assertEqual(prop_dict["strategy_name"], "soccer_prematch_value_v1")

    def test_skip_duplicate_signal(self):
        self._insert_market()
        sig = _make_signal()
        _insert_alpha_signal(self.conn, sig)
        self.conn.commit()

        # Import once
        import_signals(self.conn)
        self.conn.commit()

        # Reset signal status to simulate re-run edge case
        self.conn.execute(
            "UPDATE alpha_signals SET status='ready_for_import' WHERE signal_id='sig-001'"
        )
        self.conn.commit()

        # Import again
        results = import_signals(self.conn)
        duplicates = [r for r in results if r["action"] == "skipped_duplicate"]
        self.assertEqual(len(duplicates), 1)

    def test_skip_expired_signal(self):
        self._insert_market()
        sig = _make_signal(expires_minutes_from_now=-10)
        _insert_alpha_signal(self.conn, sig)
        self.conn.commit()

        results = import_signals(self.conn)
        self.conn.commit()

        expired = [r for r in results if r["action"] == "skipped_expired"]
        self.assertEqual(len(expired), 1)

        # Verify signal marked as expired
        row = self.conn.execute(
            "SELECT status FROM alpha_signals WHERE signal_id=?", ("sig-001",)
        ).fetchone()
        self.assertEqual(row[0], "expired")

    def test_skip_missing_market(self):
        # Don't insert market
        sig = _make_signal(market_id="nonexistent-market")
        _insert_alpha_signal(self.conn, sig)
        self.conn.commit()

        results = import_signals(self.conn)
        skipped = [r for r in results if r["action"] == "skipped_no_market"]
        self.assertEqual(len(skipped), 1)

    def test_dry_run_no_writes(self):
        self._insert_market()
        sig = _make_signal()
        _insert_alpha_signal(self.conn, sig)
        self.conn.commit()

        results = import_signals(self.conn, dry_run=True)
        preview = [r for r in results if r["action"] == "would_import"]
        self.assertEqual(len(preview), 1)

        # Signal should still be ready_for_import
        row = self.conn.execute(
            "SELECT status FROM alpha_signals WHERE signal_id=?", ("sig-001",)
        ).fetchone()
        self.assertEqual(row[0], "ready_for_import")

        # No proposals created
        count = self.conn.execute("SELECT COUNT(*) FROM proposals").fetchone()[0]
        self.assertEqual(count, 0)

    def test_max_signals_limit(self):
        self._insert_market()
        for i in range(5):
            sig = _make_signal(signal_id=f"sig-{i:03d}")
            _insert_alpha_signal(self.conn, sig)
        self.conn.commit()

        results = import_signals(self.conn, max_signals=2)
        self.conn.commit()

        imported = [r for r in results if r["action"] == "imported"]
        capped = [r for r in results if r["action"] == "skipped_max_reached"]
        self.assertEqual(len(imported), 2)
        self.assertEqual(len(capped), 3)

    def test_multiple_signals_different_markets(self):
        self._insert_market("mkt-001")
        self._insert_market("mkt-002")

        sig1 = _make_signal(signal_id="sig-A", market_id="mkt-001")
        sig2 = _make_signal(signal_id="sig-B", market_id="mkt-002")
        _insert_alpha_signal(self.conn, sig1)
        _insert_alpha_signal(self.conn, sig2)
        self.conn.commit()

        results = import_signals(self.conn)
        self.conn.commit()

        imported = [r for r in results if r["action"] == "imported"]
        self.assertEqual(len(imported), 2)

        # Both proposals exist with correct alpha_signal_id
        for sid in ["sig-A", "sig-B"]:
            row = self.conn.execute(
                "SELECT alpha_signal_id FROM proposals WHERE alpha_signal_id=?", (sid,)
            ).fetchone()
            self.assertIsNotNone(row)


class TestListImportableSignals(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False)
        self.db_path = Path(self.tmp.name)
        self.tmp.close()
        init_db(self.db_path)
        self.conn = connect_db(self.db_path)
        _create_alpha_signals_table(self.conn)
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self.db_path.unlink(missing_ok=True)

    def test_only_ready_signals_returned(self):
        _insert_alpha_signal(self.conn, _make_signal("sig-ready", status="ready_for_import"))
        _insert_alpha_signal(self.conn, _make_signal("sig-draft", status="draft"))
        _insert_alpha_signal(self.conn, _make_signal("sig-imported", status="imported"))
        self.conn.commit()

        signals = list_importable_signals(self.conn)
        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0]["signal_id"], "sig-ready")

    def test_ordered_by_net_edge(self):
        _insert_alpha_signal(self.conn, _make_signal("sig-low", net_edge_bps=100))
        _insert_alpha_signal(self.conn, _make_signal("sig-high", net_edge_bps=500))
        _insert_alpha_signal(self.conn, _make_signal("sig-mid", net_edge_bps=300))
        self.conn.commit()

        signals = list_importable_signals(self.conn)
        edges = [s["net_edge_bps"] for s in signals]
        self.assertEqual(edges, [500.0, 300.0, 100.0])


if __name__ == "__main__":
    unittest.main()
