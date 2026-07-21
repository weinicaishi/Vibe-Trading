# Market Morning｜环境与 Secrets 矩阵

本文件只登记变量名和责任边界，不保存任何值。真实值必须进入本地未跟踪 `.env` 或部署 secrets manager。

## 环境矩阵

| 能力 | Local | CI | Staging | T1 |
|---|---|---|---|---|
| 产品功能开关 | 默认关闭；人工开启 | 默认关闭 | 仅部署审核后开启 | 全 Gate 签字后开启 |
| 数据库 | 本地 MySQL 或专用开发库 | 临时/专用测试库 | 独立 staging MySQL | 独立 T1 MySQL |
| migration | 开发者执行 | 空库 + upgrade/downgrade 演练 | 独立 migration job | 审批后的 migration job |
| 数据来源 | fixture；真实来源默认禁止 | fixture only | 仅批准的窄范围来源 | 仅已批准来源 |
| 日历/行情 | fixture | fixture | licensed non-fixture | licensed non-fixture |
| 模型 | fixture 或显式开发 adapter | fixture | 选定 provider，真实 usage/cost | 同 staging 已验配置 |
| 邮件 | fake/in-memory | fake | Resend sandbox/已验证域名的窄范围真实地址，或审批后的等价 provider | 邀请用户地址 |
| 产品认证 | synthetic/local adapter 可选 | fake verifier | 真实 OIDC staging tenant | 真实 OIDC T1 tenant |
| 运营认证 | 本地 API Key 兼容或 fake adapter | 按权限 fake verifier | 个人化 OIDC + 最小权限 | 个人化 OIDC + 值班/撤权策略 |
| 监控 | 本地日志/metrics | OpenMetrics、规则与 scrape 模板合同测试 | 版本化 Prometheus rules + 机器身份 scrape + warning/critical/resolved 通知演练 | 值班、告警与 token 轮换启用 |
| fixture/synthetic | 必须显式标识 | 允许 | runtime 禁止 | 禁止 |

## 核心配置名称

