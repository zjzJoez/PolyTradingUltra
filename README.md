# Polymarket Trading OS v0.5

Local-first Polymarket trading system with SQLite as the single business source of truth.

当前仓库已经不再只是一个 `scan -> approve -> execute` 的 MVP。它现在支持：

- 市场扫描与上下文抓取
- event clustering 与 research memo
- proposal 元数据增强
- 单 proposal 风控 + 组合风险 + 策略授权
- Telegram 人工审批路径
- `authorized_for_execution` 自动执行队列
- `mock` / `real` 执行
- positions / reconciliations / kill-switch / exit-review 基础设施

## Architecture

系统职责分层：

- OpenClaw / LLM
  - 作为可选的薄包装 adapter
  - 可以生成 memo、supervisor 决策、review
  - deterministic 路径始终可跑
- Python pipeline
  - 扫描市场、抓事件、聚类、生成 memo、产出 proposal
  - 执行风险检查、授权检查、审批、下单、对账、建仓
- SQLite
  - proposal、approval、execution、position、review 的唯一状态源
- Risk + control plane
  - slippage gate
  - session spend guard
  - strategy authorization
  - kill-switch

## Current Operating Modes

### 1. Authorization-first queue

这是当前 v0.3/v0.4 的主工作流：

1. `poly-scanner`
2. `event-fetcher`
3. `cluster-events`
4. `build-memos`
5. `proposal-generator`
6. `risk-engine`
7. `poly-executor --source authorized_queue`
8. `update-positions`
9. `position-report`

当 proposal 命中有效的 `strategy_authorization` 时，`risk-engine` 会把 proposal 状态推进到 `authorized_for_execution`，然后由 executor 扫描队列执行。

### 2. Human approval fallback

当 proposal 没有命中自动授权时，`risk-engine` 会把它推进到 `pending_approval`，然后进入 Telegram 审批路径：

1. `tg-approver send`
2. `tg-approver serve`
3. Telegram `Approve / Reject`
4. `poly-executor --proposal-file ...`

注意：

- 当前仓库的 workflow YAML 重点覆盖 authorization-first 路径
- Telegram 路径仍然可用，但更适合 operator 手动选择 pending proposals 后执行

## Repository Layout

```text
src/polymarket_mvp/
  agents/
    exit_agent.py
    research_agent.py
    review_agent.py
    supervisor_agent.py
  migrations/
    20260322_v03_v04.sql
  services/
    authorization_service.py
    event_cluster_service.py
    kill_switch_service.py
    memo_service.py
    openclaw_adapter.py
    portfolio_risk_service.py
    position_manager.py
    reconciler.py
    shadow_service.py
  autopilot.py
  autopilot_status.py
  authorize_strategy.py
  build_memos.py
  cluster_events.py
  common.py
  db.py
  db_init.py
  event_fetcher.py
  kill_switch.py
  list_authorizations.py
  mock_executor.py
  poly_executor.py
  poly_scanner.py
  position_report.py
  proposer.py
  resolution_backfill.py
  risk_engine.py
  run_exit_agent.py
  run_review_agent.py
  shadow_execute.py
  sync_orders.py
  tg_approver.py
  update_positions.py

schema.sql
workflows/openclaw-polymarket-mvp.yaml
deploy/
  com.polymarket.autopilot.plist
  com.polymarket.tg-webhook.plist
```

## Installation

