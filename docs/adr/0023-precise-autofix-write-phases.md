---
status: accepted
date: 2026-08-10
supersedes: ADR-0019 re-arm semantics
---

# Persist precise Auto-fix write phases

Auto-fix publication persists a `write_phase` before and after each GitLab
write. The phases are `preflight`, `commit_write_started`, `commit_confirmed`,
`mr_write_started`, `mr_confirmed`, and `published`. A lease expiry in
`preflight` is safe to reclaim as normal publication because no external write
has started. Every other unfinished phase enters reconciliation; normal
publication never blindly repeats an uncertain write.

The commit and Merge Request response identities are persisted separately.
After an uncertain Merge Request write, the Reconciler requires two durable
absence observations separated by at least five seconds before clearing that
boundary and attempting one new MR write. This minimum is independent of the
configured polling delay. A discovered child is re-fetched and its exact head
must equal the confirmed branch commit before Relation persistence. An
uncertain commit without a confirmed commit identity remains
reconciliation-only and cannot be reissued automatically.

An Operator may `rearm` only a blocked Plan. Rearm resets its bounded attempt
budget without changing the frozen Plan, digest, Intent generation, or nonce.
A `preflight` Plan returns to normal publication; a Plan that crossed an
external-write boundary returns only to reconciliation. An Operator may
`abandon` an idle frozen, blocked, or reconciliation-needed Plan. Abandon is
terminal and remains a failed Output Expectation.

Both decisions require a reason and immutable action ID and record the OS
principal, host, tool version, prior state, resulting state, write phase,
result, and redacted rejection diagnostic. Rejected transitions are audited
when the Plan exists. Neither command performs a GitLab write.

Creating a new Intent generation is reserved for a genuinely new candidate or
publication choice, not for retrying the same immutable Plan. This replaces
ADR-0019's earlier re-arm description.
