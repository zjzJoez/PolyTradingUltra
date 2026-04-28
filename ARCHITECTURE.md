# PolyTradingUltra — System Architecture

> Last updated: 2026-04-28  
> Status: Phase 2 (GPT-5.5 + real-time sports data, in development)

---

## 1. What This System Does

PolyTradingUltra is a fully automated prediction market trading bot for Polymarket.
It continuously scans markets, uses an LLM to identify mispriced outcomes, sizes positions
via a conviction-tier ladder, executes trades, and manages exits — all without human intervention.

**Account**: ~$52 USDC on Polymarket (Polygon chain)  
**Target horizon**: 3-week sprint window, 1.5× floor / 10× moonshot  
**Risk posture**: long-tail lottery (speculative to extreme tiers, $2–$15 per trade)

---

## 2. Full Pipeline (Market → Trade → Exit)

```
Every 10 minutes (systemd timer on EC2)
│
├─ [SCAN]      poly_scanner.py
│              Hits Gamma API, pulls ~200 active markets
│              Filter: liquidity ≥ $10k, days_to_expiry ≤ 7, accepting_orders
│              Stores to: market_snapshots table
│
├─ [CONTEXT]   event_fetcher.py
│              For top-25 scanned markets, fetches context from:
│              ├── SportsDataAdapter    → football-data.org (team form, last 5 matches) [NEW]
│              ├── PerplexityAdapter    → AI-generated news summary (if key set)
│              ├── CryptoPanicAdapter   → crypto news (if key set)
│              └── WebSearchAdapter     → DuckDuckGo fallback
│              Stores to: market_contexts table
│
├─ [PROPOSE]   proposer.py + agents/poly_proposer.py
│              ├── select_llm_candidates(): score + rank markets
│              │   Scoring: sqrt(liquidity + 0.5×volume) × price_zone_multiplier × category_multiplier
│              │   Category multipliers: politics 1.8×, tech 1.5×, other 1.4×, sports 0.8×, crypto 0.0×
│              │   Sports un-penalised (×2.0) when football-data.org context is available
│              │
│              ├── LLM call: GPT-5.5 via Codex CLI (OAuth subscription) [NEW]
│              │   System prompt: FALLBACK_SYSTEM_PROMPT + _MACHINE_CONTRACT
│              │   User payload: market question + outcomes/prices + context assembled_text
│              │
│              ├── LLM outputs per proposal:
│              │   - confidence_score: float [0,1] — LLM's P(outcome)
│              │   - resolution_clarity: objective | subjective | ambiguous
│              │   - catalyst_clarity: none | weak | moderate | strong
│              │   - downside_risk: limited | moderate | substantial
│              │   - asymmetric_target_multiplier: float (expected payoff ratio)
│              │   - thesis_catalyst_deadline: ISO date or null
│              │
│              └── Conviction-tier sizing: conviction.py (deterministic, no LLM)
│                  edge = |confidence_score − market_price|
│                  ambiguous resolution_clarity → skip
│                  edge < 0.05  → skip
│                  edge 0.05–0.10 → speculative ($2)
│                  edge 0.10–0.12 + moderate/strong catalyst → medium ($4)
│                  edge 0.12–0.20 + any catalyst ≠ none → medium ($4)
│                  edge 0.20–0.30 + strong catalyst → high ($8)
│                  edge ≥ 0.30 + strong + not substantial risk → extreme ($15)
│                  subjective resolution_clarity → downgrade one tier
│                  liquidity < $20k → downgrade one tier
│                  CLOB top-5 depth check → downgrade if size > 25% of book
│                  Account scale: $50 → 1×, $100 → 2×, $200 → 4× (sizes double per doubling)
│
├─ [RISK]      risk_engine.py + portfolio_risk_service.py
│              Single-proposal checks (skip for exit proposals):
│              ├── market_not_tradeable
│              ├── market_class_disabled (crypto blocked)
│              ├── size_above_risk_limit (bypassed for conviction-tier entries)
│              ├── confidence_below_threshold (bypassed for conviction-tier entries)
│              ├── slippage_above_risk_limit
│              ├── insufficient_balance (uses configured_balance in shadow mode)
│              ├── selected_outcome_has_no_live_price
│              ├── shares_below_polymarket_minimum (5 shares)
│              ├── selected_outcome_price_outside_tradable_band [0.10, 0.80]
│              └── gamma_clob_price_divergence_exceeded (> 500 bps)
│
│              Portfolio checks:
│              ├── drawdown_breaker (disabled: POLY_MAX_DRAWDOWN_USDC=999999999)
│              ├── market_outcome_exposure_exists (no double-entry same market/outcome)
│              ├── topic/cluster exposure limits
│              ├── portfolio_open_exposure_limit (70% of balance)
│              └── tier_concurrent_cap (extreme≤2, high≤3, medium≤4, speculative≤5)
│
├─ [APPROVE]   Shadow mode: auto-approve → authorized_for_execution
│              Real mode: Telegram notification → human approval or auto-execute rule
│
├─ [EXECUTE]   poly_executor.py
│              Shadow: record to shadow_executions table (no real trade)
│              Real: CLOB API order submission (Polymarket Polygon)
│              Auto-redeemer: web3 on-chain redemption of resolved winning tokens
│
└─ [EXIT]      agents/exit_agent.py (every 30s)
               Deterministic triggers:
               ├── Market resolved → close 100%
               ├── Take-profit: mark ≥ entry × target_multiplier × 0.63 → reduce 50%
               │   (0.63 = 0.7 time-decay × 0.9 slippage buffer)
               ├── Catalyst deadline passed → LLM reassessment
               └── Market expiry < 30 min → close 100%
```

