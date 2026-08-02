"""Small SQLite store for idempotent Review Runs."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

from .gitlab import is_valid_sha
from .security import redact

DEFAULT_LEASE_SECONDS = 1860
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_NOTIFICATION_LEASE_SECONDS = 30
DEFAULT_NOTIFICATION_MAX_ATTEMPTS = 3
DEFAULT_EFFECT_LEASE_SECONDS = 60


@dataclass(frozen=True)
class ReviewRun:
    run_id: int
    project_id: str
    mr_iid: int
    sha: str
    kind: str
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
class NotificationDelivery:
    delivery_id: int
    correlation_id: str
    channel_index: int
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


class SQLiteStore:
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

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS review_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_id TEXT NOT NULL,
                    mr_iid INTEGER NOT NULL,
                    sha TEXT NOT NULL,
                    kind TEXT NOT NULL DEFAULT 'normal',
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
                    UNIQUE(project_id, mr_iid, sha, kind)
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
                    channel_index INTEGER NOT NULL,
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
                    UNIQUE(correlation_id, channel_index)
                );
                """
            )
            self._add_column_if_missing(connection, "review_runs", "finished_at", "TEXT")
            self._add_column_if_missing(connection, "review_runs", "error", "TEXT")
            self._add_column_if_missing(connection, "review_runs", "next_attempt_at", "TEXT")
            self._add_column_if_missing(connection, "review_runs", "lease_until", "TEXT")
            self._add_column_if_missing(
                connection, "review_runs", "attempts", "INTEGER NOT NULL DEFAULT 0"
            )
            self._add_column_if_missing(connection, "review_runs", "claim_token", "TEXT")
            self._add_column_if_missing(connection, "review_runs", "failure_class", "TEXT")
            self._add_column_if_missing(
                connection, "review_runs", "retryable", "INTEGER NOT NULL DEFAULT 0"
            )
            self._add_column_if_missing(
                connection, "auto_fix_mrs", "parent_sha", "TEXT NOT NULL DEFAULT ''"
            )
            self._add_column_if_missing(connection, "effect_receipts", "next_attempt_at", "TEXT")
            self._add_column_if_missing(connection, "effect_receipts", "lease_until", "TEXT")
            self._add_column_if_missing(connection, "effect_receipts", "claim_token", "TEXT")

    @staticmethod
    def _add_column_if_missing(
        connection: sqlite3.Connection, table: str, column: str, definition: str
    ) -> None:
        columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    @staticmethod
    def _review_run_from_row(row: sqlite3.Row) -> ReviewRun:
        values = dict(row)
        values["retryable"] = bool(values.get("retryable", 0))
        return ReviewRun(**values)

    @staticmethod
    def _review_run_columns() -> str:
        return (
            "id AS run_id, project_id, mr_iid, sha, kind, status, attempts, "
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

    def enqueue(self, project_id: str, mr_iid: int, sha: str, kind: str = "normal") -> ReviewRun:
        if not project_id or not str(project_id).strip():
            raise ValueError("project_id cannot be empty")
        if not is_valid_sha(sha):
            raise ValueError("invalid SHA: expected 40 or 64 hexadecimal characters")
        created_at = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO review_runs(
                    project_id, mr_iid, sha, kind, created_at, next_attempt_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(project_id, mr_iid, sha, kind) DO NOTHING
                """,
                (project_id, mr_iid, sha, kind, created_at, created_at),
            )
            row = connection.execute(
                f"SELECT {self._review_run_columns()} FROM review_runs "
                "WHERE project_id = ? AND mr_iid = ? AND sha = ? AND kind = ?",
                (project_id, mr_iid, sha, kind),
            ).fetchone()
        assert row is not None
        return self._review_run_from_row(row)

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
            "kind": run.kind,
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

        now = datetime.now(UTC)
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
            query += " AND state = 'running' AND claim_token = ?"
            parameters += (claim_token,)
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
        kind: str,
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
                INSERT INTO review_runs(
                    project_id, mr_iid, sha, kind, created_at, next_attempt_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(project_id, mr_iid, sha, kind) DO NOTHING
                """,
                (project_id, mr_iid, sha, kind, created_at, created_at),
            )
            row = connection.execute(
                f"SELECT {self._review_run_columns()} FROM review_runs "
                "WHERE project_id = ? AND mr_iid = ? AND sha = ? AND kind = ?",
                (project_id, mr_iid, sha, kind),
            ).fetchone()
        assert row is not None
        return self._review_run_from_row(row)

    def enqueue_notifications(
        self,
        correlation_id: str,
        title: str,
        body: str,
        url: str | None,
        recipients: tuple[str, ...],
        channels: tuple[tuple[int, str], ...],
    ) -> tuple[NotificationDelivery, ...]:
        if not correlation_id:
            raise ValueError("notification correlation_id cannot be empty")
        created_at = datetime.now(UTC).isoformat()
        recipients_json = json.dumps(list(recipients), separators=(",", ":"))
        with self._connect() as connection:
            for channel_index, channel in channels:
                connection.execute(
                    """
                    INSERT INTO notification_outbox(
                        correlation_id, channel_index, channel, title, body, url,
                        recipients_json, next_attempt_at, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(correlation_id, channel_index) DO NOTHING
                    """,
                    (
                        correlation_id,
                        channel_index,
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
                SELECT id AS delivery_id, correlation_id, channel_index, channel,
                       title, body, url, recipients_json, status, attempts,
                       next_attempt_at, lease_until, claim_token, created_at,
                       delivered_at, last_error
                FROM notification_outbox
                WHERE correlation_id = ?
                ORDER BY channel_index
                """,
                (correlation_id,),
            ).fetchall()
        return tuple(self._notification_from_row(row) for row in rows)

    def get_notification_delivery(self, delivery_id: int) -> NotificationDelivery | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT id AS delivery_id, correlation_id, channel_index, channel,
                       title, body, url, recipients_json, status, attempts,
                       next_attempt_at, lease_until, claim_token, created_at,
                       delivered_at, last_error
                FROM notification_outbox WHERE id = ?
                """,
                (delivery_id,),
            ).fetchone()
        return self._notification_from_row(row) if row is not None else None

    def claim_next_notification(
        self,
        lease_seconds: int = DEFAULT_NOTIFICATION_LEASE_SECONDS,
        max_attempts: int = DEFAULT_NOTIFICATION_MAX_ATTEMPTS,
    ) -> NotificationDelivery | None:
        if lease_seconds <= 0:
            raise ValueError("notification lease_seconds must be positive")
        if max_attempts <= 0:
            raise ValueError("notification max_attempts must be positive")

        now = datetime.now(UTC)
        now_text = now.isoformat()
        lease_until = (now + timedelta(seconds=lease_seconds)).isoformat()
        claim_token = uuid4().hex
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE notification_outbox
                SET status = CASE
                        WHEN attempts >= ? THEN 'failed'
                        ELSE 'pending'
                    END,
                    next_attempt_at = CASE
                        WHEN attempts >= ? THEN NULL
                        ELSE ?
                    END,
                    lease_until = NULL,
                    claim_token = NULL,
                    last_error = CASE
                        WHEN attempts >= ? THEN 'notification delivery lease expired'
                        ELSE 'notification delivery lease expired; retrying'
                    END
                WHERE status = 'running'
                  AND (lease_until IS NULL OR lease_until <= ?)
                """,
                (max_attempts, max_attempts, now_text, max_attempts, now_text),
            )
            row = connection.execute(
                """
                SELECT id AS delivery_id, correlation_id, channel_index, channel,
                       title, body, url, recipients_json, status, attempts,
                       next_attempt_at, lease_until, claim_token, created_at,
                       delivered_at, last_error
                FROM notification_outbox
                WHERE status = 'pending' AND next_attempt_at <= ?
                ORDER BY id
                LIMIT 1
                """,
                (now_text,),
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
            claimed = connection.execute(
                """
                SELECT id AS delivery_id, correlation_id, channel_index, channel,
                       title, body, url, recipients_json, status, attempts,
                       next_attempt_at, lease_until, claim_token, created_at,
                       delivered_at, last_error
                FROM notification_outbox WHERE id = ?
                """,
                (row["delivery_id"],),
            ).fetchone()
        assert claimed is not None
        return self._notification_from_row(claimed)

    def claim_notification(
        self,
        delivery_id: int,
        lease_seconds: int = DEFAULT_NOTIFICATION_LEASE_SECONDS,
        max_attempts: int = DEFAULT_NOTIFICATION_MAX_ATTEMPTS,
    ) -> NotificationDelivery | None:
        if lease_seconds <= 0:
            raise ValueError("notification lease_seconds must be positive")
        if max_attempts <= 0:
            raise ValueError("notification max_attempts must be positive")

        now = datetime.now(UTC)
        now_text = now.isoformat()
        lease_until = (now + timedelta(seconds=lease_seconds)).isoformat()
        claim_token = uuid4().hex
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE notification_outbox
                SET status = CASE
                        WHEN attempts >= ? THEN 'failed'
                        ELSE 'pending'
                    END,
                    next_attempt_at = CASE
                        WHEN attempts >= ? THEN NULL
                        ELSE ?
                    END,
                    lease_until = NULL,
                    claim_token = NULL,
                    last_error = CASE
                        WHEN attempts >= ? THEN 'notification delivery lease expired'
                        ELSE 'notification delivery lease expired; retrying'
                    END
                WHERE id = ? AND status = 'running'
                  AND (lease_until IS NULL OR lease_until <= ?)
                """,
                (max_attempts, max_attempts, now_text, max_attempts, delivery_id, now_text),
            )
            row = connection.execute(
                """
                SELECT id AS delivery_id, correlation_id, channel_index, channel,
                       title, body, url, recipients_json, status, attempts,
                       next_attempt_at, lease_until, claim_token, created_at,
                       delivered_at, last_error
                FROM notification_outbox
                WHERE id = ? AND status = 'pending' AND next_attempt_at <= ?
                """,
                (delivery_id, now_text),
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
                (lease_until, claim_token, delivery_id),
            )
            claimed = connection.execute(
                """
                SELECT id AS delivery_id, correlation_id, channel_index, channel,
                       title, body, url, recipients_json, status, attempts,
                       next_attempt_at, lease_until, claim_token, created_at,
                       delivered_at, last_error
                FROM notification_outbox WHERE id = ?
                """,
                (delivery_id,),
            ).fetchone()
        assert claimed is not None
        return self._notification_from_row(claimed)

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
        now = datetime.now(UTC)
        now_text = now.isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            query = "SELECT attempts FROM notification_outbox WHERE id = ? AND status = 'running'"
            parameters: tuple[object, ...] = (delivery_id,)
            if claim_token is not None:
                query += " AND claim_token = ?"
                parameters += (claim_token,)
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
                last_error = (
                    self._redact(detail)
                    if detail
                    else "notification delivery failed"
                )
            else:
                status = "pending"
                delay = retry_delay_seconds * (2 ** max(attempts - 1, 0))
                next_attempt_at = (now + timedelta(seconds=delay)).isoformat()
                delivered_at = None
                last_error = (
                    self._redact(detail)
                    if detail
                    else "notification delivery failed; retrying"
                )
            update_query = (
                "UPDATE notification_outbox SET status = ?, next_attempt_at = ?, "
                "lease_until = NULL, claim_token = NULL, delivered_at = ?, last_error = ? "
                "WHERE id = ? AND status = 'running'"
            )
            update_parameters: tuple[object, ...] = (
                status,
                next_attempt_at,
                delivered_at,
                last_error,
                delivery_id,
            )
            if claim_token is not None:
                update_query += " AND claim_token = ?"
                update_parameters += (claim_token,)
            update = connection.execute(update_query, update_parameters)
        return update.rowcount == 1

    @staticmethod
    def _notification_from_row(row: sqlite3.Row) -> NotificationDelivery:
        values = dict(row)
        values["recipients"] = tuple(json.loads(values.pop("recipients_json")))
        return NotificationDelivery(**values)

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
            pending_merge = connection.execute(
                """
                SELECT merged_sha FROM auto_fix_merge_events
                WHERE auto_fix_project_id = ? AND auto_fix_mr_iid = ?
                """,
                (auto_fix_project_id, auto_fix_mr_iid),
            ).fetchone()
            if pending_merge is not None:
                connection.execute(
                    """
                    UPDATE auto_fix_mrs
                    SET status = 'merged', merged_sha = ?
                    WHERE auto_fix_project_id = ? AND auto_fix_mr_iid = ?
                    """,
                    (pending_merge["merged_sha"], auto_fix_project_id, auto_fix_mr_iid),
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

    def mark_auto_fix_merged(self, auto_fix_project_id: str, auto_fix_mr_iid: int, merged_sha: str) -> None:
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
                        datetime.now(UTC).isoformat(),
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
