---
status: accepted
date: 2026-08-08
---

# Require explicit Operator recovery for lost Auto-fix relations

Normal Auto-fix identity continues to require a Store-owned Intent or relation;
GitLab artifacts are not adopted automatically after Store loss. An Operator
may explicitly recover a relation only after ReviewFlow verifies one unique
Merge Request from the configured Review Bot with the exact Auto-fix identity marker,
expected Project, parent MR and SHA, and expected source and target branches.
The recovery is recorded as an auditable action rather than inferred from a
Webhook or background reconciliation.

The canonical standalone Auto-fix marker includes both the Review Key and an
opaque random Intent nonce:

`<!-- reviewflow:v1;project=<project-id>;mr=<parent-iid>;sha=<parent-sha>;kind=<kind>;effect=autofix;intent=<nonce> -->`

The canonical source branch is independently derivable from the same marker:

`autofix/MR-<parent-iid>/sha-<parent-sha>/intent-<nonce>`

Recovery validates that exact branch and marker together; it does not derive
identity from a replacement Run identifier.

Existing `ReviewFlow-Auto-Fix-Run-ID:<run-id> SHA:<sha>` markers remain valid
only while their Store-owned Intent or relation exists; they are not sufficient
for recovery after Store loss. The new marker and explicit recovery command are
accepted target behavior and are not yet implemented.
