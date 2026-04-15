# Claude Code Handoff: Polymarket Trading OS v0.6

## Goal

把当前系统推进到 `v0.6 Stability / Risk Hardening`。

这次更新的核心不是加新策略，而是先把系统做稳、做硬、做可解释。

主文档：

- [NEXT_UPDATE_PRD_v0.6.md](/Users/joez/Desktop/polymarket-mvp/NEXT_UPDATE_PRD_v0.6.md:1)
- [summary.md](/Users/joez/Desktop/polymarket-mvp/artifacts/review-2026-04/summary.md:1)
- [trade_review.py](/Users/joez/Desktop/polymarket-mvp/src/polymarket_mvp/trade_review.py:1)

## Priority

先做 `P0`，不要先碰 `P1/P2`。

推荐开发顺序：

1. `P0-1` duplicate exposure hard block
2. `P0-2` proposal-level active execution uniqueness
3. `P0-3` terminal-state / resolution consistency hardening
4. `P0-4` structured execution failure categories + runtime hardening

## Hard Product Decisions

这些决策已经锁定，不要再改方向：

- `crypto_up_down` 默认退出 live universe
- 当前不接 Alpha Lab live signal
- 当前不扩大自动批准范围
- 当前不提高默认 order size
- 当前不基于原始 confidence 放大仓位

## Current Review Facts

- 历史数据：`1545 proposals / 143 executions / 63 positions`
- 已实现 PnL：`+8.249847 USDC`
- gross wins / gross losses：`+161.807447 / -153.5576`
- 近似最大回撤：`49.1776`
- 最大可控问题：
  - `duplicate_exposure`
  - `execution_failure`
  - `reconciliation_gap`
  - `ops_lock`
  - `market_type_concentration`

关键证据：

- `duplicate_exposure` incidents: `20`
- `execution_failure` incident groups: `5`
- `reconciliation_gap` incidents: `5`
- `ops_lock` incident rows: `56`
- `crypto_up_down` realized pnl: `-28.6673`
- `sports_winner` realized pnl: `+34.073288`

## First Coding Milestone

先完成一个最小但完整的 `P0` 里程碑：

### Scope

- 阻止同一 `market_id + outcome` 的重复 live entry
- 确保同一 proposal 同时最多只有一个 active execution
- 为这两条规则补齐自动化测试

### Acceptance

- 重复同向 proposal 无法进入可执行态
- 同一 proposal 无法生成多条 `submitted/live` execution
- 不破坏现有测试
- 新增测试覆盖：
  - duplicate market/outcome exposure
  - duplicate active execution lineage

## Relevant Files

高相关：

- [NEXT_UPDATE_PRD_v0.6.md](/Users/joez/Desktop/polymarket-mvp/NEXT_UPDATE_PRD_v0.6.md:1)
- [src/polymarket_mvp/services/portfolio_risk_service.py](/Users/joez/Desktop/polymarket-mvp/src/polymarket_mvp/services/portfolio_risk_service.py:1)
- [src/polymarket_mvp/poly_executor.py](/Users/joez/Desktop/polymarket-mvp/src/polymarket_mvp/poly_executor.py:1)
- [src/polymarket_mvp/services/reconciler.py](/Users/joez/Desktop/polymarket-mvp/src/polymarket_mvp/services/reconciler.py:1)
- [src/polymarket_mvp/services/position_manager.py](/Users/joez/Desktop/polymarket-mvp/src/polymarket_mvp/services/position_manager.py:1)
- [src/polymarket_mvp/db.py](/Users/joez/Desktop/polymarket-mvp/src/polymarket_mvp/db.py:1)
- [tests/test_v03_v04_flow.py](/Users/joez/Desktop/polymarket-mvp/tests/test_v03_v04_flow.py:1)
- [tests/test_trade_review.py](/Users/joez/Desktop/polymarket-mvp/tests/test_trade_review.py:1)

次相关：

- [src/polymarket_mvp/autopilot.py](/Users/joez/Desktop/polymarket-mvp/src/polymarket_mvp/autopilot.py:1)
- [src/polymarket_mvp/ops_snapshot.py](/Users/joez/Desktop/polymarket-mvp/src/polymarket_mvp/ops_snapshot.py:1)
- [src/polymarket_mvp/proposer.py](/Users/joez/Desktop/polymarket-mvp/src/polymarket_mvp/proposer.py:1)

## Repo Constraints

- 这是一个 dirty worktree，不要回滚不属于当前任务的改动
- 不要改 Alpha Lab 范围
- 不要扩大 live market scope
- 不要把 review CLI 删掉或改成依赖在线 DB 查询
- 所有改动都必须带测试

## Suggested Commands

```bash
PYTHONPATH=src .venv311/bin/pytest -q tests/test_v03_v04_flow.py
PYTHONPATH=src .venv311/bin/pytest -q tests/test_trade_review.py
PYTHONPATH=src .venv311/bin/pytest -q tests
PYTHONPATH=src .venv311/bin/python -m polymarket_mvp.trade_review --db var/polymarket_mvp.sqlite3 --output-dir artifacts/review-2026-04
```

## Deliverables

Claude Code 每一轮应交付：

- 实际代码改动
- 新增或更新的测试
- 简短变更说明
- 运行过的验证命令
- 未解决风险或后续建议

## Copy-Paste Prompt

```text
You are working in /Users/joez/Desktop/polymarket-mvp.

Read these first:
- NEXT_UPDATE_PRD_v0.6.md
- artifacts/review-2026-04/summary.md
- src/polymarket_mvp/trade_review.py

Goal:
Implement the first v0.6 milestone for Polymarket Trading OS: stability and risk hardening, not new strategy work.

Strict priorities:
1. Block duplicate live exposure on the same market_id + outcome.
2. Enforce proposal-level active execution uniqueness so one proposal cannot have multiple active execution lineages.
3. Add or update tests for both behaviors.

Constraints:
- This repo has a dirty worktree. Do not revert unrelated changes.
- Do not expand live market scope.
- Do not integrate Alpha Lab live signals.
- Do not remove the snapshot-first trade review flow.
- Keep changes minimal, explicit, and well-tested.

Relevant files:
- src/polymarket_mvp/services/portfolio_risk_service.py
- src/polymarket_mvp/poly_executor.py
- src/polymarket_mvp/services/reconciler.py
- src/polymarket_mvp/services/position_manager.py
- src/polymarket_mvp/db.py
- tests/test_v03_v04_flow.py
- tests/test_trade_review.py

Acceptance criteria:
- Duplicate same-direction entries cannot progress into executable state.
- A proposal cannot have more than one active submitted/live execution at a time.
- Tests cover both behaviors.
- Existing tests continue to pass.

After implementing, run targeted tests first, then the full test suite.
In your final response, include:
- what changed
- what tests passed
- any remaining risks
```
