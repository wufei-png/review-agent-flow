"""Review Run worker orchestration."""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from .events import parse_auto_fix_branch
from .gitlab import GitLabClient
from .notifications import FanoutNotifier, NotificationEvent
from .policy import ReviewPolicy
from .runner import AgentContext, AgentResult, LocalCliAgentRunner
from .store import ReviewRun, SQLiteStore
from .worktree import GitWorktreeManager


@dataclass(frozen=True)
class WorkerResult:
    run_id: int
    status: str
    agent_result: AgentResult | None = None
    detail: str = ""


class ReviewWorker:
    def __init__(
        self,
        store: SQLiteStore,
        gitlab: GitLabClient,
        worktrees: GitWorktreeManager,
        policy: ReviewPolicy,
        repository: Path,
        gitlab_url: str,
        gitlab_token: str | None = None,
        notifier: FanoutNotifier | None = None,
        agent_timeout_seconds: int = 1800,
    ) -> None:
        self.store = store
        self.gitlab = gitlab
        self.worktrees = worktrees
        self.policy = policy
        self.repository = repository
        self.gitlab_url = gitlab_url
        self.gitlab_token = gitlab_token
        self.notifier = notifier or FanoutNotifier()
        self.agent_timeout_seconds = agent_timeout_seconds

    def process_one(self) -> WorkerResult | None:
        run = self.store.claim_next()
        if run is None:
            return None

        try:
            merge_request = self.gitlab.get_merge_request(run.project_id, run.mr_iid)
            if run.kind == "autofix":
                parent = parse_auto_fix_branch(merge_request.source_branch)
                if parent is None:
                    raise ValueError("auto-fix MR source branch does not identify its parent MR")
                parent_merge_request = self.gitlab.get_merge_request(run.project_id, parent[0])
                self._notify_autofix(parent_merge_request, merge_request)
                self.store.finish(run.run_id, "succeeded")
                return WorkerResult(run.run_id, "succeeded", detail="auto-fix MR notification")

            if self.policy.pipeline_gate:
                pipeline = self.gitlab.get_latest_pipeline(run.project_id, run.sha)
                if pipeline is None or pipeline.status != "success":
                    self.store.requeue(run.run_id, "waiting for successful pipeline")
                    return WorkerResult(run.run_id, "deferred", detail="pipeline gate")

            self._notify_humans(merge_request)
            if not self.policy.ai.enabled or self.store.is_suppressed_parent_sha(
                run.project_id, run.mr_iid, run.sha
            ):
                self.store.finish(run.run_id, "succeeded")
                return WorkerResult(run.run_id, "succeeded", detail="human path only")

            worktree = self.worktrees.create(run.sha, str(run.run_id))
            try:
                result = self._run_agent(run, merge_request, worktree)
                if result.returncode != 0:
                    self.store.finish(run.run_id, "failed", result.stderr[-4000:] or "Agent failed")
                    return WorkerResult(run.run_id, "failed", result, "Agent process failed")
                self._record_auto_fix_if_created(run, merge_request)
                self.store.finish(run.run_id, "succeeded")
                return WorkerResult(run.run_id, "succeeded", result)
            finally:
                self.worktrees.remove(worktree)
        except Exception as exc:  # noqa: BLE001 - a failed Run must be persisted
            self.store.finish(run.run_id, "failed", str(exc))
            return WorkerResult(run.run_id, "failed", detail=str(exc))

    def run_forever(self, poll_interval_seconds: float = 5.0) -> None:
        while True:
            result = self.process_one()
            if result is None or result.status == "deferred":
                time.sleep(poll_interval_seconds)

    def _run_agent(self, run: ReviewRun, merge_request, worktree: Path) -> AgentResult:
        command = self.policy.ai.command
        runner = LocalCliAgentRunner(command, self.agent_timeout_seconds)
        skill_path = None
        if self.policy.ai.skill_path:
            configured = Path(self.policy.ai.skill_path)
            skill_path = configured if configured.is_absolute() else worktree / configured
        return runner.run(
            AgentContext(
                project_id=run.project_id,
                mr_iid=run.mr_iid,
                sha=run.sha,
                gitlab_url=self.gitlab_url,
                worktree=worktree,
                skill_path=skill_path,
                gitlab_token=self.gitlab_token,
                auto_fix_enabled=self.policy.ai.auto_fix,
            )
        )

    def _notify_humans(self, merge_request) -> None:
        if not self.policy.human.enabled:
            return
        self.notifier.notify(
            NotificationEvent(
                title=f"Review requested: !{merge_request.iid} {merge_request.title}",
                body="A human review is requested.",
                url=merge_request.web_url,
                recipients=self.policy.human.reviewers,
            )
        )

    def _notify_autofix(self, parent_merge_request, auto_fix_merge_request) -> None:
        self.notifier.notify(
            NotificationEvent(
                title=f"Auto-fix review: !{auto_fix_merge_request.iid} {auto_fix_merge_request.title}",
                body="An Auto-fix Merge Request is ready for the original MR creator to review.",
                url=auto_fix_merge_request.web_url,
                recipients=(parent_merge_request.author_username,),
            )
        )

    def _record_auto_fix_if_created(self, run: ReviewRun, merge_request) -> None:
        branch = f"autofix/MR-{merge_request.iid}/run-{run.run_id}"
        auto_fix = self.gitlab.find_merge_request_by_source_branch(
            run.project_id, branch, merge_request.source_branch
        )
        if auto_fix is not None:
            self.store.record_auto_fix(
                parent_project_id=run.project_id,
                parent_mr_iid=run.mr_iid,
                parent_sha=run.sha,
                run_id=run.run_id,
                auto_fix_project_id=auto_fix.project_id,
                auto_fix_mr_iid=auto_fix.iid,
            )
