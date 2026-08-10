# Agent and Provider contract

## Separation of authority

`AIProvider` and `AgentBackend` are different abstractions:

- `AIProvider` is a model inference endpoint using an OpenAI-compatible API shape.
- `AgentBackend` is an Operator-authorized executable workflow such as a local CLI or OpenCode service.
- A Project selects an allowed backend/profile and may supply review instructions; it never supplies executable commands or credentials.
- The Agent produces review content and an optional workspace diff. ReviewFlow alone publishes Notes, commits, branches and Merge Requests.

The core does not ship vendor-specific model SDKs. LiteLLM may be used as an external model gateway.

## Invocation

Before execution, ReviewFlow freezes the instruction-bearing `AgentTaskV1` snapshot once per Review Run. The durable snapshot contains the ReviewKey, untrusted MR title and branch presentation, exact GitLab `base_sha`, `start_sha` and `head_sha`, and the selected Skill's source path, bounded content and SHA-256 digest. A Project Skill is read only as a regular Git blob from the already-frozen target-branch commit; it is never read from the MR-head worktree. The packaged default is an Operator-owned regular file. Both sources are strict UTF-8, reject symlinks/NUL bytes and are limited to 64 KiB. Retries reuse these exact bytes.

Each attempt materializes those frozen Skill bytes and the controlled Result Path under a private mode-`0700` Run directory. The canonical prompt embeds the complete Skill, records its source/digest and labels MR data as untrusted; it requires review of `base_sha...head_sha` and reserves every GitLab, notification, commit and push effect to ReviewFlow. The bounded prompt remains below the per-argument limit required by the reference CLI invocation.

All Backend adapters consume that same canonical task. An adapter may map it to its CLI's native non-interactive invocation, but cannot add domain authority or change the Result contract. The generic Operator-command adapter receives a private task file; the OpenCode reference adapter passes the canonical prompt to `opencode run`. Codex and Claude Code may be added later as peer adapters without changing the core task.

The Worker invokes the selected Operator command with `shell=False` and the exact-SHA Trusted Workspace as `cwd`. It creates `REVIEW_AGENT_FLOW_RESULT_PATH` outside that workspace with restrictive permissions. The generic command compatibility surface supplies these ReviewFlow variables, the profile's explicit environment allowlist, and the Operator-owned execution `PATH`:

| Variable | Meaning |
| --- | --- |
| `REVIEW_AGENT_FLOW_PROJECT_ID` | GitLab Project identifier |
| `REVIEW_AGENT_FLOW_MR_IID` | Parent Merge Request IID |
| `REVIEW_AGENT_FLOW_RUN_ID` | Local trace identifier; not publication identity |
| `REVIEW_AGENT_FLOW_SHA` | Exact reviewed commit and required unchanged `HEAD` |
| `REVIEW_AGENT_FLOW_SKILL` | Private mode-`0600` materialization of the frozen Skill bytes |
| `REVIEW_AGENT_FLOW_AUTO_FIX_ENABLED` | `true` only when Project request and Operator authorization both allow a proposal |
| `REVIEW_AGENT_FLOW_RESULT_PATH` | Controlled path for the single structured result |
| `REVIEW_AGENT_FLOW_TASK_PATH` | Mode-`0600` file containing the canonical `AgentTaskV1` prompt |

ReviewFlow does not expose the GitLab token, publication marker, commit message or readiness to the Agent. Title and branch fields are untrusted review context only and convey no executable or publication authority. Stdout and stderr are bounded diagnostics only and never carry the authoritative result.

## Structured result v1

On success the Agent atomically writes one bounded UTF-8 JSON object to `REVIEW_AGENT_FLOW_RESULT_PATH`:

```json
{
  "version": 1,
  "review_markdown": "Actionable review text.",
  "autofix_proposal": {
    "summary": "What the proposed workspace changes fix."
  }
}
```

`version` must equal `1`. `review_markdown` is required and must be a non-empty string. `autofix_proposal` is optional; when present, `summary` is required and the workspace must contain a non-empty eligible diff. Unknown fields, malformed JSON, missing files, symlinks, oversized files and unsupported versions are contract failures. A non-zero exit or timeout publishes neither Note nor Auto-fix.

ReviewFlow publishes `review_markdown` as raw GitLab Markdown after adding its own marker. It does not sanitize Markdown. Deployment permissions therefore remain the control for GitLab quick actions and other account-level effects.

## Trusted Workspace and diff envelope

The Agent starts in a detached worktree at the exact ReviewKey SHA. It must not change `HEAD`, create commits, push branches, call GitLab or create an MR. When Auto-fix is disabled or `autofix_proposal` is absent, it must leave the workspace unchanged.

For an Auto-fix proposal, ReviewFlow captures tracked and untracked changes only after confirming `HEAD` still equals the ReviewKey SHA. The accepted envelope is bounded regular, non-binary, UTF-8-compatible text add/modify/delete/chmod actions. Commits, symlinks, gitlinks/submodules, path traversal, binary content and oversized per-file or aggregate output are rejected. An empty diff, or a mismatch between proposal presence and diff presence, blocks Auto-fix publication as a contract error. A valid `review_markdown` remains independently publishable when Auto-fix validation fails.

An Operator verifier runs after capture and before Plan freeze. ReviewFlow records a canonical digest of the captured candidate, runs the verifier against that candidate, then rechecks `HEAD` and recaptures the complete Diff Envelope. The post-verifier candidate must exactly equal the captured digest before the verified actions can be frozen into the Plan. No configured verifier, verifier failure, candidate mutation or unverifiable output prevents Auto-fix publication. The verifier is not controlled by Project content.

The Trusted Workspace is an execution boundary, not a security sandbox. The Operator Backend Profile remains responsible for process, filesystem, host and network isolation.

## Default Skill responsibilities

The packaged Skill tells an Agent to:

1. inspect the exact worktree and MR context;
2. review correctness, security, reliability and maintainability;
3. write the strict result JSON at `REVIEW_AGENT_FLOW_RESULT_PATH`;
4. optionally make only safe proposal edits when Auto-fix is enabled;
5. leave `HEAD` unchanged and never commit, push, publish, notify, approve or block.

A valid regular Project `.gitlab/review-agent/SKILL.md` at the frozen target commit replaces the packaged review instructions but cannot replace the runtime or publication authority. Missing configured paths fall back to the packaged Skill; invalid or oversized objects fail closed.
