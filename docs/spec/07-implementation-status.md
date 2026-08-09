# Implementation status

## Migration status

The normative specifications and ADRs describe the accepted Program-owned publication target. Migration is in progress, and the implementation must not be described as target-complete until every acceptance criterion below is implemented and verified.

Policy v3, kind-free ReviewKey storage/Note identity, strict external Operator Backend/Profile authorization, minimal Agent environment and RESULT_PATH v1 parsing are implemented. The current code still observes the old Agent-owned GitLab Note/MR publication path and has an Auto-fix reconciler that searches for Agent-created MRs. Those remaining paths are intentionally replaced in later implementation stages. There is no migration or compatibility requirement for old database rows, policies or markers.

Implemented foundations that remain useful include exact-SHA worktrees, bound-Project webhook validation, SQLite claims/leases, notification outbox primitives, GitLab event normalization, Pipeline Gate, provider protocols and Feishu/WeCom adapters. Their contracts may still change while adopting the target model.

## Historical evidence

On 2026-08-03 a JiHuLab canary in Project `358268` completed Review Run `2`, parent MR `!3`, Auto-fix MR `!4` and Agent-owned Note `8733889`. Project `358268` was later marked for deletion.

This is evidence for the old Agent-owned path only. It does not verify strict RESULT_PATH, Program-owned Note publication, Diff Envelope validation, immutable Publication Plans, GitLab Commits API publication, the new markers or recovery contracts.

## Target acceptance criteria

- Duplicate intake for one Project/parent MR/exact SHA creates one kind-free Review Run.
- Policy v3 rejects executable commands and selects only an Operator-authorized backend/profile; v1/v2 are rejected.
- Startup strictly validates the external Operator configuration and bound Project allowlists.
- The Agent receives a detached exact-SHA Trusted Workspace, an environment allowlist and a controlled RESULT_PATH, but no GitLab publication token.
- Missing, malformed, unsupported or oversized Agent results fail closed; valid raw review Markdown is Program-published with the canonical ReviewKey Note marker.
- A stale parent head supersedes unpublished effects; a confirmed user-deleted Note is not recreated.
- Note and Auto-fix are independent effects: an invalid proposal or diff does not suppress a valid Note.
- Auto-fix requires an active immutable Intent nonce, eligible Diff Envelope and passing Operator verifier.
- The complete immutable Publication Plan is durable before any GitLab write; its digest excludes derived digest/marker fields and appears in the Program-owned marker.
- AutoFixPublisher uses the GitLab Commits API with `start_sha` equal to the ReviewKey SHA and `force=false`, then atomically persists Relation with its Notification Request.
- Creation readiness and CI behavior are frozen from Operator configuration; `ready/manual` is rejected.
- Relation identity survives presentation edits, while source/target/tree drift is observed without force overwrite.
- Child MR webhooks update or wake only an existing Intent/Relation; they never create a Review Run or local authority.
- Notification delivery and reconciliation/cleanup use independent loops within one supervised Worker process.
- Store-loss relation recovery and any notification resend are separate explicit audited Operator actions.
- Review Outcome is derived from immutable expectations and durable receipts, distinguishing success, pending, degraded, failed, blocked and superseded effects.
- Full unit/integration tests cover crash windows, idempotent retries, stale heads, duplicate markers, invalid diffs, verifier failure and recovery validation.

Live GitLab verification of the target path remains required after the implementation and local integration gates pass.