| 名称 | 敏感 | 用途 | Local/CI | Staging/T1 |
|---|---|---|---|---|
| `VIBE_MARKET_MORNING_ENABLED` | 否 | API 功能开关 | 默认 `false` | Gate 批准后 `true` |
| `VITE_MARKET_MORNING_ENABLED` | 否 | 前端入口开关 | 默认 `false` | 与后端一致 |
| `VIBE_MARKET_MORNING_SYNTHETIC_EDITION_ENABLED` | 否 | 合成朝刊 | demo 可显式开启 | 必须 `false` |
| `VIBE_MARKET_MORNING_DATABASE_URL` | **是** | 产品 MySQL URL | 本地 secret | secrets manager |
| `VIBE_MARKET_MORNING_DATABASE_ECHO` | 否 | SQL 日志 | 默认 `false` | 必须 `false` |
| `VIBE_MARKET_MORNING_DATABASE_POOL_SIZE` | 否 | 连接池 | 小值 | 容量评估后设置 |
| `VIBE_MARKET_MORNING_DATABASE_POOL_RECYCLE_SECONDS` | 否 | 连接回收 | `1800` 基线 | 按数据库策略设置 |
| `VIBE_MARKET_MORNING_RUNTIME_ENABLED` | 否 | worker/scheduler 二次开关 | 默认 `false` | preflight 完成后 `true` |
| `VIBE_MARKET_MORNING_RUNTIME_FACTORY` | 否 | 最终无参 runtime factory | fixture/custom factory 可选 | 推荐固定为 `src.market_morning.production_runtime_factory:build_runtime_dependencies`；自定义 factory 仍须满足同一严格 contract |
| `VIBE_MARKET_MORNING_PROVIDER_BUNDLE_FACTORY` | 否 | 内置生产 factory 加载的已审 licensed adapters、日历与三项外部 preflight bundle | fixture runtime 不使用；本地集成可用 fake bundle | 使用内置生产 factory 时必填；返回 `ApprovedMarketMorningProviderBundle`，不得在 bundle 或日志中保存 secret |
| `VIBE_MARKET_MORNING_OPENROUTER_MODEL` | 否 | EventBrief 固定模型 ID | mock/批准的测试模型 | owner 批准且实测 structured outputs 的固定版本 |
| `VIBE_MARKET_MORNING_OPENROUTER_API_KEY` | **是** | OpenRouter EventBrief 凭据 | mock，不设真实值 | secrets manager；独立 staging/T1 key 与预算 |
| `VIBE_MARKET_MORNING_OPENROUTER_UPSTREAM_PROVIDERS` | 否 | 逗号分隔的 upstream provider 精确 allowlist | mock slug | 隐私/成本 owner 批准的 1–10 项 |
| `VIBE_MARKET_MORNING_OPENROUTER_ENDPOINT` | 否 | 固定 OpenRouter chat-completions endpoint | 官方 global/EU endpoint | 与合同和数据驻留决定一致 |
| `VIBE_MARKET_MORNING_OPENROUTER_ALLOW_APPROVED_FALLBACKS` | 否 | 是否在 allowlist 内回退 | `false` | 只接受精确 `true/false`，默认 `false` |
| `VIBE_MARKET_MORNING_EDINET_API_KEY` | **是** | 金融厅 EDINET API v2 subscription key | mock，不设真实值 | secrets manager；仅在 EDINET 权利 Gate 批准后注入 worker |
| `VIBE_MARKET_MORNING_EDINET_LOOKBACK_DAYS` | 否 | JST 文档列表订正/撤回回看天数 | `2` | `0–7`，按限频与事故窗口批准 |
| `VIBE_MARKET_MORNING_TDNET_API_KEY` | **是** | JPX 付费 TDnet API access key | mock，不设真实值 | secrets manager；仅在 TDnet 合同／权利 Gate 批准后注入 worker |
| `VIBE_MARKET_MORNING_TDNET_LOOKBACK_DAYS` | 否 | JST 当前及订正／删除历史回看天数 | `2` | `0–7`；API 固定不超过每秒 1 请求 |
| 公司 IR feed 配置（不设通用 env JSON） | 逐公司判断 | 每家公司独立 issuer/provider/index URL/path allowlist/parser | fixture 或 `MockTransport` 配置 | 写入 deployment provider bundle 的已审代码；URL 与 parser 逐公司审批，不接受任意 URL 或通配域名 |
| 日美 calendar feed 配置（不设通用 env JSON） | provider header 可能敏感 | JPX/US 各一份 provider/endpoint/path allowlist/header/parser/freshness/coverage | fixture 或 `MockTransport` | 写入 deployment provider bundle 的已审代码；启动前完整预加载，禁止新闻页推断 |
| 五项 market feed 配置（不设通用 env JSON） | provider header 可能敏感 | 每个 instrument 的 provider/endpoint/path allowlist/header/parser/currency/delay | fixture 或 `MockTransport` | 写入 deployment provider bundle 的已审代码；必须完整覆盖五项并与 global provider mapping 一致 |
| `VIBE_MARKET_MORNING_AUTH_FACTORY` | 否 | 产品 OIDC adapter factory | fake 可选 | 内置 `build_product_auth_adapter` 或审核后的自定义 factory |
| `VIBE_MARKET_MORNING_ADMIN_AUTH_FACTORY` | 否 | 运营 OIDC/角色 adapter factory | fake 可选；缺省仅兼容 API Key | 内置 `build_admin_auth_adapter` 或审核后的个人化最小权限 factory |
| `VIBE_MARKET_MORNING_OIDC_PROVIDER` | 内置 OIDC 时是 | 脱敏 provider ID | `oidc` | 稳定小写标识 |
| `VIBE_MARKET_MORNING_OIDC_ISSUER` | 内置 OIDC 时是 | 精确 token issuer | 测试 HTTPS issuer | secrets/config manager 中批准值 |
| `VIBE_MARKET_MORNING_OIDC_JWKS_URL` | 内置 OIDC 时是 | 固定 JWKS HTTPS URL | 测试端点 | 无重定向的批准端点 |
| `VIBE_MARKET_MORNING_OIDC_AUDIENCE` | 内置 OIDC 时是 | API audience | 测试 audience | Market Morning 专用 audience |
| `VIBE_MARKET_MORNING_OIDC_ALGORITHM` | 否 | 固定非对称 JWT 算法 | `RS256` | provider 批准算法 |
| `VIBE_MARKET_MORNING_OIDC_JWKS_TTL_SECONDS` | 否 | JWKS cache TTL | `300` | `30–3600` |
| `VIBE_MARKET_MORNING_OIDC_HTTP_TIMEOUT_SECONDS` | 否 | JWKS 请求超时 | `5` | `1–15` |
| `VIBE_MARKET_MORNING_OIDC_CLOCK_SKEW_SECONDS` | 否 | JWT 时钟偏差 | `30` | `0–120` |
| `VIBE_MARKET_MORNING_OIDC_SESSION_VALIDATOR_FACTORY` | 内置 OIDC 时是 | 逐请求撤权/session 校验 | fake 可选 | provider-backed fail-closed callback |
| `VIBE_MARKET_MORNING_OIDC_ADMIN_ROLES_CLAIM` | 否 | 运营 roles claim 名 | `roles` | provider 固定 claim |
| `VIBE_MARKET_MORNING_OIDC_ADMIN_ROLE_PERMISSIONS_JSON` | 内置运营 OIDC 时是 | role 到四项权限的显式映射 | fake role | 审核后的最小权限 JSON |
| `VIBE_MARKET_MORNING_EMAIL_WEBHOOK_FACTORY` | 否 | 邮件回调 adapter factory | fake 可选 | provider factory |
| `VIBE_MARKET_MORNING_RESEND_API_KEY` | **是** | Resend 发送凭据 | mock，不设真实值 | secrets manager；只授予发送权限 |
| `VIBE_MARKET_MORNING_RESEND_FROM` | 否 | 已验证的发件身份 | 测试域名 | 已验证的产品发件域名 |
| `VIBE_MARKET_MORNING_RESEND_WEBHOOK_SECRET` | **是** | 当前 Svix webhook HMAC secret | 合成 secret | secrets manager |
| `VIBE_MARKET_MORNING_RESEND_WEBHOOK_SECRET_PREVIOUS` | **是** | webhook 零停机轮换的上一 secret | 通常为空 | 仅轮换窗口设置，完成后清除 |
| `VIBE_MARKET_MORNING_EMAIL_IDENTITY_FACTORY` | 否 | 发送时按内部 user UUID + 伪名 OIDC subject 解析已验证邮箱的 deployment adapter | fake directory | 返回 `MarketMorningEmailIdentityAdapter` 的已审 factory；必须同时回绑两个标识且不得把邮箱写入产品库或日志 |
| `VIBE_MARKET_MORNING_DELIVERY_LINK_BASE_URL` | 否 | 私有朝刊页精确 HTTPS URL | 测试产品 URL | 不允许 IP、userinfo、query、fragment 或路径穿越 |
| `VIBE_MARKET_MORNING_DELIVERY_LINK_SIGNING_SECRET` | **是** | 私有邮件链接 HMAC 主密钥 | 合成 32+ bytes | secrets manager；不得复用 OIDC、数据库或邮件 provider secret |
| `VIBE_MARKET_MORNING_DELIVERY_LINK_SIGNING_SECRET_PREVIOUS` | **是** | 深链零停机轮换上一密钥 | 通常为空 | 仅覆盖在途 delivery job 的短轮换窗口，随后清除 |
| `VIBE_MARKET_MORNING_RUNTIME_ROLE` | 否 | `all/worker/scheduler` | `all` | 分离部署 |
| `VIBE_MARKET_MORNING_ALLOW_FIXTURE_RUNTIME` | 否 | 本地 fixture 逃生阀 | 默认 `false` | 必须 `false` |
| `VIBE_MARKET_MORNING_WORKER_ID` | 否 | 实例标识 | 自动/本地 | 部署注入唯一值 |
| `VIBE_MARKET_MORNING_SCHEDULER_OWNER_ID` | 否 | scheduler lease owner | 自动/本地 | 部署注入唯一值 |
| `VIBE_MARKET_MORNING_WORKER_POLL_SECONDS` | 否 | worker poll | `1` 基线 | 容量评估后设置 |
| `VIBE_MARKET_MORNING_SCHEDULER_POLL_SECONDS` | 否 | scheduler poll | `30` 基线 | 容量评估后设置 |
| `VIBE_MARKET_MORNING_MIGRATION_DATABASE_URL` | **是** | 可丢弃迁移验收库 | 可选 | CI/验收 secret，禁止生产 |

