# review-agent-flow

GitLab 人类评审与 AI Agent 协同的开源工作流编排器。

当前项目从零实现，不复制内部业务代码。它接收 GitLab Merge Request Webhook，在独立 Worker 中为精确 SHA 创建 Git worktree，调用本地 Agent CLI，让 Agent 直接向 GitLab 提交 Note；必要时 Agent 创建独立的 Auto-fix MR，由原 MR 创建者决定是否合并。

## Current status

当前首版已具备：

- Python package and `review-agent-flow init` policy template command
- Project policy loading with safe defaults
- SQLite idempotency store, Auto-fix relationship tracking and database-backed Review Run queue
- Generic local CLI Agent Runner
- Git worktree lifecycle helper
- GitLab event normalization and a `python-gitlab` adapter boundary
- FastAPI health and GitLab Webhook endpoint with token validation
- Feishu and WeCom webhook notifier adapters
- API + Worker orchestration, Pipeline Gate polling and auto-fix loop suppression
- Default review-agent Skill and architecture ADRs

真实 GitLab、聊天机器人和 Agent CLI 的端到端验证需要部署者提供对应凭据与运行环境。Agent 负责直接创建 Auto-fix MR，核心 Worker 负责识别并记录其关系。

## Design

- Project 是唯一策略边界；可选 `.gitlab/reviewflow.yml` 覆盖服务默认值。
- Reviewer 通过 GitLab username 显式配置，首版不自动解析 CODEOWNERS。
- AI Review 和 auto-fix 默认关闭，独立开关。
- 每个 Review Run 使用精确 SHA 的独立 Git worktree。
- Agent 不需要返回 JSON，可以直接写 GitLab Note。
- Auto-fix 创建独立 MR，分支格式为 `autofix/MR-1/run-1`，标题以 `[ReviewFlow Auto-Fix]` 开头，只通知原 MR 创建者。
- Auto-fix MR 不触发 AI Review，但正常运行 CI；合并回原 source branch 后只跳过对应 SHA 的自动 AI Review。
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
export REVIEW_AGENT_FLOW_REPOSITORY=/workspace/project
review-agent-flow worker
```

单次领取任务可用于调试：

```bash
review-agent-flow worker --once
```

Docker 镜像默认启动 Webhook API；Worker 建议作为同镜像的独立进程或独立容器运行，并共享数据库、Project checkout 和 worktree 根目录。

## License

Apache-2.0
