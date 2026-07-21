# Market Morning MVP｜完成度审计

> 审计日期：2026-07-22。判定依据是当前代码、测试和已保存证据，不以计划描述或口头说明代替运行证据。

## 判定口径

- `完成`：代码、自动测试和当前可要求的证据均存在。
- `工程完成/外部待验`：核心或 adapter contract 已完成，但真实供应商、许可、凭据或 staging 证据缺失。
- `部分完成`：仍有计划内代码或运营流程缺口。
- `未开始`：没有足够当前证据。

## Sprint 审计

| 范围 | 当前判定 | 主要证据 | 尚缺证据/工作 |
|---|---|---|---|
| Sprint 0：隔离、配置、ADR、登记表、telemetry、骨架 | 部分完成 | feature flag、隔离领域层、开发文档、ADR、环境矩阵、telemetry 字典、生产 provider 推荐提案 | Auth0/Resend/OpenRouter 推荐项仍待 owner/预算/隐私批准；所有生产数据 owner 和许可仍未签字 |
| Sprint 1：账户、MySQL、IssuerMaster、搜索 | 工程完成/外部待验 | 0001–0017 migrations、MySQL 8 空库/升级/回退、远程 acceptance 12/12 与 migration 3/3、带备份/维护/revision/postflight Gate 的产品 schema change job、内置 OIDC/JWKS backend、前端 product/operator lifecycle 端口与官方 Auth0 SPA SDK bootstrap、邀请、停用、快照/规范化/别名模型、50 条 benchmark、固定原因且审计的别名审核队列/API | 真实 Auth0 tenant/application 的登录/刷新/登出/撤权 E2E、许可 IssuerMaster 导入、生产主库从 0004 升级到 0017 |
| Sprint 2：watchlist、设置、产品界面 | 工程完成/外部待验 | 用户 API、3–10 只规则、设置/同意/删除、认证初始化前不挂载私有 API 的 React shell、产品登录/登出/重试、仅 development 可用且明确标注 synthetic 的一键浏览器 Demo、真实 Playwright 朝刊/搜索添加/研究笔记/设置验收、权限测试及本地真实 MySQL 多用户/删除 probe | 真实 OIDC 跨页面/跨会话 E2E、产品主库升级后隔离验收 |
| Sprint 3：来源与事件标准化 | 工程完成/外部待验 | SourceAdapter fixtures、证据正文持久化、EDINET v2 与 TDnet 固定官方 endpoint/关注股过滤/revision cursor/PDF 安全下载、逐发行人公司 IR 精确 hostname/path/公网 DNS/parser/revision/独立 health adapter、cursor/record/event version、订正/撤回、merge candidate、只记录决策且不自动合并的审核队列/API、coverage 和测试 | TDnet/EDINET 权利/key、首批公司 IR 逐公司权利/URL/parser 与三类窄范围 staging |
| Sprint 4：日历、快照、global run、runtime | 工程完成/外部待验 | fixture + 启动前预加载 licensed calendars、五品种精确 HTTPS market adapters、strict runtime assembly、内置最终无参 production factory、同步/异步 provider bundle loader、默认关闭的独立 Compose runtime profile、worker 覆盖共享 API healthcheck、真实容器 build/fail-closed smoke、06:00 source、06:30 snapshot、scheduler/worker/lease、global run/preflight、MySQL 8 双 worker SKIP LOCKED 实测 | 具体获权 calendar/market endpoint/header/parser、真实 deployment provider bundle、5 日 staging |
| Sprint 5：EventBrief 与内容 Gate | 工程完成/外部待验 | strict EventBrief、引用/数字/禁词 Gate、degraded、OpenRouter 官方 endpoint + model/upstream allowlist + ZDR/deny + usage/cost fail-closed adapter、reachability adapter、审核下架、固定原因内容报告、别名及 merge candidate 隐私安全运营处理 | 批准具体 model/upstream 与 hostname；真实 strict-schema/usage/cost 和隐私策略 staging 证据 |
| Sprint 6：用户朝刊、研究、邮件、观测 | 工程完成/外部待验 | lazy edition、私有状态、来源打开、个股页/备注、delivery/webhook、Resend 发送与 Svix 验签/事件 parser、严格回绑 user UUID + 伪名 subject 的 email identity resolver factory、共享 signer/session factory 的 Resend dispatch+delivery runner 装配、opaque fragment 深链 signer/轮换/认证后兑换及不落浏览器存储的时序、ops/metrics、Prometheus rules/scrape、warning/critical 分路且 secret-file-only 的 Alertmanager 基线、OIDC/JWKS 验签/轮换/伪名/角色及 session-validator port | Resend 账号/域名与真实登录回放、部署身份目录 adapter、provider-backed session invalidation、真实 Prometheus/Alertmanager receiver 通知链 |
| Sprint 7：T0、运行手册、隐私、T1 准备 | 部分完成 | T0 12 场景 manifest、运行手册、邀请/停用/导出/删除、本地及远程专用 MySQL 12+3 全通过、CI 自动 MySQL Gate、把 clean tree/依赖锁/0017 数据库包/前后端与 MySQL 输出绑定到同一 Git revision 的不可变 release candidate Gate、把 11 项 raw probe hash 自动绑定到同一 candidate/run/calendar 的单项 artifact 与单日 evidence builder、五日证据 CLI、四类证据 T1 总发布 Gate、个人化运营认证 port 与四级最小权限、运营前端登录/登出/初始化失败恢复、Auth0 callback 成败清理与 logout 私有数据卸载、严格 monitoring drill 证据合同/CLI/默认 blocked 模板 | 11 Gate 连续 5 日、五项真实外部签字、真实 Auth0 产品/运营角色与撤权 E2E、真实 receiver 的双人值班/告警恢复演练 |