Python 3.11+ is recommended. Real execution requires Python 3.10+ and `py-clob-client`.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -e .
```

For real trading:

```bash
pip install -e .[real-exec]
```

## Environment

Copy `.env.example`:

```bash
cp .env.example .env
```

Important variable groups:

- Core
  - `POLYMARKET_MVP_STATE_DIR`
  - `POLYMARKET_MVP_DB_PATH`
- Telegram
  - `TG_BOT_TOKEN`
  - `TG_CHAT_ID`
  - `TG_WEBHOOK_SECRET`
  - `TG_AUTO_EXECUTE_ON_APPROVE`
  - `TG_AUTO_EXECUTE_MODE`
- Context adapters
  - `CRYPTOPANIC_AUTH_TOKEN`
  - `APIFY_TOKEN` or `APIFY_API_KEY`
  - `PERPLEXITY_API_KEY`
  - `PERPLEXITY_MODEL`
- OpenClaw
  - `OPENCLAW_TRANSPORT`
  - `OPENCLAW_CLI_PATH`
  - `OPENCLAW_AGENT_ID`
  - `OPENCLAW_THINKING`
  - `OPENCLAW_TIMEOUT_SECONDS`
  - `OPENCLAW_API_URL`
  - `OPENCLAW_API_KEY`
  - `OPENCLAW_MODEL`
- Risk
  - `POLY_RISK_MAX_ORDER_USDC`
  - `POLY_RISK_MIN_CONFIDENCE`
  - `POLY_RISK_MAX_SLIPPAGE_BPS`
  - `POLY_RISK_REQUIRE_EXECUTABLE_MARKET`
  - `POLY_RISK_MAX_TOPIC_EXPOSURE_USDC`
  - `POLY_RISK_MAX_CLUSTER_EXPOSURE_USDC`
  - `POLY_RISK_MAX_STRATEGY_DAILY_GROSS_USDC`
  - `POLYMARKET_AVAILABLE_BALANCE_U`
- Real execution / CLOB
  - `POLY_CLOB_HOST`
  - `CHAIN_ID` or `POLY_CLOB_CHAIN_ID`
  - `SIGNATURE_TYPE` or `POLY_CLOB_SIGNATURE_TYPE`
  - `FUNDER` or `POLY_CLOB_FUNDER`
  - `POLY_API_KEY`
  - `POLY_API_SECRET`
  - `POLY_API_PASSPHRASE`
  - `POLY_CLOB_SIGNER_KEY`
  - `SESSION_MAX_BALANCE_USDC`
  - `SESSION_MAX_SPEND_USDC`
  - `POLYGON_RPC_URL` — Polygon RPC endpoint for on-chain approve txns (default: `https://polygon-bor-rpc.publicnode.com`)

## CLI Entry Points

Current console scripts:

```bash
db-init
poly-scanner
event-fetcher
cluster-events
build-memos
proposal-generator
risk-engine
authorize-strategy
list-authorizations
shadow-execute
tg-approver
poly-executor
poly-mock-executor
update-positions
sync-orders
kill-switch
run-exit-agent
run-review-agent
position-report
backfill-resolutions
polymarket-autopilot
autopilot-status
```

Equivalent module form:

```bash
PYTHONPATH=src python3 -m polymarket_mvp.<module>
```

## Data Model

Main tables:

- `market_snapshots`
- `market_contexts`
- `event_clusters`
- `market_event_links`
- `research_memos`
- `proposals`
- `proposal_contexts`
- `strategy_authorizations`
- `approvals`
- `executions`
- `shadow_executions`
- `positions`
- `position_events`
- `order_reconciliations`
- `kill_switches`
- `exit_recommendations`
- `agent_reviews`
- `market_resolutions`

Main proposal statuses:

- `proposed`
- `risk_blocked`
- `pending_approval`
- `approved`
- `rejected`
- `authorized_for_execution`
- `executed`
- `failed`
- `expired`
- `cancelled`

Important semantics:

- `approved` means explicit human approval
- `authorized_for_execution` means code-level strategy authorization matched
- `executed` means an execution record was successfully persisted
- `real` executor still performs stricter preflight than `risk-engine`

## Quick Start: Authorization-first v0.4 Flow

### 1. Initialize or migrate database

```bash
PYTHONPATH=src python3 -m polymarket_mvp.db_init
```

### 2. Scan live markets

```bash
PYTHONPATH=src python3 -m polymarket_mvp.poly_scanner \
  --min-liquidity 10000 \
  --max-expiry-days 7 \
  --output artifacts/markets.json
```

### 3. Fetch event context

```bash
PYTHONPATH=src python3 -m polymarket_mvp.event_fetcher \
  --market-file artifacts/markets.json \
  --output artifacts/contexts.json
```

### 4. Cluster markets

```bash
PYTHONPATH=src python3 -m polymarket_mvp.cluster_events \
  --market-file artifacts/markets.json \
  --output artifacts/clusters.json
```

### 5. Build research memos

```bash
PYTHONPATH=src python3 -m polymarket_mvp.build_memos \
  --market-file artifacts/markets.json \
  --output artifacts/memos.json
```

### 6. Seed strategy authorizations

Create a JSON file like:

