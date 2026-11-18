"""Generic local Agent CLI execution."""

from __future__ import annotations

import json
import os
import signal
import stat
import subprocess
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from .agent_task import AgentTaskV1
from .backend_adapter import COMMAND_ADAPTER, build_backend_invocation
from .security import redact

MAX_AGENT_RESULT_BYTES = 1024 * 1024
MAX_AGENT_DIAGNOSTIC_CHARS = 64 * 1024
MAX_AGENT_DIAGNOSTIC_BYTES = MAX_AGENT_DIAGNOSTIC_CHARS * 4


class AgentContractError(ValueError):
    """Raised when a successful Agent does not produce the strict result contract."""


@dataclass(frozen=True)
class AutoFixProposal:
    summary: str


@dataclass(frozen=True)
class AgentOutput:
    version: int
    review_markdown: str
    autofix_proposal: AutoFixProposal | None = None


class _DiagnosticCapture:
    """Drain one process pipe while retaining only a safe, bounded tail."""

    def __init__(self, secrets: tuple[str, ...]) -> None:
        self._secrets = secrets
        self._tail = bytearray()
        self._truncated = False

    def consume(self, stream: BinaryIO) -> None:
        try:
            while chunk := stream.read(64 * 1024):
                self._tail.extend(chunk)
                overflow = len(self._tail) - MAX_AGENT_DIAGNOSTIC_BYTES
                if overflow > 0:
                    del self._tail[:overflow]
                    self._truncated = True
        except OSError:
            pass
        finally:
            stream.close()

    def render(self) -> str:
        text = bytes(self._tail).decode("utf-8", errors="replace")
        prefix = ""
        if self._truncated:
            prefix = "...[truncated]\n"
            boundaries = [offset for offset in (text.find("\n"), text.find("\r")) if offset >= 0]
            if not boundaries:
                return f"{prefix}[oversized diagnostic line omitted]"
            text = text[min(boundaries) + 1 :]
        redacted = redact(
            text,
            max_length=MAX_AGENT_DIAGNOSTIC_BYTES * 16,
            secrets=self._secrets,
        )
        available = MAX_AGENT_DIAGNOSTIC_CHARS - len(prefix)
        return prefix + redacted[-available:]


@dataclass(frozen=True)
class AgentContext:
    project_id: str
    mr_iid: int
    run_id: int
    sha: str
    worktree: Path
    result_path: Path
    skill_path: Path | None = None
    auto_fix_enabled: bool = False
    task: AgentTaskV1 | None = None


@dataclass(frozen=True)
class AgentResult:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False
    output: AgentOutput | None = None


