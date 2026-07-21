# ADR 0001：Market Morning 产品垂直层与交易内核隔离

- 状态：Accepted
- 日期：2026-07-21
- 适用范围：Market Morning MVP

## 背景

Vibe-Trading 已包含研究、回测、策略、券商连接、影子账户和通用 Agent 工作流。Market Morning 的目标是提供可追溯的盘前研究，不是交易执行系统。直接复用交易链会扩大权限、合规和故障半径。

## 决策

- 产品领域代码集中在 `agent/src/market_morning/`，产品 API 集中在 `agent/src/api/market_morning_*`。
- 产品表使用 `mm_` 前缀和独立 MySQL database；不写入现有 SQLite、JSON runtime 或交易状态。
- Market Morning 默认关闭；关闭时不创建数据库连接、不启动 worker，也不改变 A 股、指数、研究、回测和 Agent 行为。
- MVP 不依赖或暴露券商、自动交易、影子账户、回测执行或 TradingAgents 决策链。
- 现有 Vibe API Key 只保护内部运营 API，不作为产品用户身份。

## 后果

- 产品需要独立认证、迁移、worker、数据授权与运行手册。
- 共用能力仅限稳定的基础设施模式，例如配置、API 安全基础和 canonical market model；不能把开发数据源自动升级为产品授权来源。
- 任何跨越上述边界的变更都需要单独安全与合规评审。
