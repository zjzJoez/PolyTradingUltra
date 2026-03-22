# Polymarket Trading OS v0.3 / v0.4 Locked Implementation Plan

## Summary

目标是在不破坏现有 v0.2 功能的前提下，把系统升级为可研究、可授权、可管仓、可强停机的交易平台。

已锁定的实现决策：

- LLM 接入采用“薄包装接入”
  - 保留现有 external proposal JSON 兼容路径
  - 新增 OpenClaw adapter/service 作为可选执行路径
  - deterministic 路径必须始终可跑
- 数据库升级采用“`schema.sql` + migrations”
  - `schema.sql` 保持全量初始化基线
  - 新增 migration runner 和 `schema_migrations` 表
  - 历史库走增量升级，不允许靠手工删库重建
- 命中策略授权的自动执行采用“独立执行队列”
  - `risk-engine` 只负责把 proposal 状态推进到 `authorized_for_execution`
  - 不在 `risk-engine` 内直接下单
  - 由独立 executor 命令扫描并执行

计划文档落点固定为项目主目录：

- `IMPLEMENTATION_PLAN_v0.3_v0.4.md`

## Core Components

### v0.3: Research / Authorization / Portfolio Risk

新增表：

- `schema_migrations`
- `event_clusters`
- `market_event_links`
- `research_memos`
- `strategy_authorizations`
- `shadow_executions`

扩展表：

- `proposals.strategy_name`
- `proposals.topic`
- `proposals.event_cluster_id`
- `proposals.source_memo_id`
- `proposals.authorization_status`
- `proposals.supervisor_decision`
- `proposals.priority_score`

新增模块：

- `src/polymarket_mvp/migrations/`
  - 迁移 SQL 文件
  - migration runner
- `src/polymarket_mvp/services/event_cluster_service.py`
  - 规则化 market tagging 和 cluster linking
- `src/polymarket_mvp/services/memo_service.py`
  - 基于 `market_contexts` 生成结构化 memo
  - 默认 deterministic，可选走 OpenClaw adapter
- `src/polymarket_mvp/services/openclaw_adapter.py`
  - 封装 OpenClaw 调用
  - 失败时可回退 deterministic 或 external JSON
- `src/polymarket_mvp/services/authorization_service.py`
  - 命中授权规则并返回 `authorization_status`
- `src/polymarket_mvp/services/portfolio_risk_service.py`
  - 基于 executions 近似 open exposure
- `src/polymarket_mvp/services/shadow_service.py`
  - 真实执行前写 shadow record
- `src/polymarket_mvp/agents/research_agent.py`
- `src/polymarket_mvp/agents/supervisor_agent.py`

保留但改造的现有模块：

- `event_fetcher.py`
  - 不改主职责，只增加给 memo 层消费的稳定接口
- `proposer.py`
  - 支持优先读取 memo，再回退直接上下文
- `risk_engine.py`
  - 顺序固定为：
    1. 单 proposal 风控
    2. 组合风险
    3. 授权检查
  - 输出状态固定为：
    - `risk_blocked`
    - `pending_approval`
    - `authorized_for_execution`
- `tg_approver.py`
  - 继续只处理 `pending_approval`
  - 新增“自动授权已执行/待执行”的通知消息能力，但不承担自动执行职责
- `poly_executor.py`
  - 新增扫描 `authorized_for_execution` proposal 的入口
  - 仍保留现有按 proposal-file 执行模式

新增 CLI：

- `cluster-events`
- `build-memos`
- `authorize-strategy`
- `list-authorizations`
- `shadow-execute`
- `poly-executor --source authorized_queue`

### v0.4: Position / Reconciliation / Kill-Switch / Review

新增表：

- `positions`
- `position_events`
- `order_reconciliations`
- `kill_switches`
- `exit_recommendations`
- `agent_reviews`

新增模块：

- `src/polymarket_mvp/services/position_manager.py`
- `src/polymarket_mvp/services/reconciler.py`
- `src/polymarket_mvp/services/kill_switch_service.py`
- `src/polymarket_mvp/agents/exit_agent.py`
- `src/polymarket_mvp/agents/review_agent.py`

