# Market Morning MVP｜完成度审计

> 审计日期：2026-07-23。判定依据是当前代码、测试和已保存证据，不以计划描述或口头说明代替运行证据。

## 判定口径

- `完成`：代码、自动测试和当前可要求的证据均存在。
- `工程完成/外部待验`：核心或 adapter contract 已完成，但真实供应商、许可、凭据或 staging 证据缺失。
- `部分完成`：仍有计划内代码或运营流程缺口。
- `未开始`：没有足够当前证据。

## Sprint 审计

| 范围 | 当前判定 | 主要证据 | 尚缺证据/工作 |
|---|---|---|---|
| Sprint 0：隔离、配置、ADR、登记表、telemetry、骨架 | 部分完成 | feature flag、隔离领域层、开发文档、ADR、环境矩阵、telemetry 字典、生产 provider 推荐提案；已选择并实现应用侧 hash-only 可撤销 session ledger | Resend/OpenRouter 与生产数据的 owner/预算/隐私批准及许可仍未签字；Alpha A 暂不把 Resend 伪装为已完成 |
| Sprint 1：账户、MySQL、IssuerMaster、搜索 | 工程完成/外部待验 | 0001–0018 migrations、MySQL 8 空库/升级/回退、远程 acceptance 12/12 与 migration 3/3、带备份/维护/revision/postflight Gate 的产品 schema change job、内置 OIDC/JWKS backend、Auth0 Post-Login Action、access-token hash session ledger、前端 product/operator lifecycle、邀请、停用、快照/规范化/别名模型、50 条 benchmark、固定原因且审计的别名审核队列/API；Alpha A 已用 900 秒真实 Auth0 token 完成 Product/Operator 登录、刷新轮换、角色保护、精确注销与多 token 隔离 | 许可 IssuerMaster 导入、生产主库从 0004 升级到 0018、同一不可变 revision 的非 localhost HTTPS Staging OIDC 复验 |
| Sprint 2：watchlist、设置、产品界面 | 工程完成/外部待验 | 用户 API、3–10 只规则、设置/同意/删除、认证初始化前不挂载私有 API 的 React shell、产品登录/登出/重试、仅 development 可用且明确标注 synthetic 的一键浏览器 Demo、真实 Playwright 朝刊/搜索添加/研究笔记/设置验收、权限测试及本地真实 MySQL 多用户/删除 probe；Alpha A 已完成真实 OIDC 关注股写入与第二个独立 token 会话读取 | 产品主库升级后隔离验收、非 localhost HTTPS Staging 跨会话复验 |
| Sprint 3：来源与事件标准化 | 工程完成/外部待验 | SourceAdapter fixtures、证据正文持久化、EDINET v2 与 TDnet 固定官方 endpoint/关注股过滤/revision cursor/PDF 安全下载、逐发行人公司 IR 精确 hostname/path/公网 DNS/parser/revision/独立 health adapter、cursor/record/event version、订正/撤回、merge candidate、只记录决策且不自动合并的审核队列/API、coverage 和测试 | TDnet/EDINET 权利/key、首批公司 IR 逐公司权利/URL/parser 与三类窄范围 staging |
| Sprint 4：日历、快照、global run、runtime | 工程完成/外部待验 | fixture + 启动前预加载 licensed calendars、五品种精确 HTTPS market adapters、strict runtime assembly、内置最终无参 production factory、同步/异步 provider bundle loader、默认关闭的独立 Compose runtime profile、worker 覆盖共享 API healthcheck、真实容器 build/fail-closed smoke、06:00 source、06:30 snapshot、scheduler/worker/lease、global run/preflight、MySQL 8 双 worker SKIP LOCKED 实测 | 具体获权 calendar/market endpoint/header/parser、真实 deployment provider bundle、5 日 staging |
| Sprint 5：EventBrief 与内容 Gate | 工程完成/外部待验 | strict EventBrief、引用/数字/禁词 Gate、degraded、OpenRouter 官方 endpoint + model/upstream allowlist + ZDR/deny + usage/cost fail-closed adapter、reachability adapter、审核下架、固定原因内容报告、别名及 merge candidate 隐私安全运营处理 | 批准具体 model/upstream 与 hostname；真实 strict-schema/usage/cost 和隐私策略 staging 证据 |
| Sprint 6：用户朝刊、研究、邮件、观测 | 工程完成/外部待验 | lazy edition、私有状态、来源打开、个股页/备注、delivery/webhook、Resend 发送与 Svix 验签/事件 parser、严格回绑 user UUID + 伪名 subject 的 email identity resolver factory、共享 signer/session factory 的 Resend dispatch+delivery runner 装配、opaque fragment 深链 signer/轮换/认证后兑换、ops/metrics、Prometheus rules/scrape、warning/critical 分路且 secret-file-only 的 Alertmanager 基线、OIDC/JWKS 验签/轮换/伪名/角色及应用侧即时撤销 | Alpha B 的 Resend 账号/域名与真实登录回放、部署身份目录 adapter、真实 Prometheus/Alertmanager receiver 通知链 |
| Sprint 7：T0、运行手册、隐私、T1 准备 | 部分完成 | T0 12 场景 manifest、运行手册、邀请/停用/导出/删除、远程专用 MySQL 12+3 全通过、CI 自动 MySQL Gate、把 clean tree/依赖锁/0018 数据库包/Auth0 Action 与部署合同/前后端和 MySQL 输出绑定到同一 Git revision 的不可变 release candidate Gate、把 11 项 raw probe hash 自动绑定到同一 candidate/run/calendar 的单项 artifact 与单日 evidence builder、对 `oidc_session` 强制同 release/run/刊期的 900 秒 TTL、Product/Operator、精确注销与跨会话无敏感数据合同、从 owner-only token 文件执行完整真实链路的脱敏自动探针、五日证据 CLI、四类证据 T1 总发布 Gate、四级最小权限、运营前端登录/登出/初始化失败恢复、严格 monitoring drill 证据合同/CLI/默认 blocked 模板；Alpha A 本地真实 Auth0 E2E 已通过 | 11 Gate 连续 5 日、五项真实外部签字、非 localhost HTTPS Staging 的同 revision OIDC 证据、真实 receiver 的双人值班/告警恢复演练 |

