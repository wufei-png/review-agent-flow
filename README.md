# review-agent-flow

GitLab 人类评审与 AI Agent 协同的开源工作流编排器。

当前项目从零实现，不复制内部业务代码。它接收 GitLab Merge Request Webhook，在独立 Worker 中为精确 SHA 创建 Git worktree，调用本地 Agent CLI，让 Agent 直接向 GitLab 提交 Note；必要时 Agent 创建独立的 Auto-fix MR，由原 MR 创建者决定是否合并。

## Current status

早期骨架已具备：

- Python package and `review-agent-flow init` policy template command
- Project policy loading with safe defaults
- SQLite idempotency store and database-backed Review Run queue primitive
- Generic local CLI Agent Runner
- Git worktree lifecycle helper
- FastAPI health and GitLab webhook endpoint
- Default review-agent Skill and architecture ADRs

GitLab API orchestration、飞书/企业微信发送器、Worker 主循环和 Auto-fix MR 创建仍在后续迭代中。

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
python -m venv .venv
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

## License

Apache-2.0
