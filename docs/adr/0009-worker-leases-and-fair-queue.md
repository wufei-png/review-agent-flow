# Worker leases and a fair SQLite queue

Pipeline Gate and Draft checks are temporary eligibility conditions, not terminal failures. A Run that is waiting for a successful Pipeline or for a Draft MR to become ready remains `pending` with a future `next_attempt_at`. The Worker claims the oldest eligible pending Run, so a deferred older Run does not hold the queue ahead of later eligible Runs.

The Store assigns every `running` Run an expiration and an opaque claim token. A Worker must present that token when finishing or deferring the Run; a stale Worker cannot overwrite a Run that another Worker has reclaimed. The next claim transaction recovers an expired lease. Recovery has its own bounded counter and fails the Run after three lease expirations by default. Retryable provider/runtime failures use a separate bounded failure counter. Claim count remains diagnostic only: Draft and Pipeline eligibility deferrals may claim a Run repeatedly without consuming either failure budget. Agent contract failures remain terminal and do not use lease recovery as an implicit retry policy.

The lease and claim token are kept in SQLite so the behavior survives Worker process crashes and works with the reference single-Worker deployment. Postgres/Redis implementations must preserve the same claim, eligibility and stale-token semantics.
