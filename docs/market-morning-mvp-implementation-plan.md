# Market Morning MVP｜详细实施计划

> 状态：Sprint 0–7 的工程主体已形成，当前处于准上线缺口收口与外部 T1 Gate 验证阶段；完成度与证据见
> [MVP 完成度审计](./market-morning-mvp-completion-audit.md)。
> 本次文档校准所依据的本地不可变候选为 `e4a10ec7b1f41b14b8d6a3515397e721e60566fb`：无真实
> `.env` 的整仓后端 `6317 passed, 25 skipped`，前端 production build 与 `342 passed`，远程专用
> MySQL acceptance `12/12`、migration rehearsal `3/3`；九项 release-candidate 检查全绿，runtime
> schema 为 `0018_market_morning_auth_sessions`，16 个固定发布源文件和 5 份验证证据 hash 完整，
> manifest SHA-256 为 `8745fa64db9e02a85ffc5e4e4cf3ba2516031174b2157d8cbf268b2e74fe5b3c`。
> 远端发布线必须由 GitHub Actions 对最终同一 revision 独立复验；本地 manifest 不替代 CI。
> 本地/CI 证据均固定不计连续 staging 日或 T1 发布证据。产品主库仍为 `0004`
> 且未执行迁移。
>
> 上位规格：[Market Morning MVP v3](./market-morning-mvp-v3-xmind.md)。本计划不改变其中的产品边界；如有冲突，以 v3 为准，先更新 v3 再实施。

## 1. 执行结论

### 1.1 技术路线

保留 Vibe-Trading 作为研究与 API 工程底座，但新增一个独立的 `market_morning` 产品垂直层。它与现有回测、交易执行、券商连接、影子账户和通用 Agent 工作流隔离。

```text
React 产品界面
        │
FastAPI：Market Morning 私有 API
        │
Market Morning 应用服务
├── MySQL：账户、关注股、来源、事件、朝刊、投递、审计
├── Worker：采集、标准化、生成、发布、邮件任务
├── Source adapters：TDnet / EDINET / 公司 IR / 获授权行情
├── Content gate：事实、引用与非投顾措辞校验
└── Vibe 可复用能力：API 安全基础、市场数据规范化、前端工程、运行配置
```

### 1.2 不把现有模块直接当作产品能力

| 现有能力 | 处理方式 | 原因 |
|---|---|---|
| `scheduled_research` | 保留；不直接承载产品朝刊 | 当前为本地 JSON 存储、UTC cron，缺少 JPX 日历、租户、数据库锁与投递审计。 |
| `/market/indices` | 只复用 canonical model / provider policy 的思路 | 现有四指数仍受功能开关和数据授权限制，不可直接公开给用户。 |
| API Key 认证 | 仅保留为内部 API 保护 | 它不是注册用户、会话、删除请求或多租户权限模型。 |
| `channels/email.py` | 不直接复用为朝刊投递 | 它是收件箱轮询与回复通道，不具备产品化投递、退信与幂等记录模型。 |
| 回测、策略、券商、影子账户 | MVP 禁止依赖或暴露 | 会把研究辅助产品拉向交易建议与执行。 |
| TradingAgents | 不接入 MVP | 只在 V1.5 评审“深度研究模式”时，择取 checkpoint、模型抽象等模式；不引入交易决策链。 |

### 1.3 MVP 的交付终点

不是“功能全部写完”，而是达到 T0 工程就绪：

1. 私有邀请账户可添加 3–10 只日本股票并看见站内朝刊；
2. 每张事件卡都能回到至少一个原始来源；
3. 日本开市日的发布、失败、部分可用和邮件幂等都有可追溯记录；
4. T1 私测的所有数据与合规闸门已准备好，但公开 Beta 和付费不提前开启。

---

## 2. 先锁定的工程决策

以下是本计划采用的默认方案。它们不是不可变技术偏好；若确认时改变，需同步更新本计划、ADR 与验收项。

已接受的架构决策记录在 [ADR 索引](./adr/README.md)；数据权利与 owner 状态记录在
[数据来源登记表](./market-morning-data-source-register.md)，环境与 secrets 边界记录在
[环境矩阵](./market-morning-environment-matrix.md)，分析事件 contract 记录在
[Telemetry 字典](./market-morning-telemetry-dictionary.md)。

| 决策 | 默认方案 | 不能妥协的约束 |
|---|---|---|
| 产品数据库 | MySQL 8.x + InnoDB，独立于现有本地 SQLite/JSON 状态 | 生产数据不得落入本机 runtime 目录；必须支持事务、唯一约束、审计与并发 worker。数据库与连接会话统一使用 UTC。 |
| ORM 与迁移 | SQLAlchemy 2 async + Alembic，MySQL driver 使用 `asyncmy`（新依赖） | 所有产品表必须通过版本化迁移创建，不手工改生产表；迁移必须在目标 MySQL 版本执行验证。 |
| 用户认证 | 外部身份认证服务或自建 OIDC 适配层，支持邮箱验证 / 邀请制 | 不复用 Vibe API Key；应用只保存外部 subject、必要资料与同意记录。 |
| 后台任务 | 独立 worker + MySQL job 表 / 行锁；部署层以带租约的单活 scheduler 触发 | worker 使用 `SELECT ... FOR UPDATE SKIP LOCKED` 领取任务；scheduler 使用数据库租约表，任务必须可重试、幂等、可恢复。 |
| 时区与日历 | 存储 UTC，业务日期 `edition_date_jst` 明确为 JST；JPX 现货日历为发布真相 | 普通工作日 cron 不能决定是否发布。 |
| 邮件 | 可替换 `EmailDeliveryPort`，私测接一个事务型邮件服务商 | 邮件只含“已准备好”与私有链接；应用层幂等，不重复生成内容提醒。 |
| 内容生成 | 结构化 JSON 输出，事件级生成一次 | 没有可用来源时，不得生成事实；禁止交易指令词。 |
| 部署 | API、worker、MySQL、迁移 job 分离；生产接入 secrets / TLS / 日志平台 | 不将密钥写入 repo、前端或运行日志。 |

### 2.1 MySQL 专项约束

- 全库默认字符集使用 `utf8mb4`；日文搜索使用应用层生成的 `normalized_search_key`，并以确定性 binary collation 建索引，不把全半角、法人后缀或别名判断交给数据库 collation。
- 业务时间使用 UTC `DATETIME(6)`；数据库连接初始化为 UTC。`edition_date_jst` 单独使用 `DATE`，所有 JST/UTC 转换在应用服务中显式完成。
- MySQL 不依赖 PostgreSQL 式 partial unique index。软删除实体通过 generated active-key column + unique index，或单独 active relation，保证“仅活跃记录唯一”。
- 可变内容可以使用 MySQL `JSON`，但 issuer、状态、来源 ID、发布时间、幂等键等检索字段必须是普通列；不能把核心查询埋在 JSON 内。
- scheduler 不使用连接级 advisory lock 作为唯一正确性保障；使用带 `owner_id`、`lease_until`、`heartbeat_at` 的 `mm_scheduler_leases` 表，过期后才能安全接管。

