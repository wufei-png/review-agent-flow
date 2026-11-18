import hashlib
import json
import os
import sys
from pathlib import Path

import pytest

from review_agent_flow.agent_task import AgentTaskSnapshot, AgentTaskV1
from review_agent_flow.runner import (
    MAX_AGENT_DIAGNOSTIC_BYTES,
    MAX_AGENT_DIAGNOSTIC_CHARS,
    MAX_AGENT_RESULT_BYTES,
    AgentContext,
    AgentContractError,
    LeaseLostError,
    LocalCliAgentRunner,
)

SHA = "a" * 40


def _context(tmp_path: Path, **values) -> AgentContext:
    return AgentContext(
        project_id="42",
        mr_iid=7,
        run_id=11,
        sha=SHA,
        worktree=tmp_path,
        result_path=tmp_path.parent / f"{tmp_path.name}-result.json",
        **values,
    )


def _adapted_context(tmp_path: Path, *, auto_fix_enabled: bool = False) -> AgentContext:
    skill_path = (tmp_path.parent / f"{tmp_path.name}-SKILL.md").resolve()
    skill_path.write_text("# Review\n", encoding="utf-8")
    result_path = (tmp_path.parent / f"{tmp_path.name}-result.json").resolve()
    snapshot = AgentTaskSnapshot(
        run_id=11,
        project_id="42",
        mr_iid=7,
        title="Review exact change",
        source_project_id="42",
        source_branch="feature",
        target_branch="main",
        base_sha="b" * 40,
        start_sha="c" * 40,
        head_sha=SHA,
        skill_source_path=str(skill_path),
        skill_content="# Review\n",
        skill_sha256=hashlib.sha256(b"# Review\n").hexdigest(),
        created_at="2026-08-10T00:00:00+00:00",
    )
    task = AgentTaskV1(snapshot, skill_path, result_path, auto_fix_enabled)
    return AgentContext(
        project_id="42",
        mr_iid=7,
        run_id=11,
        sha=SHA,
        worktree=tmp_path,
        result_path=result_path,
        skill_path=skill_path,
        auto_fix_enabled=auto_fix_enabled,
        task=task,
    )


def _success_code(diagnostic: str = "") -> str:
    return (
        "import json, os, pathlib; "
        "pathlib.Path(os.environ['REVIEW_AGENT_FLOW_RESULT_PATH']).write_text("
        "json.dumps({'version': 1, 'review_markdown': 'reviewed'})); " + diagnostic
    )


def test_runner_passes_controlled_context_and_parses_result(tmp_path: Path) -> None:
    result = LocalCliAgentRunner(
        [sys.executable, "-c", _success_code("print(os.environ['REVIEW_AGENT_FLOW_MR_IID'])")],
        timeout_seconds=5,
    ).run(_context(tmp_path))

    assert result.returncode == 0
    assert result.stdout.strip() == "7"
    assert result.output is not None
    assert result.output.review_markdown == "reviewed"


def test_command_adapter_receives_private_canonical_task_file(tmp_path: Path) -> None:
    code = _success_code(
        "task = pathlib.Path(os.environ['REVIEW_AGENT_FLOW_TASK_PATH']); "
        "print(task.read_text()); print(oct(task.stat().st_mode & 0o777))"
    )

    result = LocalCliAgentRunner(
        [sys.executable, "-c", code],
        timeout_seconds=5,
        adapter="command",
    ).run(_adapted_context(tmp_path))

    assert result.returncode == 0
    assert '"base_sha": "' + "b" * 40 + '"' in result.stdout
    assert "AgentTask v1" in result.stdout
    assert result.stdout.rstrip().endswith("0o600")


