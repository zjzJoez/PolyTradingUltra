from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, Mapping

import requests

from ..common import sanitize_text, utc_now_iso


# Transient-error throttled logging + cooldown for rate-limited upstream calls.
_TRANSIENT_STATE: dict[str, float] = {"last_log": 0.0, "cooldown_until": 0.0}

# Metadata from the most recent successful claude CLI invocation. Callers that
# want to persist {model, latency_ms, tokens, cost, session_id} per proposal
# pull from here right after chat_list() / chat_json() returns, rather than
# threading an extra return value through every helper.
_LAST_META: dict[str, Any] = {}


class LLMRateLimitError(RuntimeError):
    """Raised when an LLM transport (claude CLI, codex CLI, …) hits a usage limit.

    Carries stderr_snippet + cooldown_applied_sec + consecutive_count so
    callers can persist the event and honor the cooldown. `transport` names
    which provider triggered the limit so observability can group events.
    """

    def __init__(
        self,
        stderr_snippet: str,
        cooldown_sec: int,
        consecutive_count: int,
        transport: str = "llm",
    ):
        self.stderr_snippet = stderr_snippet
        self.cooldown_sec = cooldown_sec
        self.consecutive_count = consecutive_count
        self.transport = transport
        super().__init__(
            f"{transport} CLI usage limit (consecutive={consecutive_count}, cooldown={cooldown_sec}s)"
        )


# Exponential-backoff cooldown is per-transport — a Claude 5-hour-limit hit
# must NOT silence the Codex CLI and vice versa. Each transport gets its own
# {cooldown_until, last_hit_at, consecutive_count} bucket. The "default" key
# preserves the pre-split semantics for callers that don't pass a transport.
# Cadence: 1st hit = 30 min, 2nd within 2hr = 1hr, 3rd within 6hr = 6hr.
_LLM_COOLDOWN_STATES: dict[str, dict[str, float]] = {}

_COOLDOWN_SCHEDULE_SEC: tuple[int, ...] = (30 * 60, 60 * 60, 6 * 60 * 60)
_RESET_WINDOW_SEC = 6 * 60 * 60

_RATE_LIMIT_MARKERS: tuple[str, ...] = (
    "usage limit reached",
    "rate limit",
    "rate_limit",
    "5-hour limit",
    "5 hour limit",
    "weekly limit",
    "weekly_limit",
    "429",
)

# Patterns that a CLI's stderr/stdout might leak into the rate-limit log
# (and onward into Postgres llm_rate_limit_events.stderr_snippet). Redact
# the value side of any of these before persisting.
_SECRET_REDACT_PATTERN = re.compile(
    r"\b(authorization|bearer|token|api[_-]?key|x[_-]auth[_-]token|x[_-]api[_-]key|"
    r"openai[_-]?api[_-]?key|anthropic[_-]?api[_-]?key|x[_-]?goog[_-]?api[_-]?key)"
    r"(\s*[:=]\s*|\s+)([^\s\"'`]{6,})",
    flags=re.IGNORECASE,
)
# Matches likely bearer/sk-/eyJ tokens floating in plain text (Bearer prefix
# already covered above; this catches naked tokens in error blobs).
_BARE_SECRET_PATTERN = re.compile(
    r"\b(?:sk-[A-Za-z0-9_\-]{16,}|eyJ[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-.]+)",
)


def _redact_secrets(text: str) -> str:
    if not text:
        return text
    text = _SECRET_REDACT_PATTERN.sub(lambda m: f"{m.group(1)}{m.group(2)}<redacted>", text)
    text = _BARE_SECRET_PATTERN.sub("<redacted>", text)
    return text


def _looks_like_rate_limit(text: str) -> bool:
    lowered = (text or "").lower()
    return any(marker in lowered for marker in _RATE_LIMIT_MARKERS)


def _cooldown_state(transport: str) -> dict[str, float]:
    state = _LLM_COOLDOWN_STATES.get(transport)
    if state is None:
        state = {"cooldown_until": 0.0, "last_hit_at": 0.0, "consecutive_count": 0}
        _LLM_COOLDOWN_STATES[transport] = state
    return state


