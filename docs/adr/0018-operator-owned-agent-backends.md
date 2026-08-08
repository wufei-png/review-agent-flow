---
status: accepted
date: 2026-08-08
---

# Make Agent Backends Operator-owned

Project Policy selects an Operator-authorized Agent Backend and optional
profile; it does not provide executable argv or a command template. The
Operator owns each backend invocation and its Project allowlist, environment,
timeout and process constraints. This retains Agent-owned GitLab output for a
Trusted Project Deployment without letting repository configuration define
arbitrary host commands. The pre-release Policy contract moves directly to
schema version 3 with `ai.backend` and optional `ai.profile`; version 2
`ai.command` policies are rejected without a legacy compatibility path.
This is an accepted target contract; the current pre-release implementation
still loads schema version 2 and must migrate the loader, runtime wiring,
template, executable specification and tests together.
