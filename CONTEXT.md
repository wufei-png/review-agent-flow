# Review Agent Flow

本上下文定义 GitLab Project 中的人类评审、AI Agent 评审和可选 auto-fix 工作流。

## Scope

**Project**: GitLab 中拥有 Merge Request 和可选策略文件的仓库边界。首版不引入 Group 默认策略。

**Review Policy**: 描述触发条件、Reviewer、AI Agent、auto-fix 和通知渠道的 Project 级规则集合。

**Policy File**: 可选的 `.gitlab/reviewflow.yml`。缺失时使用安全默认值，存在时覆盖默认值。

## Review

**Reviewer**: 显式配置的 GitLab username，仅作为 Notify-only 人审通知对象；Approval Requirement 不属于 v0.1。

**AI Review**: Agent 针对一个 MR 的一个 SHA 执行评审并直接向 GitLab 提交 Note。首版默认关闭，不批准也不阻塞合并。

**Review Run**: 对一个 MR 的一个 SHA 执行的一次评审尝试。它拥有独立的 Run Lifecycle；用户是否已收到评审由 Review Outcome 表达。

**Review Key**: 一个 Project/MR/精确 SHA/评审类型组合的稳定逻辑身份。它在本地 Store 重建后仍可用于识别已有的 Review Artifact。
_Avoid_: Run ID, Attempt ID

**Run Identifier**: 本地持久化 Review Run 的追踪身份。它不作为外部 Review Artifact 的幂等身份。
_Avoid_: Review Key, Effect Marker

**Run Lifecycle**: Review Run 从排队、执行到终止的编排状态。终止后保持稳定，不把外部 Review Artifact 的可见性编码为生命周期状态。
_Avoid_: Review Outcome, Delivery Status

**Review Outcome**: 从 Run Lifecycle、用户可见 Effect Receipts 和 Notification Delivery 记录派生的当前用户结果；它可随后续投递与对账而变化。
_Avoid_: Run Status, Process Exit Status

**Requested Output**: Review Run 入队时从 Policy Snapshot 持久化的不可变用户可见输出请求。它记录 Project 想要什么，但不证明本次 Run 具备产生该输出的资格。
_Avoid_: Output Expectation, Observed Artifact

**Output Expectation**: Effective Run Decision 持久化的本次实际用户可见输出预期。它在首个用户可见副作用或 Agent 执行前形成，并区分“无需输出”和“预期输出未能产生”。
_Avoid_: Effect Receipt, Observed Artifact

**Effective Run Decision**: ReviewFlow 结合 Policy Snapshot、Run 类型、当前 MR/SHA、抑制关系和已解析接收者后得到的本次实际动作集合。Output Expectations 由它产生，而不是直接从原始 Policy 推断。
_Avoid_: Raw Policy, Webhook Payload

**Effect Receipt**: 连接 Review Run、预期外部副作用和平台已观察身份/状态的持久记录。它用于重放与对账，不代表外部平台操作具备 exactly-once 语义。

**Notification Delivery**: 一个通知渠道面向一组接收者的持久化投递结果。它独立于 Run Lifecycle，但参与 Review Outcome。
_Avoid_: Review Run Status, Synchronous Notification

**Auto-fix Merge Request**: Agent 创建的独立 MR，目标为原 MR 的 source branch。它必须由 Auto-fix Intent 授权并携带可验证的 Auto-fix 身份；标题或分支本身不能建立可信关系。

**Auto-fix Intent**: ReviewFlow 在允许 Agent 创建 Auto-fix Merge Request 前持久化的服务端授权。正常执行中只有 Intent 或由它建立的关系能够赋予 Auto-fix 身份。
_Avoid_: Branch Convention, Title Marker

**Auto-fix Recovery**: Store 丢失后，由 Operator 显式发起并审计的 Auto-fix 关系重建。它不是 Webhook 或后台 reconciliation 的自动分类路径。
_Avoid_: Automatic Adoption, Marker-only Recovery

## Execution

**Ingress Authentication**: A ReviewFlow deployment may accept GitLab webhook
events only when its runtime authentication secret is configured and matches.
An unset secret makes the deployment unusable for webhook intake rather than
turning webhook requests into an anonymous mode.

**Readiness**: Process liveness is separate from operational readiness. A
`/healthz` response means the API process is alive. A `/readyz` response checks
required configuration, the bound Project identity, and SQLite read/write
availability without making a live GitLab request by default. A live provider
probe is explicit and separate from the default readiness gate.

**Status Query**: Review Run status is read-only and Project-bound. It exposes
status, attempts, redacted failures, effect-receipt summaries and safe
diagnostics, but never lease claim tokens, GitLab tokens or notification
credentials. Unknown or out-of-bound Runs return the same not-found response.
The HTTP status routes and explicit live-probe route require
`X-ReviewFlow-Internal-Token` equal to the configured `GITLAB_WEBHOOK_SECRET`;
missing or incorrect tokens fail with `401` before Store or provider access.
`/health`, `/healthz`, and `/readyz` remain public.

**Operational Logs**: Webhook and Worker key paths emit bounded JSON events
with correlation, Run, Project, MR, event and status fields. Raw webhook
payloads and credentials are not default log inputs; diagnostics use the
existing redaction helper.

**AI Provider**: OpenAI-compatible API。核心只定义接口，部署文档推荐 LiteLLM，不内置模型厂商 SDK。

**Agent Backend**: Operator 管理的 Agent 执行能力，将稳定名称映射为本地 CLI 或 Agent 服务及其运行约束。Project Policy 只能选择被授权的 Backend，不能定义宿主机命令。
_Avoid_: Project Command, AI Provider

**Backend Profile**: Agent Backend 中由 Operator 命名和授权的一组运行约束，供 Project Policy 选择。
_Avoid_: Command Template, Project Script

**Review Agent Skill**: 指导 Agent 读取上下文、提交 Note、创建 Auto-fix MR 的默认能力说明。Project 可用 `.gitlab/review-agent/SKILL.md` 覆盖。

**Trusted Project Deployment**: Operator 接受 Project Policy、被接纳的 MR 内容和配置的 Agent 会共同影响一个具备 Review Bot 能力的内部部署。它不承诺安全地无人值守处理任意外部或不可信贡献。
_Avoid_: Untrusted-MR Sandbox, Multi-tenant Service

**Trusted Execution Environment**: Operator 接受 Agent 在读取仓库和 MR 内容后以 Review Bot 能力行动的执行边界。它不是针对任意不可信贡献的隔离或沙箱边界。
_Avoid_: Secure Sandbox, Untrusted Execution

**Deployment boundary**: v0.1 一个 API/Worker 进程只服务一个 GitLab Project；Webhook secret 和 Project ID 必须配置。Repository 对象缺失由显式 `ensure_sha` 在仓库锁内限时 fetch 后再校验，Worktree manager 不隐藏 fetch。

**Runtime Boundary**: ReviewFlow 中 Project/MR/SHA 评审编排以及 Effect Receipt、Worker Lease 语义所拥有的独立版本与依赖边界。相似的工程辅助能力不因此自动成为本项目的运行时依赖。
_Avoid_: Shared Runtime, Common Runtime

**Thin Protocol**: 跨项目用于表达最小、可独立演化语义的文档层约定；它不包含任一项目的领域模型、安全默认值或 Python 运行时实现。
_Avoid_: Shared Runtime Library, Shared Domain Model