### 2.2 确认前的四个外部 Gate

这些事项不是工程团队能用代码绕过的。Sprint 可以并行准备接口和 fixture，但不得把未获许可的真实数据暴露给外部用户。

1. **TDnet**：确认 API 合同、读取/缓存/展示/AI 处理/再分发边界与凭据开通。
2. **EDINET**：确认 API key、使用条款、频率和文档回链范围。
3. **JPX IssuerMaster 与行情/汇率**：确认主数据、指数、外汇的公开展示与再分发权利。
4. **日本法律审查**：在真实收费前审查排序、文案、邮件和订阅流程，确认不构成需要登记的投资建议服务。

若其中任一项未完成：仅允许使用合成 fixture、内部开发或经明确许可的窄范围数据；不得以“Beta”名义绕过。

---

## 3. 目标代码边界与目录

新增内容集中在以下边界，避免把 SaaS 逻辑渗入既有交易研究内核。

```text
agent/
├── migrations/market_morning/                 # 新：Alembic migrations
├── src/
│   ├── market_morning/                        # 新：产品领域根目录
│   │   ├── db.py                              # Session、transaction、health check
│   │   ├── models/                            # SQLAlchemy 实体
│   │   ├── repositories/                      # 查询与唯一约束边界
│   │   ├── services/                          # 账户、watchlist、edition、delivery
│   │   ├── sources/                           # TDnet、EDINET、IR、行情 adapters
│   │   ├── pipeline/                          # cursor → record → event → brief → edition
│   │   ├── calendar/                          # JPX/US 日历与覆盖
│   │   ├── content/                           # schema、prompt、policy validation
│   │   ├── jobs/                              # durable jobs、worker、locks、retry
│   │   ├── admin/                             # 审核、运营与审计服务
│   │   └── telemetry/                         # 私测事件、成本、指标
│   └── api/
│       ├── market_morning_routes.py           # 新：用户私有 API
│       └── market_morning_admin_routes.py     # 新：后台 API
└── tests/
    ├── market_morning/                        # 新：领域、API、集成、fixture 测试
    └── contract/                              # 新：source / provider 契约测试

frontend/src/
├── features/marketMorning/                    # 新：产品页面、状态、API client
├── pages/MarketMorning*.tsx                   # 新：朝刊、关注股、个股、设置、后台
└── i18n/locales/ja.json                       # 增量：仅 Market Morning 文案
```

允许修改的既有入口只限：

- `agent/api_server.py`：注册两个新 route module 与生命周期 hook；
- `agent/src/config/*`：新增产品配置 schema，不更改旧功能默认行为；
- `frontend/src/App.tsx` 或现有 router：注册私有产品路由；
- 部署编排：增加数据库、migration 和 worker 服务。

禁止在 MVP 中修改：`agent/src/trading/`、`agent/src/live/`、券商 connector、影子账户、策略回测执行语义。现有 A 股与指数功能必须回归通过。

---

## 4. 数据模型与关键不变量

### 4.1 第一版数据库实体

| 域 | 表 / 聚合 | 关键唯一约束或索引 |
|---|---|---|
| 身份 | `mm_users`、`mm_user_consents`、`mm_audit_logs` | `external_subject` 唯一；审计记录仅追加。 |
| 证券主数据 | `mm_issuer_snapshots`、`mm_issuers`、`mm_issuer_aliases` | 证券代码 + 生效区间；批准别名的规范化值唯一。 |
| 关注 | `mm_watchlist_items` | generated active-key 保证活跃 `user_id + issuer_id` 唯一；每用户活跃数量 ≤10。 |
| 来源 | `mm_source_cursors`、`mm_source_records` | `provider + provider_document_id` 唯一；原始 URL 与抓取时间不可丢失。 |
| 事件 | `mm_normalized_events`、`mm_event_sources`、`mm_event_briefs` | 事件版本不可覆盖；每一版 brief 至少关联一个 source。 |
| 市场与发布 | `mm_market_snapshots`、`mm_global_edition_runs`、`mm_global_edition_items` | 一个 `edition_date_jst + run_version`；只允许一个当前成功版本。 |
| 用户交付 | `mm_user_editions`、`mm_delivery_attempts` | `user + run` 唯一；`user + edition_date_jst + channel` 幂等唯一。 |
| 运行与运营 | `mm_jobs`、`mm_job_attempts`、`mm_scheduler_leases`、`mm_manual_overrides`、`mm_content_reports` | job idempotency key 唯一；scheduler 租约可超时接管；覆盖规则有生效区间与操作者。 |

### 4.2 必须由数据库保证的规则

- `SourceRecord` 不能没有 `source_provider`、`provider_document_id`、`original_url`、`fetched_at`。
- 发布状态只能按 `draft → running → complete / partial / failed / late` 合法转移；终态不能被静默重写。
- `EventBrief.confirmed_facts` 的每个已发布版本都要通过 `source_ids` 非空校验。
- `DeliveryAttempt` 的幂等键冲突时返回原记录，不再排队第二封邮件；并发插入以 MySQL unique-key 冲突作为最终防线。
- 用户只能读取自己的 `WatchlistItem`、`UserEdition`、备注和交付记录；运营角色的读取必须写审计日志。
- 软删除的用户从投递资格、搜索历史、活跃指标中排除；删除工作流有可验证完成状态。

### 4.3 迁移顺序

1. 先建用户、IssuerMaster、别名、关注股、审计；
2. 再建来源、事件、市场快照；
3. 再建版本化朝刊、投递、任务和后台审核；
4. 每一步提供向下迁移或明确的前向修复脚本，并在空库和升级库各跑一次。

---

## 5. 实施 Sprint 计划

节奏以一周一个 Sprint 为规划单位；实际开始日期取决于数据合同和部署资源，不以日历硬承诺替代 Gate。

### Sprint 0｜基线、合同与可运行骨架

**目标：** 让后续开发不在错误的数据权利、产品边界或部署模型上前进。

**实施项：**

1. 新建 `docs/adr/`，记录本计划第 2 节中的数据库、认证、worker、时区和隔离决策。
2. 建立数据来源登记表：来源、用途、读取/缓存/展示/AI/再分发权利、凭据状态、owner、到期复核日。
3. 完成运行环境清单：本地、CI、staging、T1；定义 secrets 名称而不提交真实值。
4. 新增 `market_morning` feature flag：默认关闭，且不影响现有首页、A 股、指数或 agent 流程。
5. 建立产品 telemetry 事件字典与脱敏规则：`signup_completed`、`watchlist_added`、`edition_opened`、`source_opened`、`delivery_clicked` 等。
6. 在合成数据下搭起 API / worker / migration 的空骨架，增加健康检查与一键本地启动说明。

**验收：**

