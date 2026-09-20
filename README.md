# review-agent-flow

GitLab 人类评审与 AI Agent 协同的开源工作流编排器。

ReviewFlow 接收单一 GitLab Project 的 Merge Request Webhook，以
`Project + 父 MR IID + 精确 SHA` 建立 ReviewKey，在该 SHA 的 Trusted
Workspace 中调用 Operator 授权的 Agent Backend。Agent 只产生评审结果和
可选修复 diff；ReviewFlow 以 Review Bot 身份持久化、发布并对账
GitLab Note、Auto-fix MR 和通知。

## Target design

- Project Policy 从 MR 目标分支精确 commit 读取并按 Run 冻结；Operator
  配置在仓库外定义可执行 Backend/Profile、通知 channel、环境 allowlist、
  校验和发布权限。
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
  Review/Publication、Notification 和 Reconciliation/Cleanup 三个循环；
  Retention 定时扫描属于第三个循环；Webhook API 是独立 OS 进程。

完整规格见 [docs/spec/README.md](docs/spec/README.md)，领域语言见
[CONTEXT.md](CONTEXT.md)，当前代码与目标的差距见
[docs/spec/07-implementation-status.md](docs/spec/07-implementation-status.md)。
有限生产试运行的分会话实施顺序与验收范围见
[试运行准备执行队列](docs/plans/pilot-readiness-queue.md)。

## Development

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
pytest
ruff check .
```

当前代码已完成从 checkpoint 中 Agent-owned 预发布实现到 Program-owned
发布路径的硬切换；Review Outcome、目标分支 Policy Snapshot 和 Operator-owned
通知绑定已实现，有限生产试运行尚未放行。不提供旧 Policy/SQLite/marker
兼容契约；已有真实 canary Store、外部收据和私有备份需要保留。schema 变化或
重建前，应先保存一致性 Store/Plan 备份与现场证据，并核对恢复路径；当前边界见
[implementation-status](docs/spec/07-implementation-status.md)。

Linux + systemd 的 API/Worker 分进程、最小凭据、优雅停机和 30 天默认保留
参考部署见 [docs/deployment-linux-systemd.md](docs/deployment-linux-systemd.md)。
无需 Docker Compose 的单机 OpenCode 试用流程见
[docs/quickstart-host-native.md](docs/quickstart-host-native.md)，其中 `review-mr`
只负责绑定 Project 的幂等手动入队，外部发布仍由 Worker 执行。

Store 丢失后的 Auto-fix Relation 只能通过显式 Operator 命令恢复。命令还需
提供发布前保存的规范 Publication Plan JSON，以便重新核对远端 commit 的父
SHA、文件内容、路径和执行位；恢复不会自动补发通知：

```bash
review-agent-flow recover-autofix-relation \
  --project-id 42 --parent-mr-iid 1 --parent-sha "$PARENT_SHA" \
  --intent-nonce "$INTENT_NONCE" --plan-sha256 "$PLAN_SHA256" \
  --plan-file /trusted/path/plan.json --child-mr-iid 9 \
  --reason "restore lost local Store" --db /trusted/path/reviewflow.db

review-agent-flow resend-autofix-notification \
  --project-id 42 --child-mr-iid 9 --operator-config /trusted/path/operator.yml \
  --reason "Operator approved notification resend" \
  --db /trusted/path/reviewflow.db

review-agent-flow rearm-autofix-plan \
  --plan-id 7 --reason "remote evidence reviewed" \
  --db /trusted/path/reviewflow.db

review-agent-flow abandon-autofix-plan \
  --plan-id 8 --reason "pilot cancelled" \
  --db /trusted/path/reviewflow.db
```

GitLab URL/token 和 Review Bot 用户名分别来自命令参数或 `GITLAB_URL`、
`GITLAB_TOKEN`、`GITLAB_REVIEW_BOT_USERNAME`。每次命令记录 OS principal、
hostname、tool version、reason、result 和独立 recovery ID。
Plan rearm/abandon 不访问 GitLab；它们依据持久化 write phase 选择安全发布重试、
仅对账重试或终止，并记录独立 action ID。

本地测试不访问真实 GitLab、飞书、企微或 Agent Provider。历史上的
[2026-08-10 A1 canary](docs/evidence/jihulab-opencode-a1-2026-08-10.md)
只验证了 Program-owned Note、幂等手动触发和租约回收。新的
[2026-09-21 exact-SHA canary](docs/evidence/jihulab-opencode-canary-2026-09-21.md)
取得 Note、Auto-fix 子 MR、仅吴非的飞书私信可见性和 MR 写后恢复收据；
commit 写后未完成发布恢复，试运行判定为 no-go。

## License

Apache-2.0
