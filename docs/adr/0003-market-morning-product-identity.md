# ADR 0003：产品身份由 deployment-owned OIDC 适配层提供

- 状态：Accepted
- 日期：2026-07-21
- 适用范围：产品登录、邀请、会话、停用和高风险账户操作

## 背景

Vibe API Key 是内部服务凭据，不具备注册用户、多租户、邮箱验证、会话撤销或近期认证语义。核心仓库也不应绑定单一身份供应商。

## 决策

- 核心提供 `src.market_morning.oidc_auth` 标准 OIDC/JWKS factory；部署侧也可为非标准 provider 返回自定义 `MarketMorningProductAuthAdapter`。
- 适配器必须固定 issuer、audience 和允许算法，验证 signature、`exp`、`nbf`，处理 JWKS rotation，并在每次请求检查 token/session 撤销。
- 核心只持久化 opaque external subject，不保存 bearer token。
- 产品 API 从已验证 principal 推导用户身份，不接受客户端提交 user ID。
- 邀请 onboarding 允许已验证但尚未建档的 subject；普通 API 只允许 active、未删除且具有效私测/试用/订阅状态的用户。
- 账户删除要求 provider 证明五分钟内近期认证，并在请求提交后立即使普通产品访问和邮件资格失效。
- 内部运营身份与产品身份分离；T1 前必须从固定 API Key actor 升级为可审计的个人运营角色。
- 内置实现要求逐请求 session validator，并把 issuer-scoped subject 哈希为 opaque identity；运营角色只允许显式映射到四项固定最小权限。

## 后果

- 未配置真实 adapter 时产品认证 fail closed；合成 UI 不构成登录验收。
- 通用 JWKS 验签、rotation、claim/算法防护和角色映射已有自动测试；供应商选型、真实 session invalidation、登录/刷新/登出与深链仍必须在 staging 留证。
