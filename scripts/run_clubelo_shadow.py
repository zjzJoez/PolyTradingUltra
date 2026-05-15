"""ClubElo shadow harness — Strategy D from the 2026-05-15 deep dive.

Two modes:
  --mode=live      : evaluate the signal against currently-active markets,
                     persist verdicts to signal_events, print a summary.
  --mode=backtest  : replay the signal against historical RESOLVED markets,
                     compare predictions to actual outcomes, print hit rate +
                     implied ROI vs market-implied baseline.

Never places orders. Pure shadow tracking + report.

Usage:
    .venv/bin/python scripts/run_clubelo_shadow.py --mode=live
    .venv/bin/python scripts/run_clubelo_shadow.py --mode=backtest --days=30
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, List

from polymarket_mvp.common import load_repo_env, utc_now_iso
from polymarket_mvp.db import connect_db, init_db
from polymarket_mvp.services.clubelo_signal import (
    EloSignal,
    DEFAULT_HOME_ADVANTAGE,
    signal_for_market,
    normalize_team,
    _parse_question,
)

load_repo_env()

SIGNAL_NAME = "clubelo"


def _row_to_market(row) -> Dict[str, Any]:
    """Convert a market_snapshots row to the dict shape signal_for_market expects."""
    m = dict(row)
    for field in ("market_json", "outcomes_json"):
        v = m.get(field)
        if isinstance(v, str):
            try:
                m[field] = json.loads(v)
            except Exception:
                pass
    if "outcomes" not in m and m.get("outcomes_json"):
        m["outcomes"] = m["outcomes_json"]
    elif "outcomes" not in m and isinstance(m.get("market_json"), dict):
        outs = m["market_json"].get("outcomes")
        if outs:
            m["outcomes"] = outs
    return m


def _persist(conn, signal: EloSignal, size_rec: float | None) -> None:
    conn.execute(
        """
        INSERT INTO signal_events
        (signal_name, market_id, outcome, recommendation, model_p, market_p, edge,
         size_recommendation_usdc, payload_json, generated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            SIGNAL_NAME,
            signal.market_id,
            signal.outcome,
            signal.recommendation,
            signal.model_p if signal.recommendation != "no_match" else None,
            signal.market_p if signal.recommendation != "no_match" else None,
            signal.edge if signal.recommendation != "no_match" else None,
            size_rec,
            json.dumps(signal.to_dict(), default=str, ensure_ascii=False),
            utc_now_iso(),
        ),
    )


def _kelly_size(edge: float, market_p: float, bankroll: float = 50.0,
                kelly_fraction: float = 0.25, max_per_trade: float = 2.0) -> float:
    """Quarter-Kelly sizing capped at $2/trade. Positive only for positive edge."""
    if edge <= 0 or market_p <= 0 or market_p >= 1:
        return 0.0
    # Kelly fraction f* = (bp - q) / b where b = (1-p)/p (decimal odds - 1),
    # p = our probability, q = 1-p. Simplified for binary contract bought at price p_market:
    # payout multiple = 1/market_p - 1
    b = (1.0 / market_p) - 1.0
    p = market_p + edge
    q = 1.0 - p
    if b <= 0:
        return 0.0
    f_full = (b * p - q) / b
    f = max(0.0, f_full * kelly_fraction)
    size = bankroll * f
    return min(size, max_per_trade)


