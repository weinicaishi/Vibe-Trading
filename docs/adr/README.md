# Market Morning ADR 索引

本目录记录 Market Morning MVP 已确认且会影响数据、部署与安全边界的架构决策。当前实现或部署方案若改变这些决策，必须先更新对应 ADR，再更新实施计划和验收证据。

| ADR | 决策 | 状态 |
|---|---|---|
| [0001](./0001-market-morning-product-isolation.md) | Market Morning 与交易/回测内核隔离 | Accepted |
| [0002](./0002-market-morning-mysql-data-layer.md) | MySQL 8.x + SQLAlchemy async + Alembic | Accepted |
| [0003](./0003-market-morning-product-identity.md) | deployment-owned OIDC 产品认证 | Accepted |
| [0004](./0004-market-morning-durable-jobs.md) | MySQL durable jobs + lease scheduler | Accepted |
| [0005](./0005-market-morning-calendar-and-time.md) | UTC 存储、JST 刊期、licensed JPX calendar | Accepted |

“Accepted”表示工程方向已确定，不代表真实供应商、数据许可或 staging 验收已经完成。