- 无真实生产数据或密钥也能启动空产品模块；
- feature flag 关闭时现有测试与行为不变化；
- 数据权利表中 TDnet、EDINET、IssuerMaster、指数、外汇各有明确 owner 与状态；
- 已明确 staging 不允许用未授权数据对外暴露。

### Sprint 1｜账户、数据库与证券主表

**目标：** 做出可信的用户与证券身份边界。

**实施项：**

1. 接入选定的认证适配层，实现邀请制注册、邮箱验证、登出、会话失效和用户停用。
2. 建立 MySQL 连接、迁移、事务边界、UTC session、连接池健康检查及测试数据库 fixture。
3. 实现 `IssuerMaster` 快照导入：原始文件 URL、版本、抓取时间、解析结果和差异报告均持久化。
4. 实现名称规范化：全半角、空格、标点和「株式会社／（株）」后缀；禁止自动语义别名。
5. 实现审核式别名队列与后台操作审计。
6. 实现搜索 API：4 字符证券代码精确匹配（兼容 2024 年起新分配的字母代码）、正式名/已批准别名前缀匹配，返回代码、正式名、市场和添加状态。
7. 编写 50 条日文搜索基准集，作为 CI 可运行 fixture。

**验收：**

- 50 条基准集至少 45 条目标公司位于前 3；
- 未命中不会猜测公司；
- 所有主数据改动可回溯到 source snapshot 或后台操作者；
- 无登录态不能访问任何用户数据。

**当前进度（2026-07-21）：** 名称规范化、50 条搜索 benchmark、代码／正式名／已批准别名搜索，
以及审核式别名队列已经完成。内部 API 只返回发行人和别名的必要字段，不返回
`source_reference`、URL 或来源 payload；批准／拒绝使用固定原因码、`SELECT ... FOR UPDATE`、
终态幂等和 `mm_audit_logs`。批准前会拒绝已过期别名及已被其他发行人批准的同一规范化别名，
避免搜索结果产生静默歧义。生产 IssuerMaster 许可和真实 OIDC／MySQL 验收仍是外部 Gate。

### Sprint 2｜关注股、产品设置与私有界面骨架

**目标：** 用户完成“添加至少 3 只关注股”的首个价值前置动作。

**实施项：**

1. 实现 watchlist API：新增、删除、排序、标签修改，数据库侧限制 10 只活跃关注股。
2. 实现用户设置：JST、邮件 opt-in、风险/数据说明确认、账户删除请求入口。
3. 前端实现登录后 onboarding、公司搜索、关注股列表、空朝刊状态和设置页。
4. 添加明确的私测空状态：无数据、休市、尚未发布、部分可用均不能显示为“无事件”。
5. 记录搜索、添加、删除和设置变更 telemetry；事件不得含个人备注或完整邮箱。

**验收：**

- 新用户可在一次会话内添加 3–10 只关注股；
- 关注列表跨会话保存，用户间严格隔离；
- 产品界面不出现 Buy / Sell / Hold / 推荐等交易建议语义；
- 页面、API 与 telemetry 的权限测试通过。

### Sprint 3｜官方来源采集与事件标准化

**目标：** 形成可增量、可去重、可回溯的事件底座。

**实施项：**

1. 定义 `SourceAdapter` 契约：`discover(cursor)`、`fetch(document_id)`、`normalize()`、`health()`；所有 adapter 在 fixture 下测试。
2. 实现 TDnet adapter：游标、开示编号、修正/删除状态、关联 issuer 和原始 URL；仅在数据合同完成后接真实 endpoint。
3. 实现 EDINET adapter：文档列表增量、文档编号、日期、元数据更新时间与原始回链；MVP 不做全量 XBRL 深解。
4. 实现公司 IR 白名单 adapter：每家公司独立配置与 health 状态；失败不补写内容。
5. 实现 `SourceCursor → SourceRecord` 幂等写入和确定性去重。
6. 实现跨来源“候选合并”规则：同 issuer、标题规范化相近、24 小时窗口；只入审核队列，不自动合并。
7. 实现撤回/更正的版本回写与受影响事件查询。

**验收：**

- 同一 provider document 永不产生重复 `SourceRecord`；
- 来源失败可重试且不会清空旧游标；
- 撤回/更正可在后台找到其关联事件和已发布朝刊；
- 任何来源抓取失败时，系统不会把缺口交给模型补全。

**当前进度（2026-07-21）：** 跨来源候选生成和运营审核闭环已经完成。审核队列只读取候选、
同一发行人的两条事件及必要相似度／时间差证据，不读取来源 URL、正文或 payload。批准前再次
锁定并核对两条事件仍属于同一 issuer；批准或拒绝只更新 `mm_event_merge_candidates` 的审核状态
并写审计，绝不改写 `mm_normalized_events`、`mm_event_sources` 或已发布朝刊。实际事件合并不属于
MVP。EDINET API v2 生产 adapter 已完成固定官方 endpoint、关注发行人过滤、0–7 天 revision cursor、
订正／撤回、PDF type=2 下载、大小／Content-Type／逻辑状态校验、受限正文提取、公开回链和 secret
不落库，并已让 `SourceRecord.normalized_payload.evidence_text` 成为 EventBrief 的真实证据入口。
真实 EDINET key、权利审批和窄范围 staging 仍未完成。TDnet 付费 API 生产 adapter 也已完成固定 JPX
endpoint、关注发行人过滤、当前／订正删除历史双索引、14 位开示编号 + history cursor、`206` 截断
fail-closed、每秒 1 请求节流、全文／summary PDF 与官方 S3 base64 安全下载、受限正文提取、公开 PDF
回链和 secret／一次性 URL 不落库。冷启动订正链保留全部不可变 revision，但只下载官方说明内容不变的
最新一份；删除链不请求已失效文档。公司 IR 生产 adapter 也已完成逐发行人
`company_ir_{provider_key}` 隔离、精确 HTTPS hostname/path allowlist、公网 DNS、禁代理/重定向、
deployment-owned parser、strict JSON index、revision cursor、订正/撤回、PDF/HTML/metadata 受限证据与
独立 health；空 watchlist 不访问公司网站，单公司失败不推进 cursor 或影响其他 provider。真实 TDnet
合同/access key、公开回链保存期/获准自托管范围，以及首批公司 IR 的逐公司用途权利、最终 URL/parser
和窄范围 staging 仍是外部 Gate。

### Sprint 4｜JPX 日历、市场快照与全局朝刊运行

**目标：** 将“每天早上跑一次”变为基于交易日、可恢复的发布系统。

**实施项：**

1. 实现 JPX 现货交易日历、美国市场状态、JST 发布窗口和运营手动覆盖。
2. 为日经、S&P 500、Nasdaq、Dow、USD/JPY 建立独立 `MarketDataAdapter`；每个值携带 `session_date`、`as_of`、延迟状态和 provider。
3. 只在授权完成后将市场数据 adapter 指向可公开的数据源；现有 Yahoo 开发实现仅用于开发环境。
4. 实现 durable job：采集、标准化、事件 brief、市场快照、全局编排、发布判定和邮件投递为独立类型。
5. 实现 06:30 截止、07:00 首判、07:15 / 07:30 / 08:00 / 08:30 重试规则；worker 使用数据库锁确保单活。
6. 实现发布容限：市场快照缺失不发布；单一来源问题可部分发布；达到“≥2 只或 >20% 失败”则不发布并写入原因。
7. 为 A–E 五种日历场景、重启恢复、重复 job、晚于 08:30 发布分别写测试。

