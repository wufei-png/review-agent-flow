# 01 · 通知可领取与结果语义

> 历史归档：本地实施已完成；现场验收由[05 当前计划](../../05-deployment-canary.md)跟踪。本文保留当时的任务与检查记录，当前状态见[执行队列](../../pilot-readiness-queue.md)。

状态：已完成本地实现与检查；现场证据待 05。优先级：P0。前置条件：无。规划代码基线：`f407d0b`；实施前已核对当前代码。

## 交付结果

Auto-fix Relation 建立和 Operator 人工 resend 产生的每个通知 outbox 行都立即具备领取时间，dispatcher 能按现有有限重试规则处理。过期 Review Run 所有者不能再改变该 Run。旧 SHA 的输出被安全抑制时，Review Outcome 不再把它当成实际送达。状态说明清楚区分服务端接受与收件人可见。

## 实施前契约与修改入口（`f407d0b` 基线）

- `src/review_agent_flow/store.py` 的 `complete_auto_fix_publication()`、`create_auto_fix_notification_resend()` 插入 outbox 时省略 `next_attempt_at`；`_claim_notification_row()` 只领取 `next_attempt_at <= now` 的 `pending` 行。普通 `enqueue_notifications()` 已写入该时间。
- `notification_dispatcher.py`、`notifications.py` 和 `docs/adr/0010-notification-outbox.md` 已实现并定义至少一次投递。`delivered` 是现存持久状态和 Review Outcome 输入；服务端成功响应不能证明收件人可见，也不能排除崩溃后的重复消息。
- `store.py::retry_or_fail()` 接收 `claim_token`，却没有同时要求有效 `lease_until`。`project_review_outcome()` 把 Note/Auto-fix 的 `superseded` 计为 `delivered`。
- 相关回归入口：`tests/test_autofix_publisher.py`、`test_autofix_recovery.py`、`test_notification_dispatcher.py`、`test_store.py`，以及 `docs/spec/05-integrations.md`、`07-implementation-status.md`。保持 AgentTask、RESULT_PATH、Agent Skill 及发布权限边界原样。

## 阶段

1. **恢复通知领取** — 两条 Auto-fix outbox 写路径在同一事务中写 `next_attempt_at=created_at`；重复调用保持原行及其领取时间。增加 publication→dispatcher、resend→dispatcher 的端到端 Store/dispatcher 回归，覆盖多 channel 和无重复请求。**依赖：** 无。**验收：** 新行没有 `NULL next_attempt_at`，到期即可领取；现有失败重试与人工 resend generation 不变；针对性 pytest 通过。一个本地提交。
2. **封住过期 Run 所有者** — 对 `retry_or_fail()` 的 claim-token 路径同时核对 token 和未过期租约，并核对调用方与其他 Run owner 写路径的一致性。**依赖：** 阶段 1。**验收：** 持旧 token 但租约已过期时返回 false 且不写失败记录；有效 owner 可正常重试或失败；相关 worker/store 测试通过。一个本地提交。
3. **修正投影与通知措辞** — `superseded` 表示预期安全退出，不表示效果送达；全被 supersede 的旧 ReviewKey 投影为 `no_effect`，已发布 Note 与可选 Auto-fix supersede 的组合仍按实际已交付效果计算。保持现有顶层六态与 outbox `delivered` 存储名，在只读状态说明、规格及必要的用户文档中说明该通知状态表示“服务端已接受，可能重复”；可见性仅由 canary 人工记录。**依赖：** 阶段 2。**验收：** 全 superseded、部分已交付、已交付加失败的投影测试；状态/文档不声称收件人可见；全套检查通过。一个本地提交。

## 验证与交接

先跑相关测试：`pytest -q tests/test_store.py tests/test_notification_dispatcher.py tests/test_autofix_publisher.py tests/test_autofix_recovery.py tests/test_worker.py`。会话结束跑 `ruff check .`、`pytest`、`python -m compileall -q src tests`、`git diff --check`。本地模拟不能证明飞书/企微实际送达。

旧 canary 数据库若已有 `pending` 且 `next_attempt_at IS NULL` 的行，新代码不会自动修复旧行；实施时给出只读盘点和经 Operator 审核的修复或重建步骤，不静默改写历史通知。完成记录写入[执行队列](../../pilot-readiness-queue.md)，并在本文记录提交和检查结果。

## 旧 canary 通知处置

本次未自动改写历史 outbox。仓库内和 quickstart 文档所示的本机默认路径均未发现 canary 数据库；Operator 在实际部署主机上先确认数据库路径、停止 API/Worker，并保存一致性 SQLite 快照及对应的 Plan backup。随后只读盘点：

```bash
sqlite3 -readonly "$REVIEW_AGENT_FLOW_DB" \
  "SELECT id, correlation_id, channel_id, status, created_at, next_attempt_at
   FROM notification_outbox
   WHERE status = 'pending' AND next_attempt_at IS NULL ORDER BY id;"
```

Operator 对每个命中行核对 Notification Request generation、channel 授权、接收者、已有服务端接受证据及重复发送风险。确认应继续投递时，在保留快照并停机的条件下，仅对审核通过的具体数字 ID 以事务把 `next_attempt_at` 设为原 `created_at`，核对变更行数与复查查询后再恢复 Worker；若历史投递身份无法确认，则保留原行与证据，按显式 audited resend generation 或试运行前重建流程处理。不要以本地状态 `delivered` 推断收件人可见。

## 完成记录

- 2026-09-20，阶段 1：`f85aaa8`。两条 Auto-fix 写路径的 outbox 行在同一事务获得 `next_attempt_at=created_at`；publication/resend 多 channel 经 Store/dispatcher 领取，重复请求不新增 generation。
- 阶段 2：`b3e4381`。`retry_or_fail()` 同时核对 claim token 与有效租约；过期或 NULL 租约不写 Run、失败记录或 Effect Receipt，由下一次 claim 回收。
- 阶段 3：`bb67f5c`。Note/Auto-fix `superseded` 不再计为送达；全被抑制为 `no_effect`，已发布 Note 加被抑制 Auto-fix 仍按已观察效果投影。领域词汇、ADR、规格及只读状态说明明确通知 `delivered` 只表示服务端接受，可能重复。
- 独立只读 review 提出一项 P2：可重试异常遇过期租约时 Worker 错报终止失败。核实后以 `7e5e6ac` 修正为 `deferred` 并新增回归；无其他发现。
- 最终 `.venv/bin/python -m pytest`：380 passed，1 条 Starlette/httpx 测试依赖弃用警告；`.venv/bin/ruff check .`、`.venv/bin/python -m compileall -q src tests`、`git diff --check` 通过。未运行真实 GitLab、Agent provider、飞书/企微发送、收件人可见或崩溃恢复演练；既有 A1 为部分历史证据，不替代新 canary。
