# Market Morning｜T1 私测运行手册

> 状态：准上线准备文档。只有完成真实 MySQL、OIDC、授权数据源、生产模型/邮件 adapter、
> staging 连续 5 个日本交易日演练和日本合规审查后，才允许打开 T1 邀请制私测开关。

## 1. 值班入口与权限边界

内部运营页面：

```text
/market-morning-ops
```

页面顶部的“デプロイ準備”直接显示静态预检状态与稳定阻断码；`blocked` 的 HTTP 503 是可读业务
状态，不应误判为页面宕机。401、网络失败或其他异常仍按内部控制面故障处理。

内部 API：

```text
GET    /market-morning/_internal/ready
GET    /market-morning/_internal/deployment-preflight
GET    /market-morning/_internal/operations/summary?hours=24&recent_run_limit=10
GET    /market-morning/_internal/operations/metrics?hours=24
GET    /market-morning/_internal/event-briefs?review_status=auto_validated&limit=20
POST   /market-morning/_internal/event-briefs/{brief_id}/review
GET    /market-morning/_internal/issuer-aliases?review_status=pending&limit=50
POST   /market-morning/_internal/issuer-aliases/{alias_id}/review
GET    /market-morning/_internal/event-merge-candidates?review_status=pending&limit=50
POST   /market-morning/_internal/event-merge-candidates/{candidate_id}/review
GET    /market-morning/_internal/content-reports?report_status=pending&limit=50
POST   /market-morning/_internal/content-reports/{report_id}/review
POST   /market-morning/_internal/operations/halts
DELETE /market-morning/_internal/operations/halts/{edition_date}
```

- 所有内部接口使用独立运营 Bearer/OIDC adapter；产品用户 OIDC 不能访问运营接口。
- `VIBE_MARKET_MORNING_ADMIN_AUTH_FACTORY` 必须返回稳定 `actor_reference` 和最小权限：只读看板/
  readiness/metrics 为 `operations.read`，EventBrief、别名、跨来源候选和内容报告审核为
  `content.review`，停发/撤销为 `publication.control`，邀请和用户停用/恢复为 `access.manage`。
- 未配置 factory 时仅为本地开发和迁移兼容回退到 Vibe API Key，审计记录
  `vibe-api-key-operator`；deployment preflight 会以 `admin_auth_factory_missing` 阻止该配置进入
  staging/T1。生产必须使用能识别具体人员、支持角色撤销和 session 失效的 adapter。
- 页面和 API 不返回个人备注、用户 ID、邮箱、deep-link token、模型原文、source payload 或来源 URL。
- 不允许通过浏览器控制台、SQL 客户端或临时脚本直接修改迁移管理的业务表。

`operations/metrics` 返回 OpenMetrics 1.0，必须由监控系统以具备 `operations.read` 的服务身份在私有网络
抓取，不得放到公网匿名 endpoint。`market_morning_operations_health` 的值为 `0=healthy`、
`1=warning`、`2=critical`；实际告警以 `market_morning_operations_alert_count{code,severity}`
为准。窗口计数都是 gauge，不能按单调 counter 计算 rate。指标只带稳定聚合标签，不包含用户、
run ID、刊期、URL、payload、halt 原因或 provider request ID。

## 2. 每个日本交易日 07:00 JST 前检查

### 06:20–06:30｜启动条件

Compose 部署只有显式选择 `market-morning` profile 才会启动常驻 runtime：

```bash
docker compose --profile market-morning up -d market-morning-runtime
```

默认 profile 不会启动 worker/scheduler。不得通过增加 restart 次数绕过配置或外部 preflight 失败。
该 service 已禁用共享 API 镜像的 `:8899/live` healthcheck；worker 不监听 HTTP 端口，值班判断应以容器
进程状态、启动日志中的稳定错误码、runtime preflight 和运营指标为准，不得因缺少 API health 状态重启它。

