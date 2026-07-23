# Market Morning｜本地开发与迁移

Market Morning 是 Vibe-Trading 内默认关闭的独立产品垂直层。未设置
`VIBE_MARKET_MORNING_ENABLED=true` 时，不会创建数据库连接，也不会改变现有
研究、回测、交易或前端行为。

## 环境变量

```dotenv
VIBE_MARKET_MORNING_ENABLED=true
VIBE_MARKET_MORNING_DATABASE_URL=mysql+asyncmy://user:password@127.0.0.1:3306/market_morning?charset=utf8mb4
VIBE_MARKET_MORNING_DATABASE_ECHO=false
VIBE_MARKET_MORNING_DATABASE_POOL_SIZE=5
VIBE_MARKET_MORNING_DATABASE_POOL_RECYCLE_SECONDS=1800
# 产品用户与运营人员使用两个隔离的内置 Bearer/OIDC adapter。
VIBE_MARKET_MORNING_AUTH_FACTORY=src.market_morning.oidc_auth:build_product_auth_adapter
VIBE_MARKET_MORNING_ADMIN_AUTH_FACTORY=src.market_morning.oidc_auth:build_admin_auth_adapter
VIBE_MARKET_MORNING_OIDC_PROVIDER=oidc
VIBE_MARKET_MORNING_OIDC_ISSUER=https://identity.example.com/market-morning
VIBE_MARKET_MORNING_OIDC_JWKS_URL=https://identity.example.com/market-morning/jwks
VIBE_MARKET_MORNING_OIDC_AUDIENCE=market-morning-api
VIBE_MARKET_MORNING_OIDC_ALGORITHM=RS256
VIBE_MARKET_MORNING_OIDC_SESSION_VALIDATOR_FACTORY=deployment.identity:build_session_validator
VIBE_MARKET_MORNING_OIDC_SESSION_CLAIM=__access_token_sha256__
VIBE_MARKET_MORNING_OIDC_AUTH_TIME_CLAIM=https://market-morning.invalid/claims/auth-time
VIBE_MARKET_MORNING_OIDC_ADMIN_ROLES_CLAIM=https://market-morning.invalid/claims/roles
VIBE_MARKET_MORNING_OIDC_ADMIN_ROLE_PERMISSIONS_JSON='{"market-morning-reader":["operations.read"],"market-morning-reviewer":["content.review"]}'
# 独立进程仍需二次显式启用；内置 factory 负责最终装配，部署侧只提供已审 provider bundle。
VIBE_MARKET_MORNING_RUNTIME_ENABLED=false
VIBE_MARKET_MORNING_RUNTIME_FACTORY=src.market_morning.production_runtime_factory:build_runtime_dependencies
VIBE_MARKET_MORNING_PROVIDER_BUNDLE_FACTORY=deployment.market_morning_providers:build_provider_bundle
# Resend webhook 使用仓库内置的 Svix 验签／事件解析 factory；未配置时固定 fail closed。
VIBE_MARKET_MORNING_EMAIL_WEBHOOK_FACTORY=src.api.market_morning_resend_webhook_factory:build_resend_webhook_adapters
VIBE_MARKET_MORNING_RUNTIME_ROLE=all
VIBE_MARKET_MORNING_ALLOW_FIXTURE_RUNTIME=false
```

仓库 Compose 默认仍只启动 API。配置完成后，常驻 runtime 通过显式 profile 启动：

```bash
docker compose --profile market-morning up -d market-morning-runtime
```

该服务与 API 分离、不发布端口、使用只读根文件系统，并以 `on-failure:5` 限制启动失败重试。它会
显式禁用共享 API 镜像中探测 `:8899/live` 的 HTTP healthcheck，因为 worker 不提供 HTTP 服务；运行健康
必须由进程退出状态、runtime preflight 和运营指标判断，不能用不存在的 API 端口判断。数据库
revision、provider bundle 或五项 preflight 不满足时会退出而不是领取任务。默认 role 为 `all`，即同一
runtime 进程内运行 durable worker 和带数据库 lease 的 scheduler；需要按角色拆成多个部署实例时，分别
注入 `VIBE_MARKET_MORNING_RUNTIME_ROLE=worker` 和 `scheduler`，但不得同时运行默认 `all` 实例。

真实凭据只能进入本地未跟踪 `.env` 或部署 secrets。数据库必须使用 MySQL
8.x、InnoDB、`utf8mb4`，服务器和连接会话时区统一为 UTC。

## 一键本地浏览器 Demo

不配置数据库、OIDC 或外部 provider 时，可从前端目录启动一个只存在于 Vite 开发服务器内存中的
可交互 Demo：

```bash
cd frontend
npm run dev:market-morning-demo
```

固定访问地址为 `http://127.0.0.1:5901/market-morning`。命令使用 `--strictPort`；如果端口已被占用会
直接失败，不会静默切换到另一个地址。Demo 提供今日朝刊、关注股搜索/增删、丰田个股研究与私人
笔记、通知设置及账户数据导出所需的最小 JSON contract；状态只保存在当前 Vite 进程内存，重启即
恢复初始值。

`VITE_MARKET_MORNING_DEMO=true` 只有在 Vite `development` mode 下才会安装 middleware；production
或其他 mode 即使误设同名变量也不会启用。朝刊响应固定标记
`data_mode=synthetic_fixture`、`fixture_version=local-browser-demo-v1`，页面持续显示“デモデータ”，
引用使用不可访问的 `example.invalid`。production build 会额外扫描并拒绝本地 Demo payload 泄漏。

该入口只用于 UI/交互验收，不连接产品 MySQL，不发送邮件，不调用 Auth0、TDnet、EDINET、公司 IR、
行情或模型，也不构成真实 provider、T1 或连续 staging 日证据。

## 初始化数据库

先由数据库管理员创建 database 和最小权限用户，再从仓库根目录执行：

```bash
alembic -c agent/alembic-market-morning.ini upgrade head
```

生成只读 SQL 供审核：

```bash
alembic -c agent/alembic-market-morning.ini upgrade head --sql
```

回滚一版：

```bash
alembic -c agent/alembic-market-morning.ini downgrade -1
```

不要在生产数据库直接运行自动生成但未经审核的迁移，也不要手工修改迁移已管理的表。

## 验证

内部 readiness endpoint：

```text
GET /market-morning/_internal/ready
```

- feature flag 关闭：`503 / disabled`；
- 已开启但 URL 不合法：`503 / misconfigured`；
- 数据库不可达：`503 / unavailable`；
- MySQL 可连接且 `alembic_version` 精确等于当前产品 revision：`200 / ready`；
- MySQL 可连接但 revision 缺失或过期：`503 / misconfigured`。

该端点使用独立运营认证。生产由 `VIBE_MARKET_MORNING_ADMIN_AUTH_FACTORY` 验证 Bearer 并要求
`operations.read`；未配置 factory 时仅为本地开发和迁移兼容回退到 Vibe API Key。它不代表产品
用户认证已经完成，不能直接作为 T1 用户登录方案。

部署静态预检 endpoint：

```text
GET /market-morning/_internal/deployment-preflight
```

该端点在不加载 deployment factory、不调用外部 provider 的前提下，汇总 feature、精确数据库
revision、产品与运营认证 factory、前端公开 runtime config、runtime、邮件 webhook、私有深链 URL/密钥、
synthetic data 和 fixture runtime 配置。若选择内置生产 runtime factory，还会要求已配置 provider bundle factory。全部满足
时返回 `200 / configuration_ready`，否则返回 `503 / blocked` 以及稳定的
`blocking_checks` 代码。响应只含布尔值和代码，不返回数据库 URL、factory 路径、secret 或底层错误。
`scope=static_configuration_and_database` 且 `counts_as_t1_evidence=false`：它是启动前的必要检查，
不能替代 runtime 五项外部 preflight、真实 OIDC/provider 验收或连续 staging 演练。

## 内部运营与内容审核

内部运营页面 `/market-morning-ops` 和产品用户 OIDC 完全分离。生产必须通过
`VIBE_MARKET_MORNING_ADMIN_AUTH_FACTORY` 返回可识别个人的 operator 与最小权限；未配置时虽可在
本地继续使用 Vibe API Key 兼容路径，但 deployment preflight 会以
`admin_auth_factory_missing` 阻止上线。页面顶部会展示 `configuration_ready / blocked` 状态与稳定
阻断码，并明确该状态不计为 T1 证据。权限分为 `operations.read`、`content.review`、
`publication.control`、`access.manage`；所有变更审计使用 adapter 返回的稳定
`actor_reference`。对应 API：

```text
GET    /market-morning/_internal/operations/summary
GET    /market-morning/_internal/operations/metrics
GET    /market-morning/_internal/event-briefs
POST   /market-morning/_internal/event-briefs/{brief_id}/review
GET    /market-morning/_internal/issuer-aliases
POST   /market-morning/_internal/issuer-aliases/{alias_id}/review
GET    /market-morning/_internal/event-merge-candidates
POST   /market-morning/_internal/event-merge-candidates/{candidate_id}/review
GET    /market-morning/_internal/content-reports
POST   /market-morning/_internal/content-reports/{report_id}/review
POST   /market-morning/_internal/operations/halts
DELETE /market-morning/_internal/operations/halts/{edition_date}
POST   /market-morning/_internal/private-beta/invites
DELETE /market-morning/_internal/private-beta/invites/{invite_id}
POST   /market-morning/_internal/users/{user_id}/access
```

运营摘要只查询聚合状态、脱敏错误码、来源健康、最近 global run、投递和 engagement 计数；不选择
用户 ID、个人备注、邮箱、token、job/model payload 或来源 URL。EventBrief 批准只允许
`published + auto_validated`，拒绝会从后续内容查询中排除该 brief；两者都不修改不可变 payload
和 hash，并写审计记录。publication halt 只能停发或撤销，不能强制开市、绕过内容 Gate 或补发
08:30 后邮件。值班流程和告警阈值见 `docs/market-morning-operations-runbook.md`。

别名和跨来源候选审核同样使用固定原因码和终态状态机。别名批准会拒绝过期记录以及已被另一
发行人批准的同一规范化值；队列不返回内部 `source_reference`。跨来源候选批准会再次确认两条
事件属于同一 issuer，但只把候选标为 approved 并写审计，不会修改事件、事件来源或历史朝刊。
因此 approved candidate 不能解释为数据库已完成事件合并。

`operations/metrics` 以 OpenMetrics 1.0 文本返回与运营摘要同源的 1–168 小时窗口聚合值，供
Prometheus 或兼容监控器抓取。生产抓取身份必须具备 `operations.read`，不能公开暴露。所有窗口计数都使用 gauge，
因为记录离开观察窗口后数值可以下降；费用按 provider 返回的币种 millionth unit 分开输出。
出口不包含用户／邮箱、run ID、刊期、来源 URL、halt 原因、provider request ID 或任何 payload。
版本化 Prometheus 规则、scrape 示例和 Alertmanager 路由基线位于
`deploy/market-morning/monitoring/`：规则直接消费稳定 `code`/`severity`，另检测 scrape down、
snapshot missing/stale；scrape 示例只通过 secret 文件读取 Bearer；Alertmanager 将 warning 与
critical 分路，webhook URL 只从 `url_file` 读取，并为两路都固定 `send_resolved=true`。仓库不包含
receiver URL、认证 header 或个人联系方式。真实 target、机器身份 token 刷新和 receiver 仍由部署
环境完成；`vibe-trading-market-morning-monitoring-drill` 只校验真实演练输入并生成 privacy-safe
证据，不会主动触发告警，也不能把模板或 synthetic 通知变成 staging 证据。