## 当前验证基线

- Market Morning 普通后端回归：直接按 `agent/tests/test_market_morning_*.py` 运行得到 `922 passed, 15 skipped`；15 项仅因普通命令未显式授权两套专用数据库写入。专用远程 MySQL 8 的业务 acceptance 为 `12/12 passed`，其中 schema/session 场景已覆盖 access-token hash 的幂等首见、精确注销、不可复活与多 token 隔离；destructive migration 为 `3/3 passed`。两份 manifest 均为 `environment=ci` 且固定不计 staging/T1。
- SQL 交付包：`market_morning_schema_0018.sql` 已直接导入 MySQL 8 空库，核对为 34 张业务表、精确 `0018_market_morning_auth_sessions` revision、`varchar(64)` 版本字段及 `utf8mb4_ja_0900_as_cs`。
- Vibe-Trading 整仓后端：release CI 在不包含任何真实 `.env` 的干净 runner 中运行 `agent/tests`，得到 `6354 passed, 25 skipped`、无失败或错误。25 项 skip 均为已登记的 MySQL／合成因子／Tushare 前置条件。CI 环境变量 Gate 与语法检查通过。
- T0 合成演练：12/12 通过，证据位于 `docs/evidence/market-morning/t0-local-synthetic-2026-07-21.json`，固定不计为 staging 日。
- 前端：342 项测试通过，TypeScript 与 production build 通过；覆盖 product/operator lifecycle 并发初始化、官方 Auth0 SDK 配置/PKCE/memory cache/callback 清理/revoke/logout、无 bearer 的无凭据开发诊断、401、初始化失败重试、邮件深链认证后兑换时序，以及 Demo 只能在 development 启用、状态变更和完整 API 代理路径。
- 容器运行时：`docker compose --profile market-morning build market-morning-runtime` 已从干净源码真实构建成功，容器内前端 build 与哈希锁定 Python 安装均通过；镜像以 uid 1000 的非 root `vibe` 用户运行，`asyncmy` 和最终 production runtime factory 可导入。worker service 显式禁用共享 API 镜像的 `:8899/live` healthcheck；禁用态容器以 exit 2 和稳定 `market_morning_disabled` 错误码拒绝启动。容器内只读 schema probe 对产品库返回 `Market Morning database schema is not current`，对 acceptance 库返回 `ready`；清空 provider bundle factory 后正式 CLI 以稳定 `production_provider_bundle_factory_missing` 拒绝启动，未运行采集、模型或邮件任务。
- 本地浏览器 Demo：`npm run dev:market-morning-demo` 固定启动在 `127.0.0.1:5901`；Playwright 已验证朝刊明确显示 synthetic/订正/“不足以判断”、9432 搜索添加后为 4/10、丰田研究笔记保存和邮件提醒设置保存。8 个业务 API 请求均为 200，控制台 0 error/0 warning；证据见 `browser-demo-smoke-2026-07-22.md`，固定不计为 staging 日。
- Alpha A 本地真实 OIDC：Product/Operator 均使用新签发的 900 秒 Auth0 access token；浏览器虚拟推进 16 分钟后都取得不同的新 token，刷新后的受保护 API 为 200。Product 已把 7203 写入独立 staging MySQL，并由第二个独立 token 会话读到同一关注股；精确注销后旧 Product/Operator token 均为 401，未注销的另一个 Product token 仍为 200。去敏证据见 `alpha-a-oidc-local-2026-07-23.json`；它不保存 token、OAuth code、邀请码或身份信息，并固定 `counts_as_staging_day=false`、`counts_as_t1_release_evidence=false`。
- 不可变发布候选：固定源文件已从 18 项扩为 20 项，在 Auth0 Post-Login Action、900 秒部署合同、OIDC staging 严格合同与默认 blocked 模板之外，把自动化真实 OIDC 演练探针及 CLI 纳入 SHA-256 绑定。远端发布线必须由 GitHub Actions 对最终同一 revision 独立复验，本地 manifest 不替代 CI；两类候选均固定 `counts_as_staging_day=false`、`counts_as_t1_release_evidence=false`。候选历史和实时有效 revision 以 `release/market-morning-mvp` 的 CI artifact 为准，不在文档中手填一个可能过期的“当前” hash。
- Staging 日报生产链：新增单项 Gate artifact CLI 与单日 builder；raw probe 只进入 SHA-256，11 项 artifact 必须共享通过的 candidate、release/schema、run、刊期和 JPX calendar 链，任一失败会保留失败日报但固定不计数。release/staging/OIDC evidence 合同与自动探针专项共 90 项通过；当前仍无真实 staging 日报，因此没有提前满足五日 Gate。
- 监控部署基线：Prometheus rules、HTTPS Bearer-file scrape、Alertmanager secret-file receiver 路由和严格 monitoring drill 证据合同共 19 项专项测试通过；仓库内模板固定 `failed`，真实 receiver 尚未演练。
- 静态检查：Market Morning 及本轮触达的 API/指数/MCP 测试范围 Ruff 通过；仓库级既有 lint 债务仍单独记录，不冒充本模块失败。
- T1 staging：严格合同和聚合器已有 19 项测试，但真实合格日报为 0，五日 Gate 未通过。
- T1 总发布 Gate：严格聚合 `environment=staging` 的真实 MySQL、destructive migration、五日 staging 和 TDnet/EDINET/公司 IR registry/JPX/日本法律五项签字；本地/CI 证据会被拒绝，当前仍只有 blocked 模板。

