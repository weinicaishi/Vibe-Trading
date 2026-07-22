# Market Morning MySQL 初始化文件

本目录把全新空库初始化到 `0018_market_morning_auth_sessions`，也支持把
revision 精确为 `0017_market_morning_content_reports` 的已有库增量升级到 0018。

## 文件

- `market_morning_schema_0018.sql`：全新空库完整建表 SQL，包含 34 张 `mm_` 业务表。
- `market_morning_upgrade_0017_to_0018.sql`：仅供 0017 已有库增量升级。
- `market_morning_verify_0018.sql`：只读检查 revision、表数、字符集、排序规则及时区。

## 前置条件

- MySQL 8.x、InnoDB、`utf8mb4`
- 推荐排序规则 `utf8mb4_ja_0900_as_cs`
- 应用连接使用 UTC
- 对已有库执行前已完成可恢复备份，并安排维护窗口

## 全新空库

```bash
mysql --host=<host> --port=3306 --user=<user> --password \
  --database=<database> < database/market-morning/mysql/market_morning_schema_0018.sql
```

## 从 0017 升级

先执行 `SELECT version_num FROM alembic_version;`，只有结果精确为
`0017_market_morning_content_reports` 时才可在备份后执行：

```bash
mysql --host=<host> --port=3306 --user=<user> --password \
  --database=<database> < database/market-morning/mysql/market_morning_upgrade_0017_to_0018.sql
```

最后执行 `market_morning_verify_0018.sql`，revision 必须是
`0018_market_morning_auth_sessions`，`mm_` 业务表必须为 34 张。
