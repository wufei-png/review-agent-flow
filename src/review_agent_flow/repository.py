"""Repository object availability checks used before creating a worktree."""

from __future__ import annotations

import fcntl
import subprocess
from dataclasses import dataclass
from pathlib import Path


class ShaUnavailableError(RuntimeError):
    """Raised when a requested commit cannot be made available locally."""


class ShaFetchError(ShaUnavailableError):
    """Raised when a temporary transport or local fetch failure may recover."""


_PERMANENT_FETCH_ERROR_MARKERS = (
    "couldn't find remote ref",
    "could not find remote ref",
    "not our ref",
    "unknown revision",
    "not a valid object name",
    "invalid refspec",
    "repository not found",
    "authentication failed",
    "permission denied",
    "could not read username",
    "returned error: 401",
    "returned error: 403",
)


def _is_permanent_fetch_error(detail: str) -> bool:
    lowered = detail.lower()
    return any(marker in lowered for marker in _PERMANENT_FETCH_ERROR_MARKERS)


@dataclass(frozen=True)
class GitShaEnsurer:
    repository: Path
    remote: str = "origin"
    fetch_timeout_seconds: int = 60

    def __post_init__(self) -> None:
        if self.fetch_timeout_seconds <= 0:
            raise ValueError("fetch_timeout_seconds must be positive")

    def ensure_sha(self, sha: str) -> None:
        if not sha:
            raise ShaUnavailableError("empty SHA")
        if self._has_commit(sha):
            return

        git_dir = self._git_dir()
        lock_path = git_dir / "review-agent-flow.fetch.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with lock_path.open("a+", encoding="utf-8") as lock_file:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                if self._has_commit(sha):
                    return
                completed = subprocess.run(
                    [
                        "git",
                        "-C",
                        str(self.repository),
                        "fetch",
                        "--no-tags",
                        "--prune",
                        self.remote,
                        sha,
                    ],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=self.fetch_timeout_seconds,
                )
                if completed.returncode:
                    detail = completed.stderr.strip() or completed.stdout.strip()
                    error_type = (
                        ShaUnavailableError if _is_permanent_fetch_error(detail) else ShaFetchError
                    )
                    raise error_type(f"git fetch for {sha} failed: {detail or 'unknown git error'}")
                if not self._has_commit(sha):
                    raise ShaUnavailableError(
                        f"git fetch completed but commit {sha} is still unavailable"
                    )
        except subprocess.TimeoutExpired as exc:
            raise ShaFetchError(f"git fetch for {sha} timed out") from exc
        except OSError as exc:
            raise ShaFetchError(f"cannot fetch {sha}: {exc}") from exc

    def _git_dir(self) -> Path:
        try:
            completed = subprocess.run(
                ["git", "-C", str(self.repository), "rev-parse", "--git-dir"],
                check=False,
                capture_output=True,
                text=True,
                timeout=self.fetch_timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            raise ShaFetchError("resolving repository git dir timed out") from exc
        except OSError as exc:
            raise ShaFetchError(f"cannot inspect repository: {exc}") from exc
        if completed.returncode:
            detail = completed.stderr.strip() or completed.stdout.strip()
            raise ShaUnavailableError(f"cannot resolve repository git dir: {detail}")
        git_dir = Path(completed.stdout.strip())
        return git_dir if git_dir.is_absolute() else self.repository / git_dir

    def _has_commit(self, sha: str) -> bool:
        try:
            completed = subprocess.run(
                [
                    "git",
                    "-C",
                    str(self.repository),
                    "cat-file",
                    "-e",
                    f"{sha}^{{commit}}",
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=self.fetch_timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            raise ShaFetchError(f"checking commit {sha} timed out") from exc
        except OSError as exc:
            raise ShaFetchError(f"cannot inspect commit {sha}: {exc}") from exc
        return completed.returncode == 0
