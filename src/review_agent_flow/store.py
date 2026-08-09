"""SQLite persistence for idempotent Review Runs and their side effects."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

from .gitlab import canonical_note_marker, has_exact_note_marker, is_valid_sha
from .security import redact

DEFAULT_LEASE_SECONDS = 1860
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_NOTIFICATION_LEASE_SECONDS = 30
DEFAULT_NOTIFICATION_MAX_ATTEMPTS = 3
DEFAULT_WORKTREE_CLEANUP_LEASE_SECONDS = 60
DEFAULT_WORKTREE_CLEANUP_MAX_ATTEMPTS = 5
SCHEMA_VERSION = 4
DEFAULT_EFFECT_LEASE_SECONDS = 60
DEFAULT_NOTE_PUBLICATION_LEASE_SECONDS = 60
DEFAULT_NOTE_PUBLICATION_MAX_ATTEMPTS = 5
DEFAULT_NOTE_UNCERTAIN_LOOKUP_ATTEMPTS = 3


def auto_fix_intent_marker(run_id: int, parent_sha: str) -> str:
    """Return the stable identity marker required in an Auto-fix MR."""

    return f"ReviewFlow-Auto-Fix-Run-ID:{run_id} SHA:{parent_sha}"


@dataclass(frozen=True)
class ReviewRun:
    run_id: int
    project_id: str
    mr_iid: int
    sha: str
    status: str
    attempts: int = 0
    lease_until: str | None = None
    claim_token: str | None = None
    failure_class: str | None = None
    retryable: bool = False
    error: str | None = None
    next_attempt_at: str | None = None
    created_at: str | None = None
    finished_at: str | None = None


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
class AutoFixIntent:
    intent_id: int
    parent_project_id: str
    parent_mr_iid: int
    parent_sha: str
    run_id: int
    source_branch: str
    target_branch: str
    title: str
    status: str
    auto_fix_project_id: str | None
    auto_fix_mr_iid: int | None


@dataclass(frozen=True)
class AutoFixRelation:
    relation_id: int
    parent_project_id: str
    parent_mr_iid: int
    parent_sha: str
    run_id: int
    auto_fix_project_id: str
    auto_fix_mr_iid: int
    merged_sha: str | None
    status: str


@dataclass(frozen=True)
class AutoFixMatch:
    parent_project_id: str
    parent_mr_iid: int
    parent_sha: str
    run_id: int
    auto_fix_project_id: str
    auto_fix_mr_iid: int
    status: str


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

    Version 4 is intentionally a fresh schema.  There is no production database
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
                    return {"status": "not_ready", "diagnostic": "sqlite probe read returned no row"}
        except (OSError, sqlite3.Error) as exc:
            return {"status": "not_ready", "diagnostic": self._redact(exc, max_length=1000)}
        return {"status": "ok", "diagnostic": None}

    def _validate_schema_version(self) -> None:
        with self._connect() as connection:
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version != SCHEMA_VERSION:
            raise RuntimeError(
                f"unsupported SQLite schema version {version}; recreate {self.path}"
            )

    def _initialize(self) -> None:
        with self._connect() as connection:
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            existing_tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
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
                    claim_token TEXT,
                    failure_class TEXT,
                    retryable INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(project_id, mr_iid, sha)
                );
                CREATE TABLE IF NOT EXISTS auto_fix_intents (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    parent_project_id TEXT NOT NULL,
                    parent_mr_iid INTEGER NOT NULL,
                    parent_sha TEXT NOT NULL,
                    run_id INTEGER NOT NULL REFERENCES review_runs(id),
                    source_branch TEXT NOT NULL,
                    target_branch TEXT NOT NULL,
                    title TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'planned',
                    auto_fix_project_id TEXT,
                    auto_fix_mr_iid INTEGER,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(run_id)
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
                    UNIQUE(project_id, mr_iid, sha)
                );
                CREATE TABLE IF NOT EXISTS auto_fix_mrs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    parent_project_id TEXT NOT NULL,
                    parent_mr_iid INTEGER NOT NULL,
                    parent_sha TEXT NOT NULL,
                    run_id INTEGER NOT NULL REFERENCES review_runs(id),
                    auto_fix_project_id TEXT NOT NULL,
                    auto_fix_mr_iid INTEGER NOT NULL,
                    merged_sha TEXT,
                    status TEXT NOT NULL DEFAULT 'opened',
                    UNIQUE(auto_fix_project_id, auto_fix_mr_iid)
                );
                CREATE TABLE IF NOT EXISTS auto_fix_merge_events (
                    auto_fix_project_id TEXT NOT NULL,
                    auto_fix_mr_iid INTEGER NOT NULL,
                    merged_sha TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(auto_fix_project_id, auto_fix_mr_iid)
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
                    last_error TEXT,
                    UNIQUE(correlation_id, channel_id)
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
                PRAGMA user_version = 4;
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

    def enqueue(self, project_id: str, mr_iid: int, sha: str) -> ReviewRun:
        if not project_id or not str(project_id).strip():
            raise ValueError("project_id cannot be empty")
        if not is_valid_sha(sha):
            raise ValueError("invalid SHA: expected 40 or 64 hexadecimal characters")
        created_at = datetime.now(UTC).isoformat()
        with self._connect() as connection:
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

    def wake_run(self, project_id: str, mr_iid: int, sha: str) -> ReviewRun | None:
        """Make a deferred Review Run immediately eligible after a green Pipeline."""

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

    def get_project_bound_review_run_status(
        self, project_id: str, run_id: int
    ) -> dict[str, object] | None:
        """Return a read-only, credential-free Review Run status snapshot.

        The project comparison happens before any failure or receipt details
        are returned.  The response is deliberately assembled from a
        whitelist rather than serializing ``ReviewRun`` or ``EffectReceipt``
        directly, because those internal records contain lease claim tokens.
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
                "error": self._redact(failure.error, max_length=2000)
                if failure.error
                else None,
                "recorded_at": failure.recorded_at,
            }
            for failure in failures[-20:]
        ]
        latest_failure = failure_items[-1] if failure_items else None
        receipt_items = [
            {
                "receipt_id": receipt.receipt_id,
                "effect_kind": receipt.effect_kind,
                "attempt": receipt.attempt,
                "state": receipt.state,
                "external_identity": self._redact(receipt.external_identity, max_length=1000)
                if receipt.external_identity
                else None,
                "detail": self._redact(receipt.detail, max_length=1000)
                if receipt.detail
                else None,
                "error": self._redact(receipt.error, max_length=2000)
                if receipt.error
                else None,
                "updated_at": receipt.updated_at,
            }
            for receipt in self.list_effect_receipts(run_id)[-50:]
        ]
        return {
            "run_id": run.run_id,
            "project_id": run.project_id,
            "mr_iid": run.mr_iid,
            "sha": run.sha,
            "status": run.status,
            "attempt": run.attempts,
            "attempts": run.attempts,
            "failure": latest_failure,
            "failures": failure_items,
            "effect_receipts": receipt_items,
            "diagnostic": {
                "error": self._redact(run.error, max_length=2000) if run.error else None,
                "failure_class": run.failure_class,
                "retryable": run.retryable,
                "next_attempt_at": run.next_attempt_at,
            },
            "created_at": run.created_at,
            "finished_at": run.finished_at,
        }

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
                "SELECT id, attempts FROM review_runs "
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
                        WHEN COALESCE(attempts, 0) >= ? THEN 'failed'
                        ELSE 'pending'
                    END,
                    finished_at = CASE
                        WHEN COALESCE(attempts, 0) >= ? THEN ?
                        ELSE NULL
                    END,
                    error = CASE
                        WHEN COALESCE(attempts, 0) >= ? THEN 'worker lease expired'
                        ELSE 'worker lease expired; retrying'
                    END,
                    failure_class = 'lease_expired',
                    retryable = 1,
                    next_attempt_at = CASE
                        WHEN COALESCE(attempts, 0) >= ? THEN NULL
                        ELSE ?
                    END,
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
                exhausted = int(expired["attempts"] or 0) >= max_attempts
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
        """Record a retryable failure and requeue it until the attempt cap."""

        if not failure_class:
            raise ValueError("failure_class cannot be empty")
        if delay_seconds < 0:
            raise ValueError("delay_seconds cannot be negative")
        if max_attempts <= 0:
            raise ValueError("max_attempts must be positive")
        now = datetime.now(UTC)
        safe_reason = self._redact(reason)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            query = "SELECT attempts FROM review_runs WHERE id = ? AND status = 'running'"
            parameters: tuple[object, ...] = (run_id,)
            if claim_token is not None:
                query += " AND claim_token = ?"
                parameters += (claim_token,)
            row = connection.execute(query, parameters).fetchone()
            if row is None:
                return False
            attempts = int(row["attempts"] or 0)
            exhausted = attempts >= max_attempts
            status = "failed" if exhausted else "pending"
            finished_at = now.isoformat() if exhausted else None
            next_attempt_at = None if exhausted else (
                now + timedelta(seconds=delay_seconds)
            ).isoformat()
            update_query = (
                "UPDATE review_runs SET status = ?, finished_at = ?, error = ?, "
                "failure_class = ?, retryable = 1, next_attempt_at = ?, "
                "lease_until = NULL, claim_token = NULL WHERE id = ? AND status = 'running'"
            )
            update_parameters: tuple[object, ...] = (
                status,
                finished_at,
                safe_reason,
                failure_class,
                next_attempt_at,
                run_id,
            )
            if claim_token is not None:
                update_query += " AND claim_token = ?"
                update_parameters += (claim_token,)
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
                    (safe_reason, now.isoformat(), run_id),
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
            initial_state = (
                "pending" if has_exact_note_marker(body, marker) else "blocked"
            )
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
            if (
                request.receipt_id != receipt_id
                or request.marker != marker
                or request.body != body
            ):
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

    def get_note_publication_request(
        self, request_id: int
    ) -> NotePublicationRequest | None:
        with self._connect() as connection:
            row = connection.execute(
                f"SELECT {self._note_publication_columns()} "
                "FROM note_publication_requests WHERE id = ?",
                (request_id,),
            ).fetchone()
        return self._note_publication_from_row(row) if row is not None else None

    def get_note_publication_for_run(
        self, run_id: int
    ) -> NotePublicationRequest | None:
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

    def mark_note_write_uncertain(
        self, request_id: int, claim_token: str | None
    ) -> bool:
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
        if state == "published" and (
            type(external_note_id) is not int or external_note_id <= 0
        ):
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
                    error,
                    reconcile_next_at,
                    request_id,
                ),
            )
            identity = (
                f"gitlab:{row['project_id']}!{row['mr_iid']}#note-{row['external_note_id']}"
            )
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
        if effect_kind == "autofix_reconciliation":
            run_status_scope = (
                " AND EXISTS ("
                "SELECT 1 FROM review_runs "
                "WHERE review_runs.id = effect_receipts.run_id "
                "AND review_runs.status IN ('succeeded', 'failed', 'cancelled'))"
            )
            joined_run_status_scope = " AND review_runs.status IN ('succeeded', 'failed', 'cancelled')"
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
            attempt_scope = (
                " AND effect_receipts.attempt < ?" if max_attempts is not None else ""
            )
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
                claim_attempt_scope = (
                    " AND attempt < ?" if max_attempts is not None else ""
                )
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
        if not is_valid_sha(sha):
            raise ValueError("invalid SHA: expected 40 or 64 hexadecimal characters")
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
                    " AND claim_token = ?"
                    " AND lease_until IS NOT NULL AND lease_until > ?"
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

    def create_auto_fix_intent(
        self,
        parent_project_id: str,
        parent_mr_iid: int,
        parent_sha: str,
        run_id: int,
        source_branch: str,
        target_branch: str,
        title: str,
    ) -> AutoFixIntent:
        now = self._now().isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO auto_fix_intents(
                    parent_project_id, parent_mr_iid, parent_sha, run_id,
                    source_branch, target_branch, title, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    parent_project_id = excluded.parent_project_id,
                    parent_mr_iid = excluded.parent_mr_iid,
                    parent_sha = excluded.parent_sha,
                    source_branch = excluded.source_branch,
                    target_branch = excluded.target_branch,
                    title = excluded.title,
                    updated_at = excluded.updated_at
                """,
                (
                    parent_project_id,
                    parent_mr_iid,
                    parent_sha,
                    run_id,
                    source_branch,
                    target_branch,
                    title,
                    now,
                    now,
                ),
            )
            row = connection.execute(
                """
                SELECT id AS intent_id, parent_project_id, parent_mr_iid, parent_sha,
                       run_id, source_branch, target_branch, title, status,
                       auto_fix_project_id, auto_fix_mr_iid
                FROM auto_fix_intents WHERE run_id = ?
                """,
                (run_id,),
            ).fetchone()
        assert row is not None
        return AutoFixIntent(**dict(row))

    def get_auto_fix_intent(self, run_id: int) -> AutoFixIntent | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT id AS intent_id, parent_project_id, parent_mr_iid, parent_sha,
                       run_id, source_branch, target_branch, title, status,
                       auto_fix_project_id, auto_fix_mr_iid
                FROM auto_fix_intents WHERE run_id = ?
                """,
                (run_id,),
            ).fetchone()
        return AutoFixIntent(**dict(row)) if row is not None else None

    def abandon_auto_fix_intent(self, run_id: int) -> bool:
        """Close an intent after a terminal Agent run produced no Auto-fix MR."""

        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE auto_fix_intents
                SET status = 'abandoned', updated_at = ?
                WHERE run_id = ? AND status IN ('planned', 'created')
                  AND auto_fix_mr_iid IS NULL
                  AND EXISTS (
                      SELECT 1 FROM review_runs
                      WHERE review_runs.id = auto_fix_intents.run_id
                        AND review_runs.status IN ('succeeded', 'failed', 'cancelled')
                  )
                """,
                (self._now().isoformat(), run_id),
            )
        return cursor.rowcount == 1

    @staticmethod
    def _auto_fix_match_from_row(row: sqlite3.Row) -> AutoFixMatch:
        return AutoFixMatch(
            parent_project_id=str(row["parent_project_id"]),
            parent_mr_iid=int(row["parent_mr_iid"]),
            parent_sha=str(row["parent_sha"]),
            run_id=int(row["run_id"]),
            auto_fix_project_id=str(row["auto_fix_project_id"]),
            auto_fix_mr_iid=int(row["auto_fix_mr_iid"]),
            status=str(row["status"]),
        )

    @staticmethod
    def _select_relation(
        connection: sqlite3.Connection, project_id: str, mr_iid: int
    ) -> sqlite3.Row | None:
        return connection.execute(
            """
            SELECT parent_project_id, parent_mr_iid, parent_sha, run_id,
                   auto_fix_project_id, auto_fix_mr_iid, status
            FROM auto_fix_mrs
            WHERE auto_fix_project_id = ? AND auto_fix_mr_iid = ?
            """,
            (project_id, mr_iid),
        ).fetchone()

    def _attach_intent(
        self,
        connection: sqlite3.Connection,
        intent: sqlite3.Row,
        auto_fix_project_id: str,
        auto_fix_mr_iid: int,
    ) -> sqlite3.Row:
        connection.execute(
            """
            INSERT INTO auto_fix_mrs(
                parent_project_id, parent_mr_iid, parent_sha, run_id,
                auto_fix_project_id, auto_fix_mr_iid
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(auto_fix_project_id, auto_fix_mr_iid) DO UPDATE SET
                parent_project_id = excluded.parent_project_id,
                parent_mr_iid = excluded.parent_mr_iid,
                parent_sha = excluded.parent_sha,
                run_id = excluded.run_id
            """,
            (
                intent["parent_project_id"],
                intent["parent_mr_iid"],
                intent["parent_sha"],
                intent["run_id"],
                auto_fix_project_id,
                auto_fix_mr_iid,
            ),
        )
        connection.execute(
            """
            UPDATE auto_fix_intents
            SET status = 'created', auto_fix_project_id = ?, auto_fix_mr_iid = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (auto_fix_project_id, auto_fix_mr_iid, self._now().isoformat(), intent["id"]),
        )
        pending = connection.execute(
            """
            SELECT merged_sha FROM auto_fix_merge_events
            WHERE auto_fix_project_id = ? AND auto_fix_mr_iid = ?
            """,
            (auto_fix_project_id, auto_fix_mr_iid),
        ).fetchone()
        if pending is not None:
            connection.execute(
                """
                UPDATE auto_fix_mrs SET status = 'merged', merged_sha = ?
                WHERE auto_fix_project_id = ? AND auto_fix_mr_iid = ?
                """,
                (pending["merged_sha"], auto_fix_project_id, auto_fix_mr_iid),
            )
            connection.execute(
                """
                UPDATE auto_fix_intents SET status = 'merged', updated_at = ?
                WHERE id = ?
                """,
                (self._now().isoformat(), intent["id"]),
            )
            connection.execute(
                """
                DELETE FROM auto_fix_merge_events
                WHERE auto_fix_project_id = ? AND auto_fix_mr_iid = ?
                """,
                (auto_fix_project_id, auto_fix_mr_iid),
            )
        row = self._select_relation(connection, auto_fix_project_id, auto_fix_mr_iid)
        assert row is not None
        return row

    def resolve_auto_fix_match(
        self,
        project_id: str,
        mr_iid: int,
        source_branch: str | None = None,
        target_branch: str | None = None,
        description: str | None = None,
    ) -> AutoFixMatch | None:
        """Resolve only a server-recorded Auto-fix relation or intent.

        A branch-shaped MR may be attached to an intent only when its
        description carries the intent's run/SHA marker.  This prevents a
        fresh database whose numeric run IDs have reset from adopting an old
        GitLab MR that happens to reuse the same branch name.
        """

        with self._connect() as connection:
            relation = self._select_relation(connection, project_id, mr_iid)
            if relation is None and source_branch:
                intents = connection.execute(
                    """
                    SELECT * FROM auto_fix_intents
                    WHERE parent_project_id = ? AND source_branch = ?
                      AND status IN ('planned', 'created')
                      AND (auto_fix_mr_iid IS NULL OR auto_fix_mr_iid = ?)
                      AND (? IS NULL OR target_branch = ?)
                    ORDER BY id DESC
                    """,
                    (
                        project_id,
                        source_branch,
                        mr_iid,
                        target_branch,
                        target_branch,
                    ),
                ).fetchall()
                for intent in intents:
                    marker = auto_fix_intent_marker(
                        int(intent["run_id"]), str(intent["parent_sha"])
                    )
                    if description is None or marker not in description:
                        continue
                    relation = self._attach_intent(connection, intent, project_id, mr_iid)
                    break
            return self._auto_fix_match_from_row(relation) if relation is not None else None

    def record_auto_fix(
        self,
        parent_project_id: str,
        parent_mr_iid: int,
        parent_sha: str,
        run_id: int,
        auto_fix_project_id: str,
        auto_fix_mr_iid: int,
        status: str = "opened",
        merged_sha: str | None = None,
        run_claim_token: str | None = None,
        effect_receipt_id: int | None = None,
        effect_claim_token: str | None = None,
    ) -> AutoFixRelation | None:
        if not is_valid_sha(parent_sha):
            raise ValueError("invalid SHA: expected 40 or 64 hexadecimal characters")
        if status not in {"opened", "closed", "merged"}:
            raise ValueError(f"invalid Auto-fix status: {status}")
        if status != "merged":
            merged_sha = None
        elif merged_sha is not None and not is_valid_sha(merged_sha):
            raise ValueError("invalid SHA: expected 40 or 64 hexadecimal characters")
        if (effect_receipt_id is None) != (effect_claim_token is None):
            raise ValueError(
                "effect_receipt_id and effect_claim_token must be provided together"
            )
        with self._connect() as connection:
            if run_claim_token is not None or effect_receipt_id is not None:
                now_text = datetime.now(UTC).isoformat()
                connection.execute("BEGIN IMMEDIATE")
                if run_claim_token is not None:
                    active = connection.execute(
                        """
                        SELECT 1 FROM review_runs
                        WHERE id = ? AND status = 'running' AND claim_token = ?
                          AND lease_until IS NOT NULL AND lease_until > ?
                        """,
                        (run_id, run_claim_token, now_text),
                    ).fetchone()
                    if active is None:
                        return None
                if effect_receipt_id is not None and effect_claim_token is not None:
                    active_receipt = connection.execute(
                        """
                        SELECT 1 FROM effect_receipts
                        WHERE id = ? AND run_id = ? AND state = 'running'
                          AND claim_token = ? AND lease_until IS NOT NULL
                          AND lease_until > ?
                        """,
                        (effect_receipt_id, run_id, effect_claim_token, now_text),
                    ).fetchone()
                    if active_receipt is None:
                        return None
            connection.execute(
                """
                INSERT INTO auto_fix_mrs(
                    parent_project_id, parent_mr_iid, parent_sha, run_id,
                    auto_fix_project_id, auto_fix_mr_iid, status, merged_sha
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(auto_fix_project_id, auto_fix_mr_iid) DO UPDATE SET
                    parent_project_id = excluded.parent_project_id,
                    parent_mr_iid = excluded.parent_mr_iid,
                    parent_sha = excluded.parent_sha,
                    run_id = excluded.run_id,
                    status = CASE
                        WHEN auto_fix_mrs.status = 'merged' THEN 'merged'
                        ELSE excluded.status
                    END,
                    merged_sha = CASE
                        WHEN auto_fix_mrs.status = 'merged'
                            THEN COALESCE(auto_fix_mrs.merged_sha, excluded.merged_sha)
                        WHEN excluded.status = 'merged' THEN excluded.merged_sha
                        ELSE NULL
                    END
                """,
                (
                    parent_project_id,
                    parent_mr_iid,
                    parent_sha,
                    run_id,
                    auto_fix_project_id,
                    auto_fix_mr_iid,
                    status,
                    merged_sha,
                ),
            )
            connection.execute(
                """
                UPDATE auto_fix_intents
                SET status = CASE WHEN status = 'merged' THEN status ELSE 'created' END,
                    auto_fix_project_id = ?, auto_fix_mr_iid = ?, updated_at = ?
                WHERE run_id = ?
                """,
                (auto_fix_project_id, auto_fix_mr_iid, self._now().isoformat(), run_id),
            )
            pending = connection.execute(
                """
                SELECT merged_sha FROM auto_fix_merge_events
                WHERE auto_fix_project_id = ? AND auto_fix_mr_iid = ?
                """,
                (auto_fix_project_id, auto_fix_mr_iid),
            ).fetchone()
            if pending is not None:
                connection.execute(
                    """
                    UPDATE auto_fix_mrs SET status = 'merged', merged_sha = ?
                    WHERE auto_fix_project_id = ? AND auto_fix_mr_iid = ?
                    """,
                    (pending["merged_sha"], auto_fix_project_id, auto_fix_mr_iid),
                )
                connection.execute(
                    """
                    UPDATE auto_fix_intents SET status = 'merged', updated_at = ?
                    WHERE run_id = ?
                    """,
                    (self._now().isoformat(), run_id),
                )
                connection.execute(
                    """
                    DELETE FROM auto_fix_merge_events
                    WHERE auto_fix_project_id = ? AND auto_fix_mr_iid = ?
                    """,
                    (auto_fix_project_id, auto_fix_mr_iid),
                )
            row = connection.execute(
                """
                SELECT id AS relation_id, parent_project_id, parent_mr_iid, parent_sha,
                       run_id, auto_fix_project_id, auto_fix_mr_iid, merged_sha, status
                FROM auto_fix_mrs
                WHERE auto_fix_project_id = ? AND auto_fix_mr_iid = ?
                """,
                (auto_fix_project_id, auto_fix_mr_iid),
            ).fetchone()
        assert row is not None
        return AutoFixRelation(**dict(row))

    def mark_auto_fix_merged(
        self, auto_fix_project_id: str, auto_fix_mr_iid: int, merged_sha: str
    ) -> None:
        if not is_valid_sha(merged_sha):
            raise ValueError("invalid SHA: expected 40 or 64 hexadecimal characters")
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE auto_fix_mrs SET status = 'merged', merged_sha = ?
                WHERE auto_fix_project_id = ? AND auto_fix_mr_iid = ?
                """,
                (merged_sha, auto_fix_project_id, auto_fix_mr_iid),
            )
            if cursor.rowcount:
                connection.execute(
                    """
                    UPDATE auto_fix_intents
                    SET status = 'merged', updated_at = ?
                    WHERE auto_fix_project_id = ? AND auto_fix_mr_iid = ?
                    """,
                    (self._now().isoformat(), auto_fix_project_id, auto_fix_mr_iid),
                )
                connection.execute(
                    """
                    DELETE FROM auto_fix_merge_events
                    WHERE auto_fix_project_id = ? AND auto_fix_mr_iid = ?
                    """,
                    (auto_fix_project_id, auto_fix_mr_iid),
                )
            else:
                connection.execute(
                    """
                    INSERT INTO auto_fix_merge_events(
                        auto_fix_project_id, auto_fix_mr_iid, merged_sha, created_at
                    ) VALUES (?, ?, ?, ?)
                    ON CONFLICT(auto_fix_project_id, auto_fix_mr_iid) DO UPDATE SET
                        merged_sha = excluded.merged_sha,
                        created_at = excluded.created_at
                    """,
                    (
                        auto_fix_project_id,
                        auto_fix_mr_iid,
                        merged_sha,
                        self._now().isoformat(),
                    ),
                )

    def is_suppressed_parent_sha(self, project_id: str, mr_iid: int, sha: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM auto_fix_mrs
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
                       delivered_at, last_error
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
                       delivered_at, last_error
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
                       delivered_at, last_error
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
                       delivered_at, last_error
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
                   delivered_at, last_error
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
                    last_error = CASE WHEN attempts >= ?
                        THEN 'notification delivery lease expired'
                        ELSE 'notification delivery lease expired; retrying' END
                WHERE status = 'running'
                  AND (lease_until IS NULL OR lease_until <= ?)
                """,
                (max_attempts, max_attempts, now_text, max_attempts, now_text),
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
                query += (
                    " AND claim_token = ? AND lease_until IS NOT NULL AND lease_until > ?"
                )
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
                "lease_until = NULL, claim_token = NULL, delivered_at = ?, last_error = ? "
                "WHERE id = ? AND status = 'running'"
            )
            parameters = (status, next_attempt_at, delivered_at, last_error, delivery_id)
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
                query += (
                    " AND claim_token = ? AND lease_until IS NOT NULL AND lease_until > ?"
                )
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
                last_error = (
                    self._redact(detail) if detail else "worktree cleanup failed; retrying"
                )
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
