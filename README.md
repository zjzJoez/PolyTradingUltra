# Polymarket Trading OS v0.2

This repo implements a local-first Polymarket event-driven trading MVP with:

- SQLite as the only business source of truth
- OpenClaw/LLM-ready proposal records
- hard risk checks before Telegram approval
- strict human approval gating
- `mock` and `real` execution modes
- auditable proposal, approval, execution, and resolution records

The operating principle stays the same:

- OpenClaw manages the reasoning layer
- Python code manages data ingestion, gateways, persistence, and execution
- risk controls and human approval decide whether anything is allowed to trade

## What Changed In v0.2

- `schema.sql` defines the SQLite schema for proposals, contexts, approvals, executions, and resolutions
- `poly_scanner` now also upserts market snapshots into SQLite
- `event_fetcher` adds three adapters:
  - CryptoPanic
  - Apify Twitter Scraper
  - Perplexity
- `proposer` now persists normalized proposal records with `max_slippage_bps`
- `risk_engine` moves proposals into `pending_approval` or `risk_blocked`
- `tg_approver` reads and writes approval state from SQLite
- `poly_executor` supports `mock` and `real`

## Core Commands

Base install:

```bash
pip install -e .
```

Optional real execution dependencies (`py-clob-client`) require Python 3.10+:

```bash
pip install -e .[real-exec]
```

After install, the main commands are:

```bash
db-init
poly-scanner
event-fetcher
proposal-generator
risk-engine
tg-approver
poly-executor
backfill-resolutions
```

If you prefer not to install console scripts yet, use module form:

```bash
PYTHONPATH=src python3 -m polymarket_mvp.<module>
```

## Proposal Shape

All normalized proposals now use this schema:

```json
{
  "market_id": "1576206",
  "outcome": "Yes",
  "confidence_score": 0.82,
  "recommended_size_usdc": 10.0,
  "reasoning": "Explain the trade thesis.",
  "max_slippage_bps": 500
}
```

## Environment Variables

Copy `.env.example` to `.env` and fill in the values you actually use.

Required groups:

- core
  - `POLYMARKET_MVP_STATE_DIR`
  - `POLYMARKET_MVP_DB_PATH`
- Telegram
  - `TG_BOT_TOKEN`
  - `TG_CHAT_ID`
  - `TG_WEBHOOK_SECRET`
- event fetcher
  - `CRYPTOPANIC_AUTH_TOKEN`
  - `CRYPTOPANIC_BASE_URL` (optional, default points to `/posts/`)
  - `APIFY_TOKEN` (or `APIFY_API_KEY`)
  - `PERPLEXITY_API_KEY`
- real execution
  - `POLY_CLOB_HOST`
  - `CHAIN_ID` (or `POLY_CLOB_CHAIN_ID`)
  - `SIGNATURE_TYPE` (or `POLY_CLOB_SIGNATURE_TYPE`)
  - `FUNDER` (or `POLY_CLOB_FUNDER`)
  - `POLY_API_KEY`
  - `POLY_API_SECRET`
  - `POLY_API_PASSPHRASE`
  - `POLY_CLOB_SIGNER_KEY` (order signer key, separate from funder wallet)
  - `SESSION_MAX_BALANCE_USDC`
  - `SESSION_MAX_SPEND_USDC`

Risk defaults:

- `POLY_RISK_MAX_ORDER_USDC`
- `POLY_RISK_MIN_CONFIDENCE`
- `POLY_RISK_MAX_SLIPPAGE_BPS`
- `POLYMARKET_AVAILABLE_BALANCE_U`

## Local Flow

### 1. Initialize SQLite

```bash
PYTHONPATH=src python3 -m polymarket_mvp.db_init
```

### 2. Scan markets

```bash
PYTHONPATH=src python3 -m polymarket_mvp.poly_scanner \
  --min-liquidity 10000 \
  --max-expiry-days 7 \
  --output artifacts/markets.json
```

### 3. Fetch external contexts

```bash
PYTHONPATH=src python3 -m polymarket_mvp.event_fetcher \
  --market-file artifacts/markets.json \
  --output artifacts/contexts.json
```

### 4. Generate proposal records

Heuristic mode:

```bash
PYTHONPATH=src python3 -m polymarket_mvp.proposer \
  --market-file artifacts/markets.json \
  --context-file artifacts/contexts.json \
  --engine heuristic \
  --size-usdc 10 \
  --top 3 \
  --max-slippage-bps 500 \
  --output artifacts/proposals-v2.json
```

External LLM/OpenClaw mode:

```bash
PYTHONPATH=src python3 -m polymarket_mvp.proposer \
  --market-file artifacts/markets.json \
  --context-file artifacts/contexts.json \
  --engine openclaw_llm \
  --proposal-file artifacts/openclaw-proposals.json \
  --output artifacts/proposals-v2.json
```

### 5. Apply hard risk gate

```bash
PYTHONPATH=src python3 -m polymarket_mvp.risk_engine \
  --proposal-file artifacts/proposals-v2.json \
  --output artifacts/risk.json
```

### 6. Start Telegram webhook server

```bash
PYTHONPATH=src python3 -m polymarket_mvp.tg_approver serve --port 8787
```

### 7. Send approval requests

Dry run:

```bash
PYTHONPATH=src python3 -m polymarket_mvp.tg_approver send \
  --proposal-file artifacts/proposals-v2.json \
  --dry-run \
  --output artifacts/approval-request.json
```

Real send:

```bash
PYTHONPATH=src python3 -m polymarket_mvp.tg_approver send \
  --proposal-file artifacts/proposals-v2.json \
  --output artifacts/approval-request.json
```

### 8. Wait for approval

```bash
PYTHONPATH=src python3 -m polymarket_mvp.tg_approver await \
  --proposal-file artifacts/proposals-v2.json \
  --timeout-seconds 300 \
  --poll-interval 2 \
  --output artifacts/approval-status.json
```

### 9. Execute approved proposals

Mock:

```bash
PYTHONPATH=src python3 -m polymarket_mvp.poly_executor \
  --proposal-file artifacts/proposals-v2.json \
  --mode mock \
  --output artifacts/execution.json
```

Real:

```bash
PYTHONPATH=src python3 -m polymarket_mvp.poly_executor \
  --proposal-file artifacts/proposals-v2.json \
  --mode real \
  --output artifacts/execution.json
```

## Telegram Webhook Notes

Telegram button callbacks require a public webhook URL pointing to:

```text
POST /telegram/webhook
```

Typical local setup:

```bash
ngrok http 8787
```

Then register the webhook:

```bash
PYTHONPATH=src python3 -m polymarket_mvp.tg_approver set-webhook \
  --webhook-url https://your-ngrok-url.ngrok-free.app
```

Inspect current webhook configuration:

```bash
PYTHONPATH=src python3 -m polymarket_mvp.tg_approver webhook-info
```

## Notes

- `proposal_contexts.raw_text` stores the full archived material
- the actual context fed to the reasoning layer is the truncated `context_payload_json`
- Perplexity summaries are prioritized ahead of CryptoPanic and Twitter snippets
- `mock` execution is intended to stay as the regression-safe path even after `real` trading is enabled
