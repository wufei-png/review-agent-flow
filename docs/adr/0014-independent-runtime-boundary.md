---
status: accepted
date: 2026-08-02
---

# Keep ReviewFlow's runtime boundary independent

ReviewFlow keeps an independently versioned and dependency-managed runtime boundary for Project/MR/SHA review orchestration, Effect Receipt, and Worker Lease semantics; it does not extract a cross-repository Python runtime library from the overlap with Git Evidence. ReviewFlow's review and execution concepts are distinct from Git Evidence's repository/provider/capability/coverage/publication semantics, so similar redaction, retry, and diagnostic code remains local rather than defining a shared security boundary. Reconsider extraction only when at least two real consumers exist, the semantic contract is versioned and stable, shared tests demonstrate no change to either domain's semantics, and an independent release and rollback path exists. Until then, only documentation-level candidate concepts or thin protocols may be shared; no Python runtime dependency is introduced.
