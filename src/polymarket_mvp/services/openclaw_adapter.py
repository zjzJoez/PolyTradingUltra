from __future__ import annotations

import json
import os
from typing import Any, Dict, Mapping

import requests

from ..common import sanitize_text


def _chat_endpoint() -> tuple[str | None, dict[str, str]]:
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


def is_enabled() -> bool:
    endpoint, _ = _chat_endpoint()
    return endpoint is not None


def chat_json(system_prompt: str, user_prompt: str, *, temperature: float = 0.2) -> Dict[str, Any] | None:
    endpoint, headers = _chat_endpoint()
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
    body = response.json()
    content = body["choices"][0]["message"]["content"]
    text = sanitize_text(content)
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
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