1. `ready` 必须为 `200 / ready`，数据库 schema 必须精确为当前 runtime revision。
2. `deployment-preflight` 必须为 `200 / configuration_ready` 且 `blocking_checks=[]`。该结果仅证明
   静态配置和数据库 Gate，固定 `counts_as_t1_evidence=false`，不能作为 T1 或 staging 成功证据。
3. runtime flag、内置 production factory、部署 provider bundle、JPX/US calendar provider 均为生产配置；出现
   `fixture_` provider 时立即停发。
4. runtime 必须在创建 worker 前完成 `licensed_sources`、`market_snapshots`、`event_brief_model`、
   `source_reachability`、`email_delivery` 五个 preflight；失败／超时不得绕过。
5. runtime preflight 只允许无副作用探测；若 staging 证据显示探针发送邮件、调用计费模型、推进 cursor 或
   写业务数据，视为 provider bundle/preflight 不合格。
6. scheduler 应已在 06:00 JST 为 provider bundle 明确声明的每个 licensed source 创建唯一
   `source-ingestion:{edition_date}:0600:{provider}` job；缺少 provider、出现 `fixture_` 或同日 payload
   漂移时立即按配置错误处理。
7. 运营摘要中的 active halt 不得与当日计划冲突；如果是有意停发，记录事件单号和原因。
8. 日本交易日历不可用时按 fail closed 处理，不能人工强制改成交易日。

### 06:30–06:50｜采集完成、EventBrief 与快照

1. 06:00 来源 job 已完成，TDnet、EDINET 和已批准公司 IR feed 的最新状态为 `healthy`；失败 job
   可以按固定退避重试，但不得通过手工改 cursor 或复制内容绕过。
2. Nikkei 225、S&P 500、Nasdaq Composite、DJIA、USD/JPY 五项快照 job 均进入
   `succeeded`；任一缺失、交易日错位或重复候选都不能发布。
3. 美国休市不是错误：朝刊必须引用最后有效美国交易日，并明确显示休市上下文。
4. 日本休市时仍可采集事件，但不创建可发布朝刊或邮件。

### 06:50–07:00｜内容与发布

1. `blocked` EventBrief 必须能由脱敏 error code 解释；不得把 blocked 内容人工复制进朝刊。
2. `auto_validated` 可进入朝刊；高风险或抽样内容可在运营页批准或拒绝。
3. 全局 run 必须通过五项市场快照 Gate。07:00 未完成时等待既定 07:15、07:30、08:00、
   08:30 槽位，不创建新的随意 generation key。
4. 08:30 后首次成功只能站内 `late` 发布，禁止补发邮件。

## 3. 当前告警阈值

| 告警码 | 当前触发条件 | 级别 | 处置 |
|---|---|---|---|
| `source_error` | 任一来源的最近结果为 error | critical | 判断是否单源隔离；覆盖不足则 partial 或停发。 |
| `source_never_run` | 任一配置来源从未运行 | warning | T1 前视为配置错误；fixture 演练需说明。 |
| `job_failures` | 24 小时窗口内任一 job 为 failed | critical | 按失败码分类，确认是否仍有重试槽位。 |
| `event_brief_blocked` | 24 小时内有 blocked brief | warning | 检查 schema、引用、禁止词或来源可达性。 |
| `delivery_failures` | 24 小时内有邮件失败 | warning | 不重复创建同日 attempt；只走既有幂等重试。 |
| `cost_observability_no_invocations` | 24 小时内没有模型调用记录 | warning | 确认是否确实无事件；不能用零调用证明成本可持续。 |
| `model_usage_missing` | 模型已调用但 provider usage 缺失 | warning | 检查 generator adapter；不得用估算 token 填补。 |
| `model_cost_missing` | 已记录调用但没有实际定价成本 | warning | 接入 provider 回执或批准的版本化 rate card；不得跨币种合计。 |

