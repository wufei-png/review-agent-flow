import subprocess
from pathlib import Path

import pytest

from review_agent_flow.repository import GitShaEnsurer, ShaFetchError


def _git_repository(path: Path) -> str:
    subprocess.run(["git", "init", "-b", "main", str(path)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Test"], check=True)
    (path / "README.md").write_text("initial\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-m", "initial"], check=True, capture_output=True)
    return subprocess.check_output(["git", "-C", str(path), "rev-parse", "HEAD"], text=True).strip()


def test_ensure_sha_fetches_missing_commit_under_repository_lock(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    _git_repository(source)
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(source), "remote", "add", "origin", str(remote)], check=True)
    subprocess.run(["git", "-C", str(source), "push", "-u", "origin", "main"], check=True, capture_output=True)

    checkout = tmp_path / "checkout"
    subprocess.run(["git", "clone", str(remote), str(checkout)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(source), "config", "user.email", "test@example.com"], check=True)
    (source / "README.md").write_text("new commit\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(source), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(source), "commit", "-m", "second"], check=True, capture_output=True)
    new_sha = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
    subprocess.run(["git", "-C", str(source), "push", "origin", "main"], check=True, capture_output=True)

    GitShaEnsurer(checkout, fetch_timeout_seconds=5).ensure_sha(new_sha)
    assert subprocess.run(
        ["git", "-C", str(checkout), "cat-file", "-e", f"{new_sha}^{{commit}}"],
        check=False,
    ).returncode == 0


def test_temporary_commit_probe_failure_is_retryable(monkeypatch, tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    ensurer = GitShaEnsurer(repository)

    def timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired("git cat-file", 1)

    monkeypatch.setattr("review_agent_flow.repository.subprocess.run", timeout)

    with pytest.raises(ShaFetchError, match="checking commit .* timed out"):
        ensurer.ensure_sha("a" * 40)
