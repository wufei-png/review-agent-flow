"""Generic local Agent CLI execution."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AgentContext:
    project_id: str
    mr_iid: int
    sha: str
    gitlab_url: str
    worktree: Path
    skill_path: Path | None = None
    gitlab_token: str | None = None


@dataclass(frozen=True)
class AgentResult:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False


class LocalCliAgentRunner:
    """Run a configured Agent command without requiring a provider-specific adapter."""

    def __init__(self, command: Sequence[str], timeout_seconds: int = 1800) -> None:
        if not command:
            raise ValueError("Agent command cannot be empty")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.command = tuple(command)
        self.timeout_seconds = timeout_seconds

    def run(self, context: AgentContext, extra_env: Mapping[str, str] | None = None) -> AgentResult:
        env = os.environ.copy()
        env.update(
            {
                "REVIEW_AGENT_FLOW_PROJECT_ID": context.project_id,
                "REVIEW_AGENT_FLOW_MR_IID": str(context.mr_iid),
                "REVIEW_AGENT_FLOW_SHA": context.sha,
                "REVIEW_AGENT_FLOW_GITLAB_URL": context.gitlab_url,
            }
        )
        if context.skill_path is not None:
            env["REVIEW_AGENT_FLOW_SKILL"] = str(context.skill_path)
        if context.gitlab_token is not None:
            env["GITLAB_TOKEN"] = context.gitlab_token
        if extra_env:
            env.update(extra_env)

        try:
            completed = subprocess.run(
                list(self.command),
                cwd=context.worktree,
                env=env,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout or ""
            stderr = exc.stderr or ""
            if isinstance(stdout, bytes):
                stdout = stdout.decode(errors="replace")
            if isinstance(stderr, bytes):
                stderr = stderr.decode(errors="replace")
            return AgentResult(returncode=124, stdout=stdout, stderr=stderr, timed_out=True)
        return AgentResult(completed.returncode, completed.stdout, completed.stderr)
