# Market Morning｜生产 Provider 选型提案

> 状态：**推荐默认项，待产品 owner、预算、安全/隐私与数据许可 owner 签字**。本文件不是采购授权，
> 也不允许提前开启 T1。审查日期：2026-07-22。

## 1. 建议结论

| 能力 | 推荐默认项 | 代码状态 | 上线前仍需完成 |
|---|---|---|---|
| 产品/运营身份 | Auth0 独立 tenant/application | 通用 OIDC/JWKS、角色、逐请求 session-validator port、官方 Auth0 SPA SDK 2.23 product/operator bootstrap（PKCE、memory cache、rotating refresh、callback 清理、revoke/logout）与登录/登出/失败恢复界面，以及按 user UUID + 伪名 subject 严格回绑的邮箱 resolver factory 已完成 | 真实 tenant/application、允许 URL 与 role Action、真实撤权/session validator、身份目录查询 adapter、跨会话与管理员最小权限 E2E |
| 事务邮件 | Resend | 固定 HTTPS 发送、provider 幂等、Svix HMAC、防重放、事件 parser 已完成 | 账号/预算、已验证域名、SPF/DKIM/DMARC、真实回放、合同与隐私批准 |
| EventBrief 模型网关 | OpenRouter，固定一个支持 structured outputs 的付费模型和受限 provider policy | policy-locked adapter 已固定官方 endpoint、model/upstream allowlist、strict schema、ZDR/deny 与真实 usage/cost fail-closed | 批准具体 model/upstream、真实 strict-schema/usage/cost 样本、账号级日志策略、DPA/隐私批准 |
| TDnet | JPX 官方付费 TDnet API | 固定官方 endpoint、cursor/revision/删除、限流与安全 PDF 证据 adapter 已完成 | 申请、合同权利、API 凭据、公开回链保存范围、staging 样本 |
| 法定披露 | 金融厅 EDINET API v2 | 固定官方 endpoint、版本 cursor 与安全 PDF 证据 adapter 已完成 | API key、使用条款复核、XBRL/PDF 范围决定、staging 样本 |
| 公司 IR | 逐发行人批准的官网 feed；一家公司一个 provider/parser | 精确 HTTPS hostname/path、公共 DNS、禁代理/重定向、revision/撤回、PDF/HTML/元数据证据和独立 health adapter 已完成 | 首批公司清单、逐公司用途权利、最终 URL/parser、网站改版 owner、窄范围 staging |
| IssuerMaster/JPX 行情 | JPX 官方或明确包含展示、缓存、AI/衍生与再分发权利的商业合同 | schema/search/snapshot 及精确 HTTPS、五品种 provider-neutral adapter 已完成 | 商务合同、允许字段/延迟/保存期、具体 endpoint/header/parser 与权利登记 |
| 日美交易日历 | 已许可的 JPX/US calendar feed；不得从新闻页推断 | calendar contract/scheduler、启动前 strict manifest loader 与同步 licensed calendar 已完成 | provider 合同、具体 endpoint/parser、日美节假日样本、连续五日证据 |
| 指标/告警 | Prometheus + Alertmanager | OpenMetrics、rules 与 HTTPS bearer-file scrape 模板已完成 | receiver、机器身份、token 轮换、warning/critical/resolved 演练 |

## 2. 为什么是这些默认项

### 2.1 Auth0

- SPA 使用 Authorization Code Flow with PKCE；access token 只放前端内存，不写 localStorage 或 URL。
- 开启 rotating refresh tokens 与 reuse detection；退出和账户删除必须触发 provider session/token 撤权，
  不能只清浏览器状态。
- 为 Market Morning 建立唯一 API audience，签名算法固定 RS256；产品和运营可共享 tenant，但使用不同
  application/audience 或明确隔离 scope。
- Prometheus 使用 M2M client，只授予 `operations.read`；四项运营权限从 Auth0 role 显式映射，禁止
  wildcard。
