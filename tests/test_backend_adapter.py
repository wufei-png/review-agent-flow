import hashlib
import json
from pathlib import Path

import pytest

from review_agent_flow.agent_task import AgentTaskSnapshot, AgentTaskV1
from review_agent_flow.backend_adapter import build_backend_invocation


@pytest.mark.parametrize(("auto_fix_enabled", "edit_default"), [(False, "deny"), (True, "allow")])
def test_opencode_adapter_maps_autofix_to_task_scoped_edit_permission(
    tmp_path: Path,
    auto_fix_enabled: bool,
    edit_default: str,
) -> None:
    skill = (tmp_path / "SKILL.md").resolve()
    skill.write_text("# Review\n", encoding="utf-8")
    result = (tmp_path / "private" / "result.json").resolve()
    snapshot = AgentTaskSnapshot(
        1,
        "42",
        7,
        "Review",
        "42",
        "feature",
        "main",
        "a" * 40,
        "b" * 40,
        "c" * 40,
        str(skill),
        "# Review\n",
        hashlib.sha256(b"# Review\n").hexdigest(),
        "2026-08-10T00:00:00+00:00",
    )
    task = AgentTaskV1(snapshot, skill, result, auto_fix_enabled)

    invocation = build_backend_invocation("opencode", ("opencode",), task, tmp_path / "task.md")

    assert invocation.command[:3] == ("opencode", "run", "--pure")
    assert invocation.command[3:5] == ("--agent", "reviewflow")
    assert invocation.command[-1] == task.render_prompt()
    assert "--auto" not in invocation.command
    config = json.loads(invocation.environment["OPENCODE_CONFIG_CONTENT"])
    assert config["permission"]["edit"]["*"] == edit_default
    assert config["permission"]["edit"][str(result.parent / "**")] == "allow"
    assert config["permission"]["read"] == "deny"
    assert config["permission"]["skill"] == "deny"
    assert config["permission"]["bash"]["*"] == "deny"
    assert (
        config["permission"]["bash"][f"git diff --no-ext-diff {'a' * 40}...{'c' * 40}"] == "allow"
    )
    assert all(not pattern.endswith("-- *") for pattern in config["permission"]["bash"])
    assert config["agent"]["reviewflow"]["permission"] == config["permission"]
    assert config["share"] == "disabled"
    assert invocation.environment["OPENCODE_DISABLE_PROJECT_CONFIG"] == "1"
    assert invocation.environment["OPENCODE_DISABLE_SHARE"] == "1"
    assert invocation.environment["OPENCODE_CONFIG_DIR"] == str(result.parent / "opencode-config")
    assert invocation.environment["XDG_DATA_HOME"] == str(result.parent / "opencode-data")


def test_command_adapter_preserves_operator_argv_and_supplies_task_path(tmp_path: Path) -> None:
    skill = (tmp_path / "SKILL.md").resolve()
    skill.write_text("# Review\n", encoding="utf-8")
    result = (tmp_path / "result.json").resolve()
    snapshot = AgentTaskSnapshot(
        1,
        "42",
        7,
        "Review",
        "42",
        "feature",
        "main",
        "a" * 40,
        "b" * 40,
        "c" * 40,
        str(skill),
        "# Review\n",
        hashlib.sha256(b"# Review\n").hexdigest(),
        "2026-08-10T00:00:00+00:00",
    )
    task = AgentTaskV1(snapshot, skill, result, False)
    task_path = tmp_path / "task.md"

    invocation = build_backend_invocation("command", ("custom-agent", "--batch"), task, task_path)

    assert invocation.command == ("custom-agent", "--batch")
    assert invocation.environment == {"REVIEW_AGENT_FLOW_TASK_PATH": str(task_path)}


def test_opencode_adapter_passes_operator_profile_model(tmp_path: Path) -> None:
    skill = (tmp_path / "SKILL.md").resolve()
    skill.write_text("# Review\n", encoding="utf-8")
    snapshot = AgentTaskSnapshot(
        1,
        "42",
        7,
        "Review",
        "42",
        "feature",
        "main",
        "a" * 40,
        "b" * 40,
        "c" * 40,
        str(skill),
        "# Review\n",
        hashlib.sha256(b"# Review\n").hexdigest(),
        "2026-08-10T00:00:00+00:00",
    )
    task = AgentTaskV1(snapshot, skill, (tmp_path / "result.json").resolve(), False)

    invocation = build_backend_invocation(
        "opencode", ("opencode",), task, None, "openai/review-model"
    )

    assert invocation.command[-3:] == (
        "--model",
        "openai/review-model",
        task.render_prompt(),
    )
