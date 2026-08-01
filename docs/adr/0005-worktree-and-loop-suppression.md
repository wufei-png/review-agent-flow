# Worktrees and Auto-fix loop suppression

Every Review Run uses a disposable worktree at the exact SHA. Auto-fix MRs use `[ReviewFlow Auto-Fix]` and `autofix/MR-<iid>/run-<run-id>`, do not trigger AI Review, and are related to the parent MR so a merge suppresses only the corresponding parent SHA.
