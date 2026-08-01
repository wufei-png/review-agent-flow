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
version: 1
pipeline_gate: true
human:
  reviewers: [alice, bob]
ai:
  enabled: true
  command: opencode run
  skill: .gitlab/review-agent/SKILL.md
  auto_fix: true
notifications:
  channels:
    - type: feishu
""",
        encoding="utf-8",
    )
    policy = load_policy(path)
    assert policy.pipeline_gate is True
    assert policy.human.reviewers == ("alice", "bob")
    assert policy.ai.command == ("opencode", "run")
    assert policy.ai.auto_fix is True
    assert policy.notifications.channels[0]["type"] == "feishu"


def test_rejects_invalid_policy(tmp_path: Path) -> None:
    path = tmp_path / "reviewflow.yml"
    path.write_text("ai:\n  enabled: maybe\n", encoding="utf-8")
    with pytest.raises(PolicyError):
        load_policy(path)


def test_ai_enabled_requires_a_command(tmp_path: Path) -> None:
    path = tmp_path / "reviewflow.yml"
    path.write_text("ai:\n  enabled: true\n", encoding="utf-8")
    with pytest.raises(PolicyError, match="ai.command"):
        load_policy(path)