**当前进度（2026-07-21）：** durable job、attempt、scheduler lease 与 manual override 的 MySQL
schema／迁移已完成；幂等入队、`SKIP LOCKED` 领取、成功／重试／终态、lease 续期与过期恢复
已有离线测试。source ingestion 与 edition generation handler 已注册：前者按“短事务读 cursor →
事务外采集 → 锁定 cursor 后短事务写入”运行并独立记录失败，后者严格解析版本化 command。
worker 已支持执行中 heartbeat、lease-loss 取消、过期任务恢复、空闲轮询与 stop event 优雅停止。
JPX／美国 fixture 日历、A–E 场景、JST 06:30–08:30 窗口、审计型人工停发，以及带数据库租约的
单活 scheduler policy 已实现；重启会选择最近到期槽位，相同槽位以 durable job key 幂等，晚于
08:30 只允许站内 `late` 运行。五项市场快照的 fixture adapter、不可变 `0008` 表、payload
hash 幂等写入和“缺项／交易日错位／重复候选即阻止发布”的完整性 Gate 已完成。

市场快照 durable handler 与 `0009` 全局刊期聚合现已完成。scheduler 从 06:00 起先按 deployment
声明的 licensed `source_providers` 幂等排入 source-ingestion job，即使休市／停发／已发布也继续
采集；06:30 再按固定顺序排入五项高优先级 snapshot job，发布 slot 使用稳定
`scheduled_at`，避免相同幂等键在重启 tick 中出现 payload 漂移；global handler 执行
`draft → running`，加载指定 provider 的五项快照，Gate 失败时持久化 failed run 且不发布，
成功时写有序 manifest、保留旧版本并通过 generated unique key 切换唯一 current success。
默认关闭的 `vibe-trading-market-morning` 进程入口已完成：启动前验证完整 handler、licensed
calendar contract、MySQL 连通性和精确 `0018` revision。生产 worker 还会在创建 task 前按固定
顺序执行 licensed sources、market snapshots、EventBrief model、source reachability、email
delivery 五个 deployment-owned 无副作用探针；缺项、异常或 10 秒超时均以脱敏错误码拒绝启动。
fixture／缺配置／旧 schema 均拒绝启动；scheduler 与 worker 共用 SIGINT/SIGTERM stop event。当前直接按
`agent/tests/test_market_morning_*.py` 运行的普通 Market Morning 回归为 885 项通过、15 项按默认安全策略跳过；隔离 MySQL 8.0 与远程专用验收库上的 12 项业务 probe、3 项
破坏性 migration probe 均已分别 12/12、3/3 通过。远程并发验收还发现并修正了 current global run
切换时的同表更新顺序：先单独 flush 旧版本 demotion，再 promotion 新版本，避免 MySQL generated
unique key 观察到瞬时双 current。前端最近一次回归为 342 项通过且生产构建成功，迁移 head 已推进到
`0018`；空库 bootstrap 包含 34 张业务表、精确 revision 与
`varchar(64)` 版本字段，最新专用数据库的空库升级与破坏性迁移演练已分别以
`12/12` 和 `3/3` 通过。

生产 provider-neutral adapter 现已补齐：JPX／美国日历在进程启动前从精确 HTTPS/path allowlist
异步加载完整、无缺日且有 freshness/coverage 的 strict manifest，scheduler 只读取不可变同步日历，
不会在数据库 lease 内访问网络；五项市场数据各自绑定 instrument/provider/endpoint/currency/delay
contract，拒绝 JSON float、私网 DNS、redirect、环境代理、越界 path、超限响应和陈旧观察值。
`assemble_runtime_dependencies()` 进一步要求完整 source registry、五项行情、两份非 fixture 日历、
EventBrief/邮件完整 ports 和五项 preflight；来源与行情两个 probe 直接由实际 adapter registry 生成，
factory 同时支持同步和异步加载。新增生产边界与现有 Sprint 4 契约测试合计 94 项通过。

仓库内置最终无参 production factory 已完成：它加载经审 provider bundle，并统一绑定产品 MySQL、
OpenRouter EventBrief、Resend runners 与严格 runtime 装配。尚未完成的是获授权 JPX／美国日历和行情的
具体 provider endpoint/header/parser 配置及返回这些真实 ports/checks 的 deployment provider bundle factory、
Resend 真实账号／域名／回放、真实 Prometheus／告警通知链和生产主库升级（当前远程主库仍为 `0004`），
因此 scheduler 尚未启用、本 Sprint 仍为进行中。

**验收：**

- 日本休市绝不发朝刊或邮件，仍能采集事件；
- 美国休市的朝刊明确显示最后有效美股日期；
- 同一业务日期的重复触发只产生一个当前发布版本；
- 08:30 后首次完成只站内发布，不触发邮件。

### Sprint 5｜事件研究卡与内容质量 Gate

**目标：** 让 AI 仅整理有来源的事实，而不扮演荐股员。

**实施项：**

1. 定义版本化 `EventBrief` JSON schema：`confirmed_facts`、`open_questions`、`source_ids`、`model_version`、`review_status`。
2. 实现事件级生成任务：同一新事件或修订最多生成一次；失败后可重试并保留失败原因。
3. 实现 deterministic pre/post validation：来源存在、URL 可访问、事实不为空时 source_ids 非空、禁止词、数字/日期检查、JSON schema 校验。
4. 生成失败的降级卡仅展示标题、时间和来源链接，不显示模型推断。
5. 实现运营后台审核：下架、修订状态、错误报告、候选跨源合并审批；所有动作写审计。
6. 建立一组中日文 fixture，覆盖：真实事件、来源缺失、撤回、矛盾日期、推荐措辞、无法判断。

