from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, Mapping

import requests

from ..common import sanitize_text


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
    starts = [idx for idx, char in enumerate(text) if char in "[{"]
    for idx in reversed(starts):
        candidate = text[idx:]
        try:
            return json.loads(candidate)
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


def chat_payload(system_prompt: str, user_prompt: str, *, temperature: float = 0.2) -> Any | None:
    mode = _transport_mode()
    if mode in {"cli", "local"}:
        return _cli_payload(system_prompt, user_prompt)
    endpoint, headers = _http_chat_endpoint()
    if mode in {"http", "api", "openai"} or endpoint is not None:
        if endpoint is None:
            return None
        payload = {
            "model": os.getenv("OPENCLAW_MODEL") or os.getenv("OPENAI_MODEL") or "gpt-4.1-mini",
            "temperature": temperature,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }
        response = requests.post(endpoint, headers=headers, json=payload, timeout=45)
        response.raise_for_status()
        return response.json()
    return _cli_payload(system_prompt, user_prompt)


def is_enabled() -> bool:
    return _http_chat_endpoint()[0] is not None or _cli_path() is not None


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


def chat_list(system_prompt: str, user_prompt: str, *, temperature: float = 0.2) -> list[Dict[str, Any]] | None:
    body = chat_payload(system_prompt, user_prompt, temperature=temperature)
    if body is None:
        return None
    if isinstance(body, list):
        return [item for item in body if isinstance(item, dict)]
    if isinstance(body, dict) and isinstance(body.get("proposals"), list):
        return [item for item in body["proposals"] if isinstance(item, dict)]
    text = _extract_text(body)
    if not text:
        return None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict) and isinstance(payload.get("proposals"), list):
        return [item for item in payload["proposals"] if isinstance(item, dict)]
    return None


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
            " Return valid JSON only. No markdown, no commentary, no prose outside JSON."
            " Preferred format: an object with a proposals array; a top-level JSON array is also accepted."
            " If there are no valid proposals, return {\"proposals\": []}."
            " Only use market_id values and outcome strings that exactly appear in the provided payload."
            " Never invent markets, outcomes, sources, prices, probabilities, or extra fields."
            " Each proposal object must contain exactly these keys: market_id, outcome, confidence_score, recommended_size_usdc, reasoning, max_slippage_bps."
            " confidence_score must be a number between 0 and 1."
            " recommended_size_usdc must be a positive number and should respect the provided default/constraints unless strong evidence in the payload supports going lower."
            " max_slippage_bps must be a positive integer and must not exceed the provided constraint."
            " reasoning must be concise, factual, plain text, and grounded only in the provided market/context payload."
            " Do not duplicate the same market_id + outcome pair."
            " At most one proposal per market unless the payload explicitly allows otherwise."
            " If an item would violate constraints or requires guessing, drop it instead of repairing it with invented data."
        ),
        json.dumps(prompt_payload, ensure_ascii=True, sort_keys=False),
    )
