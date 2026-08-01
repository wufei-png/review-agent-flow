"""SQLite persistence for idempotent Review Runs and their side effects."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

DEFAULT_LEASE_SECONDS = 1860
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_NOTIFICATION_LEASE_SECONDS = 30
DEFAULT_NOTIFICATION_MAX_ATTEMPTS = 3
DEFAULT_WORKTREE_CLEANUP_LEASE_SECONDS = 60
DEFAULT_WORKTREE_CLEANUP_MAX_ATTEMPTS = 5
SCHEMA_VERSION = 2


def auto_fix_intent_marker(run_id: int, parent_sha: str) -> str:
    """Return the stable identity marker required in an Auto-fix MR."""

    return f"ReviewFlow-Auto-Fix-Run-ID:{run_id} SHA:{parent_sha}"


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

    Version 2 is intentionally a fresh schema.  There is no production database
    migration contract for this pre-release implementation; an older database
    must be recreated rather than guessed into a new notification or Auto-fix
    routing model.
    """

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
                PRAGMA user_version = 2;
                """
            )

    @staticmethod
    def _now() -> datetime:
        return datetime.now(UTC)

    @staticmethod
    def _run_from_row(row: sqlite3.Row) -> ReviewRun:
        return ReviewRun(**dict(row))

    def enqueue(self, project_id: str, mr_iid: int, sha: str, kind: str = "normal") -> ReviewRun:
        created_at = self._now().isoformat()
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
                FROM review_runs
                WHERE project_id = ? AND mr_iid = ? AND sha = ? AND kind = ?
                """,
                (project_id, mr_iid, sha, kind),
            ).fetchone()
        assert row is not None
        return self._run_from_row(row)

    def wake_run(self, project_id: str, mr_iid: int, sha: str) -> ReviewRun | None:
        """Make a deferred normal Run immediately eligible after a green Pipeline."""

        now = self._now().isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE review_runs
                SET next_attempt_at = ?, error = NULL
                WHERE project_id = ? AND mr_iid = ? AND sha = ?
                  AND kind = 'normal' AND status = 'pending'
                """,
                (now, project_id, mr_iid, sha),
            )
            row = connection.execute(
                """
                SELECT id AS run_id, project_id, mr_iid, sha, kind, status,
                       attempts, lease_until, claim_token
                FROM review_runs
                WHERE project_id = ? AND mr_iid = ? AND sha = ? AND kind = 'normal'
                """,
                (project_id, mr_iid, sha),
            ).fetchone()
        return self._run_from_row(row) if row is not None else None

    def claim_next(
        self,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    ) -> ReviewRun | None:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        if max_attempts <= 0:
            raise ValueError("max_attempts must be positive")

        now = self._now()
        now_text = now.isoformat()
        lease_until = (now + timedelta(seconds=lease_seconds)).isoformat()
        claim_token = uuid4().hex
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
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
                """,
                (
                    max_attempts,
                    max_attempts,
                    now_text,
                    max_attempts,
                    max_attempts,
                    now_text,
                    now_text,
                ),
            )
            row = connection.execute(
                """
                SELECT id AS run_id, project_id, mr_iid, sha, kind, status,
                       attempts, lease_until, claim_token
                FROM review_runs
                WHERE status = 'pending'
                  AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
                ORDER BY id
                LIMIT 1
                """,
                (now_text,),
            ).fetchone()
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
        return self._run_from_row(claimed)

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
                "lease_until = NULL, claim_token = NULL "
                "WHERE id = ? AND status = 'running'"
            )
            parameters: tuple[object, ...] = (
                status,
                self._now().isoformat(),
                error,
                run_id,
            )
            if claim_token is not None:
                query += " AND claim_token = ?"
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
        next_attempt_at = (self._now() + timedelta(seconds=delay_seconds)).isoformat()
        with self._connect() as connection:
            query = (
                "UPDATE review_runs SET status = 'pending', error = ?, finished_at = NULL, "
                "next_attempt_at = ?, lease_until = NULL, claim_token = NULL "
                "WHERE id = ? AND status = 'running'"
            )
            parameters: tuple[object, ...] = (reason, next_attempt_at, run_id)
            if claim_token is not None:
                query += " AND claim_token = ?"
                parameters += (claim_token,)
            cursor = connection.execute(query, parameters)
        return cursor.rowcount == 1

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
    ) -> AutoFixRelation:
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
                WHERE parent_project_id = ? AND parent_mr_iid = ? AND merged_sha = ?
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
    ) -> sqlite3.Row | None:
        if delivery_id is None:
            row = connection.execute(
                """
                SELECT id AS delivery_id, correlation_id, channel_id, channel,
                       title, body, url, recipients_json, status, attempts,
                       next_attempt_at, lease_until, claim_token, created_at,
                       delivered_at, last_error
                FROM notification_outbox
                WHERE status = 'pending' AND next_attempt_at <= ?
                ORDER BY id LIMIT 1
                """,
                (now_text,),
            ).fetchone()
        else:
            row = connection.execute(
                """
                SELECT id AS delivery_id, correlation_id, channel_id, channel,
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
                connection, delivery_id, now_text, lease_until, claim_token
            )
        return self._notification_from_row(claimed) if claimed is not None else None

    def claim_next_notification(
        self,
        lease_seconds: int = DEFAULT_NOTIFICATION_LEASE_SECONDS,
        max_attempts: int = DEFAULT_NOTIFICATION_MAX_ATTEMPTS,
    ) -> NotificationDelivery | None:
        return self._claim_notification(None, lease_seconds, max_attempts)

    def claim_notification(
        self,
        delivery_id: int,
        lease_seconds: int = DEFAULT_NOTIFICATION_LEASE_SECONDS,
        max_attempts: int = DEFAULT_NOTIFICATION_MAX_ATTEMPTS,
    ) -> NotificationDelivery | None:
        return self._claim_notification(delivery_id, lease_seconds, max_attempts)

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
            query = (
                "UPDATE notification_outbox SET status = ?, next_attempt_at = ?, "
                "lease_until = NULL, claim_token = NULL, delivered_at = ?, last_error = ? "
                "WHERE id = ? AND status = 'running'"
            )
            parameters = (status, next_attempt_at, delivered_at, last_error, delivery_id)
            if claim_token is not None:
                query += " AND claim_token = ?"
                parameters += (claim_token,)
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
                WHERE id = ? AND status != 'cleaned'
                """,
                (detail, self._now().isoformat(), cleanup_id),
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
                (self._now().isoformat(), detail, cleanup_id),
            )
        return cursor.rowcount == 1

    def mark_worktree_cleaned(self, cleanup_id: int, claim_token: str | None = None) -> bool:
        query = (
            "UPDATE worktree_cleanup SET status = 'cleaned', cleaned_at = ?, "
            "next_attempt_at = NULL, lease_until = NULL, claim_token = NULL "
            "WHERE id = ? AND status IN ('active', 'pending_cleanup', 'running')"
        )
        parameters: tuple[object, ...] = (self._now().isoformat(), cleanup_id)
        if claim_token is not None:
            query += " AND claim_token = ?"
            parameters += (claim_token,)
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
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            query = "SELECT attempts FROM worktree_cleanup WHERE id = ? AND status = 'running'"
            parameters: tuple[object, ...] = (cleanup_id,)
            if claim_token is not None:
                query += " AND claim_token = ?"
                parameters += (claim_token,)
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
                last_error = detail or "worktree cleanup failed"
            else:
                status = "pending_cleanup"
                next_attempt_at = (
                    self._now() + timedelta(seconds=retry_delay_seconds * (2 ** (attempts - 1)))
                ).isoformat()
                cleaned_at = None
                last_error = detail or "worktree cleanup failed; retrying"
            query = (
                "UPDATE worktree_cleanup SET status = ?, next_attempt_at = ?, "
                "lease_until = NULL, claim_token = NULL, cleaned_at = ?, last_error = ? "
                "WHERE id = ? AND status = 'running'"
            )
            parameters = (status, next_attempt_at, cleaned_at, last_error, cleanup_id)
            if claim_token is not None:
                query += " AND claim_token = ?"
                parameters += (claim_token,)
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