**当前进度（2026-07-21）：** `EventBrief v1` 领域 schema、deterministic quality Gate、`0010`
持久化与事件级 durable 执行链已完成。
模型 JSON 使用严格字段和类型解析；每条 fact 自带 `source_ids`，top-level source manifest、HTTPS
URL 与外部 reachability 结果共同参与 fail-closed 校验。已禁止中／日／英荐股和保证收益措辞，
数字与日期 token 必须能在所引用证据文本中找到；“现有资料不足以判断”是合法有来源结果。
模型失败可构建只含事件标题、时间和来源链接的 degraded card，不携带事实或推断。生成记录会
保存 spec／payload hash、尝试次数、脱敏失败码和具体来源外键；重复终态执行返回原 durable hash，
不同内容不能覆写。新来源 revision 创建 normalized event 后，会在同一事务原子排入确定性
EventBrief job，避免事件与任务脱节；URL 检查和模型调用均位于短事务之外。runtime 现在要求
`event_brief_generation` handler，构建 registry 时 runner 与 model/prompt dispatch config 必须成对
配置。生产 HTTP reachability adapter 已完成：仅允许部署侧精确 hostname allowlist 内的 HTTPS，
拒绝 userinfo、非 443 端口、IP literal 与任一非公网 DNS 结果；重定向逐跳重验，HEAD 不支持时
使用不读取正文的 range GET，4xx 与 DNS／网络／5xx 分别映射为不可达或检查暂时不可用。运营审核
API 也已完成。OpenAI-compatible 生产模型 adapter 现已强制 strict JSON schema、输入／响应预算、
单一完整 choice，并读取真实 token usage、实际返回 model、request/generation ID 和可选 provider
cost；缺失 usage/cost 可配置为 fail closed，已有 usage 会随失败一起进入审计，不使用字符数或本地
费率估算。OpenRouter 具体 adapter 进一步固定官方 global/EU endpoint、单一 model、upstream
`order/only` allowlist、`require_parameters=true`、`data_collection=deny`、`zdr=true` 和必须存在的
usage/USD cost；fallback 默认关闭，即使开启也不得越出 allowlist，错误布尔配置会拒绝启动。
当前尚未完成具体 model/upstream 的审批、strict-schema/usage/cost 与账号隐私策略 staging 样本，
以及随授权来源批准的 hostname allowlist；真实 MySQL 业务/并发 12 项和迁移 3 项已在远程专库通过。
用户朝刊生成已按确定性优先级读取
最新可用 EventBrief，复核 payload
schema、SHA-256、event revision、review status 与 source manifest；每条已确认事实保留自己的
精确来源，degraded／blocked／无效内容只回退到官方事件标题和原始来源，不展示未经核验的模型
文本。Sprint 5 仍为进行中。

**验收：**

- 每张已发布的事件卡至少有一个具体来源链接；
- 禁止词或无来源事实会阻止发布而不是仅记录 warning；
- “不足以判断”可以作为合法结果；
- 模型故障不会阻塞其他有完整来源的事件。

### Sprint 6｜用户朝刊、个股页、邮件与观测

**目标：** 完成从全局内容到用户阅读与可衡量留存的价值闭环。

**实施项：**

1. 实现 `DeliveryEligibleUser` 查询：至少 3 只有效关注股、近 7 天活动、email opt-in、私测/有效订阅状态。
2. 实现 lazy `UserEdition`：用户点击邮件或首次打开时，以当时关注股和当前成功 `GlobalEditionRun` 编排；同日只创建一个版本。
3. 实现用户朝刊页：头部状态、市场环境、关注事件、风险雷达、来源打开、已读/稍后/无关状态。
4. 实现个股页与历史事件；支持轻量个人备注，不支持自由投顾对话。
5. 实现邮件投递 port、幂等 `DeliveryAttempt`、provider webhook 状态（送达/失败/点击）；邮件正文仅含私有跳转链接。
6. 页面设为私有、`noindex`、无公开 URL、无分享卡；所有深链都需会话校验。
7. 增加运营看板：运行状态、来源健康、失败原因、成本、交付数、打开与来源点击。

**当前进度（2026-07-21）：** `DeliveryEligibleUser` 查询、`0011` delivery-attempt、`0012`
provider webhook 基础、`0013` 私有阅读交互表、`0014` 私有个股备注表、`0015` 私测邀请表，以及
`0016` provider-neutral model usage/cost 表与 `0017` 隐私安全内容报告表已完成。
资格 Gate 同时要求至少 3 只有效关注股、7 天内产品活动、email opt-in、active 账户，以及
private beta／trialing／active 订阅状态。邮件尝试以用户、刊期和渠道唯一，另有 provider message
与幂等键约束；仅保存 deep-link token hash，不保存邮箱或原始 token。repository 已支持并发安全
创建、sending 开始、脱敏失败、sent 完成与终态 suppression，并保留固定 provider identity。
全局 run 成功后会按资格创建投递和 `email_delivery` job；handler 在实际发送前再次检查 run、
用户状态和关注股数量，身份解析、私有链接签发与 provider 调用均位于短事务之外。provider
webhook 核心在验签成功后才入库，只保存 payload hash，并对 delivered／failed／clicked 做幂等、
不倒退的状态更新。lazy `UserEdition` 也已接入今日朝刊读取路径：已有快照直接复用，否则锁定
current global run 与 active user，以全局刊完成时间为事件截止点、按首次打开时的当前关注股生成，
并以日级 generation key 保证同日只生成一次。持久化朝刊现在返回不可猜测的 `edition_id`；页面
按事件合并同一 EventBrief 的多条事实，支持“既读／稍后／无关”状态的读取、乐观更新与失败回滚。
来源点击必须与该用户不可变朝刊中的 citation 以及原始 `SourceRecord` 精确匹配，幂等记录只保存
source record ID，不复制 URL；`source_opened` telemetry 也只保存来源类型。所有交互 route 均从
认证 principal 取得 user ID，不接受客户端自报身份。个股研究页现已按关注股权限加载官方事件
版本历史、订正／撤回关系、通过 EventBrief Gate 的事实与逐条来源；没有有效 EventBrief 时只
展示官方标题和原始来源。轻量个人备注按 `user_id + issuer_id` 私有保存，限制 1000 字，不写入
telemetry；读写、删除 API 均从认证 principal 取用户身份。前端提供关注股到研究页的私有入口，
Market Morning shell 动态设置 `noindex,nofollow,noarchive`，开发代理也已覆盖朝刊交互、来源打开
与个股研究 API。内部运营看板现已汇总来源健康、job、global run、EventBrief、投递、使用和停发
状态，并提供只能停发/撤销的 publication halt 控制。每次实际 EventBrief 模型调用现在都会形成
brief/attempt 唯一的计量事件；token、按币种微单位成本和 provider request ID hash 可聚合，usage
缺失与未定价分别显示，绝不伪造估算。EventBrief 审核队列支持对 auto-validated 内容批准或拒绝
下架，所有变更保留不可变 payload/hash 并写脱敏审计。外部邮件回调 route 现已接入 API：1 MB
流式请求上限、部署 factory、未知 provider／未配置 factory 的 fail-closed 行为、签名失败与非法
payload 的脱敏错误映射，以及 accepted／duplicate 最小回执均有 API 测试；route 不使用产品会话或
Vibe API Key，原始 payload 与签名只在内存中交给 adapter。生产模型 HTTP adapter 已完成，但仍需
选定 provider/model 的真实 usage/cost 样本。产品 Bearer HTTP 边界也已完成：deployment factory
返回 verifier，核心区分 401／403／503，并只把已验证 opaque subject 映射到 active、未删除且有效
私测／试用／订阅用户；邀请 onboarding 复用验签但不提前查询用户表。具体 Resend 发送 adapter、
Svix 原始 body 签名／防重放和 delivery event parser 已完成。私有深链 signer 与兑换闭环也已完成：
确定性 HMAC token 不包含身份明文，数据库只存 SHA-256，URL 仅在 fragment 携带 token；前端立即清除
fragment 并用当前产品 Bearer 在 POST body 兑换，后端同时绑定认证用户、邮件状态和 7 日窗口，且支持
主/上一密钥轮换。通用 email identity resolver factory 也已完成：产品库不保存邮箱，部署目录必须按
内部 user UUID + 伪名 OIDC subject 查询，并将两个标识原样回绑；错用户、错 subject、未验证、停用或
非法地址均 fail closed。`build_resend_email_runtime_runners()` 会让 dispatch 与 delivery 共享同一 signer
和 session factory，并把 identity resolver、深链和 Resend provider 作为事务外 ports 注入 worker；构造
不发送邮件，真实账户可用性仍由 deployment preflight 验证。静态 deployment preflight 会阻断缺失／非法
factory 与深链配置。前端 provider-neutral lifecycle contract 已完成：部署在 React 渲染前分别注册
product/operator adapter，核心保证私有 API 前初始化、每请求即时取 token、同源无 fragment 的登录／
登出返回路径、初始化失败重试和 401 登录入口；产品邮件 token 会先离开 URL，只在认证成功后兑换，
不落 localStorage/sessionStorage。官方 Auth0 SPA SDK adapter 也已接入启动链：产品/运营使用独立 client，
固定 PKCE、memory cache、offline rotating refresh token、禁 iframe fallback、callback 参数清理、
refresh-token revoke + provider logout，并动态加载 SDK，不拖累普通 Vibe 路由。尚未完成真实账号／域名／
邮件回放、实际 identity directory adapter，以及真实 Auth0 tenant 的允许 URL、角色、刷新、撤权与跨会话
验收，Sprint 6 仍为进行中。通用 OIDC/JWKS
adapter 已内置：固定 HTTPS issuer/JWKS、精确 audience 和非对称算法，验证 signature/exp/iat/
auth_time、限定 JWKS cache/response、支持未知 kid 单次轮换刷新并抑制 kid 流量放大、逐请求调用
deployment session 撤权 validator、对 issuer-scoped subject 做不可逆伪名化；运营角色仅能映射到
四项固定权限。账户删除 route 现已要求 provider 证明的 5 分钟内近期认证；请求
与 durable job 原子创建后立即把用户置为 `deletion_pending`、停止邮件和普通产品访问，同时仅为
删除 endpoint 保留幂等重试，以防首次响应丢失。生产仍须证明真实 `auth_time` 与 provider session
撤销语义，不能由客户端自报。

