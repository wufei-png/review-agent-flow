# Domain and consolidated decisions

## Product shape

`review-agent-flow` 是面向 GitLab Project 的人类评审与 AI Agent 评审编排器。它接收 GitLab Webhook，按 Project Policy 创建 Review Run，在精确提交版本的独立 worktree 中运行本地 Agent；Agent 自己向 GitLab 写 Review Note，并可以创建独立 Auto-fix Merge Request。

它不是：

- GitLab Group 级策略中心
- AI 模型厂商 SDK 聚合层
- 直接替用户批准或阻塞合并的 AI Gate
- 直接修改用户 MR source branch 的机器人

## Decisions

| Area | Decision |
| --- | --- |
| Project boundary | 只支持 Project 级策略覆盖；不引入 Group 默认策略 |
| Policy file | 可选 `.gitlab/reviewflow.yml`；`init` 只生成模板，服务不维护文件 |
| Review mode | v0.1 只有 `Notify-only`；Reviewer 只代表通知对象，不实现 `Enforce`、Approval Requirement 或 GitLab Approval 写入 |
| Reviewer | 显式配置 GitLab username；首版不自动解析 CODEOWNERS |
| Human/AI | 人审与 AI 独立开关；AI 和 auto-fix 默认关闭 |
| Trigger | MR Ready 后触发；启用 Pipeline Gate 时等待最新 Pipeline 成功；同 SHA 幂等，新 SHA 可重跑 |
| Pipeline Hook | 仅在启用 Gate 时唤醒同一 Project/MR/SHA 的已有 pending Run；不创建独立 Pipeline Run，先到的 Hook 等待 MR Hook |
| Agent output | Agent 不需要返回 JSON，直接提交 GitLab Note |
| Agent execution | 支持通用本地 CLI，也为 OpenCode Web/Server/ACP 等入口预留适配 |
| Model API | 核心只定义 OpenAI-compatible Provider 接口；部署文档推荐 LiteLLM |
| Worktree | 每个 Review Run 使用精确 SHA 的独立 Git worktree |
| Auto-fix | 创建独立 MR，目标为原 MR source branch；分支 `autofix/MR-<iid>/run-<run-id>`，标题前缀 `[ReviewFlow Auto-Fix]` |
| Auto-fix identity | 服务端先持久化 intent；标题和分支是展示/关联字段，不能单独使事件进入 Auto-fix 路径 |
| Auto-fix review | Auto-fix MR 只跑 CI 和原 MR 创建者的人审，不跑 AI；通知只发给原 MR 创建者 |
| Auto-fix loop | 服务记录 parent MR 与 Auto-fix MR 关系；合并后只跳过对应 parent SHA，普通用户提交立即恢复 |
| Identity | 使用独立 Review Bot 身份，不复用 webhook 触发者 |
| Execution trust | 首版采用 Trusted Execution Environment，核心不裁剪 Agent 工具/网络/凭据能力 |
| Runtime | API + 独立 Worker + SQLite + 数据库轮询；存储和队列保留扩展接口 |
| Deployment | 一个 API/Worker 进程只绑定一个 Project；Webhook secret、Project ID 必填，事件 Project ID 必须严格匹配 |
| License | Apache-2.0 |

## Explicit non-goals for v0.1

- Group inheritance、CODEOWNERS 自动推导
- Enforce/Approval Requirement、GitLab Approval 写入和多 Project 进程级隔离
- 直接 merge、AI approval、AI blocking
- Provider-specific SDK
- 统一 Agent JSON 输出协议
- Redis/Postgres 必选依赖
- 多租户安全隔离或沙箱能力
