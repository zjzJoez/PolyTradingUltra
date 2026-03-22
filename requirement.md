Polymarket Event-Driven Trading OS - v0.2 PRD

Status note:

- This file is kept as the historical v0.2 product requirement document.
- The current implementation baseline is now v0.3/v0.4.
- Use [README.md](/private/tmp/polymarket-mvp/README.md) for the current operating guide.
- Use [IMPLEMENTATION_PLAN_v0.3_v0.4.md](/private/tmp/polymarket-mvp/IMPLEMENTATION_PLAN_v0.3_v0.4.md) for the current implementation plan.
- Use [openclaw-polymarket-mvp.yaml](/private/tmp/polymarket-mvp/workflows/openclaw-polymarket-mvp.yaml) for the current authorization-first workflow.

1. 项目背景与核心理念 (Project Vision)
本项目旨在构建一个基于 Polymarket 的事件驱动型自动化交易系统 MVP。
系统的核心架构哲学是：“OpenClaw 管脑子，代码管手，风控管命。”
目前 v0.1 版本的本地 Mock 流转（抓取 -> AI 提案 -> TG 审批 -> 模拟拦截）已跑通。v0.2 的目标是将系统接入真实的数据源和交易 API，并建立持久化的数据资产库。

2. 系统架构与技术栈 (Architecture & Stack)
编排大脑层 (Brain): OpenClaw (负责调用 Skills，执行 Prompt 逻辑推理)。

语言与环境: Python 3.9+ (纯 Python 实现底层组件，避免 Node.js 混写增加复杂度)。

外部接口 (Skills/Sensors):

Polymarket Gamma API (获取盘口与流动性)。

News / Twitter Web Scraper API (获取事件触发源与共识情绪)。

网关与风控 (Shield): Telegram Bot API (Human-in-the-loop 审批流) + 本地 Python 风控校验逻辑。

执行层 (Hands): Polymarket 官方 pyclob 库 (负责签名与下单)。

数据存储 (Memory): SQLite (轻量级本地落库，记录完整决策链，为未来机器学习和策略微调提供高质量数据集)。

3. 核心模块与功能需求 (Module Specifications)
模块 A：感知层 (Data Ingestion & Sensors)
功能 1: 盘口雷达 (poly_scanner)

定时轮询 Polymarket，过滤出流动性 > 设定阈值（如 10,000 U）、距离到期时间 < 设定天数（如 7 天）的活跃盘口。

功能 2: 外部事件嗅探 (event_fetcher) [New]

根据盘口 Topic，调用外部新闻 API 或 Twitter API，抓取最近 24 小时内的高相关性文本，作为 AI 决策的 Context。

模块 B：决策层 (AI Reasoning)
由 OpenClaw 的 Workflow 控制，整合模块 A 的数据，输入给大模型。

严格输出约束: 必须输出包含 market_id, outcome (Yes/No), confidence_score (0-1), recommended_size_usdc, reasoning 的标准 JSON 格式。

模块 C：风控与审批网关 (Risk & Approval Gateway)
功能 1: 硬编码风控拦截 (risk_engine)

在推送到 Telegram 之前，校验单笔下单金额是否超过硬性上限（如 10 U 保护阈值）。

校验钱包当前可用余额是否充足。

功能 2: Telegram 审批流 (tg_approver)

通过 Webhook 接收回调。发送包含盘口链接、AI Reasoning 和 [Approve] / [Reject] 按钮的消息。

任何非 "Approve" 状态的提案，绝对不允许进入执行层。

模块 D：真实执行层 (Execution Engine) [New]
功能 1: 钱包与客户端初始化

通过 .env 读取独立的测试钱包私钥，初始化 pyclob 客户端。

功能 2: 下单与确认 (poly_executor)

收到 Approve 指令后，解析 JSON 提案，调用 Polymarket Clob API 执行市价/限价买入。

捕获执行结果（成功 txhash 或失败原因）。

模块 E：数据持久化 (Data Pipeline) [New]
功能要求: 弃用临时 JSONL 文件，改用 SQLite。每一笔交易必须记录完整的生命周期。

核心表结构要求 (需包含):

Proposals: 记录 AI 输出的完整 JSON、参考的新闻素材文本、生成时间。

Approvals: 记录人工审批结果、审批时间戳。

Executions: 记录链上 TxHash、实际成交滑点、消耗 Gas。

Market_Resolutions: 记录该盘口最终是 Yes 还是 No（用于后续计算该次 AI 决策的准确率）。

4. 给 AI Coding 助手的开发纪律 (AI Developer Guidelines)
Fail-Safe 原则: 任何 API 调用失败（网络波动、节点限流）、JSON 解析异常，必须直接向外抛出异常并阻断流程，严禁默默忽略（swallow errors）导致空单执行。

绝对解耦: 模块 D（执行层）和模块 B（决策层）之间只能通过 JSON 和 SQLite 交互，严禁代码逻辑互相交叉。

机密隔离: 所有私钥、Token、API Key 必须通过 os.getenv() 读取，并在代码开头进行断言校验，缺失则拒绝启动。

幂等性: 审批回调和下单执行必须是幂等的，防止网络重发导致重复下单。