```json
{
  "strategy_name": "near_expiry_conviction",
  "scope_topic": "BTC",
  "scope_market_type": "binary",
  "scope_event_cluster_id": null,
  "max_order_usdc": 5.0,
  "max_daily_gross_usdc": 25.0,
  "max_open_positions": 5,
  "max_daily_loss_usdc": 25.0,
  "max_slippage_bps": 500,
  "allow_auto_execute": true,
  "requires_human_if_above_usdc": 5.0,
  "valid_from": "2026-03-31T00:00:00Z",
  "valid_until": "2026-12-31T00:00:00Z",
  "status": "active",
  "created_by": "operator"
}
```

Then load it:

```bash
PYTHONPATH=src python3 -m polymarket_mvp.authorize_strategy create \
  --json-file artifacts/strategy-authorization.json \
  --output artifacts/authorization.json
```

### 7. Generate proposals

Heuristic:

```bash
PYTHONPATH=src python3 -m polymarket_mvp.proposer \
  --market-file artifacts/markets.json \
  --context-file artifacts/contexts.json \
  --engine heuristic \
  --size-usdc 5 \
  --top 3 \
  --max-slippage-bps 500 \
  --output artifacts/proposals.json
```

External OpenClaw / LLM JSON:

```bash
PYTHONPATH=src python3 -m polymarket_mvp.proposer \
  --market-file artifacts/markets.json \
  --context-file artifacts/contexts.json \
  --engine openclaw_llm \
  --proposal-file artifacts/openclaw-proposals.json \
  --output artifacts/proposals.json
```

Direct OpenClaw generation:

```bash
OPENCLAW_TRANSPORT=cli \
PYTHONPATH=src python3 -m polymarket_mvp.proposer \
  --market-file artifacts/markets.json \
  --context-file artifacts/contexts.json \
  --engine openclaw_llm \
  --size-usdc 5 \
  --top 3 \
  --max-slippage-bps 500 \
  --output artifacts/proposals.json
```

Notes:

- `OPENCLAW_TRANSPORT=cli` uses your local `openclaw agent --local --json`.
- If `OPENCLAW_TRANSPORT` is omitted, the adapter tries `OPENCLAW_API_URL` first, then local CLI, then OpenAI chat completion fallback.
- `--proposal-file` remains supported when you want OpenClaw to run outside the repo and only import the final JSON.

### 8. Apply risk and authorization

```bash
PYTHONPATH=src python3 -m polymarket_mvp.risk_engine \
  --proposal-file artifacts/proposals.json \
  --output artifacts/risk.json
```

Expected outcomes:

- `risk_blocked`
- `pending_approval`
- `authorized_for_execution`

### 9. Execute authorized queue

Mock:

```bash
PYTHONPATH=src python3 -m polymarket_mvp.poly_executor \
  --source authorized_queue \
  --mode mock \
  --output artifacts/authorized-execution.json
```

Real:

```bash
PYTHONPATH=src python3 -m polymarket_mvp.poly_executor \
  --source authorized_queue \
  --mode real \
  --output artifacts/authorized-execution.json
```

### 10. Refresh positions and report

```bash
PYTHONPATH=src python3 -m polymarket_mvp.update_positions \
  --output artifacts/positions.json
```

```bash
PYTHONPATH=src python3 -m polymarket_mvp.position_report \
  --output artifacts/position-report.json
```

## Human Approval Path

### 1. Run Telegram webhook server

```bash
PYTHONPATH=src python3 -m polymarket_mvp.tg_approver serve --port 8787
```

### 2. Expose it publicly

```bash
ngrok http 8787
```

### 3. Register webhook

```bash
PYTHONPATH=src python3 -m polymarket_mvp.tg_approver set-webhook \
  --webhook-url https://<your-ngrok-domain>
```

### 4. Send pending approvals

```bash
PYTHONPATH=src python3 -m polymarket_mvp.tg_approver send \
  --proposal-file artifacts/proposals.json \
  --output artifacts/approval-request.json
```

### 5. Wait or poll status

```bash
PYTHONPATH=src python3 -m polymarket_mvp.tg_approver status \
  --proposal-file artifacts/proposals.json \
  --output artifacts/approval-status.json
```

Operator note:

- `tg-approver send` is intended for proposals currently in `pending_approval`
- If your proposal file mixes `authorized_for_execution` and `pending_approval`, run the auto-exec queue separately first or export a pending-only proposal subset

## Reconciliation, Control, And Review

Sync live orders:

```bash
PYTHONPATH=src python3 -m polymarket_mvp.sync_orders \
  --output artifacts/reconciliations.json
```

