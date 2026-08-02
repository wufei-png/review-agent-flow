# Review Agent Flow

本上下文定义 GitLab Project 中的人类评审、AI Agent 评审和可选 auto-fix 工作流。

## Scope

**Project**: GitLab 中拥有 Merge Request 和可选策略文件的仓库边界。首版不引入 Group 默认策略。

**Review Policy**: 描述触发条件、Reviewer、AI Agent、auto-fix 和通知渠道的 Project 级规则集合。

**Policy File**: 可选的 `.gitlab/reviewflow.yml`。缺失时使用安全默认值，存在时覆盖默认值。

## Review

**Reviewer**: 显式配置的 GitLab username。Reviewer 与 Approval Requirement 分开建模。

**AI Review**: Agent 针对一个 MR 的一个 SHA 执行评审并直接向 GitLab 提交 Note。首版默认关闭，不批准也不阻塞合并。

**Review Run**: 对一个 MR 的一个 SHA 执行的一次评审尝试。每个 Run 使用独立 Git worktree，并按 Project/MR/SHA 幂等。

**Effect Receipt**: 连接 Review Run、预期外部副作用和平台已观察身份/状态的持久记录。它用于重放与对账，不代表外部平台操作具备 exactly-once 语义。

**Auto-fix Merge Request**: Agent 创建的独立 MR，目标为原 MR 的 source branch，使用 `[ReviewFlow Auto-Fix]` 标题前缀和 `autofix/MR-<iid>/run-<run-id>` 分支，只通知原 MR 创建者。

## Execution

**Ingress Authentication**: A ReviewFlow deployment may accept GitLab webhook
events only when its runtime authentication secret is configured and matches.
An unset secret makes the deployment unusable for webhook intake rather than
turning webhook requests into an anonymous mode.

**AI Provider**: OpenAI-compatible API。核心只定义接口，部署文档推荐 LiteLLM，不内置模型厂商 SDK。

**Agent Backend**: 本地 CLI 或本地 Agent 服务。它与 AI Provider 分离。

**Review Agent Skill**: 指导 Agent 读取上下文、提交 Note、创建 Auto-fix MR 的默认能力说明。Project 可用 `.gitlab/review-agent/SKILL.md` 覆盖。

**Trusted Execution Environment**: 部署者信任 Worker 主机和 Agent 能力的运行边界。核心不默认裁剪 Agent 工具、网络或凭据权限。