**验收：**

- 非活跃账户不预生成 UserEdition、不接收邮件，但登录后可看最近成功版本；
- 同一用户同一天不会收到第二封应用层提醒邮件；
- 用户只能访问自己的朝刊和备注；
- 能从运行、事件卡、来源、邮件尝试一路追溯到原始记录。

### Sprint 7｜T0 验收、演练与邀请制私测准备

**目标：** 不以真实用户作为第一个故障测试者。

**实施项：**

1. 补齐 API、数据库、worker、日历、内容、端到端和前端回归测试；现有 Vibe 测试必须继续通过。
2. 在 staging 用合成数据和被批准的窄范围数据演练：标准日、美国休市、日本休市、单源失败、市场数据失败、模型失败、worker 重启、邮件 provider 超时。
3. 完成运行手册：07:00 前检查、失败分类、重跑条件、手动停发、撤回/修订、用户支持和数据删除流程。
4. 完成 observability：结构化日志、job attempt、指标、告警阈值与 dashboard；日志不写完整原文和敏感账户信息。
5. 完成私测 invite、停用、邮箱退订、数据导出/删除和后台角色权限流程。
6. 对照 v3 的 T1 Gate 逐项签字：搜索、日历、去重、撤回、引用、失败、邮件幂等、非活跃降级。

