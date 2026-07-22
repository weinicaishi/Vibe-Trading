# Market Morning Auth0 Staging

`market-morning-post-login.js` 是 Product 与 Operator SPA 共用的 Post-Login Action。
它只向自定义 API access token 写入 namespaced claims，不读取或保存 token、邮箱、原始 subject，
也不依赖 Enterprise-only session / refresh-token metadata。

## 部署

1. Auth0 Dashboard → Applications → APIs → Market Morning Staging API → Settings，
   把 `Maximum Access Token Lifetime (Seconds)` 固定为 `900` 并保存。项目后端会拒绝寿命超过
   15 分钟的 token；修改后必须重新登录，已经签发的旧 token 不会自动缩短。
2. Auth0 Dashboard → Actions → Library → Build Custom。
3. Trigger 选择 `Login / Post Login`，Runtime 使用当前受支持的 Node.js 版本。
4. 复制 `market-morning-post-login.js`，添加 Action Secret：
   `MARKET_MORNING_OPERATOR_CLIENT_ID=<Operator SPA Client ID>`。
5. Deploy，然后进入 Actions → Flows → Login，把 Action 拖入 flow 并 Apply。
6. 在负责签发私测邀请的 Operator 测试用户 `app_metadata` 中设置 access-admin 角色：

```json
{
  "market_morning_roles": ["market-morning-access-admin"]
}
```

只读运营账号应改用 `market-morning-reader`；Product 用户不需要角色。不要把 Client Secret、数据库
URL 或 API token 写入 Action 源码。

## Claims

- `https://market-morning.invalid/claims/auth-time`：只在 Auth0 提供真实 authentication method
  timestamp 时写入；缺失时普通访问可继续，高风险 recent-auth 操作保持拒绝。
- `https://market-morning.invalid/claims/roles`：只为 Operator SPA 写入，优先使用
  `app_metadata.market_morning_roles`，其次使用 Auth0 Authorization roles。

Staging 使用保留的 `.invalid` 命名空间，避免声称控制尚未申请的产品域名；获得正式域名后需以一次
受控配置变更同步替换 Action 与后端两个 claim 名称。