---

## 3. Conviction Tier Ladder

| Tier | Size ($50 acc) | Size ($100 acc) | Edge Required | Catalyst Required |
|---|---|---|---|---|
| speculative | $2 | $4 | 0.05–0.10 | any |
| medium | $4 | $8 | 0.10–0.20 | weak+ |
| high | $8 | $16 | 0.20–0.30 | strong |
| extreme | $15 | $30 | ≥ 0.30 | strong + limited downside |

Modifiers that **downgrade one tier**:
- `resolution_clarity = subjective`
- `liquidity_usdc < $20,000`

Modifiers that **skip entirely**:
- `resolution_clarity = ambiguous`
- `edge < 0.05`
- CLOB depth check fails at speculative level

---

## 4. Market Category Scoring

Markets are classified by `event_cluster_service.classify_market_class()` and weighted in `_market_llm_score()`:

| Category | Keywords | Score Multiplier | Risk Limit |
|---|---|---|---|
| politics | election, president, war, sanction, nato… | **1.8×** | $15/trade, 8 positions |
| tech | ai, openai, nvidia, ipo, merger… | **1.5×** | $10/trade, 6 positions |
| other | everything else | 1.4× | $5/trade, per config |
| esports | league of legends, dota, valorant… | 1.0× | $5/trade |
| sports_winner | nba, nfl, epl, champions league… | 0.8× (1.4× with live data) | $10/trade |
| sports_totals | over/under… | 0.7× (1.4× with live data) | $5/trade |
| crypto_up_down | BTC/ETH/SOL up/down | **0.0× (blocked)** | disabled |

---

## 5. LLM Configuration

**Current (Phase 2)**:
- Model: **GPT-5.5** via Codex CLI (OAuth subscription, not API-key billing)
- Transport: `OPENCLAW_TRANSPORT=codex_cli`
- Cadence: ≤ 1 LLM call per 10 minutes (`POLY_LLM_MIN_INTERVAL_SECONDS=600`)
- Rate-limit fallback: exponential backoff 30min → 1hr → 6hr

**Previous (Phase 1)**:
- Model: Claude Sonnet 4.6 via Claude CLI
- Deprecated but available as fallback: `OPENCLAW_TRANSPORT=cli`

**Prompt structure**:
```
[FALLBACK_SYSTEM_PROMPT or workspace IDENTITY.md + SOUL.md]
+
[_MACHINE_CONTRACT]  ← trading heuristics, conviction fields schema, output contract
+
[market payload JSON]  ← question, outcomes, prices, context assembled_text
```

---

## 6. Real-time Sports Data (Phase 2)

**Source**: football-data.org (API key: `FOOTBALL_DATA_API_KEY`)  
**Module**: `src/polymarket_mvp/services/sports_data.py`

Data fetched per sports market:
- Team name extraction from market question
- Last 5 match results per team (W/D/L + score)
- Format injected into LLM context:
  ```
  RECENT FORM (Team A, last 5): W 2-1, L 0-3, W 1-0, D 1-1, W 3-0 [3W 1D 1L]
  RECENT FORM (Team B, last 5): L 0-2, L 1-2, W 2-0, L 0-1, D 0-0 [1W 1D 3L]
  ```

Adapter: `SportsDataAdapter` in `event_fetcher.py`  
Priority: highest in context assembly (appears first in assembled_text, importance_weight=1.2)  
Failure handling: fully silent (returns None on any API error, main flow continues)

---

## 7. Key Environment Variables (EC2 `.env`)

