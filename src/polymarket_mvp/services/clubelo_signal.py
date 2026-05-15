"""ClubElo-driven signal for Polymarket sports markets.

Strategy D from the 2026-05-15 deep-dive: use ClubElo's free, no-auth ELO
ratings to compute a market-derived probability for "Will X win?" /
"Will X vs Y end in a draw?" markets, then bet when our Elo-implied
probability exceeds Polymarket's offered price by a configurable threshold.

History-data observation from PolyTradingUltra's own DB: in the <1.7×
asymmetric-ceiling band (market price > 0.37 — i.e., favorites), the bot
historically hit 14/25 = 56% resolutions. ClubElo lets us replicate that
"bet favorites with strong rating gap" intuition WITHOUT the LLM judgement
that has been hitting 11% in the long-tail band.

API:
  http://api.clubelo.com/{YYYY-MM-DD}   → CSV: Rank,Club,Country,Level,Elo,From,To
  http://api.clubelo.com/{ClubName}     → CSV history (we don't use this path)

Free, no key, no rate limit documented. We cache the daily snapshot on disk
to avoid hammering for every market.
"""
from __future__ import annotations

import csv
import io
import os
import re
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Tuple

import requests

# Snapshot cache lives next to the football-data team-index cache so it
# follows the same POLYMARKET_MVP_STATE_DIR convention.
_CACHE_TTL_SEC = 6 * 3600  # 6h — Elo only updates after matches, daily is fine
_CACHE_FILENAME = "clubelo_snapshot.csv"


def _cache_path() -> Path:
    base = (
        os.getenv("POLYMARKET_MVP_STATE_DIR")
        or str(Path.home() / ".cache" / "polymarket-mvp")
    )
    return Path(base) / _CACHE_FILENAME


def _fetch_today_snapshot(session: requests.Session | None = None) -> str:
    """Fetch ClubElo's full ratings snapshot for today (UTC). Returns raw CSV."""
    sess = session or requests.Session()
    today = datetime.now(timezone.utc).date().isoformat()
    url = f"http://api.clubelo.com/{today}"
    resp = sess.get(url, timeout=15)
    resp.raise_for_status()
    return resp.text


def _load_snapshot(force_refresh: bool = False, session: requests.Session | None = None) -> List[Dict[str, Any]]:
    """Return today's club ratings as a list of dicts. Cached on disk for _CACHE_TTL_SEC."""
    path = _cache_path()
    use_cache = (
        not force_refresh
        and path.exists()
        and path.stat().st_mtime > time.time() - _CACHE_TTL_SEC
    )
    if use_cache:
        text = path.read_text(encoding="utf-8")
    else:
        text = _fetch_today_snapshot(session=session)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        except OSError:
            pass
    rows: List[Dict[str, Any]] = []
    reader = csv.DictReader(io.StringIO(text))
    for raw in reader:
        try:
            elo = float(raw.get("Elo") or 0)
        except (TypeError, ValueError):
            elo = 0.0
        if elo <= 0:
            continue
        rows.append(
            {
                "club": (raw.get("Club") or "").strip(),
                "country": (raw.get("Country") or "").strip(),
                "level": (raw.get("Level") or "").strip(),
                "elo": elo,
            }
        )
    return rows


# ────────────────────────── Team-name normalization ──────────────────────────
# Strip accents + ASCII-lowercase + remove club boilerplate. Matches the
# convention used in services.sports_data._normalize, so a name normalised
# here can be cross-referenced with the football-data adapter later.
_BOILERPLATE = re.compile(
    r"\b(?:fc|cf|sc|ac|as|sv|sk|tsg|vfl|vfb|ff|cd|club|football|the|de|del|da|do|dos|das|cp)\b",
    re.IGNORECASE,
)


