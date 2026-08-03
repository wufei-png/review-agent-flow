# Integration specification

## GitLab ingress

The API accepts GitLab Merge Request Hooks at `POST /webhooks/gitlab` only after `GITLAB_WEBHOOK_SECRET` and `REVIEW_AGENT_FLOW_PROJECT_ID` are configured. It checks `X-Gitlab-Token` on every request; a wrong token returns `401`, and a payload from another Project returns `403` without enqueueing work. The deployment is single-Project: the configured Project ID must match the payload Project ID.

Normal Merge Request review runs use only `object_attributes.last_commit.id` or `object_attributes.last_commit_id`. `merge_commit_sha` is never a normal review fallback; it is reserved for the Auto-fix MR merge event. Every enqueued or suppression SHA must be a 40- or 64-character hexadecimal SHA. Missing or invalid Project/MR identity or SHA is rejected without enqueueing work.

The current endpoint is intentionally small. The remaining GitLab adapter must provide:

- MR fetch and source/target branch details;
- Pipeline status for Pipeline Gate;
- Note creation, preserving the public `create_note(project_id, mr_iid, body) -> None` contract;
- Optional Note observation through a separate `NoteObserver` capability:
  `find_note_by_marker(project_id, mr_iid, marker) -> Note | None`;
- reviewer lookup/mention data;
- branch push and Auto-fix MR creation;
- parent/Auto-fix MR merge relationship lookup.

Auto-fix classification is based on a server-owned Store intent/relation plus the exact Run/SHA marker in the child MR description. A title or branch that merely looks like an Auto-fix output is not sufficient. The MR lookup used to reconcile an Agent result must include closed and merged MRs, not only open MRs, and must skip candidates without the current intent marker. A terminal intent with no reconciled child MR is marked abandoned. Note-marker deduplication must paginate through all Notes and only trust Notes authored by the configured Review Bot username (`GITLAB_REVIEW_BOT_USERNAME`); if that username is not configured, the safe behavior is to run again rather than trust an unverified marker.

When a worktree SHA is absent locally, the Worker calls an explicit `ensure_sha` operation. It checks the local object database, takes a repository lock, performs a bounded fetch and verifies the commit again before invoking `git worktree add`. Fetch credentials and remote access remain deployment inputs.

The adapter should use `python-gitlab` behind a narrow interface so the domain and Worker do not depend on GitLab SDK objects.

The Worker detects the optional observer capability at runtime, so existing
GitLab clients and fakes that only implement Note creation remain compatible.
ReviewFlow passes the canonical token
`<!-- review-run:<run-id>:note -->`. The observer must scope the lookup to the
Project and MR and return a `Note` only when exactly one Note body contains one
standalone line equal to that full token and the native Note ID is a strict
positive integer. Zero matches, duplicate exact matches, prefix/suffix matches,
and IDs represented by zero, negative numbers, booleans, floats, or strings are
fail-closed as `None`. The Worker repeats the exact-token validation before
updating `note_expected` to `observed`; missing observer capability or an
unverified Note leaves the receipt `not_observed` without changing Agent Run
success. The Note receipt keeps the historical dedupe key
`review-run:<run-id>:note`; the canonical HTML token is an observation token,
not a replacement dedupe key.

## Human notifications

The first release supports both Feishu and WeCom. A Project may configure both channels. Each configured channel has a stable unique `id`; outbox routing uses that ID rather than list position. Normal human review can notify configured reviewers; Auto-fix MR notifications go only to the original parent MR creator. Channel credentials and identity mappings are runtime secrets/configuration, not committed policy content. Notification payloads include the event title, body and optional URL.

The Worker writes one SQLite outbox record per configured channel before attempting delivery. A channel failure does not fail or roll back the Review Run, and does not prevent another channel from being delivered. Failed deliveries retry up to three attempts with exponential delay and remain auditable as `failed` after the final attempt. Delivery is at-least-once across a Worker crash between provider delivery and outbox acknowledgement, so providers may receive a duplicate.

The webhook adapter reports delivery success only when the HTTP response is 2xx and the provider business response is successful (`code == 0` for Feishu, `errcode == 0` for WeCom). Empty, malformed or provider-error responses are failed deliveries.

Each notifier should implement a small common interface such as:

```python
notify(event: NotificationEvent) -> NotificationResult
```

The event should contain Project/MR identity, recipient identity, title, URL, summary and correlation ID. Provider-specific mention markup stays inside the adapter.

## OpenCode and other Agents

OpenCode Web is an operator-facing local UI. Automated execution should use a CLI or a programmatic local endpoint such as OpenCode Server/ACP. Other tools (`claude`, `codex`, and future Agents) are supported through the generic configured CLI command before dedicated adapters are justified.

## Model layer

The core only defines an OpenAI-compatible Provider contract. LiteLLM is a deployment recommendation and should be documented as an external gateway, not imported by the core package.
