# Review Agent Flow

本上下文定义 GitLab Project 中的人类评审、AI Agent 评审和可选 auto-fix 工作流。

## Scope

**Project**: GitLab 中拥有 Merge Request 和可选策略文件的仓库边界。首版不引入 Group 默认策略。

**Review Policy**: 描述触发条件、Reviewer、AI Agent、auto-fix 和通知渠道的 Project 级规则集合。

**Policy File**: 可选的 `.gitlab/reviewflow.yml`。缺失时使用安全默认值，存在时覆盖默认值。

## Review

**Reviewer**: 显式配置的 GitLab username，仅作为 Notify-only 人审通知对象；Approval Requirement 不属于 v0.1。

**AI Review**: Agent 针对一个 MR 的一个 SHA 执行评审并直接向 GitLab 提交 Note。首版默认关闭，不批准也不阻塞合并。

**Review Run**: 对一个 MR 的一个 SHA 执行的一次评审尝试。每个 Run 使用独立 Git worktree，并按 Project/MR/SHA/类型幂等；租约过期允许至少一次重试，外部副作用通过稳定标记和关系协调。

**Auto-fix Merge Request**: Agent 创建的独立 MR，目标为原 MR 的 source branch。Worker 先登记服务端 intent，再向 Agent 提供 `[ReviewFlow Auto-Fix]` 标题、`autofix/MR-<iid>/run-<run-id>` 分支和唯一 Run/SHA marker；Agent 必须把 marker 写入 MR description，只通知原 MR 创建者；标题/分支本身不是可信身份。

## Execution

**AI Provider**: OpenAI-compatible API。核心只定义接口，部署文档推荐 LiteLLM，不内置模型厂商 SDK。

**Agent Backend**: 本地 CLI 或本地 Agent 服务。它与 AI Provider 分离。

**Review Agent Skill**: 指导 Agent 读取上下文、提交 Note、创建 Auto-fix MR 的默认能力说明。Project 可用 `.gitlab/review-agent/SKILL.md` 覆盖。

**Trusted Execution Environment**: 部署者信任 Worker 主机和 Agent 能力的运行边界。核心不默认裁剪 Agent 工具、网络或凭据权限。

**Deployment boundary**: v0.1 一个 API/Worker 进程只服务一个 GitLab Project；Webhook secret 和 Project ID 必须配置。Repository 对象缺失由显式 `ensure_sha` 在仓库锁内限时 fetch 后再校验，Worktree manager 不隐藏 fetch。