当前阈值采取“任何一次失败即告警”的保守策略。积累 20 个有效日本交易日后，才允许基于真实
基线调整阈值；不能为了让看板变绿而放宽。

上线前必须在 staging 完成一次真实 scrape 和通知链演练：分别构造 warning 与 critical，验证
告警触发、恢复和静默规则；保存抓取时间、规则版本、告警码与通知结果，不保存完整 metrics body
或认证 header。没有这份证据时只能认定“指标出口已实现”，不能认定“生产告警已接通”。

仓库提供受版本控制的部署基线：

- `deploy/market-morning/monitoring/market-morning.rules.yml`：把稳定 warning/critical 条件码直接转成
  告警，并检测 scrape down、snapshot missing 和超过 10 分钟的 stale snapshot；
- `deploy/market-morning/monitoring/prometheus-scrape.example.yml`：固定 HTTPS、24 小时窗口和
  `credentials_file`，不允许把 bearer token 写入 YAML 或 URL。
- `deploy/market-morning/monitoring/alertmanager.example.yml`：warning/critical 独立路由，critical
  首次通知不等待、重复间隔更短；两个 receiver 都使用 `url_file` 且发送 resolved 通知。默认
  `market-morning-unmatched` receiver 不发送任何内容，避免这份专用基线把其他服务告警误投递。

部署时必须把示例 target 替换成 staging 内部地址，挂载仅有 `operations.read` 的独立机器身份 token，
并由 secret sidecar 或平台机制更新 token 文件；不得复用个人运营 token。Prometheus reload 前运行
`promtool check rules /etc/prometheus/rules/market-morning.rules.yml`，Alertmanager reload 前运行
`amtool check-config /etc/alertmanager/alertmanager.yml`。规则和配置通过仓库结构/隐私合同测试并不等于
真实服务已加载，也不替代 warning、critical、resolved 三段通知链的 staging 证据。

真实演练完成后，复制
`docs/evidence/market-morning/monitoring-drill.template.json`，填入同一不可变 release、规则／配置
SHA-256、不可逆 receiver reference SHA-256、时区明确的起止时间和每一步原始 artifact SHA-256。
模板固定为 `failed`，不能直接计数。输入不得包含 webhook URL、token、header、个人姓名、消息正文或
完整 metrics body。然后运行：

```bash
vibe-trading-market-morning-monitoring-drill \
  --input docs/evidence/market-morning/monitoring-drill-raw-YYYY-MM-DD.json \
  --output docs/evidence/market-morning/monitoring-drill-YYYY-MM-DD.json
```

只有 Prometheus rule 与 Alertmanager route 已真实加载、warning/critical 的 firing 和 resolved 四段均
送达、silence 到期后恢复通知、并有两名值班人员确认时，报告才允许
`status=passed`、`counts_as_staging_observability_evidence=true`。失败报告仍写出且退出码为 `1`；不能
删除失败演练后只保留绿色结果。每日 staging manifest 的 `observability` artifact 必须引用该规范化
报告的 SHA-256，而不是引用配置文件或单元测试输出。

## 4. 失败分类与重跑条件

| 类型 | 典型信号 | 是否可重跑 | 操作 |
|---|---|---|---|
| 数据库/认证 | readiness 非 ready、deployment preflight blocked、schema mismatch、401/403 | 修复配置后 | 保持 runtime 关闭，先恢复 readiness 和静态预检；禁止绕过认证。 |
| 来源暂时失败 | timeout、5xx、rate limit | 是 | 让 durable job 按原 idempotency key 重试；不推进失败 cursor。 |
| 来源永久失败 | 文档无效、host 不在白名单、许可撤回 | 否 | 隔离 feed；覆盖不足明确显示，必要时停发。 |
| 市场快照 Gate | 缺项、交易日错位、重复候选 | 修复数据后 | 等待下一固定 slot；08:30 后只允许站内 late。 |
| 模型暂时失败 | `event_brief_model_unavailable` | 在 max attempts 内 | 使用同一 EventBrief generation key 重试。 |
| 内容 Gate | schema、无引用、禁止词、数字/日期无证据 | 不直接重跑 | 修复 prompt/model 后生成新版本；原 blocked 记录保留。 |
| worker 中断 | running lease 过期 | 是 | 恢复 worker；由 lease recovery 接管，不能手改成功状态。 |
| 邮件 provider 超时 | sending/failed + 脱敏错误码 | 在原 attempt 内 | 使用已有 attempt/provider 幂等键；不得创建第二封。 |

