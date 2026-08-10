# Implementation status

## Migration status

The normative specifications and ADRs describe the accepted Program-owned publication target. Publication, target-branch Policy Snapshots, Operator-owned notification bindings, Review Outcome projection, retention enforcement and the Linux systemd reference deployment are implemented. Live GitLab, reverse-proxy and backup/restore verification remain deployment gates.

Policy v4, Operator configuration v3, kind-free ReviewKey storage, strict external Operator Backend/Profile and notification authorization, minimal Agent environment, RESULT_PATH v1 parsing, durable Program-owned Note publication, immutable Auto-fix Intent creation, bounded Diff Envelope capture, Operator verification, Publication Plan freeze, normal-path AutoFixPublisher and exceptional online reconciliation are implemented. The Publisher persists precise preflight/commit/MR write phases, uses one GitLab Commits API request with the frozen `start_sha` and `force=false`, creates the child MR, and atomically persists its Relation, Notification Request and outbox deliveries. The Reconciler processes only Plans that crossed an external-write boundary and established Relations; child Webhooks cannot create local authority. One supervised Worker process runs independent Review/Publication, Notification and Reconciliation/Cleanup loops, with Retention scheduled in the third loop, separate from the API OS process. Explicit Store-loss recovery requires the canonical Plan payload and validates the unique Review Bot MR plus remote commit parent/actions before atomically rebuilding authority; it never creates a notification, and notification resend fails closed without the retained frozen Policy Snapshot. Separate audited commands resend notification generations, rearm blocked Plans according to their write phase, or terminally abandon idle Plans. The fresh schema contains neither Agent-owned Auto-fix tables nor Run-ID marker readers. There is no migration or compatibility requirement for old database rows, policies or markers.

Implemented foundations that remain useful include exact-SHA worktrees, bound-Project webhook validation, SQLite claims/leases, notification outbox primitives, GitLab event normalization, Pipeline Gate, provider protocols and Feishu/WeCom adapters. Webhook transport/authentication is separated from the project-bound decision applier, and Worker polling delegates publication, idle reconciliation and completed-effect publication to focused seams without changing their scheduling order. Their contracts may still change while adopting the target model.

## Historical evidence

On 2026-08-03 a JiHuLab canary in Project `358268` completed Review Run `2`, parent MR `!3`, Auto-fix MR `!4` and Agent-owned Note `8733889`. Project `358268` was later marked for deletion.

This is evidence for the old Agent-owned path only. It does not verify strict RESULT_PATH, Program-owned Note publication, Diff Envelope validation, immutable Publication Plans, GitLab Commits API publication, the new markers or recovery contracts.

## Target acceptance criteria

- Duplicate intake for one Project/parent MR/exact SHA creates one kind-free Review Run.
- Policy v4 rejects executable commands and notification credentials, and selects only Operator-authorized backend/profile and channel IDs; v1-v3 are rejected.
- Startup strictly validates the external Operator configuration and bound Project allowlists.
- The Agent receives a detached exact-SHA Trusted Workspace, a profile environment allowlist plus Operator-owned execution PATH, and a controlled RESULT_PATH, but no GitLab publication token.
- Missing, malformed, unsupported or oversized Agent results fail closed; valid raw review Markdown is Program-published with the canonical ReviewKey Note marker.
- A stale parent head supersedes unpublished effects; a confirmed user-deleted Note is not recreated.
- Note and Auto-fix are independent effects: an invalid proposal or diff does not suppress a valid Note.
- Auto-fix requires an active immutable Intent nonce, eligible Diff Envelope and passing Operator verifier.
- The complete immutable Publication Plan is durable before any GitLab write; its digest excludes derived digest/marker fields and appears in the Program-owned marker.
- AutoFixPublisher uses the GitLab Commits API with `start_sha` equal to the ReviewKey SHA and `force=false`, then atomically persists Relation with its Notification Request.
- Auto-fix write phases fence both external calls; only expired `preflight` leases return to normal publication, while later phases reconcile.
- Creation readiness and CI behavior are frozen from Operator configuration; `ready/manual` is rejected.
- Relation identity survives presentation edits, while source/target/tree drift is observed without force overwrite.
- Child MR webhooks update or wake only an existing Intent/Relation; they never create a Review Run or local authority.
- Notification delivery and reconciliation/cleanup use independent loops within one supervised Worker process.
- Store-loss relation recovery, notification resend, blocked-Plan rearm and Plan abandonment are separate explicit audited Operator actions.
- Review Outcome is derived from immutable expectations and durable receipts as `pending`, `no_effect`, `delivered`, `degraded`, `undelivered` or `indeterminate`.
- Full unit/integration tests cover crash windows, idempotent retries, stale heads, duplicate markers, invalid diffs, verifier failure and recovery validation.
- Retention tests prove backup-before-purge, immutable backup conflicts, terminal payload purge and graceful Worker shutdown.
- The Worker now freezes `AgentTaskV1` context and exact GitLab diff refs; the legacy environment-variable command runner remains active until the thin-adapter stage wires this canonical task into Backend invocation.

Live GitLab verification of the target path remains required after the implementation and local integration gates pass.
