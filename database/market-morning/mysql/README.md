# Market Morning MySQL 初始化文件

本目录用于把一个**全新空数据库**初始化到 Market Morning 当前 schema：
`0017_market_morning_content_reports`，也支持把已执行旧包的 `0016` 数据库增量升级到 `0017`。

## 文件

- `market_morning_schema_0017.sql`：全新空库完整建表 SQL，包含 Alembic revision 表、33 张 `mm_` 业务表、索引、外键、约束和最终 revision。
- `market_morning_upgrade_0016_to_0017.sql`：仅供 revision 精确为 `0016_market_morning_model_usage` 的已有库增量升级。
- `market_morning_verify_0017.sql`：只读验收 SQL，检查 MySQL、时区、字符集、revision、Alembic 列宽和业务表清单。
- `market_morning_schema_0016.sql`、`market_morning_verify_0016.sql`：旧版留档，不再用于新建数据库。

## 前置条件

- MySQL 8.x
- database 默认字符集为 `utf8mb4`
- 推荐 database 排序规则为 `utf8mb4_ja_0900_as_cs`；若托管服务不提供，再使用 `utf8mb4_0900_as_cs`
- 表引擎使用 InnoDB
- 应用连接会话使用 UTC
- 执行账号对目标 database 具有建表、索引和外键权限

数据库和账号由 DBA 单独创建。这里不保存主机、账号、密码，也不包含 `CREATE USER` 或 `GRANT`。

## 当前验证基线

2026-07-21 已在隔离的官方 `mysql:8.0` 容器完成以下验证：

- Alembic 空库 `0001 -> 0017`、`0016 -> 0017` 和 `0017 -> 0016 -> 0017` 均通过；
- `market_morning_schema_0017.sql` 可直接导入全新空库；
- 导入后 revision 为 `0017_market_morning_content_reports`、业务表为 33 张、
  `alembic_version.version_num` 为 `varchar(64)`；
- database 排序规则为 `utf8mb4_ja_0900_as_cs`；
- 12 项业务/并发 acceptance 与 3 项 destructive migration rehearsal 全部通过。

这是本地真实 MySQL 8 工程证据，不替代部署环境的 TLS、备份、最小权限、连续 staging 或 T1 签字。

## 全新空库执行

先确认目标 database 是空库，再运行：

```bash
mysql --host=<host> --port=3306 --user=<user> --password \
  --database=<database> < database/market-morning/mysql/market_morning_schema_0017.sql
```

执行只读验收：

```bash
mysql --host=<host> --port=3306 --user=<user> --password \
  --database=<database> < database/market-morning/mysql/market_morning_verify_0017.sql
```

验收重点：

- `schema_revision` 必须是 `0017_market_morning_content_reports`
- `market_morning_table_count` 必须是 `33`
- `alembic_version_column_type` 必须至少是 `varchar(64)`
- `session_time_zone` 应为 `+00:00`
- database 字符集应为 `utf8mb4`
- database 排序规则建议为 `utf8mb4_ja_0900_as_cs`

## 已有数据库升级

不要对已有 Market Morning 数据库执行完整 bootstrap SQL。

如果你已经执行过 `market_morning_schema_0016.sql`，先确认：

```sql
SELECT version_num FROM alembic_version;
```

结果精确为 `0016_market_morning_model_usage` 时，可备份后执行：

```bash
mysql --host=<host> --port=3306 --user=<user> --password \
  --database=<database> < database/market-morning/mysql/market_morning_upgrade_0016_to_0017.sql
```

随后执行 `market_morning_verify_0017.sql`。其他已有库应配置
`VIBE_MARKET_MORNING_DATABASE_URL` 后从仓库根目录运行 Alembic：

```bash
alembic -c agent/alembic-market-morning.ini upgrade head
```

应用启动后还会严格检查 `alembic_version` 是否等于当前 revision；手工只创建业务表但没有正确 revision 会被 readiness 拒绝。
