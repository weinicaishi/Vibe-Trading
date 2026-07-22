# ADR 0002：MySQL 8.x 作为 Market Morning 产品数据层

- 状态：Accepted
- 日期：2026-07-21
- 适用范围：Market Morning 账户、内容、任务、投递与审计数据

## 背景

产品需要事务、唯一约束、并发 worker、不可变版本和可审计迁移。现有本地 JSON/SQLite 状态不能满足多人 SaaS 和后台任务正确性。

## 决策

- 使用 MySQL 8.x、InnoDB、`utf8mb4`；数据库默认排序规则推荐 `utf8mb4_ja_0900_as_cs`；应用使用 SQLAlchemy 2 async 与 `asyncmy`。
- schema 仅通过 `agent/migrations/market_morning/` 下的 Alembic 迁移演进。
- 当前精确 revision 为 `0018_market_morning_auth_sessions`；readiness 不接受缺失或过期 revision。
- 数据库和连接 session 使用 UTC；时间列使用 `DATETIME(6)`，JST 业务日期单列为 `DATE`。
- 日文搜索键由应用确定性规范化并使用 binary collation；不依赖数据库模糊语义。
- MySQL 不使用 partial unique index；软删除唯一性由 generated active key 和唯一约束保证。
- 生产、业务验收和破坏性迁移演练使用不同 database，后两者不得指向生产目标。

## 后果

- DDL 在 MySQL 中非事务化，生产迁移必须先在可丢弃迁移库演练。
- 数据库 URL 只通过 secrets 注入，禁止进入仓库、前端、日志和证据 manifest。
- 手工建表只允许使用经过审核、与当前 revision 一致的 bootstrap SQL；已有库必须使用 Alembic 增量升级。