重跑前必须同时满足：根因已修复、原业务对象仍有效、重试次数未耗尽、仍在允许发布窗口。
任何一项不满足时停发或降级，不直接修改 `status`、`attempt_count`、payload hash 或 cursor。

## 5. 手动停发

运营页的“刊行停止コントロール”只能停发，不能强制公开。

允许的原因：

- `source_incident`
- `market_data_incident`
- `content_quality_incident`
- `delivery_incident`
- `operator_review`

操作流程：

1. 选择 JST 刊期和原因，执行“停発する”。
2. 刷新运营摘要，确认 active halt 已出现；数据库审计应记录创建动作。
3. 根因消除且二人复核后执行“停発を解除”；确认审计记录撤销动作。
4. 停发解除不会自动补发已经错过 08:30 的邮件，也不会把休市日改成交易日。

## 6. EventBrief 批准、下架与修订

- `approve` 只接受 `published + auto_validated`，仅把数据库 review 状态改为 approved；不可变
  payload 和 SHA-256 不变。
- `reject` 可下架 pending、auto-validated 或已批准内容；后续朝刊与个股研究查询会排除 rejected。
- 已 rejected 的 brief 不能原地重新批准。修复模型、prompt 或证据后生成新的 EventBrief 版本。
- 每个真实变更写 `mm_audit_logs`，只包含前后状态、固定操作者引用和枚举原因码，不保存模型原文。
- 官方开示的 corrected/withdrawn 状态仍由来源事件版本链决定；运营审核不能伪造官方撤回。

### 6.1 发行人别名与跨来源候选审核

- 别名批准只使用 `verified_company_name`；拒绝使用 `ambiguous_alias`、`wrong_issuer` 或
  `unsupported_source`。过期别名和已由其他发行人占用的规范化别名必须返回 conflict，不能用
  手工 SQL 绕过唯一性。
- 别名审核终态不可原地翻转；判断变化时应由受控主数据流程创建新的候选记录并保留旧审计。
- 跨来源候选批准只使用 `same_disclosure_event`；拒绝使用 `distinct_events` 或
  `insufficient_evidence`。批准前系统会锁定并复核两条事件仍属于同一 issuer。
- **批准候选不会自动合并事件。** 它只记录运营判断，不修改 `mm_normalized_events`、
  `mm_event_sources`、EventBrief 或已发布朝刊。未来若实现实际合并，必须另行设计版本链、影响查询、
  回滚和审计，不能复用本按钮静默改数据。
- 两个队列都不展示内部 source reference、来源 URL、正文或 payload；不得为方便审核把这些字段
  临时加入响应或普通工单。

## 7. 用户支持、退订与删除

1. 邮件退订通过产品设置将 `email_opt_in=false`；之后资格 Gate 必须立即排除该用户。
2. 用户可在设置页下载认证后的 JSON 数据导出。导出包含用户自有内容，但不包含 deep-link/invite
   hash、provider message ID 或 job payload；服务端不另存导出文件。
3. 账户删除入口在同一事务创建 `pending` 请求并排入 `account_deletion` durable job。worker 清除
   用户私有数据、解除 analytics/audit 用户外键、匿名化 external subject，并把申请置为
   `completed`；不得用手工 SQL 跳过 job、审计或状态机。
