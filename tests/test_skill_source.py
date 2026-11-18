import os
import subprocess
from pathlib import Path

import pytest

from review_agent_flow.skill_source import (
    MAX_AGENT_SKILL_BYTES,
    SkillSourceError,
    load_operator_skill,
    load_project_skill,
)


def _repository(path: Path) -> str:
    subprocess.run(["git", "init", "-b", "main", str(path)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Test"], check=True)
    return ""


def _commit(path: Path, message: str) -> str:
    subprocess.run(["git", "-C", str(path), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(path), "commit", "-m", message],
        check=True,
        capture_output=True,
    )
    return subprocess.check_output(["git", "-C", str(path), "rev-parse", "HEAD"], text=True).strip()


def test_project_skill_reads_exact_commit_not_worktree(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    _repository(repository)
    skill = repository / ".reviewflow" / "SKILL.md"
    skill.parent.mkdir()
    skill.write_text("# Target Skill\n", encoding="utf-8")
    target_sha = _commit(repository, "target skill")
    skill.write_text("# MR-controlled replacement\n", encoding="utf-8")
    _commit(repository, "replace skill")

    assert load_project_skill(repository, target_sha, ".reviewflow/SKILL.md") == (
        "# Target Skill\n"
    )


def test_project_skill_rejects_symlink_and_oversized_blob(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    _repository(repository)
    (repository / "outside.md").write_text("# Outside\n", encoding="utf-8")
    os.symlink("outside.md", repository / "SKILL.md")
    symlink_sha = _commit(repository, "symlink")
    with pytest.raises(SkillSourceError, match="not a symlink"):
        load_project_skill(repository, symlink_sha, "SKILL.md")

    (repository / "SKILL.md").unlink()
    (repository / "SKILL.md").write_bytes(b"x" * (MAX_AGENT_SKILL_BYTES + 1))
    oversized_sha = _commit(repository, "oversized")
    with pytest.raises(SkillSourceError, match="exceeds"):
        load_project_skill(repository, oversized_sha, "SKILL.md")


def test_operator_skill_rejects_symlink(tmp_path: Path) -> None:
    target = tmp_path / "target.md"
    target.write_text("# Skill\n", encoding="utf-8")
    linked = tmp_path / "SKILL.md"
    linked.symlink_to(target)

    with pytest.raises(SkillSourceError, match="regular file"):
        load_operator_skill(linked)


@pytest.mark.parametrize(
    ("content", "message"),
    [(b"\xff", "UTF-8"), (b"valid\x00text", "NUL")],
)
def test_project_skill_rejects_invalid_text(tmp_path: Path, content: bytes, message: str) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    _repository(repository)
    (repository / "SKILL.md").write_bytes(content)
    revision = _commit(repository, "invalid text")

    with pytest.raises(SkillSourceError, match=message):
        load_project_skill(repository, revision, "SKILL.md")


def test_operator_skill_rejects_directory(tmp_path: Path) -> None:
    directory = tmp_path / "SKILL.md"
    directory.mkdir()

    with pytest.raises(SkillSourceError, match="regular file"):
        load_operator_skill(directory)