def _record_llm_rate_limit_hit(stderr_snippet: str, transport: str = "default") -> LLMRateLimitError:
    state = _cooldown_state(transport)
    now = time.time()
    # If the previous hit was long enough ago, reset the escalation counter.
    if now - state["last_hit_at"] > _RESET_WINDOW_SEC:
        state["consecutive_count"] = 0
    state["consecutive_count"] = int(state["consecutive_count"]) + 1
    idx = min(int(state["consecutive_count"]) - 1, len(_COOLDOWN_SCHEDULE_SEC) - 1)
    cooldown = _COOLDOWN_SCHEDULE_SEC[idx]
    state["cooldown_until"] = now + cooldown
    state["last_hit_at"] = now
    return LLMRateLimitError(
        stderr_snippet=_redact_secrets(sanitize_text(stderr_snippet))[:400],
        cooldown_sec=cooldown,
        consecutive_count=int(state["consecutive_count"]),
        transport=transport,
    )


def llm_cooldown_remaining_sec(transport: str = "default") -> float:
    """Seconds remaining in the active cooldown for `transport`. 0 if none."""
    state = _cooldown_state(transport)
    return max(0.0, state["cooldown_until"] - time.time())


def reset_llm_cooldown_state(transport: str | None = None) -> None:
    """Test-only: clear cooldown state. Pass a transport name or None for all."""
    if transport is None:
        _LLM_COOLDOWN_STATES.clear()
        return
    _LLM_COOLDOWN_STATES.pop(transport, None)


def get_last_meta() -> dict[str, Any] | None:
    """Return a copy of the metadata from the most recent claude CLI call."""
    return dict(_LAST_META) if _LAST_META else None


def clear_last_meta() -> None:
    _LAST_META.clear()


def _log_transient(status: int, retry_after: str | None) -> None:
    now = time.time()
    if now - _TRANSIENT_STATE["last_log"] < 30:
        return
    _TRANSIENT_STATE["last_log"] = now
    hint = f" retry_after={retry_after}" if retry_after else ""
    print(
        f"[openclaw_adapter] upstream transient HTTP {status}{hint}; skipping this tick",
        file=sys.stderr,
        flush=True,
    )


def _set_cooldown(seconds: float) -> None:
    _TRANSIENT_STATE["cooldown_until"] = max(
        _TRANSIENT_STATE["cooldown_until"], time.time() + seconds
    )


def _in_cooldown() -> bool:
    return time.time() < _TRANSIENT_STATE["cooldown_until"]


def _transport_mode() -> str:
    return (os.getenv("OPENCLAW_TRANSPORT") or "auto").strip().lower()


def _cli_path() -> str | None:
    configured = (os.getenv("OPENCLAW_CLI_PATH") or "").strip()
    candidates = []
    if configured:
        candidates.append(configured)
    discovered = shutil.which("openclaw")
    if discovered:
        candidates.append(discovered)
    candidates.extend(
        [
            str(Path.home() / ".openclaw-local" / "bin" / "openclaw"),
            str(Path.home() / ".local" / "bin" / "openclaw"),
        ]
    )
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return candidate
    return None


def _claude_cli_path() -> str | None:
    configured = (os.getenv("CLAUDE_CLI_PATH") or "").strip()
    candidates = []
    if configured:
        candidates.append(configured)
    discovered = shutil.which("claude")
    if discovered:
        candidates.append(discovered)
    candidates.extend(
        [
            str(Path.home() / ".local" / "bin" / "claude"),
            "/usr/local/bin/claude",
        ]
    )
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return candidate
    return None


def _codex_cli_path() -> str | None:
    configured = (os.getenv("CODEX_CLI_PATH") or "").strip()
    candidates: list[str] = []
    if configured:
        candidates.append(configured)
    discovered = shutil.which("codex")
    if discovered:
        candidates.append(discovered)
    candidates.extend(
        [
            str(Path.home() / ".local" / "bin" / "codex"),
            "/usr/local/bin/codex",
        ]
    )
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return candidate
    return None


