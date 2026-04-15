# Polymarket Trading OS v0.6 PRD

## Title

`v0.6 Stability / Risk Hardening`

## Summary

这次大更新不以“扩展新策略”或“接入更多研究信号”为目标。

这次版本的主目标是把当前系统从“能交易但不稳定、能复盘但问题集中”推进到“风险边界清晰、状态机可验证、市场分层明确、运行可持续”的状态。

基于 `artifacts/review-2026-04/` 的全量历史复盘，当前系统的关键特征如下：

- 历史区间：`2026-04-02T09:06:19Z` 到 `2026-04-15T09:44:42Z`
- `1545 proposals / 143 executions / 63 positions`
- 已实现 PnL：`+8.249847 USDC`
- `gross wins = +161.807447`
- `gross losses = -153.5576`
- 已结算 `55` 笔，`25` 胜，`28` 负
- 近似最大回撤：`49.1776`

核心判断：

- 当前系统不是“稳定赚钱”，而是在较高 operational risk 和 market selection noise 下勉强保持正收益。
- 当前最主要的问题不在“缺 alpha”，而在“状态机不够硬、风控边界不够早、市场分类不够细、观测与归因不够结构化”。
- 因此，`v0.6` 应被定义为一次 `risk-control + execution-integrity + observability` 更新。

## Product Goal

`v0.6` 的目标只有四个：

1. 消除重复暴露、重复执行、状态漂移这三类会直接污染账本和放大亏损的问题。
2. 把不同 market class 分层管理，先停止最差一类 live 市场的自动交易。
3. 把 execution / risk / resolution 的结构化 telemetry 补齐，保证后续迭代建立在真实证据而不是猜测上。
4. 把运行稳定性提升到可连续运行且不会持续污染数据库和仓位状态。

## Non-Goals

`v0.6` 明确不做以下事情：

- 不把 Alpha Lab 新信号直接接入 live trading
- 不扩大自动交易 universe
- 不提高默认 order size 或交易频率
- 不引入新的高复杂度 supervisor / orchestration 逻辑
- 不把 paper gain 当成 release success criterion

## Review-Derived Findings

### 1. Duplicate Exposure Is The Largest Controllable Loss Amplifier

复盘显示，最大亏损组多数都带有重复同向 entry：

- `1527148 / Yes`：`-20.00`，重复开了 `2` 笔
- `1968884 / Down`：`-15.075`，重复开了 `3` 笔
- `1968779 / Down`：`-15.05`，重复开了 `3` 笔
- `1718674 / Yes`：`-10.1304`，重复开了 `2` 笔
- `1559828 / Under`：`-10.1194`，重复开了 `2` 笔

这不是 alpha 错误，而是 exposure control 错误。

### 2. Market-Class Performance Is Highly Uneven

按市场类型拆分：

- `crypto_up_down`：`-28.6673`
- `sports_totals`：`-3.187541`
- `sports_winner`：`+34.073288`
- `esports`：`+6.0314`

`crypto_up_down` 明显拖累整体表现，且伴随较高重复暴露和短周期噪声。

### 3. Confidence Is Not Reliably Calibrated

按 confidence bucket 拆分：

- `<0.40`：`-5.0652`
- `0.40-0.49`：`+32.715388`
- `0.50-0.59`：`-18.5953`
- `0.60-0.69`：`-13.050847`
- `0.70+`：样本极少，不足以下结论

当前 confidence 既不能稳定排序收益，也不应该继续直接驱动 sizing。

### 4. Runtime And Execution Integrity Problems Still Exist

incident 汇总显示：

- `duplicate_exposure`: `20`
- `execution_failure`: `5` 类
- `reconciliation_gap`: `5`
- `ops_lock`: `1`
- `market_type_concentration`: `1`
- `split_resolution`: `1`

这说明线上运行问题不是孤立 bug，而是系统层面的状态一致性问题。

## Problem Register

### P0-1. Duplicate Exposure

问题：

- 同一 `market_id + outcome` 在没有显式加仓策略的情况下被重复开仓。

根因：

- 当前 risk gate 对“同题同向已有 exposure / pending entry”的限制不够早或不够严。
- 系统没有区分“重复 proposal”与“有意 scale-in”。

证据：

- `20` 个 `duplicate_exposure` incident
- 最大亏损组中多数都存在 `2-3` 次重复 entry

风险：

- 单一 thesis 的错误会被机械放大。
- 风控看起来通过，但组合层实际过度集中。

优化方案：

