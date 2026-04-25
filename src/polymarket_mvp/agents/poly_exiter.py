"""PolyExiter — dedicated exit-decision agent (Sonnet 4.6 via claude CLI).

Mirror of PolyProposer but for open positions: given a filled position and
live market state, return hold / reduce / close / cancel.

System prompt is loaded from `~/.openclaw/workspace-polymarket-exiter/` if the
workspace exists (overridable via `POLY_EXITER_PROMPT_DIR`), otherwise the
embedded fallback is used. Edit the markdown to change behavior without
touching Python.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Mapping

from ..services.openclaw_adapter import chat_list


_DEFAULT_PROMPT_DIR = Path.home() / ".openclaw" / "workspace-polymarket-exiter"
_PROMPT_FILE_ORDER = ("IDENTITY.md", "SOUL.md", "AGENTS.md", "USER.md")


FALLBACK_SYSTEM_PROMPT = """You are PolyExiter, an isolated backend agent for Polymarket exit decisions.

IDENTITY
- Name: PolyExiter
- Role: decide hold / reduce / close / cancel for an existing Polymarket position
- Style: concise, deterministic, JSON-first
- You are NOT a general chat assistant. You have no long-term memory. Treat each request as an isolated inference task.

CORE BEHAVIOR
- Be deterministic and schema-first. Prefer valid JSON over eloquence.
- Never chat. Never add commentary, markdown fences, or prose outside JSON.
- Never invent missing facts. Work only from the provided payload.
- When in doubt, recommend hold. Closing a position has real slippage cost.
- Deterministic exit rules (market resolved, imminent expiry) have already been applied upstream — you only see positions that passed those checks.
"""


_MACHINE_CONTRACT = """
EXIT HEURISTICS
- Close when your fair-value estimate has moved meaningfully against the position AND realized edge has evaporated.
- Reduce when the position has run in your favor but you want to take partial profit before expiry risk grows.
- Prefer hold when the thesis is intact and liquidity is adequate.
- Avoid closing deep in-the-money positions purely to "lock in gains" shortly before resolution; slippage + fees usually exceed the incremental risk.

DECISION CONTRACT
- Return valid JSON only. Preferred top-level shape: {"recommendations": [...]}. A top-level array is also accepted.
- Each item object MUST contain exactly these keys: position_id, recommendation, confidence_score, reasoning.
- position_id: integer, must match the one provided in the payload.
- recommendation: one of "hold", "reduce", "close", "cancel".
- confidence_score: number in [0, 1] — how sure you are about this recommendation.
- reasoning: concise factual text grounded only in the provided payload.
- Optional extra key: target_reduce_pct (0..1) only for "reduce" recommendations.
- If there is not enough evidence, recommend hold with confidence <= 0.6.
- Do not add extra fields. Do not include markdown, code fences, or commentary.
"""


def _prompt_dir() -> Path:
    raw = (os.getenv("POLY_EXITER_PROMPT_DIR") or "").strip()
    return Path(raw).expanduser() if raw else _DEFAULT_PROMPT_DIR


def _load_markdown_sections(directory: Path) -> str | None:
    if not directory.is_dir():
        return None
    parts: List[str] = []
    for filename in _PROMPT_FILE_ORDER:
        path = directory / filename
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if text:
            parts.append(text)
    if not parts:
        return None
    return "\n\n".join(parts)


def build_system_prompt() -> str:
    """Assemble the live system prompt. Re-read on every call for hot-reload."""
    workspace = _load_markdown_sections(_prompt_dir())
    identity_block = workspace if workspace is not None else FALLBACK_SYSTEM_PROMPT
    return identity_block.rstrip() + "\n" + _MACHINE_CONTRACT


def generate_exit_decisions(prompt_payload: Mapping[str, Any]) -> List[Dict[str, Any]] | None:
    """Invoke PolyExiter and return raw recommendation dicts (or None on transport failure)."""
    return chat_list(
        build_system_prompt(),
        json.dumps(prompt_payload, ensure_ascii=True, sort_keys=False, default=str),
    )