## 当前验证基线

- Market Morning 普通后端回归：直接按 `agent/tests/test_market_morning_*.py` 运行得到 `867 passed, 15 skipped`；15 项仅因普通命令未显式授权两套专用数据库写入。隔离和远程专用 MySQL 在线证据均为业务 acceptance `12/12 passed`、destructive migration `3/3 passed`；2026-07-22 最新远程 retest manifest 分别为 `mysql-acceptance-remote-retest-2026-07-22.json` 与 `mysql-migration-remote-retest-2026-07-22.json`。产品 schema change job 的实际只读 preflight 已确认主库为 `0004`/14 表，未执行 Alembic。
- SQL 交付包：`market_morning_schema_0017.sql` 已直接导入第三个 MySQL 8 空库，核对为 33 张业务表、精确 `0017` revision、`varchar(64)` 版本字段及 `utf8mb4_ja_0900_as_cs`。
- Vibe-Trading 整仓后端：按 CI 约定排除独立 `agent/tests/e2e_backtest` 和真实 LLM 专用 `test_e2e_harness_v2.py` 后，本轮实测为 `6300 passed, 24 skipped`，无失败或错误。24 项 skip 均为已登记的 MySQL／合成因子／Tushare 前置条件，无未知 skip。CI 环境变量 Gate 通过，仅保留一条既有 `os.environ.pop()` 非阻断 warning。
- T0 合成演练：12/12 通过，证据位于 `docs/evidence/market-morning/t0-local-synthetic-2026-07-21.json`，固定不计为 staging 日。
- 前端：341 项测试通过，TypeScript 与 production build 通过；覆盖 product/operator lifecycle 并发初始化、官方 Auth0 SDK 配置/PKCE/memory cache/callback 清理/revoke/logout、401、初始化失败重试、邮件深链认证后兑换时序，以及 Demo 只能在 development 启用、状态变更和完整 API 代理路径。
- 容器运行时：`docker compose --profile market-morning build market-morning-runtime` 已从干净源码真实构建成功，容器内前端 build 与哈希锁定 Python 安装均通过；镜像以 uid 1000 的非 root `vibe` 用户运行，`asyncmy` 和最终 production runtime factory 可导入。worker service 显式禁用共享 API 镜像的 `:8899/live` healthcheck；禁用态容器以 exit 2 和稳定 `market_morning_disabled` 错误码拒绝启动，未访问数据库。
- 本地浏览器 Demo：`npm run dev:market-morning-demo` 固定启动在 `127.0.0.1:5901`；Playwright 已验证朝刊明确显示 synthetic/订正/“不足以判断”、9432 搜索添加后为 4/10、丰田研究笔记保存和邮件提醒设置保存。8 个业务 API 请求均为 200，控制台 0 error/0 warning；证据见 `browser-demo-smoke-2026-07-22.md`，固定不计为 staging 日。
- 不可变发布候选：新增 17 项专项测试和 CI artifact；clean tree、预期/实际 revision、当前 schema、14 个固定发布源文件（含最终 production runtime factory、运行手册和两份严格证据模板）及 5 份验证输出任一缺失、未跟踪、失配或不可读都会 `blocked`。Market Morning 的计划、ADR、运行手册与隐私安全证据目录已从仓库全局 `docs/` 忽略规则中精确放行；浏览器日志、截图、CodeGraph 索引、`output/` 和本地 zip 仍明确忽略。提交前的脏工作树会被正确拒绝；CI 只能在 clean checkout 上为该提交生成通过的 manifest。输出不包含路径、Git status 原文、URL 或凭据。该 Gate 固定不计为 staging/T1 证据。
- Staging 日报生产链：新增单项 Gate artifact CLI 与单日 builder；raw probe 只进入 SHA-256，11 项 artifact 必须共享通过的 candidate、release/schema、run、刊期和 JPX calendar 链，任一失败会保留失败日报但固定不计数。相关 release/staging evidence 专项共 60 项通过；当前仍无真实 staging 日报，因此没有提前满足五日 Gate。
- 监控部署基线：Prometheus rules、HTTPS Bearer-file scrape、Alertmanager secret-file receiver 路由和严格 monitoring drill 证据合同共 19 项专项测试通过；仓库内模板固定 `failed`，真实 receiver 尚未演练。
- 静态检查：Market Morning 及本轮触达的 API/指数/MCP 测试范围 Ruff 通过；仓库级既有 lint 债务仍单独记录，不冒充本模块失败。
- T1 staging：严格合同和聚合器已有 19 项测试，但真实合格日报为 0，五日 Gate 未通过。
- T1 总发布 Gate：严格聚合 `environment=staging` 的真实 MySQL、destructive migration、五日 staging 和 TDnet/EDINET/公司 IR registry/JPX/日本法律五项签字；本地/CI 证据会被拒绝，当前仍只有 blocked 模板。