def _codex_payload(system_prompt: str, user_prompt: str) -> Any:
    """Run GPT-5.5 via Codex CLI (OAuth-authenticated, no API key billing)."""
    cli_path = _codex_cli_path()
    if cli_path is None:
        return None
    model = (os.getenv("CODEX_MODEL") or "gpt-5.5").strip()
    timeout_seconds = (os.getenv("CODEX_TIMEOUT_SECONDS") or "").strip()
    timeout = int(timeout_seconds) if timeout_seconds else 180
    # Codex CLI exposes a non-interactive `exec` mode for scripted use:
    # `codex exec --json -m <model> --skip-git-repo-check <prompt>` reads the
    # full prompt as a positional arg and emits one JSONL event per line.
    prompt = (
        "System instructions:\n"
        f"{system_prompt}\n\n"
        "User payload:\n"
        f"{user_prompt}\n\n"
        "Return valid JSON only. Do not wrap the JSON in markdown fences."
    )
    command = [
        cli_path,
        "exec",
        "--json",
        "--skip-git-repo-check",
        "--sandbox",
        "read-only",
        "-m",
        model,
        prompt,
    ]
    if llm_cooldown_remaining_sec("codex") > 0:
        state = _cooldown_state("codex")
        raise LLMRateLimitError(
            stderr_snippet="still in cooldown; skipping CLI invocation",
            cooldown_sec=max(0, int(state["cooldown_until"] - time.time())),
            consecutive_count=int(state["consecutive_count"]),
            transport="codex",
        )
    start = time.monotonic()
    # `codex exec` reads stdin even when a positional prompt is provided,
    # so we close it explicitly — otherwise subprocess hangs waiting for EOF
    # when the parent has no TTY (systemd-run, our case).
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        stdin=subprocess.DEVNULL,
    )
    latency_ms = int((time.monotonic() - start) * 1000)
    if result.returncode != 0:
        raw = result.stderr or result.stdout or ""
        stderr = _redact_secrets(sanitize_text(raw))
        if _looks_like_rate_limit(raw):
            raise _record_llm_rate_limit_hit(stderr, transport="codex")
        raise RuntimeError(f"Codex CLI failed: {stderr or result.returncode}")
    final_text = _extract_codex_final_text(result.stdout or "")
    parsed = _decode_json_candidate(final_text or result.stdout or "")
    _LAST_META.clear()
    _LAST_META.update({
        "model": model,
        "session_id": None,
        "latency_ms": latency_ms,
        "called_at": utc_now_iso(),
        "transport": "codex_cli",
    })
    return parsed if parsed is not None else final_text


def _extract_codex_final_text(stdout: str) -> str | None:
    """Walk Codex `exec --json` JSONL events and return the final agent message text.

    Codex emits one JSON object per line. The terminal text we want lives in
    an `item.completed` event whose item is `{"type":"agent_message","text":"..."}`.
    """
    if not stdout:
        return None
    last_text: str | None = None
    for line in stdout.splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        item = event.get("item")
        if isinstance(item, dict):
            item_type = item.get("type") or item.get("item_type")
            if item_type in ("agent_message", "assistant_message"):
                text = item.get("text") or item.get("content")
                if isinstance(text, str) and text.strip():
                    last_text = text
        msg = event.get("msg")
        if isinstance(msg, dict):
            text = msg.get("message") or msg.get("text")
            if isinstance(text, str) and text.strip():
                last_text = text
    return last_text


_DEEPSEEK_DEFAULT_BASE_URL = "https://api.deepseek.com/v1"
_DEEPSEEK_DEFAULT_MODEL = "deepseek-chat"


