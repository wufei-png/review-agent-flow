# Integration specification

## GitLab ingress

The API accepts GitLab Merge Request Hooks at `POST /webhooks/gitlab` only after `GITLAB_WEBHOOK_SECRET` and `REVIEW_AGENT_FLOW_PROJECT_ID` are configured. It checks `X-Gitlab-Token` on every request; a wrong token returns `401`, and a payload from another Project returns `403` without enqueueing work. The reference deployment binds one Project per process.

Parent review intake uses only `object_attributes.last_commit.id` or `object_attributes.last_commit_id`. `merge_commit_sha` is never a parent review fallback. Every ReviewKey SHA must be a 40- or 64-character hexadecimal SHA; invalid or incomplete identity is rejected.

A Pipeline Hook only wakes an existing deferred Review Run for the same ReviewKey. An Auto-fix child MR Hook only updates or wakes an existing local Intent/Relation or reconciliation item. It never creates a Review Run and never establishes a relation without local authority.

## GitLab adapter and Note publication

The adapter uses `python-gitlab` behind narrow domain interfaces for:

- parent MR, Pipeline and branch observation;
- paginated trusted Review Bot Note lookup and Note creation;
- repository commit creation through the GitLab Commits API;
- Auto-fix MR creation and exact source/target/state observation.

Before publishing any effect, ReviewFlow confirms the parent MR still has the ReviewKey SHA. A stale ReviewKey becomes `superseded`; no old Note or Auto-fix is published.

ReviewFlow creates a Note containing exactly one canonical marker:

```text
<!-- reviewflow:v1;project=<project-id>;mr=<iid>;sha=<sha>;effect=note -->
```

Lookup is scoped to the Project and parent MR, paginates all Notes and trusts only the configured Review Bot author's exact marker. Zero matches permits creation; one valid match adopts the external identity; duplicates or malformed matches fail closed for reconciliation. A Note confirmed deleted by the user is not recreated.

## Auto-fix publication

AutoFixPublisher consumes a persisted immutable Publication Plan. It creates the source branch and commit atomically with the GitLab Commits API using the Plan's ordered actions, `start_sha=ReviewKey.sha` and `force=false`, then creates a same-Project child MR targeting the parent MR's original source branch. Fork-source Auto-fix is unsupported in v0.1.

The Program owns branch, commit, title, target, creation readiness, CI trigger and description marker:

```text
autofix/MR-<iid>/sha-<sha>/intent-<nonce>
<!-- reviewflow:v1;project=<project-id>;mr=<iid>;sha=<sha>;effect=autofix;intent=<nonce>;plan_sha256=<digest> -->
```

For `initial_readiness=draft`, the frozen creation title begins with GitLab's canonical `Draft: ` prefix; `ready` omits it. Readiness is not sent as an undocumented create-MR field.

The digest input is the canonical UTF-8 JSON Plan payload with sorted keys and no insignificant whitespace. It contains every publication input and a marker template, but excludes the derived `plan_sha256` value and final rendered marker. ReviewFlow hashes that payload, renders the marker from the digest, and freezes the payload, digest and final marker together. This avoids self-reference while binding every independent publication choice. Retry uses the same frozen Plan and Intent nonce.

After GitLab returns the child identity, ReviewFlow persists the Auto-fix Relation and its idempotent Notification Request in one SQLite transaction. A publishable Relation cannot be committed without that request; reconciliation repairs an older incomplete transaction state before treating publication as complete.

Presentation changes after creation, including title, description text around the marker and Draft/Ready state, do not invalidate a Relation. Source/target/tree drift is observed as divergence; ReviewFlow never force-updates user changes. A Relation records opened, closed and merged states, including the merge SHA used to suppress only that exact parent SHA.

The Reconciler searches only when an external result is uncertain, a process may have crashed, relation state may have changed or an Operator requested recovery. The normal path does not search for Agent-created artifacts because the Agent cannot create them. After an ambiguous create-MR result it requires two delayed, unique zero-candidate observations before recreating the MR, and only when the branch commit identity is already durable and the parent still matches the frozen exact SHA. An ambiguous commit without a durable commit identity is never reissued automatically.

## Human notifications

The first release supports Feishu and WeCom. A Project may configure both. Each channel has a stable unique ID; outbox routing uses that ID rather than list position. Normal human review may notify configured reviewers; an Auto-fix MR notification targets only the original parent MR creator.

ReviewFlow persists one immutable Notification Request and one outbox delivery per selected channel before delivery. Channel failure does not roll back the Review Run or another channel. Failed deliveries retry with bounded backoff and remain auditable. Delivery is at-least-once across a crash between provider delivery and acknowledgement, so a provider may receive a duplicate.

The webhook adapter succeeds only for HTTP 2xx plus provider business success (`code == 0` for Feishu, `errcode == 0` for WeCom). Provider-specific mention markup stays in the adapter.

## Explicit recovery

Store-loss recovery is never inferred from an Auto-fix-looking branch, title or marker alone. A trusted-host Operator CLI requires an exact Project, parent MR, parent SHA, Intent nonce, Plan digest, the canonical full Plan payload and child MR identity. It requires one unique configured Review Bot MR, revalidates the parent and child identities, and reconstructs the child commit's single exact parent plus its bounded paths, contents and executable modes before comparing those remote actions with the Plan. It records reason, principal, hostname, time, tool version, recovery ID and success or failure.

Recovery does not automatically resend historical notifications because delivery state is indeterminate. A separate audited resend command creates a new immutable Notification Request generation and fresh outbox deliveries; it does not reset or overwrite historical delivery state.

## Agent and model integrations

Automated OpenCode execution uses its CLI or a programmatic local endpoint through an Operator-owned Backend Profile. Other Agents can implement the same backend contract. OpenCode Web remains an Operator-facing UI, not the publication path.

The core only defines an OpenAI-compatible Provider contract. LiteLLM is a deployment recommendation, not a core dependency.
