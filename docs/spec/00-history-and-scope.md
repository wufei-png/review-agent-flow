# History, OSS boundary and project rationale

## Origin

The source project was an internal Go service that handled GitLab Merge Request and Pipeline Webhooks, persisted workflow state in SQLite, notified reviewers through an enterprise chat channel, and applied organization-specific roles, metadata and review rules. Its implementation was tightly coupled to internal usernames, file-change assumptions, deployment credentials and business reporting.

That repository is a reference for behavior and failure modes, not a source tree for this project. `review-agent-flow` is a clean Python implementation with a new domain model, no internal credentials, no internal usernames, no private metadata schema and no copied business logic.

## Replacement research

No single open-source project covered the complete workflow. The nearest capabilities were deliberately treated as boundaries, not dependencies:

- GitLab native Approval Rules, Code Owners and reviewer assignment cover ownership and approval mechanics, but not the complete Agent/Note/Auto-fix/Feishu/WeCom workflow.
- Slack-oriented reminder tools cover notification schedules, but not the GitLab review execution model or local Agent contract.
- MR compliance tools cover metadata, title, description, approval and CODEOWNERS rules, but not Program-owned Agent result publication or Auto-fix MR lifecycle.
- Merge automation tools solve merge queues or automatic merging, which is explicitly outside this project's AI authority.
- Existing AI code-review projects are useful integration references, but their prompt, provider and deployment assumptions are not adopted as core dependencies.

The OSS value is therefore the generalized orchestration boundary: Project-owned requests, Operator-owned execution and publication authority, provider-neutral Agent execution, GitLab-native Review Notes, human/AI independent triggers, dual enterprise notification adapters and an auditable Auto-fix MR loop.

## Technology decisions

- Python was selected because the core workflow needs local Agent CLIs, AI ecosystem integration and fast iteration; Go is not required for the new core.
- The model layer remains only an OpenAI-compatible Provider interface. LiteLLM is recommended as an external gateway, not embedded as a mandatory dependency.
- OpenCode Web is an operator-facing local UI; automated execution uses a generic CLI or a programmatic local endpoint. The same Agent Backend boundary can support OpenCode, Claude, Codex and future tools.
- The first deployment is intentionally single-node with API, one supervised Worker OS process, SQLite and database polling. The Worker hosts independent Review/Publication, Notification and Reconciliation/Cleanup loops. Postgres/Redis and separate worker processes are extension points after the workflow contract and scaling need stabilize.