**当前进度（2026-07-23）：** 独立认证保护的运营摘要、内部看板、OpenMetrics 1.0 聚合指标出口、
EventBrief 批准/拒绝、仅停发的 publication halt 控制和审计已完成；运行手册已覆盖 07:00 前检查、失败分类、重跑条件、停发、
修订/撤回、支持/删除边界、告警阈值和 staging 场景。一次性 hash-only 私测邀请、邀请撤销、用户
停用/恢复、认证用户 JSON 数据导出、删除申请自动排队与幂等删除 worker 已完成；运营页面可发行
邀请和停用账户，产品设置页可下载导出。邀请接受 service 与 product route 已实现，但 route 的
onboarding principal 在未配置内置 OIDC 或审核后的自定义 factory 时保持 fail closed。后台控制面已接入 deployment-owned
运营认证 port，按 `operations.read`、`content.review`、`publication.control`、`access.manage`
分离权限，并把个人 `actor_reference` 写入所有变更审计；本地兼容路径仍可使用 Vibe API Key。
前端已增加只驻留内存的 product/operator lifecycle 端口，每个 scope 在私有请求前并发安全地初始化，
每次请求即时取 token；产品壳与运营页均具备登录、登出、初始化失败重试，产品请求不复用旧 API Key，
注册运营 provider/lifecycle 后也不会回退旧 key。内置 Auth0 SDK wrapper 已完成 PKCE、memory cache、
rotating refresh token、callback 清理、refresh-token revoke 与 provider logout；真实 tenant/application、
允许 URL、Post-Login Action 和两个 SPA 的 User-delegated Access 已配置。内置 factory 的静态预检已增加 issuer/JWKS/audience/session validator/role mapping 稳定阻断码，
PyJWT crypto 也进入核心哈希锁依赖。
真实 provider usage/费率样本、900 秒新 access token 下的产品/运营 OIDC 跨页面与跨会话 E2E、
精确 token 撤销、外部 adapter 和连续 5 日 staging 演练仍未完成；远程 MySQL 12+3
已在 2026-07-23 重新通过，但生产主库仍需从 `0004` 维护升级到 `0018`。产品库已新增独立的 fail-closed schema change job：先做只读
revision/表数预检，正式执行必须提供备份证据 hash、变更单、维护窗口、runtime 停止和不自动
downgrade 的显式确认；升级后精确校验 `0018`/34 表并输出不含凭据的 hash-only manifest。真正产品库
变更仍须单独审批。T0 本地自动演练 runner 已把运行手册
12 个场景固化为 pytest probes，失败不会中止其余场景，并输出只含 node ID、exit code、耗时与
输出 SHA-256 的 JSON manifest；2026-07-21 参考报告为 12/12 通过。报告固定标记
`counts_as_staging_day=false`，不能冒充真实 staging 交易日，因此 T1
开关继续关闭，Sprint 7 仍为进行中。已按哈希锁文件补齐全仓库运行依赖，并在开发依赖中声明
`pytest-asyncio`；生产日历/行情与 runtime strict assembly 落地后，直接按
`agent/tests/test_market_morning_*.py` 运行的 Market Morning 普通回归为
`885 passed, 15 skipped`。按 CI 约定排除独立 `e2e_backtest` 和真实 LLM 专用
`test_e2e_harness_v2.py` 的正常本机权限整仓 JUnit 结果为
`6317 passed, 25 skipped`，无失败或错误；25 项 skip 均有登记的外部前置条件。CI 环境变量 Gate 已通过，仅有一条既有非阻断 warning。这些结果仍不能替代真实 MySQL、
授权数据源或 staging 证据。仓库级 Ruff 仍有上游既有
lint 债务；Market Morning 范围 Ruff 已通过，两个范围必须分别记录。
指标出口与运营摘要共用同一隐私安全 read model，输出健康度、告警、来源状态、job／brief／投递／
engagement 计数和按币种实际模型成本；不输出用户、run ID、刊期、URL、payload 或 provider request
ID。版本化 Prometheus rules、secret-file scrape 和 Alertmanager warning/critical 分路基线已完成，
两个 webhook receiver 均使用 `url_file` 并发送 resolved。严格 monitoring drill 合同／CLI 只接受真实
staging、同一不可变 release、四段 firing/resolved、silence 到期和双人确认，并输出不含原消息、URL、
header 或个人身份的 hash-only 报告；默认模板固定 blocked。实际 receiver、真实通知送达与恢复演练
仍须在 staging 留证，因此 observability 不能提前判定为生产完成。
生产 runtime 也已增加五项 deployment-owned preflight contract，并保证在数据库校验后、worker
领取任务前执行；探针缺失、失败或超时均 fail closed。实际 factory 仍需在取得授权来源、provider
凭据和邮件配置后实现这些无副作用探针并提供 staging 证据。
内部 readiness 已从单纯 `SELECT 1` 收紧为精确校验 `alembic_version`；旧版或缺失 schema 会明确
返回 `misconfigured`。新增独立运营认证保护的 `deployment-preflight`，把 feature、数据库、产品与运营认证、
runtime、邮件 webhook、私有深链签名、synthetic/fixture 配置汇总为不泄密的布尔值和稳定阻断码。该静态 Gate
固定标记 `counts_as_t1_evidence=false`，因此只能阻止错误部署，不能替代五项真实 runtime preflight、
外部 provider/OIDC 验收或连续 5 日 staging 证据。选择内置 OIDC factory 后，issuer、JWKS URL、
audience、session validator、运营 role mapping 或深链 URL/密钥缺失/非法也会以稳定码阻断。
运营 factory 缺失会以 `admin_auth_factory_missing` 阻止生产配置通过静态预检；具体 OIDC 角色目录、
撤权服务与 session 失效仍须由部署侧接入并在 staging 留证。
本地浏览器可手测缺口已在 2026-07-22 收口：`npm run dev:market-morning-demo` 以固定端口启动只驻留
Vite development 进程内存的 synthetic API，页面持续显示“デモデータ”，production mode 无法启用且
生产 bundle 不包含本地 Demo payload。真实 Playwright 已完成朝刊、关注股搜索/添加、个股研究笔记和
通知设置四条流程，业务请求全部返回 JSON 200，控制台无 error/warning。该证据固定
`counts_as_staging_day=false`，不能替代真实 OIDC、数据源、邮件或 T1 staging。
现有内部运营页也已接入该静态 Gate：HTTP 503 的 `blocked` payload 会作为可操作状态展示，而
认证、网络和其他错误仍进入故障提示；值班人员无需通过浏览器控制台或临时脚本读取阻断原因。
真实 MySQL 验收也已开始固化为显式 CLI：只接受名称匹配 `market_morning_acceptance*` 且与生产
目标不同的专用数据库，并要求逐次写入确认。当前十二项真实 probe 覆盖 MySQL 8.x/UTC/utf8mb4/
精确 revision、access-token hash session 的幂等首见／精确注销／不可复活／多 token 隔离、来源
revision/cursor、用户朝刊 generation/revision、job 写入与 `SKIP LOCKED` 领取、
scheduler 单活租约、publication halt 并发与审计、global run 唯一 current success、EventBrief／model
usage 并发幂等、邮件 attempt 并发，以及 webhook 重复／乱序状态机与账户删除事务回滚；
manifest 不保存 URL、host、凭据或 pytest 原文，并固定不计为 staging 日。2026-07-21 已在隔离的
MySQL 8.0 容器和远程 `environment=staging` 专用 acceptance 库各执行 12/12 passed；远程证据位于
`docs/evidence/market-morning/mysql-acceptance-remote-2026-07-21.json`。job 领取也修正为有序候选 ID + 精确主键
`FOR UPDATE SKIP LOCKED` 两阶段锁定，双 worker 在首个事务持锁时可领取不同任务。CI 会为每次提交
自动运行相同 probes，但 `environment=ci` 的结果不能作为 T1 staging 输入。

CI 现已在上述后端、前端 build/test 和 MySQL 12+3 全部成功后生成不可变 release candidate manifest。
Gate 要求 checkout 为 clean tree、Git revision 与 CI 预期 revision 一致、runtime schema 为 0018，且
依赖锁、容器/compose、CI workflow、当前 migration、0018 SQL/ZIP、monitoring rules、最终 production
runtime factory、主运行手册、Auth0 Post-Login Action／部署合同及 monitoring/T1 严格证据模板共十六项
固定发布源文件均已被 Git 跟踪并可计算
SHA-256；五份验证输出也必须完整，MySQL 两份还须通过同环境合同
校验。输出只保留固定名称、revision、检查状态、阻断码和 hash，且固定不计为 staging/T1 证据。
staging 部署和随后五日 evidence 必须使用该 manifest 的同一 revision，不允许重新构建或手填漂移版本。
真实 staging 日报的生产链也已补齐：每项 Gate 先用 CLI 对原始 probe 文件计算 SHA-256，并自动从
通过的 release candidate 派生 revision/schema/candidate hash；11 个严格 artifact 再由单日 builder
验证共同 run ID、刊期、JPX 前后交易日、calendar provider 和至少五个生产 provider 后生成既有
`market_morning_staging_day` 合同。失败 Gate 仍产生可审计但不计数的日报。由此五日聚合器不再依赖
人工复制 revision 或手写整份日报；真实 provider/OIDC/邮件/监控证据仍须由 staging 系统产生。

迁移验收使用另一个默认拒绝执行的 CLI 与名称匹配
`market_morning_migration_acceptance*` 的独立可丢弃数据库，不允许复用上述业务验收库。它要求显式
确认 destructive reset，并分别执行 `base → 0018`、`0016 → 0018` 与
`0018 → 0016 → 0018`，验证精确 revision、完整表集合和 0016 以前的账户／私测邀请数据保留；manifest
固定 `contains_destructive_migration_evidence=true`，仍不计为 staging 日。2026-07-21 已在独立
MySQL 8.0 可丢弃库及远程 `environment=staging` migration acceptance 库执行 3/3 passed；远程证据
位于 `docs/evidence/market-morning/mysql-migration-remote-2026-07-21.json`。本地还直接导入了交付
bootstrap SQL；这些 MySQL manifest 本身均明确不能计作连续五日 staging 日。

