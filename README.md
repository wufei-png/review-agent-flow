# review-agent-flow

GitLab 人类评审与 AI Agent 协同的开源工作流编排器。

ReviewFlow 接收单一 GitLab Project 的 Merge Request Webhook，以
`Project + 父 MR IID + 精确 SHA` 建立 ReviewKey，在该 SHA 的 Trusted
Workspace 中调用 Operator 授权的 Agent Backend。Agent 只产生评审结果和
可选修复 diff；ReviewFlow 以 Review Bot 身份持久化、发布并对账
GitLab Note、Auto-fix MR 和通知。

## Target design

- Project Policy 是仓库级请求；Operator 配置在仓库外定义可执行
  Backend/Profile、环境 allowlist、校验和发布权限。
- AI Review 和 Auto-fix 默认关闭；v0.1 是 Notify-only，不批准、不阻塞、
  不直接合并父 MR。
- Agent 不持有 GitLab 发布凭据，不提交、push、创建 MR 或发 Note。
- Agent 向受控 `RESULT_PATH` 写入版本化 JSON；stdout/stderr 只用于诊断。
- Note marker 仅由 ReviewKey 和 `effect=note` 组成，不含 Run ID 或
  固定 `kind`。
- Auto-fix 使用不可变 Intent nonce、受限 Diff Envelope、Operator
  verification 和不可变 Publication Plan。
- AutoFixPublisher 通过 GitLab Commits API 以精确 SHA 为基线创建分支
  和子 MR。安全默认是 Draft MR 且跳过自动 CI，由人工检查后启动。
- NotificationDispatcher 独立投递持久化请求。AutoFixReconciler 只处理
  不确定外部结果、崩溃恢复和已知 Relation 状态，不自动接管 MR。
- Store 丢失后只能由本地 Operator CLI 显式、完整校验并审计地恢复。
- Run Lifecycle 与 Review Outcome 分离；进程成功不代表用户已看到结果。
- v0.1 使用一个受 supervisor 管理的 Worker OS 进程，内含独立
  Review/Publication、Notification 和 Reconciliation/Cleanup 循环。

完整规格见 [docs/spec/README.md](docs/spec/README.md)，领域语言见
[CONTEXT.md](CONTEXT.md)，当前代码与目标的差距见
[docs/spec/07-implementation-status.md](docs/spec/07-implementation-status.md)。

## Development

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
pytest
ruff check .
```

当前正在从 checkpoint 中的 Agent-owned 预发布实现迁移到上述目标。
迁移期间不提供旧 Policy/SQLite/marker 兼容契约；项目尚在开发阶段，
请以当前测试和 implementation-status 为准。

本地测试不访问真实 GitLab、飞书、企微或 Agent Provider。真实 E2E
需要 Operator 提供受控凭据和运行环境。

## License

Apache-2.0
