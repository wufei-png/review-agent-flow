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
- Separate `/healthz` liveness, `/readyz` SQLite/config readiness, and explicit live GitLab probe
- Project-bound Review Run status through HTTP and `review-agent-flow status`
- Bounded JSON operational logs with redacted diagnostics and no raw webhook payloads
- Feishu and WeCom webhook notifier adapters
- API + Worker orchestration, Pipeline Gate polling and auto-fix loop suppression
- Default review-agent Skill and architecture ADRs

真实 GitLab、聊天机器人和 Agent CLI 的端到端验证需要部署者提供对应凭据与运行环境。Agent 负责直接创建 Auto-fix MR，核心 Worker 负责识别并记录其关系。

## Design

- Project 是唯一策略边界；可选 `.gitlab/reviewflow.yml` 覆盖服务默认值。
- Reviewer 通过 GitLab username 显式配置，首版不自动解析 CODEOWNERS。
- AI Review 和 auto-fix 默认关闭，独立开关。
- 每个 Review Run 使用精确 SHA 的独立 Git worktree。
- Agent 不需要返回 JSON，可以直接写 GitLab Note；`status` 和 `worker --once` CLI 命令输出 JSON 运行状态，不构成 Agent JSON 报告，仓库也没有独立的 JSON render 命令。
- Auto-fix 创建独立 MR，分支格式为 `autofix/MR-1/run-1`，标题以 `[ReviewFlow Auto-Fix]` 开头，只通知原 MR 创建者。
- Auto-fix MR 不触发 AI Review，但正常运行 CI；合并回原 source branch 后只跳过对应 SHA 的自动 AI Review。
- 通知按渠道写入 SQLite outbox；单渠道失败不影响 Review Run，最多重试 3 次并保留失败记录。
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
export GITLAB_WEBHOOK_SECRET='replace-with-a-random-secret'
export REVIEW_AGENT_FLOW_PROJECT_ID=42
uvicorn review_agent_flow.web:create_app --factory --host 127.0.0.1 --port 8080
```

`GITLAB_WEBHOOK_SECRET` 和 `REVIEW_AGENT_FLOW_PROJECT_ID` 都是 API 启动所需的部署配置。Secret 缺失或为空时服务直接启动失败；请求 token 错误返回 `401`，其他 Project 返回 `403`，不会进入队列。测试或显式嵌入式调用可以向 `create_app(..., webhook_secret=..., project_id=...)` 传值，但不存在匿名 Webhook 模式。

健康检查和状态查询：

```bash
curl http://127.0.0.1:8080/healthz
curl -i http://127.0.0.1:8080/readyz
curl -i -H "X-ReviewFlow-Internal-Token: $GITLAB_WEBHOOK_SECRET" http://127.0.0.1:8080/probes/gitlab  # only an explicitly wired live probe
review-agent-flow status --project-id 42 --db review-agent-flow.sqlite3 --run-id 1
```

`/readyz` 默认不访问 GitLab；未注入 live probe 时，带正确内部 token 的 `/probes/gitlab` 返回 `503 not_ready`，不会假装 provider 已就绪。新的 status/probe endpoint 要求 `X-ReviewFlow-Internal-Token`，其值复用已配置的 `GITLAB_WEBHOOK_SECRET`；缺失或错误 token 返回 `401`，不能触发 Store 查询或 live probe。`/health`、`/healthz`、`/readyz` 保持公开。

Worker 使用同一个 SQLite 数据库：

```bash
export GITLAB_URL=https://gitlab.example.com
export GITLAB_TOKEN=glpat-...
export REVIEW_AGENT_FLOW_PROJECT_ID=42
export REVIEW_AGENT_FLOW_REPOSITORY=/workspace/project
review-agent-flow worker
```

Worker 也必须绑定同一个 `REVIEW_AGENT_FLOW_PROJECT_ID`；可以用 `--project-id 42` 显式传入，CLI 参数优先于环境变量。它只领取该 Project 的任务，并在执行前确认 MR 仍处于打开状态且当前 head SHA 与 Run 完全一致。

单次领取任务可用于调试：

```bash
review-agent-flow worker --once
```

Docker 镜像默认启动 Webhook API；运行容器时必须注入 `GITLAB_WEBHOOK_SECRET` 和 `REVIEW_AGENT_FLOW_PROJECT_ID`。Worker 建议作为同镜像的独立进程或独立容器运行，并共享数据库、Project checkout 和 worktree 根目录。

本地 contract test 入口为 `python -m pytest tests/test_p2_contract.py`；它不访问 GitLab，也不构造 live sandbox 结果。真实 GitLab sandbox 仍需按部署者的受控环境另行验证。

## License

Apache-2.0
