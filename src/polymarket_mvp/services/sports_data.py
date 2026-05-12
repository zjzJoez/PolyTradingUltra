"""football-data.org adapter for real-time team form context.

The adapter is intentionally best-effort: any network/API/parse failure
silently returns None so the proposer pipeline keeps running with whatever
other context providers produced. We never raise out of this module.

Free-tier rate limit is 10 req/min — the public `/teams?name=` endpoint
silently ignores the name filter (returns the first 50 teams in id order
regardless of the query), so we instead pre-load each free-tier
competition's team roster once and build an in-memory + on-disk name
index. Subsequent lookups are O(1) and don't touch the network.
"""

from __future__ import annotations

import json
import os
import re
import time
import unicodedata
from pathlib import Path
from typing import Any, Mapping

import requests


_BASE_URL = "https://api.football-data.org/v4"
_TIMEOUT = 8

# Competitions the free tier exposes. Loading all of them is ~9 API calls;
# we cache the result to disk so we only pay that cost on cold start.
_FREE_TIER_COMPETITIONS = (
    "PL",   # Premier League
    "PD",   # La Liga
    "BL1",  # Bundesliga
    "SA",   # Serie A
    "FL1",  # Ligue 1
    "DED",  # Eredivisie
    "PPL",  # Primeira Liga
    "ELC",  # Championship
    "CL",   # UEFA Champions League
    "EC",   # European Championship
    "WC",   # World Cup
)

_CACHE_TTL_SEC = 60 * 60 * 24  # roster shifts are slow; one-day cache is fine


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
_LEADING_LABEL = re.compile(
    r"^(?:game\s+handicap|games?\s+total|game\s+total|spread|exact\s+score|"
    r"handicap|moneyline|first\s+half|second\s+half|over/under|o/u|map\s*\d+|"
    r"counter[- ]?strike|league\s+of\s+legends|valorant|dota\s*2?|csgo|cs2)\s*[:\-]\s*",
    flags=re.IGNORECASE,
)
_TRAILING_QUALIFIER = re.compile(
    r"\s*(?:[:\-]\s*(?:o/u|over/under|over|under|map\s*\d+|map\s+winner|"
    r"first\s+half|second\s+half|moneyline|spread|total|tiebreak|set\s*\d+|"
    r"both\s+teams\s+to\s+score).*"
    r"|\([^)]*\)"
    r"|\s+(?:end\s+in\s+a\s+draw|to\s+win|to\s+score|both\s+teams\s+to\s+score).*"
    r")\s*$",
    flags=re.IGNORECASE,
)


def _clean_team_token(raw: str) -> str:
    s = (raw or "").strip()
    s = _LEADING_LABEL.sub("", s)
    s = _NOISE_PREFIX.sub("", s)
    s = _TRAILING_QUALIFIER.sub("", s)
    s = _NOISE_SUFFIX.sub("", s)
    s = re.sub(r"[?!.,;:]+$", "", s).strip()
    s = re.sub(r"\s+", " ", s)
    return s


def _extract_teams(question: str) -> tuple[str, str] | None:
    """Pull two team-name tokens from a market question."""
    if not question:
        return None
    q = question.strip()
    parts = _VS_PATTERN.split(q, maxsplit=1)
    if len(parts) == 2:
        a = _clean_team_token(parts[0])
        b = _clean_team_token(parts[1])
        if a and b and a.lower() != b.lower():
            return a, b
    m = re.match(
        r"^(?:will|does|do|did)\s+(.+?)\s+(?:beat|defeat|outscore)\s+(.+?)(?:\?|$)",
        q,
        flags=re.IGNORECASE,
    )
    if m:
        a = _clean_team_token(m.group(1))
        b = _clean_team_token(m.group(2))
        if a and b and a.lower() != b.lower():
            return a, b
    return None


