# review-agent-flow

GitLab 人类评审与 AI Agent 协同的开源工作流编排器。

当前项目从零实现，不复制内部业务代码。它接收 GitLab Merge Request Webhook，在独立 Worker 中为精确 SHA 创建 Git worktree，调用本地 Agent CLI，让 Agent 直接向 GitLab 提交 Note；必要时 Agent 创建独立的 Auto-fix MR，由原 MR 创建者决定是否合并。

## Current status

当前首版已具备：

- Python package and `review-agent-flow init` policy template command
- Project policy loading with safe defaults
- SQLite idempotency store, Auto-fix relationship tracking, notification outbox and database-backed Review Run queue
- Generic local CLI Agent Runner
- Git worktree lifecycle helper
- GitLab event normalization and a `python-gitlab` adapter boundary
- FastAPI health and GitLab Webhook endpoint with token validation
- Feishu and WeCom webhook notifier adapters
- API + Worker orchestration, Pipeline Gate polling and auto-fix loop suppression
- Single-Project Webhook binding, required secret validation and explicit `ensure_sha` fetch/recheck
- Server-owned Auto-fix intents, stable notification channel IDs and independently retried worktree cleanup
- Default review-agent Skill and architecture ADRs

真实 GitLab、聊天机器人和 Agent CLI 的端到端验证需要部署者提供对应凭据与运行环境。Agent 负责直接创建 Auto-fix MR，核心 Worker 负责识别并记录其关系。

## Design

- Project 是唯一策略边界；可选 `.gitlab/reviewflow.yml` 覆盖服务默认值。
- v0.1 是 Notify-only；Reviewer 通过 GitLab username 显式配置，首版不实现 Approval Requirement、Enforce 或 CODEOWNERS 自动解析。
- 一个 API/Worker 进程只绑定一个 Project；Webhook secret 和 Project ID 缺失时启动失败，事件 Project ID 必须严格匹配。
- AI Review 和 auto-fix 默认关闭，独立开关。
- 每个 Review Run 使用精确 SHA 的独立 Git worktree。
- Agent 不需要返回 JSON，可以直接写 GitLab Note。
- Auto-fix 由服务端 intent 绑定身份，创建独立 MR，分支格式为 `autofix/MR-1/run-1`，目标为原 MR source branch，标题以 `[ReviewFlow Auto-Fix]` 开头，只通知原 MR 创建者。
- Auto-fix MR 不触发 AI Review，但正常运行 CI；合并回原 source branch 后只跳过对应 SHA 的自动 AI Review。
- 通知按稳定 channel ID 写入 SQLite outbox；单渠道失败不影响 Review Run，最多重试 3 次并保留失败记录。HTTP 2xx 还必须通过 Feishu/WeCom 业务码校验。
- Run 语义为至少一次执行，不承诺绝对 exactly-once；Note 使用 Run ID/SHA 标记，Auto-fix 和通知使用稳定协调键。
- 首版使用可信执行环境，不在核心层裁剪 Agent 权限。
- 模型层只定义 OpenAI-compatible Provider 接口；部署文档推荐使用 [LiteLLM](https://docs.litellm.ai/)。
- Agent 层支持通用本地 CLI，并为 [OpenCode Web](https://dev.opencode.ai/docs/web/)、[OpenCode Server](https://dev.opencode.ai/docs/server/) 和其他 CLI 预留入口。

## Quick start

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'

review-agent-flow init
pytest
```

生成的 `.gitlab/reviewflow.yml` 默认关闭 AI 和 auto-fix。启用后，`ai.command` 可以配置为 `opencode`、`claude`、`codex` 或其他本地 Agent CLI。

启动 Webhook API：

```bash
uvicorn review_agent_flow.web:create_app --factory --host 127.0.0.1 --port 8080
```

Worker 使用同一个 SQLite 数据库：

```bash
export GITLAB_URL=https://gitlab.example.com
export GITLAB_TOKEN=glpat-...
export GITLAB_REVIEW_BOT_USERNAME=review-flow-bot
export REVIEW_AGENT_FLOW_PROJECT_ID=123
export GITLAB_WEBHOOK_SECRET=webhook-secret
export REVIEW_AGENT_FLOW_REPOSITORY=/workspace/project
review-agent-flow worker
```

单次领取任务可用于调试：

```bash
review-agent-flow worker --once
```

Docker 镜像默认启动 Webhook API；API 需要 `GITLAB_WEBHOOK_SECRET`、`REVIEW_AGENT_FLOW_PROJECT_ID`，并可用 `REVIEW_AGENT_FLOW_PIPELINE_GATE=true|false` 配置 Pipeline Hook 唤醒行为。Worker 建议作为同镜像的独立进程或独立容器运行，并共享数据库、Project checkout 和 worktree 根目录。`GITLAB_REVIEW_BOT_USERNAME` 用于只信任 Review Bot 的 Note marker；未配置时会安全地重复执行，而不会把任意用户 Note 当作完成证明。Repository 必须可写、非 shallow；Worker 在创建 worktree 前会对缺失 SHA 加仓库锁并限时 fetch/recheck。

当前 Policy schema 为 v2（通知 channel 必须有稳定 ID），SQLite 为 schema v2 的新结构；没有历史配置或数据库迁移契约。升级前应显式更新旧 Policy，并备份后重建旧数据库，避免旧的 channel index 被错误映射。

## License

Apache-2.0