私测邀请只持久化 bearer token 的 SHA-256，原始 token 仅在创建响应中显示一次；运营可撤销尚未
使用的邀请，也可按枚举原因停用或恢复用户。停用会同步关闭 email opt-in 和最近活动资格，不能
替代账户删除。生产 adapter 返回的 `actor_reference` 会进入审计；只有本地兼容路径才记录固定的
`vibe-api-key-operator`。真实 OIDC issuer/audience/算法、角色映射和 session 失效仍必须由部署身份层
验收。

当前直接按 `agent/tests/test_market_morning_*.py` 运行的普通验证基线是 Market Morning 后端
`809 passed, 15 skipped`；在没有显式提供专用数据库和确认参数时，
12 项真实 MySQL 业务 probe 与 3 项破坏性 migration probe 仍按安全策略跳过。
另已在隔离的官方 MySQL 8.0 容器执行完整在线链路：12/12 业务 probe、3/3 migration probe 以及
bootstrap SQL 直接导入全部通过。另于 2026-07-22 对远程专用 acceptance/migration 数据库再次执行
`environment=remote_mysql_8_retest` 的 12/12 与 3/3；证据分别位于
`docs/evidence/market-morning/mysql-acceptance-remote-retest-2026-07-22.json` 和
`docs/evidence/market-morning/mysql-migration-remote-retest-2026-07-22.json`。这些 manifest 均不计 staging
日；远程产品主库仍保持只读核对结果 `0004`/14 表，须在正式维护窗口备份后另行升级。
受控 schema change CLI 已在候选 revision `bf71b387f7cc424da4125071f0a0a904ad0e83b1` 上重新完成实际只读
preflight，hash-only 证据位于
`docs/evidence/market-morning/product-schema-preflight-release-bf71b387-2026-07-22.json`；主库前后均为
`0004`/14 表、`execution=null`，该证据没有执行 Alembic，不能替代备份和产品库变更批准。
前端 341 项、Market Morning 范围 Ruff 与前端
production build 通过。
仓库锁定运行依赖和 `.[dev]` 测试依赖补齐后，按仓库约定排除独立 `e2e_backtest` 目录执行
`python -m pytest agent/tests --ignore=agent/tests/e2e_backtest -q`，当前结果为
`6241 passed, 24 skipped`。24 项跳过均有明确原因：15 项在普通回归中未显式启用专用 MySQL、5 项因合成
因子面板不满足特定前置条件、4 项等待 `TUSHARE_TOKEN`；不存在未知 skip。CI/dev 必须使用隔离、
可写的运行目录，并允许仅绑定 `127.0.0.1` 临时端口以执行 MCP 集成测试。仓库级
`ruff check agent` 目前还会命中上游既有 lint 债务；
Market Morning 的准入命令必须使用下面记录的产品范围，不能把仓库级失败隐去或误报为本模块失败。

## 发行人主数据与搜索

当前实现只接收来源 adapter 已解码的表格行，并使用合成 fixture 验证搜索规则；尚未下载或公开使用 JPX 主数据。接入真实来源前必须完成使用、缓存、展示和再分发许可核验。

证券代码按 4 字符处理：既支持传统纯数字代码，也支持 2024 年起按 JPX／SICC 规则在第 2、4 位使用英文字母的新代码。搜索仅提供代码精确匹配、正式名称规范化前缀和已批准别名前缀；不做错别字纠正、语义猜测或自动生成别名。

```text
GET /market-morning/issuers/search?q=987A&limit=10
```

该接口默认关闭；启用后使用下述独立产品 Bearer 认证，不接受内部 Vibe API Key 或客户端自报
user ID。搜索结果不返回用户字段；页面只用同一认证会话已经加载的关注列表判断“已添加”，不会
让客户端提交或猜测其他用户身份。

## 关注股事务边界

关注股 API 契约已经建立：

```text
GET    /market-morning/watchlist
POST   /market-morning/watchlist
PATCH  /market-morning/watchlist/{issuer_id}
DELETE /market-morning/watchlist/{issuer_id}
```

修改操作先以 `SELECT ... FOR UPDATE` 锁定对应用户行，再检查活跃数量；MySQL generated column 与 unique constraint 作为重复活跃记录的最终防线。每位用户最多 10 只活跃关注股，重复添加和重复删除返回现有状态，不重复写入。

产品认证 HTTP 边界已接入内置标准 OIDC/JWKS factory，同时保留 deployment-owned 自定义 factory：

```text
VIBE_MARKET_MORNING_AUTH_FACTORY=src.market_morning.oidc_auth:build_product_auth_adapter
```

factory 必须返回 `MarketMorningProductAuthAdapter`。核心只接受 Bearer token，并把它交给
`verify_bearer`；验证成功后再按 opaque `external_subject` 查询 `mm_users`，同时要求账户为
`active`、未删除且订阅状态属于 `private_beta / trialing / active`。缺凭据返回 401，身份有效但
无产品权限返回 403，factory／provider／数据库不可用返回脱敏 503。token 不进入 SQL、响应或业务
日志；内部 Vibe API Key 和客户端自报 user ID 都不能绕过该边界。

内置 adapter 已固定实现下列通用安全边界；部署仍必须提供真实 provider 参数和逐请求 session
validator，不能把“能解析 JWT”视为“已认证”：

- 固定允许的 issuer、audience 与签名算法，拒绝 token 自报算法或任意 JWKS URL；
- 验证签名、`exp`、适用时的 `nbf/iat`，并安全处理 JWKS key rotation；
- 把缺失／错误 claims 和无效／已失效会话映射为 `MarketMorningAuthenticationRejected`；
- 把 provider/JWKS 暂时不可用映射为异常，使核心返回 503，而不是错误接受或永久拒绝；
- 每次请求检查 provider 会话／token 撤销语义；仅清除浏览器状态不算 session invalidation；
- 对高风险动作返回来自已验证 `auth_time`（或 provider 等价字段）的 timezone-aware
  `authenticated_at`；核心只接受 5 分钟内的近期认证，并拒绝缺失、过旧或异常未来时间；
- 返回稳定、区分命名空间且不超过 255 字符的 opaque subject，不返回邮箱作为身份主键。

内置实现只接受固定的非对称 `RS* / PS* / ES*` 算法，拒绝 `none`、HMAC 算法混淆、token 自报
JWKS URL、重定向、非 HTTPS、非 443 端口、带 userinfo/query/fragment 的端点和非公网 literal IP。
JWKS 响应限制为 64 KiB/64 keys，缓存过期或遇到未知 `kid` 时仅刷新一次；provider/JWKS/session
服务不可用返回脱敏 503，签名、claims、角色或已撤销 session 无效返回 401。产品 subject 与运营
actor 都由 `issuer + sub` 做不可逆 SHA-256 命名空间化，不持久化原始 provider subject。运营角色
只可映射到 `operations.read / content.review / publication.control / access.manage` 四项权限。

`VIBE_MARKET_MORNING_OIDC_SESSION_VALIDATOR_FACTORY` 必须返回 callable；它会在签名和 claims 验证后
收到原始 token（仅内存）与不可变 claims，并逐请求返回严格布尔值 `True`。返回 `False` 表示已撤销
或 inactive；异常表示 session provider 不可用并返回 503。生产实现应使用 provider 的 `sid/jti`
撤权、introspection 或等价服务，禁止实现为永久 `True`。静态 deployment preflight 在选择内置
factory 后会额外检查 issuer、JWKS、audience、session validator 和运营 role mapping，缺项以稳定
阻断码返回且不暴露配置值。

