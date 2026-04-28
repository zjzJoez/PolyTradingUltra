"""football-data.org adapter for real-time team form context.

The adapter is intentionally best-effort: any network/API/parse failure
silently returns None so the proposer pipeline keeps running with whatever
other context providers produced. We never raise out of this module.

Free-tier rate limit: 10 req/min — fetching two team-search calls plus two
recent-match calls per market keeps us well under that ceiling at the
10-minute proposer cadence.
"""

from __future__ import annotations

import os
import re
from typing import Any, Mapping

import requests

from ..common import sanitize_text


_BASE_URL = "https://api.football-data.org/v4"
_TIMEOUT = 8


def _api_key() -> str | None:
    key = (os.getenv("FOOTBALL_DATA_API_KEY") or "").strip()
    return key or None


def _headers() -> dict[str, str]:
    return {"X-Auth-Token": _api_key() or ""}


_VS_PATTERN = re.compile(
    r"(?:\s+(?:vs\.?|v\.?|versus|@)\s+)",
    flags=re.IGNORECASE,
)
_NOISE_PREFIX = re.compile(
    r"^(?:will|do|does|did|can|could|are|is|the|a)\s+",
    flags=re.IGNORECASE,
)
_NOISE_SUFFIX = re.compile(
    r"\s+(?:win|beat|defeat|advance|qualify|score|cover|scoreline|match|game|fixture|"
    r"on\s+\w+|today|tonight|this\s+\w+|by\s+\w+|\?)\s*$",
    flags=re.IGNORECASE,
)


def _clean_team_token(raw: str) -> str:
    s = sanitize_text(raw or "")
    s = _NOISE_PREFIX.sub("", s)
    s = _NOISE_SUFFIX.sub("", s)
    s = re.sub(r"[?!.,]+$", "", s).strip()
    s = re.sub(r"\s+", " ", s)
    return s


def _extract_teams(question: str) -> tuple[str, str] | None:
    """Pull two team-name tokens from a market question.

    Handles the common Polymarket shapes: "Team A vs Team B", "Team A v.
    Team B", "Will Team A beat Team B", "Team A @ Team B". Returns None when
    we can't confidently identify two distinct names.
    """
    if not question:
        return None
    q = sanitize_text(question)
    parts = _VS_PATTERN.split(q, maxsplit=1)
    if len(parts) == 2:
        a = _clean_team_token(parts[0])
        b = _clean_team_token(parts[1])
        if a and b and a.lower() != b.lower():
            return a, b
    m = re.match(r"^(?:will|does|do|did)\s+(.+?)\s+(?:beat|defeat|outscore)\s+(.+?)(?:\?|$)", q, flags=re.IGNORECASE)
    if m:
        a = _clean_team_token(m.group(1))
        b = _clean_team_token(m.group(2))
        if a and b and a.lower() != b.lower():
            return a, b
    return None


def _search_team(name: str) -> int | None:
    if not name or _api_key() is None:
        return None
    try:
        resp = requests.get(
            f"{_BASE_URL}/teams",
            params={"name": name},
            headers=_headers(),
            timeout=_TIMEOUT,
        )
        if resp.status_code != 200:
            return None
        body = resp.json() or {}
    except Exception:
        return None
    teams = body.get("teams") or []
    if not isinstance(teams, list) or not teams:
        return None
    needle = name.lower()
    best = None
    for team in teams:
        if not isinstance(team, dict):
            continue
        if str(team.get("name") or "").lower() == needle or str(team.get("shortName") or "").lower() == needle:
            return int(team.get("id"))
        if best is None and team.get("id") is not None:
            best = int(team["id"])
    return best


def _format_match(match: Mapping[str, Any], team_id: int) -> tuple[str, str] | None:
    """Return (token, outcome_letter) or None if the match is unusable.

    token looks like "W 2-1"; outcome_letter is one of W/D/L for the summary tally.
    """
    score = match.get("score") or {}
    full_time = score.get("fullTime") or {}
    home_score = full_time.get("home")
    away_score = full_time.get("away")
    if home_score is None or away_score is None:
        return None
    home = match.get("homeTeam") or {}
    away = match.get("awayTeam") or {}
    home_id = home.get("id")
    away_id = away.get("id")
    if home_id == team_id:
        own, opp = int(home_score), int(away_score)
    elif away_id == team_id:
        own, opp = int(away_score), int(home_score)
    else:
        return None
    letter = "W" if own > opp else "L" if own < opp else "D"
    return f"{letter} {own}-{opp}", letter


def _fetch_recent_form(team_id: int, limit: int = 5) -> str | None:
    if _api_key() is None:
        return None
    try:
        resp = requests.get(
            f"{_BASE_URL}/teams/{team_id}/matches",
            params={"status": "FINISHED", "limit": limit},
            headers=_headers(),
            timeout=_TIMEOUT,
        )
        if resp.status_code != 200:
            return None
        body = resp.json() or {}
    except Exception:
        return None
    matches = body.get("matches") or []
    if not isinstance(matches, list) or not matches:
        return None
    matches = sorted(
        (m for m in matches if isinstance(m, dict) and m.get("utcDate")),
        key=lambda m: str(m.get("utcDate")),
        reverse=True,
    )[:limit]
    tokens: list[str] = []
    tally: dict[str, int] = {"W": 0, "D": 0, "L": 0}
    for match in matches:
        result = _format_match(match, team_id)
        if result is None:
            continue
        token, letter = result
        tokens.append(token)
        tally[letter] = tally.get(letter, 0) + 1
    if not tokens:
        return None
    summary = f"[{tally['W']}W {tally['D']}D {tally['L']}L]"
    return ", ".join(tokens) + " " + summary


def build_sports_context(market: Mapping[str, Any]) -> str | None:
    """Compose a multi-line TEAM FORM block for the LLM, or None on any failure."""
    if _api_key() is None:
        return None
    question = str(market.get("question") or "")
    teams = _extract_teams(question)
    if teams is None:
        return None
    team_a, team_b = teams
    try:
        team_a_id = _search_team(team_a)
        team_b_id = _search_team(team_b)
    except Exception:
        return None
    if team_a_id is None or team_b_id is None:
        return None
    try:
        form_a = _fetch_recent_form(team_a_id)
        form_b = _fetch_recent_form(team_b_id)
    except Exception:
        return None
    if not form_a or not form_b:
        return None
    return (
        f"RECENT FORM ({team_a}, last 5): {form_a}\n"
        f"RECENT FORM ({team_b}, last 5): {form_b}"
    )