## 下一批工程优先级

1. 把当前 revision 推到独立开发线与 release 线并通过不可变 release-candidate CI；随后只部署该 manifest 绑定的同一 revision 到非 localhost HTTPS Staging，使用 owner-only token 文件运行自动 OIDC 探针，复验 Alpha A 已通过的 900 秒刷新、角色、精确注销和 Product 跨会话关注股。该步骤才可生成可计数的 `oidc_session` Gate。
2. 产品主库升级必须等待维护窗口与变更单批准、可恢复备份验证及 API/worker/scheduler 停止；满足后才可从 `0004` 升级到 `0018`。两个专用测试库已完成 12+3 并恢复到 `0018`/34 表。
3. TDnet/EDINET 与 calendar/market 权利 Gate 批准后，在 deployment provider bundle factory 中用 `SqlAlchemyTrackedIssuerCodeResolver`
   注入两个官方 adapter；为首批公司逐一注入 `CompanyIrApprovedFeedAdapter`、批准 URL/path 与 parser，
   并用 strict loader/adapter 注入两份日历与五项行情；先做不对外的限频、正文、订正/撤回、节假日和
   行情 session/delay staging。
4. 在已提供的 Alertmanager 路由基线上配置真实 secret-file receiver，完成 warning/critical/resolved、
   silence 到期和双人值班演练，再用 monitoring drill CLI 生成可供每日 `observability` Gate 引用的
   hash-only 证据。
5. 当前账号的 `weinicaishi/Vibe-Trading` fork、独立开发/发布分支和不可变 release-candidate CI 已建立；每次最终文档 revision 均须推送开发线与 release 线并通过 GitHub Actions。只部署通过 manifest 的 release revision，并用同一 revision 完成连续 5 个真实 staging JPX 交易日，再申请 T1 签字。
