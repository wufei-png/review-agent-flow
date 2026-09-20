# 02 · 终态保留与 Worker 所有权

状态：本地完成；现场证据待 05。优先级：P1。前置条件：[01 · 通知可领取与结果语义](01-notification-and-outcome.md) 完成。

## 交付结果

通知敏感内容从投递终态起保留规定天数；排队较久的通知不会在完成后立即被清空。一个部署同时只有一个拥有有效运行时租约的 Worker，第二个启动实例不能领取工作，崩溃后的新实例可在租约到期后接管。既有三个内部循环继续独立运行。

## 实施前契约与修改入口

- `store.py` schema 16 的 `notification_outbox` 有 `created_at`、`delivered_at`，没有通用终态时间。`purge_sensitive_payloads()` 用 outbox/request 的 `created_at` 做保留判断。请求可能对应多个 channel；请求内容应等所有 channel 都终结且超过各自保留期再清除。
- `worker.py::run_forever()` 管理 Review/Publication、Notification、Reconciliation/Cleanup 三个线程；`cli.py::_run_worker()` 还提供 `--once`。ADR-0020 承诺一个受监管的 Worker OS 进程，但当前 Store 没有进程级所有权。
- `docs/adr/0024-retention-and-systemd-runtime.md` 要求稳定终态时间和敏感内容保留；`docs/adr/0021-target-policy-snapshots-and-operator-notifications.md` 记录 schema 16 的预生产 hard cut。部署资料与 `tests/test_retention.py`、`test_worker.py`、`test_store.py` 需随契约更新。

## 阶段

1. **一次 schema 17 切换与终态保留** — 在新 schema 中加入 notification `terminal_at` 和 Worker 运行时单例租约表；保留普通投递及 Auto-fix 通知的现有状态名。成功、尝试耗尽或过期租约造成最终失败时只设置一次 `terminal_at`；待重试行不设置。outbox 内容按自身终态时间清理，请求内容等相关 delivery 全部终结且最近一次终态已超过期限后清理；没有 delivery 的请求要有明确、可测试的保留规则。**依赖：** 01 完成，切换前保留旧 canary 证据及一致性数据库快照。**验收：** “31 天前入队、今天终结”不立即清理；保留期后才清理；多 channel 由最晚终态决定请求清理；未终结不清理；旧 schema 拒绝符合既有 hard-cut 契约。一个本地提交。
2. **落实单 Worker 所有权** — Worker 服务入口用 Store 单例租约 acquire、独立 heartbeat 和 token-fenced release；丢失所有权时停止新领取并使进程失败。常驻模式和 CLI `--once` 均受同一约束，长时间 Agent/Verifier 执行期间仍续租。保存安全的 owner/heartbeat 摘要供下一任务只读诊断，claim token 不进入日志或状态。**依赖：** 阶段 1。**验收：** 并发启动仅一方成功，旧 owner 不能释放接管者租约，异常退出后 TTL 接管，正常停机释放，三个循环退出行为及 `--once` 回归通过。一个本地提交。

## 验证与部署边界

逐阶段跑 `pytest -q tests/test_retention.py tests/test_store.py tests/test_worker.py tests/test_cli.py`；结束跑 `ruff check .`、`pytest`、`python -m compileall -q src tests`、`git diff --check`。部署时使用新数据库或明确批准的预生产重建；schema 16 不做隐式升级。新的生产数据一旦需要保留，升级必须先准备一致性备份及回滚方案。实施时更新当前规格/ADR 的 schema 版本与运行时契约，勿让文档停留在 16。

完成记录写入[执行队列](pilot-readiness-queue.md)，并在本文记录提交、检查与 schema 切换证据。

## 完成记录

- 2026-09-20 阶段 1：`b449419` 将新建 SQLite schema 切到 17，加入通知 `terminal_at` 和 Worker 单例租约表。投递成功、尝试耗尽及租约过期终结时写入稳定终态时间；清理按各 delivery 终态时间及多渠道最晚终态判断。无 delivery 请求按创建时间清理。定向测试通过，并验证 schema 16 的读写打开都拒绝。
- 阶段 2：`dc442dc` 在 Store 提供原子 acquire、独立 heartbeat、token-fenced release 和不含 token 的所有权摘要；常驻 Worker 与 `--once` 共用租约，三个循环保留。定向测试通过，覆盖并发竞争、TTL 接管、旧 token fencing、正常释放、长任务续租及循环退出。
- 实施后新的只读 review 提出两项有效发现：P1 `--once` 通知批次失租后可能继续领取，修复于 `77aa9e2`；P2 退出时未检查 release 失败，修复于 `8f54e04`。逐项复核并分别运行相关测试；无拒绝项。
- 最终 `.venv/bin/python -m pytest`：392 passed，1 条 Starlette/httpx 依赖弃用警告；`.venv/bin/ruff check .`、`.venv/bin/python -m compileall -q src tests`、`git diff --check` 通过。测试中的并发与服务均为本地模拟，不证明真实多进程部署或外部平台。
- schema 切换证据限于本地新建库的 `PRAGMA user_version = 17` 和 schema 16 拒绝测试。本次没有对真实部署数据库切换或执行一致性快照；既有 A1 canary 证据仍在 `docs/evidence/`。05 应在切换前保存需保留的现场证据与一致性数据库快照，并完成新 JiHuLab/OpenCode、通知渠道和崩溃恢复验收。