Set kill-switch:

```bash
PYTHONPATH=src python3 -m polymarket_mvp.kill_switch set \
  --scope-type strategy \
  --scope-key near_expiry_conviction \
  --reason "manual halt"
```

Generate exit suggestions:

```bash
PYTHONPATH=src python3 -m polymarket_mvp.run_exit_agent \
  --output artifacts/exit-recommendations.json
```

Backfill resolutions:

```bash
PYTHONPATH=src python3 -m polymarket_mvp.resolution_backfill \
  --input artifacts/resolutions.json \
  --output artifacts/resolution-backfill.json
```

Generate reviews:

```bash
PYTHONPATH=src python3 -m polymarket_mvp.run_review_agent \
  --output artifacts/reviews.json
```

## 24/7 Autopilot Mode (v0.5)

The autopilot replaces the manual CLI chain with a single long-running supervisor:

```bash
polymarket-autopilot
```

It continuously runs: scan -> context -> propose -> risk -> approve(TG) -> execute -> reconcile -> exit -> review.

Key features:
- Approval deadlines: each Telegram approval has a TTL; proposals auto-expire if not acted on
- Stale order cancellation: live orders exceeding their TTL are auto-cancelled
- OpenClaw exit proposals: LLM-powered exit decisions for open positions
- Heartbeat logging to `autopilot_heartbeats` table for observability

Check status:

```bash
autopilot-status
```

Local dashboard:

```text
http://127.0.0.1:8787/ops
```

JSON observability endpoints:

- `/api/ops/status`
- `/api/ops/proposals`
- `/api/ops/failures`
- `/api/ops/events`

### launchd Deployment (macOS)

```bash
# Copy plist templates
cp deploy/com.polymarket.autopilot.plist ~/Library/LaunchAgents/
cp deploy/com.polymarket.tg-webhook.plist ~/Library/LaunchAgents/

# Edit paths in plists to match your setup, then load
launchctl load ~/Library/LaunchAgents/com.polymarket.autopilot.plist
launchctl load ~/Library/LaunchAgents/com.polymarket.tg-webhook.plist

# Set Telegram webhook (one-time)
tg-approver set-webhook --webhook-url https://your-ngrok-domain

# Verify
autopilot-status
```

Autopilot environment variables:
- `POLY_SCAN_INTERVAL_SECONDS` (default 30)
- `POLY_CONTEXT_INTERVAL_SECONDS` (default 60)
- `POLY_DECISION_INTERVAL_SECONDS` (default 30)
- `POLY_RECONCILE_INTERVAL_SECONDS` (default 10)
- `POLY_EXIT_INTERVAL_SECONDS` (default 30)
- `POLY_APPROVAL_MAX_TTL_SECONDS` (default 300)
- `POLY_APPROVAL_EXPIRY_BUFFER_SECONDS` (default 120)
- `POLY_ORDER_MAX_LIVE_TTL_SECONDS` (default 300)
- `POLY_AUTOPILOT_MAX_CANDIDATES_PER_LOOP` (default 25)
- `POLY_AUTOPILOT_MAX_EXIT_PROPOSALS_PER_LOOP` (default 5)

## Notes

- `event_fetcher` Twitter adapter soft-fails when Apify blocks the actor; the pipeline continues with a fallback context.
- `risk-engine` now falls back to snapshot outcome price when CLOB `/price` returns `404 No orderbook exists`, which avoids false negatives for markets still usable in higher-level workflows.
- `real` execution is still stricter than `risk-engine`; no change was made to preflight signing, balance sanity, session spend, or slippage enforcement.
- Existing long-running `tg_approver serve` processes should be restarted after code changes.
- The current workflow YAML is authorization-first. Telegram approval remains a supported operator path, but not the only path.

## Verification

The current repo has a verified smoke-tested path for:

- `risk-engine -> authorized_for_execution`
- `poly-executor --source authorized_queue`
- `update-positions`
- `position-report`

Representative outputs:

- [artifacts/smoke-risk-3.json](/private/tmp/polymarket-mvp/artifacts/smoke-risk-3.json)
- [artifacts/smoke-authorized-execution-3.json](/private/tmp/polymarket-mvp/artifacts/smoke-authorized-execution-3.json)
- [artifacts/smoke-position-report-postfix.json](/private/tmp/polymarket-mvp/artifacts/smoke-position-report-postfix.json)
