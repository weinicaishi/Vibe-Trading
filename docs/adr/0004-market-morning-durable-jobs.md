# ADR 0004：MySQL durable jobs 与带租约的单活 scheduler

- 状态：Accepted
- 日期：2026-07-21
- 适用范围：采集、标准化、EventBrief、快照、全局刊、邮件和账户删除任务

## 背景

盘前发布必须能从进程退出、网络失败、重复触发和并发 worker 中恢复。普通 cron 或进程内队列不能证明幂等与接管行为。

## 决策

- 任务、attempt 和 scheduler lease 分别持久化在 `mm_jobs`、`mm_job_attempts`、`mm_scheduler_leases`。
- worker 使用 `SELECT ... FOR UPDATE SKIP LOCKED` 领取任务；幂等键和 payload hash 防止重复或漂移。
- 执行期间 heartbeat；lease 丢失立即取消 handler，不得落成功。
- 网络、模型和 provider 调用在数据库事务外；读取/锁定/写入使用短事务。
- scheduler 使用 owner、lease expiry 和 heartbeat 的数据库租约，不把连接级 advisory lock 作为唯一正确性保障。
- 失败区分可重试与永久错误，保存稳定脱敏代码，不保存原始 provider body 或 secrets。
- API、worker、scheduler 和 migration job 在部署上分离；runtime preflight 全部通过前不得领取任务。

## 后果

- 真实 MySQL 的锁、唯一约束、lease 和删除事务仍需专用 acceptance database 验收。
- 重跑只能使用原幂等语义；禁止通过手工 SQL 把失败 job 改为成功。
