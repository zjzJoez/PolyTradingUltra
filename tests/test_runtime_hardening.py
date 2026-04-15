from __future__ import annotations

import unittest
from unittest.mock import patch

from polymarket_mvp.event_fetcher import fetch_and_persist_contexts


class RuntimeHardeningTests(unittest.TestCase):
    def test_fetch_and_persist_contexts_fetches_before_writing(self) -> None:
        markets = [
            {"market_id": "m1"},
            {"market_id": "m2"},
        ]
        events: list[str] = []

        def fake_fetch(market, **kwargs):
            events.append(f"fetch:{market['market_id']}")
            return {
                "market_id": market["market_id"],
                "topic": "topic",
                "symbol": None,
                "contexts": [{"source_type": "test", "raw_text": "x", "display_text": "x"}],
                "context_payload": {},
            }

        def fake_replace(conn, market_id, contexts):
            del conn, contexts
            events.append(f"write:{market_id}")

        with patch("polymarket_mvp.event_fetcher.fetch_contexts_for_market", side_effect=fake_fetch):
            with patch("polymarket_mvp.event_fetcher.replace_market_contexts", side_effect=fake_replace):
                results = fetch_and_persist_contexts(object(), markets, providers=["web_search"])

        self.assertEqual(
            events,
            ["fetch:m1", "fetch:m2", "write:m1", "write:m2"],
        )
        self.assertEqual([item["market_id"] for item in results], ["m1", "m2"])