4. 删除请求必须带 provider 证明的 5 分钟内近期认证。事务提交时用户立即变为
   `deletion_pending`、email opt-in 关闭且活动资格清空；普通 API 随即 403。删除 endpoint 允许同一
   pending 用户幂等重试并返回原 request ID，不能为此恢复其他数据访问。
5. 删除完成后保留的只有全市场官方来源、normalized event、删除申请和不再指向自然人的必要审计。
   产品 Bearer、近期认证、内置 OIDC/JWKS 验签/轮换和应用内即时失效边界已完成；远程 MySQL 删除/
   并发与迁移 probe 已通过，但真实 OIDC provider 的 `auth_time`、token/session 撤销和前端 re-auth
   仍未完成，因此 T1 前仍不得声称流程已在生产身份上验收。
6. 支持工单只能引用内部 request/edition/delivery ID 和脱敏错误码；不能把邮箱、备注、token 或
   原始 provider payload 复制到普通日志或聊天工具。

### 7.1 私测邀请与停用

1. 在 `/market-morning-ops` 输入 1–30 天期限并发行邀请；原始 token 只显示一次，立即通过批准的
   私密渠道交付，禁止粘贴到普通日志、工单或群聊。
2. 未使用邀请可按 invite ID 撤销；accepted/expired 邀请不能原地复用，应新建邀请。
   用户侧接受入口为 `/market-morning/private-beta/invitations/accept`，subject 只能来自已验签 OIDC；
   当前部署未配置内置 OIDC 或审核后的自定义 factory 时必须保持 503，禁止用 API Key 或客户端
   user ID 代替。
3. 用户停用必须选择枚举原因；停用会关闭产品资格、email opt-in 和最近活动，不能代替数据删除。
4. 恢复只恢复账户访问，不自动重新打开邮件订阅。操作审计必须记录生产 adapter 返回的个人
   `actor_reference`；上线验收还需证明角色撤销和 session 失效即时生效。

## 8. Staging 演练矩阵

以下场景必须使用合成 fixture 或获得明确批准的窄范围数据逐项执行，并保存日期、run ID、预期、
实际结果和审计证据：

每次部署候选版本先从仓库根目录执行本地 T0 自动门禁：

```bash
PYTHONPATH=agent .venv/bin/python -m src.market_morning.rehearsal_cli \
  --environment local-development \
  --output docs/evidence/market-morning/t0-local-synthetic-YYYY-MM-DD.json
```

已安装项目也可以执行 `vibe-trading-market-morning-rehearsal`。退出码 `0` 只表示 12 个合成 probe
全部通过；退出码 `1` 表示至少一个 probe 失败，但 runner 仍会写完整报告并继续其他场景。证据文件
禁止包含 stdout/stderr 原文，只允许保存输出 SHA-256。任何本地报告都固定标注
`counts_as_staging_day=false`，不得计入下方连续 5 日 staging 要求。当前参考证据为
[`t0-local-synthetic-2026-07-21.json`](./evidence/market-morning/t0-local-synthetic-2026-07-21.json)，
结果 12/12 通过。

真实 MySQL 的首批验收使用 `vibe-trading-market-morning-mysql-acceptance`，只能指向名称匹配
`market_morning_acceptance*` 的专用数据库并显式确认写入。manifest 固定为
`scope=real_mysql_acceptance`、`counts_as_staging_day=false`，且不会保存连接 URL 或 pytest 原文。
当前十二项覆盖 schema/session（包括 access-token hash 的原文不落库、精确注销、不可复活与多 token
隔离）、来源 revision/cursor、用户朝刊版本、job 写入和 `SKIP LOCKED`
领取、scheduler 租约、publication halt 并发审计、global run 唯一 current success、EventBrief 与
model usage 幂等、邮件 attempt 并发、webhook 重复／乱序和删除事务；它尚不包含真实迁移／
downgrade 证据，因此即使十二项通过也不能单独解除下方 MySQL/T1 Gate。