def _load_agent_output(path: Path) -> AgentOutput:
    try:
        metadata = path.lstat()
    except FileNotFoundError as exc:
        raise AgentContractError("Agent result file is missing") from exc
    if not stat.S_ISREG(metadata.st_mode) or path.is_symlink():
        raise AgentContractError("Agent result must be a regular file, not a symlink")
    if metadata.st_size <= 0 or metadata.st_size > MAX_AGENT_RESULT_BYTES:
        raise AgentContractError("Agent result size is outside the allowed bounds")

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise AgentContractError("Agent result must be a readable regular file") from exc
    try:
        opened_metadata = os.fstat(descriptor)
        if not stat.S_ISREG(opened_metadata.st_mode):
            raise AgentContractError("Agent result must be a regular file, not a symlink")
        chunks: list[bytes] = []
        remaining = MAX_AGENT_RESULT_BYTES + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
    finally:
        os.close(descriptor)
    if not payload or len(payload) > MAX_AGENT_RESULT_BYTES:
        raise AgentContractError("Agent result size is outside the allowed bounds")

    def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, item in pairs:
            if key in result:
                raise AgentContractError(f"Agent result contains duplicate field: {key}")
            result[key] = item
        return result

    try:
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=unique_object,
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AgentContractError("Agent result must be valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise AgentContractError("Agent result root must be an object")
    allowed = {"version", "review_markdown", "autofix_proposal"}
    if set(value) - allowed:
        raise AgentContractError("Agent result contains unknown fields")
    if type(value.get("version")) is not int or value["version"] != 1:
        raise AgentContractError("Agent result version must equal 1")
    markdown = value.get("review_markdown")
    if not isinstance(markdown, str) or not markdown.strip():
        raise AgentContractError("review_markdown must be a non-empty string")
    proposal_value = value.get("autofix_proposal")
    proposal = None
    if "autofix_proposal" in value:
        if not isinstance(proposal_value, dict) or set(proposal_value) != {"summary"}:
            raise AgentContractError("autofix_proposal must contain only summary")
        summary = proposal_value.get("summary")
        if not isinstance(summary, str) or not summary.strip():
            raise AgentContractError("autofix_proposal.summary must be non-empty")
        proposal = AutoFixProposal(summary)
    return AgentOutput(1, markdown, proposal)


class LeaseLostError(RuntimeError):
    """Raised when the worker can no longer renew the Review Run lease."""


def _terminate_process(process: subprocess.Popen[bytes]) -> None:
    """Stop the Agent and wait for its process group to release inherited credentials."""

    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            if process.poll() is None:
                process.kill()
    elif process.poll() is None:
        process.kill()
    process.wait()


def _finish_diagnostics(
    threads: tuple[threading.Thread, threading.Thread],
    captures: tuple[_DiagnosticCapture, _DiagnosticCapture],
) -> tuple[str, str]:
    for thread in threads:
        thread.join()
    return captures[0].render(), captures[1].render()


class LocalCliAgentRunner:
    """Run one adapted Agent command with shared lifecycle and Result parsing."""

    def __init__(
        self,
        command: Sequence[str],
        timeout_seconds: int = 1800,
        *,
        adapter: str | None = None,
        model: str | None = None,
    ) -> None:
        if not command:
            raise ValueError("Agent command cannot be empty")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.command = tuple(command)
        self.timeout_seconds = timeout_seconds
        self.adapter = adapter
        self.model = model

    def run(
        self,
        context: AgentContext,
        extra_env: Mapping[str, str] | None = None,
        heartbeat: Callable[[], bool] | None = None,
        heartbeat_interval_seconds: float = 30.0,
    ) -> AgentResult:
        if heartbeat_interval_seconds <= 0:
            raise ValueError("heartbeat_interval_seconds must be positive")
        env = dict(extra_env or {})
        command = self.command
        if self.adapter is not None:
            if context.task is None:
                raise ValueError("adapted Agent execution requires AgentTaskV1")
            snapshot = context.task.snapshot
            if (
                snapshot.project_id != context.project_id
                or snapshot.mr_iid != context.mr_iid
                or snapshot.run_id != context.run_id
                or snapshot.head_sha != context.sha
                or context.task.result_path != context.result_path
                or context.task.skill_path != context.skill_path
                or context.task.auto_fix_enabled != context.auto_fix_enabled
            ):
                raise ValueError("Agent context does not match its frozen AgentTaskV1")
            task_path = None
            if self.adapter == COMMAND_ADAPTER:
                task_path = context.result_path.with_suffix(".task.md")
                descriptor = os.open(
                    task_path,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                )
                try:
                    payload = context.task.render_prompt().encode("utf-8")
                    with os.fdopen(descriptor, "wb", closefd=True) as stream:
                        stream.write(payload)
                        stream.flush()
                        os.fsync(stream.fileno())
                except Exception:
                    task_path.unlink(missing_ok=True)
                    raise
            invocation = build_backend_invocation(
                self.adapter,
                self.command,
                context.task,
                task_path,
                self.model,
            )
            command = invocation.command
            env.update(invocation.environment)
        env.update(
            {
                "REVIEW_AGENT_FLOW_PROJECT_ID": context.project_id,
                "REVIEW_AGENT_FLOW_MR_IID": str(context.mr_iid),
                "REVIEW_AGENT_FLOW_RUN_ID": str(context.run_id),
                "REVIEW_AGENT_FLOW_SHA": context.sha,
                "REVIEW_AGENT_FLOW_AUTO_FIX_ENABLED": str(context.auto_fix_enabled).lower(),
                "REVIEW_AGENT_FLOW_RESULT_PATH": str(context.result_path),
            }
        )
        if context.skill_path is not None:
            env["REVIEW_AGENT_FLOW_SKILL"] = str(context.skill_path)
        redaction_secrets = tuple(
            value for name, value in (extra_env or {}).items() if name != "PATH"
        )

        process = subprocess.Popen(
            list(command),
            cwd=context.worktree,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=os.name == "posix",
        )
        assert process.stdout is not None
        assert process.stderr is not None
        captures = (_DiagnosticCapture(redaction_secrets), _DiagnosticCapture(redaction_secrets))
        threads = (
            threading.Thread(target=captures[0].consume, args=(process.stdout,), daemon=True),
            threading.Thread(target=captures[1].consume, args=(process.stderr,), daemon=True),
        )
        for thread in threads:
            thread.start()
        deadline = time.monotonic() + self.timeout_seconds
        interval = min(heartbeat_interval_seconds, float(self.timeout_seconds))
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _terminate_process(process)
                stdout, stderr = _finish_diagnostics(threads, captures)
                return AgentResult(
                    returncode=124,
                    stdout=stdout,
                    stderr=stderr,
                    timed_out=True,
                )
            try:
                process.wait(timeout=min(interval, remaining))
                stdout, stderr = _finish_diagnostics(threads, captures)
                output = (
                    _load_agent_output(context.result_path) if process.returncode == 0 else None
                )
                return AgentResult(
                    process.returncode or 0,
                    stdout,
                    stderr,
                    output=output,
                )
            except subprocess.TimeoutExpired:
                if heartbeat is not None:
                    try:
                        heartbeat_ok = heartbeat()
                    except Exception as exc:
                        _terminate_process(process)
                        _finish_diagnostics(threads, captures)
                        raise LeaseLostError(
                            "Review Run lease heartbeat failed during Agent execution"
                        ) from exc
                    if not heartbeat_ok:
                        _terminate_process(process)
                        _finish_diagnostics(threads, captures)
                        raise LeaseLostError(
                            "Review Run lease was lost during Agent execution"
                        ) from None