- 默认禁止同一 `market_id + outcome` 的重复 live entry。
- 引入显式 `scale_in_policy`，没有该字段则不能二次入场。
- 若后续支持 scale-in，必须同时满足：
  - 更高置信度阈值
  - 更小增量 size
  - 明确的剩余额度检查
  - 新旧 entry 间的最小时间间隔

验收标准：

- proposal 进入 `authorized_for_execution` 前，重复同向 entry 必须被阻断。
- 新历史样本中 `duplicate_exposure` 新增实例为 `0`。

### P0-2. Proposal-Level Execution Idempotency Is Weak

问题：

- 同一 proposal 能留下多条 execution lineage。

根因：

- executor / reconcile 层没有把“proposal 同时只能有一个 active execution”作为硬约束。
- 失败重试和状态同步没有统一幂等键。

证据：

- `5` 个 `reconciliation_gap` incident
- 历史上已出现一条 proposal 对应多条 execution 记录

风险：

- 可能造成重复下单、重复记账、虚假 exposure 收敛

优化方案：

- 增加 proposal 级 active execution uniqueness guard。
- 在 executor 启动下单前，再次检查 proposal 是否已有 live/submitted execution。
- reconcile 增加 duplicate lineage detector，命中后直接告警并熔断该 proposal。

验收标准：

- 任一 proposal 在任意时刻最多只允许一个 `submitted/live/fill-in-progress` execution。
- 重试路径必须复用同一幂等键。

### P0-3. Position / Resolution State Drift

问题：

- 持仓状态、execution 状态、resolution 语义可能不同步。

根因：

- 早期模型默认把 resolution 视为 winner/loser 二元事件。
- canceled execution、split settlement、resolution backfill 的边界路径不完整。

证据：

- 出现过 `50-50` split resolution
- 之前出现过 `open_requested` 残留与 canceled execution 不一致的问题

风险：

- PnL、仓位、风险敞口全部可能失真

优化方案：

- resolution 统一基于 payout map 计算，不再做 winner/loser 简化。
- position terminal-state 增加一致性校验：
  - canceled execution 不能留下非 terminal position
  - resolved market 不能留下 live/open position
- 提供一条强制 state repair CLI，供 daily maintenance 使用。

验收标准：

- `open_requested/open` 残留在 resolved/canceled 场景中必须为 `0`。
- split resolution、canceled execution、normal resolution 三条路径均有自动化测试。

### P0-4. SQLite Locking And Runtime Stability

问题：

- 线上运行时出现过 `database is locked` 和外部依赖错误聚集。

根因：

- 长事务
- 重复 writer 竞争
- 网络依赖失败时缺少细分错误与恢复策略

证据：

- `56` 条 `ops_lock` heartbeat
- `execution_failure` 聚集成 `5` 类

风险：

- autopilot 可运行但不健康
- 失败重试可能进一步污染状态

优化方案：

- 继续收短事务边界，尤其是网络调用与写事务之间的隔离。
- 所有 loop 明确 single-writer 纪律。
- execution failure 结构化分类，至少拆成：
  - `service_not_ready`
  - `request_exception`
  - `real_preflight_failed`
  - `insufficient_balance`
  - `order_submit_failed`
  - `slippage_exceeded`
- 对外部依赖增加 readiness gate、backoff、失败阈值熔断。

验收标准：

- 新运行样本中不再出现新的 `database is locked` incident。
- execution failure 不再只以原始字符串保存，必须带标准化 category。

### P1-1. Market-Class Segmentation Is Too Weak

问题：

- 不同类型 market 被用同一套 strategy + risk 配置处理。

根因：

- 当前系统以“统一 proposal pipeline”为主，没有把 market class 当成一等配置维度。

证据：

- `crypto_up_down = -28.6673`
- `sports_winner = +34.073288`

风险：

- 某一类坏市场拖垮整体系统

优化方案：

- 建立 market class 配置层：
  - `sports_winner`
  - `sports_totals`
  - `crypto_up_down`
  - `esports`
  - `other`
- 每个 class 独立定义：
  - 是否允许 live
  - 最大 daily gross
  - 最大 open positions
  - 最大单笔 size
  - 最小置信度

下一步决策：

- `v0.6` 默认关闭 `crypto_up_down` 的 live execution。
- live 恢复范围优先限定在 `sports_winner` 和小规模 `sports_totals`。

### P1-2. Confidence-To-Size Mapping Must Be Reworked

问题：

- 当前 confidence 分桶和实际收益没有稳定正相关。

根因：

- confidence 更像一个内部评分，不是经过历史校准的赔率优势估计。

证据：

- `0.40-0.49` bucket 表现优于 `0.50-0.69`

风险：

- 当前 sizing 逻辑可能把较差的交易放大

