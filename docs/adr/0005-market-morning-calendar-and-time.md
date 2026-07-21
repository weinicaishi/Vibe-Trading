# ADR 0005：UTC 存储、JST 刊期与 licensed JPX calendar

- 状态：Accepted
- 日期：2026-07-21
- 适用范围：朝刊业务日期、采集窗口、发布与邮件

## 背景

日美节假日不对称，普通工作日 cron 不能决定日本市场是否开市。盘前产品还必须区分 UTC 时间、JST 刊期和来源 session date。

## 决策

- 所有技术时间存 UTC，数据库 session 固定 UTC；`edition_date_jst` 明确表示日本业务日期。
- licensed JPX 现货日历是是否发布的唯一真相；美国市场日历只决定隔夜市场状态和最后有效 session。
- 06:30 JST 为采集截止，07:00 首判，07:15/07:30/08:00/08:30 为确定性重试槽。
- 日本休市不发朝刊或邮件，但来源采集可继续；美国休市要展示最后有效美股日期。
- 08:30 后首次完成只能站内标记 `late`，不得补发邮件。
- 人工覆盖只能停发或撤销停发，不能强制开市、强制发布或绕过内容 Gate。

## 后果

- fixture calendar 只用于测试；生产 runtime 必须拒绝 fixture provider。
- 临时休市、日美不对称和连续 staging 日必须用授权 provider 的 previous/next session 证据验收。
