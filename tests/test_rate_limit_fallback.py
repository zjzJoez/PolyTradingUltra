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
        self.assertGreater(llm_cooldown_remaining_sec("claude"), 29 * 60)

    def test_escalation_on_second_hit_within_window(self):
        # First hit → 30 min
        with self._mock_cli(), self._mock_run("usage limit reached"):
            with self.assertRaises(LLMRateLimitError):
                openclaw_adapter._claude_payload("sys", "user")
        # Artificially expire the first cooldown so the subprocess is called again,
        # but keep last_hit_at recent (within 6h reset window).
        openclaw_adapter._cooldown_state("claude")["cooldown_until"] = 0.0

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
        self.assertEqual(llm_cooldown_remaining_sec("claude"), 0.0)

    def test_cooldown_skips_subprocess_entirely(self):
        # Arm cooldown directly, then confirm _claude_payload raises without running subprocess.
        openclaw_adapter._cooldown_state("claude")["cooldown_until"] = time.time() + 1800
        openclaw_adapter._cooldown_state("claude")["consecutive_count"] = 1
        openclaw_adapter._cooldown_state("claude")["last_hit_at"] = time.time()

        run_mock = MagicMock()
        with patch("polymarket_mvp.services.openclaw_adapter._claude_cli_path",
                   return_value="/usr/bin/claude"), \
             patch("polymarket_mvp.services.openclaw_adapter.subprocess.run", run_mock):
            with self.assertRaises(LLMRateLimitError):
                openclaw_adapter._claude_payload("sys", "user")
        run_mock.assert_not_called()

    def test_counter_resets_after_reset_window(self):
        openclaw_adapter._cooldown_state("claude")["cooldown_until"] = 0.0
        openclaw_adapter._cooldown_state("claude")["consecutive_count"] = 3
        # Last hit long ago → next hit should reset counter to 1.
        openclaw_adapter._cooldown_state("claude")["last_hit_at"] = time.time() - (7 * 60 * 60)

        with self._mock_cli(), self._mock_run("usage limit reached"):
            with self.assertRaises(LLMRateLimitError) as ctx:
                openclaw_adapter._claude_payload("sys", "user")
        self.assertEqual(ctx.exception.consecutive_count, 1)
        self.assertEqual(ctx.exception.cooldown_sec, 30 * 60)