def normalize_team(name: str) -> str:
    s = unicodedata.normalize("NFKD", name or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower()
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    s = _BOILERPLATE.sub(" ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _build_normalized_index(rows: Iterable[Mapping[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Map normalized_club_name → record. Ambiguous keys (two clubs collide
    after normalization) are mapped to a sentinel `{"_ambiguous": True}` so
    we don't silently confuse "Real Madrid" with "Real Sociedad" etc."""
    index: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        key = normalize_team(row["club"])
        if not key:
            continue
        if key in index:
            existing = index[key]
            if existing.get("_ambiguous"):
                continue
            if existing["club"] != row["club"]:
                index[key] = {"_ambiguous": True, "candidates": [existing["club"], row["club"]]}
        else:
            index[key] = dict(row)
    return index


# Known Polymarket ↔ ClubElo aliases that token-substring matching can't cover
# (e.g. "Paris Saint-Germain" vs "Paris SG", "Manchester City" vs "Man City").
# Keys must be normalize_team output of the Polymarket form.
ALIAS_OVERRIDES: Dict[str, str] = {
    "paris saint germain": "paris sg",
    "manchester city": "man city",
    "manchester united": "man united",
    "manchester utd": "man united",
    "newcastle united": "newcastle",
    "wolverhampton wanderers": "wolves",
    "tottenham hotspur": "tottenham",
    "real betis balompie": "betis",
    "racing lens": "lens",
    "stade rennais": "rennes",
    "olympique lyonnais": "lyon",
    "olympique marseille": "marseille",
    "borussia dortmund": "dortmund",
    "borussia monchengladbach": "gladbach",
    "bayern munich": "bayern",
    "bayern munchen": "bayern",
    "internazionale": "inter",
    "inter milan": "inter",
    "ac milan": "milan",  # "ac" already stripped by boilerplate, but redundant alias is harmless
    "atletico madrid": "atletico",
    "real sociedad": "sociedad",
}


def lookup_elo(team_name: str, *, force_refresh: bool = False, session: requests.Session | None = None) -> float | None:
    """Look up a team's current ELO rating. Returns None if not found or ambiguous.

    Strategy (each only fires if the previous misses):
      1. Direct hit on normalized name (ClubElo "Barcelona" → "barcelona")
      2. Explicit alias override (PolyMarket "Paris Saint-Germain" → "paris sg")
      3. Token-subset substring match — "deportivo alaves" includes club tokens
         "alaves" so we accept the ClubElo "Alaves" entry. We only accept the
         match when EXACTLY ONE candidate qualifies, to avoid silently confusing
         "real madrid" with "real sociedad".
    """
    rows = _load_snapshot(force_refresh=force_refresh, session=session)
    index = _build_normalized_index(rows)
    key = normalize_team(team_name)
    if not key:
        return None
    # 1. Direct hit
    hit = index.get(key)
    if hit is not None and not hit.get("_ambiguous"):
        return float(hit["elo"])
    # 2. Alias override
    alias = ALIAS_OVERRIDES.get(key)
    if alias:
        hit = index.get(alias)
        if hit is not None and not hit.get("_ambiguous"):
            return float(hit["elo"])
    # 3. Token-subset substring match
    my_tokens = set(key.split())
    if not my_tokens:
        return None
    candidates: List[Dict[str, Any]] = []
    for club_key, club_row in index.items():
        if club_row.get("_ambiguous"):
            continue
        club_tokens = set(club_key.split())
        if not club_tokens:
            continue
        # club fully contained in query (e.g. "alaves" ⊂ "deportivo alaves"), or
        # query fully contained in club (e.g. "city" ⊂ "manchester city" — too loose, skip)
        if club_tokens.issubset(my_tokens) and len(club_tokens) >= 1:
            candidates.append(club_row)
    if len(candidates) == 1:
        return float(candidates[0]["elo"])
    return None


# ────────────────────────── Match probability model ──────────────────────────
# Standard FIFA-style ELO logistic + an empirical draw split. League draw
# rates are ~25-30% globally; we use 26% as the unconditional prior and let
# Elo gap shift it (closer matches → more draws).
DEFAULT_HOME_ADVANTAGE = 65.0  # ELO points; calibrated to ~5pp lift for home team
# Draw-rate parameters re-calibrated 2026-05-15 after first backtest
# (n=13 Polymarket draw markets resolved over 30d). Actual draw rate 23.1%;
# original parameters (slope 0.0005, base 0.26) gave model predictions
# averaging 16.2% — systematically under-estimating draws. Empirically
# European football draws sit at 25-28% even at large Elo gaps. New
# parameters keep draw between 17% (huge favorites) and 28% (equal teams).
DRAW_BASE_RATE = 0.28
DRAW_GAP_SLOPE = 0.00015       # draw rate falls as |Elo gap| grows (much shallower)
DRAW_MIN = 0.16
DRAW_MAX = 0.30


def _logistic_p_home_vs_away_only(elo_home_eff: float, elo_away: float) -> float:
    """Binary 'home wins given there's a winner'. Standard ELO logistic on (home+adv)-away."""
    diff = elo_home_eff - elo_away
    return 1.0 / (1.0 + 10 ** (-diff / 400.0))


def compute_three_way_probs(
    elo_home: float,
    elo_away: float,
    *,
    home_advantage: float = DEFAULT_HOME_ADVANTAGE,
) -> Dict[str, float]:
    """Return {'home', 'draw', 'away'} probabilities. Sums to ~1.0.

    Approach:
      1. Compute binary P(home wins | someone wins) via ELO logistic on
         (home + home_adv) vs away.
      2. Compute P(draw) from the absolute ELO gap — bigger gaps reduce draws.
      3. Distribute the remaining (1 - P_draw) into home/away by the binary.
    """
    elo_home_eff = elo_home + home_advantage
    binary_home = _logistic_p_home_vs_away_only(elo_home_eff, elo_away)
    gap = abs(elo_home_eff - elo_away)
    p_draw = DRAW_BASE_RATE - DRAW_GAP_SLOPE * gap
    p_draw = max(DRAW_MIN, min(DRAW_MAX, p_draw))
    remaining = 1.0 - p_draw
    p_home = remaining * binary_home
    p_away = remaining * (1.0 - binary_home)
    return {"home": p_home, "draw": p_draw, "away": p_away}


# ────────────────────────── Market interpretation ──────────────────────────
class MarketSide:
    """Which side of the binary the Polymarket question is asking about."""
    HOME_WIN = "home_win"
    AWAY_WIN = "away_win"
    DRAW = "draw"
    UNKNOWN = "unknown"


def _parse_question(question: str) -> Tuple[str, str | None, str] | None:
    """Return (team_a, team_b, side) where side ∈ {HOME_WIN, AWAY_WIN, DRAW}.

    For single-team win questions like "Will Real Madrid win on 2026-05-15?"
    team_b is returned as None — the caller is expected to look up the opponent
    via market_event_links / event_clusters / other markets in the same fixture.
    """
    if not question:
        return None
    q = question.strip()
    lq = q.lower()
    # "Will A vs B end in a draw?"
    m = re.match(r"^will\s+(.+?)\s+vs\.?\s+(.+?)\s+end\s+in\s+a\s+draw", lq)
    if m:
        return (m.group(1), m.group(2), MarketSide.DRAW)
    # "Will A win on YYYY-MM-DD?" — single-team. Opponent looked up via fixture.
    m = re.match(r"^will\s+(.+?)\s+win\s+on\s+\d{4}-\d{2}-\d{2}", lq)
    if m:
        return (m.group(1), None, MarketSide.HOME_WIN)
    # "Will A beat B?" / "Will A defeat B?"
    m = re.match(r"^will\s+(.+?)\s+(?:beat|defeat|outscore)\s+(.+?)\b", lq)
    if m:
        return (m.group(1), m.group(2), MarketSide.HOME_WIN)
    return None


def _find_opponent_via_cluster(market: Mapping[str, Any], known_team_normalized: str) -> str | None:
    """For 'Will A win on date?' markets, find the opponent by looking up
    other markets in the same event_cluster that have both teams in the
    question (typically the "Will A vs B end in a draw?" market for the
    same fixture). Returns the opponent's name (raw, not normalized)."""
    cluster_id = market.get("event_cluster_id")
    if not cluster_id:
        return None
    try:
        from ..db import connect_db
    except Exception:
        return None
    try:
        with connect_db() as conn:
            rows = conn.execute(
                """
                SELECT DISTINCT ms.question
                FROM market_snapshots ms
                JOIN market_event_links mel ON mel.market_id = ms.market_id
                WHERE mel.event_cluster_id = ?
                  AND ms.market_id != ?
                  AND ms.question LIKE '%vs%'
                LIMIT 10
                """,
                (int(cluster_id), str(market.get("market_id") or "")),
            ).fetchall()
    except Exception:
        return None
    for row in rows:
        q = (row["question"] or "").strip()
        parsed = _parse_question(q)
        if parsed is None:
            continue
        a, b, _ = parsed
        if not a or not b:
            continue
        na, nb = normalize_team(a), normalize_team(b)
        if known_team_normalized in (na, nb):
            return b if known_team_normalized == na else a
    return None


# ────────────────────────── Public signal API ──────────────────────────
class EloSignal:
    """A Polymarket-market verdict from the ELO model."""

    def __init__(self, market_id: str, outcome: str, recommendation: str,
                 model_p: float, market_p: float, edge: float,
                 elo_home: float, elo_away: float, side_from_question: str,
                 raw_probs: Dict[str, float], team_a: str, team_b: str,
                 reasoning: str):
        self.market_id = market_id
        self.outcome = outcome              # which Polymarket outcome to bet ("Yes" / "No")
        self.recommendation = recommendation  # "bet" | "skip" | "no_match"
        self.model_p = model_p
        self.market_p = market_p
        self.edge = edge                    # model_p - market_p
        self.elo_home = elo_home
        self.elo_away = elo_away
        self.side = side_from_question
        self.raw_probs = raw_probs
        self.team_a = team_a
        self.team_b = team_b
        self.reasoning = reasoning

    def to_dict(self) -> Dict[str, Any]:
        return {
            "market_id": self.market_id,
            "outcome": self.outcome,
            "recommendation": self.recommendation,
            "model_p": self.model_p,
            "market_p": self.market_p,
            "edge": self.edge,
            "elo_home": self.elo_home,
            "elo_away": self.elo_away,
            "side": self.side,
            "raw_probs": self.raw_probs,
            "team_a": self.team_a,
            "team_b": self.team_b,
            "reasoning": self.reasoning,
        }


def signal_for_market(
    market: Mapping[str, Any],
    *,
    edge_threshold: float = 0.05,
    home_advantage: float = DEFAULT_HOME_ADVANTAGE,
    session: requests.Session | None = None,
) -> EloSignal | None:
    """Compute the ELO signal's verdict on a Polymarket market.

    Returns None if the market is not a recognized 'Will A vs B draw?' or
    'Will A beat B?' question, or if ELO data for one of the teams is missing.
    """
    parsed = _parse_question(market.get("question") or "")
    if parsed is None:
        return None
    team_a, team_b, side = parsed
    # If single-team-win market (team_b is None), look up opponent via
    # event_cluster sibling markets. Falls back gracefully if cluster
    # membership isn't recorded.
    if team_b is None:
        team_b = _find_opponent_via_cluster(market, normalize_team(team_a))
        if team_b is None:
            return EloSignal(
                market_id=str(market.get("market_id") or ""),
                outcome="?",
                recommendation="no_match",
                model_p=0.0,
                market_p=0.0,
                edge=0.0,
                elo_home=0.0,
                elo_away=0.0,
                side_from_question=side,
                raw_probs={},
                team_a=team_a,
                team_b="",
                reasoning=f"single-team-win market but no opponent found in event_cluster (mkt={market.get('market_id')})",
            )
    elo_home = lookup_elo(team_a, session=session)
    elo_away = lookup_elo(team_b, session=session)
    if elo_home is None or elo_away is None:
        return EloSignal(
            market_id=str(market.get("market_id") or ""),
            outcome="?",
            recommendation="no_match",
            model_p=0.0,
            market_p=0.0,
            edge=0.0,
            elo_home=elo_home or 0.0,
            elo_away=elo_away or 0.0,
            side_from_question=side,
            raw_probs={},
            team_a=team_a,
            team_b=team_b,
            reasoning=f"ELO lookup failed: {team_a}={elo_home} {team_b}={elo_away}",
        )
    probs = compute_three_way_probs(elo_home, elo_away, home_advantage=home_advantage)
    if side == MarketSide.HOME_WIN:
        model_p = probs["home"]
        target_outcome = "Yes"
    elif side == MarketSide.AWAY_WIN:
        model_p = probs["away"]
        target_outcome = "Yes"
    elif side == MarketSide.DRAW:
        model_p = probs["draw"]
        target_outcome = "Yes"
    else:
        model_p = 0.0
        target_outcome = "Yes"
    # Pull the current Yes-side market price from the outcomes list.
    market_p = None
    for o in (market.get("outcomes") or []):
        if (o.get("name") or "").strip().lower() == target_outcome.lower():
            try:
                market_p = float(o.get("price") or 0)
            except (TypeError, ValueError):
                market_p = None
            break
    if market_p is None:
        return None
    edge = model_p - market_p
    if edge >= edge_threshold:
        recommendation = "bet"
    else:
        recommendation = "skip"
    reasoning = (
        f"ELO({team_a})={elo_home:.0f} vs ELO({team_b})={elo_away:.0f} "
        f"(home_adv={home_advantage:.0f}) → P({side})={model_p:.3f}; "
        f"market={market_p:.3f}; edge={edge:+.3f}"
    )
    return EloSignal(
        market_id=str(market.get("market_id") or ""),
        outcome=target_outcome,
        recommendation=recommendation,
        model_p=model_p,
        market_p=market_p,
        edge=edge,
        elo_home=elo_home,
        elo_away=elo_away,
        side_from_question=side,
        raw_probs=probs,
        team_a=team_a,
        team_b=team_b,
        reasoning=reasoning,
    )
