# 03 · Operator 诊断

> 历史归档：本地实施已完成；现场验收由[05 当前计划](../../05-deployment-canary.md)跟踪。本文保留当时的任务与检查记录，当前状态见[执行队列](../../pilot-readiness-queue.md)。

状态：本地完成；现场证据待 05。优先级：P1。前置条件：[02 · 终态保留与 Worker 所有权](02-retention-worker-ownership.md) 完成。

## 交付结果

Operator 通过本地只读 CLI 就能判断 Worker 是否持有运行时租约、哪个 Run 或外部写阶段停滞、当前该等待自动重试/对账还是进行人工核查。汇总信息可用于初期告警，不需要读取数据库内部表，也不把凭据或 claim token 带到诊断输出。

## 当前契约与修改入口

- `store.py::get_project_bound_review_run_status()` 与 CLI `status` 已提供 Project 绑定的只读 Run 摘要；HTTP status 有独立鉴权。保留现有项目隔离、字段白名单、错误脱敏与本地查询原则。
- `store.py` 中 Note request、Auto-fix Plan、Relation、outbox 已持久化 state、write phase、lease、next attempt 和外部 identity。任务 02 已提供 Worker owner/heartbeat；不需要为每个诊断字段再复制一份状态。
- `CONTEXT.md`、`docs/spec/06-runtime-and-storage.md`、`07-implementation-status.md` 和 `tests/test_cli.py`、`test_store.py`、`test_web.py` 是状态契约入口。

## 阶段

1. **扩展单 Run 状态** — 在现有 Project-bound 状态上增补 Note 状态及写入不确定性、Auto-fix Plan 状态/write phase/已确认 commit 与 MR 身份、Relation 状态或偏差、通知 generation/channel/status/attempt/下次时间/租约有效性；从持久事实推导有限的 `next_action` 提示，不发起远端查询或状态变更。通知 `delivered` 对外解释为“服务端已接受，可能重复”，不推断群内可见。**依赖：** 02 完成。**验收：** pending、reconciliation、blocked、已发布及通知重试案例能解释当前状态；跨 Project 请求仍返回 not-found；嵌套结构不包含 token、webhook、原始敏感 payload；CLI/API 相关测试通过。一个本地提交。
2. **增加本地运维汇总** — 新 `ops-status` 只读 CLI 按绑定 Project 汇总 Worker owner/heartbeat、Run backlog 数量与最老等待时间、过期 lease、Note blocked/uncertain、Auto-fix reconciliation/blocked、通知 pending/failed 与最老等待时间、Relation divergence 及 retention 最近执行结果（若已有可靠来源）。缺少可靠持久来源的指标明确为 unavailable，不合成“健康”值。日志保留稳定 correlation 与脱敏。**依赖：** 阶段 1。**验收：** 空库、运行中、过期租约及失败状态汇总正确；不可用字段不误报为零；不会产生网络调用、状态修改或秘密字段；完整检查通过。一个本地提交。

## 验证与证据范围

逐阶段跑 `pytest -q tests/test_store.py tests/test_cli.py tests/test_web.py tests/test_worker.py`；结束跑 `ruff check .`、`pytest`、`python -m compileall -q src tests`、`git diff --check`。状态查询只证明 Store 内的已知事实；远端 MR、Note 或通知可见性仍需独立核查。部署告警可先消费 CLI 汇总；独立 `/metrics` 服务及固定流量阈值留待实际负载确定。

完成记录写入[执行队列](../../pilot-readiness-queue.md)，并在本文记录提交和检查结果。

完成记录：2026-09-20。阶段 1 `cb42a79` 增加 Project-bound Note、Plan、Relation、通知状态与有限行动提示；阶段 2 `fd4ca73` 增加本地只读 `ops-status`。独立只读 review 的两项 P2 均核实并在 `0c6484a` 修复：已发布 Note 不再被标作待对账，有效租约中的 Auto-fix 外部写入不再误计为对账。无拒绝项。

`.venv/bin/python -m pytest -q tests/test_store.py tests/test_cli.py tests/test_web.py tests/test_worker.py`、`.venv/bin/ruff check .`、`.venv/bin/python -m compileall -q src tests`、CLI `--help`、`git diff --check` 通过；完整 `.venv/bin/python -m pytest` 为 398 passed、1 条 Starlette/httpx 测试依赖弃用警告。所有集成均为本地或模拟，未取得远端 Note/MR、通知收件人可见性、真实 Worker 运行或告警阈值证据。
