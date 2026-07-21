# Market Morning｜Telemetry 事件字典与脱敏规则

实现 source of truth：`agent/src/market_morning/telemetry.py`。事件只接受显式名称和事件级属性白名单，不提供 catch-all metadata。

## 通用字段

| 字段 | 规则 |
|---|---|
| `event_id` | 服务端 UUID |
| `event_name` | 仅允许下表注册名称 |
| `user_id` | 可空；存在时必须是服务端已验证 UUID |
| `request_id` | 可空；最多 64 字符的安全 token，不接受自由文本 |
| `schema_version` | 当前固定为 `1` |
| `properties` | 仅允许对应事件的标量白名单 |
| `occurred_at` | UTC `DATETIME(6)` |

## 事件及属性

| 事件 | 触发时机 | 允许属性 | 明确禁止 |
|---|---|---|---|
| `signup_completed` | 邀请接受并建立用户 | `timezone=Asia/Tokyo` | email、external subject、invite token |
| `issuer_search_performed` | 完成一次搜索 | `matched`、`query_kind`、`query_length`、`result_count` | 搜索原文、结果公司名 |
| `watchlist_added` | 活跃关注成功增加 | `active_count`、`operation_status=added` | issuer 名称、私有标签 |
| `watchlist_removed` | 活跃关注成功移除 | `active_count`、`operation_status=removed` | issuer 名称、私有标签 |
| `watchlist_updated` | 排序或标签更新 | `active_count`、`operation_status=updated` | 标签文本、排序明细 |
| `settings_updated` | 设置变更 | `email_opt_in`、`timezone=Asia/Tokyo` | email、provider identity |
| `consent_recorded` | 风险/数据说明接受或撤销 | `consent_type`、`consent_version`、`status` | 文档正文、IP、UA |
| `account_deletion_requested` | 删除请求原子创建 | `request_status=pending` | 删除原因自由文本、subject |
| `edition_opened` | 私有朝刊打开 | `edition_date` | edition payload、关注股列表 |
| `source_opened` | 已验证 citation 打开 | `source_kind` | URL、标题、source record ID |
| `delivery_clicked` | 邮件/站内深链点击 | `channel=email/web` | token、邮箱、provider message ID |

## 属性约束

- 属性只能是 boolean、有限范围数字或最多 64 字符的短字符串。
- `active_count`、`query_length`、`result_count` 必须是非负整数。
- enum 属性只接受代码中列出的固定集合。
- `consent_version` 和 `edition_date` 只接受安全标识符字符。
- 未注册事件、额外属性、UUID 伪造、控制字符、非有限数字或超范围数字必须在写库前拒绝。
- telemetry 与业务修改共用事务；业务回滚时不能留下成功事件。

## 永久禁止进入 telemetry

完整邮箱、bearer/deep-link/invite token、external subject、搜索原文、个人备注、关注股私有标签、来源 URL、来源标题、模型 prompt/response、provider request ID、原始 webhook body、数据库 URL、异常消息和任何 secrets。