建模决策：

- 不单独建 `shadow_positions`
- 统一用 `positions` 表，加 `is_shadow` 字段区分 shadow/live
- `execution` 仍是订单层
- `position` 是仓位层，不替代 execution

执行与控制规则：

- `position_manager` 在 `mock` / `real` / `shadow` 执行成功后统一建仓
- `reconciler` 定期同步真实订单状态并回填 `executions` / `positions`
- `kill_switch_service` 必须在 executor 代码层强制检查
- `exit_agent` 第一版只生成建议，不自动平仓
- `review_agent` 在 resolution backfill 后生成复盘

新增 CLI：

- `update-positions`
- `sync-orders`
- `run-exit-agent`
- `kill-switch`
- `position-report`

## Status And Interface Rules

### Proposal Status

`proposals.status` 扩展为：

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

语义固定：

- `approved` 只表示人工批准
- `authorized_for_execution` 只表示策略授权通过
- `approved` 或 `authorized_for_execution` 都可进入 executor
- `record_execution` 成功后 proposal 统一收敛为 `executed`

### Authorization Fields

`authorization_status` 取值固定为：

- `none`
- `matched_manual_only`
- `matched_auto_execute`

### Output Schemas

`research_memos` 输出：

```json
{
  "market_id": "1657334",
  "event_cluster_key": "btc-5m-directional",
  "topic": "crypto_btc",
  "thesis": "Short explanation",
  "supporting_evidence": ["..."],
  "counter_evidence": ["..."],
  "uncertainty_notes": "...",
  "generated_by": "deterministic|openclaw"
}
```

`supervisor_agent` 输出：

```json
{
  "proposal_id": "string",
  "strategy_name": "near_expiry_conviction",
  "topic": "crypto_btc",
  "event_cluster_id": 12,
  "decision": "promote",
  "priority_score": 0.78,
  "merge_group": null,
  "notes": "..."
}
```

`exit_recommendations` 输出：

```json
{
  "position_id": 321,
  "recommendation": "reduce",
  "target_reduce_pct": 0.5,
  "reasoning": "Time decay and weaker supporting evidence",
  "confidence_score": 0.71
}
```

## Delivery Order

### Phase 1

- migration runner
- v0.3 schema additions
- db access layer for new tables

### Phase 2

- `event_cluster_service`
- `memo_service`
- `openclaw_adapter`
- `research_agent`
- `proposer.py` 接入 memo

### Phase 3

- `authorization_service`
- `portfolio_risk_service`
- `risk_engine.py` 接入授权与组合风控
- `shadow_service`

### Phase 4

- `supervisor_agent`
- authorized queue execution path
- Telegram 自动授权通知

### Phase 5

- `positions`
- `position_manager`
- `reconciler`

### Phase 6

- `kill_switch_service`
- executor 强制 kill gate

### Phase 7

- `exit_agent`
- `review_agent`
- 报告类 CLI

## Test Plan

必须覆盖：

- 旧 v0.2 流程全链路不回归
- migration 可从现有 SQLite 平滑升级
- clustering 结果稳定、可重复
- memo 可在无 OpenClaw 时 deterministic 生成
- authorization 正确区分 manual / auto-execute
- portfolio risk 超限能阻断 proposal
- `authorized_for_execution` proposal 不走 Telegram 审批
- shadow execution 不影响 real execution
- execution 成功后能创建 `positions`
- reconciler 能回填 live / filled / cancelled
- kill-switch 在 executor 层强制阻断
- resolution 后能生成 review
- 所有新增状态流转具备幂等测试

## Assumptions And Defaults

- 当前 OpenClaw 接入先做 adapter，不把全系统重写成“必须在线调用 OpenClaw”
- `scout_agent` 第一版不单独交付，先由规则化 tagging + clustering 替代
- `exit_agent` 第一版只出建议，不直接自动平仓
- kill-switch 第一版先走 CLI，不先做 Telegram slash commands
- 自动授权 proposal 的执行采用独立扫描队列，不内嵌到 `risk-engine`
- 所有新能力都必须复用现有 `risk_engine`、`tg_approver`、`poly_executor`，不允许旁路