- 产品库不保存邮箱。部署目录 adapter 必须以内部 user UUID 与不可逆伪名 subject 联合查询，并返回
  同时回绑两个标识的 active/verified 邮箱；禁止用 raw `sub`、客户端邮箱或仅 user ID 的弱匹配发送。

依据：[Auth0 PKCE 官方流程](https://auth0.com/docs/api/authentication/authorization-code-flow-with-pkce/authorize-with-pkce)、
[Refresh Token Rotation](https://dev.auth0.com/docs/secure/tokens/refresh-tokens/refresh-token-rotation)。

### 2.2 Resend

- API 原生支持 `Idempotency-Key`，可与应用 `user + edition_date + channel` 唯一约束形成双层防重。
- Webhook 使用 Svix/Standard Webhooks：签名覆盖事件 ID、时间戳和原始 body，适合现有“验签后才开事务”
  的边界。
- MVP 邮件仅含固定提醒文案和私有深链，不发送研究正文；发送 payload 不添加 user ID 或业务标签。
- Webhook 只订阅 delivered/clicked/bounced/complained/failed/suppressed，数据库只保留 provider event ID、
  message ID、标准化状态与 raw-body SHA-256。

依据：[Resend Send Email API](https://resend.com/docs/api-reference/emails/send-email)、
[Idempotency Keys](https://resend.com/docs/dashboard/emails/idempotency-keys)、
[Webhook 验签](https://resend.com/docs/webhooks/verify-webhooks-requests)、
[事件类型](https://resend.com/docs/webhooks/event-types)。

### 2.3 OpenRouter

- 当前 T1 Gate 需要每次真实调用的 token 和实际 cost；OpenRouter 非流式响应会直接返回原生 tokenizer 的
  prompt/completion token 与 `usage.cost`，能进入现有 model usage ledger，不需要字符数或本地价格估算。
- 必须固定 model，启用 `require_parameters`，只允许支持 structured outputs 的 upstream；不能接受
  provider 静默忽略 JSON schema。
- 账户和请求 policy 必须关闭 input/output logging 与训练用途，使用 `data_collection=deny`，优先限制到
  ZDR endpoint；路由变化仍须保留实际返回 model/provider 证据。
- 当前 adapter 已把 `require_parameters=true`、`data_collection=deny`、`zdr=true` 和 upstream
  `only` allowlist 固定在每次请求内；fallback 默认关闭，开启时也不能越出批准 allowlist。环境布尔值
  拼写错误会拒绝启动。该代码约束不能替代账号级隐私设置、合同审批或真实 provider 回执。
- 若隐私/合同 owner 不接受中间网关，则改用模型厂商直连，但必须另建可对账的真实费用来源；不能把
  公开价目表估算伪装成 provider 实际费用。

依据：[OpenRouter usage/cost accounting](https://openrouter.ai/docs/cookbook/administration/usage-accounting)、
[API/structured outputs 参数能力](https://openrouter.ai/docs/guides/overview/models)、
[数据策略与 ZDR 路由](https://openrouter.ai/docs/guides/routing/provider-selection)、
[数据收集说明](https://openrouter.ai/docs/guides/privacy/data-collection)、
[OpenRouter FAQ（USD 基础货币）](https://openrouter.ai/docs/faq)。

### 2.4 日本官方数据

- TDnet 使用 JPX 官方付费 API，不以网页抓取替代生产合同；合同必须逐项确认读取、缓存、用户展示、AI
  处理、衍生摘要和再分发权利。
- EDINET 使用 Version 2 API 和独立 API key；它覆盖法定披露，不替代 TDnet 及时披露。
- 公司 IR 只作为补充来源。一家公司对应一个 `company_ir_*` provider、精确 hostname/path allowlist、
  parser、cursor 与 health；核心拒绝非公网 DNS、redirect、query token 和越界 URL。禁止把任意公司网址、
  通配域名或“通用网页抓取”直接加入 allowlist；网站改版只隔离该公司并触发重新验收。
- JPX 主数据、行情、指数和外汇必须分别登记权利；“官网可查看”不等于 SaaS 可缓存、展示或交给模型。

依据：[JPX TDnet API 条款](https://www.jpx.co.jp/english/markets/paid-info-listing/tdnet/p1j4l40000000q03-att/TermsTDAPIE.pdf)、
[JPX TDnet API Service Guide](https://www.jpx.co.jp/english/markets/paid-info-listing/tdnet/p1j4l40000000q03-att/API_ServiceGuideE.pdf)、
[EDINET 官方入口/API key 指引](https://disclosure2.edinet-fsa.go.jp/week0020.aspx)。

## 3. 必须签字的决定

下列字段全部填写并留存 approval reference 前，仍视为“未选定”：

| 决定 | Owner | 当前状态 | 必须提交的证据 |
|---|---|---|---|
| Auth0 套餐、tenant 地域、产品/运营 application | 产品 + 身份/安全 | 待确认 | tenant 配置导出 hash、audience/role 表、撤权演练 |
| Resend 套餐、发件域名、数据处理条款 | 产品 + 隐私/法务 | 推荐项，待确认 | 域名验证、SPF/DKIM/DMARC、DPA/条款 approval、四类回放 artifact |
| OpenRouter model/upstream/隐私 policy/预算 | 产品 + 模型成本 + 隐私 | 推荐项，待确认 | 固定配置、真实 usage/cost、strict schema、ZDR/deny policy artifact |
| TDnet 权利 | 数据许可/法务 | 待申请 | 合同 approval、允许用途矩阵、到期复核日 |
| EDINET 权利与 key owner | 数据许可/法务 | 待登记 | key owner、条款复核、采集/缓存/展示范围 |
| 首批公司 IR 白名单 | 数据许可/法务 + 数据工程 | 待确认 | 逐公司 URL/parser、用途矩阵、改版 owner、订正/撤回和窄范围样本 |
| JPX 主数据/行情/指数/外汇权利 | 数据许可/法务 | 待采购/确认 | 各数据集用途矩阵与合同 reference |
| 日美日历 feed | 数据工程 + 许可 | 待确认 | provider、SLA、节假日样本、错误回退规则 |
| Prometheus receiver/值班 | Infra/SRE | 待部署 | scrape/receiver 配置 hash、三态通知与双人值班演练 |

## 4. 接入顺序

1. 先批准 Auth0、Resend 和 OpenRouter 三个工程 provider，并建立 staging tenant/account；不触碰生产主库。
2. 用已完成的 Auth0 SDK bootstrap 配置 staging tenant、产品/运营 SPA client、精确允许 URL 和 role
   Action，完成刷新、产品/运营登出、撤权和跨会话 E2E；并完成 Resend 窄范围真实邮件与 OpenRouter
   单事件真实调用证据。
3. 并行完成 TDnet/EDINET/JPX 权利签字；只有签字后的 endpoint 才进入 source registry。
4. 用已完成的真实 source/calendar/market adapter 边界实现 `ApprovedMarketMorningProviderBundle`；由内置最终无参 production factory 完成严格装配，五项 preflight 全部 fail closed。
5. 产品库备份并单独批准 `0004 → 0017`；部署同一不可变 release。
6. 完成连续 5 个真实 JPX 交易日 staging 和 Alertmanager 演练，再运行 T1 总发布 Gate。

## 5. 明确不接受的捷径

- 不以 Yahoo、网页抓取、新闻转载或未审条款的免费 API 代替商业数据许可。
- 不把 ID Token 当 API access token，不把 localStorage 当 token vault，不把清浏览器状态当 session 撤权。
- 不在邮件正文复制朝刊内容，不公开分享私有深链，不在 provider tags 中放 user ID。
- 不用字符数估 token，不按公开价目表伪造实际费用，不允许模型 provider 静默忽略 strict schema。
- 不因“Beta”跳过数据合同、日本法律审查、生产备份或五日 staging Gate。
