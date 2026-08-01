# Review Agent Flow Specification

这里是 `review-agent-flow` 的可执行规格。`CONTEXT.md` 只维护领域术语；`docs/adr/` 记录不可逆或容易被误解的决策；本目录描述系统应如何工作、组件如何协作以及当前实现进度。

## Documents

- [00-history-and-scope.md](./00-history-and-scope.md) — 原项目、替代调研和为什么新建独立 OSS 项目
- [01-domain-and-decisions.md](./01-domain-and-decisions.md) — 历史决策收敛后的产品边界和默认行为
- [02-event-lifecycle.md](./02-event-lifecycle.md) — Webhook、Review Run 和 Auto-fix MR 生命周期
- [03-policy-schema.md](./03-policy-schema.md) — `.gitlab/reviewflow.yml` 规范
- [04-agent-contract.md](./04-agent-contract.md) — Provider、Agent CLI、Skill 和 worktree 协议
- [05-integrations.md](./05-integrations.md) — GitLab、飞书、企业微信和 OpenCode/LiteLLM 边界
- [06-runtime-and-storage.md](./06-runtime-and-storage.md) — API、Worker、SQLite、幂等和状态存储
- [07-implementation-status.md](./07-implementation-status.md) — 当前代码映射、剩余实现和验收标准

## Normative language

“必须”表示兼容性或安全边界；“应该”表示默认实现；“可以”表示扩展点。规格中的 `Project`、`Reviewer`、`Review Run`、`Auto-fix Merge Request` 等术语以根目录 [CONTEXT.md](../../CONTEXT.md) 为准。