## 下一批工程优先级

1. 先用受控 schema change job 执行只读 preflight；在维护窗口完成可恢复备份后，把远程产品主库从 `0004` 升级到 `0017`。两个专用测试库已完成 12+3 并恢复到 `0017`/33 表。
2. 审批 `market-morning-provider-selection-proposal.md` 中的 Auth0/Resend/OpenRouter 推荐项；OIDC 后端、官方 Auth0 SPA SDK bootstrap、前端 lifecycle/产品与运营界面、Resend adapter、通用邮箱 identity resolver 与深链 signer/兑换已可直接使用，但 deployment 仍须建立真实 Auth0 tenant/application、提供 provider-backed session validator 与身份目录 adapter，并为三个外部 preflight 提供真实实现和 E2E 证据。
3. TDnet/EDINET 与 calendar/market 权利 Gate 批准后，在 deployment provider bundle factory 中用 `SqlAlchemyTrackedIssuerCodeResolver`
   注入两个官方 adapter；为首批公司逐一注入 `CompanyIrApprovedFeedAdapter`、批准 URL/path 与 parser，
   并用 strict loader/adapter 注入两份日历与五项行情；先做不对外的限频、正文、订正/撤回、节假日和
   行情 session/delay staging。
4. 在已提供的 Alertmanager 路由基线上配置真实 secret-file receiver，完成 warning/critical/resolved、
   silence 到期和双人值班演练，再用 monitoring drill CLI 生成可供每日 `observability` Gate 引用的
   hash-only 证据。
5. 先让 CI 生成 `status=passed` 的不可变 release candidate manifest；只部署其中的 revision，并用同一 revision 完成连续 5 个真实 staging JPX 交易日，再申请 T1 签字。
