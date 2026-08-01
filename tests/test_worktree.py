import shutil
import subprocess
from pathlib import Path

from review_agent_flow.worktree import GitWorktreeManager


def _git_repository(path: Path) -> str:
    subprocess.run(["git", "init", "-b", "main", str(path)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Test"], check=True)
    (path / "README.md").write_text("test\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-m", "initial"], check=True, capture_output=True)
    return subprocess.check_output(["git", "-C", str(path), "rev-parse", "HEAD"], text=True).strip()


def test_remove_prunes_git_metadata_when_worktree_directory_is_missing(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    sha = _git_repository(repository)
    root = tmp_path / "worktrees"
    manager = GitWorktreeManager(repository, root)
    worktree = manager.create(sha, "1")
    shutil.rmtree(worktree)

    manager.remove(worktree)

    worktrees = subprocess.check_output(
        ["git", "-C", str(repository), "worktree", "list", "--porcelain"], text=True
    )
    assert str(worktree) not in worktrees
