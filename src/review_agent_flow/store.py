"""Small SQLite store for idempotent Review Runs."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from .gitlab import is_valid_sha

DEFAULT_LEASE_SECONDS = 1860
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_NOTIFICATION_LEASE_SECONDS = 30
DEFAULT_NOTIFICATION_MAX_ATTEMPTS = 3


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
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

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
                    UNIQUE(project_id, mr_iid, sha, kind)
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
            self._add_column_if_missing(
                connection, "auto_fix_mrs", "parent_sha", "TEXT NOT NULL DEFAULT ''"
            )

    @staticmethod
    def _add_column_if_missing(
        connection: sqlite3.Connection, table: str, column: str, definition: str
    ) -> None:
        columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

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
                """
                SELECT id AS run_id, project_id, mr_iid, sha, kind, status,
                       attempts, lease_until, claim_token
                FROM review_runs WHERE project_id = ? AND mr_iid = ? AND sha = ? AND kind = ?
                """,
                (project_id, mr_iid, sha, kind),
            ).fetchone()
        assert row is not None
        return ReviewRun(**dict(row))

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
            select_query = """
                SELECT id AS run_id, project_id, mr_iid, sha, kind, status,
                       attempts, lease_until, claim_token
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
                """
                SELECT id AS run_id, project_id, mr_iid, sha, kind, status,
                       attempts, lease_until, claim_token
                FROM review_runs WHERE id = ?
                """,
                (row["run_id"],),
            ).fetchone()
        assert claimed is not None
        return ReviewRun(**dict(claimed))

    def finish(
        self,
        run_id: int,
        status: str,
        error: str | None = None,
        claim_token: str | None = None,
    ) -> bool:
        if status not in {"succeeded", "failed", "cancelled"}:
            raise ValueError(f"invalid terminal status: {status}")
        with self._connect() as connection:
            query = (
                "UPDATE review_runs SET status = ?, finished_at = ?, error = ?, "
                "lease_until = NULL, claim_token = NULL WHERE id = ?"
            )
            parameters: tuple[object, ...] = (status, datetime.now(UTC).isoformat(), error, run_id)
            if claim_token is not None:
                query += " AND status = 'running' AND claim_token = ?"
                parameters += (claim_token,)
            cursor = connection.execute(query, parameters)
        return cursor.rowcount == 1

    def requeue(
        self,
        run_id: int,
        reason: str | None = None,
        delay_seconds: float = 0.0,
        claim_token: str | None = None,
    ) -> bool:
        if delay_seconds < 0:
            raise ValueError("delay_seconds cannot be negative")
        next_attempt_at = (datetime.now(UTC) + timedelta(seconds=delay_seconds)).isoformat()
        with self._connect() as connection:
            query = (
                "UPDATE review_runs SET status = 'pending', error = ?, finished_at = NULL, "
                "next_attempt_at = ?, lease_until = NULL, claim_token = NULL WHERE id = ?"
            )
            parameters: tuple[object, ...] = (reason, next_attempt_at, run_id)
            if claim_token is not None:
                query += " AND status = 'running' AND claim_token = ?"
                parameters += (claim_token,)
            cursor = connection.execute(query, parameters)
        return cursor.rowcount == 1

    def cancel_and_enqueue_latest(
        self,
        run_id: int,
        project_id: str,
        mr_iid: int,
        sha: str,
        kind: str,
        reason: str,
        claim_token: str | None = None,
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
                "next_attempt_at = NULL, lease_until = NULL, claim_token = NULL "
                "WHERE id = ? AND status = 'running'"
            )
            update_parameters: tuple[object, ...] = (
                created_at,
                reason,
                run_id,
            )
            if claim_token is not None:
                update_query += " AND claim_token = ?"
                update_parameters += (claim_token,)
            if connection.execute(update_query, update_parameters).rowcount != 1:
                return None

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
                """
                SELECT id AS run_id, project_id, mr_iid, sha, kind, status,
                       attempts, lease_until, claim_token
                FROM review_runs WHERE project_id = ? AND mr_iid = ? AND sha = ? AND kind = ?
                """,
                (project_id, mr_iid, sha, kind),
            ).fetchone()
        assert row is not None
        return ReviewRun(**dict(row))

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
                last_error = detail or "notification delivery failed"
            else:
                status = "pending"
                delay = retry_delay_seconds * (2 ** max(attempts - 1, 0))
                next_attempt_at = (now + timedelta(seconds=delay)).isoformat()
                delivered_at = None
                last_error = detail or "notification delivery failed; retrying"
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
    ) -> AutoFixRelation:
        if not is_valid_sha(parent_sha):
            raise ValueError("invalid SHA: expected 40 or 64 hexadecimal characters")
        with self._connect() as connection:
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
                    parent_project_id,
                    parent_mr_iid,
                    parent_sha,
                    run_id,
                    auto_fix_project_id,
                    auto_fix_mr_iid,
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
                WHERE parent_project_id = ? AND parent_mr_iid = ? AND merged_sha = ?
                LIMIT 1
                """,
                (project_id, mr_iid, sha),
            ).fetchone()
        return row is not None
