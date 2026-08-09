# Domain and consolidated decisions

## Product shape

`review-agent-flow` 是面向单一 GitLab Project 的人类评审与 AI Review 编排器。
它为父 MR 的精确 SHA 运行 Agent，但由 ReviewFlow 持久化并发布
Note、Auto-fix MR 和通知。

它不是：

- GitLab Group 级策略中心或多租户 Agent 沙箱
- AI 模型厂商 SDK 聚合层
- 直接替用户批准、阻塞或合并 MR 的 AI Gate
- 允许 Agent 持有 GitLab token 或直接发布的能力转发器

## Decisions

| Area | Decision |
| --- | --- |
| ReviewKey | `Project + 父 MR IID + 精确 SHA`；不含 `kind` |
| Policy | Project Policy v3 只表达请求；Operator 配置授权 Backend/Profile 和 Publisher |
| Review mode | v0.1 只有 Notify-only；不写 GitLab Approval |
| Human/AI | 独立开关；AI 和 Auto-fix 默认关闭 |
| Agent result | 受控 Result Path 中的严格 v1 JSON；stdout/stderr 只是诊断 |
| Agent authority | 无 GitLab 发布凭据；不 commit、push、创建 MR 或 Note |
| Note | ReviewFlow 持久化请求并发布；marker 由 ReviewKey 和 `effect=note` 组成 |
| Auto-fix | 不可变 Intent generation/nonce + Diff Envelope + verification + Publication Plan |
| Auto-fix publication | GitLab Commits API，`start_sha=ReviewKey.sha`，`force=false` |
| Auto-fix default | `initial_readiness=draft`，`ci_trigger=manual`；`ready/manual` 非法 |
| Relation | 只能由本地 Intent 与 Publisher 结果建立；Webhook 不能自动接管 |
| Reconciliation | 处理不确定调用、崩溃、状态变化和显式恢复；非正常 Publisher |
| Recovery | Store 丢失后只有带理由和审计的本地 Operator CLI |
| Outcome | Run Lifecycle 稳定；Review Outcome 随用户可见效果与通知变化 |
| Trust | linked worktree 是 Trusted Workspace，非 sandbox；隔离由 Backend Profile/Operator 负责 |
| Runtime | 一个受监管 Worker OS 进程，内部三个独立循环，SQLite claim/lease |

## Explicit non-goals for v0.1

- Group inheritance、CODEOWNERS 自动推导、多 Project 进程
- Enforce/Approval Requirement、AI approval、AI blocking 或直接 merge
- Agent 凭据隔离的通用 OS sandbox 实现
- fork/cross-Project Auto-fix publication
- 旧 Policy v1/v2、Run-ID marker 或 SQLite schema 迁移
- 独立 Dispatcher/Reconciler OS 进程、Redis 或 Postgres
