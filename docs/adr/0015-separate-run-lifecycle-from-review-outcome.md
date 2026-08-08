---
status: accepted
date: 2026-08-08
---

# Separate Run Lifecycle from Review Outcome

Review Run lifecycle records whether orchestration has terminated and remains
stable after completion. Review Outcome is a separate value derived from
user-visible effect and notification-delivery records, and may change when
later delivery or reconciliation observes an artifact. This keeps asynchronous
delivery visible without rewriting a completed Run or treating Agent process
success as proof that a review reached the user.

Review Outcome is a projection over Run Lifecycle plus user-visible Note and
Auto-fix Effect Receipts and notification-outbox deliveries. Agent execution
and worktree cleanup are operational evidence, not user-visible delivery. The
Run intake first persists immutable Requested Outputs from its Policy Snapshot.
After resolving the current MR/SHA, Run kind, suppression state and effective
notification recipients, it persists an Effective Run Decision and immutable
Output Expectations before the first user-visible side effect, repository
mutation or Agent execution. A normal eligible Run expects a Note only when its
effective decision enables AI Review; notification expectations are recorded
per effective channel/recipient delivery. Cancelled, Auto-fix notification-only
and suppressed Runs therefore do not inherit a Note expectation merely because
the raw Policy requested AI. Auto-fix is an optional proposal rather than an
Output Expectation: an observed child MR adds a delivered artifact, but an
absent or unobserved proposal does not keep Review Outcome pending or make it
degraded. Its reconciliation state remains available as operational evidence.

The projection uses this precedence:

1. `pending` when the Run is non-terminal or any Output Expectation can still
   be delivered or reconciled;
2. `indeterminate` when the Run is terminal, Requested Outputs exist, and no
   Effective Run Decision could be persisted;
3. `no_effect` when an Effective Run Decision has no Output Expectations and
   no optional user-visible artifact was observed;
4. `delivered` when every Output Expectation has a terminal observed or
   delivered record;
5. `degraded` when at least one expectation or optional artifact was delivered,
   while at least one expectation terminated failed, not observed or without a
   corresponding record; and
6. `undelivered` when expectations exist, the Run and their delivery paths are
   terminal, and none was observed or delivered.

This accepted projection is not yet implemented in the status API.
