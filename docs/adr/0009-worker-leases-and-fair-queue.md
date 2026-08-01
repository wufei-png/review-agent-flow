# Worker leases and a fair SQLite queue

Pipeline Gate and Draft checks are temporary eligibility conditions, not terminal failures. A Run that is waiting for a successful Pipeline or for a Draft MR to become ready remains `pending` with a future `next_attempt_at`. The Worker claims the oldest eligible pending Run, so a deferred older Run does not hold the queue ahead of later eligible Runs.

The Store assigns every `running` Run an expiration and an opaque claim token. A Worker must present that token when finishing or deferring the Run; a stale Worker cannot overwrite a Run that another Worker has reclaimed. The next claim transaction recovers an expired lease. Recovery is automatic for at most three claims, after which the Run is failed with a lease-expiry diagnostic. Agent process failures remain terminal failures and do not use lease recovery as an implicit retry policy.

The lease and claim token are kept in SQLite so the behavior survives Worker process crashes and works with the reference single-Worker deployment. Postgres/Redis implementations must preserve the same claim, eligibility and stale-token semantics.
