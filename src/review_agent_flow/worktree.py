"""Disposable Git worktree lifecycle for one Review Run."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


class WorktreeError(RuntimeError):
    """Raised when Git cannot create or remove a worktree."""


@dataclass
class GitWorktreeManager:
    repository: Path
    root: Path

    def create(self, sha: str, run_id: str) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        target = self.root / f"run-{run_id}"
        if target.exists():
            raise WorktreeError(f"worktree path already exists: {target}")
        self._git("worktree", "add", "--detach", str(target), sha)
        return target

    def remove(self, path: Path) -> None:
        self._git("worktree", "remove", "--force", str(path))

    def _git(self, *args: str) -> None:
        completed = subprocess.run(
            ["git", "-C", str(self.repository), *args],
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode:
            detail = completed.stderr.strip() or completed.stdout.strip()
            raise WorktreeError(f"git {' '.join(args)} failed: {detail}")
