from pathlib import Path

import pytest

from review_agent_flow.policy import PolicyError, load_policy


def test_missing_policy_uses_safe_defaults(tmp_path: Path) -> None:
    policy = load_policy(tmp_path / "missing.yml")
    assert policy.ai.enabled is False
    assert policy.ai.auto_fix is False
    assert policy.human.enabled is True


def test_loads_project_policy(tmp_path: Path) -> None:
    path = tmp_path / ".gitlab" / "reviewflow.yml"
    path.parent.mkdir()
    path.write_text(
        """
version: 4
pipeline_gate: true
human:
  reviewers: [alice, bob]
ai:
  enabled: true
  backend: local-agent
  profile: default
  skill: .gitlab/review-agent/SKILL.md
  auto_fix: true
notifications:
      channels:
        - feishu-reviewers
""",
        encoding="utf-8",
    )
    policy = load_policy(path)
    assert policy.pipeline_gate is True
    assert policy.human.reviewers == ("alice", "bob")
    assert policy.ai.backend == "local-agent"
    assert policy.ai.profile == "default"
    assert policy.ai.auto_fix is True
    assert policy.notifications.channels == ("feishu-reviewers",)


def test_example_policy_matches_current_schema() -> None:
    path = Path(__file__).parents[1] / "examples" / "reviewflow.yml"

    policy = load_policy(path)

    assert policy.ai.backend == "local-agent"
    assert policy.ai.profile == "default"


def test_rejects_invalid_policy(tmp_path: Path) -> None:
    path = tmp_path / "reviewflow.yml"
    path.write_text("ai:\n  enabled: maybe\n", encoding="utf-8")
    with pytest.raises(PolicyError):
        load_policy(path)


def test_policy_rejects_oversized_project_input(tmp_path: Path) -> None:
    path = tmp_path / "reviewflow.yml"
    path.write_text("#" + "x" * (256 * 1024), encoding="utf-8")

    with pytest.raises(PolicyError, match="exceeds"):
        load_policy(path)


def test_policy_rejects_duplicate_authority_keys(tmp_path: Path) -> None:
    path = tmp_path / "reviewflow.yml"
    path.write_text(
        "version: 4\nai:\n  enabled: false\n  enabled: true\n",
        encoding="utf-8",
    )

    with pytest.raises(PolicyError, match="duplicate key: enabled"):
        load_policy(path)


def test_ai_enabled_requires_a_backend_and_profile(tmp_path: Path) -> None:
    path = tmp_path / "reviewflow.yml"
    path.write_text("ai:\n  enabled: true\n", encoding="utf-8")
    with pytest.raises(PolicyError, match="ai.backend and ai.profile"):
        load_policy(path)


def test_rejects_unknown_version_one_fields(tmp_path: Path) -> None:
    path = tmp_path / "reviewflow.yml"
    path.write_text("pipeline_gat: true\n", encoding="utf-8")
    with pytest.raises(PolicyError, match="unknown fields"):
        load_policy(path)


def test_requires_explicit_policy_schema_upgrade(tmp_path: Path) -> None:
    path = tmp_path / "reviewflow.yml"
    path.write_text("version: 1\n", encoding="utf-8")
    with pytest.raises(PolicyError, match="unsupported policy version: 1"):
        load_policy(path)


def test_policy_rejects_project_owned_command_and_version_two(tmp_path: Path) -> None:
    path = tmp_path / "reviewflow.yml"
    path.write_text(
        "version: 4\nai:\n  enabled: true\n  command: [opencode, run]\n",
        encoding="utf-8",
    )
    with pytest.raises(PolicyError, match="unknown fields: command"):
        load_policy(path)
    path.write_text("version: 2\n", encoding="utf-8")
    with pytest.raises(PolicyError, match="unsupported policy version: 2"):
        load_policy(path)


@pytest.mark.parametrize("skill", ["../../operator/SKILL.md", "/etc/review/SKILL.md"])
def test_policy_rejects_skill_paths_outside_the_repository(tmp_path: Path, skill: str) -> None:
    path = tmp_path / "reviewflow.yml"
    path.write_text(f"version: 4\nai:\n  skill: {skill}\n", encoding="utf-8")

    with pytest.raises(PolicyError, match="repository-relative"):
        load_policy(path)