class DeepSeekFallbackTests(unittest.TestCase):
    """When OPENCLAW_FALLBACK_PROVIDER=deepseek is set and the primary
    transport (Codex / Claude CLI) raises a recoverable error, chat_payload
    should call _deepseek_payload instead of propagating the error."""

    def setUp(self):
        reset_llm_cooldown_state()
        self._env_patches = [
            patch.dict("os.environ", {
                "OPENCLAW_FALLBACK_PROVIDER": "deepseek",
                "DEEPSEEK_API_KEY": "sk-test-deepseek",
                "OPENCLAW_TRANSPORT": "codex_cli",
            }, clear=False),
        ]
        for p in self._env_patches:
            p.start()

    def tearDown(self):
        for p in self._env_patches:
            p.stop()
        reset_llm_cooldown_state()

    def test_codex_runtime_error_falls_back_to_deepseek(self):
        """Codex CLI failure (e.g. quota exhausted: 'Codex CLI failed: ...
        thread not found') with fallback enabled → DeepSeek runs."""
        deepseek_response = {
            "choices": [{"message": {"content": '{"decision": "skip", "reason": "fallback OK"}'}}],
        }
        with patch("polymarket_mvp.services.openclaw_adapter._codex_payload") as mock_codex, \
             patch("polymarket_mvp.services.openclaw_adapter._deepseek_payload") as mock_deepseek:
            mock_codex.side_effect = RuntimeError("Codex CLI failed: thread not found")
            mock_deepseek.return_value = deepseek_response
            result = openclaw_adapter.chat_payload("sys", "user")
        self.assertEqual(result, deepseek_response)
        mock_codex.assert_called_once()
        mock_deepseek.assert_called_once()

    def test_codex_rate_limit_falls_back_to_deepseek(self):
        with patch("polymarket_mvp.services.openclaw_adapter._codex_payload") as mock_codex, \
             patch("polymarket_mvp.services.openclaw_adapter._deepseek_payload") as mock_deepseek:
            mock_codex.side_effect = LLMRateLimitError("rate limit", 1800, 1, transport="codex")
            mock_deepseek.return_value = {"choices": [{"message": {"content": "{}"}}]}
            openclaw_adapter.chat_payload("sys", "user")
        mock_deepseek.assert_called_once()

    def test_codex_success_skips_fallback(self):
        with patch("polymarket_mvp.services.openclaw_adapter._codex_payload") as mock_codex, \
             patch("polymarket_mvp.services.openclaw_adapter._deepseek_payload") as mock_deepseek:
            mock_codex.return_value = {"choices": [{"message": {"content": "{}"}}]}
            openclaw_adapter.chat_payload("sys", "user")
        mock_deepseek.assert_not_called()

    def test_fallback_failure_raises_primary_error(self):
        """If both primary and fallback fail, the primary error is raised
        (the fallback failure goes to stderr but doesn't shadow the
        original cause)."""
        primary = RuntimeError("Codex CLI failed: original error")
        with patch("polymarket_mvp.services.openclaw_adapter._codex_payload") as mock_codex, \
             patch("polymarket_mvp.services.openclaw_adapter._deepseek_payload") as mock_deepseek:
            mock_codex.side_effect = primary
            mock_deepseek.side_effect = RuntimeError("DeepSeek HTTP 401: bad key")
            with self.assertRaises(RuntimeError) as ctx:
                openclaw_adapter.chat_payload("sys", "user")
        self.assertIs(ctx.exception, primary)

    def test_fallback_disabled_by_default(self):
        """No OPENCLAW_FALLBACK_PROVIDER → no fallback call, primary error propagates."""
        with patch.dict("os.environ", {"OPENCLAW_FALLBACK_PROVIDER": ""}, clear=False):
            with patch("polymarket_mvp.services.openclaw_adapter._codex_payload") as mock_codex, \
                 patch("polymarket_mvp.services.openclaw_adapter._deepseek_payload") as mock_deepseek:
                mock_codex.side_effect = RuntimeError("Codex CLI failed")
                with self.assertRaises(RuntimeError):
                    openclaw_adapter.chat_payload("sys", "user")
                mock_deepseek.assert_not_called()


class DeepSeekPayloadTests(unittest.TestCase):
    """Direct tests for _deepseek_payload error / rate-limit handling."""

    def setUp(self):
        reset_llm_cooldown_state()
        self._patch = patch.dict("os.environ", {"DEEPSEEK_API_KEY": "sk-test"}, clear=False)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        reset_llm_cooldown_state()

    def test_missing_api_key_raises(self):
        with patch.dict("os.environ", {"DEEPSEEK_API_KEY": ""}, clear=False):
            with self.assertRaises(RuntimeError) as ctx:
                openclaw_adapter._deepseek_payload("sys", "user")
        self.assertIn("DEEPSEEK_API_KEY", str(ctx.exception))

    def test_429_is_recorded_as_rate_limit_with_deepseek_transport(self):
        mock_resp = MagicMock(status_code=429, text='{"error":{"message":"too many requests"}}')
        with patch("polymarket_mvp.services.openclaw_adapter.requests.post", return_value=mock_resp):
            with self.assertRaises(LLMRateLimitError) as ctx:
                openclaw_adapter._deepseek_payload("sys", "user")
        self.assertEqual(ctx.exception.transport, "deepseek")
        self.assertGreater(llm_cooldown_remaining_sec("deepseek"), 0)

    def test_in_cooldown_raises_without_http_call(self):
        openclaw_adapter._cooldown_state("deepseek")["cooldown_until"] = time.time() + 1800
        openclaw_adapter._cooldown_state("deepseek")["consecutive_count"] = 1
        post_mock = MagicMock()
        with patch("polymarket_mvp.services.openclaw_adapter.requests.post", post_mock):
            with self.assertRaises(LLMRateLimitError):
                openclaw_adapter._deepseek_payload("sys", "user")
        post_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