优化方案：

- `v0.6` 期间移除“置信度越高仓位越大”的隐含逻辑。
- 短期改成：
  - 先固定小仓
  - 再按 market class 加上 caps
- 未来版本再用真实历史重建 calibration curve。

验收标准：

- live sizing 不再直接由原始 confidence 单独决定。

### P1-3. Review And Risk Attribution Data Is Incomplete

问题：

- 风控阻断原因、proposal-time microstructure、execution failure attribution 保存不够完整。

根因：

- 当前 schema 更偏 execution ledger，不是为 post-trade attribution 设计的。

证据：

- `1199 risk_blocked` proposals 不能知道具体被哪条规则拦下
- 无法完整回放 proposal 时刻的 microstructure

风险：

- 系统无法知道是“策略差”还是“风控差”还是“执行差”

优化方案：

- proposals 增加结构化 risk decision 字段
- 持久化 proposal-time feature snapshot
- 持久化 proposal-time market microstructure snapshot
- execution failure telemetry 改成结构化字段而非纯文本

验收标准：

- 任意 proposal 都能回答：
  - 为什么被放行或阻断
  - 当时看到的关键市场特征是什么
  - 执行失败属于哪一类

## Locked Product Decisions

以下决策在 `v0.6` 期间锁定，不再反复讨论：

1. `crypto_up_down` 默认退出 live universe。
2. `sports_winner` 是 `v0.6` 的默认 live market class。
3. `sports_totals` 只允许在更低风险参数下逐步恢复。
4. 不在 `v0.6` 接入新的 Alpha Lab live signal。
5. 不扩大自动批准范围。
6. 不基于当前 confidence 继续放大仓位。

## Scope

### In Scope

- duplicate exposure hard block
- proposal-level active execution uniqueness
- terminal-state reconciliation hardening
- split resolution payout-map-first settlement
- structured execution failure categories
- market-class-specific risk config
- `crypto_up_down` live disable
- risk-block reason persistence
- proposal-time telemetry persistence
- snapshot-first review flow as maintained tooling

### Out Of Scope

- 新研究策略上线
- Alpha Lab 信号直连实盘
- 更复杂的自动出场 agent
- 长期 dashboard 平台化
- 换数据库引擎

## Success Criteria

`v0.6` 的成功标准不是“PnL 更高”，而是系统质量更高。

必须同时满足：

- 新增 `duplicate_exposure` 实例为 `0`
- proposal-level duplicate active execution 为 `0`
- resolved/canceled 场景下 stale position 残留为 `0`
- execution failure 均带结构化 category
- live market class 限定配置生效
- 复盘产物可以解释每笔 risk-blocked / failed / resolved 的关键原因

## Release Gates

进入 live 之前必须通过以下 gate：

1. 全量测试通过，且新增状态机和 risk telemetry 测试齐全。
2. 在 shadow/paper 模式下跑完整样本，没有出现 duplicate execution。
3. 没有新的 `database is locked` 事件。
4. 所有 market class 配置已显式声明，默认拒绝未分类市场。
5. `crypto_up_down` 在配置上明确关闭 live。
6. 最新一轮 review 报告中不存在新的 `stale_state` 和 `duplicate_exposure`。

## Rollout Plan

### Phase 1: State Machine Hardening

- duplicate exposure guard
- active execution uniqueness
- terminal position assertions
- stale-state repair CLI

### Phase 2: Runtime Hardening

- execution failure structured codes
- readiness gate / retry / backoff / breaker
- remaining long transaction cleanup

### Phase 3: Market Segmentation

- market class classifier 固化
- class-specific config
- disable `crypto_up_down` live
- lower-risk config for `sports_totals`

### Phase 4: Observability And Attribution

- risk decision persistence
- proposal-time feature snapshot
- proposal-time microstructure snapshot
- weekly review reuse on top of `trade_review`

## Known Unknowns

以下问题在 `v0.6` 不一定完全解决，但必须显式记录：

- LLM 本身是否有净贡献，目前仍无法从历史中单独识别
- approval path 样本只有 `5` 条，暂时不足以下强结论
- shadow/live 样本量仍偏小，不能做高置信度因果判断
- 当前复盘仍以交易 ledger 为主，不是 tick-level 回放系统

## Final Decision

下一个大更新应当立项为：

`Polymarket Trading OS v0.6 Stability / Risk Hardening`

版本目标是“先把系统做稳、做硬、做可解释”，而不是继续扩大 live scope。

换句话说，下一版的核心问题不是“怎么多赚”，而是“怎么避免继续把可控问题变成真实亏损”。