```bash
# LLM
OPENCLAW_TRANSPORT=codex_cli         # codex_cli | cli (claude) | http (openai api)
CODEX_MODEL=gpt-5.5
POLY_LLM_MIN_INTERVAL_SECONDS=600    # max 1 LLM call per 10 min

# Account
POLYMARKET_AVAILABLE_BALANCE_U=49    # configured balance (used in shadow mode)
POLY_ACCOUNT_BALANCE_USDC=50         # for conviction tier scaling
POLY_RISK_MAX_ORDER_USDC=5           # legacy size gate (bypassed for conviction tiers)
POLY_MAX_TRADABLE_PRICE=0.80         # reject near-certain outcomes
POLY_MIN_TRADABLE_PRICE=0.10

# Shadow / Real
MVP_SHADOW_MODE=1                    # 1=shadow, 0=real money

# Sports data
FOOTBALL_DATA_API_KEY=xxx

# Context providers (optional)
PERPLEXITY_API_KEY=xxx
CRYPTOPANIC_AUTH_TOKEN=xxx
```

---

## 8. Database Tables (Key)

| Table | Purpose |
|---|---|
| `market_snapshots` | Scanner output — one row per market, updated each scan |
| `market_contexts` | Context from news/sports APIs — multiple rows per market |
| `proposals` | LLM proposals with conviction fields, risk decisions |
| `executions` | Real and shadow order records |
| `shadow_executions` | Shadow-mode fills with theoretical PnL tracking |
| `positions` | Open/resolved real positions |
| `market_resolutions` | Resolved market outcomes (feeds exit agent + PnL) |
| `llm_rate_limit_events` | Rate-limit hit log for backoff state |
| `heartbeats` | One row per loop tick for monitoring |

---

## 9. EC2 Deployment

**Host**: AWS EC2 Ireland (`polytrade` SSH alias)  
**Repo**: `/home/ubuntu/polymarket-mvp/`  
**Service**: `mvp-autopilot.service` (systemd, triggered by `mvp-autopilot.timer`)  
**Cadence**: 10 minutes (`/etc/systemd/system/mvp-autopilot.timer.d/cadence.conf`)  
**Shadow drop-in**: `/etc/systemd/system/mvp-autopilot.service.d/shadow-mode.conf`

**Deploy cycle**:
```bash
# Local
git push

# EC2
ssh polytrade 'cd /home/ubuntu/polymarket-mvp && git pull && sudo systemctl restart mvp-autopilot.service'
```

**Switch to real money**:
```bash
ssh polytrade '
  sudo rm /etc/systemd/system/mvp-autopilot.service.d/shadow-mode.conf
  sudo systemctl daemon-reload
  sed -i "s/MVP_SHADOW_MODE=1/MVP_SHADOW_MODE=0/" .env
  sudo systemctl restart mvp-autopilot.timer
'
```

---

## 10. Phase Roadmap

| Phase | Status | Content |
|---|---|---|
| Phase 0 | ✅ Done | Claude CLI transport, heuristic proposer |
| Phase 1 | ✅ Done | Conviction-tier sizing (replaces Kelly), LLM rate-limit backoff, CLOB slippage cap, portfolio limits, shadow mode |
| Phase 2 | 🔄 In progress | GPT-5.5 via Codex CLI, football-data.org sports context, category scoring rebalance, resolution_clarity field |
| Phase 3 | Planned | Switch to real money after shadow gate; World Cup specialization |
| Phase 4 | Backlog | Exit agent LLM escalation, multi-outcome relative value, longer-horizon political markets |

---

## 11. Monitoring

**Quick health check**:
```bash
ssh polytrade 'journalctl -u mvp-autopilot.service -n 20 --no-pager'
```

**Shadow PnL check**:
```sql
SELECT COUNT(*) resolved,
  SUM(CASE WHEN mr.resolved_outcome=p.outcome THEN 1 ELSE 0 END) wins,
  ROUND(SUM(CASE
    WHEN mr.resolved_outcome=p.outcome THEN (1.0/se.simulated_fill_price-1)*se.simulated_notional
    WHEN mr.resolved_outcome!=p.outcome THEN -se.simulated_notional
    ELSE 0 END),2) pnl
FROM shadow_executions se
JOIN proposals p ON p.proposal_id=se.proposal_id
JOIN market_resolutions mr ON mr.market_id=p.market_id
WHERE p.decision_engine='openclaw_llm';
```

**Tier distribution**:
```sql
SELECT conviction_tier, COUNT(*) FROM proposals
WHERE created_at > datetime('now','-1 day') AND decision_engine='openclaw_llm'
GROUP BY conviction_tier;
```

**Rate-limit events today**:
```sql
SELECT COUNT(*) FROM llm_rate_limit_events WHERE hit_at > datetime('now','-1 day');
```