破坏性迁移证据必须由独立的
`vibe-trading-market-morning-mysql-migration-rehearsal` 生成。它只接受名称匹配
`market_morning_migration_acceptance*` 的可丢弃数据库，拒绝普通 acceptance／生产库，并要求
`--confirm-destructive-reset-of-dedicated-database`。三项场景覆盖空库到 head、0016 数据升级到
0018，以及 0018 降级 0016 后再升级；该报告固定
`contains_destructive_migration_evidence=true`、`counts_as_staging_day=false`。未实际运行并获得
3/3 passed 前，迁移 Gate 保持未完成。2026-07-21 本地隔离 MySQL 8 与远程专用验收目标均已得到
12/12 + 3/3；远程 hash-only 证据位于
`mysql-acceptance-remote-2026-07-21.json`、`mysql-migration-remote-2026-07-21.json`。两个测试库均已
恢复到 `0018`/34 表；生产主库仍为 `0004`/14 表，禁止在无备份、维护窗口和显式变更批准时升级。

### 7.3 产品主库受控升级

产品主库只能通过独立的 production schema change job 升级，不能把 acceptance URL 临时替换成产品
URL，也不能复用会执行 `downgrade base` 的 migration rehearsal。执行顺序固定为：

1. 用 `--check-only` 留存当前 target fingerprint、revision 和 `mm_*` 表数；
2. 生成并验证可恢复备份，保存备份证据文件的 SHA-256；
3. 停止 Market Morning API、worker、scheduler，确认部署环境
   `VIBE_MARKET_MORNING_RUNTIME_ENABLED=false`；
4. 在批准的维护窗口运行 `vibe-trading-market-morning-production-schema-change`，传入精确旧 revision、
   备份证据 hash、变更单 ID 和四项显式确认；
5. 只在 manifest 为 `passed`、revision 为 `0018_market_morning_auth_sessions` 且表数为 34 时继续
   readiness/preflight；随后再恢复 runtime。

任何 source revision 漂移、Alembic 非零退出、postflight 不可用或表集合不完整都保持 runtime 关闭。
命令固定 `automatic_downgrade_attempted=false`；恢复或 downgrade 必须是新的独立审批，不在失败路径
中自动执行。

每个真实 staging 日本交易日结束后，deployment-owned evidence job 必须输出一个严格
`scope=market_morning_staging_day` 报告。报告只保存 release/schema、JPX open-session 链、稳定
状态码和各 Gate artifact 的 SHA-256；不得保存 token、header、邮箱、来源正文、模型原文、metrics
body 或 provider 响应。通过日要求 11 项 Gate 全绿、06:30–08:30 JST 完整发布、至少一次实际模型
usage/cost，且明确 `contains_fixture_data=false`、`contains_synthetic_data=false`。失败日仍要保存稳定
failure code，但必须 `counts_as_staging_day=false`。

`oidc_session` 不允许直接对任意日志传入 `--status passed`。先从
`docs/evidence/market-morning/oidc-staging.template.json` 复制一份到受控证据目录，
再用同一 release revision、run ID、刊期和开始／结束时间填写真实 Auth0 演练结果。
通过证据必须同时包含下列十项检查：

- Operator 登录、角色授权和受保护 API 访问；
- Operator 精确 token 注销，同一旧 token 再访问必须被拒绝；
- Product 登录、私测邀请 onboarding 和关注股跨会话持久化；
- Product 精确 token 注销，同一旧 token 再访问必须被拒绝。

Auth0 API 的 `configured_max_access_token_lifetime_seconds` 必须精确为 `900`，两个 SPA
实际观察到的 token lifetime 必须为 `1–900` 秒，session mode 必须为
`access_token_sha256`。tenant、两个 client 和 audience 只保存 SHA-256 引用，Product
和 Operator client 引用必须不同。原始证据和汇总 JSON 均不得包含 access/
refresh/ID token、Authorization header、OAuth code/PKCE verifier、邀请 token、邮箱、
Auth0 subject 或其他个人身份。