def run_live(edge_threshold: float, home_advantage: float, verbose: bool) -> int:
    init_db()
    print(f"=== ClubElo shadow (LIVE), edge_threshold={edge_threshold}, home_adv={home_advantage} ===")
    rows: List = []
    with connect_db() as conn:
        rows = conn.execute(
            """
            SELECT * FROM market_snapshots
            WHERE active = 1 AND accepting_orders = 1
              AND closed = 0
            ORDER BY last_scanned_at DESC LIMIT 200
            """
        ).fetchall()
        results: List[EloSignal] = []
        bets: List[EloSignal] = []
        for row in rows:
            market = _row_to_market(row)
            signal = signal_for_market(
                market,
                edge_threshold=edge_threshold,
                home_advantage=home_advantage,
            )
            if signal is None:
                continue
            results.append(signal)
            size_rec = (
                _kelly_size(signal.edge, signal.market_p)
                if signal.recommendation == "bet"
                else None
            )
            _persist(conn, signal, size_rec)
            if signal.recommendation == "bet":
                bets.append(signal)
        conn.commit()
    print(f"\nScanned: {len(rows)} active markets")
    print(f"ELO-recognised markets: {len(results)}")
    print(f"  → no_match (team lookup failed): {sum(1 for s in results if s.recommendation == 'no_match')}")
    print(f"  → skip (edge < threshold):        {sum(1 for s in results if s.recommendation == 'skip')}")
    print(f"  → BET candidates:                 {len(bets)}")
    if bets:
        print(f"\n--- Bet candidates ---")
        for s in sorted(bets, key=lambda x: -x.edge)[:15]:
            print(f"  mkt={s.market_id} {s.team_a} vs {s.team_b} [{s.side}]")
            print(f"     model_p={s.model_p:.3f} market_p={s.market_p:.3f} edge={s.edge:+.3f}")
            print(f"     Elo: home={s.elo_home:.0f} away={s.elo_away:.0f}")
    if verbose:
        print(f"\n--- All recognised markets ---")
        for s in results:
            print(f"  [{s.recommendation:8s}] mkt={s.market_id} {s.team_a} vs {s.team_b} "
                  f"side={s.side} model={s.model_p:.3f} market={s.market_p:.3f} edge={s.edge:+.3f}")
    return 0


