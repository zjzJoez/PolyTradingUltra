from __future__ import annotations

import time
import unittest
from unittest.mock import MagicMock, patch

from polymarket_mvp.services import openclaw_adapter
from polymarket_mvp.services.openclaw_adapter import (
    LLMRateLimitError,
    _looks_like_rate_limit,
    llm_cooldown_remaining_sec,
    reset_llm_cooldown_state,
)


class MarkerMatchingTests(unittest.TestCase):
    def test_matches_known_markers(self):
        self.assertTrue(_looks_like_rate_limit("Claude usage limit reached"))
        self.assertTrue(_looks_like_rate_limit("error: 429 Too Many Requests"))
        self.assertTrue(_looks_like_rate_limit("5-hour limit exceeded"))
        self.assertTrue(_looks_like_rate_limit("weekly_limit exceeded, try again later"))

    def test_ignores_unrelated_errors(self):
        self.assertFalse(_looks_like_rate_limit("Network timeout"))
        self.assertFalse(_looks_like_rate_limit("Invalid API key"))
        self.assertFalse(_looks_like_rate_limit(""))


class CooldownStateMachineTests(unittest.TestCase):
    def setUp(self):
        reset_llm_cooldown_state()

    def tearDown(self):
        reset_llm_cooldown_state()

    def _mock_run(self, stderr: str, returncode: int = 1):
        mock_result = MagicMock(returncode=returncode, stderr=stderr, stdout="")
        return patch("polymarket_mvp.services.openclaw_adapter.subprocess.run", return_value=mock_result)

    def _mock_cli(self):
        return patch(
            "polymarket_mvp.services.openclaw_adapter._claude_cli_path",
            return_value="/usr/bin/claude",
        )

    def test_first_hit_triggers_30min_cooldown_and_raises(self):
        with self._mock_cli(), self._mock_run("Error: Claude usage limit reached"):
            with self.assertRaises(LLMRateLimitError) as ctx:
                openclaw_adapter._claude_payload("sys", "user")
        self.assertEqual(ctx.exception.consecutive_count, 1)
        self.assertEqual(ctx.exception.cooldown_sec, 30 * 60)
        self.assertGreater(llm_cooldown_remaining_sec(), 29 * 60)

    def test_escalation_on_second_hit_within_window(self):
        # First hit → 30 min
        with self._mock_cli(), self._mock_run("usage limit reached"):
            with self.assertRaises(LLMRateLimitError):
                openclaw_adapter._claude_payload("sys", "user")
        # Artificially expire the first cooldown so the subprocess is called again,
        # but keep last_hit_at recent (within 6h reset window).
        openclaw_adapter._LLM_COOLDOWN_STATE["cooldown_until"] = 0.0

        with self._mock_cli(), self._mock_run("rate limit exceeded"):
            with self.assertRaises(LLMRateLimitError) as ctx:
                openclaw_adapter._claude_payload("sys", "user")
        self.assertEqual(ctx.exception.consecutive_count, 2)
        self.assertEqual(ctx.exception.cooldown_sec, 60 * 60)

    def test_non_ratelimit_error_does_not_trigger_cooldown(self):
        with self._mock_cli(), self._mock_run("Fatal: invalid model name"):
            with self.assertRaises(RuntimeError) as ctx:
                openclaw_adapter._claude_payload("sys", "user")
        self.assertNotIsInstance(ctx.exception, LLMRateLimitError)
        self.assertEqual(llm_cooldown_remaining_sec(), 0.0)

    def test_cooldown_skips_subprocess_entirely(self):
        # Arm cooldown directly, then confirm _claude_payload raises without running subprocess.
        openclaw_adapter._LLM_COOLDOWN_STATE["cooldown_until"] = time.time() + 1800
        openclaw_adapter._LLM_COOLDOWN_STATE["consecutive_count"] = 1
        openclaw_adapter._LLM_COOLDOWN_STATE["last_hit_at"] = time.time()

        run_mock = MagicMock()
        with patch("polymarket_mvp.services.openclaw_adapter._claude_cli_path",
                   return_value="/usr/bin/claude"), \
             patch("polymarket_mvp.services.openclaw_adapter.subprocess.run", run_mock):
            with self.assertRaises(LLMRateLimitError):
                openclaw_adapter._claude_payload("sys", "user")
        run_mock.assert_not_called()

    def test_counter_resets_after_reset_window(self):
        openclaw_adapter._LLM_COOLDOWN_STATE["cooldown_until"] = 0.0
        openclaw_adapter._LLM_COOLDOWN_STATE["consecutive_count"] = 3
        # Last hit long ago → next hit should reset counter to 1.
        openclaw_adapter._LLM_COOLDOWN_STATE["last_hit_at"] = time.time() - (7 * 60 * 60)

        with self._mock_cli(), self._mock_run("usage limit reached"):
            with self.assertRaises(LLMRateLimitError) as ctx:
                openclaw_adapter._claude_payload("sys", "user")
        self.assertEqual(ctx.exception.consecutive_count, 1)
        self.assertEqual(ctx.exception.cooldown_sec, 30 * 60)


if __name__ == "__main__":
    unittest.main()