def test_opencode_adapter_uses_native_noninteractive_prompt_and_task_permissions(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "fake-opencode"
    capture = tmp_path / "argv.txt"
    config_capture = tmp_path / "config.json"
    executable.write_text(
        "#!/bin/sh\n"
        'printf "%s\\n" "$@" > "$CAPTURE_PATH"\n'
        'printf "%s" "$OPENCODE_CONFIG_CONTENT" > "$CONFIG_CAPTURE_PATH"\n'
        'printf \'%s\' \'{"version":1,"review_markdown":"reviewed"}\' '
        '> "$REVIEW_AGENT_FLOW_RESULT_PATH"\n',
        encoding="utf-8",
    )
    executable.chmod(0o755)

    context = _adapted_context(tmp_path)
    result = LocalCliAgentRunner(
        [str(executable)],
        timeout_seconds=5,
        adapter="opencode",
    ).run(
        context,
        extra_env={
            "PATH": os.environ.get("PATH", os.defpath),
            "CAPTURE_PATH": str(capture),
            "CONFIG_CAPTURE_PATH": str(config_capture),
        },
    )

    assert result.returncode == 0
    argv = capture.read_text(encoding="utf-8")
    assert argv.startswith(
        "run\n--pure\n--agent\nreviewflow\nYou are executing ReviewFlow AgentTask v1."
    )
    assert "--auto" not in argv
    assert context.task is not None and str(context.task.skill_path) in argv
    assert '"auto_fix_enabled": false' in argv
    config = json.loads(config_capture.read_text(encoding="utf-8"))
    permission = config["permission"]
    assert permission["edit"]["*"] == "deny"
    assert permission["read"] == "deny"
    assert permission["bash"]["*"] == "deny"
    review_range = "b" * 40 + "..." + SHA
    assert permission["bash"][f"git diff --no-ext-diff {review_range}"] == "allow"
    assert permission["task"] == "deny"
    assert permission["skill"] == "deny"
    assert permission["webfetch"] == "deny"
    assert config["share"] == "disabled"
    assert config["agent"]["reviewflow"]["permission"] == permission


def test_runner_parses_optional_autofix_proposal(tmp_path: Path) -> None:
    code = (
        "import os, pathlib; "
        "pathlib.Path(os.environ['REVIEW_AGENT_FLOW_RESULT_PATH']).write_text("
        '\'{"version":1,"review_markdown":"reviewed",\''
        '\'"autofix_proposal":{"summary":"Apply the safe fix"}}\')'
    )
    result = LocalCliAgentRunner([sys.executable, "-c", code], timeout_seconds=5).run(
        _context(tmp_path)
    )

    assert result.output is not None
    assert result.output.autofix_proposal is not None
    assert result.output.autofix_proposal.summary == "Apply the safe fix"


def test_runner_environment_is_allowlist_plus_controlled_context(tmp_path: Path) -> None:
    code = (
        "import json, os, pathlib; "
        "pathlib.Path(os.environ['REVIEW_AGENT_FLOW_RESULT_PATH']).write_text("
        "json.dumps({'version': 1, 'review_markdown': "
        "'GITLAB_TOKEN_PRESENT=' + str('GITLAB_TOKEN' in os.environ)})); "
        "print('ALLOWED=' + os.environ['MODEL_TOKEN']); "
        "print('GITLAB_TOKEN=' + str('GITLAB_TOKEN' in os.environ)); "
        "print('NOTE_MARKER=' + str('REVIEW_AGENT_FLOW_NOTE_MARKER' in os.environ))"
    )
    result = LocalCliAgentRunner([sys.executable, "-c", code], timeout_seconds=5).run(
        _context(tmp_path), extra_env={"MODEL_TOKEN": "allowed"}
    )
    assert result.output is not None
    assert result.output.review_markdown == "GITLAB_TOKEN_PRESENT=False"
    assert result.stdout.splitlines() == [
        "ALLOWED=[REDACTED]",
        "GITLAB_TOKEN=[REDACTED]",
        "NOTE_MARKER=False",
    ]


def test_runner_resolves_bare_operator_command_from_trusted_path(tmp_path: Path) -> None:
    executable_root = tmp_path / "operator-bin"
    executable_root.mkdir()
    executable = executable_root / "review-agent-test"
    executable.write_text(
        "#!/bin/sh\n"
        'printf \'%s\' \'{"version":1,"review_markdown":"reviewed"}\' '
        '> "$REVIEW_AGENT_FLOW_RESULT_PATH"\n'
        'printf "%s" "$PATH"\n',
        encoding="utf-8",
    )
    executable.chmod(0o755)

    result = LocalCliAgentRunner([executable.name], timeout_seconds=5).run(
        _context(tmp_path), extra_env={"PATH": str(executable_root)}
    )

    assert result.returncode == 0
    assert result.output is not None
    assert result.output.review_markdown == "reviewed"
    assert result.stdout == str(executable_root)


@pytest.mark.parametrize(
    "payload",
    [
        "not-json",
        '{"version": 2, "review_markdown": "x"}',
        '{"version": 1, "review_markdown": ""}',
        '{"version": 1, "review_markdown": "x", "unknown": true}',
        '{"version": 1, "review_markdown": "x", "autofix_proposal": {}}',
        '{"version": 1, "review_markdown": "x", "autofix_proposal": null}',
        '{"version": 1, "version": 1, "review_markdown": "x"}',
    ],
)
def test_runner_rejects_invalid_success_result(tmp_path: Path, payload: str) -> None:
    code = (
        "import os, pathlib; "
        f"pathlib.Path(os.environ['REVIEW_AGENT_FLOW_RESULT_PATH']).write_text({payload!r})"
    )
    with pytest.raises(AgentContractError):
        LocalCliAgentRunner([sys.executable, "-c", code], timeout_seconds=5).run(_context(tmp_path))


def test_runner_rejects_missing_symlink_and_oversized_results(tmp_path: Path) -> None:
    with pytest.raises(AgentContractError, match="missing"):
        LocalCliAgentRunner([sys.executable, "-c", "pass"], timeout_seconds=5).run(
            _context(tmp_path)
        )

    target = tmp_path.parent / "target.json"
    target.write_text('{"version": 1, "review_markdown": "x"}')
    symlink_code = (
        "import os, pathlib; "
        f"pathlib.Path(os.environ['REVIEW_AGENT_FLOW_RESULT_PATH']).symlink_to({str(target)!r})"
    )
    with pytest.raises(AgentContractError, match="symlink"):
        LocalCliAgentRunner([sys.executable, "-c", symlink_code], timeout_seconds=5).run(
            _context(tmp_path)
        )
    _context(tmp_path).result_path.unlink()

    oversize_code = (
        "import os, pathlib; pathlib.Path(os.environ['REVIEW_AGENT_FLOW_RESULT_PATH']).write_bytes("
        f"b'x' * {MAX_AGENT_RESULT_BYTES + 1})"
    )
    with pytest.raises(AgentContractError, match="size"):
        LocalCliAgentRunner([sys.executable, "-c", oversize_code], timeout_seconds=5).run(
            _context(tmp_path)
        )


def test_runner_bounds_binary_diagnostics_without_strict_decode(tmp_path: Path) -> None:
    code = (
        "import os, sys; "
        f"os.write(1, b'x' * {MAX_AGENT_DIAGNOSTIC_BYTES + 1}); "
        "os.write(2, b'invalid-utf8:\\xff'); sys.exit(7)"
    )
    result = LocalCliAgentRunner([sys.executable, "-c", code], timeout_seconds=5).run(
        _context(tmp_path)
    )

    assert result.returncode == 7
    assert len(result.stdout) <= MAX_AGENT_DIAGNOSTIC_CHARS
    assert result.stdout.startswith("...[truncated]\n")
    assert "oversized diagnostic line omitted" in result.stdout
    assert result.stderr == "invalid-utf8:\ufffd"


def test_runner_drops_partial_line_before_redacting_truncated_tail(tmp_path: Path) -> None:
    secret = "super-secret-value"
    tail_size = MAX_AGENT_DIAGNOSTIC_BYTES - (len(secret) - 5) - 1
    code = (
        "import os, sys; "
        f"os.write(1, b'prefix' + {secret!r}.encode() + b'\\n' + b'x' * {tail_size}); "
        "sys.exit(7)"
    )
    result = LocalCliAgentRunner([sys.executable, "-c", code], timeout_seconds=5).run(
        _context(tmp_path), extra_env={"MODEL_TOKEN": secret}
    )

    assert result.returncode == 7
    assert result.stdout.startswith("...[truncated]\n")
    assert secret not in result.stdout
    assert secret[5:] not in result.stdout
    assert len(result.stdout) <= MAX_AGENT_DIAGNOSTIC_CHARS


def test_runner_heartbeats_during_long_agent(tmp_path: Path) -> None:
    heartbeats: list[int] = []
    code = _success_code("import time; time.sleep(0.2)")
    result = LocalCliAgentRunner([sys.executable, "-c", code], timeout_seconds=2).run(
        _context(tmp_path),
        heartbeat=lambda: heartbeats.append(1) is None,
        heartbeat_interval_seconds=0.05,
    )
    assert result.returncode == 0
    assert heartbeats


@pytest.mark.skipif(os.name != "posix", reason="process-group assertion requires POSIX")
def test_runner_kills_agent_process_group_when_heartbeat_raises(tmp_path: Path) -> None:
    marker = tmp_path / "descendant-finished"
    child_code = (
        "import pathlib, sys, time; time.sleep(0.3); "
        "pathlib.Path(sys.argv[1]).write_text('descendant survived')"
    )
    parent_code = (
        "import subprocess, sys, time; "
        "subprocess.Popen([sys.executable, '-c', sys.argv[2], sys.argv[1]]); time.sleep(5)"
    )

    def heartbeat() -> bool:
        raise RuntimeError("heartbeat backend failed")

    with pytest.raises(LeaseLostError):
        LocalCliAgentRunner(
            [sys.executable, "-c", parent_code, str(marker), child_code], timeout_seconds=2
        ).run(
            _context(tmp_path),
            heartbeat=heartbeat,
            heartbeat_interval_seconds=0.05,
        )
    assert not marker.exists()