def run_backtest(edge_threshold: float, home_advantage: float, days: int, verbose: bool) -> int:
    """Replay ClubElo signal against historical resolved markets.

    For each resolved market in the last `days` days:
      1. Look up the latest snapshot we have for that market (its market_json,
         outcomes, prices at scan time — best proxy for "what we'd have seen")
      2. Compute signal_for_market
      3. Compare to actual resolved_outcome
      4. Tally hit rate + implied ROI vs market-implied baseline
    """
    init_db()
    print(f"=== ClubElo shadow (BACKTEST), last {days}d, edge_threshold={edge_threshold} ===")
    cutoff = f"-{days} days"
    with connect_db() as conn:
        rows = conn.execute(
            f"""
            SELECT ms.*, mr.resolved_outcome, mr.resolved_at
            FROM market_snapshots ms
            JOIN market_resolutions mr ON mr.market_id = ms.market_id
            WHERE mr.resolved_at > datetime('now', ?)
              AND mr.resolved_outcome IS NOT NULL
            """,
            (cutoff,),
        ).fetchall()
    print(f"Resolved markets in window: {len(rows)}")
    results: List[Dict[str, Any]] = []
    elo_recognised = 0
    elo_bet = 0
    correct = 0
    correct_bet = 0
    total_market_implied_correct = 0.0  # sum of market_p where outcome resolved Yes
    total_model_implied_correct = 0.0
    pnl_per_bet: List[float] = []
    for row in rows:
        market = _row_to_market(row)
        resolved_outcome = (row["resolved_outcome"] or "").strip()
        signal = signal_for_market(
            market,
            edge_threshold=edge_threshold,
            home_advantage=home_advantage,
        )
        if signal is None:
            continue
        elo_recognised += 1
        if signal.recommendation == "no_match":
            continue
        actual_yes = resolved_outcome.lower() == "yes"
        market_p = signal.market_p
        model_p = signal.model_p
        # Calibration accounting (independent of bet/skip)
        if actual_yes:
            total_market_implied_correct += market_p
            total_model_implied_correct += model_p
            correct += 1
        # Bet-only accounting
        if signal.recommendation == "bet":
            elo_bet += 1
            if actual_yes:
                correct_bet += 1
                # $1 stake at price market_p resolves to $1/market_p payout
                pnl_per_bet.append(1.0 / market_p - 1.0)
            else:
                pnl_per_bet.append(-1.0)
        results.append({
            "market_id": signal.market_id,
            "team_a": signal.team_a,
            "team_b": signal.team_b,
            "side": signal.side,
            "model_p": model_p,
            "market_p": market_p,
            "edge": signal.edge,
            "rec": signal.recommendation,
            "actual_yes": actual_yes,
        })
    n = len(results)
    print(f"\nELO-evaluable: {elo_recognised}")
    print(f"  with usable comparison: {n}")
    if n == 0:
        print("(no evaluable markets — too few resolutions in window or no team-name matches)")
        return 0

    # Overall calibration: did the model's prob beat market's prob at predicting Yes outcomes?
    avg_market_p = sum(r["market_p"] for r in results) / n
    avg_model_p = sum(r["model_p"] for r in results) / n
    yes_rate = sum(1 for r in results if r["actual_yes"]) / n

    print(f"\nCalibration over all {n} ELO-recognised resolved markets:")
    print(f"  actual Yes rate:        {yes_rate:.3f}")
    print(f"  avg market_p (implied): {avg_market_p:.3f}")
    print(f"  avg model_p (ours):     {avg_model_p:.3f}")
    print(f"  → market vs reality:    {avg_market_p - yes_rate:+.3f}pp")
    print(f"  → model  vs reality:    {avg_model_p - yes_rate:+.3f}pp")
    print(f"  → model vs market:      {avg_model_p - avg_market_p:+.3f}pp  (closer to 0 = better calibrated)")

    # Brier scores
    brier_market = sum((r["market_p"] - (1.0 if r["actual_yes"] else 0.0)) ** 2 for r in results) / n
    brier_model = sum((r["model_p"] - (1.0 if r["actual_yes"] else 0.0)) ** 2 for r in results) / n
    print(f"  Brier score market:     {brier_market:.4f}")
    print(f"  Brier score model:      {brier_model:.4f}  (lower = better; ours should beat market to bet)")

    # Bet-only accounting
    print(f"\nIf we'd actually bet (recommendation='bet'): n={elo_bet}")
    if elo_bet > 0:
        bet_hit_rate = correct_bet / elo_bet
        avg_pnl = sum(pnl_per_bet) / len(pnl_per_bet) if pnl_per_bet else 0.0
        total_pnl = sum(pnl_per_bet)
        print(f"  bet hit rate:           {correct_bet}/{elo_bet} = {bet_hit_rate:.1%}")
        print(f"  avg PnL per $1 stake:   {avg_pnl:+.3f}")
        print(f"  total PnL ($1/bet):     {total_pnl:+.2f}")
        # ROI = total_pnl / total_stakes
        roi = total_pnl / elo_bet
        print(f"  ROI per bet:            {roi:+.1%}")

    # By side
    print(f"\nBy market side:")
    by_side: Dict[str, List] = defaultdict(list)
    for r in results:
        by_side[r["side"]].append(r)
    for side, items in by_side.items():
        n_side = len(items)
        n_yes = sum(1 for r in items if r["actual_yes"])
        avg_mp = sum(r["market_p"] for r in items) / n_side
        avg_md = sum(r["model_p"] for r in items) / n_side
        bets = [r for r in items if r["rec"] == "bet"]
        bet_yes = sum(1 for r in bets if r["actual_yes"])
        print(f"  {side:10s}: n={n_side}  actual_yes={n_yes}/{n_side} ({n_yes/n_side:.1%})  "
              f"avg_market={avg_mp:.3f} avg_model={avg_md:.3f}  bets={len(bets)} bet_wins={bet_yes}")

    if verbose:
        print(f"\n--- per-market detail ---")
        for r in sorted(results, key=lambda x: -x["edge"])[:30]:
            print(f"  mkt={r['market_id']:8s} {r['team_a']:25s} vs {r['team_b']:25s}  "
                  f"[{r['side']:10s}] model={r['model_p']:.3f} market={r['market_p']:.3f} "
                  f"edge={r['edge']:+.3f} rec={r['rec']:5s} → actual={'YES' if r['actual_yes'] else 'NO'}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["live", "backtest"], required=True)
    parser.add_argument("--edge-threshold", type=float, default=0.05,
                        help="Bet when model_p - market_p >= this (default 0.05 = 5pp)")
    parser.add_argument("--home-advantage", type=float, default=DEFAULT_HOME_ADVANTAGE,
                        help=f"ELO home-advantage points (default {DEFAULT_HOME_ADVANTAGE})")
    parser.add_argument("--days", type=int, default=30,
                        help="Backtest window in days (mode=backtest only)")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    if args.mode == "live":
        return run_live(args.edge_threshold, args.home_advantage, args.verbose)
    return run_backtest(args.edge_threshold, args.home_advantage, args.days, args.verbose)


if __name__ == "__main__":
    sys.exit(main())