Resend adapter 的 API key 与 webhook secret、邮件深链签名 secret、OpenRouter EventBrief API key、EDINET subscription key、
TDnet access key 只由对应 worker/factory
在内存中读取，不进入共享配置对象、前端或通用日志。OpenRouter adapter 每次请求固定发送
`require_parameters=true`、`data_collection=deny`、`zdr=true`，并用 `only` 将任何回退限制在批准
allowlist；这不能替代账号级日志设置、DPA/隐私审批和 staging 实测。EDINET adapter 固定官方
`https://api.edinet-fsa.go.jp/api/v2`，不接受环境自定义 endpoint，且公开回链不携 subscription key。
TDnet adapter 同样固定 JPX 的 `api.arrowfront.jp` endpoint，access key 同时只发给官方 API 的 header/body；
一次性 S3 URL仅在内存中使用，不进入 cursor、来源记录或日志。
邮箱同样不进入 Market Morning 产品数据库。`VIBE_MARKET_MORNING_EMAIL_IDENTITY_FACTORY` 只保存
可导入的 factory 路径；实际 directory adapter 在投递时接收内部 user UUID 与不可逆伪名 subject，返回
同时回绑这两个标识的 active/verified 地址。返回错用户、错 subject、未验证、停用或非法地址均 fail closed；
异常只转换为稳定错误码，不回显地址或 provider payload。
公司 IR 没有全局 secret 或任意 URL 环境变量；每个 `company_ir_*` adapter 必须由 deployment provider bundle
以精确 URL/path allowlist 和已审 parser 构造。
日美 calendar 和五项 market feed 同样不接受任意 JSON env；具体 endpoint/header/parser 必须写入
已审 deployment provider bundle。核心 adapter 禁止私网 DNS、环境代理和 redirect，并在 runtime 启动前要求
完整日历 coverage 与五项 instrument registry；这些工程约束仍不能替代数据许可。
其他 deployment adapter/factory 还会使用数据 provider 凭据、OIDC
issuer/audience/JWKS、source registry 和监控凭据；这些名称由部署仓库定义。

## 进程边界

- API：用户 API、内部运营 API、邮件 webhook；不运行 scheduler。
- Worker：领取 durable jobs；启动前要求精确 schema 和五项 runtime preflight。
- Scheduler：持有数据库 lease，按 licensed calendar 排队；可与 worker 同镜像但生产建议独立角色。
- Migration job：只执行 Alembic，成功后退出；不得与常驻 API 自动并发迁移。
- MySQL：独立持久卷、备份、TLS、最小权限账号和 UTC session。
