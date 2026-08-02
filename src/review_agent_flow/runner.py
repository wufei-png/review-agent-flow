"""Generic local Agent CLI execution."""

from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from .security import redact


@dataclass(frozen=True)
class AgentContext:
    project_id: str
    mr_iid: int
    run_id: int
    sha: str
    gitlab_url: str
    worktree: Path
    skill_path: Path | None = None
    gitlab_token: str | None = None
    auto_fix_enabled: bool = False


@dataclass(frozen=True)
class AgentResult:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False


class LeaseLostError(RuntimeError):
    """Raised when the worker can no longer renew the Review Run lease."""


class LocalCliAgentRunner:
    """Run a configured Agent command without requiring a provider-specific adapter."""

    def __init__(self, command: Sequence[str], timeout_seconds: int = 1800) -> None:
        if not command:
            raise ValueError("Agent command cannot be empty")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.command = tuple(command)
        self.timeout_seconds = timeout_seconds

    def run(
        self,
        context: AgentContext,
        extra_env: Mapping[str, str] | None = None,
        heartbeat: Callable[[], bool] | None = None,
        heartbeat_interval_seconds: float = 30.0,
    ) -> AgentResult:
        if heartbeat_interval_seconds <= 0:
            raise ValueError("heartbeat_interval_seconds must be positive")
        env = os.environ.copy()
        env.update(
            {
                "REVIEW_AGENT_FLOW_PROJECT_ID": context.project_id,
                "REVIEW_AGENT_FLOW_MR_IID": str(context.mr_iid),
                "REVIEW_AGENT_FLOW_RUN_ID": str(context.run_id),
                "REVIEW_AGENT_FLOW_SHA": context.sha,
                "REVIEW_AGENT_FLOW_GITLAB_URL": context.gitlab_url,
                "REVIEW_AGENT_FLOW_AUTO_FIX_ENABLED": str(context.auto_fix_enabled).lower(),
            }
        )
        if context.skill_path is not None:
            env["REVIEW_AGENT_FLOW_SKILL"] = str(context.skill_path)
        if context.gitlab_token is not None:
            env["GITLAB_TOKEN"] = context.gitlab_token
        if extra_env:
            env.update(extra_env)
        redaction_secrets = (context.gitlab_token,) if context.gitlab_token else ()

        process = subprocess.Popen(
            list(self.command),
            cwd=context.worktree,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        deadline = time.monotonic() + self.timeout_seconds
        interval = min(heartbeat_interval_seconds, float(self.timeout_seconds))
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                process.kill()
                stdout, stderr = process.communicate()
                return AgentResult(
                    returncode=124,
                    stdout=redact(stdout or "", secrets=redaction_secrets),
                    stderr=redact(stderr or "", secrets=redaction_secrets),
                    timed_out=True,
                )
            try:
                stdout, stderr = process.communicate(timeout=min(interval, remaining))
                return AgentResult(
                    process.returncode or 0,
                    redact(stdout or "", secrets=redaction_secrets),
                    redact(stderr or "", secrets=redaction_secrets),
                )
            except subprocess.TimeoutExpired:
                if heartbeat is not None and not heartbeat():
                    process.kill()
                    process.communicate()
                    raise LeaseLostError("Review Run lease was lost during Agent execution")
