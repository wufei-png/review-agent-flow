---
status: accepted
date: 2026-08-09
supersedes: ADR-0004, ADR-0005, ADR-0006, ADR-0007, ADR-0012, ADR-0015, ADR-0016, ADR-0017, ADR-0018
---

# Make ReviewFlow own Note and Auto-fix publication

The Agent analyzes one exact-SHA Trusted Workspace and returns a bounded,
versioned result through a controlled Result Path. It receives no GitLab
publication credential and does not commit, push, create a Merge Request, or
post a Note. ReviewFlow owns every GitLab side effect so it can validate the
candidate, persist intent before calls, reconcile uncertain results, and apply
Operator-owned policy at the point where external authority is exercised.

ReviewKey is exactly the configured Project, parent MR IID, and exact SHA.
`ReviewRun.kind` is removed, v1 markers contain no fixed review kind, and there
is no legacy Run-ID read path or pre-release migration contract. Notes and
Auto-fix artifacts use distinct `effect=note|autofix` markers. A Note marker is
stable for its ReviewKey. An Auto-fix marker additionally contains one
immutable Intent nonce and `plan_sha256`, where the digest covers canonical
Publication Actions rather than a Git commit identity.

The canonical marker forms are:

`<!-- reviewflow:v1;project=<project-id>;mr=<iid>;sha=<sha>;effect=note -->`

`<!-- reviewflow:v1;project=<project-id>;mr=<iid>;sha=<sha>;effect=autofix;intent=<nonce>;plan_sha256=<digest> -->`

The canonical Auto-fix source branch is
`autofix/MR-<iid>/sha-<sha>/intent-<nonce>`.

ReviewFlow persists a Note Publication Request before posting. For Auto-fix it
persists one active Intent generation per ReviewKey before Agent execution.
Retries reuse that generation and nonce. Explicit Operator re-arm terminates
the current generation and creates a new immutable generation and nonce while
retaining the old audit history. A valid proposal and non-empty Diff Envelope
are verified by the selected Operator-owned Backend Profile and then frozen
into one immutable Publication Plan before any GitLab write. The plan retains
exact bounded actions, content, modes, base SHA, branch, marker, commit
message, title, target, creation readiness, and CI mode in canonical JSON plus
SHA-256. Once the external effect is confirmed or the Operator explicitly
abandons it, sensitive payload content is removed while identity, digest,
state, and audit metadata remain.

AutoFixPublisher uses the GitLab Commits API with `start_sha=ReviewKey.sha` and
`force=false`, then creates the child MR as the configured Review Bot. The safe
Operator defaults are `initial_readiness=draft` and `ci_trigger=manual`; the
combination `ready/manual` is invalid. These creation-time values are frozen in
the plan, while later user Draft/Ready, title, or description changes do not
break an established Relation. Source/target/tree drift is observed but never
automatically overwritten. Auto-fix publication is limited to same-Project
source branches in v0.1.

The Agent Result keeps bounded raw review Markdown. ReviewFlow does not attempt
to sanitize GitLab Quick Actions or mentions; their effects are governed by
the Review Bot's GitLab permissions and are an accepted Trusted Project
Deployment risk. ReviewFlow still owns and validates marker, branch, target,
title, commit message, readiness, CI mode, and publication identity.

Note and Auto-fix are independent effects. A valid Note may publish when an
Auto-fix proposal is absent or invalid. Auto-fix is optional until a verified
Publication Plan is frozen; at that point it becomes an Output Expectation.
Recoverable publication remains pending, while terminal failure or a blocked
plan degrades Review Outcome. If the parent MR head advances before Note or
Auto-fix publication, the stale effect becomes `superseded` and is not
published. A user-deleted confirmed Note is not recreated automatically.

Normal publication persists the returned child MR as an Auto-fix Relation and
atomically requests its notification. AutoFixReconciler handles uncertain
provider results, process crashes, Relation state changes, and explicit
Operator recovery; it does not search for Agent-created MRs on a normal path.
An Auto-fix-shaped Webhook may update or wake an existing Intent/Relation but
cannot create a Review Run or establish identity without local authority.

After Store loss, only a local trusted-host Operator CLI may recover one exact,
unique Review Bot MR after validating ReviewKey, nonce, plan digest, branch,
target, author, marker, and remote actions. Recovery requires a reason and
audits the OS principal, hostname, time, command version, and recovery ID. It
does not automatically resend an indeterminate notification; a separate
audited command creates a new Notification Request when requested.

Project Policy moves directly to schema v3 and may select only an
Operator-authorized Backend/Profile; it cannot define executable commands.
Publisher configuration and allowlists live outside the repository, load
strictly at process start, and are frozen into a plan. An Operator kill switch
may stop the next external write even for an existing plan. The linked
worktree remains a Trusted Workspace rather than a sandbox; filesystem and
network isolation belong to the Operator-owned Backend Profile.

AI Review and Auto-fix remain disabled by default. A Project may override the
default Review Agent Skill, but the override is Agent input rather than host
command or publication authority. Worktree cleanup remains an independently
retried resource task and cannot rerun the Agent. A related Auto-fix merge
suppresses only its resulting parent SHA; a later user SHA resumes normal
review.

Run Lifecycle remains stable after orchestration terminates. Review Outcome is
a separate projection over expected user-visible effects and notification
deliveries and may change as publication or reconciliation progresses. A
superseded stale effect is not a delivery failure; a confirmed effect removed
later by a user is observed as removed and is not recreated automatically.

## Considered options

- Agent-owned GitLab publication was rejected because process success could not
  establish durable external intent or safe recovery.
- Markdown sanitization was rejected in favor of GitLab-side authorization and
  a documented trusted-deployment boundary.
- Automatic CI with a Ready MR was rejected as the default because an
  unreviewed Agent diff could execute in GitLab Runner before a human sees it.
- Automatic Store-loss adoption was rejected because a marker-like artifact is
  not local authorization.
