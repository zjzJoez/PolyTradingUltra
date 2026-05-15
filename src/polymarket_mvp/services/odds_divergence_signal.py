"""Strategy B — The Odds API consensus divergence detector.

When Polymarket's price for an outcome diverges significantly from the
consensus implied probability across 30-38 professional bookmakers (after
vig removal), that's a market mispricing — Polymarket lagging behind sharp
books, illiquidity, retail momentum, etc.

This is what professional sports bettors actually use: line-shopping between
Polymarket and the sharp books (Pinnacle, Betfair Exchange, ...) rather than
trying to outpredict the bookmakers with our own model.

API: The Odds API v4 — /sports/{sport}/odds, paid (500 free/month).
We share the existing TheOddsApiAdapter's daily-cap protection and sport-
keyword mapping rather than re-implementing them.

Markets we handle:
  - "Will A vs B end in a draw?"  → consensus h2h["Draw"]
  - "Will A win on YYYY-MM-DD?"   → consensus h2h[A's bookmaker name]
                                    (Odds API event resolves home/away for us
                                    so the Strategy-D home/away ambiguity disappears)
  - "A vs B: O/U N"               → consensus totals[N]["Over N"|"Under N"]

Signal flow:
  parse Polymarket question →
  resolve sport_key via team keyword →
  fetch /v4/sports/{sport_key}/odds (with daily-cap guard) →
  match event by team names (substring) →
  compute average implied prob across books →
  remove vig (additive normalization to sum=1.0) →
  compare to Polymarket Yes price →
  bet/skip/no_match based on absolute edge threshold (default 3pp).
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Mapping, Tuple

import requests

from ..common import get_env_int
from .clubelo_signal import (
    MarketSide,
    _parse_question,
    normalize_team,
)


DEFAULT_EDGE_THRESHOLD = 0.03  # 3pp absolute edge over vig-removed consensus
DEFAULT_DAILY_CAP = 15          # mirrors TheOddsApiAdapter
SIGNAL_NAME = "odds_divergence"


# Sport-keyword map — copied from TheOddsApiAdapter to keep the signal module
# independent of the context-adapter wiring. Update both if leagues change.
SPORT_KEYWORDS: Tuple[Tuple[str, str], ...] = (
    ("FC Barcelona", "soccer_spain_la_liga"),
    ("Real Madrid", "soccer_spain_la_liga"),
    ("Liga MX", "soccer_mexico_ligamx"),
    ("Premier League", "soccer_epl"),
    ("EFL Championship", "soccer_efl_champ"),
    ("Bundesliga", "soccer_germany_bundesliga"),
    ("Serie A", "soccer_italy_serie_a"),
    ("Ligue 1", "soccer_france_ligue_one"),
    ("MLS", "soccer_usa_mls"),
    ("UEFA Champions League", "soccer_uefa_champs_league"),
    ("La Liga", "soccer_spain_la_liga"),
    ("NBA", "basketball_nba"),
    ("NFL", "americanfootball_nfl"),
    ("MLB", "baseball_mlb"),
    ("NHL", "icehockey_nhl"),
)


def _resolve_sport_key(market: Mapping[str, Any]) -> str | None:
    """Return Odds-API sport_key by matching league/team keyword in the market."""
    haystack = " ".join([
        str(market.get("question") or ""),
        json.dumps(market.get("market_json") or {}, ensure_ascii=False)[:600],
    ]).lower()
    for keyword, sport_key in SPORT_KEYWORDS:
        if keyword.lower() in haystack:
            return sport_key
    return None


def _match_event(events: list, market: Mapping[str, Any], team_a_raw: str, team_b_raw: str | None) -> Dict[str, Any] | None:
    """Find the Odds-API event whose home/away pair matches the Polymarket
    question's teams. We use normalize_team() to compare across naming
    conventions (Polymarket "FC Barcelona" vs Odds-API "Barcelona")."""
    if not events:
        return None
    na = normalize_team(team_a_raw)
    nb = normalize_team(team_b_raw) if team_b_raw else None
    a_tokens = set(na.split())
    b_tokens = set(nb.split()) if nb else None
    for ev in events:
        h = normalize_team(ev.get("home_team") or "")
        a = normalize_team(ev.get("away_team") or "")
        h_tokens, a_tokens_ev = set(h.split()), set(a.split())
        # Bi-directional token-subset (each name fully contained in the other)
        a_match_home = bool(a_tokens) and (a_tokens.issubset(h_tokens) or h_tokens.issubset(a_tokens))
        a_match_away = bool(a_tokens) and (a_tokens.issubset(a_tokens_ev) or a_tokens_ev.issubset(a_tokens))
        if b_tokens is None:
            # Single-team market: a single side match is enough
            if a_match_home or a_match_away:
                return ev
            continue
        b_match_home = b_tokens.issubset(h_tokens) or h_tokens.issubset(b_tokens)
        b_match_away = b_tokens.issubset(a_tokens_ev) or a_tokens_ev.issubset(b_tokens)
        if (a_match_home and b_match_away) or (a_match_away and b_match_home):
            return ev
    return None


def _average_consensus(event: Dict[str, Any]) -> Dict[str, Any]:
    """Mean implied probabilities across all bookmakers, additively
    normalized to sum=1.0 (removing vig).

    Returns a dict with keys:
      - 'book_count': int
      - 'h2h': {outcome_name: probability}     when h2h market present
      - 'totals': {line: {outcome_name: probability}}   when totals present
    """
    bookmakers = event.get("bookmakers") or []
    if not bookmakers:
        return {}
    h2h_acc: Dict[str, List[float]] = {}
    totals_acc: Dict[str, Dict[str, List[float]]] = {}
    for bm in bookmakers:
        for mkt in (bm.get("markets") or []):
            if mkt.get("key") == "h2h":
                for o in (mkt.get("outcomes") or []):
                    name = o.get("name") or ""
                    try:
                        price = float(o.get("price") or 0)
                    except (TypeError, ValueError):
                        price = 0.0
                    if price > 0:
                        h2h_acc.setdefault(name, []).append(1.0 / price)
            elif mkt.get("key") == "totals":
                for o in (mkt.get("outcomes") or []):
                    line = str(o.get("point") or "")
                    side_label = (o.get("name") or "")
                    try:
                        price = float(o.get("price") or 0)
                    except (TypeError, ValueError):
                        price = 0.0
                    if price > 0:
                        totals_acc.setdefault(line, {}).setdefault(side_label, []).append(1.0 / price)
    result: Dict[str, Any] = {"book_count": len(bookmakers)}
    if h2h_acc:
        avg = {k: sum(v) / len(v) for k, v in h2h_acc.items()}
        total = sum(avg.values()) or 1.0
        result["h2h"] = {k: v / total for k, v in avg.items()}
    if totals_acc:
        normalized: Dict[str, Dict[str, float]] = {}
        for line, sides in totals_acc.items():
            avg = {k: sum(v) / len(v) for k, v in sides.items()}
            total = sum(avg.values()) or 1.0
            normalized[line] = {k: v / total for k, v in avg.items()}
        result["totals"] = normalized
    return result


# ────────────────────────── Polymarket outcome inference ──────────────────────────
def _polymarket_yes_price(market: Mapping[str, Any]) -> float | None:
    """Polymarket binary markets always have a Yes side. Pull its price."""
    for o in (market.get("outcomes") or []):
        if (o.get("name") or "").strip().lower() == "yes":
            try:
                return float(o.get("price") or 0)
            except (TypeError, ValueError):
                return None
    # Fallback: O/U markets use Over/Under as outcome names; the "Over" side
    # is treated as the Yes-equivalent for our divergence comparison.
    for o in (market.get("outcomes") or []):
        if (o.get("name") or "").strip().lower() == "over":
            try:
                return float(o.get("price") or 0)
            except (TypeError, ValueError):
                return None
    return None


def _parse_ou_question(question: str) -> Tuple[str, str, float] | None:
    """Recognise 'Team A vs. Team B: O/U N.N' markets. Returns
    (team_a, team_b, line) or None if not an O/U market."""
    if not question:
        return None
    m = re.match(
        r"^(.+?)\s+vs\.?\s+(.+?):\s*O/U\s*(\d+(?:\.\d+)?)\s*$",
        question.strip(),
        flags=re.IGNORECASE,
    )
    if not m:
        return None
    return m.group(1).strip(), m.group(2).strip(), float(m.group(3))


# ────────────────────────── Public signal class ──────────────────────────
class DivergenceSignal:
    def __init__(self, market_id: str, market_question: str, outcome: str,
                 recommendation: str, polymarket_p: float, consensus_p: float,
                 edge: float, book_count: int, sport_key: str, side: str,
                 reasoning: str, raw_consensus: Dict[str, Any] | None = None):
        self.market_id = market_id
        self.market_question = market_question
        self.outcome = outcome
        self.recommendation = recommendation   # "bet" | "skip" | "no_match"
        self.polymarket_p = polymarket_p
        self.consensus_p = consensus_p
        self.edge = edge                       # consensus_p - polymarket_p ⇒ positive means PM underprices
        self.book_count = book_count
        self.sport_key = sport_key
        self.side = side
        self.reasoning = reasoning
        self.raw_consensus = raw_consensus or {}

    def to_dict(self) -> Dict[str, Any]:
        return {
            "market_id": self.market_id,
            "market_question": self.market_question,
            "outcome": self.outcome,
            "recommendation": self.recommendation,
            "polymarket_p": self.polymarket_p,
            "consensus_p": self.consensus_p,
            "edge": self.edge,
            "book_count": self.book_count,
            "sport_key": self.sport_key,
            "side": self.side,
            "reasoning": self.reasoning,
            "raw_consensus": self.raw_consensus,
        }


def _no_match(market_id: str, question: str, sport_key: str, side: str, reason: str) -> DivergenceSignal:
    return DivergenceSignal(
        market_id=market_id, market_question=question, outcome="?",
        recommendation="no_match", polymarket_p=0.0, consensus_p=0.0,
        edge=0.0, book_count=0, sport_key=sport_key or "", side=side,
        reasoning=reason,
    )


def signal_for_market(
    market: Mapping[str, Any],
    *,
    edge_threshold: float = DEFAULT_EDGE_THRESHOLD,
    session: requests.Session | None = None,
    api_key: str | None = None,
) -> DivergenceSignal | None:
    """Compute the divergence signal for one market.

    Returns None if the market isn't recognised as a sports H2H / draw / O/U
    market we can compare. Returns a no_match signal when we recognise the
    market shape but cannot retrieve consensus data (no API key, no daily
    budget, no league match, no event match).
    """
    market_id = str(market.get("market_id") or "")
    question = market.get("question") or ""
    api_key = (api_key if api_key is not None else os.getenv("ODDS_API_KEY") or "").strip()
    if not api_key:
        return None  # cannot evaluate without key — signal is silent, not a no_match

    # 1. Detect market shape
    ou = _parse_ou_question(question)
    if ou:
        team_a, team_b, line = ou
        side = "ou"
    else:
        parsed = _parse_question(question)
        if parsed is None:
            return None
        team_a, team_b, side = parsed
        line = None

    # 2. Resolve league
    sport_key = _resolve_sport_key(market)
    if not sport_key:
        return _no_match(market_id, question, "", side, "no league keyword match")

    # 3. Daily budget guard (shared with TheOddsApiAdapter via the same table)
    from ..db import connect_db, adapter_budget_calls_today, adapter_budget_increment
    daily_cap = get_env_int("ODDS_DIVERGENCE_DAILY_CAP", DEFAULT_DAILY_CAP)
    with connect_db() as conn:
        used = adapter_budget_calls_today(conn, SIGNAL_NAME)
        if used >= daily_cap:
            return _no_match(market_id, question, sport_key, side,
                             f"daily cap {daily_cap} for {SIGNAL_NAME} hit; will retry tomorrow")

    # 4. Fetch event list for this sport
    sess = session or requests.Session()
    try:
        resp = sess.get(
            f"https://api.the-odds-api.com/v4/sports/{sport_key}/odds",
            params={
                "apiKey": api_key,
                "regions": "eu,uk",
                "markets": "h2h,totals" if line is None else "totals",
                "oddsFormat": "decimal",
            },
            timeout=25,
        )
        resp.raise_for_status()
        events = resp.json()
    except Exception as exc:
        return _no_match(market_id, question, sport_key, side, f"Odds API error: {exc}")
    with connect_db() as conn:
        adapter_budget_increment(conn, SIGNAL_NAME)
        conn.commit()

    # 5. Match event
    event = _match_event(events, market, team_a, team_b)
    if event is None:
        return _no_match(market_id, question, sport_key, side,
                         f"no event matched for {team_a} / {team_b} in {sport_key}")
    consensus = _average_consensus(event)
    book_count = consensus.get("book_count", 0)

    # 6. Pull Polymarket Yes price
    pm_p = _polymarket_yes_price(market)
    if pm_p is None:
        return _no_match(market_id, question, sport_key, side, "no Polymarket Yes price")

    # 7. Find the matching consensus probability for the side asked
    consensus_p = None
    reasoning_extra = ""
    if side == MarketSide.DRAW:
        consensus_p = (consensus.get("h2h") or {}).get("Draw")
        if consensus_p is None:
            return _no_match(market_id, question, sport_key, side, "h2h missing Draw key")
        reasoning_extra = f"h2h Draw"
    elif side == MarketSide.HOME_WIN:
        # Resolve which side of the event team_a is on, then pick that name's prob
        h2h = consensus.get("h2h") or {}
        home_name = (event.get("home_team") or "")
        away_name = (event.get("away_team") or "")
        na = set(normalize_team(team_a).split())
        if na and na.issubset(set(normalize_team(home_name).split())):
            consensus_p = h2h.get(home_name)
            reasoning_extra = f"h2h home={home_name}"
        elif na and (na.issubset(set(normalize_team(away_name).split()))
                     or set(normalize_team(away_name).split()).issubset(na)):
            consensus_p = h2h.get(away_name)
            reasoning_extra = f"h2h away={away_name}"
        if consensus_p is None:
            return _no_match(market_id, question, sport_key, side,
                             f"could not map {team_a} to h2h outcome ({home_name}/{away_name})")
    elif side == "ou":
        # Polymarket O/U: outcomes "Over"/"Under". Pull consensus totals[line]
        line_str = f"{line:.1f}" if isinstance(line, float) else str(line)
        # Odds API encodes lines as floats — try a few stringifications
        totals = consensus.get("totals") or {}
        bucket = totals.get(line_str) or totals.get(str(int(line))) or totals.get(str(line))
        if not bucket:
            # Try fuzzy match across keys
            for k, v in totals.items():
                try:
                    if abs(float(k) - float(line)) < 0.01:
                        bucket = v
                        break
                except ValueError:
                    continue
        if not bucket:
            return _no_match(market_id, question, sport_key, side,
                             f"no totals line {line_str} in consensus")
        # Polymarket Yes = Over (per _polymarket_yes_price fallback)
        for k, v in bucket.items():
            if k.lower().startswith("over"):
                consensus_p = v
                reasoning_extra = f"totals {line_str} Over"
                break
        if consensus_p is None:
            return _no_match(market_id, question, sport_key, side,
                             f"no Over side in totals {line_str}")
    else:
        return _no_match(market_id, question, sport_key, side, f"unsupported side {side}")

    # 8. Compute divergence
    edge = consensus_p - pm_p  # +ve means consensus thinks more likely than PM → buy Yes
    recommendation = "bet" if abs(edge) >= edge_threshold else "skip"
    direction = "buy Yes" if edge > 0 else ("buy No" if edge < 0 else "no_edge")
    reasoning = (
        f"{reasoning_extra} consensus across {book_count} books = {consensus_p:.3f}; "
        f"Polymarket Yes = {pm_p:.3f}; edge = {edge:+.3f} ({direction})"
    )
    # For now we only emit "bet" verdicts for edge > 0 (Polymarket under-prices).
    # The reverse case (Polymarket over-prices) corresponds to buying No, which
    # the executor doesn't currently support for binary Yes/No markets — record
    # it as 'skip' until executor handles short side.
    if recommendation == "bet" and edge < 0:
        recommendation = "skip"
        reasoning += " (edge favors No-side; skipped — executor only buys Yes today)"
    return DivergenceSignal(
        market_id=market_id, market_question=question,
        outcome="Yes" if side != "ou" else "Over",
        recommendation=recommendation,
        polymarket_p=pm_p, consensus_p=consensus_p, edge=edge,
        book_count=book_count, sport_key=sport_key, side=side,
        reasoning=reasoning,
        raw_consensus={"h2h": consensus.get("h2h"), "totals": consensus.get("totals"),
                       "home": event.get("home_team"), "away": event.get("away_team")},
    )
