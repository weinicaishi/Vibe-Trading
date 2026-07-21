# Market Morning｜数据来源与权利登记表

> 状态：T1 外部 Gate 登记表。`Blocked` 不代表工程 adapter 不存在，而是尚无足以对外使用的合同/法务证据。任何状态变更都必须附合同、条款快照或书面批准的内部引用；本文件不存凭据和合同正文。

## 状态定义

- `Blocked`：owner、权利或凭据任一未确认，不得进入 T1。
- `Review`：已指定 owner，正在审查条款或技术开通。
- `Approved narrow`：仅批准明确列出的私测范围。
- `Approved production`：展示、AI 处理和目标用户范围均已批准。
- `Development only`：仅可用于本地/CI，不得对外。

## 来源登记

| 来源 | 产品用途 | 读取 | 缓存 | 用户展示 | AI 处理 | 再分发 | 凭据状态 | 业务/法务 owner | 工程 owner | 复核日 | 当前状态 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| JPX IssuerMaster / 上场公司主表 | 公司代码、正式名、市场、搜索 | 待确认 | 待确认 | 待确认 | 不需要全文 AI | 待确认 | 未开通/未登记 | **未指定（T1 blocker）** | 待指定 | 未安排 | Blocked |
| TDnet 适时开示 | 关注股事件、订正、删除、受限 PDF 文本与原始回链 | 待确认 | 仅结构化记录与≤50k证据文本，待确认 | 待确认；公开 PDF 回链的可用期需确认 | PDF 文本送 EventBrief，待确认 | 待确认 | access key 未登记；付费 API adapter 工程完成 | **未指定（T1 blocker）** | 待指定 | 未安排 | Blocked |
| EDINET | 法定披露事件、元数据、受限 PDF 文本、原始回链 | 待确认 | 仅结构化记录与≤50k证据文本，待确认 | 待确认 | PDF 文本送 EventBrief，待确认 | 待确认 | API key 未登记；v2 adapter 工程完成 | **未指定（T1 blocker）** | 待指定 | 未安排 | Blocked |
| 公司 IR 白名单 | 发行人公告补充来源、受限 PDF/HTML/元数据证据与原始回链 | 逐公司确认 | 只保存结构化记录与≤50k证据文本，逐公司确认 | 逐公司确认 | 逐公司确认 | 逐公司确认 | 逐发行人安全 adapter 工程完成；尚未批准实际 feed/URL/parser | **未指定（T1 blocker）** | 待指定 | 未安排 | Blocked |
| Nikkei 225 | 市场环境快照 | 待确认 | 待确认 | 待确认 | 仅结构化摘要，待确认 | 待确认 | 未选定 provider | **未指定（T1 blocker）** | 待指定 | 未安排 | Blocked |
| S&P 500 | 隔夜市场快照 | 待确认 | 待确认 | 待确认 | 仅结构化摘要，待确认 | 待确认 | 未选定 provider | **未指定（T1 blocker）** | 待指定 | 未安排 | Blocked |
| Nasdaq Composite | 隔夜市场快照 | 待确认 | 待确认 | 待确认 | 仅结构化摘要，待确认 | 待确认 | 未选定 provider | **未指定（T1 blocker）** | 待指定 | 未安排 | Blocked |
| DJIA | 隔夜市场快照 | 待确认 | 待确认 | 待确认 | 仅结构化摘要，待确认 | 待确认 | 未选定 provider | **未指定（T1 blocker）** | 待指定 | 未安排 | Blocked |
| USD/JPY | 汇率环境快照 | 待确认 | 待确认 | 待确认 | 仅结构化摘要，待确认 | 待确认 | 未选定 provider | **未指定（T1 blocker）** | 待指定 | 未安排 | Blocked |
| JPX trading calendar | 是否发布、previous/next session | 待确认 | 待确认 | 仅状态/日期，待确认 | 不送入模型 | 待确认 | 未选定 provider | **未指定（T1 blocker）** | 待指定 | 未安排 | Blocked |
| US trading calendar | 美国休市与最后有效 session | 待确认 | 待确认 | 仅状态/日期，待确认 | 不送入模型 | 待确认 | 未选定 provider | **未指定（T1 blocker）** | 待指定 | 未安排 | Blocked |
| Yahoo 开发行情 | 本地四指数开发验证 | 允许开发读取，需复核条款 | 仅本地短缓存 | **禁止产品展示** | **禁止产品 AI** | **禁止** | 无 | 不适用 | 工程维护 | 发布前复核 | Development only |
| 合成 fixture | CI、T0 演练、UI demo | 允许 | 允许 | 仅明确 demo 环境 | 允许测试 | 禁止冒充真实数据 | 无 | 不适用 | 工程维护 | 每次 schema 变更 | Development only |

## 批准证据最小字段

每个进入 `Approved narrow` 或 `Approved production` 的来源必须补齐：

1. provider 和具体产品/SKU；
2. 合同或条款版本、内部证据引用、批准日期与批准人；
3. 允许的读取、缓存时长、展示字段、AI 输入、派生内容和再分发范围；
4. 用户范围（内部、staging、邀请制 T1、公开 Beta、付费）；
5. 限频、SLA、数据延迟和事故联系方式；
6. 凭据 owner、rotation 周期和到期复核日；
7. 删除/停止使用时的数据处置要求。