连续五日 T1 Gate 已固化为独立的 strict evidence CLI。单日输入必须是
`scope=market_morning_staging_day`、`environment_tier=staging`，使用当前 schema、不可变 release
revision、非 fixture／synthetic provider、完整的 11 项每日 Gate 和各自 artifact SHA-256；通过日还
必须在 06:30–08:30 JST 完整发布并至少包含一次实际模型 usage/cost。聚合器按 manifest 中 licensed
JPX calendar 的 previous/next open-session 链判断连续性；失败日、漏日、release 或 calendar provider
切换都会重置 streak，且只认最新连续五日。T0、普通 MySQL 与 migration manifest 会因 scope 和
`counts_as_staging_day=false` 被拒绝。当前仅完成合同、聚合器和 19 项离线测试，尚无任何真实 staging
单日报告，因此 T1 五日 Gate 仍未通过。
在五日 Gate 之上又新增了 fail-closed 的 T1 总发布 Gate：它只接受 12/12 真实 MySQL acceptance、
使用另一数据库的 3/3 destructive migration rehearsal、最新连续五日 staging 报告，以及 TDnet、
EDINET、公司 IR registry、JPX 主数据/行情权利和日本法律审查五项未过期签字。输出只含
release/schema、检查状态、
稳定阻断码和四份输入的 canonical hash；任一 scope、场景、数据库隔离、release/schema 或签字不
一致都不能批准。外部签字 contract 已升级为 schema v2；即使某 release 使用空 IR registry，也必须
明确签字，不能以后静默加入未审 feed。当前 blocked 签字模板已提供，但真实四类聚合输入尚不存在，
因此总 Gate 仍为未通过。

**验收：**

- T1 Gate 全部有自动测试或演练证据；
- 连续 5 个 staging 交易日可稳定发布；
- 任一失败日都能解释“未发布/部分发布”的根因；
- 没有邀请、来源许可或审查未完成时，T1 开关保持关闭。

---

## 6. 发布后的 T1 观察计划

开发完成后不立即做公开订阅。先进入邀请制私测，并至少收集 20 个**有效日本交易日**。

### 每日要看

- 发布成功率、08:30 前发布率、部分发布率；
- 来源 adapter 成功率、游标延迟、撤回/修订处理时间；
- 激活用户数、朝刊打开率、来源打开率、阅读完成度；
- 3–4、5–7、8–10 只关注股的内容厚度；
- 每活跃交付用户每日可变内容成本；
- 内容纠错、无关标记、用户支持与退订情况。

### 公开 Beta 前的结论会议

必须同时回答：

1. 用户是否在第二周仍重复打开朝刊？
2. “来源可回溯”是否被实际使用，而非只停留在文案？
3. 官方开示与 IR 是否足以形成有用内容，还是需要后续新闻授权？
4. 关注股数量上限是否应保持 10？
5. 数据、模型与邮件成本是否支持一个标准个人套餐？
6. 数据许可、隐私和投顾边界是否全部通过？

任一项没有证据时，继续 T1 或缩小来源范围；不以付费墙代替价值验证。

---

## 7. 风险、依赖与处理策略

| 风险 | 触发信号 | 处理策略 |
|---|---|---|
| TDnet / 行情许可延迟 | 合同或使用范围未确认 | 继续做 adapter contract 与 fixture；禁止真实外部测试和公开发布。 |
| JPX 主数据的复用范围不清楚 | 无法确认公开搜索权 | 不把该表加入公开环境；使用经许可的替代主数据或延后 T1。 |
| IR 页面不稳定 | 多家公司连续抓取失败 | 仅白名单接入；逐公司隔离失败，不让 IR 影响 TDnet/EDINET 主链。 |
| 生成成本膨胀 | EventBrief 数、token 或活跃用户上升异常 | 事件级缓存、元数据优先、每日预算阈值、超过阈值自动停发并告警。 |
| 错误/越界内容 | 禁止词、无来源事实或用户投诉 | 内容 gate 阻断发布、后台下架、保留版本与来源审计。 |
| 07:00 发布失败 | worker / 数据 / 邮件异常 | 独立重试、08:30 截止、站内状态、无空邮件；运行手册处理。 |
| 多租户泄漏 | 跨用户 ID 访问、权限测试失败 | 所有查询强制 `user_id` scope；端到端授权测试；审计后台读取。 |
| 旧项目回归 | A 股、指数或 agent 测试失败 | 新模块独立、feature flag 默认关闭、CI 加入旧回归组。 |

---

## 8. 开发顺序与依赖图

```text
Sprint 0：合同 / ADR / 环境 / feature flag
    │
    ├── Sprint 1：认证 + MySQL + IssuerMaster
    │       │
    │       └── Sprint 2：搜索 + watchlist + 产品界面
    │
    └── Sprint 3：TDnet / EDINET / IR → SourceRecord → Event
            │
            ├── Sprint 4：JPX 日历 + 市场快照 + 全局朝刊
            │       │
            │       └── Sprint 6：用户朝刊 + 邮件 + 指标
            │
            └── Sprint 5：EventBrief + 内容质量 Gate ───────┘
                                                        │
                                                 Sprint 7：T0 演练
                                                        │
                                             T1：20 个有效交易日观察
                                                        │
                                             T2：公开 Beta Go / No-Go
```

可并行但需明确 owner 的工作：

- 数据/法务 owner：Sprint 0 起持续推进数据来源 Gate；
- 后端 owner：Sprint 1、3、4、5；
- 前端 owner：Sprint 2、6；
- 产品/运营 owner：搜索基准集、别名审核、内容 fixture、运行手册、T1 招募；
- 测试 owner：从 Sprint 1 起维护测试矩阵，Sprint 7 组织演练。

---

## 9. 本计划确认清单

开始编码前，只需确认以下项目：

- [x] 同意以 MySQL + 独立 worker 取代现有本地 JSON scheduler 作为产品数据底座；
- [x] 同意新增隔离的 `agent/src/market_morning/`，不改写交易/回测主路径；
- [ ] Sprint 0 确定 T1 认证服务与邮件服务商的具体选型和 owner；
- [ ] Sprint 0 确定 TDnet、EDINET、公司 IR registry、JPX 主数据、行情/外汇的商务或法务 owner；
- [x] 同意 Sprint 0–7 的顺序，以及“先 T1 观察、后 T2 订阅”的发布纪律；
- [x] 同意 TradingAgents 不进入 MVP，只保留为 V1.5 评审项。

确认后，下一份工程产物应是 **Sprint 0–1 的可执行 backlog**：每张任务卡包含文件边界、接口/迁移、测试、验收与依赖，不直接跳到页面开发。
