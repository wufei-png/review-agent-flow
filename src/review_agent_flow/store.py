"""SQLite persistence for idempotent Review Runs and their side effects."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

from .agent_task import MAX_MR_TITLE_CHARS, AgentTaskSnapshot
from .gitlab import canonical_note_marker, has_exact_note_marker, is_valid_sha, normalize_sha
from .publication_plan import PublicationPlanV1
from .security import redact
from .skill_source import validate_skill_content

DEFAULT_LEASE_SECONDS = 1860
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_NOTIFICATION_LEASE_SECONDS = 30
DEFAULT_NOTIFICATION_MAX_ATTEMPTS = 3
DEFAULT_WORKTREE_CLEANUP_LEASE_SECONDS = 60
DEFAULT_WORKTREE_CLEANUP_MAX_ATTEMPTS = 5
DEFAULT_WORKER_RUNTIME_LEASE_SECONDS = 30
SCHEMA_VERSION = 17
DEFAULT_EFFECT_LEASE_SECONDS = 60
DEFAULT_NOTE_PUBLICATION_LEASE_SECONDS = 60
DEFAULT_NOTE_PUBLICATION_MAX_ATTEMPTS = 5
DEFAULT_NOTE_UNCERTAIN_LOOKUP_ATTEMPTS = 3
DEFAULT_AUTO_FIX_PUBLICATION_LEASE_SECONDS = 60
DEFAULT_AUTO_FIX_RECONCILIATION_LEASE_SECONDS = 60
AUTO_FIX_MR_ABSENCE_CONFIRMATION_SECONDS = 5


@dataclass(frozen=True)
class ReviewRun:
    run_id: int
    project_id: str
    mr_iid: int
    sha: str
    status: str
    attempts: int = 0
    failure_attempts: int = 0
    lease_recoveries: int = 0
    lease_until: str | None = None
    claim_token: str | None = None
    failure_class: str | None = None
    retryable: bool = False
    error: str | None = None
    next_attempt_at: str | None = None
    created_at: str | None = None
    finished_at: str | None = None


@dataclass(frozen=True)
class PolicySnapshot:
    run_id: int
    target_branch: str
    target_sha: str
    policy_version: int
    policy_json: str
    policy_sha256: str
    created_at: str


@dataclass(frozen=True)
class RequestedOutputs:
    run_id: int
    human_review: bool
    ai_review: bool
    auto_fix: bool
    notification_channels: tuple[str, ...]
    created_at: str


@dataclass(frozen=True)
class EffectiveRunDecision:
    run_id: int
    decision: str
    human_review: bool
    ai_review: bool
    auto_fix: bool
    suppressed: bool
    recipients: tuple[str, ...]
    notification_channels: tuple[str, ...]
    reason: str | None
    created_at: str


@dataclass(frozen=True)
class OutputExpectation:
    expectation_id: int
    run_id: int
    output_kind: str
    dedupe_key: str
    created_at: str


@dataclass(frozen=True)
class FailureRecord:
    failure_id: int
    run_id: int
    attempt: int
    failure_class: str
    retryable: bool
    error: str | None
    recorded_at: str


@dataclass(frozen=True)
class EffectReceipt:
    receipt_id: int
    run_id: int
    effect_kind: str
    dedupe_key: str
    attempt: int
    state: str
    external_identity: str | None
    detail: str | None
    created_at: str
    updated_at: str
    error: str | None
    next_attempt_at: str | None = None
    lease_until: str | None = None
    claim_token: str | None = None


@dataclass(frozen=True)
class NotePublicationRequest:
    request_id: int
    run_id: int
    receipt_id: int
    project_id: str
    mr_iid: int
    sha: str
    marker: str
    body: str
    state: str
    attempts: int
    write_uncertain: bool
    external_note_id: int | None
    next_attempt_at: str | None
    lease_until: str | None
    claim_token: str | None
    created_at: str
    updated_at: str
    last_error: str | None
    reconcile_attempts: int
    reconcile_next_at: str | None
    reconcile_lease_until: str | None
    reconcile_claim_token: str | None


@dataclass(frozen=True)
class AutoFixPublicationIntent:
    intent_id: int
    run_id: int
    project_id: str
    mr_iid: int
    sha: str
    generation: int
    nonce: str
    state: str
    created_at: str
    updated_at: str
    last_error: str | None


@dataclass(frozen=True)
class AutoFixPublicationPlan:
    plan_id: int
    intent_id: int
    run_id: int
    plan_json: str | None
    plan_sha256: str
    marker: str
    candidate_sha256: str
    verification_json: str
    state: str
    created_at: str
    updated_at: str
    attempts: int
    absence_confirmations: int
    absence_observed_at: str | None
    write_phase: str
    write_uncertain: bool
    branch_commit_sha: str | None
    external_mr_iid: int | None
    next_attempt_at: str | None
    lease_until: str | None
    claim_token: str | None
    last_error: str | None


@dataclass(frozen=True)
class ProgramAutoFixRelation:
    relation_id: int
    intent_id: int
    plan_id: int
    run_id: int
    parent_project_id: str
    parent_mr_iid: int
    parent_sha: str
    auto_fix_project_id: str
    auto_fix_mr_iid: int
    source_branch: str
    target_branch: str
    marker: str
    plan_sha256: str
    published_commit_sha: str
    status: str
    merged_sha: str | None
    divergence: str | None
    reconcile_requested: bool
    reconcile_attempts: int
    reconcile_next_at: str | None
    reconcile_lease_until: str | None
    reconcile_claim_token: str | None
    last_error: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class NotificationRequest:
    request_id: int
    relation_id: int
    generation: int
    request_kind: str
    correlation_id: str
    title: str
    body: str
    url: str | None
    recipients: tuple[str, ...]
    created_at: str


@dataclass(frozen=True)
class OperatorRecovery:
    audit_id: int
    recovery_id: str
    action: str
    relation_id: int | None
    project_id: str
    parent_mr_iid: int
    parent_sha: str
    intent_nonce: str
    plan_sha256: str
    child_project_id: str
    child_mr_iid: int
    reason: str
    principal: str
    hostname: str
    tool_version: str
    result: str
    diagnostic: str | None
    created_at: str


@dataclass(frozen=True)
class OperatorPlanAction:
    audit_id: int
    action_id: str
    plan_id: int
    plan_sha256: str
    action: str
    from_state: str
    to_state: str
    write_phase: str
    reason: str
    principal: str
    hostname: str
    tool_version: str
    result: str
    diagnostic: str | None
    created_at: str


@dataclass(frozen=True)
class PublicationPlanBackupCandidate:
    plan_id: int
    plan_sha256: str
    plan_json: str


@dataclass(frozen=True)
class SensitivePayloadPurgeResult:
    purge_id: int
    cutoff_at: str
    plan_payloads: int
    agent_task_payloads: int
    note_payloads: int
    notification_payloads: int
    diagnostic_payloads: int
    created_at: str


@dataclass(frozen=True)
class NotificationDelivery:
    delivery_id: int
    correlation_id: str
    channel_id: str
    channel: str
    title: str
    body: str
    url: str | None
    recipients: tuple[str, ...]
    status: str
    attempts: int
    next_attempt_at: str | None
    lease_until: str | None
    claim_token: str | None
    created_at: str
    delivered_at: str | None
    terminal_at: str | None
    last_error: str | None


@dataclass(frozen=True)
class WorktreeRecord:
    cleanup_id: int
    run_id: int
    path: str
    sha: str
    status: str
    attempts: int
    next_attempt_at: str | None
    lease_until: str | None
    claim_token: str | None
    created_at: str
    cleaned_at: str | None
    last_error: str | None


class SQLiteStore:
    """Small SQLite store.

    Version 17 is intentionally a fresh schema.  There is no production database
    migration contract for this pre-release implementation; an older database
    must be recreated rather than guessed into a new notification or Auto-fix
    routing model.
    """

    def __init__(
        self,
        path: str | Path,
        redaction_secrets: Iterable[str] | None = None,
        read_only: bool = False,
    ) -> None:
        self.path = str(path)
        self._read_only = read_only
        self._redaction_secrets: set[str] = set()
        if isinstance(redaction_secrets, str):
            self.add_redaction_secret(redaction_secrets)
        elif redaction_secrets is not None:
            for secret in redaction_secrets:
                self.add_redaction_secret(secret)
        if self._read_only:
            if not Path(self.path).is_file():
                raise FileNotFoundError(self.path)
            self._validate_schema_version()
        else:
            self._initialize()

    def add_redaction_secret(self, secret: str | None) -> None:
        """Register a runtime credential before writing audit diagnostics."""

        if secret and not secret.isspace():
            self._redaction_secrets.add(secret)

    def _redact(self, value: object, max_length: int = 4000) -> str:
        return redact(
            value,
            max_length=max_length,
            secrets=tuple(self._redaction_secrets),
        )

    def _connect(self) -> sqlite3.Connection:
        if self._read_only:
            database_uri = f"file:{quote(str(Path(self.path).resolve()), safe='/')}?mode=ro"
            connection = sqlite3.connect(database_uri, timeout=30, uri=True)
        else:
            connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        if not self._read_only:
            connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def readiness_probe(self) -> dict[str, str | None]:
        """Run a bounded, non-destructive SQLite read/write health probe.

        The write is confined to a temporary table owned by this connection,
        so readiness does not insert, update, or delete application records.
        ``quick_check`` covers the persistent database read path while the
        temporary table covers the connection's write path.
        """

        try:
            with self._connect() as connection:
                quick_check = connection.execute("PRAGMA quick_check(1)").fetchone()
                if quick_check is None or str(quick_check[0]).lower() != "ok":
                    detail = str(quick_check[0]) if quick_check is not None else "no result"
                    return {"status": "not_ready", "diagnostic": f"sqlite quick_check: {detail}"}
                connection.execute("SAVEPOINT reviewflow_readiness_probe")
                connection.execute(
                    "CREATE TEMP TABLE reviewflow_readiness_probe (value INTEGER NOT NULL)"
                )
                connection.execute("INSERT INTO reviewflow_readiness_probe(value) VALUES (1)")
                row = connection.execute(
                    "SELECT value FROM reviewflow_readiness_probe WHERE value = 1"
                ).fetchone()
                connection.execute("DROP TABLE reviewflow_readiness_probe")
                connection.execute("RELEASE SAVEPOINT reviewflow_readiness_probe")
                if row is None:
                    return {
                        "status": "not_ready",
                        "diagnostic": "sqlite probe read returned no row",
                    }
        except (OSError, sqlite3.Error) as exc:
            return {"status": "not_ready", "diagnostic": self._redact(exc, max_length=1000)}
        return {"status": "ok", "diagnostic": None}

    def _validate_schema_version(self) -> None:
        with self._connect() as connection:
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version != SCHEMA_VERSION:
            raise RuntimeError(f"unsupported SQLite schema version {version}; recreate {self.path}")

    def acquire_worker_lease(
        self,
        owner_id: str,
        lease_seconds: int = DEFAULT_WORKER_RUNTIME_LEASE_SECONDS,
    ) -> str | None:
        """Claim the single Worker slot; expired owners are replaced atomically."""
        if not owner_id.strip() or lease_seconds <= 0:
            raise ValueError("Worker owner and positive lease are required")
        now = self._now()
        now_text = now.isoformat()
        token = uuid4().hex
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            claimed = connection.execute(
                """
                INSERT INTO worker_runtime_lease(
                    singleton_id, owner_id, claim_token, heartbeat_at, lease_until
                ) VALUES (1, ?, ?, ?, ?)
                ON CONFLICT(singleton_id) DO UPDATE SET
                    owner_id = excluded.owner_id,
                    claim_token = excluded.claim_token,
                    heartbeat_at = excluded.heartbeat_at,
                    lease_until = excluded.lease_until
                WHERE worker_runtime_lease.lease_until <= ?
                """,
                (
                    owner_id, token, now_text,
                    (now + timedelta(seconds=lease_seconds)).isoformat(), now_text,
                ),
            )
        return token if claimed.rowcount == 1 else None

    def heartbeat_worker_lease(
        self,
        claim_token: str,
        lease_seconds: int = DEFAULT_WORKER_RUNTIME_LEASE_SECONDS,
    ) -> bool:
        if not claim_token or lease_seconds <= 0:
            raise ValueError("Worker claim token and positive lease are required")
        now = self._now()
        now_text = now.isoformat()
        with self._connect() as connection:
            updated = connection.execute(
                """
                UPDATE worker_runtime_lease
                SET heartbeat_at = ?, lease_until = ?
                WHERE singleton_id = 1 AND claim_token = ? AND lease_until > ?
                """,
                (now_text, (now + timedelta(seconds=lease_seconds)).isoformat(),
                 claim_token, now_text),
            )
        return updated.rowcount == 1

    def has_worker_lease(self, claim_token: str) -> bool:
        if not claim_token:
            return False
        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM worker_runtime_lease WHERE singleton_id = 1 "
                "AND claim_token = ? AND lease_until > ?",
                (claim_token, self._now().isoformat()),
            ).fetchone()
        return row is not None

    def release_worker_lease(self, claim_token: str) -> bool:
        if not claim_token:
            return False
        with self._connect() as connection:
            deleted = connection.execute(
                "DELETE FROM worker_runtime_lease WHERE singleton_id = 1 AND claim_token = ?",
                (claim_token,),
            )
        return deleted.rowcount == 1

    def worker_lease_summary(self) -> dict[str, str] | None:
        """Return operator-safe ownership facts, never the claim token."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT owner_id, heartbeat_at, lease_until FROM worker_runtime_lease "
                "WHERE singleton_id = 1"
            ).fetchone()
        return dict(row) if row is not None else None

    @staticmethod
    def _age_seconds(created_at: str | None, now: datetime) -> int | None:
        if created_at is None:
            return None
        return max(0, int((now - datetime.fromisoformat(created_at)).total_seconds()))

    def get_project_ops_status(self, project_id: str) -> dict[str, object]:
        """Read bounded, project-scoped operational aggregates without claiming work."""
        if not project_id or not project_id.strip():
            raise ValueError("project_id cannot be empty")
        now = self._now()
        now_text = now.isoformat()
        with self._connect() as connection:
            worker = connection.execute(
                "SELECT owner_id, heartbeat_at, lease_until FROM worker_runtime_lease "
                "WHERE singleton_id = 1"
            ).fetchone()
            runs = connection.execute(
                """SELECT
                     SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) AS pending,
                     SUM(CASE WHEN status = 'running' THEN 1 ELSE 0 END) AS running,
                     MIN(CASE WHEN status = 'pending' THEN created_at END) AS oldest_pending,
                     SUM(CASE WHEN status = 'running' AND
                         (lease_until IS NULL OR lease_until <= ?) THEN 1 ELSE 0 END)
                         AS expired_leases
                   FROM review_runs WHERE project_id = ?""",
                (now_text, project_id),
            ).fetchone()
            notes = connection.execute(
                """SELECT
                     SUM(CASE WHEN n.state = 'blocked' THEN 1 ELSE 0 END) AS blocked,
                     SUM(CASE WHEN n.write_uncertain = 1 AND n.state NOT IN
                         ('published', 'removed', 'superseded') THEN 1 ELSE 0 END) AS uncertain
                   FROM note_publication_requests n
                   JOIN review_runs r ON r.id = n.run_id WHERE r.project_id = ?""",
                (project_id,),
            ).fetchone()
            plans = connection.execute(
                """SELECT
                     SUM(CASE WHEN p.state IN ('needs_reconciliation', 'reconciling')
                         OR (p.state = 'running' AND p.write_phase != 'preflight')
                         THEN 1 ELSE 0 END) AS reconciliation,
                     SUM(CASE WHEN p.state = 'blocked' THEN 1 ELSE 0 END) AS blocked
                   FROM auto_fix_publication_plans p
                   JOIN review_runs r ON r.id = p.run_id WHERE r.project_id = ?""",
                (project_id,),
            ).fetchone()
            notifications = connection.execute(
                """SELECT
                     SUM(CASE WHEN o.status = 'pending' THEN 1 ELSE 0 END) AS pending,
                     SUM(CASE WHEN o.status = 'running' THEN 1 ELSE 0 END) AS running,
                     SUM(CASE WHEN o.status = 'failed' THEN 1 ELSE 0 END) AS failed,
                     MIN(CASE WHEN o.status = 'pending' THEN o.created_at END)
                         AS oldest_pending
                   FROM notification_outbox o
                   LEFT JOIN notification_requests n ON n.correlation_id = o.correlation_id
                   LEFT JOIN auto_fix_relations a ON a.id = n.relation_id
                   LEFT JOIN review_runs r ON r.id = a.run_id
                   WHERE o.correlation_id IN (
                       SELECT 'review-run:' || id || ':human' FROM review_runs
                       WHERE project_id = ?
                   ) OR r.project_id = ?""",
                (project_id, project_id),
            ).fetchone()
            divergence = connection.execute(
                """SELECT COUNT(*) FROM auto_fix_relations a
                   JOIN review_runs r ON r.id = a.run_id
                   WHERE r.project_id = ? AND a.divergence IS NOT NULL""",
                (project_id,),
            ).fetchone()[0]
            purge = connection.execute(
                "SELECT created_at FROM sensitive_payload_purges ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return {
            "project_id": project_id,
            "observed_at": now_text,
            "worker": {
                "scope": "deployment",
                "state": "absent" if worker is None else
                "active" if worker["lease_until"] > now_text else "expired",
                "owner_id": worker["owner_id"] if worker is not None else None,
                "heartbeat_at": worker["heartbeat_at"] if worker is not None else None,
                "lease_until": worker["lease_until"] if worker is not None else None,
            },
            "runs": {
                "pending": runs["pending"] or 0,
                "running": runs["running"] or 0,
                "oldest_pending_age_seconds": self._age_seconds(runs["oldest_pending"], now),
                "expired_leases": runs["expired_leases"] or 0,
            },
            "notes": {"blocked": notes["blocked"] or 0, "uncertain": notes["uncertain"] or 0},
            "auto_fix": {
                "reconciliation": plans["reconciliation"] or 0,
                "blocked": plans["blocked"] or 0,
                "relation_divergence": divergence,
            },
            "notifications": {
                "pending": notifications["pending"] or 0,
                "running": notifications["running"] or 0,
                "failed": notifications["failed"] or 0,
                "oldest_pending_age_seconds": self._age_seconds(
                    notifications["oldest_pending"], now
                ),
            },
            "retention": {
                "scope": "deployment",
                "last_purge_at": purge["created_at"] if purge is not None else None,
                "last_sweep_result": "unavailable",
            },
        }

    def _initialize(self) -> None:
        with self._connect() as connection:
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            existing_tables = {
                row[0]
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            }
            if existing_tables and version != SCHEMA_VERSION:
                raise RuntimeError(
                    f"unsupported SQLite schema version {version}; recreate {self.path}"
                )
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS review_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_id TEXT NOT NULL,
                    mr_iid INTEGER NOT NULL,
                    sha TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at TEXT NOT NULL,
                    finished_at TEXT,
                    error TEXT,
                    next_attempt_at TEXT,
                    lease_until TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    failure_attempts INTEGER NOT NULL DEFAULT 0,
                    lease_recoveries INTEGER NOT NULL DEFAULT 0,
                    claim_token TEXT,
                    failure_class TEXT,
                    retryable INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(project_id, mr_iid, sha)
                );
                CREATE TABLE IF NOT EXISTS policy_snapshots (
                    run_id INTEGER PRIMARY KEY REFERENCES review_runs(id),
                    target_branch TEXT NOT NULL,
                    target_sha TEXT NOT NULL,
                    policy_version INTEGER NOT NULL,
                    policy_json TEXT NOT NULL,
                    policy_sha256 TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS agent_task_snapshots (
                    run_id INTEGER PRIMARY KEY REFERENCES review_runs(id),
                    project_id TEXT NOT NULL,
                    mr_iid INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    source_project_id TEXT NOT NULL,
                    source_branch TEXT NOT NULL,
                    target_branch TEXT NOT NULL,
                    base_sha TEXT NOT NULL,
                    start_sha TEXT NOT NULL,
                    head_sha TEXT NOT NULL,
                    skill_source_path TEXT NOT NULL,
                    skill_content TEXT,
                    skill_sha256 TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    payload_purged_at TEXT
                );
                CREATE TABLE IF NOT EXISTS requested_outputs (
                    run_id INTEGER PRIMARY KEY REFERENCES review_runs(id),
                    human_review INTEGER NOT NULL,
                    ai_review INTEGER NOT NULL,
                    auto_fix INTEGER NOT NULL,
                    notification_channels_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS effective_run_decisions (
                    run_id INTEGER PRIMARY KEY REFERENCES review_runs(id),
                    decision TEXT NOT NULL,
                    human_review INTEGER NOT NULL,
                    ai_review INTEGER NOT NULL,
                    auto_fix INTEGER NOT NULL,
                    suppressed INTEGER NOT NULL,
                    recipients_json TEXT NOT NULL,
                    notification_channels_json TEXT NOT NULL,
                    reason TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS output_expectations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id INTEGER NOT NULL REFERENCES review_runs(id),
                    output_kind TEXT NOT NULL,
                    dedupe_key TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(run_id, output_kind, dedupe_key)
                );
                CREATE TABLE IF NOT EXISTS auto_fix_publication_intents (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id INTEGER NOT NULL UNIQUE REFERENCES review_runs(id),
                    project_id TEXT NOT NULL,
                    mr_iid INTEGER NOT NULL,
                    sha TEXT NOT NULL,
                    generation INTEGER NOT NULL,
                    nonce TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL DEFAULT 'pending',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_error TEXT,
                    UNIQUE(project_id, mr_iid, sha, generation)
                );
                CREATE TABLE IF NOT EXISTS auto_fix_publication_plans (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    intent_id INTEGER NOT NULL UNIQUE
                        REFERENCES auto_fix_publication_intents(id),
                    run_id INTEGER NOT NULL UNIQUE REFERENCES review_runs(id),
                    plan_json TEXT,
                    plan_sha256 TEXT NOT NULL UNIQUE,
                    marker TEXT NOT NULL UNIQUE,
                    candidate_sha256 TEXT NOT NULL,
                    verification_json TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'frozen',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    absence_confirmations INTEGER NOT NULL DEFAULT 0,
                    absence_observed_at TEXT,
                    write_phase TEXT NOT NULL DEFAULT 'preflight',
                    write_uncertain INTEGER NOT NULL DEFAULT 0,
                    branch_commit_sha TEXT,
                    external_mr_iid INTEGER,
                    next_attempt_at TEXT,
                    lease_until TEXT,
                    claim_token TEXT,
                    payload_purged_at TEXT,
                    last_error TEXT
                );
                CREATE TABLE IF NOT EXISTS auto_fix_relations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    intent_id INTEGER NOT NULL UNIQUE
                        REFERENCES auto_fix_publication_intents(id),
                    plan_id INTEGER NOT NULL UNIQUE
                        REFERENCES auto_fix_publication_plans(id),
                    run_id INTEGER NOT NULL UNIQUE REFERENCES review_runs(id),
                    parent_project_id TEXT NOT NULL,
                    parent_mr_iid INTEGER NOT NULL,
                    parent_sha TEXT NOT NULL,
                    auto_fix_project_id TEXT NOT NULL,
                    auto_fix_mr_iid INTEGER NOT NULL,
                    source_branch TEXT NOT NULL,
                    target_branch TEXT NOT NULL,
                    marker TEXT NOT NULL UNIQUE,
                    plan_sha256 TEXT NOT NULL UNIQUE,
                    published_commit_sha TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'opened',
                    merged_sha TEXT,
                    divergence TEXT,
                    reconcile_requested INTEGER NOT NULL DEFAULT 0,
                    reconcile_attempts INTEGER NOT NULL DEFAULT 0,
                    reconcile_next_at TEXT,
                    reconcile_lease_until TEXT,
                    reconcile_claim_token TEXT,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(auto_fix_project_id, auto_fix_mr_iid)
                );
                CREATE TABLE IF NOT EXISTS notification_requests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    relation_id INTEGER NOT NULL REFERENCES auto_fix_relations(id),
                    generation INTEGER NOT NULL,
                    request_kind TEXT NOT NULL,
                    correlation_id TEXT NOT NULL UNIQUE,
                    title TEXT NOT NULL,
                    body TEXT NOT NULL,
                    url TEXT,
                    recipients_json TEXT NOT NULL,
                    payload_sha256 TEXT NOT NULL,
                    payload_purged_at TEXT,
                    created_at TEXT NOT NULL,
                    UNIQUE(relation_id, generation)
                );
                CREATE TABLE IF NOT EXISTS operator_recoveries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    recovery_id TEXT NOT NULL UNIQUE,
                    action TEXT NOT NULL,
                    relation_id INTEGER REFERENCES auto_fix_relations(id),
                    project_id TEXT NOT NULL,
                    parent_mr_iid INTEGER NOT NULL,
                    parent_sha TEXT NOT NULL,
                    intent_nonce TEXT NOT NULL,
                    plan_sha256 TEXT NOT NULL,
                    child_project_id TEXT NOT NULL,
                    child_mr_iid INTEGER NOT NULL,
                    reason TEXT NOT NULL,
                    principal TEXT NOT NULL,
                    hostname TEXT NOT NULL,
                    tool_version TEXT NOT NULL,
                    result TEXT NOT NULL,
                    diagnostic TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS operator_plan_actions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    action_id TEXT NOT NULL UNIQUE,
                    plan_id INTEGER NOT NULL REFERENCES auto_fix_publication_plans(id),
                    plan_sha256 TEXT NOT NULL,
                    action TEXT NOT NULL,
                    from_state TEXT NOT NULL,
                    to_state TEXT NOT NULL,
                    write_phase TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    principal TEXT NOT NULL,
                    hostname TEXT NOT NULL,
                    tool_version TEXT NOT NULL,
                    result TEXT NOT NULL,
                    diagnostic TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS review_run_failures (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id INTEGER NOT NULL REFERENCES review_runs(id),
                    attempt INTEGER NOT NULL,
                    failure_class TEXT NOT NULL,
                    retryable INTEGER NOT NULL DEFAULT 0,
                    error TEXT,
                    recorded_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS effect_receipts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id INTEGER NOT NULL REFERENCES review_runs(id),
                    effect_kind TEXT NOT NULL,
                    dedupe_key TEXT NOT NULL,
                    attempt INTEGER NOT NULL,
                    state TEXT NOT NULL,
                    external_identity TEXT,
                    detail TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    error TEXT,
                    next_attempt_at TEXT,
                    lease_until TEXT,
                    claim_token TEXT,
                    UNIQUE(run_id, effect_kind, dedupe_key, attempt)
                );
                CREATE TABLE IF NOT EXISTS note_publication_requests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id INTEGER NOT NULL UNIQUE REFERENCES review_runs(id),
                    receipt_id INTEGER NOT NULL UNIQUE REFERENCES effect_receipts(id),
                    project_id TEXT NOT NULL,
                    mr_iid INTEGER NOT NULL,
                    sha TEXT NOT NULL,
                    marker TEXT NOT NULL,
                    body TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    write_uncertain INTEGER NOT NULL DEFAULT 0,
                    external_note_id INTEGER,
                    next_attempt_at TEXT,
                    lease_until TEXT,
                    claim_token TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_error TEXT,
                    reconcile_attempts INTEGER NOT NULL DEFAULT 0,
                    reconcile_next_at TEXT,
                    reconcile_lease_until TEXT,
                    reconcile_claim_token TEXT,
                    payload_retention_at TEXT,
                    payload_purged_at TEXT,
                    UNIQUE(project_id, mr_iid, sha)
                );
                CREATE TABLE IF NOT EXISTS notification_outbox (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    correlation_id TEXT NOT NULL,
                    channel_id TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    title TEXT NOT NULL,
                    body TEXT NOT NULL,
                    url TEXT,
                    recipients_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at TEXT,
                    lease_until TEXT,
                    claim_token TEXT,
                    created_at TEXT NOT NULL,
                    delivered_at TEXT,
                    terminal_at TEXT,
                    payload_purged_at TEXT,
                    last_error TEXT,
                    UNIQUE(correlation_id, channel_id)
                );
                CREATE TABLE IF NOT EXISTS worker_runtime_lease (
                    singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
                    owner_id TEXT NOT NULL,
                    claim_token TEXT NOT NULL,
                    heartbeat_at TEXT NOT NULL,
                    lease_until TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS worktree_cleanup (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id INTEGER NOT NULL REFERENCES review_runs(id),
                    path TEXT NOT NULL,
                    sha TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at TEXT,
                    lease_until TEXT,
                    claim_token TEXT,
                    created_at TEXT NOT NULL,
                    cleaned_at TEXT,
                    last_error TEXT,
                    UNIQUE(run_id, path)
                );
                CREATE TABLE IF NOT EXISTS publication_plan_backups (
                    plan_id INTEGER PRIMARY KEY REFERENCES auto_fix_publication_plans(id),
                    plan_sha256 TEXT NOT NULL UNIQUE,
                    backup_path TEXT NOT NULL UNIQUE,
                    backed_up_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sensitive_payload_purges (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    cutoff_at TEXT NOT NULL,
                    plan_payloads INTEGER NOT NULL,
                    agent_task_payloads INTEGER NOT NULL,
                    note_payloads INTEGER NOT NULL,
                    notification_payloads INTEGER NOT NULL,
                    diagnostic_payloads INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );
                PRAGMA user_version = 17;
                """
            )

    @staticmethod
    def _now() -> datetime:
        return datetime.now(UTC)

    @staticmethod
    def _run_from_row(row: sqlite3.Row) -> ReviewRun:
        return ReviewRun(**dict(row))

    @staticmethod
    def _review_run_from_row(row: sqlite3.Row) -> ReviewRun:
        values = dict(row)
        values["retryable"] = bool(values.get("retryable", 0))
        return ReviewRun(**values)

    @staticmethod
    def _review_run_columns() -> str:
        return (
            "id AS run_id, project_id, mr_iid, sha, status, attempts, "
            "failure_attempts, lease_recoveries, "
            "lease_until, claim_token, failure_class, retryable, error, next_attempt_at, "
            "created_at, finished_at"
        )

    def _record_failure(
        self,
        connection: sqlite3.Connection,
        run_id: int,
        attempt: int,
        failure_class: str,
        retryable: bool,
        error: str | None,
    ) -> None:
        connection.execute(
            """
            INSERT INTO review_run_failures(
                run_id, attempt, failure_class, retryable, error, recorded_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                attempt,
                failure_class,
                int(retryable),
                self._redact(error) if error else None,
                datetime.now(UTC).isoformat(),
            ),
        )

    @staticmethod
    def _effect_from_row(row: sqlite3.Row) -> EffectReceipt:
        return EffectReceipt(**dict(row))

    @staticmethod
    def _note_publication_from_row(row: sqlite3.Row) -> NotePublicationRequest:
        values = dict(row)
        values["write_uncertain"] = bool(values["write_uncertain"])
        return NotePublicationRequest(**values)

    @staticmethod
    def _note_publication_columns() -> str:
        return (
            "id AS request_id, run_id, receipt_id, project_id, mr_iid, sha, marker, body, "
            "state, attempts, write_uncertain, external_note_id, next_attempt_at, "
            "lease_until, claim_token, created_at, updated_at, last_error, "
            "reconcile_attempts, reconcile_next_at, reconcile_lease_until, "
            "reconcile_claim_token"
        )

    @staticmethod
    def _notification_payload_sha256(
        title: str,
        body: str,
        url: str | None,
        recipients: tuple[str, ...],
        channels: tuple[tuple[str, str], ...],
    ) -> str:
        payload = json.dumps(
            {
                "body": body,
                "channels": [list(channel) for channel in sorted(channels)],
                "recipients": list(recipients),
                "title": title,
                "url": url,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @staticmethod
    def _auto_fix_publication_intent_from_row(
        row: sqlite3.Row,
    ) -> AutoFixPublicationIntent:
        return AutoFixPublicationIntent(**dict(row))

    @staticmethod
    def _auto_fix_publication_intent_columns() -> str:
        return (
            "id AS intent_id, run_id, project_id, mr_iid, sha, generation, nonce, "
            "state, created_at, updated_at, last_error"
        )

    @staticmethod
    def _auto_fix_publication_plan_from_row(
        row: sqlite3.Row,
    ) -> AutoFixPublicationPlan:
        values = dict(row)
        values["write_uncertain"] = bool(values["write_uncertain"])
        return AutoFixPublicationPlan(**values)

    @staticmethod
    def _auto_fix_publication_plan_columns() -> str:
        return (
            "id AS plan_id, intent_id, run_id, plan_json, plan_sha256, marker, "
            "candidate_sha256, verification_json, state, created_at, updated_at, "
            "attempts, absence_confirmations, absence_observed_at, write_phase, "
            "write_uncertain, "
            "branch_commit_sha, external_mr_iid, "
            "next_attempt_at, lease_until, claim_token, last_error"
        )

    @staticmethod
    def _program_auto_fix_relation_columns() -> str:
        return (
            "id AS relation_id, intent_id, plan_id, run_id, parent_project_id, "
            "parent_mr_iid, parent_sha, auto_fix_project_id, auto_fix_mr_iid, "
            "source_branch, target_branch, marker, plan_sha256, status, merged_sha, "
            "published_commit_sha, "
            "divergence, reconcile_requested, reconcile_attempts, reconcile_next_at, "
            "reconcile_lease_until, reconcile_claim_token, last_error, created_at, updated_at"
        )

    @staticmethod
    def _program_auto_fix_relation_from_row(
        row: sqlite3.Row,
    ) -> ProgramAutoFixRelation:
        values = dict(row)
        values["reconcile_requested"] = bool(values["reconcile_requested"])
        return ProgramAutoFixRelation(**values)

    def enqueue(self, project_id: str, mr_iid: int, sha: str) -> ReviewRun:
        return self.enqueue_with_status(project_id, mr_iid, sha)[0]

    def enqueue_with_status(
        self, project_id: str, mr_iid: int, sha: str
    ) -> tuple[ReviewRun, bool]:
        """Return the stable Review Run and whether this call created it."""

        if not project_id or not str(project_id).strip():
            raise ValueError("project_id cannot be empty")
        sha = normalize_sha(sha)
        created_at = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            inserted = connection.execute(
                """
                INSERT INTO review_runs(project_id, mr_iid, sha, created_at, next_attempt_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(project_id, mr_iid, sha) DO NOTHING
                """,
                (project_id, mr_iid, sha, created_at, created_at),
            ).rowcount
            row = connection.execute(
                f"SELECT {self._review_run_columns()} FROM review_runs "
                "WHERE project_id = ? AND mr_iid = ? AND sha = ?",
                (project_id, mr_iid, sha),
            ).fetchone()
        assert row is not None
        return self._review_run_from_row(row), inserted == 1

    def wake_run(self, project_id: str, mr_iid: int, sha: str) -> ReviewRun | None:
        """Make a deferred Review Run immediately eligible after a green Pipeline."""

        sha = normalize_sha(sha)
        now = self._now().isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE review_runs
                SET next_attempt_at = ?, error = NULL
                WHERE project_id = ? AND mr_iid = ? AND sha = ? AND status = 'pending'
                """,
                (now, project_id, mr_iid, sha),
            )
            row = connection.execute(
                f"SELECT {self._review_run_columns()} FROM review_runs "
                "WHERE project_id = ? AND mr_iid = ? AND sha = ?",
                (project_id, mr_iid, sha),
            ).fetchone()
        return self._review_run_from_row(row) if row is not None else None

    def get_review_run(self, run_id: int) -> ReviewRun | None:
        with self._connect() as connection:
            row = connection.execute(
                f"SELECT {self._review_run_columns()} FROM review_runs WHERE id = ?",
                (run_id,),
            ).fetchone()
        return self._review_run_from_row(row) if row is not None else None

    def get_policy_snapshot(self, run_id: int) -> PolicySnapshot | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT run_id, target_branch, target_sha, policy_version,
                       policy_json, policy_sha256, created_at
                FROM policy_snapshots WHERE run_id = ?
                """,
                (run_id,),
            ).fetchone()
        return PolicySnapshot(**dict(row)) if row is not None else None

    def get_agent_task_snapshot(self, run_id: int) -> AgentTaskSnapshot | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM agent_task_snapshots WHERE run_id = ?", (run_id,)
            ).fetchone()
        return AgentTaskSnapshot(**dict(row)) if row is not None else None

    def freeze_agent_task_snapshot(
        self,
        run_id: int,
        claim_token: str | None,
        *,
        project_id: str,
        mr_iid: int,
        title: str,
        source_project_id: str,
        source_branch: str,
        target_branch: str,
        base_sha: str,
        start_sha: str,
        head_sha: str,
        skill_source_path: str,
        skill_content: str,
    ) -> AgentTaskSnapshot | None:
        """Get or freeze exact MR context once, fenced by the active Run lease."""

        if not claim_token:
            return None
        existing = self.get_agent_task_snapshot(run_id)
        if existing is None:
            if not project_id or project_id.strip() != project_id:
                raise ValueError("Agent task project_id must be a non-empty trimmed string")
            if type(mr_iid) is not int or mr_iid <= 0:
                raise ValueError("Agent task mr_iid must be a positive integer")
            if not title or len(title) > MAX_MR_TITLE_CHARS:
                raise ValueError(
                    f"Agent task title must contain 1-{MAX_MR_TITLE_CHARS} characters"
                )
            strings = {
                "source_project_id": source_project_id,
                "source_branch": source_branch,
                "target_branch": target_branch,
            }
            for field, value in strings.items():
                if not value or value.strip() != value:
                    raise ValueError(f"Agent task {field} must be a non-empty trimmed string")
            base_sha = normalize_sha(base_sha)
            start_sha = normalize_sha(start_sha)
            head_sha = normalize_sha(head_sha)
            if not skill_source_path or skill_source_path.strip() != skill_source_path:
                raise ValueError(
                    "Agent task skill_source_path must be a non-empty trimmed string"
                )
            skill_content = validate_skill_content(skill_content)
            skill_sha256 = hashlib.sha256(skill_content.encode("utf-8")).hexdigest()
        now = self._now().isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            active = connection.execute(
                """
                SELECT 1 FROM review_runs
                WHERE id = ? AND status = 'running' AND claim_token = ?
                  AND lease_until IS NOT NULL AND lease_until > ?
                """,
                (run_id, claim_token, now),
            ).fetchone()
            if active is None:
                return None
            row = connection.execute(
                "SELECT * FROM agent_task_snapshots WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is not None:
                return AgentTaskSnapshot(**dict(row))
            connection.execute(
                """
                INSERT INTO agent_task_snapshots(
                    run_id, project_id, mr_iid, title, source_project_id, source_branch,
                    target_branch, base_sha, start_sha, head_sha, skill_source_path,
                    skill_content, skill_sha256, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO NOTHING
                """,
                (
                    run_id,
                    project_id,
                    mr_iid,
                    title,
                    source_project_id,
                    source_branch,
                    target_branch,
                    base_sha,
                    start_sha,
                    head_sha,
                    skill_source_path,
                    skill_content,
                    skill_sha256,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM agent_task_snapshots WHERE run_id = ?", (run_id,)
            ).fetchone()
        assert row is not None
        return AgentTaskSnapshot(**dict(row))

    def freeze_policy_snapshot(
        self,
        run_id: int,
        claim_token: str | None,
        *,
        target_branch: str,
        target_sha: str,
        policy_version: int,
        policy_json: str,
    ) -> PolicySnapshot | None:
        """Persist the exact target-branch policy once, fenced by the Run lease."""

        if not claim_token:
            return None
        if not target_branch or target_branch.strip() != target_branch:
            raise ValueError("target_branch must be a non-empty trimmed string")
        target_sha = normalize_sha(target_sha)
        if type(policy_version) is not int or policy_version <= 0:
            raise ValueError("policy_version must be a positive integer")
        try:
            parsed_policy = json.loads(policy_json)
        except json.JSONDecodeError as exc:
            raise ValueError("policy snapshot must be valid JSON") from exc
        canonical_policy = json.dumps(
            parsed_policy, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        if canonical_policy != policy_json or not isinstance(parsed_policy, dict):
            raise ValueError("policy snapshot must be a canonical JSON object")
        policy_sha256 = hashlib.sha256(policy_json.encode("utf-8")).hexdigest()
        now = self._now().isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            active = connection.execute(
                """
                SELECT 1 FROM review_runs
                WHERE id = ? AND status = 'running' AND claim_token = ?
                  AND lease_until IS NOT NULL AND lease_until > ?
                """,
                (run_id, claim_token, now),
            ).fetchone()
            if active is None:
                return None
            connection.execute(
                """
                INSERT INTO policy_snapshots(
                    run_id, target_branch, target_sha, policy_version,
                    policy_json, policy_sha256, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO NOTHING
                """,
                (
                    run_id,
                    target_branch,
                    target_sha,
                    policy_version,
                    policy_json,
                    policy_sha256,
                    now,
                ),
            )
            row = connection.execute(
                """
                SELECT run_id, target_branch, target_sha, policy_version,
                       policy_json, policy_sha256, created_at
                FROM policy_snapshots WHERE run_id = ?
                """,
                (run_id,),
            ).fetchone()
        assert row is not None
        snapshot = PolicySnapshot(**dict(row))
        if (
            snapshot.target_branch != target_branch
            or snapshot.target_sha != target_sha
            or snapshot.policy_version != policy_version
            or snapshot.policy_json != policy_json
            or snapshot.policy_sha256 != policy_sha256
        ):
            raise ValueError("Policy Snapshot is immutable for a Review Run")
        return snapshot

    @staticmethod
    def _decode_string_tuple(value: str, field_name: str) -> tuple[str, ...]:
        try:
            loaded = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError(f"stored {field_name} is invalid JSON") from exc
        if not isinstance(loaded, list) or not all(isinstance(item, str) for item in loaded):
            raise ValueError(f"stored {field_name} must be a string list")
        return tuple(loaded)

    @classmethod
    def _requested_outputs_from_row(cls, row: sqlite3.Row) -> RequestedOutputs:
        return RequestedOutputs(
            run_id=int(row["run_id"]),
            human_review=bool(row["human_review"]),
            ai_review=bool(row["ai_review"]),
            auto_fix=bool(row["auto_fix"]),
            notification_channels=cls._decode_string_tuple(
                row["notification_channels_json"], "notification channels"
            ),
            created_at=str(row["created_at"]),
        )

    def get_requested_outputs(self, run_id: int) -> RequestedOutputs | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM requested_outputs WHERE run_id = ?", (run_id,)
            ).fetchone()
        return self._requested_outputs_from_row(row) if row is not None else None

    def freeze_requested_outputs(
        self,
        run_id: int,
        claim_token: str | None,
        *,
        human_review: bool,
        ai_review: bool,
        auto_fix: bool,
        notification_channels: tuple[str, ...],
    ) -> RequestedOutputs | None:
        if not claim_token:
            return None
        if len(set(notification_channels)) != len(notification_channels):
            raise ValueError("requested notification channel IDs must be unique")
        channels_json = json.dumps(list(notification_channels), separators=(",", ":"))
        now = self._now().isoformat()
        expected = (
            int(human_review),
            int(ai_review),
            int(auto_fix),
            channels_json,
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            active = connection.execute(
                """
                SELECT 1 FROM review_runs
                WHERE id = ? AND status = 'running' AND claim_token = ?
                  AND lease_until IS NOT NULL AND lease_until > ?
                """,
                (run_id, claim_token, now),
            ).fetchone()
            if active is None:
                return None
            connection.execute(
                """
                INSERT INTO requested_outputs(
                    run_id, human_review, ai_review, auto_fix,
                    notification_channels_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO NOTHING
                """,
                (run_id, *expected, now),
            )
            row = connection.execute(
                "SELECT * FROM requested_outputs WHERE run_id = ?", (run_id,)
            ).fetchone()
        assert row is not None
        if (
            tuple(
                row[key]
                for key in ("human_review", "ai_review", "auto_fix", "notification_channels_json")
            )
            != expected
        ):
            raise ValueError("Requested Outputs are immutable for a Review Run")
        return self._requested_outputs_from_row(row)

    @classmethod
    def _effective_decision_from_row(cls, row: sqlite3.Row) -> EffectiveRunDecision:
        return EffectiveRunDecision(
            run_id=int(row["run_id"]),
            decision=str(row["decision"]),
            human_review=bool(row["human_review"]),
            ai_review=bool(row["ai_review"]),
            auto_fix=bool(row["auto_fix"]),
            suppressed=bool(row["suppressed"]),
            recipients=cls._decode_string_tuple(row["recipients_json"], "recipients"),
            notification_channels=cls._decode_string_tuple(
                row["notification_channels_json"], "notification channels"
            ),
            reason=str(row["reason"]) if row["reason"] is not None else None,
            created_at=str(row["created_at"]),
        )

    def get_effective_run_decision(self, run_id: int) -> EffectiveRunDecision | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM effective_run_decisions WHERE run_id = ?", (run_id,)
            ).fetchone()
        return self._effective_decision_from_row(row) if row is not None else None

    def list_output_expectations(self, run_id: int) -> tuple[OutputExpectation, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id AS expectation_id, run_id, output_kind, dedupe_key, created_at
                FROM output_expectations WHERE run_id = ? ORDER BY id
                """,
                (run_id,),
            ).fetchall()
        return tuple(OutputExpectation(**dict(row)) for row in rows)

    def freeze_effective_run_decision(
        self,
        run_id: int,
        claim_token: str | None,
        *,
        decision: str,
        human_review: bool,
        ai_review: bool,
        auto_fix: bool,
        suppressed: bool,
        recipients: tuple[str, ...],
        notification_channels: tuple[str, ...],
        reason: str | None = None,
        expectations: tuple[tuple[str, str], ...] = (),
    ) -> EffectiveRunDecision | None:
        if not claim_token:
            return None
        if decision not in {"execute", "cancelled", "suppressed"}:
            raise ValueError("invalid Effective Run Decision")
        if len(set(notification_channels)) != len(notification_channels):
            raise ValueError("effective notification channel IDs must be unique")
        if len(set(expectations)) != len(expectations):
            raise ValueError("Output Expectations must be unique")
        if any(
            kind not in {"note", "notification", "autofix"} or not key for kind, key in expectations
        ):
            raise ValueError("invalid Output Expectation")
        recipients_json = json.dumps(list(recipients), separators=(",", ":"))
        channels_json = json.dumps(list(notification_channels), separators=(",", ":"))
        safe_reason = self._redact(reason) if reason else None
        now = self._now().isoformat()
        expected = (
            decision,
            int(human_review),
            int(ai_review),
            int(auto_fix),
            int(suppressed),
            recipients_json,
            channels_json,
            safe_reason,
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            active = connection.execute(
                """
                SELECT 1 FROM review_runs
                WHERE id = ? AND status = 'running' AND claim_token = ?
                  AND lease_until IS NOT NULL AND lease_until > ?
                """,
                (run_id, claim_token, now),
            ).fetchone()
            if active is None:
                return None
            connection.execute(
                """
                INSERT INTO effective_run_decisions(
                    run_id, decision, human_review, ai_review, auto_fix, suppressed,
                    recipients_json, notification_channels_json, reason, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO NOTHING
                """,
                (run_id, *expected, now),
            )
            row = connection.execute(
                "SELECT * FROM effective_run_decisions WHERE run_id = ?", (run_id,)
            ).fetchone()
            assert row is not None
            actual = tuple(
                row[key]
                for key in (
                    "decision",
                    "human_review",
                    "ai_review",
                    "auto_fix",
                    "suppressed",
                    "recipients_json",
                    "notification_channels_json",
                    "reason",
                )
            )
            if actual != expected:
                raise ValueError("Effective Run Decision is immutable for a Review Run")
            for output_kind, dedupe_key in expectations:
                connection.execute(
                    """
                    INSERT INTO output_expectations(
                        run_id, output_kind, dedupe_key, created_at
                    ) VALUES (?, ?, ?, ?)
                    ON CONFLICT(run_id, output_kind, dedupe_key) DO NOTHING
                    """,
                    (run_id, output_kind, dedupe_key, now),
                )
            saved_expectations = {
                (saved["output_kind"], saved["dedupe_key"])
                for saved in connection.execute(
                    "SELECT output_kind, dedupe_key FROM output_expectations WHERE run_id = ?",
                    (run_id,),
                )
            }
            requested_expectations = set(expectations)
            if not requested_expectations.issubset(saved_expectations) or any(
                expectation not in requested_expectations and expectation[0] != "autofix"
                for expectation in saved_expectations
            ):
                raise ValueError("Output Expectations are immutable for a Review Run")
        return self._effective_decision_from_row(row)

    def project_review_outcome(self, run_id: int) -> str:
        """Project user-visible delivery without mutating the stable Run lifecycle."""

        run = self.get_review_run(run_id)
        if run is None:
            raise ValueError("Review Run does not exist")
        if run.status in {"pending", "running"}:
            return "pending"
        requested = self.get_requested_outputs(run_id)
        decision = self.get_effective_run_decision(run_id)
        if decision is None:
            with self._connect() as connection:
                if (
                    connection.execute(
                        "SELECT 1 FROM auto_fix_relations WHERE run_id = ?", (run_id,)
                    ).fetchone()
                    is not None
                ):
                    return "delivered"
            return "indeterminate" if requested is not None else "no_effect"
        expectations = self.list_output_expectations(run_id)
        with self._connect() as connection:
            optional_delivered = (
                connection.execute(
                    "SELECT 1 FROM auto_fix_relations WHERE run_id = ?", (run_id,)
                ).fetchone()
                is not None
            )
        if not expectations:
            return "delivered" if optional_delivered else "no_effect"

        states: list[str] = []
        with self._connect() as connection:
            for expectation in expectations:
                if expectation.output_kind == "notification":
                    row = connection.execute(
                        """
                        SELECT status FROM notification_outbox
                        WHERE correlation_id = ? AND channel_id = ?
                        """,
                        (f"review-run:{run_id}:human", expectation.dedupe_key),
                    ).fetchone()
                    state = str(row["status"]) if row is not None else "missing"
                    states.append(
                        "delivered"
                        if state == "delivered"
                        else "pending"
                        if state in {"pending", "running"}
                        else "failed"
                    )
                elif expectation.output_kind == "note":
                    row = connection.execute(
                        "SELECT state FROM note_publication_requests WHERE run_id = ?",
                        (run_id,),
                    ).fetchone()
                    state = str(row["state"]) if row is not None else "missing"
                    states.append(
                        "delivered"
                        if state == "published"
                        else "pending"
                        if state in {"pending", "running", "uncertain"}
                        else "superseded"
                        if state == "superseded"
                        else "failed"
                    )
                else:
                    row = connection.execute(
                        """
                        SELECT state FROM auto_fix_publication_plans
                        WHERE run_id = ? AND plan_sha256 = ?
                        """,
                        (run_id, expectation.dedupe_key),
                    ).fetchone()
                    state = str(row["state"]) if row is not None else "missing"
                    states.append(
                        "delivered"
                        if state == "published"
                        else "pending"
                        if state in {"frozen", "running", "needs_reconciliation", "reconciling"}
                        else "superseded"
                        if state == "superseded"
                        else "failed"
                    )
        if "pending" in states:
            return "pending"
        observed_delivery = optional_delivered or "delivered" in states
        if "failed" not in states:
            return "delivered" if observed_delivery else "no_effect"
        return "degraded" if observed_delivery else "undelivered"

    def get_project_bound_review_run_status(
        self, project_id: str, run_id: int
    ) -> dict[str, object] | None:
        """Return a read-only, credential-free Review Run status snapshot.

        The project comparison happens before any failure or receipt details
        are returned.  The response is deliberately assembled from a
        whitelist rather than serializing ``ReviewRun`` or ``EffectReceipt``
        directly, because those internal records contain lease claim tokens.
        A delivered notification means provider acceptance, not recipient
        visibility; at-least-once delivery may send duplicates after a crash.
        """

        if not project_id or not project_id.strip() or run_id <= 0:
            return None
        run = self.get_review_run(run_id)
        if run is None or run.project_id != project_id:
            return None

        failures = self.list_failures(run_id)
        failure_items = [
            {
                "failure_id": failure.failure_id,
                "attempt": failure.attempt,
                "failure_class": failure.failure_class,
                "retryable": failure.retryable,
                "error": self._redact(failure.error, max_length=2000) if failure.error else None,
                "recorded_at": failure.recorded_at,
            }
            for failure in failures[-20:]
        ]
        latest_failure = failure_items[-1] if failure_items else None
        policy_snapshot = self.get_policy_snapshot(run_id)
        requested = self.get_requested_outputs(run_id)
        effective = self.get_effective_run_decision(run_id)
        expectations = self.list_output_expectations(run_id)
        receipt_items = [
            {
                "receipt_id": receipt.receipt_id,
                "effect_kind": receipt.effect_kind,
                "attempt": receipt.attempt,
                "state": receipt.state,
                "external_identity": self._redact(receipt.external_identity, max_length=1000)
                if receipt.external_identity
                else None,
                "detail": self._redact(receipt.detail, max_length=1000) if receipt.detail else None,
                "error": self._redact(receipt.error, max_length=2000) if receipt.error else None,
                "updated_at": receipt.updated_at,
            }
            for receipt in self.list_effect_receipts(run_id)[-50:]
        ]
        effects = self._run_effect_diagnostics(run_id)
        now = self._now().isoformat()
        return {
            "run_id": run.run_id,
            "project_id": run.project_id,
            "mr_iid": run.mr_iid,
            "sha": run.sha,
            "status": run.status,
            "attempt": run.attempts,
            "attempts": run.attempts,
            "claim_count": run.attempts,
            "failure_attempts": run.failure_attempts,
            "lease_recoveries": run.lease_recoveries,
            "lease_active": self._lease_active(run.lease_until, now),
            "lease_expired": run.lease_until is not None
            and run.lease_until <= now,
            "review_outcome": self.project_review_outcome(run_id),
            "policy_snapshot": (
                {
                    "target_branch": policy_snapshot.target_branch,
                    "target_sha": policy_snapshot.target_sha,
                    "version": policy_snapshot.policy_version,
                    "sha256": policy_snapshot.policy_sha256,
                }
                if policy_snapshot is not None
                else None
            ),
            "requested_outputs": (
                {
                    "human_review": requested.human_review,
                    "ai_review": requested.ai_review,
                    "auto_fix": requested.auto_fix,
                    "notification_channels": list(requested.notification_channels),
                }
                if requested is not None
                else None
            ),
            "effective_decision": (
                {
                    "decision": effective.decision,
                    "human_review": effective.human_review,
                    "ai_review": effective.ai_review,
                    "auto_fix": effective.auto_fix,
                    "suppressed": effective.suppressed,
                    "recipients": list(effective.recipients),
                    "notification_channels": list(effective.notification_channels),
                    "reason": effective.reason,
                }
                if effective is not None
                else None
            ),
            "output_expectations": [
                {
                    "expectation_id": expectation.expectation_id,
                    "output_kind": expectation.output_kind,
                    "dedupe_key": expectation.dedupe_key,
                }
                for expectation in expectations
            ],
            "failure": latest_failure,
            "failures": failure_items,
            "effect_receipts": receipt_items,
            "effects": effects,
            "diagnostic": {
                "error": self._redact(run.error, max_length=2000) if run.error else None,
                "failure_class": run.failure_class,
                "retryable": run.retryable,
                "next_attempt_at": run.next_attempt_at,
            },
            "created_at": run.created_at,
            "finished_at": run.finished_at,
        }

    @staticmethod
    def _lease_active(lease_until: str | None, now: str) -> bool:
        return lease_until is not None and lease_until > now

    def _run_effect_diagnostics(self, run_id: int) -> dict[str, object]:
        """Project-scoped caller only; select safe columns from local durable effects."""
        now = self._now().isoformat()
        with self._connect() as connection:
            note = connection.execute(
                """SELECT id, state, attempts, write_uncertain, external_note_id,
                          next_attempt_at, lease_until, reconcile_attempts,
                          reconcile_next_at, reconcile_lease_until, last_error
                   FROM note_publication_requests WHERE run_id = ?""",
                (run_id,),
            ).fetchone()
            plan = connection.execute(
                """SELECT id, state, attempts, write_phase, write_uncertain,
                          branch_commit_sha, external_mr_iid, next_attempt_at,
                          lease_until, last_error
                   FROM auto_fix_publication_plans WHERE run_id = ?""",
                (run_id,),
            ).fetchone()
            relation = connection.execute(
                """SELECT id, status, auto_fix_project_id, auto_fix_mr_iid,
                          published_commit_sha, divergence, reconcile_requested,
                          reconcile_attempts, reconcile_next_at,
                          reconcile_lease_until, last_error
                   FROM auto_fix_relations WHERE run_id = ?""",
                (run_id,),
            ).fetchone()
            deliveries = connection.execute(
                """SELECT o.id, n.generation, o.channel_id, o.channel,
                          o.status, o.attempts, o.next_attempt_at, o.lease_until,
                          o.created_at, o.delivered_at, o.last_error
                   FROM notification_outbox o
                   LEFT JOIN notification_requests n
                     ON n.correlation_id = o.correlation_id
                   LEFT JOIN auto_fix_relations r ON r.id = n.relation_id
                   WHERE o.correlation_id = ? OR r.run_id = ?
                   ORDER BY o.id""",
                (f"review-run:{run_id}:human", run_id),
            ).fetchall()

        note_item = None
        if note is not None:
            note_item = {
                "request_id": note["id"], "state": note["state"],
                "attempts": note["attempts"], "write_uncertain": bool(note["write_uncertain"]),
                "external_note_id": note["external_note_id"],
                "next_attempt_at": note["next_attempt_at"],
                "lease_active": self._lease_active(note["lease_until"], now),
                "lease_expired": note["lease_until"] is not None and note["lease_until"] <= now,
                "reconcile_attempts": note["reconcile_attempts"],
                "reconcile_next_at": note["reconcile_next_at"],
                "reconcile_lease_active": self._lease_active(note["reconcile_lease_until"], now),
                "error": self._redact(note["last_error"]) if note["last_error"] else None,
                "next_action": (
                    "operator_check" if note["state"] == "blocked"
                    else "wait_for_reconciliation" if note["write_uncertain"]
                    else "wait_for_worker" if note["state"] in {"pending", "running"}
                    else "none"
                ),
            }
        plan_item = None
        if plan is not None:
            plan_item = {
                "plan_id": plan["id"], "state": plan["state"],
                "attempts": plan["attempts"], "write_phase": plan["write_phase"],
                "write_uncertain": bool(plan["write_uncertain"]),
                "branch_commit_sha": plan["branch_commit_sha"],
                "external_mr_iid": plan["external_mr_iid"],
                "next_attempt_at": plan["next_attempt_at"],
                "lease_active": self._lease_active(plan["lease_until"], now),
                "lease_expired": plan["lease_until"] is not None and plan["lease_until"] <= now,
                "error": self._redact(plan["last_error"]) if plan["last_error"] else None,
                "next_action": (
                    "operator_check" if plan["state"] == "blocked"
                    else "wait_for_reconciliation" if plan["state"] in {
                        "needs_reconciliation", "reconciling"
                    } or (plan["state"] == "running" and plan["write_phase"] != "preflight")
                    else "wait_for_worker" if plan["state"] in {"frozen", "running"}
                    else "none"
                ),
            }
        relation_item = None
        if relation is not None:
            relation_item = {
                "relation_id": relation["id"], "status": relation["status"],
                "auto_fix_project_id": relation["auto_fix_project_id"],
                "auto_fix_mr_iid": relation["auto_fix_mr_iid"],
                "published_commit_sha": relation["published_commit_sha"],
                "divergence": relation["divergence"],
                "reconcile_requested": bool(relation["reconcile_requested"]),
                "reconcile_attempts": relation["reconcile_attempts"],
                "reconcile_next_at": relation["reconcile_next_at"],
                "reconcile_lease_active": self._lease_active(
                    relation["reconcile_lease_until"], now
                ),
                "error": self._redact(relation["last_error"])
                if relation["last_error"] else None,
                "next_action": "operator_check" if relation["divergence"]
                else "wait_for_reconciliation" if relation["reconcile_requested"]
                else "none",
            }
        notification_items = [
            {
                "delivery_id": row["id"], "generation": row["generation"],
                "channel_id": row["channel_id"], "channel": row["channel"],
                "status": row["status"], "attempts": row["attempts"],
                "next_attempt_at": row["next_attempt_at"],
                "lease_active": self._lease_active(row["lease_until"], now),
                "lease_expired": row["lease_until"] is not None and row["lease_until"] <= now,
                "created_at": row["created_at"], "delivered_at": row["delivered_at"],
                "error": self._redact(row["last_error"]) if row["last_error"] else None,
                "delivery_meaning": "server_accepted_may_duplicate"
                if row["status"] == "delivered" else None,
                "next_action": "operator_check" if row["status"] == "failed"
                else "wait_for_worker" if row["status"] in {"pending", "running"}
                else "none",
            }
            for row in deliveries
        ]
        return {"note": note_item, "auto_fix_plan": plan_item,
                "auto_fix_relation": relation_item, "notifications": notification_items}

    def get_review_run_status(self, project_id: str, run_id: int) -> dict[str, object] | None:
        """Compatibility alias for the project-bound status snapshot."""

        return self.get_project_bound_review_run_status(project_id, run_id)

    def claim_next(
        self,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        project_id: str | None = None,
    ) -> ReviewRun | None:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        if max_attempts <= 0:
            raise ValueError("max_attempts must be positive")
        if project_id is not None and not project_id.strip():
            raise ValueError("project_id cannot be empty")

        now = self._now()
        now_text = now.isoformat()
        lease_until = (now + timedelta(seconds=lease_seconds)).isoformat()
        claim_token = uuid4().hex
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            expired_query = (
                "SELECT id, attempts, lease_recoveries FROM review_runs "
                "WHERE status = 'running' AND (lease_until IS NULL OR lease_until <= ?)"
            )
            expired_parameters: tuple[object, ...] = (now_text,)
            if project_id is not None:
                expired_query += " AND project_id = ?"
                expired_parameters += (project_id,)
            expired_runs = connection.execute(expired_query, expired_parameters).fetchall()
            recovery_query = """
                UPDATE review_runs
                SET status = CASE
                        WHEN COALESCE(lease_recoveries, 0) + 1 >= ? THEN 'failed'
                        ELSE 'pending'
                    END,
                    finished_at = CASE
                        WHEN COALESCE(lease_recoveries, 0) + 1 >= ? THEN ?
                        ELSE NULL
                    END,
                    error = CASE
                        WHEN COALESCE(lease_recoveries, 0) + 1 >= ? THEN 'worker lease expired'
                        ELSE 'worker lease expired; retrying'
                    END,
                    failure_class = 'lease_expired',
                    retryable = 1,
                    next_attempt_at = CASE
                        WHEN COALESCE(lease_recoveries, 0) + 1 >= ? THEN NULL
                        ELSE ?
                    END,
                    lease_recoveries = COALESCE(lease_recoveries, 0) + 1,
                    lease_until = NULL,
                    claim_token = NULL
                WHERE status = 'running'
                  AND (lease_until IS NULL OR lease_until <= ?)
            """
            recovery_parameters: tuple[object, ...] = (
                max_attempts,
                max_attempts,
                now_text,
                max_attempts,
                max_attempts,
                now_text,
                now_text,
            )
            if project_id is not None:
                recovery_query += " AND project_id = ?"
                recovery_parameters += (project_id,)
            connection.execute(recovery_query, recovery_parameters)
            for expired in expired_runs:
                exhausted = int(expired["lease_recoveries"] or 0) + 1 >= max_attempts
                self._record_failure(
                    connection,
                    int(expired["id"]),
                    max(int(expired["attempts"] or 0), 1),
                    "lease_expired",
                    True,
                    "worker lease expired" if exhausted else "worker lease expired; retrying",
                )
                connection.execute(
                    """
                    UPDATE effect_receipts
                    SET state = 'not_observed', error = 'worker lease expired',
                        updated_at = ?, lease_until = NULL, claim_token = NULL
                    WHERE run_id = ? AND state = 'running'
                    """,
                    (now_text, int(expired["id"])),
                )
            select_query = f"""
                SELECT {self._review_run_columns()}
                FROM review_runs
                WHERE status = 'pending'
                  AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
            """
            select_parameters: tuple[object, ...] = (now_text,)
            if project_id is not None:
                select_query += " AND project_id = ?"
                select_parameters += (project_id,)
            select_query += " ORDER BY id LIMIT 1"
            row = connection.execute(select_query, select_parameters).fetchone()
            if row is None:
                return None
            connection.execute(
                """
                UPDATE review_runs
                SET status = 'running', attempts = COALESCE(attempts, 0) + 1,
                    lease_until = ?, claim_token = ?
                WHERE id = ? AND status = 'pending'
                """,
                (lease_until, claim_token, row["run_id"]),
            )
            claimed = connection.execute(
                f"SELECT {self._review_run_columns()} FROM review_runs WHERE id = ?",
                (row["run_id"],),
            ).fetchone()
        assert claimed is not None
        return self._review_run_from_row(claimed)

    def extend_lease(
        self,
        run_id: int,
        claim_token: str | None,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
    ) -> bool:
        """Atomically extend an unexpired lease owned by *claim_token*."""

        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        if not claim_token:
            return False
        now = datetime.now(UTC)
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE review_runs
                SET lease_until = ?
                WHERE id = ? AND status = 'running' AND claim_token = ?
                  AND lease_until IS NOT NULL AND lease_until > ?
                """,
                (
                    (now + timedelta(seconds=lease_seconds)).isoformat(),
                    run_id,
                    claim_token,
                    now.isoformat(),
                ),
            )
        return cursor.rowcount == 1

    heartbeat = extend_lease

    def has_active_lease(self, run_id: int, claim_token: str | None) -> bool:
        if not claim_token:
            return False
        now_text = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM review_runs
                WHERE id = ? AND status = 'running' AND claim_token = ?
                  AND lease_until IS NOT NULL AND lease_until > ?
                """,
                (run_id, claim_token, now_text),
            ).fetchone()
        return row is not None

    def finish(
        self,
        run_id: int,
        status: str,
        error: str | None = None,
        claim_token: str | None = None,
        failure_class: str | None = None,
        retryable: bool = False,
    ) -> bool:
        if status not in {"succeeded", "failed", "cancelled"}:
            raise ValueError(f"invalid terminal status: {status}")
        safe_error = self._redact(error) if error else None
        if status == "succeeded":
            failure_class = None
            retryable = False
        with self._connect() as connection:
            now_text = datetime.now(UTC).isoformat()
            query = (
                "UPDATE review_runs SET status = ?, finished_at = ?, error = ?, "
                "failure_class = ?, retryable = ?, lease_until = NULL, claim_token = NULL "
                "WHERE id = ?"
            )
            parameters: tuple[object, ...] = (
                status,
                now_text,
                safe_error,
                failure_class,
                int(retryable),
                run_id,
            )
            if claim_token is not None:
                query += (
                    " AND status = 'running' AND claim_token = ?"
                    " AND lease_until IS NOT NULL AND lease_until > ?"
                )
                parameters += (claim_token, now_text)
            else:
                query += " AND status = 'running'"
            cursor = connection.execute(query, parameters)
            if cursor.rowcount == 1 and failure_class is not None:
                row = connection.execute(
                    "SELECT attempts FROM review_runs WHERE id = ?", (run_id,)
                ).fetchone()
                self._record_failure(
                    connection,
                    run_id,
                    max(int(row["attempts"] or 0), 1) if row is not None else 1,
                    failure_class,
                    retryable,
                    safe_error,
                )
        return cursor.rowcount == 1

    def requeue(
        self,
        run_id: int,
        reason: str | None = None,
        delay_seconds: float = 0.0,
        claim_token: str | None = None,
        failure_class: str | None = None,
        retryable: bool = False,
    ) -> bool:
        if delay_seconds < 0:
            raise ValueError("delay_seconds cannot be negative")
        now = datetime.now(UTC)
        now_text = now.isoformat()
        next_attempt_at = (now + timedelta(seconds=delay_seconds)).isoformat()
        with self._connect() as connection:
            query = (
                "UPDATE review_runs SET status = 'pending', error = ?, finished_at = NULL, "
                "next_attempt_at = ?, failure_class = ?, retryable = ?, "
                "lease_until = NULL, claim_token = NULL WHERE id = ?"
            )
            parameters: tuple[object, ...] = (
                self._redact(reason) if reason else None,
                next_attempt_at,
                failure_class,
                int(retryable),
                run_id,
            )
            if claim_token is not None:
                query += (
                    " AND status = 'running' AND claim_token = ?"
                    " AND lease_until IS NOT NULL AND lease_until > ?"
                )
                parameters += (claim_token, now_text)
            else:
                query += " AND status = 'running'"
            cursor = connection.execute(query, parameters)
            if cursor.rowcount == 1 and failure_class is not None:
                row = connection.execute(
                    "SELECT attempts FROM review_runs WHERE id = ?", (run_id,)
                ).fetchone()
                self._record_failure(
                    connection,
                    run_id,
                    max(int(row["attempts"] or 0), 1) if row is not None else 1,
                    failure_class,
                    retryable,
                    reason,
                )
        return cursor.rowcount == 1

    def retry_or_fail(
        self,
        run_id: int,
        failure_class: str,
        reason: str,
        delay_seconds: float,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        claim_token: str | None = None,
    ) -> bool:
        """Record a retryable failure and requeue it until the failure cap."""

        if not failure_class:
            raise ValueError("failure_class cannot be empty")
        if delay_seconds < 0:
            raise ValueError("delay_seconds cannot be negative")
        if max_attempts <= 0:
            raise ValueError("max_attempts must be positive")
        now = datetime.now(UTC)
        now_text = now.isoformat()
        safe_reason = self._redact(reason)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            query = (
                "SELECT attempts, failure_attempts FROM review_runs "
                "WHERE id = ? AND status = 'running'"
            )
            parameters: tuple[object, ...] = (run_id,)
            if claim_token is not None:
                query += " AND claim_token = ? AND lease_until IS NOT NULL AND lease_until > ?"
                parameters += (claim_token, now_text)
            row = connection.execute(query, parameters).fetchone()
            if row is None:
                return False
            attempts = int(row["attempts"] or 0)
            failure_attempts = int(row["failure_attempts"] or 0) + 1
            exhausted = failure_attempts >= max_attempts
            status = "failed" if exhausted else "pending"
            finished_at = now_text if exhausted else None
            next_attempt_at = (
                None if exhausted else (now + timedelta(seconds=delay_seconds)).isoformat()
            )
            update_query = (
                "UPDATE review_runs SET status = ?, finished_at = ?, error = ?, "
                "failure_class = ?, retryable = 1, next_attempt_at = ?, "
                "failure_attempts = ?, "
                "lease_until = NULL, claim_token = NULL WHERE id = ? AND status = 'running'"
            )
            update_parameters: tuple[object, ...] = (
                status,
                finished_at,
                safe_reason,
                failure_class,
                next_attempt_at,
                failure_attempts,
                run_id,
            )
            if claim_token is not None:
                update_query += (
                    " AND claim_token = ? AND lease_until IS NOT NULL AND lease_until > ?"
                )
                update_parameters += (claim_token, now_text)
            updated = connection.execute(update_query, update_parameters)
            if updated.rowcount == 1:
                self._record_failure(
                    connection,
                    run_id,
                    max(attempts, 1),
                    failure_class,
                    True,
                    safe_reason,
                )
                connection.execute(
                    """
                    UPDATE effect_receipts
                    SET state = 'not_observed',
                        error = COALESCE(error, ?),
                        updated_at = ?, lease_until = NULL, claim_token = NULL
                    WHERE run_id = ? AND state = 'running'
                    """,
                    (safe_reason, now_text, run_id),
                )
        return updated.rowcount == 1 and not exhausted

    def list_failures(self, run_id: int) -> tuple[FailureRecord, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id AS failure_id, run_id, attempt, failure_class,
                       retryable, error, recorded_at
                FROM review_run_failures WHERE run_id = ? ORDER BY id
                """,
                (run_id,),
            ).fetchall()
        return tuple(
            FailureRecord(
                failure_id=int(row["failure_id"]),
                run_id=int(row["run_id"]),
                attempt=int(row["attempt"]),
                failure_class=str(row["failure_class"]),
                retryable=bool(row["retryable"]),
                error=row["error"],
                recorded_at=str(row["recorded_at"]),
            )
            for row in rows
        )

    def create_effect_receipt(
        self,
        run_id: int,
        effect_kind: str,
        dedupe_key: str,
        attempt: int = 1,
        state: str = "planned",
        external_identity: str | None = None,
        detail: str | None = None,
        error: str | None = None,
        next_attempt_at: str | None = None,
    ) -> EffectReceipt:
        if run_id <= 0:
            raise ValueError("run_id must be positive")
        if not effect_kind or not dedupe_key:
            raise ValueError("effect_kind and dedupe_key cannot be empty")
        if attempt <= 0:
            raise ValueError("effect attempt must be positive")
        if not state:
            raise ValueError("effect state cannot be empty")
        dedupe_key = self._redact(dedupe_key)
        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO effect_receipts(
                    run_id, effect_kind, dedupe_key, attempt, state,
                    external_identity, detail, created_at, updated_at, error,
                    next_attempt_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id, effect_kind, dedupe_key, attempt) DO NOTHING
                """,
                (
                    run_id,
                    effect_kind,
                    dedupe_key,
                    attempt,
                    state,
                    self._redact(external_identity) if external_identity else None,
                    self._redact(detail) if detail else None,
                    now,
                    now,
                    self._redact(error) if error else None,
                    next_attempt_at,
                ),
            )
            row = connection.execute(
                """
                SELECT id AS receipt_id, run_id, effect_kind, dedupe_key, attempt, state,
                       external_identity, detail, created_at, updated_at, error,
                       next_attempt_at, lease_until, claim_token
                FROM effect_receipts
                WHERE run_id = ? AND effect_kind = ? AND dedupe_key = ? AND attempt = ?
                """,
                (run_id, effect_kind, dedupe_key, attempt),
            ).fetchone()
        assert row is not None
        return self._effect_from_row(row)

    def get_effect_receipt(self, receipt_id: int) -> EffectReceipt | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT id AS receipt_id, run_id, effect_kind, dedupe_key, attempt, state,
                       external_identity, detail, created_at, updated_at, error,
                       next_attempt_at, lease_until, claim_token
                FROM effect_receipts WHERE id = ?
                """,
                (receipt_id,),
            ).fetchone()
        return self._effect_from_row(row) if row is not None else None

    def list_effect_receipts(
        self, run_id: int, effect_kind: str | None = None
    ) -> tuple[EffectReceipt, ...]:
        query = """
            SELECT id AS receipt_id, run_id, effect_kind, dedupe_key, attempt, state,
                   external_identity, detail, created_at, updated_at, error,
                   next_attempt_at, lease_until, claim_token
            FROM effect_receipts WHERE run_id = ?
        """
        parameters: tuple[object, ...] = (run_id,)
        if effect_kind is not None:
            query += " AND effect_kind = ?"
            parameters += (effect_kind,)
        query += " ORDER BY id"
        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return tuple(self._effect_from_row(row) for row in rows)

    def create_note_publication_request(
        self,
        run_id: int,
        receipt_id: int,
        review_markdown: str,
        run_claim_token: str | None,
    ) -> NotePublicationRequest | None:
        """Freeze one Note body while the producing Review Run lease is active."""

        if run_id <= 0 or receipt_id <= 0:
            raise ValueError("run_id and receipt_id must be positive")
        if not isinstance(review_markdown, str) or not review_markdown.strip():
            raise ValueError("review_markdown must be a non-empty string")
        if not run_claim_token:
            return None
        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            run = connection.execute(
                """
                SELECT project_id, mr_iid, sha FROM review_runs
                WHERE id = ? AND status = 'running' AND claim_token = ?
                  AND lease_until IS NOT NULL AND lease_until > ?
                """,
                (run_id, run_claim_token, now),
            ).fetchone()
            if run is None:
                return None
            marker = canonical_note_marker(
                str(run["project_id"]), int(run["mr_iid"]), str(run["sha"])
            )
            body = f"{marker}\n{review_markdown}"
            initial_state = "pending" if has_exact_note_marker(body, marker) else "blocked"
            initial_error = (
                None
                if initial_state == "pending"
                else "review_markdown conflicts with the canonical Note marker"
            )
            receipt = connection.execute(
                """
                SELECT id FROM effect_receipts
                WHERE id = ? AND run_id = ? AND effect_kind = 'note_expected'
                  AND dedupe_key = ?
                """,
                (receipt_id, run_id, marker),
            ).fetchone()
            if receipt is None:
                raise ValueError("Note effect receipt does not match the ReviewKey")
            connection.execute(
                """
                INSERT INTO note_publication_requests(
                    run_id, receipt_id, project_id, mr_iid, sha, marker, body,
                    state, next_attempt_at, created_at, updated_at, last_error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO NOTHING
                """,
                (
                    run_id,
                    receipt_id,
                    run["project_id"],
                    run["mr_iid"],
                    run["sha"],
                    marker,
                    body,
                    initial_state,
                    now if initial_state == "pending" else None,
                    now,
                    now,
                    initial_error,
                ),
            )
            row = connection.execute(
                f"SELECT {self._note_publication_columns()} "
                "FROM note_publication_requests WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            assert row is not None
            request = self._note_publication_from_row(row)
            if request.receipt_id != receipt_id or request.marker != marker or request.body != body:
                raise ValueError("Note Publication Request is immutable")
            connection.execute(
                """
                UPDATE effect_receipts
                SET state = ?, detail = ?, error = ?,
                    next_attempt_at = ?, updated_at = ?
                WHERE id = ? AND state = 'planned'
                """,
                (
                    initial_state,
                    "Program-owned GitLab Note publication queued"
                    if initial_state == "pending"
                    else "Program-owned GitLab Note publication blocked by marker conflict",
                    initial_error,
                    now if initial_state == "pending" else None,
                    now,
                    receipt_id,
                ),
            )
        return request

    def get_note_publication_request(self, request_id: int) -> NotePublicationRequest | None:
        with self._connect() as connection:
            row = connection.execute(
                f"SELECT {self._note_publication_columns()} "
                "FROM note_publication_requests WHERE id = ?",
                (request_id,),
            ).fetchone()
        return self._note_publication_from_row(row) if row is not None else None

    def get_note_publication_for_run(self, run_id: int) -> NotePublicationRequest | None:
        with self._connect() as connection:
            row = connection.execute(
                f"SELECT {self._note_publication_columns()} "
                "FROM note_publication_requests WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        return self._note_publication_from_row(row) if row is not None else None

    def claim_note_publication(
        self,
        *,
        request_id: int | None = None,
        project_id: str | None = None,
        excluded_request_ids: tuple[int, ...] = (),
        lease_seconds: int = DEFAULT_NOTE_PUBLICATION_LEASE_SECONDS,
        max_attempts: int = DEFAULT_NOTE_PUBLICATION_MAX_ATTEMPTS,
    ) -> NotePublicationRequest | None:
        if lease_seconds <= 0 or max_attempts <= 0:
            raise ValueError("Note publication lease and attempt limits must be positive")
        uncertain_limit = max_attempts + DEFAULT_NOTE_UNCERTAIN_LOOKUP_ATTEMPTS
        now_value = datetime.now(UTC)
        now = now_value.isoformat()
        lease_until = (now_value + timedelta(seconds=lease_seconds)).isoformat()
        filters = ""
        parameters: list[object] = [now]
        if request_id is not None:
            filters += " AND id = ?"
            parameters.append(request_id)
        if project_id is not None:
            filters += " AND project_id = ?"
            parameters.append(project_id)
        if excluded_request_ids:
            if any(type(value) is not int or value <= 0 for value in excluded_request_ids):
                raise ValueError("excluded Note request IDs must be positive integers")
            placeholders = ",".join("?" for _ in excluded_request_ids)
            filters += f" AND id NOT IN ({placeholders})"
            parameters.extend(excluded_request_ids)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE note_publication_requests
                SET state = CASE
                        WHEN attempts >= ? AND NOT (
                            write_uncertain = 1 AND attempts < ?
                        ) THEN 'blocked' ELSE 'pending' END,
                    next_attempt_at = CASE
                        WHEN attempts >= ? AND NOT (
                            write_uncertain = 1 AND attempts < ?
                        ) THEN NULL ELSE ? END,
                    lease_until = NULL, claim_token = NULL, updated_at = ?,
                    last_error = COALESCE(last_error, 'publication lease expired')
                WHERE state = 'running' AND (lease_until IS NULL OR lease_until <= ?)
                """,
                (
                    max_attempts,
                    uncertain_limit,
                    max_attempts,
                    uncertain_limit,
                    now,
                    now,
                    now,
                ),
            )
            connection.execute(
                """
                UPDATE note_publication_requests
                SET state = 'blocked', next_attempt_at = NULL, updated_at = ?,
                    last_error = COALESCE(last_error, 'publication attempt limit reached')
                WHERE state = 'pending' AND attempts >= ?
                  AND NOT (write_uncertain = 1 AND attempts < ?)
                """,
                (now, max_attempts, uncertain_limit),
            )
            connection.execute(
                """
                UPDATE effect_receipts
                SET state = (
                        SELECT state FROM note_publication_requests
                        WHERE receipt_id = effect_receipts.id
                    ),
                    error = (
                        SELECT last_error FROM note_publication_requests
                        WHERE receipt_id = effect_receipts.id
                    ),
                    next_attempt_at = (
                        SELECT next_attempt_at FROM note_publication_requests
                        WHERE receipt_id = effect_receipts.id
                    ),
                    lease_until = NULL, claim_token = NULL, updated_at = ?
                WHERE id IN (
                    SELECT receipt_id FROM note_publication_requests
                    WHERE state IN ('pending', 'blocked')
                ) AND state = 'running'
                """,
                (now,),
            )
            row = connection.execute(
                f"""
                SELECT id FROM note_publication_requests
                WHERE state = 'pending' AND (
                        attempts < ? OR (write_uncertain = 1 AND attempts < ?)
                    )
                  AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
                  {filters}
                ORDER BY id LIMIT 1
                """,
                (max_attempts, uncertain_limit, *parameters),
            ).fetchone()
            if row is None:
                return None
            token = uuid4().hex
            updated = connection.execute(
                """
                UPDATE note_publication_requests
                SET state = 'running', attempts = attempts + 1, lease_until = ?,
                    claim_token = ?, updated_at = ?
                WHERE id = ? AND state = 'pending' AND (
                    attempts < ? OR (write_uncertain = 1 AND attempts < ?)
                )
                """,
                (
                    lease_until,
                    token,
                    now,
                    row["id"],
                    max_attempts,
                    uncertain_limit,
                ),
            )
            if updated.rowcount != 1:
                return None
            claimed = connection.execute(
                f"SELECT {self._note_publication_columns()} "
                "FROM note_publication_requests WHERE id = ?",
                (row["id"],),
            ).fetchone()
            assert claimed is not None
            connection.execute(
                """
                UPDATE effect_receipts SET state = 'running', updated_at = ?
                WHERE id = ?
                """,
                (now, claimed["receipt_id"]),
            )
        return self._note_publication_from_row(claimed)

    def mark_note_write_uncertain(self, request_id: int, claim_token: str | None) -> bool:
        """Durably record the create boundary before invoking GitLab."""

        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            updated = connection.execute(
                """
                UPDATE note_publication_requests
                SET write_uncertain = 1, updated_at = ?
                WHERE id = ? AND state = 'running' AND claim_token = ?
                  AND lease_until IS NOT NULL AND lease_until > ?
                """,
                (now, request_id, claim_token, now),
            )
        return updated.rowcount == 1

    def finish_note_publication(
        self,
        request_id: int,
        claim_token: str | None,
        *,
        state: str,
        external_note_id: int | None = None,
        detail: str | None = None,
        error: str | None = None,
        next_attempt_at: str | None = None,
        reconcile_next_at: str | None = None,
    ) -> bool:
        allowed_states = {"pending", "published", "blocked", "superseded"}
        if state not in allowed_states:
            raise ValueError("invalid Note publication completion state")
        if state == "published" and (type(external_note_id) is not int or external_note_id <= 0):
            raise ValueError("published Note requires a positive external_note_id")
        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT receipt_id, project_id, mr_iid FROM note_publication_requests
                WHERE id = ? AND state = 'running' AND claim_token = ?
                  AND lease_until IS NOT NULL AND lease_until > ?
                """,
                (request_id, claim_token, now),
            ).fetchone()
            if row is None:
                return False
            identity = (
                f"gitlab:{row['project_id']}!{row['mr_iid']}#note-{external_note_id}"
                if external_note_id is not None
                else None
            )
            connection.execute(
                """
                UPDATE note_publication_requests
                SET state = ?, external_note_id = ?, next_attempt_at = ?,
                    lease_until = NULL, claim_token = NULL, updated_at = ?, last_error = ?,
                    payload_retention_at = CASE
                        WHEN ? IN ('published', 'blocked', 'superseded')
                        THEN COALESCE(payload_retention_at, ?)
                        ELSE payload_retention_at
                    END,
                    reconcile_attempts = CASE WHEN ? = 'published' THEN 0
                                              ELSE reconcile_attempts END,
                    reconcile_next_at = CASE WHEN ? = 'published' THEN ?
                                             ELSE reconcile_next_at END,
                    reconcile_lease_until = NULL, reconcile_claim_token = NULL
                WHERE id = ?
                """,
                (
                    state,
                    external_note_id,
                    next_attempt_at,
                    now,
                    self._redact(error) if error else None,
                    state,
                    now,
                    state,
                    state,
                    reconcile_next_at,
                    request_id,
                ),
            )
            connection.execute(
                """
                UPDATE effect_receipts
                SET state = ?, external_identity = ?, detail = ?, error = ?,
                    next_attempt_at = ?, lease_until = NULL, claim_token = NULL,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    state,
                    identity,
                    self._redact(detail) if detail else None,
                    self._redact(error) if error else None,
                    next_attempt_at,
                    now,
                    row["receipt_id"],
                ),
            )
        return True

    def claim_published_note_reconciliation(
        self,
        *,
        request_id: int | None = None,
        project_id: str | None = None,
        lease_seconds: int = DEFAULT_NOTE_PUBLICATION_LEASE_SECONDS,
    ) -> NotePublicationRequest | None:
        if lease_seconds <= 0:
            raise ValueError("Note reconciliation lease must be positive")
        now_value = datetime.now(UTC)
        now = now_value.isoformat()
        lease_until = (now_value + timedelta(seconds=lease_seconds)).isoformat()
        filters = ""
        parameters: list[object] = [now]
        if request_id is not None:
            filters += " AND id = ?"
            parameters.append(request_id)
        if project_id is not None:
            filters += " AND project_id = ?"
            parameters.append(project_id)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE note_publication_requests
                SET reconcile_lease_until = NULL, reconcile_claim_token = NULL,
                    reconcile_next_at = ?, updated_at = ?,
                    last_error = COALESCE(last_error, 'Note reconciliation lease expired')
                WHERE state = 'published' AND reconcile_claim_token IS NOT NULL
                  AND (reconcile_lease_until IS NULL OR reconcile_lease_until <= ?)
                """,
                (now, now, now),
            )
            row = connection.execute(
                f"""
                SELECT id FROM note_publication_requests
                WHERE state = 'published' AND external_note_id IS NOT NULL
                  AND payload_purged_at IS NULL
                  AND reconcile_claim_token IS NULL
                  AND (reconcile_next_at IS NULL OR reconcile_next_at <= ?)
                  {filters}
                ORDER BY id LIMIT 1
                """,
                tuple(parameters),
            ).fetchone()
            if row is None:
                return None
            token = uuid4().hex
            updated = connection.execute(
                """
                UPDATE note_publication_requests
                SET reconcile_attempts = reconcile_attempts + 1,
                    reconcile_lease_until = ?, reconcile_claim_token = ?, updated_at = ?
                WHERE id = ? AND state = 'published' AND reconcile_claim_token IS NULL
                """,
                (lease_until, token, now, row["id"]),
            )
            if updated.rowcount != 1:
                return None
            claimed = connection.execute(
                f"SELECT {self._note_publication_columns()} "
                "FROM note_publication_requests WHERE id = ?",
                (row["id"],),
            ).fetchone()
        assert claimed is not None
        return self._note_publication_from_row(claimed)

    def finish_published_note_reconciliation(
        self,
        request_id: int,
        reconcile_claim_token: str | None,
        *,
        state: str,
        detail: str,
        error: str | None = None,
        reconcile_next_at: str | None = None,
    ) -> bool:
        if state not in {"published", "removed", "blocked"}:
            raise ValueError("invalid Note reconciliation state")
        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT receipt_id, project_id, mr_iid, external_note_id
                FROM note_publication_requests
                WHERE id = ? AND state = 'published' AND reconcile_claim_token = ?
                  AND reconcile_lease_until IS NOT NULL AND reconcile_lease_until > ?
                """,
                (request_id, reconcile_claim_token, now),
            ).fetchone()
            if row is None:
                return False
            connection.execute(
                """
                UPDATE note_publication_requests
                SET state = ?, updated_at = ?, last_error = ?,
                    payload_retention_at = COALESCE(payload_retention_at, ?),
                    reconcile_attempts = CASE WHEN ? IS NULL THEN 0
                                              ELSE reconcile_attempts END,
                    reconcile_next_at = ?, reconcile_lease_until = NULL,
                    reconcile_claim_token = NULL
                WHERE id = ?
                """,
                (
                    state,
                    now,
                    self._redact(error) if error else None,
                    now,
                    error,
                    reconcile_next_at,
                    request_id,
                ),
            )
            identity = f"gitlab:{row['project_id']}!{row['mr_iid']}#note-{row['external_note_id']}"
            connection.execute(
                """
                UPDATE effect_receipts
                SET state = ?, external_identity = ?, detail = ?, error = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    state,
                    identity,
                    self._redact(detail),
                    self._redact(error) if error else None,
                    now,
                    row["receipt_id"],
                ),
            )
        return True

    def update_effect_receipt(
        self,
        receipt_id: int,
        *,
        state: str,
        external_identity: str | None = None,
        detail: str | None = None,
        error: str | None = None,
        next_attempt_at: str | None = None,
        claim_token: str | None = None,
        run_id: int | None = None,
        run_claim_token: str | None = None,
    ) -> bool:
        if not state:
            raise ValueError("effect state cannot be empty")
        if (run_id is None) != (run_claim_token is None):
            raise ValueError("run_id and run_claim_token must be provided together")
        now_text = datetime.now(UTC).isoformat()
        query = (
            "UPDATE effect_receipts SET state = ?, external_identity = ?, detail = ?, "
            "error = ?, next_attempt_at = ?, updated_at = ?, lease_until = NULL, "
            "claim_token = NULL WHERE id = ?"
        )
        parameters: tuple[object, ...] = (
            state,
            self._redact(external_identity) if external_identity else None,
            self._redact(detail) if detail else None,
            self._redact(error) if error else None,
            next_attempt_at,
            now_text,
            receipt_id,
        )
        if claim_token is not None:
            query += (
                " AND state = 'running' AND claim_token = ?"
                " AND lease_until IS NOT NULL AND lease_until > ?"
            )
            parameters += (claim_token, now_text)
        if run_id is not None and run_claim_token is not None:
            query += (
                " AND run_id = ? AND EXISTS ("
                "SELECT 1 FROM review_runs "
                "WHERE review_runs.id = effect_receipts.run_id "
                "AND review_runs.id = ? AND review_runs.status = 'running' "
                "AND review_runs.claim_token = ? "
                "AND review_runs.lease_until IS NOT NULL "
                "AND review_runs.lease_until > ?)"
            )
            parameters += (run_id, run_id, run_claim_token, now_text)
        with self._connect() as connection:
            cursor = connection.execute(query, parameters)
        return cursor.rowcount == 1

    def claim_pending_effect_receipts(
        self,
        effect_kind: str,
        limit: int = 1,
        lease_seconds: int = DEFAULT_EFFECT_LEASE_SECONDS,
        project_id: str | None = None,
        max_attempts: int | None = None,
    ) -> tuple[EffectReceipt, ...]:
        if not effect_kind:
            raise ValueError("effect_kind cannot be empty")
        if limit <= 0:
            raise ValueError("effect receipt limit must be positive")
        if lease_seconds <= 0:
            raise ValueError("effect receipt lease_seconds must be positive")
        if max_attempts is not None and max_attempts <= 0:
            raise ValueError("effect receipt max_attempts must be positive")
        if project_id is not None and not project_id.strip():
            raise ValueError("project_id cannot be empty")
        now = datetime.now(UTC)
        now_text = now.isoformat()
        lease_until = (now + timedelta(seconds=lease_seconds)).isoformat()
        project_scope = ""
        project_parameters: tuple[object, ...] = ()
        if project_id is not None:
            project_scope = (
                " AND EXISTS ("
                "SELECT 1 FROM review_runs "
                "WHERE review_runs.id = effect_receipts.run_id "
                "AND review_runs.project_id = ?)"
            )
            project_parameters = (project_id,)
        run_status_scope = ""
        joined_run_status_scope = ""
        if max_attempts is None:
            reclaim_state = "'pending'"
            reclaim_next_attempt = "?"
            reclaim_error = "error"
            reclaim_parameters: tuple[object, ...] = (now_text, now_text)
        else:
            reclaim_state = "CASE WHEN attempt >= ? THEN 'not_observed' ELSE 'pending' END"
            reclaim_next_attempt = "CASE WHEN attempt >= ? THEN NULL ELSE ? END"
            reclaim_error = (
                "CASE WHEN attempt >= ? THEN "
                "COALESCE(error, 'effect receipt attempt limit reached after lease expiry') "
                "ELSE error END"
            )
            reclaim_parameters = (
                max_attempts,
                max_attempts,
                now_text,
                now_text,
                max_attempts,
            )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                f"""
                UPDATE effect_receipts
                SET state = {reclaim_state}, next_attempt_at = {reclaim_next_attempt},
                    lease_until = NULL, claim_token = NULL, updated_at = ?,
                    error = {reclaim_error}
                WHERE effect_kind = ? AND state = 'running'
                  AND (lease_until IS NULL OR lease_until <= ?)
                  {project_scope}
                  {run_status_scope}
                """,
                (
                    *reclaim_parameters,
                    effect_kind,
                    now_text,
                    *project_parameters,
                ),
            )
            if max_attempts is not None:
                connection.execute(
                    f"""
                    UPDATE effect_receipts
                    SET state = 'not_observed', next_attempt_at = NULL,
                        lease_until = NULL, claim_token = NULL, updated_at = ?,
                        error = COALESCE(
                            error, 'effect receipt attempt limit reached'
                        )
                    WHERE effect_kind = ? AND state = 'pending' AND attempt >= ?
                      {project_scope}
                      {run_status_scope}
                    """,
                    (
                        now_text,
                        effect_kind,
                        max_attempts,
                        *project_parameters,
                    ),
                )
            attempt_scope = " AND effect_receipts.attempt < ?" if max_attempts is not None else ""
            rows = connection.execute(
                f"""
                SELECT effect_receipts.id FROM effect_receipts
                JOIN review_runs ON review_runs.id = effect_receipts.run_id
                WHERE effect_receipts.effect_kind = ? AND effect_receipts.state = 'pending'
                  AND (effect_receipts.next_attempt_at IS NULL
                       OR effect_receipts.next_attempt_at <= ?)
                  {"AND review_runs.project_id = ?" if project_id is not None else ""}
                  {joined_run_status_scope}
                  {attempt_scope}
                ORDER BY effect_receipts.id LIMIT ?
                """,
                (
                    effect_kind,
                    now_text,
                    *(project_parameters or ()),
                    *((max_attempts,) if max_attempts is not None else ()),
                    limit,
                ),
            ).fetchall()
            claimed_rows: list[sqlite3.Row] = []
            for row in rows:
                token = uuid4().hex
                claim_attempt_scope = " AND attempt < ?" if max_attempts is not None else ""
                claimed_update = connection.execute(
                    f"""
                    UPDATE effect_receipts
                    SET state = 'running', attempt = attempt + 1,
                        lease_until = ?, claim_token = ?, updated_at = ?
                    WHERE id = ? AND state = 'pending'
                      {project_scope}
                      {run_status_scope}
                      {claim_attempt_scope}
                    """,
                    (
                        lease_until,
                        token,
                        now_text,
                        row["id"],
                        *project_parameters,
                        *((max_attempts,) if max_attempts is not None else ()),
                    ),
                )
                if claimed_update.rowcount != 1:
                    continue
                claimed = connection.execute(
                    """
                    SELECT id AS receipt_id, run_id, effect_kind, dedupe_key, attempt, state,
                           external_identity, detail, created_at, updated_at, error,
                           next_attempt_at, lease_until, claim_token
                    FROM effect_receipts WHERE id = ?
                    """,
                    (row["id"],),
                ).fetchone()
                if claimed is not None:
                    claimed_rows.append(claimed)
        return tuple(self._effect_from_row(row) for row in claimed_rows)

    def cancel_and_enqueue_latest(
        self,
        run_id: int,
        project_id: str,
        mr_iid: int,
        sha: str,
        reason: str,
        claim_token: str | None = None,
        failure_class: str | None = None,
        retryable: bool = False,
    ) -> ReviewRun | None:
        """Cancel a stale claimed run and atomically make the current SHA pending."""

        if not project_id or not project_id.strip():
            raise ValueError("project_id cannot be empty")
        sha = normalize_sha(sha)
        created_at = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            update_query = (
                "UPDATE review_runs SET status = 'cancelled', finished_at = ?, error = ?, "
                "failure_class = ?, retryable = ?, "
                "next_attempt_at = NULL, lease_until = NULL, claim_token = NULL "
                "WHERE id = ? AND status = 'running'"
            )
            update_parameters: tuple[object, ...] = (
                created_at,
                self._redact(reason),
                failure_class,
                int(retryable),
                run_id,
            )
            if claim_token is not None:
                update_query += (
                    " AND claim_token = ? AND lease_until IS NOT NULL AND lease_until > ?"
                )
                update_parameters += (claim_token, created_at)
            updated = connection.execute(update_query, update_parameters)
            if updated.rowcount != 1:
                return None
            if failure_class is not None:
                current = connection.execute(
                    "SELECT attempts FROM review_runs WHERE id = ?", (run_id,)
                ).fetchone()
                self._record_failure(
                    connection,
                    run_id,
                    max(int(current["attempts"] or 0), 1) if current is not None else 1,
                    failure_class,
                    retryable,
                    reason,
                )

            connection.execute(
                """
                INSERT INTO review_runs(project_id, mr_iid, sha, created_at, next_attempt_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(project_id, mr_iid, sha) DO NOTHING
                """,
                (project_id, mr_iid, sha, created_at, created_at),
            )
            row = connection.execute(
                f"SELECT {self._review_run_columns()} FROM review_runs "
                "WHERE project_id = ? AND mr_iid = ? AND sha = ?",
                (project_id, mr_iid, sha),
            ).fetchone()
        assert row is not None
        return self._review_run_from_row(row)

    def create_auto_fix_publication_intent(
        self,
        run_id: int,
        run_claim_token: str | None,
    ) -> AutoFixPublicationIntent | None:
        """Create the immutable authorization instance before Agent execution."""

        if not run_claim_token:
            return None
        now = self._now().isoformat()
        with self._connect() as connection:
            run = connection.execute(
                """
                SELECT project_id, mr_iid, sha FROM review_runs
                WHERE id = ? AND status = 'running' AND claim_token = ?
                  AND lease_until IS NOT NULL AND lease_until > ?
                """,
                (run_id, run_claim_token, now),
            ).fetchone()
            if run is None:
                return None
            existing = connection.execute(
                f"SELECT {self._auto_fix_publication_intent_columns()} "
                "FROM auto_fix_publication_intents WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO auto_fix_publication_intents(
                        run_id, project_id, mr_iid, sha, generation, nonce,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, 1, ?, ?, ?)
                    """,
                    (
                        run_id,
                        run["project_id"],
                        run["mr_iid"],
                        run["sha"],
                        uuid4().hex,
                        now,
                        now,
                    ),
                )
                existing = connection.execute(
                    f"SELECT {self._auto_fix_publication_intent_columns()} "
                    "FROM auto_fix_publication_intents WHERE run_id = ?",
                    (run_id,),
                ).fetchone()
            assert existing is not None
            intent = self._auto_fix_publication_intent_from_row(existing)
            if (
                intent.project_id != str(run["project_id"])
                or intent.mr_iid != int(run["mr_iid"])
                or intent.sha != str(run["sha"])
                or intent.generation != 1
            ):
                raise ValueError("Auto-fix Intent is immutable for its ReviewKey")
        return intent

    def get_auto_fix_publication_intent(self, run_id: int) -> AutoFixPublicationIntent | None:
        with self._connect() as connection:
            row = connection.execute(
                f"SELECT {self._auto_fix_publication_intent_columns()} "
                "FROM auto_fix_publication_intents WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        return self._auto_fix_publication_intent_from_row(row) if row is not None else None

    def block_incomplete_auto_fix_after_frozen_note(
        self,
        run_id: int,
        run_claim_token: str | None,
        error: str,
    ) -> bool:
        """Fail closed when a crash left only the independent Note durable."""

        if not run_claim_token:
            return False
        now = self._now().isoformat()
        safe_error = self._redact(error)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            run = connection.execute(
                """
                SELECT 1 FROM review_runs
                WHERE id = ? AND status = 'running' AND claim_token = ?
                  AND lease_until IS NOT NULL AND lease_until > ?
                """,
                (run_id, run_claim_token, now),
            ).fetchone()
            if run is None:
                return False
            intent = connection.execute(
                "SELECT id, nonce, state FROM auto_fix_publication_intents WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if intent is None or intent["state"] != "pending":
                return True
            plan = connection.execute(
                "SELECT 1 FROM auto_fix_publication_plans WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if plan is not None:
                return True
            connection.execute(
                """
                UPDATE auto_fix_publication_intents
                SET state = 'blocked', updated_at = ?, last_error = ?
                WHERE id = ? AND state = 'pending'
                """,
                (now, safe_error, intent["id"]),
            )
            connection.execute(
                """
                UPDATE effect_receipts
                SET state = 'blocked',
                    detail = 'Auto-fix candidate was not durable before Worker recovery',
                    error = ?, next_attempt_at = NULL, lease_until = NULL,
                    claim_token = NULL, updated_at = ?
                WHERE run_id = ? AND effect_kind = 'autofix_plan'
                  AND dedupe_key = ? AND state IN ('planned', 'running')
                """,
                (safe_error, now, run_id, f"autofix-intent:{intent['nonce']}"),
            )
        return True

    def resolve_auto_fix_publication_intent(
        self,
        run_id: int,
        receipt_id: int,
        state: str,
        detail: str,
        run_claim_token: str | None,
        *,
        error: str | None = None,
    ) -> bool:
        """Resolve a non-publishable proposal without failing the Review Run."""

        if state not in {"no_proposal", "blocked"} or not run_claim_token:
            raise ValueError("invalid Auto-fix Intent terminal state")
        now = self._now().isoformat()
        safe_error = self._redact(error) if error else None
        with self._connect() as connection:
            intent = connection.execute(
                """
                SELECT nonce FROM auto_fix_publication_intents
                WHERE run_id = ? AND state = 'pending'
                """,
                (run_id,),
            ).fetchone()
            if intent is None:
                current = self.get_auto_fix_publication_intent(run_id)
                return current is not None and current.state == state
            receipt = connection.execute(
                """
                SELECT id FROM effect_receipts
                WHERE id = ? AND run_id = ? AND effect_kind = 'autofix_plan'
                  AND dedupe_key = ? AND state = 'planned'
                """,
                (receipt_id, run_id, f"autofix-intent:{intent['nonce']}"),
            ).fetchone()
            if receipt is None:
                raise ValueError("Auto-fix effect receipt does not match the Intent")
            cursor = connection.execute(
                """
                UPDATE auto_fix_publication_intents
                SET state = ?, updated_at = ?, last_error = ?
                WHERE run_id = ? AND state = 'pending'
                  AND EXISTS (
                    SELECT 1 FROM review_runs
                    WHERE id = ? AND status = 'running' AND claim_token = ?
                      AND lease_until IS NOT NULL AND lease_until > ?
                  )
                """,
                (state, now, safe_error, run_id, run_id, run_claim_token, now),
            )
            if cursor.rowcount != 1:
                return False
            connection.execute(
                """
                UPDATE effect_receipts
                SET state = ?, detail = ?, error = ?, updated_at = ?
                WHERE id = ? AND state = 'planned'
                """,
                (state, detail, safe_error, now, receipt_id),
            )
        return True

    def freeze_auto_fix_publication_plan(
        self,
        run_id: int,
        receipt_id: int,
        payload: dict[str, object],
        run_claim_token: str | None,
    ) -> AutoFixPublicationPlan | None:
        """Atomically freeze canonical independent inputs and their derived identity."""

        if not run_claim_token or "plan_sha256" in payload:
            raise ValueError("Publication Plan inputs must exclude derived identity")
        plan_json = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        plan_sha256 = hashlib.sha256(plan_json.encode("utf-8")).hexdigest()
        template = payload.get("marker_template")
        candidate_sha256 = payload.get("candidate_sha256")
        verification = payload.get("verification")
        if (
            not isinstance(template, str)
            or template.count("{plan_sha256}") != 1
            or not isinstance(candidate_sha256, str)
            or not isinstance(verification, dict)
            or verification.get("candidate_sha256") != candidate_sha256
        ):
            raise ValueError("Publication Plan inputs are inconsistent")
        marker = template.replace("{plan_sha256}", plan_sha256)
        verification_json = json.dumps(
            verification, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        now = self._now().isoformat()
        with self._connect() as connection:
            intent_row = connection.execute(
                f"SELECT {self._auto_fix_publication_intent_columns()} "
                "FROM auto_fix_publication_intents WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if intent_row is None:
                raise ValueError("Auto-fix Intent does not exist")
            intent = self._auto_fix_publication_intent_from_row(intent_row)
            expected_review_key = {
                "project_id": intent.project_id,
                "mr_iid": intent.mr_iid,
                "sha": intent.sha,
            }
            expected_intent = {
                "generation": intent.generation,
                "nonce": intent.nonce,
            }
            expected_template = (
                f"<!-- reviewflow:v1;project={intent.project_id};mr={intent.mr_iid};"
                f"sha={intent.sha};effect=autofix;intent={intent.nonce};"
                "plan_sha256={plan_sha256} -->"
            )
            expected_branch = f"autofix/MR-{intent.mr_iid}/sha-{intent.sha}/intent-{intent.nonce}"
            command_sha256 = verification.get("command_sha256")
            if (
                payload.get("review_key") != expected_review_key
                or payload.get("intent") != expected_intent
                or template != expected_template
                or payload.get("branch") != expected_branch
                or len(candidate_sha256) != 64
                or any(character not in "0123456789abcdef" for character in candidate_sha256)
                or not isinstance(command_sha256, str)
                or len(command_sha256) != 64
                or any(character not in "0123456789abcdef" for character in command_sha256)
            ):
                raise ValueError("Publication Plan identity does not match the immutable Intent")
            receipt = connection.execute(
                """
                SELECT id, state FROM effect_receipts
                WHERE id = ? AND run_id = ? AND effect_kind = 'autofix_plan'
                  AND dedupe_key = ?
                """,
                (receipt_id, run_id, f"autofix-intent:{intent.nonce}"),
            ).fetchone()
            if receipt is None:
                raise ValueError("Auto-fix effect receipt does not match the Intent")
            existing = connection.execute(
                f"SELECT {self._auto_fix_publication_plan_columns()} "
                "FROM auto_fix_publication_plans WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if existing is None:
                if receipt["state"] != "planned":
                    raise ValueError("Auto-fix effect receipt is not ready for Plan freeze")
                active = connection.execute(
                    """
                    SELECT 1 FROM review_runs
                    WHERE id = ? AND status = 'running' AND claim_token = ?
                      AND lease_until IS NOT NULL AND lease_until > ?
                    """,
                    (run_id, run_claim_token, now),
                ).fetchone()
                if active is None or intent.state != "pending":
                    return None
                connection.execute(
                    """
                    INSERT INTO auto_fix_publication_plans(
                        intent_id, run_id, plan_json, plan_sha256, marker,
                        candidate_sha256, verification_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        intent.intent_id,
                        run_id,
                        plan_json,
                        plan_sha256,
                        marker,
                        candidate_sha256,
                        verification_json,
                        now,
                        now,
                    ),
                )
                connection.execute(
                    """
                    UPDATE auto_fix_publication_intents
                    SET state = 'plan_frozen', updated_at = ?, last_error = NULL
                    WHERE id = ? AND state = 'pending'
                    """,
                    (now, intent.intent_id),
                )
                connection.execute(
                    """
                    UPDATE effect_receipts
                    SET state = 'plan_frozen',
                        detail = 'Verified immutable Auto-fix Publication Plan frozen',
                        error = NULL, updated_at = ?
                    WHERE id = ? AND state = 'planned'
                    """,
                    (now, receipt_id),
                )
                connection.execute(
                    """
                    INSERT INTO output_expectations(
                        run_id, output_kind, dedupe_key, created_at
                    ) VALUES (?, 'autofix', ?, ?)
                    ON CONFLICT(run_id, output_kind, dedupe_key) DO NOTHING
                    """,
                    (run_id, plan_sha256, now),
                )
                existing = connection.execute(
                    f"SELECT {self._auto_fix_publication_plan_columns()} "
                    "FROM auto_fix_publication_plans WHERE run_id = ?",
                    (run_id,),
                ).fetchone()
            assert existing is not None
            plan = self._auto_fix_publication_plan_from_row(existing)
            if (
                plan.intent_id != intent.intent_id
                or plan.plan_json != plan_json
                or plan.plan_sha256 != plan_sha256
                or plan.marker != marker
            ):
                raise ValueError("Auto-fix Publication Plan is immutable")
        return plan

    def get_auto_fix_publication_plan(self, run_id: int) -> AutoFixPublicationPlan | None:
        with self._connect() as connection:
            row = connection.execute(
                f"SELECT {self._auto_fix_publication_plan_columns()} "
                "FROM auto_fix_publication_plans WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        return self._auto_fix_publication_plan_from_row(row) if row is not None else None

    def claim_auto_fix_publication_plan(
        self,
        *,
        plan_id: int | None = None,
        project_id: str | None = None,
        lease_seconds: int = DEFAULT_AUTO_FIX_PUBLICATION_LEASE_SECONDS,
    ) -> AutoFixPublicationPlan | None:
        if lease_seconds <= 0:
            raise ValueError("Auto-fix publication lease must be positive")
        now_value = self._now()
        now = now_value.isoformat()
        lease_until = (now_value + timedelta(seconds=lease_seconds)).isoformat()
        filters = ""
        parameters: list[object] = [now]
        reclaim_filters = ""
        reclaim_parameters: list[object] = [now, now]
        if plan_id is not None:
            filters += " AND auto_fix_publication_plans.id = ?"
            parameters.append(plan_id)
            reclaim_filters += " AND id = ?"
            reclaim_parameters.append(plan_id)
        if project_id is not None:
            if not project_id.strip():
                raise ValueError("project_id cannot be empty")
            filters += " AND auto_fix_publication_intents.project_id = ?"
            parameters.append(project_id)
            reclaim_filters += (
                " AND EXISTS (SELECT 1 FROM auto_fix_publication_intents "
                "WHERE auto_fix_publication_intents.id = "
                "auto_fix_publication_plans.intent_id "
                "AND auto_fix_publication_intents.project_id = ?)"
            )
            reclaim_parameters.append(project_id)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                f"""
                UPDATE auto_fix_publication_plans
                SET state = 'frozen', lease_until = NULL, claim_token = NULL,
                    updated_at = ?, next_attempt_at = NULL,
                    last_error = COALESCE(
                        last_error, 'publication lease expired before any external write'
                    )
                WHERE state = 'running' AND write_phase = 'preflight'
                  AND (lease_until IS NULL OR lease_until <= ?)
                  {reclaim_filters}
                """,
                tuple(reclaim_parameters),
            )
            connection.execute(
                f"""
                UPDATE auto_fix_publication_plans
                SET state = 'needs_reconciliation', write_uncertain = 1,
                    lease_until = NULL, claim_token = NULL, updated_at = ?,
                    last_error = COALESCE(
                        last_error, 'publication lease expired with external result uncertain'
                    )
                WHERE state = 'running' AND write_phase != 'preflight'
                  AND (lease_until IS NULL OR lease_until <= ?)
                  {reclaim_filters}
                """,
                tuple(reclaim_parameters),
            )
            connection.execute(
                """
                UPDATE auto_fix_publication_intents
                SET state = 'needs_reconciliation', updated_at = ?,
                    last_error = COALESCE(
                        last_error, 'publication lease expired with external result uncertain'
                    )
                WHERE state = 'plan_frozen' AND id IN (
                    SELECT intent_id FROM auto_fix_publication_plans
                    WHERE state = 'needs_reconciliation' AND write_uncertain = 1
                )
                """,
                (now,),
            )
            connection.execute(
                """
                UPDATE effect_receipts
                SET state = 'needs_reconciliation', updated_at = ?,
                    error = COALESCE(
                        error, 'publication lease expired with external result uncertain'
                    )
                WHERE effect_kind = 'autofix_plan' AND state = 'plan_frozen'
                  AND run_id IN (
                    SELECT run_id FROM auto_fix_publication_plans
                    WHERE state = 'needs_reconciliation' AND write_uncertain = 1
                  )
                """,
                (now,),
            )
            row = connection.execute(
                f"""
                SELECT auto_fix_publication_plans.id
                FROM auto_fix_publication_plans
                JOIN auto_fix_publication_intents
                  ON auto_fix_publication_intents.id = auto_fix_publication_plans.intent_id
                WHERE auto_fix_publication_plans.state = 'frozen'
                  AND (auto_fix_publication_plans.next_attempt_at IS NULL
                       OR auto_fix_publication_plans.next_attempt_at <= ?)
                  {filters}
                ORDER BY auto_fix_publication_plans.id LIMIT 1
                """,
                tuple(parameters),
            ).fetchone()
            if row is None:
                return None
            token = uuid4().hex
            updated = connection.execute(
                """
                UPDATE auto_fix_publication_plans
                SET state = 'running', attempts = attempts + 1,
                    lease_until = ?, claim_token = ?, updated_at = ?, last_error = NULL
                WHERE id = ? AND state = 'frozen'
                """,
                (lease_until, token, now, row["id"]),
            )
            if updated.rowcount != 1:
                return None
            claimed = connection.execute(
                f"SELECT {self._auto_fix_publication_plan_columns()} "
                "FROM auto_fix_publication_plans WHERE id = ?",
                (row["id"],),
            ).fetchone()
        assert claimed is not None
        return self._auto_fix_publication_plan_from_row(claimed)

    def claim_auto_fix_reconciliation_plan(
        self,
        *,
        project_id: str,
        lease_seconds: int = DEFAULT_AUTO_FIX_RECONCILIATION_LEASE_SECONDS,
    ) -> AutoFixPublicationPlan | None:
        if not project_id.strip() or lease_seconds <= 0:
            raise ValueError("invalid Auto-fix reconciliation claim")
        now_value = self._now()
        now = now_value.isoformat()
        lease_until = (now_value + timedelta(seconds=lease_seconds)).isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE auto_fix_publication_plans
                SET state = 'needs_reconciliation', lease_until = NULL,
                    claim_token = NULL, updated_at = ?,
                    last_error = COALESCE(
                        last_error, 'reconciliation lease expired before completion'
                    )
                WHERE state = 'reconciling'
                  AND (lease_until IS NULL OR lease_until <= ?)
                  AND EXISTS (
                      SELECT 1 FROM auto_fix_publication_intents i
                      WHERE i.id = auto_fix_publication_plans.intent_id
                        AND i.project_id = ?
                  )
                """,
                (now, now, project_id),
            )
            row = connection.execute(
                """
                SELECT p.id
                FROM auto_fix_publication_plans p
                JOIN auto_fix_publication_intents i ON i.id = p.intent_id
                WHERE p.state = 'needs_reconciliation'
                  AND (p.next_attempt_at IS NULL OR p.next_attempt_at <= ?)
                  AND i.project_id = ?
                ORDER BY p.id LIMIT 1
                """,
                (now, project_id),
            ).fetchone()
            if row is None:
                return None
            token = uuid4().hex
            updated = connection.execute(
                """
                UPDATE auto_fix_publication_plans
                SET state = 'reconciling', attempts = attempts + 1,
                    lease_until = ?, claim_token = ?, updated_at = ?
                WHERE id = ? AND state = 'needs_reconciliation'
                """,
                (lease_until, token, now, row["id"]),
            )
            if updated.rowcount != 1:
                return None
            claimed = connection.execute(
                f"SELECT {self._auto_fix_publication_plan_columns()} "
                "FROM auto_fix_publication_plans WHERE id = ?",
                (row["id"],),
            ).fetchone()
        assert claimed is not None
        return self._auto_fix_publication_plan_from_row(claimed)

    def defer_auto_fix_reconciliation_plan(
        self,
        plan_id: int,
        claim_token: str | None,
        error: str,
        *,
        delay_seconds: float,
        max_attempts: int,
        confirm_absent: bool = False,
    ) -> bool:
        if not claim_token or delay_seconds < 0 or max_attempts <= 0:
            return False
        now_value = self._now()
        now = now_value.isoformat()
        next_attempt_at = (now_value + timedelta(seconds=delay_seconds)).isoformat()
        safe_error = self._redact(error)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT p.attempts, p.run_id, p.intent_id, i.nonce
                FROM auto_fix_publication_plans p
                JOIN auto_fix_publication_intents i ON i.id = p.intent_id
                WHERE p.id = ? AND p.state = 'reconciling' AND p.claim_token = ?
                  AND p.lease_until IS NOT NULL AND p.lease_until > ?
                """,
                (plan_id, claim_token, now),
            ).fetchone()
            if row is None:
                return False
            exhausted = int(row["attempts"]) >= max_attempts
            state = "blocked" if exhausted else "needs_reconciliation"
            connection.execute(
                """
                UPDATE auto_fix_publication_plans
                SET state = ?, next_attempt_at = ?, lease_until = NULL,
                    claim_token = NULL, last_error = ?, updated_at = ?,
                    absence_confirmations = absence_confirmations + ?,
                    absence_observed_at = CASE WHEN ? = 1
                        THEN COALESCE(absence_observed_at, ?)
                        ELSE absence_observed_at END
                WHERE id = ? AND state = 'reconciling' AND claim_token = ?
                """,
                (
                    state,
                    None if exhausted else next_attempt_at,
                    safe_error,
                    now,
                    int(confirm_absent),
                    int(confirm_absent),
                    now,
                    plan_id,
                    claim_token,
                ),
            )
            connection.execute(
                """
                UPDATE auto_fix_publication_intents
                SET state = ?, updated_at = ?, last_error = ? WHERE id = ?
                """,
                (state, now, safe_error, row["intent_id"]),
            )
            connection.execute(
                """
                UPDATE effect_receipts
                SET state = ?, error = ?, updated_at = ?
                WHERE run_id = ? AND effect_kind = 'autofix_plan'
                  AND dedupe_key = ?
                """,
                (
                    state,
                    safe_error,
                    now,
                    row["run_id"],
                    f"autofix-intent:{row['nonce']}",
                ),
            )
        return not exhausted

    def finish_auto_fix_publication_plan(
        self,
        plan_id: int,
        claim_token: str | None,
        *,
        state: str,
        error: str,
        write_uncertain: bool = False,
    ) -> bool:
        if state not in {"blocked", "superseded", "needs_reconciliation"}:
            raise ValueError("invalid Auto-fix Publication Plan terminal state")
        if not claim_token:
            return False
        now = self._now().isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT p.run_id, p.intent_id, i.nonce
                FROM auto_fix_publication_plans p
                JOIN auto_fix_publication_intents i ON i.id = p.intent_id
                WHERE p.id = ? AND p.state IN ('running', 'reconciling')
                  AND p.claim_token = ?
                  AND p.lease_until IS NOT NULL AND p.lease_until > ?
                """,
                (plan_id, claim_token, now),
            ).fetchone()
            if row is None:
                return False
            cursor = connection.execute(
                """
                UPDATE auto_fix_publication_plans
                SET state = ?, write_uncertain = ?, last_error = ?, updated_at = ?,
                    next_attempt_at = NULL, lease_until = NULL, claim_token = NULL
                WHERE id = ? AND state IN ('running', 'reconciling')
                  AND claim_token = ?
                  AND lease_until IS NOT NULL AND lease_until > ?
                """,
                (
                    state,
                    int(write_uncertain),
                    self._redact(error),
                    now,
                    plan_id,
                    claim_token,
                    now,
                ),
            )
            if cursor.rowcount == 1:
                connection.execute(
                    """
                    UPDATE auto_fix_publication_intents
                    SET state = ?, updated_at = ?, last_error = ?
                    WHERE id = ?
                    """,
                    (state, now, self._redact(error), row["intent_id"]),
                )
                connection.execute(
                    """
                    UPDATE effect_receipts
                    SET state = ?, detail = ?, error = ?, updated_at = ?
                    WHERE run_id = ? AND effect_kind = 'autofix_plan'
                      AND dedupe_key = ?
                      AND state IN ('plan_frozen', 'needs_reconciliation')
                    """,
                    (
                        state,
                        "Auto-fix publication requires reconciliation"
                        if state == "needs_reconciliation"
                        else "Auto-fix publication did not create a Relation",
                        self._redact(error),
                        now,
                        row["run_id"],
                        f"autofix-intent:{row['nonce']}",
                    ),
                )
        return cursor.rowcount == 1

    def extend_auto_fix_publication_lease(
        self,
        plan_id: int,
        claim_token: str | None,
        lease_seconds: int = DEFAULT_AUTO_FIX_PUBLICATION_LEASE_SECONDS,
    ) -> bool:
        if not claim_token or lease_seconds <= 0:
            return False
        now_value = self._now()
        now = now_value.isoformat()
        lease_until = (now_value + timedelta(seconds=lease_seconds)).isoformat()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE auto_fix_publication_plans
                SET lease_until = ?, updated_at = ?
                WHERE id = ? AND state IN ('running', 'reconciling')
                  AND claim_token = ?
                  AND lease_until IS NOT NULL AND lease_until > ?
                """,
                (lease_until, now, plan_id, claim_token, now),
            )
        return cursor.rowcount == 1

    def retry_auto_fix_publication_plan(
        self,
        plan_id: int,
        claim_token: str | None,
        error: str,
        *,
        delay_seconds: float,
        max_attempts: int,
    ) -> bool:
        if not claim_token:
            return False
        if delay_seconds < 0 or max_attempts <= 0:
            raise ValueError("invalid Auto-fix publication retry settings")
        now_value = self._now()
        now = now_value.isoformat()
        next_attempt = (now_value + timedelta(seconds=delay_seconds)).isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT p.attempts, p.run_id, p.intent_id, i.nonce
                FROM auto_fix_publication_plans p
                JOIN auto_fix_publication_intents i ON i.id = p.intent_id
                WHERE p.id = ? AND p.state = 'running' AND p.claim_token = ?
                  AND p.lease_until IS NOT NULL AND p.lease_until > ?
                  AND p.write_phase = 'preflight'
                """,
                (plan_id, claim_token, now),
            ).fetchone()
            if row is None:
                return False
            exhausted = int(row["attempts"]) >= max_attempts
            connection.execute(
                """
                UPDATE auto_fix_publication_plans
                SET state = ?, next_attempt_at = ?, lease_until = NULL,
                    claim_token = NULL, last_error = ?, updated_at = ?
                WHERE id = ? AND state = 'running' AND claim_token = ?
                """,
                (
                    "blocked" if exhausted else "frozen",
                    None if exhausted else next_attempt,
                    self._redact(error),
                    now,
                    plan_id,
                    claim_token,
                ),
            )
            if exhausted:
                safe_error = self._redact(error)
                connection.execute(
                    """
                    UPDATE auto_fix_publication_intents
                    SET state = 'blocked', updated_at = ?, last_error = ?
                    WHERE id = ?
                    """,
                    (now, safe_error, row["intent_id"]),
                )
                connection.execute(
                    """
                    UPDATE effect_receipts
                    SET state = 'blocked',
                        detail = 'Auto-fix publication preflight retry limit reached',
                        error = ?, updated_at = ?
                    WHERE run_id = ? AND effect_kind = 'autofix_plan'
                      AND dedupe_key = ? AND state = 'plan_frozen'
                    """,
                    (
                        safe_error,
                        now,
                        row["run_id"],
                        f"autofix-intent:{row['nonce']}",
                    ),
                )
        return not exhausted

    def record_auto_fix_branch_commit(
        self,
        plan_id: int,
        claim_token: str | None,
        commit_sha: str,
    ) -> bool:
        if not is_valid_sha(commit_sha):
            raise ValueError("invalid Auto-fix commit SHA")
        if not claim_token:
            return False
        now = self._now().isoformat()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE auto_fix_publication_plans
                SET branch_commit_sha = ?, write_phase = 'commit_confirmed',
                    write_uncertain = 0, updated_at = ?
                WHERE id = ? AND state = 'running' AND claim_token = ?
                  AND lease_until IS NOT NULL AND lease_until > ?
                  AND write_phase = 'commit_write_started'
                  AND (branch_commit_sha IS NULL OR branch_commit_sha = ?)
                """,
                (commit_sha, now, plan_id, claim_token, now, commit_sha),
            )
        return cursor.rowcount == 1

    def mark_auto_fix_write_started(
        self,
        plan_id: int,
        claim_token: str | None,
        *,
        operation: str,
    ) -> bool:
        """Persist the uncertainty boundary immediately before an external write."""

        transitions = {
            "commit": ("preflight", "commit_write_started"),
            "merge_request": ("commit_confirmed", "mr_write_started"),
        }
        if operation not in transitions:
            raise ValueError("invalid Auto-fix external write operation")
        if not claim_token:
            return False
        from_phase, to_phase = transitions[operation]
        now = self._now().isoformat()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE auto_fix_publication_plans
                SET write_phase = ?, write_uncertain = 1, updated_at = ?
                WHERE id = ? AND state IN ('running', 'reconciling')
                  AND claim_token = ?
                  AND lease_until IS NOT NULL AND lease_until > ?
                  AND write_phase = ?
                """,
                (to_phase, now, plan_id, claim_token, now, from_phase),
            )
        return cursor.rowcount == 1

    def record_auto_fix_merge_request(
        self,
        plan_id: int,
        claim_token: str | None,
        external_mr_iid: int,
    ) -> bool:
        """Persist a confirmed MR identity before Relation materialization."""

        if not claim_token or external_mr_iid <= 0:
            return False
        now = self._now().isoformat()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE auto_fix_publication_plans
                SET external_mr_iid = ?, write_phase = 'mr_confirmed',
                    write_uncertain = 0, updated_at = ?
                WHERE id = ? AND state IN ('running', 'reconciling')
                  AND claim_token = ?
                  AND lease_until IS NOT NULL AND lease_until > ?
                  AND write_phase IN ('commit_confirmed', 'mr_write_started')
                  AND (external_mr_iid IS NULL OR external_mr_iid = ?)
                """,
                (
                    external_mr_iid,
                    now,
                    plan_id,
                    claim_token,
                    now,
                    external_mr_iid,
                ),
            )
        return cursor.rowcount == 1

    def confirm_auto_fix_merge_request_absent(
        self,
        plan_id: int,
        claim_token: str | None,
    ) -> bool:
        """Clear an uncertain MR boundary after two durable negative observations."""

        if not claim_token:
            return False
        now_value = self._now()
        now = now_value.isoformat()
        cutoff = (
            now_value - timedelta(seconds=AUTO_FIX_MR_ABSENCE_CONFIRMATION_SECONDS)
        ).isoformat()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE auto_fix_publication_plans
                SET write_phase = 'commit_confirmed', write_uncertain = 0,
                    updated_at = ?
                WHERE id = ? AND state = 'reconciling' AND claim_token = ?
                  AND lease_until IS NOT NULL AND lease_until > ?
                  AND write_phase = 'mr_write_started'
                  AND absence_confirmations >= 1
                  AND absence_observed_at IS NOT NULL
                  AND absence_observed_at <= ?
                  AND external_mr_iid IS NULL
                """,
                (now, plan_id, claim_token, now, cutoff),
            )
        return cursor.rowcount == 1

    def complete_auto_fix_publication(
        self,
        plan_id: int,
        claim_token: str | None,
        *,
        auto_fix_project_id: str,
        auto_fix_mr_iid: int,
        source_branch: str,
        target_branch: str,
        marker: str,
        title: str,
        body: str,
        url: str,
        recipients: tuple[str, ...],
        channels: tuple[tuple[str, str], ...],
    ) -> tuple[ProgramAutoFixRelation, NotificationRequest] | None:
        """Persist Relation, Notification Request and deliveries in one transaction."""

        if not claim_token or auto_fix_mr_iid <= 0:
            return None
        if len({channel_id for channel_id, _channel in channels}) != len(channels):
            raise ValueError("notification channel IDs must be unique")
        payload_sha256 = self._notification_payload_sha256(title, body, url, recipients, channels)
        now = self._now().isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT p.id AS plan_id, p.run_id, p.plan_json, p.plan_sha256,
                       p.marker, p.branch_commit_sha, i.id AS intent_id,
                       i.project_id, i.mr_iid, i.sha, i.nonce
                FROM auto_fix_publication_plans p
                JOIN auto_fix_publication_intents i ON i.id = p.intent_id
                WHERE p.id = ? AND p.state IN ('running', 'reconciling')
                  AND p.claim_token = ?
                  AND p.lease_until IS NOT NULL AND p.lease_until > ?
                  AND p.branch_commit_sha IS NOT NULL
                  AND p.write_phase = 'mr_confirmed'
                  AND p.external_mr_iid = ?
                """,
                (plan_id, claim_token, now, auto_fix_mr_iid),
            ).fetchone()
            if row is None:
                return None
            payload = json.loads(str(row["plan_json"]))
            if (
                auto_fix_project_id != str(row["project_id"])
                or source_branch != payload.get("branch")
                or target_branch != payload.get("target_branch")
                or marker != str(row["marker"])
            ):
                raise ValueError("published Auto-fix identity does not match its frozen Plan")
            connection.execute(
                """
                INSERT INTO auto_fix_relations(
                    intent_id, plan_id, run_id, parent_project_id, parent_mr_iid,
                    parent_sha, auto_fix_project_id, auto_fix_mr_iid,
                    source_branch, target_branch, marker, plan_sha256,
                    published_commit_sha, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(plan_id) DO NOTHING
                """,
                (
                    row["intent_id"],
                    row["plan_id"],
                    row["run_id"],
                    row["project_id"],
                    row["mr_iid"],
                    row["sha"],
                    auto_fix_project_id,
                    auto_fix_mr_iid,
                    source_branch,
                    target_branch,
                    marker,
                    row["plan_sha256"],
                    row["branch_commit_sha"],
                    now,
                    now,
                ),
            )
            relation_row = connection.execute(
                f"""
                SELECT {self._program_auto_fix_relation_columns()}
                FROM auto_fix_relations WHERE plan_id = ?
                """,
                (plan_id,),
            ).fetchone()
            assert relation_row is not None
            relation = self._program_auto_fix_relation_from_row(relation_row)
            if (
                relation.auto_fix_project_id != auto_fix_project_id
                or relation.auto_fix_mr_iid != auto_fix_mr_iid
                or relation.source_branch != source_branch
                or relation.target_branch != target_branch
                or relation.marker != marker
            ):
                raise ValueError("Auto-fix Relation is immutable")
            correlation_id = f"autofix-plan:{row['plan_sha256']}"
            recipients_json = json.dumps(recipients)
            connection.execute(
                """
                INSERT INTO notification_requests(
                    relation_id, generation, request_kind, correlation_id, title, body, url,
                    recipients_json, payload_sha256, created_at
                ) VALUES (?, 1, 'publication', ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(relation_id, generation) DO NOTHING
                """,
                (
                    relation.relation_id,
                    correlation_id,
                    title,
                    body,
                    url,
                    recipients_json,
                    payload_sha256,
                    now,
                ),
            )
            request_row = connection.execute(
                """
                SELECT id AS request_id, relation_id, generation, request_kind,
                       correlation_id, title,
                       body, url, recipients_json, payload_sha256, created_at
                FROM notification_requests WHERE relation_id = ? AND generation = 1
                """,
                (relation.relation_id,),
            ).fetchone()
            assert request_row is not None
            request_values = dict(request_row)
            saved_payload_sha256 = str(request_values.pop("payload_sha256"))
            request_recipients = tuple(json.loads(request_values.pop("recipients_json")))
            request = NotificationRequest(**request_values, recipients=request_recipients)
            if (
                request.generation != 1
                or request.request_kind != "publication"
                or request.correlation_id != correlation_id
                or request.title != title
                or request.body != body
                or request.url != url
                or request.recipients != recipients
                or saved_payload_sha256 != payload_sha256
            ):
                raise ValueError("Auto-fix Notification Request is immutable")
            for channel_id, channel in channels:
                connection.execute(
                    """
                    INSERT INTO notification_outbox(
                        correlation_id, channel_id, channel, title, body, url,
                        recipients_json, next_attempt_at, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(correlation_id, channel_id) DO NOTHING
                    """,
                    (
                        correlation_id,
                        channel_id,
                        channel,
                        title,
                        body,
                        url,
                        recipients_json,
                        now,
                        now,
                    ),
                )
            connection.execute(
                """
                UPDATE auto_fix_publication_plans
                SET state = 'published', external_mr_iid = ?, write_uncertain = 0,
                    write_phase = 'published',
                    updated_at = ?, lease_until = NULL, claim_token = NULL,
                    last_error = NULL
                WHERE id = ?
                """,
                (auto_fix_mr_iid, now, plan_id),
            )
            connection.execute(
                """
                UPDATE auto_fix_publication_intents
                SET state = 'published', updated_at = ?, last_error = NULL
                WHERE id = ?
                """,
                (now, row["intent_id"]),
            )
            connection.execute(
                """
                UPDATE effect_receipts
                SET state = 'published',
                    external_identity = ?,
                    detail = 'Program-owned Auto-fix Merge Request published',
                    error = NULL, updated_at = ?
                WHERE run_id = ? AND effect_kind = 'autofix_plan'
                  AND dedupe_key = ?
                  AND state IN ('plan_frozen', 'needs_reconciliation')
                """,
                (
                    f"gitlab:{auto_fix_project_id}!{auto_fix_mr_iid}",
                    now,
                    row["run_id"],
                    f"autofix-intent:{row['nonce']}",
                ),
            )
        return relation, request

    def get_program_auto_fix_relation(self, plan_id: int) -> ProgramAutoFixRelation | None:
        with self._connect() as connection:
            row = connection.execute(
                f"""
                SELECT {self._program_auto_fix_relation_columns()}
                FROM auto_fix_relations WHERE plan_id = ?
                """,
                (plan_id,),
            ).fetchone()
        return self._program_auto_fix_relation_from_row(row) if row is not None else None

    def get_program_auto_fix_relation_by_child(
        self, project_id: str, mr_iid: int
    ) -> ProgramAutoFixRelation | None:
        with self._connect() as connection:
            row = connection.execute(
                f"SELECT {self._program_auto_fix_relation_columns()} "
                "FROM auto_fix_relations "
                "WHERE auto_fix_project_id = ? AND auto_fix_mr_iid = ?",
                (project_id, mr_iid),
            ).fetchone()
        return self._program_auto_fix_relation_from_row(row) if row is not None else None

    @staticmethod
    def _program_relation_divergence(
        relation: sqlite3.Row,
        source_branch: str,
        target_branch: str,
        description: str,
        source_sha: str | None = None,
    ) -> str | None:
        mismatches: list[str] = []
        if source_branch != str(relation["source_branch"]):
            mismatches.append("source_branch")
        if target_branch != str(relation["target_branch"]):
            mismatches.append("target_branch")
        if str(relation["marker"]) not in description:
            mismatches.append("marker")
        if source_sha is not None and source_sha != str(relation["published_commit_sha"]):
            mismatches.append("tree")
        return ",".join(mismatches) if mismatches else None

    def observe_program_auto_fix_webhook(
        self,
        project_id: str,
        mr_iid: int,
        *,
        status: str,
        merged_sha: str | None,
        source_branch: str | None,
        target_branch: str | None,
        description: str | None,
        source_sha: str | None = None,
    ) -> ProgramAutoFixRelation | None:
        """Update and wake only a pre-existing Program-owned Relation."""

        if status not in {"opened", "closed", "merged"}:
            raise ValueError("invalid Auto-fix relation status")
        if status == "merged" and not is_valid_sha(merged_sha):
            raise ValueError("merged Auto-fix relation requires a valid SHA")
        now = self._now().isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM auto_fix_relations "
                "WHERE auto_fix_project_id = ? AND auto_fix_mr_iid = ?",
                (project_id, mr_iid),
            ).fetchone()
            if row is None:
                return None
            divergence = str(row["divergence"]) if row["divergence"] else None
            if source_branch is not None and target_branch is not None and description is not None:
                divergence = self._program_relation_divergence(
                    row, source_branch, target_branch, description, source_sha
                )
            connection.execute(
                """
                UPDATE auto_fix_relations
                SET status = CASE WHEN status = 'merged' THEN status ELSE ? END,
                    merged_sha = CASE
                        WHEN status = 'merged' THEN merged_sha
                        WHEN ? = 'merged' THEN ?
                        ELSE NULL
                    END,
                    divergence = ?, reconcile_requested = 1,
                    reconcile_next_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    status,
                    status,
                    merged_sha,
                    divergence,
                    now,
                    now,
                    row["id"],
                ),
            )
            updated = connection.execute(
                f"SELECT {self._program_auto_fix_relation_columns()} "
                "FROM auto_fix_relations WHERE id = ?",
                (row["id"],),
            ).fetchone()
        assert updated is not None
        return self._program_auto_fix_relation_from_row(updated)

    def claim_program_auto_fix_relation(
        self,
        *,
        project_id: str,
        lease_seconds: int = DEFAULT_AUTO_FIX_RECONCILIATION_LEASE_SECONDS,
    ) -> ProgramAutoFixRelation | None:
        if not project_id.strip() or lease_seconds <= 0:
            raise ValueError("invalid Auto-fix Relation reconciliation claim")
        now_value = self._now()
        now = now_value.isoformat()
        lease_until = (now_value + timedelta(seconds=lease_seconds)).isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE auto_fix_relations
                SET reconcile_lease_until = NULL, reconcile_claim_token = NULL,
                    reconcile_requested = 1, reconcile_next_at = ?, updated_at = ?,
                    last_error = COALESCE(
                        last_error, 'Relation reconciliation lease expired'
                    )
                WHERE auto_fix_project_id = ?
                  AND reconcile_claim_token IS NOT NULL
                  AND (reconcile_lease_until IS NULL OR reconcile_lease_until <= ?)
                """,
                (now, now, project_id, now),
            )
            row = connection.execute(
                """
                SELECT id FROM auto_fix_relations
                WHERE auto_fix_project_id = ?
                  AND reconcile_claim_token IS NULL
                  AND (
                      reconcile_requested = 1
                      OR (
                          status != 'merged'
                          AND (reconcile_next_at IS NULL OR reconcile_next_at <= ?)
                      )
                  )
                ORDER BY reconcile_requested DESC, id
                LIMIT 1
                """,
                (project_id, now),
            ).fetchone()
            if row is None:
                return None
            token = uuid4().hex
            updated = connection.execute(
                """
                UPDATE auto_fix_relations
                SET reconcile_claim_token = ?, reconcile_lease_until = ?,
                    reconcile_attempts = reconcile_attempts + 1,
                    reconcile_requested = 0, updated_at = ?
                WHERE id = ? AND reconcile_claim_token IS NULL
                """,
                (token, lease_until, now, row["id"]),
            )
            if updated.rowcount != 1:
                return None
            claimed = connection.execute(
                f"SELECT {self._program_auto_fix_relation_columns()} "
                "FROM auto_fix_relations WHERE id = ?",
                (row["id"],),
            ).fetchone()
        assert claimed is not None
        return self._program_auto_fix_relation_from_row(claimed)

    def finish_program_auto_fix_relation_reconciliation(
        self,
        relation_id: int,
        claim_token: str | None,
        *,
        status: str,
        merged_sha: str | None,
        source_branch: str,
        target_branch: str,
        description: str,
        source_sha: str,
        error: str | None = None,
        retry_delay_seconds: float = 300.0,
    ) -> bool:
        if not claim_token or retry_delay_seconds < 0:
            return False
        if status not in {"opened", "closed", "merged"}:
            raise ValueError("invalid Auto-fix relation status")
        if status == "merged" and not is_valid_sha(merged_sha):
            raise ValueError("merged Auto-fix relation requires a valid SHA")
        now_value = self._now()
        now = now_value.isoformat()
        next_at = (now_value + timedelta(seconds=retry_delay_seconds)).isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT * FROM auto_fix_relations
                WHERE id = ? AND reconcile_claim_token = ?
                  AND reconcile_lease_until IS NOT NULL
                  AND reconcile_lease_until > ?
                """,
                (relation_id, claim_token, now),
            ).fetchone()
            if row is None:
                return False
            divergence = self._program_relation_divergence(
                row, source_branch, target_branch, description, source_sha
            )
            cursor = connection.execute(
                """
                UPDATE auto_fix_relations
                SET status = CASE WHEN status = 'merged' THEN status ELSE ? END,
                    merged_sha = CASE
                        WHEN status = 'merged' THEN merged_sha
                        WHEN ? = 'merged' THEN ?
                        ELSE NULL
                    END,
                    divergence = ?,
                    reconcile_next_at = CASE
                        WHEN status = 'merged' OR ? = 'merged' THEN NULL
                        ELSE ?
                    END,
                    reconcile_lease_until = NULL,
                    reconcile_claim_token = NULL, last_error = ?, updated_at = ?
                WHERE id = ? AND reconcile_claim_token = ?
                """,
                (
                    status,
                    status,
                    merged_sha if status == "merged" else None,
                    divergence,
                    status,
                    next_at,
                    self._redact(error) if error else None,
                    now,
                    relation_id,
                    claim_token,
                ),
            )
        return cursor.rowcount == 1

    def defer_program_auto_fix_relation_reconciliation(
        self,
        relation_id: int,
        claim_token: str | None,
        error: str,
        *,
        retry_delay_seconds: float,
    ) -> bool:
        """Release a Relation claim without rewriting its observed identity or state."""

        if not claim_token or retry_delay_seconds < 0:
            return False
        now_value = self._now()
        now = now_value.isoformat()
        next_at = (now_value + timedelta(seconds=retry_delay_seconds)).isoformat()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE auto_fix_relations
                SET reconcile_next_at = ?, reconcile_lease_until = NULL,
                    reconcile_claim_token = NULL, last_error = ?, updated_at = ?
                WHERE id = ? AND reconcile_claim_token = ?
                  AND reconcile_lease_until IS NOT NULL
                  AND reconcile_lease_until > ?
                """,
                (
                    next_at,
                    self._redact(error),
                    now,
                    relation_id,
                    claim_token,
                    now,
                ),
            )
        return cursor.rowcount == 1

    def get_auto_fix_notification_request(
        self, relation_id: int, generation: int | None = None
    ) -> NotificationRequest | None:
        generation_filter = "" if generation is None else " AND generation = ?"
        parameters: tuple[object, ...] = (relation_id,)
        if generation is not None:
            parameters += (generation,)
        with self._connect() as connection:
            row = connection.execute(
                f"""
                SELECT id AS request_id, relation_id, generation, request_kind,
                       correlation_id, title,
                       body, url, recipients_json, created_at
                FROM notification_requests WHERE relation_id = ?
                {generation_filter}
                ORDER BY generation DESC LIMIT 1
                """,
                parameters,
            ).fetchone()
        if row is None:
            return None
        values = dict(row)
        recipients = tuple(json.loads(values.pop("recipients_json")))
        return NotificationRequest(**values, recipients=recipients)

    @staticmethod
    def _validate_operator_audit_values(
        *,
        recovery_id: str,
        action: str,
        project_id: str,
        parent_mr_iid: int,
        parent_sha: str,
        intent_nonce: str,
        plan_sha256: str,
        child_project_id: str,
        child_mr_iid: int,
        reason: str,
        principal: str,
        hostname: str,
        tool_version: str,
        strict_identity: bool = True,
    ) -> None:
        if action not in {"relation_recovery", "notification_resend"}:
            raise ValueError("invalid Operator recovery action")
        text_values = (
            recovery_id,
            project_id,
            parent_sha,
            intent_nonce,
            plan_sha256,
            child_project_id,
            reason,
            principal,
            hostname,
            tool_version,
        )
        if any(not isinstance(value, str) or not value.strip() for value in text_values):
            raise ValueError("Operator recovery audit fields cannot be empty")
        if strict_identity and (parent_mr_iid <= 0 or child_mr_iid <= 0):
            raise ValueError("Operator recovery Merge Request IIDs must be positive")
        if strict_identity and not is_valid_sha(parent_sha):
            raise ValueError("Operator recovery parent SHA is invalid")
        if strict_identity and (
            len(plan_sha256) != 64
            or any(character not in "0123456789abcdef" for character in plan_sha256)
        ):
            raise ValueError("Operator recovery Plan digest is invalid")

    @staticmethod
    def _operator_recovery_from_row(row: sqlite3.Row) -> OperatorRecovery:
        return OperatorRecovery(**dict(row))

    @staticmethod
    def _operator_recovery_columns() -> str:
        return (
            "id AS audit_id, recovery_id, action, relation_id, project_id, "
            "parent_mr_iid, parent_sha, intent_nonce, plan_sha256, "
            "child_project_id, child_mr_iid, reason, principal, hostname, "
            "tool_version, result, diagnostic, created_at"
        )

    def record_operator_recovery_failure(
        self,
        *,
        recovery_id: str,
        action: str,
        project_id: str,
        parent_mr_iid: int,
        parent_sha: str,
        intent_nonce: str,
        plan_sha256: str,
        child_project_id: str,
        child_mr_iid: int,
        reason: str,
        principal: str,
        hostname: str,
        tool_version: str,
        diagnostic: str,
    ) -> OperatorRecovery:
        """Persist a failed explicit action without creating local authority."""

        self._validate_operator_audit_values(
            recovery_id=recovery_id,
            action=action,
            project_id=project_id,
            parent_mr_iid=parent_mr_iid,
            parent_sha=parent_sha,
            intent_nonce=intent_nonce,
            plan_sha256=plan_sha256,
            child_project_id=child_project_id,
            child_mr_iid=child_mr_iid,
            reason=reason,
            principal=principal,
            hostname=hostname,
            tool_version=tool_version,
            strict_identity=False,
        )
        now = self._now().isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO operator_recoveries(
                    recovery_id, action, project_id, parent_mr_iid, parent_sha,
                    intent_nonce, plan_sha256, child_project_id, child_mr_iid,
                    reason, principal, hostname, tool_version, result,
                    diagnostic, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'failed', ?, ?)
                ON CONFLICT(recovery_id) DO NOTHING
                """,
                (
                    recovery_id,
                    action,
                    project_id,
                    parent_mr_iid,
                    parent_sha,
                    intent_nonce,
                    plan_sha256,
                    child_project_id,
                    child_mr_iid,
                    reason,
                    principal,
                    hostname,
                    tool_version,
                    self._redact(diagnostic),
                    now,
                ),
            )
            row = connection.execute(
                f"SELECT {self._operator_recovery_columns()} "
                "FROM operator_recoveries WHERE recovery_id = ?",
                (recovery_id,),
            ).fetchone()
        assert row is not None
        audit = self._operator_recovery_from_row(row)
        if (
            audit.action != action
            or audit.result != "failed"
            or audit.project_id != project_id
            or audit.parent_mr_iid != parent_mr_iid
            or audit.parent_sha != parent_sha
            or audit.intent_nonce != intent_nonce
            or audit.plan_sha256 != plan_sha256
            or audit.child_project_id != child_project_id
            or audit.child_mr_iid != child_mr_iid
            or audit.reason != reason
            or audit.principal != principal
            or audit.hostname != hostname
            or audit.tool_version != tool_version
        ):
            raise ValueError("Operator recovery ID is already bound to another action")
        return audit

    def recover_program_auto_fix_relation(
        self,
        *,
        recovery_id: str,
        project_id: str,
        parent_mr_iid: int,
        parent_sha: str,
        intent_nonce: str,
        plan_json: str,
        plan_sha256: str,
        marker: str,
        source_branch: str,
        target_branch: str,
        child_project_id: str,
        child_mr_iid: int,
        published_commit_sha: str,
        status: str,
        merged_sha: str | None,
        reason: str,
        principal: str,
        hostname: str,
        tool_version: str,
    ) -> ProgramAutoFixRelation:
        """Atomically reconstruct one exact relation after explicit validation."""

        self._validate_operator_audit_values(
            recovery_id=recovery_id,
            action="relation_recovery",
            project_id=project_id,
            parent_mr_iid=parent_mr_iid,
            parent_sha=parent_sha,
            intent_nonce=intent_nonce,
            plan_sha256=plan_sha256,
            child_project_id=child_project_id,
            child_mr_iid=child_mr_iid,
            reason=reason,
            principal=principal,
            hostname=hostname,
            tool_version=tool_version,
        )
        if child_project_id != project_id:
            raise ValueError("cross-Project Auto-fix recovery is unsupported")
        if not is_valid_sha(published_commit_sha):
            raise ValueError("Operator recovery child commit SHA is invalid")
        if status not in {"opened", "closed", "merged"}:
            raise ValueError("Operator recovery child state is invalid")
        if status == "merged" and not is_valid_sha(merged_sha):
            raise ValueError("merged recovery requires a valid merge SHA")
        if status != "merged" and merged_sha is not None:
            raise ValueError("only a merged recovery may include a merge SHA")
        payload = PublicationPlanV1.from_json(plan_json, plan_sha256)
        if (
            payload.review_key.project_id != project_id
            or payload.review_key.mr_iid != parent_mr_iid
            or payload.review_key.sha != parent_sha
            or payload.intent.nonce != intent_nonce
            or payload.branch != source_branch
            or payload.target_branch != target_branch
            or payload.marker != marker
        ):
            raise ValueError("Operator recovery Plan identity is inconsistent")
        candidate_sha256 = payload.candidate_sha256
        generation = payload.intent.generation
        verification_json = json.dumps(
            {
                "candidate_sha256": payload.verification.candidate_sha256,
                "command_sha256": payload.verification.command_sha256,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        now = self._now().isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing_audit = connection.execute(
                f"SELECT {self._operator_recovery_columns()} FROM operator_recoveries "
                "WHERE recovery_id = ?",
                (recovery_id,),
            ).fetchone()
            if existing_audit is not None:
                if (
                    existing_audit["action"] != "relation_recovery"
                    or existing_audit["result"] != "succeeded"
                    or existing_audit["relation_id"] is None
                    or existing_audit["project_id"] != project_id
                    or existing_audit["parent_mr_iid"] != parent_mr_iid
                    or existing_audit["parent_sha"] != parent_sha
                    or existing_audit["intent_nonce"] != intent_nonce
                    or existing_audit["plan_sha256"] != plan_sha256
                    or existing_audit["child_project_id"] != child_project_id
                    or existing_audit["child_mr_iid"] != child_mr_iid
                    or existing_audit["reason"] != reason
                    or existing_audit["principal"] != principal
                    or existing_audit["hostname"] != hostname
                    or existing_audit["tool_version"] != tool_version
                ):
                    raise ValueError("Operator recovery ID is already bound to another result")
                row = connection.execute(
                    f"SELECT {self._program_auto_fix_relation_columns()} "
                    "FROM auto_fix_relations WHERE id = ?",
                    (existing_audit["relation_id"],),
                ).fetchone()
                assert row is not None
                return self._program_auto_fix_relation_from_row(row)

            connection.execute(
                """
                INSERT INTO review_runs(
                    project_id, mr_iid, sha, status, created_at, finished_at
                ) VALUES (?, ?, ?, 'succeeded', ?, ?)
                ON CONFLICT(project_id, mr_iid, sha) DO NOTHING
                """,
                (project_id, parent_mr_iid, parent_sha, now, now),
            )
            run = connection.execute(
                "SELECT id, status FROM review_runs "
                "WHERE project_id = ? AND mr_iid = ? AND sha = ?",
                (project_id, parent_mr_iid, parent_sha),
            ).fetchone()
            assert run is not None
            if run["status"] != "succeeded":
                raise ValueError("existing Review Run is not compatible with recovery")
            connection.execute(
                """
                INSERT INTO auto_fix_publication_intents(
                    run_id, project_id, mr_iid, sha, generation, nonce, state,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'published', ?, ?)
                ON CONFLICT(run_id) DO NOTHING
                """,
                (
                    run["id"],
                    project_id,
                    parent_mr_iid,
                    parent_sha,
                    generation,
                    intent_nonce,
                    now,
                    now,
                ),
            )
            intent = connection.execute(
                "SELECT id, generation, nonce FROM auto_fix_publication_intents WHERE run_id = ?",
                (run["id"],),
            ).fetchone()
            assert intent is not None
            if intent["generation"] != generation or intent["nonce"] != intent_nonce:
                raise ValueError("existing Auto-fix Intent conflicts with recovery")
            connection.execute(
                """
                INSERT INTO auto_fix_publication_plans(
                    intent_id, run_id, plan_json, plan_sha256, marker,
                    candidate_sha256, verification_json, state, created_at,
                    updated_at, write_phase, branch_commit_sha, external_mr_iid
                ) VALUES (?, ?, NULL, ?, ?, ?, ?, 'published', ?, ?, 'published', ?, ?)
                ON CONFLICT(run_id) DO NOTHING
                """,
                (
                    intent["id"],
                    run["id"],
                    plan_sha256,
                    marker,
                    candidate_sha256,
                    verification_json,
                    now,
                    now,
                    published_commit_sha,
                    child_mr_iid,
                ),
            )
            plan = connection.execute(
                "SELECT id, plan_sha256, marker, branch_commit_sha, external_mr_iid "
                "FROM auto_fix_publication_plans WHERE run_id = ?",
                (run["id"],),
            ).fetchone()
            assert plan is not None
            if (
                plan["plan_sha256"] != plan_sha256
                or plan["marker"] != marker
                or plan["branch_commit_sha"] != published_commit_sha
                or plan["external_mr_iid"] != child_mr_iid
            ):
                raise ValueError("existing Publication Plan conflicts with recovery")
            connection.execute(
                """
                INSERT INTO auto_fix_relations(
                    intent_id, plan_id, run_id, parent_project_id, parent_mr_iid,
                    parent_sha, auto_fix_project_id, auto_fix_mr_iid,
                    source_branch, target_branch, marker, plan_sha256,
                    published_commit_sha, status, merged_sha, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(plan_id) DO NOTHING
                """,
                (
                    intent["id"],
                    plan["id"],
                    run["id"],
                    project_id,
                    parent_mr_iid,
                    parent_sha,
                    child_project_id,
                    child_mr_iid,
                    source_branch,
                    target_branch,
                    marker,
                    plan_sha256,
                    published_commit_sha,
                    status,
                    merged_sha,
                    now,
                    now,
                ),
            )
            relation_row = connection.execute(
                f"SELECT {self._program_auto_fix_relation_columns()} "
                "FROM auto_fix_relations WHERE plan_id = ?",
                (plan["id"],),
            ).fetchone()
            assert relation_row is not None
            relation = self._program_auto_fix_relation_from_row(relation_row)
            if (
                relation.auto_fix_project_id != child_project_id
                or relation.auto_fix_mr_iid != child_mr_iid
                or relation.source_branch != source_branch
                or relation.target_branch != target_branch
                or relation.status != status
            ):
                raise ValueError("existing Auto-fix Relation conflicts with recovery")
            connection.execute(
                """
                INSERT INTO effect_receipts(
                    run_id, effect_kind, dedupe_key, attempt, state,
                    external_identity, detail, created_at, updated_at
                ) VALUES (?, 'autofix_plan', ?, 1, 'published', ?, ?, ?, ?)
                ON CONFLICT(run_id, effect_kind, dedupe_key, attempt) DO NOTHING
                """,
                (
                    run["id"],
                    f"autofix-intent:{intent_nonce}",
                    f"gitlab:{child_project_id}!{child_mr_iid}",
                    "Relation reconstructed by explicit Operator recovery",
                    now,
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO operator_recoveries(
                    recovery_id, action, relation_id, project_id, parent_mr_iid,
                    parent_sha, intent_nonce, plan_sha256, child_project_id,
                    child_mr_iid, reason, principal, hostname, tool_version,
                    result, created_at
                ) VALUES (?, 'relation_recovery', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                          'succeeded', ?)
                """,
                (
                    recovery_id,
                    relation.relation_id,
                    project_id,
                    parent_mr_iid,
                    parent_sha,
                    intent_nonce,
                    plan_sha256,
                    child_project_id,
                    child_mr_iid,
                    reason,
                    principal,
                    hostname,
                    tool_version,
                    now,
                ),
            )
        return relation

    def create_auto_fix_notification_resend(
        self,
        relation_id: int,
        *,
        recovery_id: str,
        title: str,
        body: str,
        url: str,
        recipients: tuple[str, ...],
        channels: tuple[tuple[str, str], ...],
        reason: str,
        principal: str,
        hostname: str,
        tool_version: str,
    ) -> NotificationRequest:
        """Create a new immutable request; never mutate historical delivery state."""

        if len({channel_id for channel_id, _channel in channels}) != len(channels):
            raise ValueError("notification channel IDs must be unique")
        if not channels:
            raise ValueError("notification resend requires at least one channel")
        payload_sha256 = self._notification_payload_sha256(title, body, url, recipients, channels)
        now = self._now().isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            relation = connection.execute(
                "SELECT * FROM auto_fix_relations WHERE id = ?", (relation_id,)
            ).fetchone()
            if relation is None:
                raise ValueError("Auto-fix Relation does not exist")
            self._validate_operator_audit_values(
                recovery_id=recovery_id,
                action="notification_resend",
                project_id=str(relation["parent_project_id"]),
                parent_mr_iid=int(relation["parent_mr_iid"]),
                parent_sha=str(relation["parent_sha"]),
                intent_nonce=str(
                    connection.execute(
                        "SELECT nonce FROM auto_fix_publication_intents WHERE id = ?",
                        (relation["intent_id"],),
                    ).fetchone()[0]
                ),
                plan_sha256=str(relation["plan_sha256"]),
                child_project_id=str(relation["auto_fix_project_id"]),
                child_mr_iid=int(relation["auto_fix_mr_iid"]),
                reason=reason,
                principal=principal,
                hostname=hostname,
                tool_version=tool_version,
            )
            existing_audit = connection.execute(
                f"SELECT {self._operator_recovery_columns()} FROM operator_recoveries "
                "WHERE recovery_id = ?",
                (recovery_id,),
            ).fetchone()
            correlation_id = f"autofix-plan:{relation['plan_sha256']}:resend:{recovery_id}"
            if existing_audit is not None:
                if (
                    existing_audit["action"] != "notification_resend"
                    or existing_audit["result"] != "succeeded"
                    or existing_audit["relation_id"] != relation_id
                    or existing_audit["project_id"] != relation["parent_project_id"]
                    or existing_audit["parent_mr_iid"] != relation["parent_mr_iid"]
                    or existing_audit["parent_sha"] != relation["parent_sha"]
                    or existing_audit["plan_sha256"] != relation["plan_sha256"]
                    or existing_audit["child_project_id"] != relation["auto_fix_project_id"]
                    or existing_audit["child_mr_iid"] != relation["auto_fix_mr_iid"]
                    or existing_audit["reason"] != reason
                    or existing_audit["principal"] != principal
                    or existing_audit["hostname"] != hostname
                    or existing_audit["tool_version"] != tool_version
                ):
                    raise ValueError("Operator recovery ID is already bound to another result")
                request_row = connection.execute(
                    "SELECT id AS request_id, relation_id, generation, request_kind, "
                    "correlation_id, title, body, url, recipients_json, created_at, "
                    "payload_sha256, payload_purged_at "
                    "FROM notification_requests WHERE correlation_id = ?",
                    (correlation_id,),
                ).fetchone()
                assert request_row is not None
                values = dict(request_row)
                payload_purged = values.pop("payload_purged_at") is not None
                saved_payload_sha256 = str(values.pop("payload_sha256"))
                if saved_payload_sha256 != payload_sha256:
                    raise ValueError("Operator notification resend is immutable")
                saved_recipients = tuple(json.loads(values.pop("recipients_json")))
                if payload_purged:
                    values.update(title=title, body=body, url=url)
                    saved_recipients = recipients
                return NotificationRequest(**values, recipients=saved_recipients)
            generation = (
                int(
                    connection.execute(
                        "SELECT COALESCE(MAX(generation), 0) FROM notification_requests "
                        "WHERE relation_id = ?",
                        (relation_id,),
                    ).fetchone()[0]
                )
                + 1
            )
            recipients_json = json.dumps(recipients, separators=(",", ":"))
            connection.execute(
                """
                INSERT INTO notification_requests(
                    relation_id, generation, request_kind, correlation_id,
                    title, body, url, recipients_json, payload_sha256, created_at
                ) VALUES (?, ?, 'operator_resend', ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    relation_id,
                    generation,
                    correlation_id,
                    title,
                    body,
                    url,
                    recipients_json,
                    payload_sha256,
                    now,
                ),
            )
            for channel_id, channel in channels:
                connection.execute(
                    """
                    INSERT INTO notification_outbox(
                        correlation_id, channel_id, channel, title, body, url,
                        recipients_json, next_attempt_at, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        correlation_id,
                        channel_id,
                        channel,
                        title,
                        body,
                        url,
                        recipients_json,
                        now,
                        now,
                    ),
                )
            intent_nonce = str(
                connection.execute(
                    "SELECT nonce FROM auto_fix_publication_intents WHERE id = ?",
                    (relation["intent_id"],),
                ).fetchone()[0]
            )
            connection.execute(
                """
                INSERT INTO operator_recoveries(
                    recovery_id, action, relation_id, project_id, parent_mr_iid,
                    parent_sha, intent_nonce, plan_sha256, child_project_id,
                    child_mr_iid, reason, principal, hostname, tool_version,
                    result, created_at
                ) VALUES (?, 'notification_resend', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                          'succeeded', ?)
                """,
                (
                    recovery_id,
                    relation_id,
                    relation["parent_project_id"],
                    relation["parent_mr_iid"],
                    relation["parent_sha"],
                    intent_nonce,
                    relation["plan_sha256"],
                    relation["auto_fix_project_id"],
                    relation["auto_fix_mr_iid"],
                    reason,
                    principal,
                    hostname,
                    tool_version,
                    now,
                ),
            )
            request_row = connection.execute(
                "SELECT id AS request_id, relation_id, generation, request_kind, "
                "correlation_id, title, body, url, recipients_json, created_at "
                "FROM notification_requests WHERE correlation_id = ?",
                (correlation_id,),
            ).fetchone()
        assert request_row is not None
        values = dict(request_row)
        saved_recipients = tuple(json.loads(values.pop("recipients_json")))
        return NotificationRequest(**values, recipients=saved_recipients)

    def list_operator_recoveries(self) -> tuple[OperatorRecovery, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT {self._operator_recovery_columns()} FROM operator_recoveries ORDER BY id"
            ).fetchall()
        return tuple(self._operator_recovery_from_row(row) for row in rows)

    @staticmethod
    def _operator_plan_action_from_row(row: sqlite3.Row) -> OperatorPlanAction:
        return OperatorPlanAction(**dict(row))

    @staticmethod
    def _operator_plan_action_columns() -> str:
        return (
            "id AS audit_id, action_id, plan_id, plan_sha256, action, "
            "from_state, to_state, write_phase, reason, principal, hostname, "
            "tool_version, result, diagnostic, created_at"
        )

    def transition_auto_fix_publication_plan(
        self,
        plan_id: int,
        *,
        action_id: str,
        action: str,
        reason: str,
        principal: str,
        hostname: str,
        tool_version: str,
    ) -> OperatorPlanAction:
        """Apply an idempotent, audited Operator rearm or abandon decision."""

        if plan_id <= 0 or action not in {"rearm", "abandon"}:
            raise ValueError("invalid Operator Auto-fix Plan action")
        text_values = (action_id, reason, principal, hostname, tool_version)
        if any(not value.strip() for value in text_values):
            raise ValueError("Operator Auto-fix Plan action fields cannot be empty")
        now = self._now().isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                f"SELECT {self._operator_plan_action_columns()} "
                "FROM operator_plan_actions WHERE action_id = ?",
                (action_id,),
            ).fetchone()
            if existing is not None:
                audit = self._operator_plan_action_from_row(existing)
                if (
                    audit.plan_id != plan_id
                    or audit.action != action
                    or audit.reason != reason
                    or audit.principal != principal
                    or audit.hostname != hostname
                    or audit.tool_version != tool_version
                ):
                    raise ValueError("Operator action ID is already bound to another action")
                return audit
            row = connection.execute(
                """
                SELECT p.id, p.run_id, p.intent_id, p.plan_sha256, p.state,
                       p.write_phase, i.nonce
                FROM auto_fix_publication_plans p
                JOIN auto_fix_publication_intents i ON i.id = p.intent_id
                WHERE p.id = ?
                """,
                (plan_id,),
            ).fetchone()
            if row is None:
                raise ValueError("Auto-fix Publication Plan does not exist")
            from_state = str(row["state"])
            write_phase = str(row["write_phase"])
            allowed = (
                from_state == "blocked"
                if action == "rearm"
                else from_state in {"frozen", "blocked", "needs_reconciliation"}
            )
            if action == "rearm":
                to_state = "frozen" if write_phase == "preflight" else "needs_reconciliation"
            else:
                to_state = "abandoned"
            result = "succeeded" if allowed else "rejected"
            diagnostic = (
                None
                if allowed
                else self._redact(f"cannot {action} Auto-fix Plan from state {from_state}")
            )
            if allowed:
                connection.execute(
                    """
                    UPDATE auto_fix_publication_plans
                    SET state = ?, attempts = CASE WHEN ? = 'rearm' THEN 0 ELSE attempts END,
                        absence_confirmations = CASE WHEN ? = 'rearm' THEN 0
                                                     ELSE absence_confirmations END,
                        absence_observed_at = CASE WHEN ? = 'rearm' THEN NULL
                                                   ELSE absence_observed_at END,
                        next_attempt_at = NULL, lease_until = NULL, claim_token = NULL,
                        last_error = CASE WHEN ? = 'rearm' THEN NULL ELSE ? END,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        to_state,
                        action,
                        action,
                        action,
                        action,
                        self._redact(f"abandoned by Operator: {reason}"),
                        now,
                        plan_id,
                    ),
                )
                connection.execute(
                    """
                    UPDATE auto_fix_publication_intents
                    SET state = ?, updated_at = ?, last_error = ? WHERE id = ?
                    """,
                    (
                        "plan_frozen" if to_state == "frozen" else to_state,
                        now,
                        None if action == "rearm" else self._redact(reason),
                        row["intent_id"],
                    ),
                )
                connection.execute(
                    """
                    UPDATE effect_receipts
                    SET state = ?, detail = ?, error = ?, updated_at = ?
                    WHERE run_id = ? AND effect_kind = 'autofix_plan'
                      AND dedupe_key = ?
                    """,
                    (
                        "plan_frozen" if to_state == "frozen" else to_state,
                        "Auto-fix publication rearmed by Operator"
                        if action == "rearm"
                        else "Auto-fix publication abandoned by Operator",
                        None if action == "rearm" else self._redact(reason),
                        now,
                        row["run_id"],
                        f"autofix-intent:{row['nonce']}",
                    ),
                )
            connection.execute(
                """
                INSERT INTO operator_plan_actions(
                    action_id, plan_id, plan_sha256, action, from_state, to_state,
                    write_phase, reason, principal, hostname, tool_version,
                    result, diagnostic, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    action_id,
                    plan_id,
                    row["plan_sha256"],
                    action,
                    from_state,
                    to_state,
                    write_phase,
                    reason,
                    principal,
                    hostname,
                    tool_version,
                    result,
                    diagnostic,
                    now,
                ),
            )
            saved = connection.execute(
                f"SELECT {self._operator_plan_action_columns()} "
                "FROM operator_plan_actions WHERE action_id = ?",
                (action_id,),
            ).fetchone()
        assert saved is not None
        return self._operator_plan_action_from_row(saved)

    def list_operator_plan_actions(self) -> tuple[OperatorPlanAction, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT {self._operator_plan_action_columns()} "
                "FROM operator_plan_actions ORDER BY id"
            ).fetchall()
        return tuple(self._operator_plan_action_from_row(row) for row in rows)

    def list_publication_plan_backup_candidates(
        self,
        cutoff: datetime,
        *,
        limit: int = 100,
    ) -> tuple[PublicationPlanBackupCandidate, ...]:
        if cutoff.tzinfo is None or limit <= 0:
            raise ValueError("retention cutoff must be timezone-aware and limit positive")
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT p.id AS plan_id, p.plan_sha256, p.plan_json
                FROM auto_fix_publication_plans p
                LEFT JOIN publication_plan_backups b ON b.plan_id = p.id
                WHERE p.state IN ('published', 'abandoned')
                  AND p.updated_at <= ? AND p.plan_json IS NOT NULL
                  AND b.plan_id IS NULL
                ORDER BY p.id LIMIT ?
                """,
                (cutoff.isoformat(), limit),
            ).fetchall()
        return tuple(PublicationPlanBackupCandidate(**dict(row)) for row in rows)

    def record_publication_plan_backup(
        self,
        candidate: PublicationPlanBackupCandidate,
        backup_path: str,
    ) -> None:
        if not backup_path.strip():
            raise ValueError("Publication Plan backup path cannot be empty")
        if not Path(backup_path).is_absolute():
            raise ValueError("Publication Plan backup path must be absolute")
        if hashlib.sha256(candidate.plan_json.encode("utf-8")).hexdigest() != (
            candidate.plan_sha256
        ):
            raise ValueError("Publication Plan backup candidate digest is invalid")
        now = self._now().isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT plan_sha256, plan_json FROM auto_fix_publication_plans
                WHERE id = ? AND state IN ('published', 'abandoned')
                """,
                (candidate.plan_id,),
            ).fetchone()
            if (
                row is None
                or row["plan_sha256"] != candidate.plan_sha256
                or row["plan_json"] != candidate.plan_json
            ):
                raise ValueError("Publication Plan changed before backup acknowledgement")
            connection.execute(
                """
                INSERT INTO publication_plan_backups(
                    plan_id, plan_sha256, backup_path, backed_up_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(plan_id) DO NOTHING
                """,
                (
                    candidate.plan_id,
                    candidate.plan_sha256,
                    backup_path,
                    now,
                ),
            )
            saved = connection.execute(
                """
                SELECT plan_sha256, backup_path FROM publication_plan_backups
                WHERE plan_id = ?
                """,
                (candidate.plan_id,),
            ).fetchone()
            assert saved is not None
            if saved["plan_sha256"] != candidate.plan_sha256 or saved["backup_path"] != backup_path:
                raise ValueError("Publication Plan backup receipt is immutable")

    def purge_sensitive_payloads(
        self,
        cutoff: datetime,
    ) -> SensitivePayloadPurgeResult:
        """Purge expired content while preserving identities, states and audits."""

        if cutoff.tzinfo is None:
            raise ValueError("retention cutoff must be timezone-aware")
        cutoff_at = cutoff.isoformat()
        now = self._now().isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            plans = connection.execute(
                """
                UPDATE auto_fix_publication_plans
                SET plan_json = NULL, payload_purged_at = ?, last_error = NULL
                WHERE state IN ('published', 'abandoned')
                  AND updated_at <= ? AND plan_json IS NOT NULL
                  AND EXISTS (
                      SELECT 1 FROM publication_plan_backups b
                      WHERE b.plan_id = auto_fix_publication_plans.id
                        AND b.plan_sha256 = auto_fix_publication_plans.plan_sha256
                  )
                """,
                (now, cutoff_at),
            ).rowcount
            agent_tasks = connection.execute(
                """
                UPDATE agent_task_snapshots
                SET title = '', source_branch = '', target_branch = '',
                    skill_content = NULL,
                    payload_purged_at = ?
                WHERE created_at <= ? AND payload_purged_at IS NULL
                  AND EXISTS (
                      SELECT 1 FROM review_runs r
                      WHERE r.id = agent_task_snapshots.run_id
                        AND r.status NOT IN ('pending', 'running')
                  )
                """,
                (now, cutoff_at),
            ).rowcount
            notes = connection.execute(
                """
                UPDATE note_publication_requests
                SET body = marker, last_error = NULL, reconcile_next_at = NULL,
                    reconcile_lease_until = NULL, reconcile_claim_token = NULL,
                    payload_purged_at = ?
                WHERE state IN ('published', 'superseded', 'blocked', 'removed')
                  AND payload_retention_at IS NOT NULL
                  AND payload_retention_at <= ? AND payload_purged_at IS NULL
                  AND reconcile_claim_token IS NULL
                """,
                (now, cutoff_at),
            ).rowcount
            outbox = connection.execute(
                """
                UPDATE notification_outbox
                SET title = '', body = '', url = NULL, recipients_json = '[]',
                    last_error = NULL, payload_purged_at = ?
                WHERE status IN ('delivered', 'failed') AND terminal_at <= ?
                  AND payload_purged_at IS NULL AND claim_token IS NULL
                """,
                (now, cutoff_at),
            ).rowcount
            requests = connection.execute(
                """
                UPDATE notification_requests
                SET title = '', body = '', url = NULL, recipients_json = '[]',
                    payload_purged_at = ?
                WHERE payload_purged_at IS NULL
                  AND (
                      (NOT EXISTS (
                          SELECT 1 FROM notification_outbox o
                          WHERE o.correlation_id = notification_requests.correlation_id
                      ) AND created_at <= ?)
                      OR (
                          EXISTS (
                              SELECT 1 FROM notification_outbox o
                              WHERE o.correlation_id = notification_requests.correlation_id
                          )
                          AND NOT EXISTS (
                              SELECT 1 FROM notification_outbox o
                              WHERE o.correlation_id = notification_requests.correlation_id
                                AND (o.status NOT IN ('delivered', 'failed')
                                     OR o.terminal_at IS NULL OR o.terminal_at > ?)
                          )
                      )
                  )
                """,
                (now, cutoff_at, cutoff_at),
            ).rowcount
            diagnostics = 0
            for query, parameters in (
                (
                    (
                        "UPDATE review_runs SET error = NULL WHERE error IS NOT NULL "
                        "AND status NOT IN ('pending', 'running') "
                        "AND COALESCE(finished_at, created_at) <= ?"
                    ),
                    (cutoff_at,),
                ),
                (
                    (
                        "UPDATE review_run_failures SET error = NULL WHERE error IS NOT NULL "
                        "AND recorded_at <= ?"
                    ),
                    (cutoff_at,),
                ),
                (
                    (
                        "UPDATE effect_receipts SET detail = NULL, error = NULL "
                        "WHERE (detail IS NOT NULL OR error IS NOT NULL) AND updated_at <= ? "
                        "AND state NOT IN ('pending', 'running', 'planned', 'plan_frozen', "
                        "'needs_reconciliation', 'reconciling')"
                    ),
                    (cutoff_at,),
                ),
                (
                    (
                        "UPDATE auto_fix_publication_intents SET last_error = NULL "
                        "WHERE last_error IS NOT NULL AND updated_at <= ? "
                        "AND state IN ('published', 'abandoned', 'superseded')"
                    ),
                    (cutoff_at,),
                ),
                (
                    (
                        "UPDATE auto_fix_relations SET last_error = NULL "
                        "WHERE last_error IS NOT NULL AND updated_at <= ? "
                        "AND status IN ('closed', 'merged')"
                    ),
                    (cutoff_at,),
                ),
                (
                    (
                        "UPDATE worktree_cleanup SET last_error = NULL "
                        "WHERE last_error IS NOT NULL AND status IN ('cleaned', 'failed') "
                        "AND COALESCE(cleaned_at, created_at) <= ?"
                    ),
                    (cutoff_at,),
                ),
            ):
                diagnostics += connection.execute(query, parameters).rowcount
            connection.execute(
                """
                INSERT INTO sensitive_payload_purges(
                    cutoff_at, plan_payloads, agent_task_payloads, note_payloads,
                    notification_payloads, diagnostic_payloads, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    cutoff_at,
                    plans,
                    agent_tasks,
                    notes,
                    outbox + requests,
                    diagnostics,
                    now,
                ),
            )
            row = connection.execute(
                """
                SELECT id AS purge_id, cutoff_at, plan_payloads, agent_task_payloads,
                       note_payloads, notification_payloads, diagnostic_payloads, created_at
                FROM sensitive_payload_purges WHERE id = last_insert_rowid()
                """
            ).fetchone()
        assert row is not None
        return SensitivePayloadPurgeResult(**dict(row))

    def list_sensitive_payload_purges(self) -> tuple[SensitivePayloadPurgeResult, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id AS purge_id, cutoff_at, plan_payloads, agent_task_payloads,
                       note_payloads, notification_payloads, diagnostic_payloads, created_at
                FROM sensitive_payload_purges ORDER BY id
                """
            ).fetchall()
        return tuple(SensitivePayloadPurgeResult(**dict(row)) for row in rows)

    def is_suppressed_parent_sha(self, project_id: str, mr_iid: int, sha: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM auto_fix_relations
                WHERE parent_project_id = ? AND parent_mr_iid = ?
                  AND status = 'merged' AND merged_sha = ?
                LIMIT 1
                """,
                (project_id, mr_iid, sha),
            ).fetchone()
        return row is not None

    def enqueue_notifications(
        self,
        correlation_id: str,
        title: str,
        body: str,
        url: str | None,
        recipients: tuple[str, ...],
        channels: tuple[tuple[str, str], ...],
    ) -> tuple[NotificationDelivery, ...]:
        if not correlation_id:
            raise ValueError("notification correlation_id cannot be empty")
        if len({channel_id for channel_id, _channel in channels}) != len(channels):
            raise ValueError("notification channel IDs must be unique")
        created_at = self._now().isoformat()
        recipients_json = json.dumps(list(recipients), separators=(",", ":"))
        with self._connect() as connection:
            for channel_id, channel in channels:
                connection.execute(
                    """
                    INSERT INTO notification_outbox(
                        correlation_id, channel_id, channel, title, body, url,
                        recipients_json, next_attempt_at, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(correlation_id, channel_id) DO NOTHING
                    """,
                    (
                        correlation_id,
                        channel_id,
                        channel,
                        title,
                        body,
                        url,
                        recipients_json,
                        created_at,
                        created_at,
                    ),
                )
            rows = connection.execute(
                """
                SELECT id AS delivery_id, correlation_id, channel_id, channel,
                       title, body, url, recipients_json, status, attempts,
                       next_attempt_at, lease_until, claim_token, created_at,
                       delivered_at, terminal_at, last_error
                FROM notification_outbox
                WHERE correlation_id = ?
                ORDER BY id
                """,
                (correlation_id,),
            ).fetchall()
        return tuple(self._notification_from_row(row) for row in rows)

    def get_notification_delivery(self, delivery_id: int) -> NotificationDelivery | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT id AS delivery_id, correlation_id, channel_id, channel,
                       title, body, url, recipients_json, status, attempts,
                       next_attempt_at, lease_until, claim_token, created_at,
                       delivered_at, terminal_at, last_error
                FROM notification_outbox WHERE id = ?
                """,
                (delivery_id,),
            ).fetchone()
        return self._notification_from_row(row) if row is not None else None

    @staticmethod
    def _notification_from_row(row: sqlite3.Row) -> NotificationDelivery:
        values = dict(row)
        values["recipients"] = tuple(json.loads(values.pop("recipients_json")))
        return NotificationDelivery(**values)

    def _claim_notification_row(
        self,
        connection: sqlite3.Connection,
        delivery_id: int | None,
        now_text: str,
        lease_until: str,
        claim_token: str,
        excluded_delivery_ids: tuple[int, ...] = (),
        excluded_channel_ids: tuple[str, ...] = (),
        allowed_channel_ids: tuple[str, ...] | None = None,
    ) -> sqlite3.Row | None:
        if allowed_channel_ids == ():
            return None
        if delivery_id is None:
            exclusion = ""
            parameters: tuple[object, ...] = (now_text,)
            if allowed_channel_ids is not None:
                placeholders = ", ".join("?" for _ in allowed_channel_ids)
                exclusion = f" AND channel_id IN ({placeholders})"
                parameters += allowed_channel_ids
            if excluded_delivery_ids:
                placeholders = ", ".join("?" for _ in excluded_delivery_ids)
                exclusion += f" AND id NOT IN ({placeholders})"
                parameters += excluded_delivery_ids
            if excluded_channel_ids:
                placeholders = ", ".join("?" for _ in excluded_channel_ids)
                exclusion += f" AND channel_id NOT IN ({placeholders})"
                parameters += excluded_channel_ids
            row = connection.execute(
                f"""
                SELECT id AS delivery_id, correlation_id, channel_id, channel,
                       title, body, url, recipients_json, status, attempts,
                       next_attempt_at, lease_until, claim_token, created_at,
                       delivered_at, terminal_at, last_error
                FROM notification_outbox
                WHERE status = 'pending' AND next_attempt_at <= ?
                  {exclusion}
                ORDER BY id LIMIT 1
                """,
                parameters,
            ).fetchone()
        else:
            channel_scope = ""
            parameters = (delivery_id, now_text)
            if allowed_channel_ids is not None:
                placeholders = ", ".join("?" for _ in allowed_channel_ids)
                channel_scope = f" AND channel_id IN ({placeholders})"
                parameters += allowed_channel_ids
            row = connection.execute(
                f"""
                SELECT id AS delivery_id, correlation_id, channel_id, channel,
                       title, body, url, recipients_json, status, attempts,
                       next_attempt_at, lease_until, claim_token, created_at,
                       delivered_at, terminal_at, last_error
                FROM notification_outbox
                WHERE id = ? AND status = 'pending' AND next_attempt_at <= ?
                  {channel_scope}
                """,
                parameters,
            ).fetchone()
        if row is None:
            return None
        connection.execute(
            """
            UPDATE notification_outbox
            SET status = 'running', attempts = attempts + 1,
                lease_until = ?, claim_token = ?
            WHERE id = ? AND status = 'pending'
            """,
            (lease_until, claim_token, row["delivery_id"]),
        )
        return connection.execute(
            """
            SELECT id AS delivery_id, correlation_id, channel_id, channel,
                   title, body, url, recipients_json, status, attempts,
                   next_attempt_at, lease_until, claim_token, created_at,
                   delivered_at, terminal_at, last_error
            FROM notification_outbox WHERE id = ?
            """,
            (row["delivery_id"],),
        ).fetchone()

    def _claim_notification(
        self,
        delivery_id: int | None,
        lease_seconds: int,
        max_attempts: int,
        excluded_delivery_ids: tuple[int, ...] = (),
        excluded_channel_ids: tuple[str, ...] = (),
        allowed_channel_ids: tuple[str, ...] | None = None,
    ) -> NotificationDelivery | None:
        if lease_seconds <= 0:
            raise ValueError("notification lease_seconds must be positive")
        if max_attempts <= 0:
            raise ValueError("notification max_attempts must be positive")
        now = self._now()
        now_text = now.isoformat()
        lease_until = (now + timedelta(seconds=lease_seconds)).isoformat()
        claim_token = uuid4().hex
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE notification_outbox
                SET status = CASE WHEN attempts >= ? THEN 'failed' ELSE 'pending' END,
                    next_attempt_at = CASE WHEN attempts >= ? THEN NULL ELSE ? END,
                    lease_until = NULL, claim_token = NULL,
                    terminal_at = CASE WHEN attempts >= ?
                        THEN COALESCE(terminal_at, ?) ELSE NULL END,
                    last_error = CASE WHEN attempts >= ?
                        THEN 'notification delivery lease expired'
                        ELSE 'notification delivery lease expired; retrying' END
                WHERE status = 'running'
                  AND (lease_until IS NULL OR lease_until <= ?)
                """,
                (max_attempts, max_attempts, now_text, max_attempts, now_text,
                 max_attempts, now_text),
            )
            claimed = self._claim_notification_row(
                connection,
                delivery_id,
                now_text,
                lease_until,
                claim_token,
                excluded_delivery_ids,
                excluded_channel_ids,
                allowed_channel_ids,
            )
        return self._notification_from_row(claimed) if claimed is not None else None

    def claim_next_notification(
        self,
        lease_seconds: int = DEFAULT_NOTIFICATION_LEASE_SECONDS,
        max_attempts: int = DEFAULT_NOTIFICATION_MAX_ATTEMPTS,
        excluded_delivery_ids: tuple[int, ...] = (),
        excluded_channel_ids: tuple[str, ...] = (),
        allowed_channel_ids: tuple[str, ...] | None = None,
    ) -> NotificationDelivery | None:
        return self._claim_notification(
            None,
            lease_seconds,
            max_attempts,
            excluded_delivery_ids,
            excluded_channel_ids,
            allowed_channel_ids,
        )

    def claim_notification(
        self,
        delivery_id: int,
        lease_seconds: int = DEFAULT_NOTIFICATION_LEASE_SECONDS,
        max_attempts: int = DEFAULT_NOTIFICATION_MAX_ATTEMPTS,
        allowed_channel_ids: tuple[str, ...] | None = None,
    ) -> NotificationDelivery | None:
        return self._claim_notification(
            delivery_id,
            lease_seconds,
            max_attempts,
            allowed_channel_ids=allowed_channel_ids,
        )

    def finish_notification(
        self,
        delivery_id: int,
        delivered: bool,
        detail: str | None = None,
        claim_token: str | None = None,
        max_attempts: int = DEFAULT_NOTIFICATION_MAX_ATTEMPTS,
        retry_delay_seconds: float = 1.0,
    ) -> bool:
        if max_attempts <= 0:
            raise ValueError("notification max_attempts must be positive")
        if retry_delay_seconds < 0:
            raise ValueError("notification retry_delay_seconds cannot be negative")
        now = self._now()
        now_text = now.isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            query = "SELECT attempts FROM notification_outbox WHERE id = ? AND status = 'running'"
            parameters: tuple[object, ...] = (delivery_id,)
            if claim_token is not None:
                query += " AND claim_token = ? AND lease_until IS NOT NULL AND lease_until > ?"
                parameters += (claim_token, now_text)
            row = connection.execute(query, parameters).fetchone()
            if row is None:
                return False
            attempts = int(row["attempts"])
            if delivered:
                status = "delivered"
                next_attempt_at = None
                delivered_at = now_text
                last_error = None
            elif attempts >= max_attempts:
                status = "failed"
                next_attempt_at = None
                delivered_at = None
                last_error = self._redact(detail) if detail else "notification delivery failed"
            else:
                status = "pending"
                delay = retry_delay_seconds * (2 ** max(attempts - 1, 0))
                next_attempt_at = (now + timedelta(seconds=delay)).isoformat()
                delivered_at = None
                last_error = (
                    self._redact(detail) if detail else "notification delivery failed; retrying"
                )
            query = (
                "UPDATE notification_outbox SET status = ?, next_attempt_at = ?, "
                "lease_until = NULL, claim_token = NULL, delivered_at = ?, "
                "terminal_at = CASE WHEN ? IN ('delivered', 'failed') "
                "THEN COALESCE(terminal_at, ?) ELSE NULL END, last_error = ? "
                "WHERE id = ? AND status = 'running'"
            )
            parameters = (
                status, next_attempt_at, delivered_at, status, now_text, last_error, delivery_id
            )
            if claim_token is not None:
                query += " AND claim_token = ? AND lease_until IS NOT NULL AND lease_until > ?"
                parameters += (claim_token, now_text)
            cursor = connection.execute(query, parameters)
        return cursor.rowcount == 1

    @staticmethod
    def _worktree_from_row(row: sqlite3.Row) -> WorktreeRecord:
        return WorktreeRecord(**dict(row))

    def register_worktree(self, run_id: int, path: Path, sha: str) -> WorktreeRecord:
        created_at = self._now().isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO worktree_cleanup(run_id, path, sha, created_at, next_attempt_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(run_id, path) DO NOTHING
                """,
                (run_id, str(path), sha, created_at, created_at),
            )
            row = connection.execute(
                """
                SELECT id AS cleanup_id, run_id, path, sha, status, attempts,
                       next_attempt_at, lease_until, claim_token, created_at,
                       cleaned_at, last_error
                FROM worktree_cleanup WHERE run_id = ? AND path = ?
                """,
                (run_id, str(path)),
            ).fetchone()
        assert row is not None
        return self._worktree_from_row(row)

    def mark_worktree_cleanup_pending(self, cleanup_id: int, detail: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE worktree_cleanup
                SET status = 'pending_cleanup', last_error = ?,
                    next_attempt_at = ?, lease_until = NULL, claim_token = NULL
                WHERE id = ? AND status = 'active'
                """,
                (self._redact(detail), self._now().isoformat(), cleanup_id),
            )
        return cursor.rowcount == 1

    def mark_worktree_not_created(self, cleanup_id: int, detail: str) -> bool:
        """Retain a failed reservation without authorizing deletion of its path."""

        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE worktree_cleanup
                SET status = 'skipped', cleaned_at = ?, last_error = ?,
                    next_attempt_at = NULL, lease_until = NULL, claim_token = NULL
                WHERE id = ? AND status = 'active'
                """,
                (self._now().isoformat(), self._redact(detail), cleanup_id),
            )
        return cursor.rowcount == 1

    def mark_worktree_cleaned(self, cleanup_id: int, claim_token: str | None = None) -> bool:
        now_text = self._now().isoformat()
        query = (
            "UPDATE worktree_cleanup SET status = 'cleaned', cleaned_at = ?, "
            "next_attempt_at = NULL, lease_until = NULL, claim_token = NULL "
            "WHERE id = ? AND status = 'active'"
        )
        parameters: tuple[object, ...] = (now_text, cleanup_id)
        if claim_token is not None:
            query = (
                "UPDATE worktree_cleanup SET status = 'cleaned', cleaned_at = ?, "
                "next_attempt_at = NULL, lease_until = NULL, claim_token = NULL "
                "WHERE id = ? AND status = 'running' AND claim_token = ?"
                " AND lease_until IS NOT NULL AND lease_until > ?"
            )
            parameters = (
                now_text,
                cleanup_id,
                claim_token,
                now_text,
            )
        with self._connect() as connection:
            cursor = connection.execute(query, parameters)
        return cursor.rowcount == 1

    def claim_next_worktree_cleanup(
        self,
        lease_seconds: int = DEFAULT_WORKTREE_CLEANUP_LEASE_SECONDS,
        max_attempts: int = DEFAULT_WORKTREE_CLEANUP_MAX_ATTEMPTS,
    ) -> WorktreeRecord | None:
        if lease_seconds <= 0:
            raise ValueError("worktree cleanup lease_seconds must be positive")
        if max_attempts <= 0:
            raise ValueError("worktree cleanup max_attempts must be positive")
        now = self._now()
        now_text = now.isoformat()
        lease_until = (now + timedelta(seconds=lease_seconds)).isoformat()
        claim_token = uuid4().hex
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE worktree_cleanup
                SET status = CASE WHEN attempts >= ? THEN 'failed' ELSE 'pending_cleanup' END,
                    next_attempt_at = CASE WHEN attempts >= ? THEN NULL ELSE ? END,
                    lease_until = NULL, claim_token = NULL,
                    last_error = CASE WHEN attempts >= ?
                        THEN 'worktree cleanup lease expired'
                        ELSE 'worktree cleanup lease expired; retrying' END
                WHERE status = 'running'
                  AND (lease_until IS NULL OR lease_until <= ?)
                """,
                (max_attempts, max_attempts, now_text, max_attempts, now_text),
            )
            row = connection.execute(
                """
                SELECT wc.id AS cleanup_id, wc.run_id, wc.path, wc.sha, wc.status,
                       wc.attempts, wc.next_attempt_at, wc.lease_until, wc.claim_token,
                       wc.created_at, wc.cleaned_at, wc.last_error
                FROM worktree_cleanup wc
                JOIN review_runs rr ON rr.id = wc.run_id
                WHERE wc.status IN ('active', 'pending_cleanup')
                  AND wc.attempts < ?
                  AND wc.next_attempt_at <= ?
                  AND (
                    rr.status IN ('succeeded', 'failed', 'cancelled')
                    OR (rr.status = 'running' AND (rr.lease_until IS NULL OR rr.lease_until <= ?))
                  )
                ORDER BY wc.id LIMIT 1
                """,
                (max_attempts, now_text, now_text),
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                """
                UPDATE worktree_cleanup
                SET status = 'running', attempts = attempts + 1,
                    lease_until = ?, claim_token = ?
                WHERE id = ? AND status IN ('active', 'pending_cleanup')
                """,
                (lease_until, claim_token, row["cleanup_id"]),
            )
            claimed = connection.execute(
                """
                SELECT id AS cleanup_id, run_id, path, sha, status, attempts,
                       next_attempt_at, lease_until, claim_token, created_at,
                       cleaned_at, last_error
                FROM worktree_cleanup WHERE id = ?
                """,
                (row["cleanup_id"],),
            ).fetchone()
        assert claimed is not None
        return self._worktree_from_row(claimed)

    def finish_worktree_cleanup(
        self,
        cleanup_id: int,
        cleaned: bool,
        detail: str | None = None,
        claim_token: str | None = None,
        max_attempts: int = DEFAULT_WORKTREE_CLEANUP_MAX_ATTEMPTS,
        retry_delay_seconds: float = 5.0,
    ) -> bool:
        if max_attempts <= 0:
            raise ValueError("worktree cleanup max_attempts must be positive")
        if retry_delay_seconds < 0:
            raise ValueError("worktree cleanup retry_delay_seconds cannot be negative")
        now_text = self._now().isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            query = "SELECT attempts FROM worktree_cleanup WHERE id = ? AND status = 'running'"
            parameters: tuple[object, ...] = (cleanup_id,)
            if claim_token is not None:
                query += " AND claim_token = ? AND lease_until IS NOT NULL AND lease_until > ?"
                parameters += (claim_token, now_text)
            row = connection.execute(query, parameters).fetchone()
            if row is None:
                return False
            attempts = int(row["attempts"])
            if cleaned:
                status = "cleaned"
                next_attempt_at = None
                cleaned_at = self._now().isoformat()
                last_error = None
            elif attempts >= max_attempts:
                status = "failed"
                next_attempt_at = None
                cleaned_at = None
                last_error = self._redact(detail) if detail else "worktree cleanup failed"
            else:
                status = "pending_cleanup"
                next_attempt_at = (
                    self._now() + timedelta(seconds=retry_delay_seconds * (2 ** (attempts - 1)))
                ).isoformat()
                cleaned_at = None
                last_error = self._redact(detail) if detail else "worktree cleanup failed; retrying"
            query = (
                "UPDATE worktree_cleanup SET status = ?, next_attempt_at = ?, "
                "lease_until = NULL, claim_token = NULL, cleaned_at = ?, last_error = ? "
                "WHERE id = ? AND status = 'running'"
            )
            parameters = (status, next_attempt_at, cleaned_at, last_error, cleanup_id)
            if claim_token is not None:
                query += " AND claim_token = ? AND lease_until IS NOT NULL AND lease_until > ?"
                parameters += (claim_token, now_text)
            cursor = connection.execute(query, parameters)
        return cursor.rowcount == 1

    def get_worktree_record(self, cleanup_id: int) -> WorktreeRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT id AS cleanup_id, run_id, path, sha, status, attempts,
                       next_attempt_at, lease_until, claim_token, created_at,
                       cleaned_at, last_error
                FROM worktree_cleanup WHERE id = ?
                """,
                (cleanup_id,),
            ).fetchone()
        return self._worktree_from_row(row) if row is not None else None
