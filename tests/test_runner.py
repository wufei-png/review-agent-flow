import os
import sys
from pathlib import Path

import pytest

from review_agent_flow.runner import AgentContext, LeaseLostError, LocalCliAgentRunner

SHA = "a" * 40


def test_local_runner_passes_review_context(tmp_path: Path) -> None:
    result = LocalCliAgentRunner(
        [
            sys.executable,
            "-c",
            "import os; print(os.environ['REVIEW_AGENT_FLOW_MR_IID'])",
        ],
        timeout_seconds=5,
    ).run(AgentContext("42", 7, 11, SHA, "https://gitlab.example", tmp_path))
    assert result.returncode == 0
    assert result.stdout.strip() == "7"


def test_runner_exposes_auto_fix_policy(tmp_path: Path) -> None:
    result = LocalCliAgentRunner(
        [sys.executable, "-c", "import os; print(os.environ['REVIEW_AGENT_FLOW_AUTO_FIX_ENABLED'])"],
        timeout_seconds=5,
    ).run(AgentContext("42", 7, 11, SHA, "https://gitlab.example", tmp_path, auto_fix_enabled=True))
    assert result.stdout.strip() == "true"


def test_runner_exposes_run_id(tmp_path: Path) -> None:
    result = LocalCliAgentRunner(
        [sys.executable, "-c", "import os; print(os.environ['REVIEW_AGENT_FLOW_RUN_ID'])"],
        timeout_seconds=5,
    ).run(AgentContext("42", 7, 11, SHA, "https://gitlab.example", tmp_path))
    assert result.stdout.strip() == "11"


def test_runner_heartbeats_during_long_agent_without_a_background_thread(tmp_path: Path) -> None:
    heartbeats: list[int] = []
    result = LocalCliAgentRunner(
        [sys.executable, "-c", "import time; time.sleep(0.2)"],
        timeout_seconds=2,
    ).run(
        AgentContext("42", 7, 11, SHA, "https://gitlab.example", tmp_path),
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
        "subprocess.Popen([sys.executable, '-c', sys.argv[2], sys.argv[1]]); "
        "time.sleep(5)"
    )

    def heartbeat() -> bool:
        raise RuntimeError("heartbeat backend failed")

    with pytest.raises(LeaseLostError):
        LocalCliAgentRunner(
            [sys.executable, "-c", parent_code, str(marker), child_code],
            timeout_seconds=2,
        ).run(
            AgentContext("42", 7, 11, SHA, "https://gitlab.example", tmp_path),
            heartbeat=heartbeat,
            heartbeat_interval_seconds=0.05,
        )

    assert not marker.exists()