def _deepseek_payload(system_prompt: str, user_prompt: str, *, temperature: float = 0.2) -> Any:
    """Call DeepSeek via its OpenAI-compatible HTTP API. Used as the fallback
    transport when Codex CLI fails (rate-limit OR generic error like
    'Codex CLI failed: ... thread not found' which we've seen in production
    after quota exhaustion). Returns the raw JSON body — `chat_json` /
    `chat_list` parse the OpenAI-style `choices[0].message.content` via the
    existing `_extract_text` recursion."""
    api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("DEEPSEEK_API_KEY not configured — cannot fall back to DeepSeek")

    if llm_cooldown_remaining_sec("deepseek") > 0:
        state = _cooldown_state("deepseek")
        raise LLMRateLimitError(
            stderr_snippet="still in cooldown; skipping DeepSeek call",
            cooldown_sec=max(0, int(state["cooldown_until"] - time.time())),
            consecutive_count=int(state["consecutive_count"]),
            transport="deepseek",
        )

    base_url = (os.getenv("DEEPSEEK_BASE_URL") or _DEEPSEEK_DEFAULT_BASE_URL).rstrip("/")
    endpoint = f"{base_url}/chat/completions"
    model = (os.getenv("DEEPSEEK_MODEL") or _DEEPSEEK_DEFAULT_MODEL).strip()
    timeout_seconds_raw = (os.getenv("DEEPSEEK_TIMEOUT_SECONDS") or "").strip()
    timeout = int(timeout_seconds_raw) if timeout_seconds_raw else 60

    body = {
        "model": model,
        "temperature": temperature,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        # DeepSeek honors OpenAI's response_format for JSON mode — this keeps
        # the contract that `chat_json` expects (no markdown fences, valid
        # JSON-only output).
        "response_format": {"type": "json_object"},
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    start = time.monotonic()
    try:
        resp = requests.post(endpoint, headers=headers, json=body, timeout=timeout)
    except requests.RequestException as exc:
        raise RuntimeError(f"DeepSeek request failed: {type(exc).__name__}: {exc}")
    latency_ms = int((time.monotonic() - start) * 1000)

    if resp.status_code >= 400:
        # Body may carry an api-error JSON like {"error":{"message":"..."}}; redact
        # before persisting so a malformed key in our env doesn't leak into logs.
        body_text = _redact_secrets(sanitize_text(resp.text or "")) or f"http_{resp.status_code}"
        if resp.status_code == 429 or _looks_like_rate_limit(body_text):
            raise _record_llm_rate_limit_hit(body_text, transport="deepseek")
        raise RuntimeError(f"DeepSeek HTTP {resp.status_code}: {body_text[:300]}")

    try:
        payload = resp.json()
    except ValueError as exc:
        raise RuntimeError(f"DeepSeek returned non-JSON body: {exc}")

    _LAST_META.clear()
    _LAST_META.update({
        "model": model,
        "session_id": None,
        "latency_ms": latency_ms,
        "called_at": utc_now_iso(),
        "transport": "deepseek_http",
    })
    return payload


def _claude_payload(system_prompt: str, user_prompt: str) -> Any:
    cli_path = _claude_cli_path()
    if cli_path is None:
        return None
    model = (os.getenv("CLAUDE_MODEL") or "claude-sonnet-4-6").strip()
    timeout_seconds = (os.getenv("CLAUDE_TIMEOUT_SECONDS") or "").strip()
    timeout = int(timeout_seconds) if timeout_seconds else 180
    command = [
        cli_path,
        "-p",
        user_prompt,
        "--system-prompt",
        system_prompt,
        "--model",
        model,
        "--output-format",
        "json",
        "--no-session-persistence",
        "--tools",
        "",
        "--disable-slash-commands",
        "--setting-sources",
        "",
    ]
    # Skip work if a prior hit put us in cooldown — don't hammer the CLI and
    # don't burn wall time on a call that will fail. Raise with the *existing*
    # state rather than calling _record_llm_rate_limit_hit(), which would
    # increment consecutive_count and push cooldown_until further out on every
    # call (the exit loop fires every 30s — without this guard it turns a
    # 30-min cooldown into many hours).
    if llm_cooldown_remaining_sec("claude") > 0:
        state = _cooldown_state("claude")
        raise LLMRateLimitError(
            stderr_snippet="still in cooldown; skipping CLI invocation",
            cooldown_sec=max(0, int(state["cooldown_until"] - time.time())),
            consecutive_count=int(state["consecutive_count"]),
            transport="claude",
        )
    start = time.monotonic()
    result = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
    latency_ms = int((time.monotonic() - start) * 1000)
    if result.returncode != 0:
        raw = result.stderr or result.stdout or ""
        stderr = _redact_secrets(sanitize_text(raw))
        if _looks_like_rate_limit(raw):
            raise _record_llm_rate_limit_hit(stderr, transport="claude")
        raise RuntimeError(f"Claude CLI failed: {stderr or result.returncode}")
    envelope = _decode_json_candidate(result.stdout or "")
    if isinstance(envelope, dict):
        if envelope.get("is_error"):
            err_blob = f"{envelope.get('subtype')} {envelope.get('result')}"
            if _looks_like_rate_limit(err_blob):
                raise _record_llm_rate_limit_hit(err_blob, transport="claude")
            raise RuntimeError(
                f"Claude CLI returned error: {envelope.get('result') or envelope.get('subtype')}"
            )
        usage = envelope.get("usage") if isinstance(envelope.get("usage"), dict) else {}
        _LAST_META.clear()
        _LAST_META.update({
            "model": envelope.get("model") or model,
            "session_id": envelope.get("session_id"),
            "latency_ms": latency_ms,
            "duration_ms": envelope.get("duration_ms"),
            "num_turns": envelope.get("num_turns"),
            "input_tokens": usage.get("input_tokens"),
            "output_tokens": usage.get("output_tokens"),
            "cache_read_input_tokens": usage.get("cache_read_input_tokens"),
            "cache_creation_input_tokens": usage.get("cache_creation_input_tokens"),
            "total_cost_usd": envelope.get("total_cost_usd"),
            "stop_reason": envelope.get("stop_reason"),
            "called_at": utc_now_iso(),
        })
        inner = envelope.get("result")
        if isinstance(inner, str):
            parsed = _decode_json_candidate(inner)
            if parsed is not None:
                return parsed
            return inner
        if inner is not None:
            return inner
    return envelope


def _http_chat_endpoint() -> tuple[str | None, dict[str, str]]:
    base_url = (os.getenv("OPENCLAW_API_URL") or "").strip()
    api_key = (os.getenv("OPENCLAW_API_KEY") or "").strip()
    if base_url and api_key:
        return base_url, {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    openai_key = (os.getenv("OPENAI_API_KEY") or "").strip()
    if openai_key:
        return "https://api.openai.com/v1/chat/completions", {
            "Authorization": f"Bearer {openai_key}",
            "Content-Type": "application/json",
        }
    return None, {}


def _extract_text(payload: Any) -> str | None:
    if isinstance(payload, str):
        text = sanitize_text(payload)
        return text or None
    if isinstance(payload, list):
        for item in reversed(payload):
            text = _extract_text(item)
            if text:
                return text
        return None
    if not isinstance(payload, dict):
        return None
    if isinstance(payload.get("role"), str) and payload.get("role") == "assistant":
        text = _extract_text(payload.get("content"))
        if text:
            return text
    preferred_keys = (
        "payloads",
        "content",
        "text",
        "reply",
        "message",
        "output",
        "response",
        "result",
        "data",
        "assistant",
        "final",
        "messages",
        "choices",
    )
    for key in preferred_keys:
        if key not in payload:
            continue
        text = _extract_text(payload.get(key))
        if text:
            return text
    return None


def _decode_json_candidate(raw: str) -> Any | None:
    text = (raw or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    decoder = json.JSONDecoder()
    starts = [idx for idx, char in enumerate(text) if char in "[{"]
    for idx in starts:
        try:
            value, _ = decoder.raw_decode(text[idx:])
            return value
        except json.JSONDecodeError:
            continue
    return None


def _cli_payload(system_prompt: str, user_prompt: str) -> Any:
    cli_path = _cli_path()
    if cli_path is None:
        return None
    command = [cli_path, "agent", "--local", "--json"]
    agent_id = (os.getenv("OPENCLAW_AGENT_ID") or "").strip()
    if agent_id:
        command.extend(["--agent", agent_id])
    thinking = (os.getenv("OPENCLAW_THINKING") or "medium").strip()
    if thinking:
        command.extend(["--thinking", thinking])
    timeout_seconds = (os.getenv("OPENCLAW_TIMEOUT_SECONDS") or "").strip()
    timeout = 120
    if timeout_seconds:
        timeout = int(timeout_seconds)
        command.extend(["--timeout", str(timeout)])
    prompt = (
        "System instructions:\n"
        f"{system_prompt}\n\n"
        "User payload:\n"
        f"{user_prompt}\n\n"
        "Return valid JSON only. Do not wrap the JSON in markdown fences."
    )
    command.extend(["--message", prompt])
    result = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
    if result.returncode != 0:
        stderr = sanitize_text(result.stderr or result.stdout)
        raise RuntimeError(f"OpenClaw CLI failed: {stderr or result.returncode}")
    parsed = _decode_json_candidate(result.stdout or "")
    if parsed is not None:
        return parsed
    parsed = _decode_json_candidate(result.stderr or "")
    if parsed is not None:
        return parsed
    raw = (result.stdout or result.stderr or "").strip()
    return raw or None


def _fallback_provider() -> str | None:
    """Provider to try when the primary transport raises a recoverable
    error (rate-limit OR generic CLI failure). Returns None to disable."""
    raw = (os.getenv("OPENCLAW_FALLBACK_PROVIDER") or "").strip().lower()
    return raw or None


def _try_fallback(provider: str, system_prompt: str, user_prompt: str, temperature: float, primary_exc: BaseException) -> Any:
    """Run the fallback provider after the primary raised. If the fallback
    itself fails, raise the *original* primary exception — the fallback's
    failure is logged but doesn't shadow what the caller cares about."""
    print(
        f"[openclaw] primary transport failed ({type(primary_exc).__name__}): "
        f"{str(primary_exc)[:160]} — falling back to {provider}",
        file=sys.stderr,
        flush=True,
    )
    try:
        if provider == "deepseek":
            return _deepseek_payload(system_prompt, user_prompt, temperature=temperature)
        # Unknown provider name in env — surface clearly so the user notices
        # rather than silently swallowing.
        raise RuntimeError(f"OPENCLAW_FALLBACK_PROVIDER={provider!r} not recognized")
    except Exception as fallback_exc:
        print(
            f"[openclaw] fallback {provider} also failed ({type(fallback_exc).__name__}): "
            f"{str(fallback_exc)[:160]}",
            file=sys.stderr,
            flush=True,
        )
        raise primary_exc from fallback_exc


def chat_payload(system_prompt: str, user_prompt: str, *, temperature: float = 0.2) -> Any | None:
    mode = _transport_mode()
    fallback = _fallback_provider()
    if mode in {"codex_cli", "codex"}:
        try:
            return _codex_payload(system_prompt, user_prompt)
        except (LLMRateLimitError, RuntimeError) as exc:
            if fallback is None:
                raise
            return _try_fallback(fallback, system_prompt, user_prompt, temperature, exc)
    if mode in {"claude", "claude_cli"}:
        try:
            return _claude_payload(system_prompt, user_prompt)
        except (LLMRateLimitError, RuntimeError) as exc:
            if fallback is None:
                raise
            return _try_fallback(fallback, system_prompt, user_prompt, temperature, exc)
    if mode in {"cli", "local"}:
        return _cli_payload(system_prompt, user_prompt)
    endpoint, headers = _http_chat_endpoint()
    if mode in {"http", "api", "openai"} or endpoint is not None:
        if endpoint is None:
            return None
        if _in_cooldown():
            return None
        payload = {
            "model": os.getenv("OPENCLAW_MODEL") or os.getenv("OPENAI_MODEL") or "gpt-4.1-mini",
            "temperature": temperature,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }
        try:
            response = requests.post(endpoint, headers=headers, json=payload, timeout=45)
            response.raise_for_status()
        except requests.HTTPError as exc:
            resp = exc.response
            status = getattr(resp, "status_code", None)
            if status in (408, 429, 500, 502, 503, 504):
                retry_after = resp.headers.get("Retry-After") if resp is not None else None
                try:
                    cooldown = float(retry_after) if retry_after else 60.0
                except ValueError:
                    cooldown = 60.0
                _set_cooldown(min(max(cooldown, 15.0), 900.0))
                _log_transient(status, retry_after)
                return None
            raise
        except requests.RequestException as exc:
            _set_cooldown(30.0)
            _log_transient(0, f"{type(exc).__name__}: {exc}")
            return None
        return response.json()
    return _cli_payload(system_prompt, user_prompt)


def is_enabled() -> bool:
    return (
        _http_chat_endpoint()[0] is not None
        or _cli_path() is not None
        or _claude_cli_path() is not None
        or _codex_cli_path() is not None
    )


def chat_json(system_prompt: str, user_prompt: str, *, temperature: float = 0.2) -> Dict[str, Any] | None:
    body = chat_payload(system_prompt, user_prompt, temperature=temperature)
    if body is None:
        return None
    if isinstance(body, dict) and not any(
        key in body for key in ("choices", "result", "data", "assistant", "message", "messages", "output", "response", "payloads", "meta")
    ):
        return body
    text = _extract_text(body)
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


_LIST_ENVELOPE_KEYS = ("proposals", "recommendations", "items")


def _unwrap_list(payload: Any) -> list[Dict[str, Any]] | None:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in _LIST_ENVELOPE_KEYS:
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return None


def chat_list(system_prompt: str, user_prompt: str, *, temperature: float = 0.2) -> list[Dict[str, Any]] | None:
    body = chat_payload(system_prompt, user_prompt, temperature=temperature)
    if body is None:
        return None
    unwrapped = _unwrap_list(body)
    if unwrapped is not None:
        return unwrapped
    text = _extract_text(body)
    if not text:
        return None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    return _unwrap_list(payload)


def maybe_generate_research_memo(prompt_payload: Mapping[str, Any]) -> Dict[str, Any] | None:
    return chat_json(
        "You generate compact factual research memo JSON for a trading agent. Return JSON only.",
        json.dumps(prompt_payload, ensure_ascii=True, sort_keys=False),
    )


def maybe_generate_supervisor_decision(prompt_payload: Mapping[str, Any]) -> Dict[str, Any] | None:
    return chat_json(
        "You rank and annotate trade proposals. Return JSON only.",
        json.dumps(prompt_payload, ensure_ascii=True, sort_keys=False),
    )


def maybe_generate_review(prompt_payload: Mapping[str, Any]) -> Dict[str, Any] | None:
    return chat_json(
        "You write compact post-trade review JSON. Return JSON only.",
        json.dumps(prompt_payload, ensure_ascii=True, sort_keys=False),
    )


def maybe_generate_trade_proposals(prompt_payload: Mapping[str, Any]) -> list[Dict[str, Any]] | None:
    return chat_list(
        (
            "You are the dedicated Polymarket proposal generator."
            " Optimize for tradable opportunities, not merely the highest apparent certainty."
            " Avoid near-certain, near-expiry, low-upside markets where the selected outcome is already priced close to 0 or 1."
            " IMPORTANT: Polymarket requires a minimum of 5 shares per order. With small order sizes, strongly prefer outcomes priced below 0.40"
            " (e.g. an underdog, a 'No' side, or a lower-probability outcome) so that the dollar amount buys enough shares."
            " For example: $2 at price 0.35 = 5.7 shares (valid); $2 at price 0.53 = 3.8 shares (rejected by exchange)."
            " Prefer proposals with realistic fill probability, non-trivial upside, and prices in a healthy tradable range."
            " Return valid JSON only. No markdown, no commentary, no prose outside JSON."
            " Preferred format: an object with a proposals array; a top-level JSON array is also accepted."
            " If there are no valid proposals, return {\"proposals\": []}."
            " Only use market_id values and outcome strings that exactly appear in the provided payload."
            " Never invent markets, outcomes, sources, prices, probabilities, or extra fields."
            " Each proposal object must contain exactly these keys: market_id, outcome, confidence_score, recommended_size_usdc, reasoning, max_slippage_bps."
            " confidence_score is YOUR independent probability estimate that this outcome will occur — NOT the current market price."
            " If you believe the market underprices an outcome (e.g., market price 0.45 but you estimate true probability 0.65), set confidence_score to 0.65."
            " Prefer trades where your confidence_score exceeds the market price (you see edge), but you may also propose based on market structure,"
            " liquidity patterns, odds value, or expiry timing even without external news context."
            " If context is empty, use the market data (prices, liquidity, question, expiry) to form your estimate."
            " confidence_score must be a number between 0 and 1."
            " recommended_size_usdc must equal exactly the default_recommended_size_usdc value provided in constraints. Do not go higher. You may go lower only if the market is illiquid."
            " max_slippage_bps must be a positive integer and must not exceed the provided constraint."
            " reasoning must be concise, factual, plain text, and grounded only in the provided market/context payload."
            " Do not duplicate the same market_id + outcome pair."
            " At most one proposal per market unless the payload explicitly allows otherwise."
            " If an item would violate constraints or requires guessing, drop it instead of repairing it with invented data."
        ),
        json.dumps(prompt_payload, ensure_ascii=True, sort_keys=False),
    )


def maybe_generate_exit_proposals(prompt_payload: Mapping[str, Any]) -> list[Dict[str, Any]] | None:
    return chat_list(
        (
            "You are a position exit advisor for a Polymarket trading system."
            " Given open positions with their entry data and current market state,"
            " decide whether to hold, reduce, or close each position."
            " Return valid JSON only. No markdown, no commentary."
            " Preferred format: an object with a recommendations array; a top-level JSON array is also accepted."
            " Each item must contain: position_id (integer), recommendation (hold/reduce/close/cancel),"
            " confidence_score (0-1), reasoning (concise factual text)."
            " Optional: target_reduce_pct (0-1, for reduce recommendations only)."
            " If there is not enough evidence to recommend action, recommend hold."
            " Do not invent data. Base decisions only on the provided payload."
        ),
        json.dumps(prompt_payload, ensure_ascii=True, sort_keys=False, default=str),
    )
