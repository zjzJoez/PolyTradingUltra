"""Regression tests for the 2026-05-14 resilience fixes:
  1. propose returns None (not raises) when both Codex + DeepSeek fail
  2. cryptopanic adapter is gated on CRYPTOPANIC_AUTH_TOKEN
  3. compute_order_ttl extends TTL for far-future pre-match sports markets
"""
from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from polymarket_mvp.common import compute_order_ttl, clamp_order_live_ttl
from polymarket_mvp.event_fetcher import provider_names


class GracefulLlmFallbackTests(unittest.TestCase):
    """When primary transport fails and DeepSeek fallback also fails (HTTP 402,
    429, timeout, etc.), chat_payload must return None instead of raising —
    so autopilot._tick doesn't propagate the Traceback all the way up."""

    def test_dual_failure_returns_none(self):
        from polymarket_mvp.services import openclaw_adapter
        from polymarket_mvp.db import init_db
        tmp = tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False)
        db_path = tmp.name
        tmp.close()
        try:
            os.unlink(db_path)
            init_db(Path(db_path))
            with patch.dict(os.environ, {
                "OPENCLAW_TRANSPORT": "codex_cli",
                "OPENCLAW_FALLBACK_PROVIDER": "deepseek",
                "POLYMARKET_MVP_DB_PATH": db_path,
            }), patch.object(openclaw_adapter, "_codex_payload",
                             side_effect=RuntimeError("Codex CLI failed: stdin error")), \
                 patch.object(openclaw_adapter, "_deepseek_payload",
                              side_effect=RuntimeError("DeepSeek HTTP 402: Insufficient Balance")):
                result = openclaw_adapter.chat_payload("sys", "user")
            self.assertIsNone(result)
        finally:
            if os.path.exists(db_path):
                os.unlink(db_path)

    def test_dual_failure_records_rate_limit_event(self):
        from polymarket_mvp.services import openclaw_adapter
        from polymarket_mvp.db import init_db, connect_db
        tmp = tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False)
        db_path = tmp.name
        tmp.close()
        try:
            os.unlink(db_path)
            init_db(Path(db_path))
            with patch.dict(os.environ, {
                "OPENCLAW_TRANSPORT": "codex_cli",
                "OPENCLAW_FALLBACK_PROVIDER": "deepseek",
                "POLYMARKET_MVP_DB_PATH": db_path,
            }), patch.object(openclaw_adapter, "_codex_payload",
                             side_effect=RuntimeError("Codex error")), \
                 patch.object(openclaw_adapter, "_deepseek_payload",
                              side_effect=RuntimeError("DeepSeek HTTP 402")):
                openclaw_adapter.chat_payload("sys", "user")
                with connect_db() as conn:
                    row = conn.execute(
                        "SELECT stderr_snippet, cooldown_applied_sec FROM llm_rate_limit_events ORDER BY id DESC LIMIT 1"
                    ).fetchone()
                self.assertIsNotNone(row)
                self.assertIn("DeepSeek HTTP 402", row[0])
                self.assertEqual(row[1], 60)
        finally:
            if os.path.exists(db_path):
                os.unlink(db_path)


class CryptopanicGateTests(unittest.TestCase):
    def test_cryptopanic_disabled_without_token(self):
        with patch.dict(os.environ, {
            "FOOTBALL_DATA_API_KEY": "",
            "PERPLEXITY_API_KEY": "",
            "TAVILY_API_KEY": "",
            "ODDS_API_KEY": "",
            "REDDIT_CLIENT_ID": "",
            "REDDIT_CLIENT_SECRET": "",
            "CRYPTOPANIC_AUTH_TOKEN": "",
        }, clear=False):
            names = provider_names(None)
        self.assertNotIn("cryptopanic", names)
        # web_search still always-on (no key needed)
        self.assertIn("web_search", names)

    def test_cryptopanic_enabled_with_token(self):
        with patch.dict(os.environ, {"CRYPTOPANIC_AUTH_TOKEN": "abc"}, clear=False):
            names = provider_names(None)
        self.assertIn("cryptopanic", names)


class ComputeOrderTtlTests(unittest.TestCase):
    """compute_order_ttl extends TTL up to (end_date - now - safety_buffer)
    for far-future matches, while still respecting the static ceiling for
    near-expiry markets and the system floor for already-expired ones."""

    def _now_iso(self):
        return "2026-05-14T08:00:00Z"

    def test_far_future_match_extends_ttl(self):
        # Match in 10 hours, no agent TTL → expect dynamic ceiling (6h default)
        market = {"end_date": "2026-05-14T18:00:00Z"}
        with patch.dict(os.environ, {
            "POLY_ORDER_MAX_LIVE_TTL_SECONDS": "3600",
            "POLY_ORDER_DYNAMIC_TTL_MAX_SECONDS": "21600",
            "POLY_ORDER_DYNAMIC_TTL_SAFETY_SEC": "600",
        }, clear=False):
            ttl = compute_order_ttl(market, agent_ttl=None, now_iso=self._now_iso())
        # 10h - 10min buffer = 35400s, but capped at dynamic ceiling 21600s
        self.assertEqual(ttl, 21600)

    def test_short_window_match_uses_remaining_time(self):
        # Match in 90 minutes; dynamic ceiling 6h. Should pick min(90m-buffer, 6h) = 80m.
        market = {"end_date": "2026-05-14T09:30:00Z"}
        with patch.dict(os.environ, {
            "POLY_ORDER_MAX_LIVE_TTL_SECONDS": "3600",
            "POLY_ORDER_DYNAMIC_TTL_MAX_SECONDS": "21600",
            "POLY_ORDER_DYNAMIC_TTL_SAFETY_SEC": "600",
        }, clear=False):
            ttl = compute_order_ttl(market, agent_ttl=None, now_iso=self._now_iso())
        # 90m - 10m buffer = 80m = 4800s
        self.assertEqual(ttl, 4800)

    def test_expired_market_returns_floor(self):
        market = {"end_date": "2026-05-14T07:00:00Z"}  # 1h ago
        ttl = compute_order_ttl(market, agent_ttl=None, now_iso=self._now_iso())
        self.assertEqual(ttl, 15)

    def test_missing_end_date_falls_back_to_static_clamp(self):
        with patch.dict(os.environ, {"POLY_ORDER_MAX_LIVE_TTL_SECONDS": "3600"}, clear=False):
            ttl = compute_order_ttl({}, agent_ttl=None, now_iso=self._now_iso())
        self.assertEqual(ttl, 3600)

    def test_agent_ttl_capped_by_dynamic_max(self):
        # Agent asks for 10h TTL, match is 2h away → cap at 2h - 10min = 110m
        market = {"end_date": "2026-05-14T10:00:00Z"}
        with patch.dict(os.environ, {
            "POLY_ORDER_DYNAMIC_TTL_MAX_SECONDS": "21600",
            "POLY_ORDER_DYNAMIC_TTL_SAFETY_SEC": "600",
        }, clear=False):
            ttl = compute_order_ttl(market, agent_ttl=36000, now_iso=self._now_iso())
        # 2h remaining - 10min buffer = 6600s, agent_ttl 36000 → capped
        self.assertEqual(ttl, 6600)

    def test_unparseable_end_date_falls_back_to_static_clamp(self):
        with patch.dict(os.environ, {"POLY_ORDER_MAX_LIVE_TTL_SECONDS": "3600"}, clear=False):
            ttl = compute_order_ttl({"end_date": "not-a-date"}, agent_ttl=None, now_iso=self._now_iso())
        self.assertEqual(ttl, 3600)


if __name__ == "__main__":
    unittest.main()
