---
status: superseded by ADR-0019
---

# Worktrees and Auto-fix loop suppression

Every Review Run uses a disposable worktree at the exact SHA. The Worker explicitly ensures the SHA is locally available with a repository lock and bounded fetch before invoking the Worktree manager. Cleanup is an independently retried resource task and cannot cause the Agent to run again. Auto-fix MRs use `[ReviewFlow Auto-Fix]` and `autofix/MR-<iid>/run-<run-id>`, do not trigger AI Review, and are related to the parent MR so a merge suppresses only the corresponding parent SHA.