真实演练统一使用 `vibe-trading-market-morning-oidc-staging-probe`，不要再手工填写
passed JSON。执行前必须同时满足：

- 输入一个已通过且与待部署 staging 完全同 revision 的 release-candidate manifest；
- Operator access token 来自独立 Operator SPA，账号至少具有
  `market-morning-access-admin` 角色，以同时证明 `operations.read` 与 `access.manage`；
- 两个 Product access token 来自同一个新私测用户、同一个 Product SPA，但必须是两个不同
  token 实例；第二个 token 用于证明第一个 token 注销后，跨会话关注股仍存在；
- 三个 token 只能放在互不相同的 owner-only 普通文件中（`0600`、单硬链接、非 symlink），
  不得放进命令行、环境变量、日志、工单或仓库；
- `--api-base-url` 必须是非 localhost 的 HTTPS staging 地址，`--issuer-id` 必须是 staging
  IssuerMaster 中已存在的 UUID v4；刊期必须等于执行开始时的 JST 日期；
- API 的 Maximum Access Token Lifetime 已保存为 900 秒，并在保存后重新登录取得全部新 token。
- 自定义 API 已启用 `Allow Offline Access`，Product/Operator 两个 SPA 已启用
  `Allow Refresh Token Rotation`；至少完成一次 access token 过期后的静默续期验证。

由受控凭据引导任务写入上述三个 token 文件后，运行：

```bash
vibe-trading-market-morning-oidc-staging-probe \
  --release-candidate /secure/evidence/release-candidate.json \
  --api-base-url https://staging.example.invalid \
  --api-audience https://api.example.invalid/market-morning \
  --run-id staging-YYYY-MM-DD \
  --edition-date YYYY-MM-DD \
  --issuer-id 00000000-0000-4000-8000-000000000000 \
  --product-token-file /secure/volatile/product-a.token \
  --product-secondary-token-file /secure/volatile/product-b.token \
  --operator-token-file /secure/volatile/operator.token \
  --output /secure/evidence/oidc-staging.json \
  --confirm-staging-side-effects
```

该命令会真实创建一份邀请、接受私测、写入一只关注股，并精确撤销 Operator token 与第一个
Product token；因此 `--confirm-staging-side-effects` 是强制参数。输出只包含严格合同允许的状态、
稳定 failure code 和 SHA-256 引用。失败时也保存脱敏证据但退出码为 1；不安全的本地输入在发出
网络请求前退出码为 2。执行后立即安全销毁三个 token 文件，不删除失败证据。

生成 `oidc_session` artifact 时仍使用
`vibe-trading-market-morning-staging-gate-artifact`；CLI 会先解析上述严格合同，再校验
release/run/刊期/时间/status 与 `--provider-id auth0`，任一不匹配都拒绝生成
passed artifact。仓库模板永久是 `failed` 且 `counts_as_staging_oidc_evidence=false`，
不能作为真实演练证据。

从本次 release 的首个 staging 日起，把全部单日报告交给
`vibe-trading-market-morning-staging-gate`；不要删除中间失败日或只挑选绿色报告。聚合器只认最新
连续 streak：JPX previous/next open-session 链断裂、失败日、release 或 calendar provider 切换都会
重新计数。输出同时满足 `status=passed`、`counts_as_t1_evidence=true`、
`trailing_consecutive_days>=5` 才能进入 T1 签字；历史曾通过但最新一天失败时立即回到
`not_ready`。T0、普通 MySQL acceptance 和 migration rehearsal scope 均会被拒绝，不能计数。

