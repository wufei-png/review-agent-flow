# Store backup and restore

Use this procedure for the single Project, schema 17 deployment. The bundle is
private: its SQLite snapshot can contain Review content, notification fields,
Policy Snapshots, credentials accidentally persisted in old diagnostics, and
canonical Publication Plans. Do not put a bundle or its manifest in Git.

## Create and verify a bundle

Run as the Worker identity. Create an empty, Worker-owned mode-`0700` output
parent on the protected backup medium. The `--db` value is the active Store;
`--plan-backup-dir` is the exact Operator-configured Plan directory. The output
name must not already exist.

```bash
review-agent-flow backup-store \
  --db /var/lib/review-agent-flow/shared/review-agent-flow.sqlite3 \
  --plan-backup-dir /var/lib/review-agent-flow-worker/plan-backups \
  --output /private/reviewflow-bundles/2026-09-21T000000Z
review-agent-flow verify-backup \
  --bundle /private/reviewflow-bundles/2026-09-21T000000Z
```

`backup-store` uses SQLite's backup API while WAL writers may continue. Its
manifest records the application version, schema version, snapshot time,
database digest and length, and every snapshot receipt's Plan ID, digest,
original absolute path, bundle-relative path and length. Only the Plan files
referenced by that snapshot are copied. The command checks owner, mode,
canonical bytes and digests, fsyncs the files and directories, then publishes
the complete bundle by same-parent rename. `verify-backup` checks the private
mode of every bundle file and directory, exact manifest layout, SQLite
integrity/schema, and the complete receipt-to-file mapping. It makes no GitLab
or notification calls. Keep the original bundle protected and verify it again
after transfer to the recovery host.

## Restore on an isolated host

1. Stop both `review-agent-flow-api` and `review-agent-flow-worker`; confirm
   neither process remains. Do not start either unit until all checks below
   pass. Use a fresh recovery directory and the same deployment paths. There
   must be no existing database, `-wal` or `-shm` file, or destination Plan
   file. This is a complete Store replacement, never a merge with a running
   Store.
2. As the Worker identity, verify the transferred bundle. Check that its
   `schema_version` is supported and its `snapshot_at` is the intended restore
   point. Review the manifest's `source_path` values: each must be a direct
   child of the configured absolute Plan directory. Create that directory as
   Worker-owned mode `0700`; create the shared Store directory with the
   deployment owner/group and mode `2770` described in
   [deployment-linux-systemd.md](deployment-linux-systemd.md).
3. Copy `store.sqlite3` to the configured Store path and each `plans/*.json`
   file to its manifest `source_path`, without overwriting any target. The
   Store file must be owned by `reviewflow-worker:reviewflow-shared` with mode
   `0660` so both services can use it. Plan files must be Worker-owned mode
   `0600`, and their directory mode `0700`. Fsync restored files and their
   parent directories. Do not rewrite the immutable receipt paths in SQLite;
   a host that cannot recreate them is not an equivalent recovery target.
4. Re-run `verify-backup` on the bundle. Compare restored file SHA-256 and byte
   length against each manifest entry. Check owner and mode, run SQLite
   `PRAGMA integrity_check` and confirm schema 17 on the restored database.
   Query representative ReviewKeys with read-only `status`, then `ops-status`;
   inspect a sample Policy Snapshot, Publication Plan/Relation, notification
   delivery and Operator audit in a read-only SQLite session. Confirm every
   `publication_plan_backups.backup_path` exists at its exact absolute path
   and matches its Plan digest. Keep sensitive values out of the evidence log.
5. Only after those checks pass, start the API and Worker, check `/readyz`,
   Worker ownership and provider behavior using the deployment canary plan.

An interrupted restore leaves an incomplete recovery target. Discard that
target and repeat in another empty directory. A failed `verify-backup` is a
hard stop: do not restore from the damaged bundle.

Record the bundle's application/schema version, snapshot time, verification
result, restored file count, sampled identity counts and command results in a
private operations log. Public evidence may contain only redacted counts and
timestamps. A local temporary-directory rehearsal proves the offline tool
contract; it does not prove the backup medium, Unix identities, systemd stop
and restart, JiHuLab, OpenCode, or notification delivery.
