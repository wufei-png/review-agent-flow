import sys
from pathlib import Path

from review_agent_flow.runner import AgentContext, LocalCliAgentRunner


def test_local_runner_passes_review_context(tmp_path: Path) -> None:
    result = LocalCliAgentRunner(
        [
            sys.executable,
            "-c",
            "import os; print(os.environ['REVIEW_AGENT_FLOW_MR_IID'])",
        ],
        timeout_seconds=5,
    ).run(AgentContext("42", 7, 11, "abc", "https://gitlab.example", tmp_path))
    assert result.returncode == 0
    assert result.stdout.strip() == "7"


def test_runner_exposes_auto_fix_policy(tmp_path: Path) -> None:
    result = LocalCliAgentRunner(
        [sys.executable, "-c", "import os; print(os.environ['REVIEW_AGENT_FLOW_AUTO_FIX_ENABLED'])"],
        timeout_seconds=5,
    ).run(AgentContext("42", 7, 11, "abc", "https://gitlab.example", tmp_path, auto_fix_enabled=True))
    assert result.stdout.strip() == "true"


def test_runner_exposes_run_id(tmp_path: Path) -> None:
    result = LocalCliAgentRunner(
        [sys.executable, "-c", "import os; print(os.environ['REVIEW_AGENT_FLOW_RUN_ID'])"],
        timeout_seconds=5,
    ).run(AgentContext("42", 7, 11, "abc", "https://gitlab.example", tmp_path))
    assert result.stdout.strip() == "11"


def test_runner_exposes_side_effect_idempotency_context(tmp_path: Path) -> None:
    result = LocalCliAgentRunner(
        [
            sys.executable,
            "-c",
            (
                "import os; print(os.environ['REVIEW_AGENT_FLOW_NOTE_MARKER']); "
                "print(os.environ['REVIEW_AGENT_FLOW_AUTO_FIX_TARGET_BRANCH']); "
                "print(os.environ['REVIEW_AGENT_FLOW_AUTO_FIX_MARKER'])"
            ),
        ],
        timeout_seconds=5,
    ).run(
        AgentContext(
            "42",
            7,
            11,
            "abc",
            "https://gitlab.example",
            tmp_path,
            auto_fix_target_branch="feature/source",
            auto_fix_marker="ReviewFlow-Auto-Fix-Run-ID:11 SHA:abc",
            note_marker="ReviewFlow-Run-ID:11 SHA:abc",
        )
    )
    assert result.stdout.splitlines() == [
        "ReviewFlow-Run-ID:11 SHA:abc",
        "feature/source",
        "ReviewFlow-Auto-Fix-Run-ID:11 SHA:abc",
    ]