实现时以 [OpenID Connect Core 1.0 的 token validation](https://openid.net/specs/openid-connect-core-1_0.html#IDTokenValidation)
和 [RFC 8725 JWT Best Current Practices](https://www.rfc-editor.org/rfc/rfc8725.html) 为最低安全基线，
并叠加所选 provider 对 access token、JWKS cache 与撤销的正式文档；不能把 ID Token 直接当成任意
API access token 使用。

当前仓库提供的是上述 provider-neutral 边界，不包含某一家 OIDC 的 JWKS adapter 或凭据。未配置
factory 时固定返回脱敏 503，测试仍可通过 dependency override 注入已验证 principal。

前端已内置官方 `@auth0/auth0-spa-js` 2.23 adapter。`main.tsx` 会先请求同源、无认证且
`Cache-Control: no-store` 的 `GET /market-morning/runtime-config`，再在 React `createRoot` 前注册 adapter；
生产环境只使用该运行时响应，响应缺失、非法或配置不完整时 fail closed，仅关闭 Market Morning，
不阻断普通 Vibe-Trading 页面。Vite development 可显式回退到 `VITE_*`，但这些构建期值不再是
staging/T1 配置来源，因此同一个不可变 Docker/CI 镜像可以跨环境部署。
SDK 本体只在访问产品或运营私有路由并开始认证初始化时动态加载，不增加普通 Vibe 路由的首屏 SDK
负担。启用时必须同时配置两个独立 SPA application：

```text
VITE_MARKET_MORNING_AUTH_PROVIDER=auth0
VITE_MARKET_MORNING_AUTH0_DOMAIN=tenant.jp.auth0.com
VITE_MARKET_MORNING_AUTH0_AUDIENCE=https://api.market-morning.example
VITE_MARKET_MORNING_AUTH0_PRODUCT_CLIENT_ID=<public SPA client id>
VITE_MARKET_MORNING_AUTH0_OPERATOR_CLIENT_ID=<public SPA client id>
```

这些 `VITE_*` 只用于 standalone Vite development。Docker/staging/T1 必须改为在 API/worker 的
`agent/.env` 或 secrets/config manager 中提供：

```text
VIBE_MARKET_MORNING_PUBLIC_AUTH_PROVIDER=auth0
VIBE_MARKET_MORNING_PUBLIC_AUTH0_DOMAIN=tenant.jp.auth0.com
VIBE_MARKET_MORNING_PUBLIC_AUTH0_AUDIENCE=https://api.market-morning.example
VIBE_MARKET_MORNING_PUBLIC_AUTH0_PRODUCT_CLIENT_ID=<public SPA client id>
VIBE_MARKET_MORNING_PUBLIC_AUTH0_OPERATOR_CLIENT_ID=<public SPA client id>
```

API 通过无认证的 `GET /market-morning/runtime-config` 只返回以上公开值、状态和稳定阻断码，
不返回 issuer、JWKS URL、数据库 URL、secret 或 token。正式前端先加载该配置再挂载 React；
加载失败或配置不完整时仅关闭 Market Morning。开发模式可以回退 `VITE_*`，production 不回退，
因此 CI 构建的同一不可变镜像可用于不同环境。

这些都是浏览器可见的公开标识，严禁填写 client secret。adapter 固定使用 Authorization Code
with PKCE、`cacheLocation=memory`、offline rotating refresh token、禁用 iframe fallback、10 秒 HTTP
timeout，并为产品与运营使用不同 client/cache 实例。产品 callback 是 `/market-morning`，运营 callback
是 `/market-morning-ops`；两者的 logout return URL 都是站点根路径。Auth0 Dashboard 必须精确登记相同
Callback/Logout/Web Origin，不能使用通配符。OAuth callback 的 `code/error/state` 在成功或失败后都会
立即从地址栏清除；恢复路径再次经过同源、无 fragment 校验，provider 错误细节不会显示给用户。

登出先尽力调用 SDK refresh-token revocation，再始终执行 Auth0 provider logout；即使 revocation endpoint
暂时失败，也会卸载当前页私有数据并清除 provider/local session。后端逐请求 session validator 仍是
撤权的权威 Gate，前端行为不能替代它。真实 tenant 还必须启用 refresh token rotation/reuse detection，
并在左侧 `Applications → APIs → Market Morning Staging API → Settings` 中启用 `Allow Offline Access`
（它不会显示在 Product/Operator SPA Application 的 Settings）。Product 和 Operator 两个 SPA Application
都必须在 Settings 的 Refresh Token Rotation 区域启用 `Allow Refresh Token Rotation`；前端会请求
`openid profile offline_access`，否则 900 秒 access token 到期后无法静默续期。随后再验证允许 URL、
Action 注入的运营 roles、退出、停用与账户删除后的撤权。
实现依据为 [Auth0 SPA SDK](https://auth0.com/docs/libraries/auth0-single-page-app-sdk)、
[Auth0ClientOptions](https://auth0.github.io/auth0-spa-js/interfaces/Auth0ClientOptions.html) 与
[LogoutOptions](https://auth0.github.io/auth0-spa-js/interfaces/LogoutOptions.html) 的官方合同。

#### Alpha A 本地 Auth0 / MySQL 联调

真实 Auth0 的本地回调必须从 `http://localhost:5173` 发起；`localhost` 与 `127.0.0.1`
在 Auth0 Allowed Callback URLs 中不是同一个 origin。后端仍只绑定 `127.0.0.1`。为了防止本地浏览器
联调误读 `~/.vibe-trading/.env` 中的产品数据库，Alpha A 不直接运行普通 `uvicorn` 或
`scripts/dev up`，而使用 fail-closed 入口：

```bash
# agent/.env 同时配置产品 URL 和独立、库名含 staging 的 Alpha A URL
VIBE_MARKET_MORNING_DATABASE_URL=mysql+asyncmy://...
VIBE_MARKET_MORNING_STAGING_DATABASE_URL=mysql+asyncmy://...

# 只检查隔离目标；不会打印 URL、主机、库名或凭据
PYTHONPATH=agent .venv/bin/python -m src.market_morning.alpha_a_dev_cli --check-only

# 后端固定 loopback:8898，并强制 runtime disabled
PYTHONPATH=agent .venv/bin/python -m src.market_morning.alpha_a_dev_cli

# 另一个终端启动前端；浏览器必须打开 http://localhost:5173
cd frontend
VITE_API_URL=http://127.0.0.1:8898 npm run dev -- --host 127.0.0.1 --port 5173 --strictPort
```

启动器使用 python-dotenv 解析 `agent/.env`，因此密码中的 `#` 必须按数据库 URL 规则进行 percent
encoding，但不会被 shell 的注释语义截断。它拒绝缺失的 staging URL、非 MySQL URL、库名不含
`staging`、与产品目标相同或非 loopback 的目标，并无条件把 durable runtime 关闭。该联调仍固定
`counts_as_staging_day=false`，不能代替非 localhost HTTPS Staging、授权数据、邮件、监控或连续五个
JPX 交易日证据。

底层仍保留 provider-neutral 的 OIDC lifecycle 端口。非 Auth0 部署侧必须在 React `createRoot` 之前注册产品与
运营两个隔离 scope 的 SDK wrapper；核心会在任何私有 API Provider／运营数据请求挂载前等待
`initialize()`，并在每次请求前调用 `getAccessToken()`。登录和登出只接受同源、无 fragment 的相对
返回路径，前端核心不会把 access/refresh token 写入 localStorage、sessionStorage、URL 或业务状态：

```ts
import { setMarketMorningAuthLifecycleAdapter } from "@/lib/marketMorningAuth";

setMarketMorningAuthLifecycleAdapter("product", {
  initialize: () => productSession.initialize(),
  getAccessToken: () => productSession.getAccessToken(),
  login: (returnTo) => productSession.login(returnTo),
  logout: (returnTo) => productSession.logout(returnTo),
});
setMarketMorningAuthLifecycleAdapter("operator", {
  initialize: () => operatorSession.initialize(),
  getAccessToken: () => operatorSession.getAccessToken(),
  login: (returnTo) => operatorSession.login(returnTo),
  logout: (returnTo) => operatorSession.logout(returnTo),
});
```

两个 scope 相互隔离并各自只初始化一次；并发请求共享同一个初始化 Promise，初始化失败后可由界面
显式重试。产品私有壳在 401 时提供登录入口，运营控制面也提供独立的运营登录与登出；未注册 lifecycle
adapter 时仍保留原本的本地开发行为。产品请求绝不复用 Vibe API Key；运营请求仅在未注册任何
operator provider/lifecycle 的本地兼容模式下回退旧 API Key。回调返回空值时请求不带 Authorization，
由后端 401；回调异常或返回超长、带首尾空白／控制字符的 token 时在发起网络请求前失败。

邮件专用链接的 fragment 会在页面首次 effect 中立即移出地址栏，仅暂存在当前 React 内存；只有产品
认证初始化和用户数据加载成功后才兑换。若 SDK 使用整页重定向，内存 token 会随页面卸载而丢失，界面
会要求用户在登录后重新打开邮件链接，禁止为了“自动续接”把 token 落入浏览器持久存储。

上述 lifecycle contract、产品/运营界面和 Auth0 SDK wrapper 的 PKCE、内存 cache、rotating refresh、
provider logout/refresh-token revocation 代码与并发/失败测试已经完成。生产仍须提供真实 Auth0 tenant、
两套 SPA client、允许 URL 和 Action/role 配置，并把后端 session validator 与身份目录 adapter 接到同一
tenant；跨页面、跨会话、停用和账户删除 E2E 通过前不能视为认证上线。

私测邀请接受 route 已预留：

```text
POST /market-morning/private-beta/invitations/accept
```

请求体只含一次性 token；external subject 必须来自独立的、已验签
`MarketMorningOnboardingPrincipal`，客户端不能提交 subject 或 user ID。该 dependency 使用同一个
deployment verifier，但邀请接受前不查询用户表；只有生产 adapter 完成上述完整验证后才可用。

## 设置、说明确认、数据导出与账户删除

Sprint 2 的用户设置 API 契约已经建立：

```text
GET   /market-morning/settings
PATCH /market-morning/settings
POST  /market-morning/consents
GET   /market-morning/account-data-export
POST  /market-morning/account-deletion-requests
```

- MVP 时区固定为 `Asia/Tokyo`，邮件通知使用显式 `email_opt_in`，空 PATCH 和其他时区返回 422。
- 风险说明与数据说明按 `consent_type + consent_version` 独立保存；接受、撤回和重复请求均有明确状态，不以单个布尔值覆盖历史版本。
- 数据导出从认证 principal 取 user ID，返回账户、同意、关注股、朝刊、阅读状态、来源打开、个人
  备注、投递状态、telemetry、审计和删除申请；显式排除 deep-link token hash、邀请 token hash、
  provider message ID 与 job payload。前端下载为本地 JSON，不在服务端另存导出文件。
- 删除入口要求 provider 已验证且不超过 5 分钟的近期认证；随后同步创建 `pending` 请求，并在同一
  事务中幂等排入最高优先级 `account_deletion` durable job。提交成功时账户立即进入
  `deletion_pending`、关闭 email opt-in 并清除活动资格，普通产品 API 从下一请求起返回 403，
  不等待异步 worker 才撤销访问。删除 endpoint 自己仍允许携带近期认证的 `deletion_pending`
  principal，以便首次响应丢失时返回原 request ID；suspended/deleted 用户仍不能调用。
- 删除 worker 锁定申请和用户后，在一个事务中清除同意、关注股、私有朝刊与交互、个人备注、
  投递记录和 user-scoped job；analytics 与审计仅解除用户外键，外部 subject 改为不可逆
  `deleted:<sha256>`，账户进入 `deleted`。全市场官方来源、normalized event 和法定审计不删除。
- worker 重试按 request ID 幂等；已完成申请不会再次执行破坏性 SQL。核心已经强制删除前 5 分钟
  re-auth 并在请求事务内撤销产品访问；生产 OIDC adapter 仍须验证真实 provider `auth_time` 和
  session/token 撤销状态，不能用客户端自报时间或普通 token 签发时间代替。
- 删除事务发现同一用户仍有 running job 时会返回脱敏可重试错误，等待该 job 退出后再清除；不会
  在其他 worker 仍持有用户 payload 时直接删除其 durable job 行。
- 设置、说明确认和删除请求都与审计记录处于同一数据库事务。

这些接口与关注股接口共用同一个默认拒绝的产品认证边界。未配置 adapter 时不会进入用户数据库；
配置后也只使用已验证 subject 映射出的 active principal，不接受客户端自报 user ID。

## 脱敏 telemetry

`mm_analytics_events` 只接收已注册事件名称和每个事件自己的属性白名单。当前允许的事件包括 `issuer_search_performed`、`watchlist_added`、`watchlist_removed`、`watchlist_updated`、`settings_updated`、`consent_recorded`、`account_deletion_requested`，以及后续朝刊使用的 `edition_opened`、`source_opened`、`delivery_clicked`。

禁止写入完整邮箱、搜索原文、关注股私有标签、个人备注和来源 URL。关注股写操作只记录操作结果与活跃数量；重复且未发生状态变化的请求不重复计数。telemetry 写入与对应业务修改共用事务，失败时一起回滚。

当前迁移链为：

```text
0001_market_morning_foundation
  -> 0002_market_morning_watchlist
  -> 0003_market_morning_settings
  -> 0004_market_morning_sources_events
  -> 0005_market_morning_editions
  -> 0006_market_morning_jobs
  -> 0007_market_morning_manual_overrides
  -> 0008_market_morning_market_snapshots
  -> 0009_market_morning_global_runs
  -> 0010_market_morning_event_briefs
  -> 0011_market_morning_delivery_attempts
  -> 0012_market_morning_delivery_webhooks
  -> 0013_market_morning_edition_interactions
  -> 0014_market_morning_issuer_research
  -> 0015_market_morning_beta_privacy
  -> 0016_market_morning_model_usage
  -> 0017_market_morning_content_reports
  -> 0018_market_morning_auth_sessions
```

`0007_market_morning_manual_overrides` 增加仅允许“停发”的运营覆盖：同一
`edition_date` 只能存在一个 active publication halt，创建与撤销都写入审计日志；相同原因
重复创建返回已有记录，不同原因会明确冲突。该覆盖不能把休市日强制改为开市日，也不能绕过
日历不可用时的 fail-closed 决策。

`0008_market_morning_market_snapshots` 保存不可变市场观察值。当前固定品种为 Nikkei 225、
S&P 500、Nasdaq Composite、DJIA 和 USD/JPY；每条记录必须带 provider、`session_date`、
`as_of`、明确的延迟状态、精确数值和 payload hash。发布 Gate 要求五项全部存在、业务交易日
与预期一致且每项只有一个候选，否则阻止发布。除显式 `fixture_` adapter 外，现已提供
`LicensedMarketDataAdapter`：每个实例只服务一个 instrument/provider/endpoint，精确限制 HTTPS
hostname/path、currency 和 delay status，使用公网 DNS、禁环境代理/redirect、有界响应和 strict JSON
contract；固定 query 只能包含非敏感参数，API key/token/signature 必须由 header factory 即时提供；
数值只接受十进制字符串，不接受 JSON float。具体 endpoint/header/parser 仍必须在许可批准
后由 deployment factory 注入。durable market-snapshot handler 只有显式配置 adapter 时才会注册。

`0009_market_morning_global_runs` 增加 `mm_global_edition_days`、`mm_global_edition_runs` 和
`mm_global_edition_items`。date row 是同刊期的数据库聚合锁；07:00、07:15、07:30、08:00、
08:30、late 映射为确定性 run version。run 按 `draft → running → complete/partial/failed/late`
转移，generated current-success date + unique constraint 保证同日最多一个 current success；成功
manifest 固化五项 snapshot ID 与 hash，旧成功版本只取消 current 标记，不被覆盖。

`0010_market_morning_event_briefs` 增加版本化 `mm_event_briefs` 与
`mm_event_brief_sources`。同一 event revision、schema、model 和 prompt 组合只有一个生成记录；
published／degraded 行必须持有至少一个具体来源外键。生成尝试、失败码、验证错误、不可变 payload
hash 与有序来源 manifest 都会持久化，终态不能被不同内容覆盖。

`0011_market_morning_delivery_attempts` 增加隐私安全的邮件提醒生命周期。唯一
`user_id + edition_date + channel` 保证同一用户同日最多一次应用层提醒；provider message ID 和
幂等键也分别唯一。表内只保存 deep-link token 的 SHA-256，不保存原始 token 或邮箱地址；
pending／sending／sent／delivered／failed／clicked／suppressed 状态、尝试次数与脱敏失败码可审计。

`0012_market_morning_delivery_webhooks` 增加验签后才允许写入的 provider event 审计信封。
同一 provider event ID 幂等，库内只保存 payload SHA-256，不保存原始 webhook 或签名；
delivered／failed／clicked 事件以单调状态机更新投递记录，迟到事件只记为 stale，不会把
delivered／clicked 倒退，未知 provider message ID 则记为 unmatched。

外部回调入口为 `POST /market-morning/webhooks/email/{provider}`。该 route 不使用产品 OIDC 或
Vibe API Key，而以邮件供应商签名作为认证边界；它会以流式方式把原始请求体限制在 1 MB，先从
`VIBE_MARKET_MORNING_EMAIL_WEBHOOK_FACTORY=module:function` 取得部署侧 adapter，再依次执行
验签、标准化和原子入库。factory 必须返回非空的
`Mapping[str, EmailWebhookAdapter]`，mapping key 必须与 adapter 的 provider 完全一致。功能关闭、
factory 缺失／加载失败时返回脱敏 `503`，未知 provider 返回 `404`，签名失败返回 `401`，非法
payload 返回 `400`；已处理的 provider event 返回 `200 {"status":"duplicate"}`，其余已接受事件
返回 `200 {"status":"accepted"}`。响应、日志和业务表均不得包含原始 payload、签名或异常消息。

仓库现提供可选的具体 Resend 实现，不需要额外 SDK。发送 worker 通过
`build_resend_email_provider_from_env()` 得到 `ResendEmailProvider`；它只访问固定的
`https://api.resend.com/emails`，关闭环境代理与重定向，使用 `Idempotency-Key`，限制响应体大小，
并把 provider 错误统一映射为脱敏可重试错误。邮件只发送已批准的固定日文标题／正文和私有 HTTPS
链接，不添加 user ID、标签或研究内容。API 回调配置为：

```dotenv
VIBE_MARKET_MORNING_RESEND_API_KEY=re_xxx
VIBE_MARKET_MORNING_RESEND_FROM='Market Morning <morning@example.jp>'
VIBE_MARKET_MORNING_RESEND_WEBHOOK_SECRET=whsec_xxx
VIBE_MARKET_MORNING_RESEND_WEBHOOK_SECRET_PREVIOUS=
VIBE_MARKET_MORNING_EMAIL_WEBHOOK_FACTORY=src.api.market_morning_resend_webhook_factory:build_resend_webhook_adapters
VIBE_MARKET_MORNING_DELIVERY_LINK_BASE_URL=https://morning.example.jp/market-morning
VIBE_MARKET_MORNING_DELIVERY_LINK_SIGNING_SECRET=replace-with-at-least-32-random-bytes
VIBE_MARKET_MORNING_DELIVERY_LINK_SIGNING_SECRET_PREVIOUS=
```

Resend 回调使用原始 body 和 `svix-id`、`svix-timestamp`、`svix-signature` 验证
`HMAC-SHA256`；签名用常量时间比较，时间戳必须位于 5 分钟窗口，`svix-id` 同时作为库内幂等事件
ID。轮换期间可以短暂配置 previous secret，完成后必须清除。Webhook 端点只订阅
`email.delivered`、`email.clicked`、`email.bounced`、`email.complained`、`email.failed`、
`email.suppressed`；其他事件不属于当前状态机，误配时会 fail closed。parser 仅提取已签名事件 ID、
`data.email_id` 和标准化状态，收件人、主题与 raw payload 不进入业务表。

私有提醒链接由 `DeliveryLinkSigner` 以 `user_id + global_run_id + edition_date` 和专用
HMAC-SHA256 密钥确定性生成；业务库仍只保存 token 的 SHA-256。原始 URL 不含这些标识，token
只出现在 `#fragment`，因此不会随首次页面请求进入 access log。前端读取后立即用
`history.replaceState` 清除 fragment，并以当前产品 Bearer 将 token 放在 POST body 调用
`POST /market-morning/delivery-links/redeem`；后端同时匹配当前认证 user、email channel、可兑换状态和
7 日窗口，错误统一返回 404，不区分 token 无效、过期或属于其他用户。主/上一密钥支持短窗口轮换；
静态 deployment preflight 会在 URL/密钥缺失或非法时 fail closed。兑换成功只返回固定站内路径，
不会把 token 或用户 ID 回显给浏览器。最终 deployment factory 应用同一个 signer 的
`token_digest_builder` 与 `link_builder` 分别装配 dispatch 和 delivery runner。

代码完成不等于 provider 上线：staging 仍须完成发件域名验证、SPF/DKIM/DMARC、API key 最小权限、
回调注册、secret 轮换、退信／投诉处理、真实登录会话下的私有深链和 sent/delivered/clicked/failed 回放，并确认
Resend 合同、数据处理地域与日本用户隐私要求。实际 provider 未由 owner 批准前，runtime 的
`email_delivery` preflight 必须继续失败。

`0013_market_morning_edition_interactions` 增加与不可变用户朝刊分离的私有阅读状态和来源打开
审计。`mm_edition_event_states` 以用户、朝刊、事件唯一，状态仅允许 read／later／irrelevant，
首次既读时间只写一次；`mm_edition_source_opens` 以用户和客户端 request ID 幂等，只引用
`source_record_id`，不复制来源 URL。写入前必须验证朝刊归属、payload hash、event ID 与 citation
三元组，来源打开还会复核 SourceRecord 的原始 URL 与朝刊快照一致。

`0014_market_morning_issuer_research` 增加轻量私有个股备注。`mm_issuer_research_notes` 以
`user_id + issuer_id` 唯一，只允许当前 active 用户对自己仍在关注的 active issuer 读写；备注
正文限制 1000 字，不进入 analytics、审计属性或响应日志。个股研究历史本身不复制事件或来源，
而是读取官方 normalized-event 版本链和最新可用 EventBrief，并复用朝刊的 payload hash、review
status、revision 与来源 manifest 校验；校验失败时只返回官方标题和原始来源。

`0015_market_morning_beta_privacy` 增加一次性私测邀请表。数据库只保存 token SHA-256、状态、
期限和脱敏操作者引用；pending／accepted／revoked／expired 受 check constraint 约束，接受后只
关联内部 user ID。创建、接受、撤销、用户停用／恢复和删除完成均写审计，原始 token、OIDC
subject 与个人备注不会进入审计 details。

`0016_market_morning_model_usage` 为每次实际 EventBrief 模型调用保存一条幂等 usage event，以
brief ID + attempt number 唯一。token 和成本均使用非负整数，成本使用货币 millionth unit，避免
浮点误差；provider request ID 只保存 SHA-256，不保存模型输入、输出或 provider 原始响应。
provider 未返回 usage 时保存 `missing/unpriced`，返回 token 但没有实际价格时保存
`reported/unpriced`，只有 usage 与金额都可核对时才视为完整。运营摘要按币种汇总已报告成本，
不会跨币种相加，也不会为缺失数据生成估算值。

`0017_market_morning_content_reports` 增加隐私安全的内容错误报告。用户只能针对自己不可变朝刊
快照内实际存在且带引用的 event 选择固定原因，不接受自由文本、URL 或客户端自报 user ID；同一
用户、朝刊和 event 只保留一条报告。运营队列不选择用户 ID、朝刊 payload 或来源 URL，处理只能
以固定 resolution code 标记 resolved／dismissed 并写审计。Alembic 默认的
`version_num` 只有 32 位，而首个超长 revision 已在 `0004` 出现，因此空库迁移会在 `0001`
开始时永久扩为 64 位；`0017` 仍保留兼容性扩宽，确保从旧 `0016` bootstrap 升级时同样安全。

`0018_market_morning_auth_sessions` 增加 hash-only 应用会话账本。它不保存原始 access token、
provider subject 或 session id；用户登出、账号停用与删除请求会即时撤销对应 session。已经撤销
的 session 不会因账号重新启用而自动恢复，access token 有效期同时限制在 15 分钟以内。

## 交易日历与发布窗口

朝刊业务日期以 `Asia/Tokyo` 为准，JPX 现货日历决定是否允许发布，美国日历只决定隔夜市场
上下文和最后有效美股交易日。当前已实现可重复测试的 `FixtureTradingCalendar`，provider 名称
必须以 `fixture_` 开头，避免被误认为生产授权数据。生产路径使用
`load_licensed_trading_calendar()`：在 runtime 启动前异步读取 deployment-approved endpoint，验证
provider/market、manifest freshness、连续日期和 required coverage 后，生成不可变同步
`LicensedTradingCalendar`；coverage 之外一律抛出 `CalendarUnavailable`，scheduler fail closed。
具体 JPX／US provider 和 endpoint 尚未获许可或注入。

日历决策覆盖以下场景：

- A：日本开市且上一有效美股交易日可用，正常生成；
- B：日本开市、美国休市，使用最后有效美股日期并明确标注；
- C：日本休市、美国开市，只继续采集，不生成、不发邮件；
- D：周末或双方休市，只继续采集并返回下一 JPX 交易日；
- E：人工停发或日历不可用，fail closed，只继续采集。

JST 发布窗口为 06:30 前采集、06:30–07:00 准备、07:00 首判，以及 07:15、07:30、
08:00、08:30 的显式重试槽。进程重启后选择当前时间最近一个已到期槽位；08:30:00 仍属于
最后邮件窗口，晚于该时刻首次成功只能生成 `late` 站内版，不允许邮件。

从 06:30 起，scheduler 会先按固定顺序幂等排入五个 `market_snapshot` job；key 为
`market-snapshot:{edition_date}:{instrument}`，优先级高于 global run，重启后的重复 tick 不会
形成重复任务。到达发布槽位后才排入 `global_edition_run`，由 snapshot Gate 决定是否发布。

## 官方来源与事件管线

Sprint 3 的离线基础、EDINET v2、TDnet 与逐发行人公司 IR 工程 adapter 已建立，但真实外部数据源
仍保持关闭：

- `SourceAdapter` 统一使用 `discover(cursor) -> fetch(document) -> normalize(payload)`
  契约，并提供独立 `health()`；
- 采集器必须先完整拉取并规范化一个 discovery batch，任何文档失败时都不会调用
  repository，也不会推进 durable cursor；
- `source_provider + provider_document_id + provider_revision_key` 是来源版本的幂等
  身份。修订和撤回创建新行，通过 `supersedes_record_id` 串联，禁止覆盖旧版本；
- 来源记录、事件版本、事件来源关系、待审核跨源合并候选和 cursor 在同一 MySQL
  事务中写入；唯一约束与 `ON DUPLICATE KEY UPDATE` 共同处理重复 worker；
- 跨来源候选仅在同 issuer、24 小时窗口和确定性标题相似度达到阈值时创建，状态固定
  为 `pending`，不会自动合并；
- 运营审核只能把候选终态标记为 `approved` 或 `rejected`；批准前重新核对同 issuer，审核结果
  只代表人工判断，不会重写 `NormalizedEvent`、`EventSource` 或已发布内容；
- 数据库只持久化规范化元数据、最多 50,000 字的折叠空白证据文本、内容哈希、原始 URL 和
  抓取时间，不保存 fixture 或外部文档原始字节；EventBrief 直接消费该证据文本，不再把标题
  当作已抓取正文。

生产 `EdinetApiV2Adapter` 的边界如下：

- endpoint 固定为金融厅 `https://api.edinet-fsa.go.jp/api/v2`，禁用环境代理和自动重定向；
- `documents.json` 按 JST 当前日及 0–7 天回看窗口轮询，cursor 保存关注发行人的 provider-row
  revision SHA-256。每次重新比较窗口内全部关注文档，因此旧 `seqNumber` 的订正／撤回仍能形成
  新的不可变 revision，而未变化文档不会重复下载；
- 关注代码由 `SqlAlchemyTrackedIssuerCodeResolver` 用短只读事务从 active 用户、active watchlist、
  active issuer 中去重读取，查询不选择用户 ID；空关注列表不请求全市场 endpoint；
- 只下载 `pdfFlag=1` 且未撤回的 type=2 PDF，流式限制列表 10 MB、PDF 25 MB；PDF 文本提取在线程
  中执行，最多前 40 页／50,000 字，不做 OCR 或 XBRL 深解。无文本层时明确标记
  `metadata_only_pdf_text_unavailable`，不伪造正文；
- HTTP 200 的逻辑错误必须再检查 JSON `metadata.status`；下载必须核对 `Content-Type`，JSON 错误体
  不会被当作 PDF；subscription key 只随官方 API 请求发送，不进入公开 viewer URL、cursor、数据库、
  repr 或异常文本。

生产 `TdnetApiAdapter` 的边界如下：

- endpoint 固定为 JPX 的 `https://api.arrowfront.jp/tdlist` 与 `/tdfile`，不接受环境自定义 base URL、
  自动重定向或系统代理；access key 仅随这两个 endpoint 的 `x-api-key` 与 JSON `accessKey` 发送；
- 每次 discovery 对 JST 当前日及 0–7 天回看窗口分别请求当前记录和 `editDelFlag=1` 历史，按
  `14 位 disclosureNumber + modifiedHistory` 保存 cursor，并以规范化 provider row SHA-256 识别同一
  历史号的异常变化。全市场结果在内存中只保留 active watchlist 发行人；空关注列表不发请求；
- `statusCode=206` 表示 10,000 条截断，必须整体失败且不推进 cursor。订正冷启动会保留全部历史 revision，
  但依据官方“订正前后文档不变”规则只下载最新历史号；删除记录及其旧历史不请求已经不可用的文档；
- 全文 PDF 优先，缺失时才取 summary PDF。小文件响应的 `fileData` 和大文件 S3 内容都严格 base64 解码、
  校验 PDF magic 与大小；S3 只允许官方精确 host、HTTPS、443、无 userinfo/fragment，并禁止重定向。
  24 小时一次性 URL 不进入 normalized metadata、cursor、数据库、repr 或异常；
- 所有付费 API POST 在 adapter 内串行并至少间隔 1 秒；S3 文件 GET 不携 access key。PDF 文本最多前
  40 页并与状态、公司、标题、开示时间／历史号元数据共同组成≤50,000 字证据；无文本层明确降级为
  metadata-only，不补写内容；
- 数据库 document ID 保持官方 14 位开示编号；用户原始回链使用公开阅览服务的
  `1401{disclosureNumber}.pdf`。公开阅览服务只面向近期信息，历史回链保存期和获准自托管方案仍须在
  TDnet 合同审查中确认。

生产 `CompanyIrApprovedFeedAdapter` 的边界如下：

- 一家公司对应一个 `company_ir_{provider_key}` provider、一个 issuer code、一个 health/cursor 和一组
  deployment 批准的精确 HTTPS hostname + path prefix；不接受通配域名、IP literal、userinfo、非 443、
  query/fragment 或路径穿越。索引和文档每次请求前都重新解析 DNS，只要任一结果不是公网地址就拒绝；
  HTTP 禁用环境代理与自动重定向；
- 公司官网没有统一 API，因此 deployment factory 必须为每家公司提供一个只解析已下载 bytes、不得自行
  发网请求的 parser。核心提供严格 `schema_version=1` JSON index parser，也允许已审代码解析该公司的
  HTML/RSS/JSON；parser 只能返回 typed `CompanyIrIndexEntry`，网络、大小、Content-Type、URL 白名单与
  durable cursor 全由核心控制；
- index item 使用稳定 `document_id`、provider revision、公开 URL、发布时间、状态和 content kind。
  revision key 对全部规范化字段做 SHA-256；同一 document ID 的订正或撤回形成新不可变 revision，
  撤回记录只保存元数据证据，不请求可能已经失效的旧文档；重复 ID、未知字段、naive 时间或越界 URL
  会让整个 batch 失败且不推进 cursor；
- PDF 必须同时满足 `application/pdf`、大小上限和 `%PDF-` magic；HTML 只接受批准的 HTML media type，
  默认支持 UTF-8、CP932/Shift-JIS 与 EUC-JP，并移除 script/style 等非可见内容。两者都只提取不超过
  50,000 字证据；没有正文时明确保留 `metadata_only`，不得补写；
- `SqlAlchemyTrackedIssuerCodeResolver` 不包含该发行人时不访问其网站。不同公司由不同 durable job
  独立重试；一家公司的 parser、DNS、网页或文档失败只降低该公司的 source coverage，不会推进其 cursor、
  清空其他 provider 或把缺口交给模型。

部署示例（URL 仅作合同形状说明，不代表 Toyota 已批准）：

```python
config = CompanyIrApprovedFeedConfig(
    issuer_code="7203",
    provider_key="toyota",
    index_url="https://global.toyota/en/ir/feed/index.json",
    allowed_base_urls=("https://global.toyota/en/ir/",),
)
adapter = CompanyIrApprovedFeedAdapter(
    config=config,
    issuer_code_resolver=tracked_issuer_codes,
    index_parser=parse_company_ir_json_index,
)
assert adapter.provider == "company_ir_toyota"
```

生产部署必须把每个 `company_ir_*` 同时加入 adapter mapping、scheduler `source_providers` 和该发行人的
coverage policy；三者不一致时 preflight/运行配置应 fail closed。公司官网改版后只能更新对应 parser
和批准 URL，再以 fixture + `MockTransport` + 窄范围 staging 重新验收，不能放宽成任意 URL 爬虫。

开发和 CI 默认使用 `FixtureSourceAdapter`。它要求 provider 名称以 `fixture_` 开头，且不会包含
任何网络调用。EDINET／TDnet／公司 IR 生产代码的 HTTP contract 只用 `MockTransport` 验证；真实 key、
endpoint 或公司 feed 调用仍必须等读取、缓存、展示、AI 处理和回链权利逐项确认后才能进入 staging；
不得把 fixture adapter 改名后用于真实数据。

## 朝刊快照与生成编排

当前已经打通不依赖 LLM 的持久化生成路径：

```text
活跃用户行锁
  -> 活跃关注股（排序后最多 10 只）
  -> 24 小时事件窗口（最多读取 300 条 event-source 行）
  -> 同事件合并多个原始来源引用
  -> 确定性事实／推断分离与修订选择
  -> mm_morning_editions 不可变快照
```

- `run_morning_edition_generation()` 在一个 MySQL transaction 内完成读取、生成与发布；
- `generation_key` 保证同一次运行幂等，同日重新发布会建立新的 version 与
  `supersedes_edition_id`；
- 超过来源行预算时整次失败，不会静默截断后发布；单条无效来源 URL 不会成为事实引用；
- JPX 休市仍可发布明确的 `market_holiday` 快照，但不会读取关注股事件；
- 现有 `mm_source_cursors` 只能表达 provider 级采集结果，尚不能证明“某发行人的所有
  应查来源均完整”。因此未提供 coverage policy 时，数据库事件默认映射为
  `partial / source_coverage_unverified`，不会把空结果写成“已确认无新事件”。

## EventBrief v1 与内容 Gate

`src.market_morning.event_briefs` 已定义严格版本化的事件研究卡契约。模型输出只允许
`confirmed_facts`、`open_questions`、`source_ids`、`model_version` 和 `review_status` 等 v1
固定字段，未知字段、隐式类型转换和 naive datetime 都会在进入持久化前被拒绝。

- 每条事实必须声明自己的来源 ID，且必须属于 top-level source manifest；
- source manifest 必须能解析到 HTTPS 链接，外部 reachability 检查结果为 false 时阻止发布；
- 中／日／英荐股、保证收益和立即买卖措辞会阻止发布；
- 事实和问题中的数字／日期 token 必须能在对应来源证据中找到；
- “资料不足以判断”可作为有来源的合法结果；
- 模型失败时只生成标题、时间和来源链接组成的 degraded card，不携带模型事实或推断。

reachability 与模型生成均由部署侧以 port 注入，核心代码不会自行选择供应商或读取 provider
密钥。`HttpSourceReachabilityChecker` 提供可部署的 HTTP port：只接受精确 allowlist 中的 HTTPS
hostname，拒绝 userinfo、非 443 端口和 IP literal；每个初始 URL／重定向 URL 都重新执行 allowlist
与 DNS 公网地址检查，任一解析结果属于 loopback、link-local、private、reserved 等非公网地址即
fail closed。请求禁用环境代理和自动重定向，HEAD 被 403／405／501 拒绝时才改用不读取正文的
`Range: bytes=0-0` 流式 GET；4xx 判定来源不可达，408／429／5xx、DNS 和 transport 故障抛出
脱敏 unavailable，让 EventBrief 进入可重试／degraded 路径而不是误报来源不存在。部署示例：

```python
from src.market_morning.http_reachability import HttpSourceReachabilityChecker

reachability = HttpSourceReachabilityChecker(
    allowed_hosts={
        "disclosure2dl.edinet-fsa.go.jp",
        # 其余 hostname 必须来自已批准的 source registry；禁止通配符。
    },
    timeout_seconds=5.0,
    max_redirects=3,
)
```

精确 hostname allowlist 是主要 SSRF 边界，DNS 公网检查与部署 egress firewall 是补充防线；不得把
用户输入、任意公司官网或通配域名直接放入 allowlist。`0010` 持久化、短事务生成服务和事件级
durable handler 已完成；来源入库时会在同一事务按新 event revision 原子排入 EventBrief job，
入队失败会连同事件和 cursor 一起回滚。模型调用和 URL 检查均在数据库事务外执行。运营审核 API
已完成，真实 hostname 清单仍需随数据授权一同批准。

`OpenAICompatibleEventBriefGenerator` 提供 provider-neutral 的非流式模型 port，不复用会丢失 request ID／cost
的通用 `ChatLLM` 包装。它向部署指定的 HTTPS chat-completions endpoint 发送 strict JSON schema，
schema 固定 event identity、source ID 枚举、字段上限和 `review_status=pending`；evidence 在 system
prompt 中被声明为未信任数据，模型不得执行其中的命令。请求禁用自动重定向和环境代理，并限制
输入字符数及响应字节数；只接受唯一 `stop` choice 中的 JSON object。完整 provider response、模型
输出、API key 和错误 body 都不会落库或写日志。

adapter 直接读取 provider 的 `prompt_tokens`、`completion_tokens`、实际返回 model、`X-Request-ID`
（缺失时使用 generation ID）和可选 `usage.cost`。cost 用 `Decimal` 解析并转换为货币 millionth
unit，不经过 float 或本地估算。`require_usage=True`／`require_cost=True` 时任一字段缺失都会让本次
EventBrief 安全降级，同时把已经取得的真实 usage 写入审计。`cost_currency` 必须由部署依据供应商
账单合同显式声明；核心代码不会把未知 provider 的“credits”自动猜成 USD。

推荐的 OpenRouter 路径使用更窄的 `OpenRouterEventBriefGenerator`。它只接受官方 global/EU
chat-completions endpoint、一个固定 model 和 1–10 个 upstream provider slug；每次请求都同时设置
`order`、`only`、`require_parameters=true`、`data_collection=deny`、`zdr=true`。默认关闭 fallback；
即使显式开启也只能在 `only` allowlist 内回退。usage 和 cost 都是强制项，OpenRouter 的 USD 基础
货币按 millionth unit 记录。部署装配示例：

```python
from functools import partial

from src.market_morning.event_brief_model_adapter import (
    build_openrouter_event_brief_generator_from_env,
)
from src.market_morning.event_brief_service import run_event_brief_generation

generator = build_openrouter_event_brief_generator_from_env()
event_brief_runner = partial(
    run_event_brief_generation,
    generator=generator,
    reachability_checker=reachability,
    session_factory=session_factory,
)
```

所需变量为 `VIBE_MARKET_MORNING_OPENROUTER_MODEL`、`..._API_KEY`、
`..._UPSTREAM_PROVIDERS`，以及可选的 `..._ENDPOINT` 和
`..._ALLOW_APPROVED_FALLBACKS`。布尔值只接受 `true/false`，拼写错误会拒绝启动而不是静默放宽。
chosen model 必须在 staging 实测支持 strict `json_schema`；不支持时保持 HTTP 失败／degraded，禁止
自动回退到无法约束的自由文本或字符数 token 估算。generic adapter 仍可为其他 provider 产生
`unpriced` 开发证据，但 OpenRouter 生产 adapter 缺失 usage/cost 时固定 fail closed，不能通过 T1
成本可持续 Gate。

来源覆盖服务已经建立，但必需 provider 列表由后续 job 显式配置，不在领域层猜测：

- TDnet、EDINET 作为全局必需来源；公司 IR 只对配置了白名单 feed 的发行人追加为
  必需来源；
- 默认新鲜度窗口为 30 分钟，可由内部 policy 缩短或放宽，但上限为 24 小时；
- 全部必需来源最近一次成功采集都足够新鲜时为 `complete`；至少一个新鲜但仍有缺失、
  过期或失败时为 `partial`；没有任何新鲜来源时为 `unavailable`；
- 晚于最后成功时间的失败优先，成功时间异常领先生成时刻 5 分钟以上也按错误处理；
- `record_source_failure()` 只更新 `last_error_at/code`，不会推进 cursor 或覆盖上次成功
  时间；下一次完整成功会清除错误状态。

durable worker 已注册 source-ingestion handler。它先用短事务读取 cursor，在事务外完成
discover／fetch／normalize，再用第二个短事务锁定并复核 cursor 后持久化；如果网络采集、
规范化、数据库写入或 cursor 竞争失败，会在独立短事务调用 `record_source_failure()`，不会
推进 cursor 或把缺口留到新鲜度过期后才暴露。

scheduler 从 06:00 JST 起为 deployment factory 的每个 `source_providers` 项创建一次
`source-ingestion:{edition_date}:0600:{provider}` durable job，priority 120、最多 5 次显式尝试；
重启或重复 tick 复用同一个 key。来源任务早于 06:30 市场快照，且即使日本休市、人工停发或当日
朝刊已成功也仍会采集。生产 scheduler 要求至少一个规范化、唯一、非 `fixture_` provider；provider
列表为空或非法时 runtime 在领取任务前拒绝启动。

当前已有默认关闭的生产进程入口、EDINET／TDnet 官方 endpoint、逐发行人公司 IR，以及 provider-neutral
生产日历／行情代码边界，但尚未获批／注入真实 key、批准公司 feed、具体 JPX/US 日历与行情 endpoint，
也没有已批准的模型配置。
生成服务是 durable job handler 的事务内执行单元，不应直接由公开 HTTP 请求触发。

## Durable job 与 worker

`0006_market_morning_jobs` 增加以下运行表：

- `mm_jobs`：任务类型、幂等键、规范化 payload 哈希、优先级、可执行时间、状态、尝试次数
  和有期限的 worker lease；
- `mm_job_attempts`：每次领取的 append-only 执行记录；
- `mm_scheduler_leases`：带 owner、heartbeat 和过期时间的单活 scheduler 租约。

当前 job contract 支持 `source_ingestion`、`market_snapshot`、`global_edition_run`、
`edition_generation` 和 `email_delivery` 五类。入队以唯一 `idempotency_key` 和 MySQL
`ON DUPLICATE KEY UPDATE` 抵御并发重复；
相同幂等键若携带不同 payload 会拒绝执行，不会覆盖已有任务。第一次入队的优先级、时间和
最大尝试次数生效，后续相同入队请求只返回已有任务。

worker 使用 `SELECT ... FOR UPDATE SKIP LOCKED` 领取一项到期任务，并在同一短事务中创建
attempt 与 worker lease。handler 在事务外运行；成功或失败再用独立短事务落库，因此网络、
模型和来源处理不会长期占用数据库行锁。可重试异常必须给出规范化 `error_code` 和 1 秒至
24 小时的显式退避；永久或未处理异常进入终态，数据库不保存原始异常文本。过期 lease 可
恢复为重试或终态，scheduler lease 也只能由当前 owner 续租或在过期后被其他实例接管。

单活 scheduler policy 已接入数据库租约、人工停发读取、A–E 日历判定和发布窗口；06:00 先按
provider 建立来源任务，06:30 建立五项快照，再以
`global-edition-run:{edition_date}:{attempt_key}` 入队；同一槽位的进程重启或重复 tick 只返回
同一个 durable job。每个槽位使用固定 `scheduled_at`，07:20 与 07:29 的重复 tick 不会形成
相同 key、不同 payload 的冲突。scheduler 默认读取 durable current-success 状态，已成功刊期
不会再创建重试任务。

handler registry 只注册具备完整执行路径且依赖已显式配置的任务。来源任务严格校验 provider，
只能使用显式注入的 adapter；EventBrief runner 与 dispatch config 必须成对提供，否则 factory
拒绝构建；朝刊任务
严格解码版本化 JSON command，并将 payload、用户状态、预算冲突与数据库暂时不可用分成
永久失败或显式重试。

`run_one_job()` 是单次执行入口：handler 运行期间按 lease 的三分之一周期自动续租；续租失败
会取消 handler，绝不再写成功状态。`run_worker()` 提供可停止的进程循环，每轮先恢复一个
过期 job，再领取一个任务；空闲轮询可由 stop event 立即唤醒，基础设施错误只记录异常类型。
`market_snapshot`、`global_edition_run`、`event_brief_generation` 和 `email_delivery` handler
已实现并按 adapter/provider policy 条件注册；
global run 会先执行五项 snapshot Gate，缺项时持久化 failed run 而不发布，完整时写 manifest 并
切换 current success；允许邮件时再按资格查询创建幂等投递尝试并排入邮件 job。发送前会重新
检查 run 与用户资格，身份解析、私有链接签发和邮件 provider 调用都在事务外执行，数据库只保存
脱敏状态。provider webhook 的验签、标准化、幂等与单调状态机核心及 fail-closed HTTP route
均已完成；Resend 发送、Svix 签名验证和事件解析 adapter 也已提供。部署仍须注入 identity resolver、
私有链接 signer、已验证域名／凭据并完成 staging 回放；未配置时不会发送或处理回调。

邮件身份解析使用独立的 deployment factory，不能从产品数据库、Bearer token 或客户端请求读取邮箱：

```bash
VIBE_MARKET_MORNING_EMAIL_IDENTITY_FACTORY=deployment.identity:build_email_identity
```

factory 必须返回 `MarketMorningEmailIdentityAdapter(provider, resolve_identity)`。异步
`resolve_identity(user_id, external_subject)` 只能返回 `VerifiedEmailIdentity` 或 `None`；返回对象必须把
相同的内部 user UUID 与伪名 subject 原样回绑，并明确 `active`、`email_verified`。核心只把 active、已验证、
语法安全的地址传给 provider；错用户／错 subject／错误 contract 作为可重试基础设施故障，未找到、停用或
未验证地址作为永久抑制处理。邮箱、目录异常和原始 provider subject 不进入业务日志或 Market Morning 表。
通用 resolver/factory contract 已完成；Auth0 或其他身份目录的实际查询 adapter、权限和 staging 回放仍由
部署仓库实现和验收。

最终 worker factory 可调用
`build_resend_email_runtime_runners(session_factory=..., environ=...)`，一次性构造严格配对的
`email_delivery_runner` 与 `email_dispatch_runner`。两者共享同一个数据库 factory 和同一个深链 signer：
dispatch 写入的 token SHA-256 必然能由 delivery 重建，identity resolver、provider 和 signer 都只在事务外
调用。构造阶段只验证本地配置，不访问 Resend 或发送邮件；deployment 仍必须单独提供无副作用的
`email_delivery` 账户／域名可用性 preflight，并完成真实 staging 回放。

用户首次打开当日朝刊时会执行 lazy `UserEdition`：已有日级快照直接复用；没有时在同一事务中
锁定 current global run 与 active user，以 global run 完成时间作为事件截止点，再读取当时的当前
关注股并发布私有快照。固定 `user-edition:{edition_date}:{user_id}` generation key 保证该路径
同一用户同日只创建一次；global manifest 校验失败、当日尚未发布或用户不再 active 时均 fail
closed，不会用 fixture 或过期内容填充。

独立进程入口为 `vibe-trading-market-morning`（源码调试也可使用
`python -m src.market_morning.runtime_cli`）。它要求 feature flag 与 runtime flag 同时开启，并从
`VIBE_MARKET_MORNING_RUNTIME_FACTORY=module:function` 加载最终
`MarketMorningRuntimeDependencies`。生产推荐固定使用：

```bash
VIBE_MARKET_MORNING_RUNTIME_FACTORY=src.market_morning.production_runtime_factory:build_runtime_dependencies
VIBE_MARKET_MORNING_PROVIDER_BUNDLE_FACTORY=deployment.market_morning_providers:build_provider_bundle
```

第一项是仓库内置的最终无参 factory：它从进程环境绑定产品 MySQL、策略锁定的 OpenRouter EventBrief
generator 与成对 Resend runners；第二项由部署仓库实现，可以同步或异步返回
`ApprovedMarketMorningProviderBundle`，负责已授权 source adapters、五项行情 adapters、启动前预加载的
JPX/US 日历、source reachability callback 和 EventBrief model/source/email 三项无副作用 preflight。
内置 factory 调用 `assemble_runtime_dependencies()`，精确要求完整 source registry、全部五项 market
adapter、两份 licensed calendar、EventBrief/邮件 ports 和五项 preflight，并从实际 adapter registry
自动生成 `licensed_sources` 与 `market_snapshots` 两个只读 probe。自定义最终 runtime factory 仍受同一
`MarketMorningRuntimeDependencies` contract 约束。启动前还会确认 handler 完整、JPX/US calendar contract、禁止
fixture provider、数据库可连接且 `alembic_version` 精确为 `0018_market_morning_auth_sessions`。
生产 worker factory 还必须提供以下五个无副作用异步 preflight check：

```text
licensed_sources
market_snapshots
event_brief_model
source_reachability
email_delivery
```

日历默认 strict JSON manifest 字段为 `schema_version/provider/market/generated_at/coverage_start/
coverage_end/sessions`，且 coverage 内每个自然日必须按顺序恰好出现一次。行情默认 strict JSON 字段为
`schema_version/provider/instrument/session_date/as_of/value/previous_close/currency/delay_status`。
若商业 provider 原始格式不同，deployment 可注入经审核、无副作用 parser，但 parser 的 typed 输出仍要
再次通过核心 provider/instrument/freshness/coverage contract；不能通过 parser 绕过网络 allowlist。

这些 check 只能验证配置、授权和连通性；不得采集／推进 cursor、发送邮件、调用计费模型或写业务
数据。runtime 在数据库 preflight 之后、创建 worker task 之前按固定顺序执行，每项最多 10 秒；
缺项、失败或超时分别以 `runtime_preflight_checks_incomplete`、`runtime_preflight_failed`、
`runtime_preflight_timeout` 拒绝启动。日志只记录固定 check 名和异常类型，不记录 provider 错误文本。
任何一项不满足都不会领取任务。worker 与 scheduler 共用 stop event，
SIGINT/SIGTERM 会触发优雅停止。只有显式 test/dev 配置才允许 fixture runtime。

T0 本地合成演练入口为 `vibe-trading-market-morning-rehearsal`（源码调试可使用
`PYTHONPATH=agent .venv/bin/python -m src.market_morning.rehearsal_cli`）。必须显式传入 `--output`
和环境名；runner 会依次执行运行手册定义的 12 个故障/边界场景，即使中途失败也会继续收集其余
结果。JSON 只保存 pytest node ID、exit code、耗时与合并输出 SHA-256，不保存 stdout/stderr 原文或
provider 响应。报告固定写入 `scope=automated_local_synthetic` 和
`counts_as_staging_day=false`，因此不能替代连续 5 个真实 staging 日本交易日。

真实 MySQL 可用且迁移完成前，不开启 runtime flag，也不执行任何外部数据任务。生产 EventBrief
generator 必须返回 provider usage/cost contract；只返回内容 payload 的旧式 port
会被明确记为 `usage_incomplete`。生产模型 HTTP adapter 已完成，仍需用选定 provider/model 做
strict-schema、实际 usage/cost 与隐私策略的 staging 验收。OpenMetrics 指标出口和告警阈值已
完成，仍需在 staging 接入实际 Prometheus／告警通知链并保存演练证据。runtime preflight contract 与
内置最终无参 production factory 已完成；Resend adapter、通用 email identity resolver 与深链
signer/兑换代码已可用，但真实账号、已验证域名、provider directory adapter 和返回真实
licensed ports／checks 的 provider bundle factory 仍需随凭据与授权落地。

### 真实 MySQL acceptance runner

已把首批真实数据库验收固化为一个默认拒绝写入的 CLI。目标数据库名必须匹配
`market_morning_acceptance*`，不得与已配置生产目标的 host/port/database 相同，并且每次运行都
必须显式传入写入确认参数；否则在任何 pytest probe 启动前退出。先由管理员创建专用数据库并执行
`alembic upgrade head`，然后从仓库根目录运行：

```bash
export VIBE_MARKET_MORNING_ACCEPTANCE_DATABASE_URL='mysql+asyncmy://user:password@host:3306/market_morning_acceptance_ci?charset=utf8mb4'
PYTHONPATH=agent .venv/bin/python -m src.market_morning.mysql_acceptance_cli \
  --environment staging \
  --confirm-write-to-dedicated-database \
  --output docs/evidence/market-morning/mysql-acceptance-YYYY-MM-DD.json
```

已安装项目也可运行 `vibe-trading-market-morning-mysql-acceptance`。当前目录包含十二项 probe：MySQL
8.x/UTC/utf8mb4/精确 schema、access-token hash session 的并发首见幂等／原文不落库／精确注销／
不可复活与多 token 隔离、来源 revision/cursor、用户朝刊 generation/revision、durable job 写入
与 `SKIP LOCKED` 领取、scheduler 单活租约、publication halt 并发审计、global run 唯一 current
success、EventBrief／model usage 并发幂等、邮件 attempt 并发、webhook 重复／乱序状态机，以及
账户删除 SQL/审计的真实事务执行。写入 probe 使用随机专用键并在 `finally` 精确清理；账户删除 probe 整体回滚。证据只
保存固定 node ID、退出码、耗时、输出 SHA-256 和目标指纹，不保存 URL、host、凭据或原始输出，
且固定 `counts_as_staging_day=false`。

该 runner 明确标记 `contains_destructive_migration_evidence=false`：它要求数据库已迁移到 head，
但不创建／删除 database，也不替代下方 `0001 -> 0018`、`0016 -> 0018`、downgrade 或其余真实
并发场景。专用 MySQL 未提供时，十二项 live test 只会 skip，不能记为验收通过。

2026-07-21 的隔离 MySQL 8.0 工程演练为 12/12 passed。领取任务使用两阶段锁定：先无锁读取最多
64 个确定性排序的候选 ID，再按主键逐个执行带状态/可用时间复核的
`FOR UPDATE SKIP LOCKED`，避免 InnoDB filesort 扫描把锁扩大到整个 ready queue。CI 也会为每次
提交创建两套临时库、执行相同 12+3 probes，并上传 hash-only artifact；CI manifest 使用
`environment=ci`，不能冒充 T1 staging 证据。

2026-07-23 又对专用远程 MySQL 8 acceptance 库执行 12/12 retest；其中 schema/session probe 已按
Alpha A 的真实 Auth0 配置改为 access-token-instance hash 模式，并验证同一 token 并发首见只生成
一行、不同 token 相互隔离、精确注销后原 token 不可复活、另一 token 仍有效，且数据库行不含
issuer、subject 或 access token 原文。该报告仍固定 `counts_as_staging_day=false`，不能计入连续五日
Staging 或 T1 发布证据。

同日两个 global run 并发发布的远程验收还验证了 generated unique key 的逐行约束行为：repository
必须先 flush 旧 current run 的 demotion，再 promotion 新 run；否则 ORM 同批更新顺序可能产生瞬时
双 current 并被 MySQL 拒绝。该更新顺序已固化为单元测试和真实 MySQL probe。

### 真实 MySQL migration rehearsal

迁移和 downgrade 使用独立 CLI 与独立可丢弃数据库。数据库名必须匹配
`market_morning_migration_acceptance*`，不能复用普通 `market_morning_acceptance*` 或生产库；CLI
不会创建／删除 database，但会在专用库内执行 `downgrade base`，所以会删除该库全部 Market
Morning schema 和数据。管理员创建空的专用数据库并授予 DDL 权限后，从仓库根目录运行：

```bash
export VIBE_MARKET_MORNING_MIGRATION_DATABASE_URL='mysql+asyncmy://user:password@host:3306/market_morning_migration_acceptance_ci?charset=utf8mb4'
PYTHONPATH=agent .venv/bin/python -m src.market_morning.mysql_migration_rehearsal_cli \
  --environment staging \
  --confirm-destructive-reset-of-dedicated-database \
  --output docs/evidence/market-morning/mysql-migration-YYYY-MM-DD.json
```

已安装项目也可运行 `vibe-trading-market-morning-mysql-migration-rehearsal`。三项 probe 依次验证：

1. `base → head` 后 revision 精确为 `0018_market_morning_auth_sessions`，`mm_*` 表集合与 ORM metadata 一致；
2. `base → 0016 → head` 后账户与私测邀请 sentinel 数据保留，并创建 content reports 表；
3. `head → 0016 → head` 时只移除／恢复 content reports 表，0016 以前的 sentinel 数据在 roundtrip 中保持。

每项结束都会尽力恢复到 head 并删除 sentinel。报告只保存固定 node ID、退出码、耗时、输出
SHA-256 与目标指纹，固定 `contains_destructive_migration_evidence=true`、
`counts_as_staging_day=false`。没有得到 3/3 passed 时，不能把离线 SQL 生成或普通业务 probes
替代真实迁移证据。

2026-07-21 的隔离 MySQL 8.0 工程演练为 3/3 passed；同轮还把当前 bootstrap SQL 直接导入第三个
空库，核对到 33 张 `mm_*` 表、精确 `0017` revision 和 `varchar(64)` 版本字段。远程 migration
acceptance 库也已完成 3/3 并恢复到 `0017`/33 表；生产主库迁移仍必须单独审批和留存变更记录。

### 受控产品库 schema change job

产品库不使用 acceptance 或 destructive rehearsal CLI。部署包提供独立的
`vibe-trading-market-morning-production-schema-change`：它只读取
`VIBE_MARKET_MORNING_DATABASE_URL`，拒绝 acceptance 数据库或与两套验收 URL 相同的目标，并把
host/port/database 仅保存为 SHA-256 指纹。先在不写库的模式确认实际 revision：

```bash
vibe-trading-market-morning-production-schema-change \
  --environment production \
  --expected-current-revision 0004_market_morning_sources_events \
  --check-only \
  --output /controlled/evidence/product-schema-preflight.json
```

正式变更前必须完成可恢复性验证的备份、停止 API/worker/scheduler 的 Market Morning runtime，并取得
维护窗口和变更单批准。`--backup-evidence-sha256` 是备份系统证据文件的 SHA-256，不是备份本身；
`--change-reference` 只接受安全标识符，manifest 中只保存它的 SHA-256：

```bash
vibe-trading-market-morning-production-schema-change \
  --environment production \
  --expected-current-revision 0004_market_morning_sources_events \
  --backup-evidence-sha256 <64位小写SHA-256> \
  --change-reference <已批准变更单ID> \
  --confirm-maintenance-window \
  --confirm-runtime-stopped \
  --confirm-no-automatic-downgrade \
  --confirm-production-schema-change \
  --output /controlled/evidence/product-schema-change.json
```

命令仅在实际 revision 精确等于 `--expected-current-revision` 时执行固定的 `alembic upgrade head`；
成功后必须同时得到 `0018_market_morning_auth_sessions` 和 34 张 `mm_*` 表。stdout/stderr 只保存
SHA-256，不保存 URL、凭据或变更单原文。失败时不会自动 downgrade，因为部分 DDL 已提交时自动回退
可能扩大损坏；应保持 runtime 关闭、保留 manifest 和数据库状态，再按独立恢复审批处理。

### 不可变 release candidate Gate

CI 在普通后端测试、前端 build/test、真实 MySQL 12/12 acceptance 和 3/3 migration rehearsal 都成功后，
运行 `vibe-trading-market-morning-release-candidate`。该 Gate 把以下内容绑定到 checkout 的同一个
40–64 位 Git revision：

- clean worktree，以及 `requirements-lock.txt`、`frontend/package-lock.json`、容器定义、CI workflow、
  当前 migration、0018 SQL/ZIP、Auth0 Post-Login Action／部署合同、OIDC staging 自动探针和
  monitoring rules 等二十项固定
  发布源文件的 SHA-256；
- 后端测试、前端 build、前端测试、MySQL acceptance 与 migration rehearsal 五份输出的 SHA-256；
- 当前 runtime schema `0018_market_morning_auth_sessions`。

命令要求五个 evidence 名称精确出现一次，两个 MySQL JSON 还必须是相同环境的 `passed` 合同。
manifest 只保存固定 artifact 名称、revision、检查状态、稳定阻断码和 hash，不保存工作区路径、Git
status 原文、数据库地址、provider URL 或凭据。输出必须位于仓库外，避免 Gate 自己把 clean tree
变脏。例如：

```bash
PYTHONPATH=agent .venv/bin/python -m src.market_morning.release_candidate_cli \
  --repository-root "$PWD" \
  --environment ci \
  --expected-release-revision <40位Git revision> \
  --evidence backend_tests=/controlled/evidence/backend-tests.log \
  --evidence frontend_build=/controlled/evidence/frontend-build-index.html \
  --evidence frontend_tests=/controlled/evidence/frontend-tests.log \
  --evidence mysql_acceptance=/controlled/evidence/mysql-acceptance.json \
  --evidence mysql_migration=/controlled/evidence/mysql-migration.json \
  --output /controlled/evidence/market-morning-release-candidate.json
```

只有 `status=passed` 且 `blocking_codes=[]` 才能部署该 revision 到 staging。这个 manifest
`counts_as_staging_day=false`、`counts_as_t1_release_evidence=false`：它证明候选源码与 CI 输出没有
漂移，但不能代替真实 OIDC、授权来源、五日运行或外部签字。部署与每日 staging manifest 必须复用
其中的 `release_revision`，禁止重新构建或手填另一个 revision。

### Staging 单项 Gate artifact

真实 staging 不再手写最终日报。每个 Gate 先用
`vibe-trading-market-morning-staging-gate-artifact` 包装自己的原始 probe 文件。CLI 会读取已通过的
release candidate，自动填入 release/schema 和 candidate hash，再对原始 probe 文件计算 SHA-256；
envelope 不复制 probe 内容或文件路径。下面以 publication 为例：

```bash
vibe-trading-market-morning-staging-gate-artifact \
  --release-candidate /controlled/evidence/release-candidate.json \
  --gate publication \
  --run-id staging-2026-07-22 \
  --edition-date 2026-07-22 \
  --previous-jpx-open-date 2026-07-21 \
  --next-jpx-open-date 2026-07-23 \
  --calendar-provider licensed-jpx-calendar \
  --provider-id market-morning-runtime \
  --started-at 2026-07-22T06:55:00+09:00 \
  --finished-at 2026-07-22T07:01:00+09:00 \
  --published-at 2026-07-22T06:58:00+09:00 \
  --status passed \
  --evidence /controlled/raw/publication-probe.json \
  --output /controlled/evidence/publication.json
```

只有 `publication` 可以设置 `published_at`，只有 `model_usage_cost` 可以设置非零
`model_invocation_count`。失败 Gate 必须使用 `--status failed --failure-code <稳定代码>`，CLI 仍写出
可审计 artifact，但退出 `1`。provider ID、calendar provider 含 fixture/synthetic/demo，raw evidence
为空、过大、符号链接，或 release candidate 被篡改时均退出 `2` 且不生成可用 artifact。

### 单日 staging evidence builder

同一 `run_id` 的 11 个 artifact 全部生成后，使用
`vibe-trading-market-morning-staging-day` 构造当天唯一日报：

```bash
vibe-trading-market-morning-staging-day \
  --release-candidate /controlled/evidence/release-candidate.json \
  --gate database_readiness=/controlled/evidence/database-readiness.json \
  --gate deployment_preflight=/controlled/evidence/deployment-preflight.json \
  --gate runtime_preflight=/controlled/evidence/runtime-preflight.json \
  --gate oidc_session=/controlled/evidence/oidc-session.json \
  --gate licensed_sources=/controlled/evidence/licensed-sources.json \
  --gate market_snapshots=/controlled/evidence/market-snapshots.json \
  --gate content_quality=/controlled/evidence/content-quality.json \
  --gate publication=/controlled/evidence/publication.json \
  --gate email_delivery=/controlled/evidence/email-delivery.json \
  --gate model_usage_cost=/controlled/evidence/model-usage-cost.json \
  --gate observability=/controlled/evidence/observability.json \
  --output /controlled/evidence/staging-2026-07-22.json
```

builder 精确要求 11 个不同文件，并验证它们共享 candidate hash、release、schema、run ID、刊期、JPX
前后交易日和 calendar provider。provider 并集不足五项、任一 artifact 身份漂移或缺失都会拒绝；
只要一个 Gate 失败，仍生成 `status=failed`、`counts_as_staging_day=false` 的日报并退出 `1`。通过日报
才可交给下面的连续五日聚合器。

### 连续五日 T1 staging evidence Gate

`vibe-trading-market-morning-staging-gate` 只聚合 deployment-owned 的真实单日报告，不主动调用
staging API，也不允许人工用 T0／MySQL probe manifest 顶替。单日报告必须使用严格
`market_morning_staging_day` schema，不能包含未声明字段，并同时满足：

- `environment_tier=staging`、当前 runtime schema 与 40–64 位不可变 release revision；
- licensed JPX calendar 的 previous/current/next open-session 日期链；
- 非 fixture／synthetic／demo 的日历和至少五个生产 provider ID；
- database readiness、deployment/runtime preflight、OIDC、授权来源、市场快照、内容质量、发布、
  邮件、model usage/cost、observability 共 11 项 Gate；
- 每项 Gate 对应一个 artifact SHA-256，聚合报告不会复制 provider ID 或原 artifact map；
- 通过日必须在 06:30–08:30 JST 发布，所有 Gate 为 passed、没有 failure code，并至少有一次真实
  model invocation；失败日必须有 failed Gate 和稳定 failure code，且不能计数。

把本次 release 从第一个 staging 日本交易日起的**全部**单日报告传入；不能省略中间失败日：

```bash
PYTHONPATH=agent .venv/bin/python -m src.market_morning.staging_evidence_cli \
  --input docs/evidence/market-morning/staging-2026-07-13.json \
  --input docs/evidence/market-morning/staging-2026-07-14.json \
  --input docs/evidence/market-morning/staging-2026-07-15.json \
  --input docs/evidence/market-morning/staging-2026-07-17.json \
  --input docs/evidence/market-morning/staging-2026-07-20.json \
  --output docs/evidence/market-morning/t1-staging-gate.json
```

只有输出 `status=passed`、`counts_as_t1_evidence=true` 且
`trailing_consecutive_days>=5` 才满足五日 Gate。中间失败、缺少预期 JPX 交易日、release／calendar
provider 变更都会从下一合格日重新计数；历史 streak 曾达到五日但最新一天失败时仍返回
`not_ready`。CLI 通过时退出 `0`，证据有效但尚不足五日时退出 `1`，输入 schema 不安全时退出
`2`。这个 Gate 只证明五日运行证据连续，不能替代 MySQL、OIDC、数据许可或日本合规的独立签字。

### T1 总发布 Gate

五日 Gate 通过后仍不能单独开启 T1。`vibe-trading-market-morning-t1-release-gate` 会同时验证：

1. 12/12 真实 MySQL 业务 acceptance；
2. 使用另一目标数据库的 3/3 destructive migration rehearsal；
3. 最新 release 连续五个 JPX 交易日的 staging Gate；
4. TDnet、EDINET、公司 IR registry、JPX 主数据/行情权利和日本法律审查五项外部签字。

外部签字从
[`t1-external-signoffs.template.json`](./evidence/market-morning/t1-external-signoffs.template.json)
复制到受控的证据工作区。仓库模板故意全部为 `blocked` 且使用零值 hash，不能直接作为证据。每项
真实记录只允许安全的内部 approval reference、批准时间、复核到期日和原始批准材料 SHA-256；不
写人员姓名、邮箱、合同正文或 secret。五项 key 必须精确完整，已过复核日也会阻断；
`company_ir_rights` 即使批准的是“本次 release 不配置任何公司 IR feed”的空 registry 也必须出现，
避免之后静默加入未审 `company_ir_*` provider。

```bash
PYTHONPATH=agent .venv/bin/python -m src.market_morning.t1_release_gate_cli \
  --mysql-acceptance docs/evidence/market-morning/mysql-acceptance-YYYY-MM-DD.json \
  --mysql-migration docs/evidence/market-morning/mysql-migration-YYYY-MM-DD.json \
  --staging-gate docs/evidence/market-morning/t1-staging-gate.json \
  --external-signoffs /controlled/evidence/t1-external-signoffs.json \
  --output docs/evidence/market-morning/t1-release-gate.json
```

四个输入必须是不同文件，输出不能覆盖输入。CLI 对错误 scope、缺场景、summary 漂移、复用同一
MySQL 目标、release/schema 不一致、缺签字、未批准或过期均 fail closed。输入合法但仍有阻断项时
写出 `not_ready` 并退出 `1`；结构不安全时退出 `2`。只有输出同时满足 `status=approved`、
`counts_as_t1_release_evidence=true`、`blocking_codes=[]` 才能提交最终变更审批。输出只保留 release、
schema、检查状态、稳定阻断码和四份输入的 canonical SHA-256，不复制数据库指纹、approval
reference、证据 hash 或内容。

获得 MySQL 连接信息后，需要补做以下集成验收：

1. 在空库执行 `0001 -> 0018`，再从已有 `0017` 的升级库执行 `0018`；
2. 并发写入相同来源版本，确认只保留一条 `mm_source_records`；
3. 连续写入 active、corrected、withdrawn 三个版本，确认来源和事件历史完整串联；
4. 模拟第二个文档失败，确认前一 durable cursor 未变化；
5. 写入两个相似跨来源事件，确认只生成一条 `pending` merge candidate；
6. 对同一 `generation_key` 并发生成，确认只保留一个朝刊版本；使用新 key 重发时确认
   version 与 supersedes 链正确；
7. 并发入队同一幂等键并由多个 worker 领取，确认任务唯一、attempt 唯一且不会被重复执行；
8. 模拟 worker 在领取后退出，确认 lease 到期后可恢复；模拟两个 scheduler owner，确认过期前
   只有一个 owner 可以持有租约；
9. 运行超过一个 lease heartbeat 周期的 handler，确认 lease 可续期；强制续租失败时确认 handler
   被取消且不会落成功结果；
10. 并发创建同一刊期的人工停发，确认只存在一个 active override，创建与撤销审计完整；
11. 对五项市场快照分别模拟缺失、交易日错位和重复候选，确认发布 Gate 全部 fail closed；
12. 并发触发同刊期不同 retry slot，确认 run version 确定、current success 唯一、旧版本保留；
13. 并发触发相同 event revision 的 EventBrief，确认 generation key 唯一、重试计数正确、终态
   payload hash 稳定；模拟模型失败、不可达来源和撤回事件，确认分别重试、阻断或安全降级；
14. 对相同用户、日期、渠道并发创建邮件尝试，确认只有一条；模拟失败重试和 provider ID
   回写，确认原始邮箱与 deep-link token 从未进入业务表；
15. 执行 `0018 -> 0016` 的 staging downgrade 演练，并确认 auth sessions 与 content reports 表移除后，0016 及以前的
   私测邀请、账户、关注股、来源事件、用户朝刊、阅读状态、来源打开、job、人工停发、市场快照、
   全局刊期、EventBrief、投递尝试与 provider event 表仍保持完整；
16. 对重复、乱序、未知 provider message ID 和无效签名分别回放 webhook，确认幂等、不倒退、
   unmatched 可审计，且原始 payload 与签名从未进入业务表。
17. 并发更新同一朝刊事件状态，确认唯一行和首次既读时间稳定；伪造其他用户的 edition ID、
   event ID、citation 或 URL 时必须 fail closed；重复来源 request ID 只产生一条打开记录和一次
   telemetry。
18. 并发更新同一用户和 issuer 的个人备注，确认唯一行稳定；伪造其他用户、已移除关注股或停用
   issuer 时必须 fail closed，备注正文不得出现在 analytics 或日志属性中。
19. 并发创建、接受和撤销邀请，确认原始 token 只返回一次、库内与日志中均只有 SHA-256；停用
   用户后确认产品数据服务和邮件资格立即 fail closed，恢复操作不会自动重新订阅邮件。
20. 对同一删除申请并发执行 worker，确认只有一个事务清除私有数据、另一个幂等返回；导出文件
   不包含内部 token/provider/job secrets，删除后 OIDC subject、备注、标签和私有朝刊均不可恢复。
21. 对相同 brief/attempt 并发记录 provider usage，确认只保留一条且内容漂移 fail closed；分别回放
   完整 usage+cost、只有 token、usage 缺失和模型失败，确认看板显示 `complete`、
   `cost_incomplete` 或 `usage_incomplete`，且 provider request ID 原文从未进入数据库或 API。