| 场景 | 必须证明 |
|---|---|
| 标准交易日 | 07:00 前完整发布，同日只有一个 current success 和一封应用层提醒。 |
| 美国休市 | 使用最后有效美国交易日，朝刊仍可发布且状态明确。 |
| 日本休市 | 不发布朝刊/邮件，来源采集仍可执行。 |
| 单源失败 | 失败隔离、cursor 不前进、覆盖不足不伪装成“无新事件”。 |
| 市场数据失败 | 五项 Gate 阻止发布，不发送空邮件。 |
| 模型失败 | 重试后安全 degraded/blocked，不影响其他有来源事件。 |
| worker 重启 | lease 到期恢复，job/attempt 不重复。 |
| 邮件超时 | 原 attempt 幂等重试，同日不生成第二封。 |
| EventBrief 下架 | rejected 立即从新生成朝刊和个股研究中排除，历史审计保留。 |
| 手动停发 | scheduler 尊重 halt；撤销不绕过日历、质量或 08:30 规则。 |
| 邀请与停用 | token 只显示一次且库内只有 hash；停用后访问和邮件资格立即失效。 |
| 导出与删除 | 导出不含内部 secrets；并发删除只完成一次且私有数据不可恢复。 |

连续 5 个 staging 日本交易日全部满足预期后，才能申请 T1 签字。

## 9. T1 Gate 签字表

| Gate | 自动证据 | Staging/外部证据 | 当前状态 |
|---|---|---|---|
| 公司搜索与 10 只上限 | issuer/watchlist 测试 | 许可主数据实测 | 外部待完成 |
| 日美日历不对称 | calendar/scheduler 测试 | 生产 calendar provider | 外部待完成 |
| 来源去重、订正、撤回 | ingestion/normalization 测试 | 授权来源窄范围演练 | 外部待完成 |
| 每条事实可回来源 | EventBrief/edition 测试 | 人工抽样 | 部分完成 |
| 失败隔离与降级 | source/brief/global-run 测试 | 故障演练 | 部分完成 |
| 邮件幂等 | delivery/webhook/HTTP callback 测试 | 生产 provider timeout、签名与重放演练 | 部分完成 |
| 非活跃用户降级 | auth/eligibility/user-edition + 内置 OIDC/JWKS/角色测试 | 生产 provider session 撤销与订阅状态演练 | 部分完成 |
| 运营审核与停发 | operations/admin 测试 | 双人值班演练 | 部分完成 |
| 隐私、导出与删除 | product-auth/settings/privacy/worker + 远程 MySQL 12+3 | OIDC re-auth/provider session invalidation | 部分完成 |
| 成本可持续 | model adapter + usage/cost 幂等记录与聚合测试 | 实际 provider usage/cost 和 5 日单位成本 | 部分完成 |

未完成项不能通过口头说明、免责声明或手工 SQL 代替。T1 开关在所有 Gate 签字前保持关闭。

签字表全部完成后，仍必须运行总发布 Gate，禁止只引用其中一份绿色报告：

```bash
vibe-trading-market-morning-t1-release-gate \
  --mysql-acceptance /controlled/evidence/mysql-acceptance.json \
  --mysql-migration /controlled/evidence/mysql-migration.json \
  --staging-gate /controlled/evidence/t1-staging-gate.json \
  --external-signoffs /controlled/evidence/t1-external-signoffs.json \
  --output /controlled/evidence/t1-release-gate.json
```

外部签字模板位于
[`t1-external-signoffs.template.json`](./evidence/market-morning/t1-external-signoffs.template.json)，
原模板固定为 blocked，必须由各 owner 以实际批准材料 hash 和复核日期替换。总 Gate 还会确认两套
MySQL 验收目标互不相同、staging 与签字使用同一不可变 release、schema 精确为当前 head。只有
`status=approved` 且 `blocking_codes=[]` 的 hash-only 报告才允许进入最终变更审批；该报告本身不
替代部署审批，也不能授权公开 Beta、收费或扩展数据用途。
