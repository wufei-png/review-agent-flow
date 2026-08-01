"""Small SQLite store for idempotent Review Runs."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


@dataclass(frozen=True)
class ReviewRun:
    run_id: int
    project_id: str
    mr_iid: int
    sha: str
    kind: str
    status: str


class SQLiteStore:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
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
                    UNIQUE(project_id, mr_iid, sha, kind)
                );
                CREATE TABLE IF NOT EXISTS auto_fix_mrs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    parent_project_id TEXT NOT NULL,
                    parent_mr_iid INTEGER NOT NULL,
                    run_id INTEGER NOT NULL REFERENCES review_runs(id),
                    auto_fix_project_id TEXT NOT NULL,
                    auto_fix_mr_iid INTEGER NOT NULL,
                    merged_sha TEXT,
                    status TEXT NOT NULL DEFAULT 'opened',
                    UNIQUE(auto_fix_project_id, auto_fix_mr_iid)
                );
                """
            )

    def enqueue(self, project_id: str, mr_iid: int, sha: str, kind: str = "normal") -> ReviewRun:
        created_at = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO review_runs(project_id, mr_iid, sha, kind, created_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(project_id, mr_iid, sha, kind) DO NOTHING
                """,
                (project_id, mr_iid, sha, kind, created_at),
            )
            row = connection.execute(
                """
                SELECT id AS run_id, project_id, mr_iid, sha, kind, status
                FROM review_runs WHERE project_id = ? AND mr_iid = ? AND sha = ? AND kind = ?
                """,
                (project_id, mr_iid, sha, kind),
            ).fetchone()
        assert row is not None
        return ReviewRun(**dict(row))

    def claim_next(self) -> ReviewRun | None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT id AS run_id, project_id, mr_iid, sha, kind, status FROM review_runs "
                "WHERE status = 'pending' ORDER BY id LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            connection.execute("UPDATE review_runs SET status = 'running' WHERE id = ?", (row["run_id"],))
        return ReviewRun(**{**dict(row), "status": "running"})

    def finish(self, run_id: int, status: str) -> None:
        if status not in {"succeeded", "failed", "cancelled"}:
            raise ValueError(f"invalid terminal status: {status}")
        with self._connect() as connection:
            connection.execute("UPDATE review_runs SET status = ? WHERE id = ?", (status, run_id))