def _normalize(name: str) -> str:
    """Fold to ASCII lowercase, strip universally-meaningless club boilerplate.

    Keeps distinguishing tokens (real, atletico, racing, …) so that
    "Real Madrid" and "Atlético Madrid" don't collide. Only strips suffix
    abbreviations (FC, CF, SC) and connector words (de, del, the, club,
    football) so "Club Atlético de Madrid" → "atletico madrid" matches
    Polymarket's "Atletico Madrid".
    """
    s = unicodedata.normalize("NFKD", name or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower()
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    s = re.sub(
        r"\b(?:fc|cf|sc|ac|sv|tsg|vfl|vfb|ff|cd|club|football|the|de|del|da|do|dos|das)\b",
        " ",
        s,
    )
    s = re.sub(r"\s+", " ", s).strip()
    return s


# In-memory + disk-backed team index. {normalized_name: team_id}.
# Ambiguous keys (two distinct team_ids normalize to the same string) get
# their team_id replaced with `None` — _search_team treats those as
# unresolvable and returns None instead of fabricating a head-to-head from
# whichever team happened to be inserted first.
_TEAM_INDEX: dict[str, int | None] | None = None
_INDEX_LOADED_AT: float = 0.0
_RATE_LIMIT_BACKOFF_UNTIL: float = 0.0


def _cache_path() -> Path:
    base = (
        os.getenv("POLYMARKET_MVP_STATE_DIR")
        or str(Path.home() / ".cache" / "polymarket-mvp")
    )
    return Path(base) / "football_data_team_index.json"


def _load_disk_cache() -> dict[str, int | None] | None:
    path = _cache_path()
    if not path.exists():
        return None
    try:
        if path.stat().st_mtime < time.time() - _CACHE_TTL_SEC:
            return None
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    out: dict[str, int | None] = {}
    for k, v in raw.items():
        if v is None:
            out[str(k)] = None
        else:
            try:
                out[str(k)] = int(v)
            except (TypeError, ValueError):
                continue
    return out


def _save_disk_cache(index: dict[str, int | None]) -> None:
    """Atomic write so concurrent readers never see a half-flushed file."""
    path = _cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
        tmp.write_text(json.dumps(index, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)
    except Exception:
        # Best-effort cleanup of the temp file if rename failed.
        try:
            tmp.unlink(missing_ok=True)  # type: ignore[name-defined]
        except Exception:
            pass


def _record_rate_limit_hit() -> None:
    global _RATE_LIMIT_BACKOFF_UNTIL
    # 429 on free tier means burst budget exceeded; back off for a minute.
    _RATE_LIMIT_BACKOFF_UNTIL = time.time() + 70.0


def _in_rate_limit_backoff() -> bool:
    return time.time() < _RATE_LIMIT_BACKOFF_UNTIL


def _add_team_to_index(index: dict[str, int | None], team: Mapping[str, Any]) -> None:
    """Insert a team's normalized name keys into the index.

    Collisions with an *existing different* team_id flip the slot to None
    (= ambiguous) so _search_team can refuse to guess instead of returning
    whichever team happened to be inserted first.
    """
    team_id = team.get("id")
    if not isinstance(team_id, int):
        return
    for key in ("name", "shortName", "tla"):
        raw = team.get(key)
        if not isinstance(raw, str) or not raw.strip():
            continue
        normalized = _normalize(raw)
        if not normalized:
            continue
        existing = index.get(normalized, "_unset")
        if existing == "_unset":
            index[normalized] = team_id
        elif existing is None:
            continue  # already marked ambiguous
        elif existing != team_id:
            index[normalized] = None


def _fetch_competition_teams(competition_code: str) -> list[Mapping[str, Any]]:
    if _api_key() is None or _in_rate_limit_backoff():
        return []
    try:
        resp = requests.get(
            f"{_BASE_URL}/competitions/{competition_code}/teams",
            headers=_headers(),
            timeout=_TIMEOUT,
        )
    except Exception:
        return []
    if resp.status_code == 429:
        _record_rate_limit_hit()
        return []
    if resp.status_code != 200:
        return []
    try:
        body = resp.json() or {}
    except Exception:
        return []
    teams = body.get("teams") or []
    return teams if isinstance(teams, list) else []


def _build_team_index() -> dict[str, int | None]:
    """Hydrate the team-name → id index from cache or by fetching rosters."""
    global _TEAM_INDEX, _INDEX_LOADED_AT
    if _TEAM_INDEX is not None and time.time() - _INDEX_LOADED_AT < _CACHE_TTL_SEC:
        return _TEAM_INDEX
    cached = _load_disk_cache()
    if cached:
        _TEAM_INDEX = cached
        _INDEX_LOADED_AT = time.time()
        return _TEAM_INDEX
    index: dict[str, int | None] = {}
    for code in _FREE_TIER_COMPETITIONS:
        if _in_rate_limit_backoff():
            break
        for team in _fetch_competition_teams(code):
            _add_team_to_index(index, team)
    if index:
        _save_disk_cache(index)
        _TEAM_INDEX = index
        _INDEX_LOADED_AT = time.time()
    else:
        # Avoid re-hammering when every competition fetch failed; remember the
        # empty result for the cache TTL so we stay silent until the upstream
        # recovers (or the cache is wiped).
        _TEAM_INDEX = {}
        _INDEX_LOADED_AT = time.time()
    return _TEAM_INDEX


def _search_team(name: str) -> int | None:
    if not name or _api_key() is None:
        return None
    index = _build_team_index()
    if not index:
        return None
    needle = _normalize(name)
    if not needle:
        return None
    if needle in index:
        # `None` slot = two distinct teams collapsed to this normalized key.
        # Refuse to guess — return None so the adapter skips the market.
        return index[needle]
    # Multi-token containment fallback: every token of the needle must appear
    # as a token of the candidate (or vice versa for unique 2+-token names).
    # Plain substring matches were too loose — every "Esports" team and every
    # "Saudi" club collapsed to whichever key happened to be inserted first.
    needle_tokens = needle.split()
    if len(needle_tokens) < 2:
        return None
    needle_set = set(needle_tokens)
    matches: list[tuple[int, int]] = []  # (score, team_id) for unambiguous slots
    for key, tid in index.items():
        if tid is None:
            continue
        key_tokens = set(key.split())
        if len(key_tokens) < 2:
            continue
        if needle_set.issubset(key_tokens) or key_tokens.issubset(needle_set):
            matches.append((len(needle_set & key_tokens), tid))
    if not matches:
        return None
    # If multiple distinct team_ids tie at the top score, refuse to pick.
    matches.sort(key=lambda x: x[0], reverse=True)
    top_score = matches[0][0]
    top_ids = {tid for score, tid in matches if score == top_score}
    if len(top_ids) > 1:
        return None
    return matches[0][1]


def _format_match(match: Mapping[str, Any], team_id: int) -> tuple[str, str] | None:
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
    if _api_key() is None or _in_rate_limit_backoff():
        return None
    try:
        resp = requests.get(
            f"{_BASE_URL}/teams/{team_id}/matches",
            params={"status": "FINISHED", "limit": limit},
            headers=_headers(),
            timeout=_TIMEOUT,
        )
    except Exception:
        return None
    if resp.status_code == 429:
        _record_rate_limit_hit()
        return None
    if resp.status_code != 200:
        return None
    try:
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
    tally = {"W": 0, "D": 0, "L": 0}
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
    team_a_id = _search_team(team_a)
    team_b_id = _search_team(team_b)
    if team_a_id is None or team_b_id is None:
        return None
    if team_a_id == team_b_id:
        # Both halves resolved to the same id — extraction or normalization
        # collapsed two distinct teams. Don't fabricate a head-to-head form.
        return None
    form_a = _fetch_recent_form(team_a_id)
    form_b = _fetch_recent_form(team_b_id)
    if not form_a or not form_b:
        return None
    return (
        f"RECENT FORM ({team_a}, last 5): {form_a}\n"
        f"RECENT FORM ({team_b}, last 5): {form_b}"
    )


def _reset_caches_for_test() -> None:
    """Test hook — clear in-memory caches between unit tests."""
    global _TEAM_INDEX, _INDEX_LOADED_AT, _RATE_LIMIT_BACKOFF_UNTIL
    _TEAM_INDEX = None
    _INDEX_LOADED_AT = 0.0
    _RATE_LIMIT_BACKOFF_UNTIL = 0.0
