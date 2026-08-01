"""Review Run worker orchestration."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from .events import parse_auto_fix_branch
from .gitlab import GitLabClient
from .notifications import FanoutNotifier, NotificationEvent, NotificationResult
from .policy import ReviewPolicy
from .runner import AgentContext, AgentResult, LocalCliAgentRunner
from .store import (
    DEFAULT_LEASE_SECONDS,
    DEFAULT_MAX_ATTEMPTS,
    DEFAULT_NOTIFICATION_LEASE_SECONDS,
    DEFAULT_NOTIFICATION_MAX_ATTEMPTS,
    ReviewRun,
    SQLiteStore,
)
from .worktree import GitWorktreeManager

DEFAULT_REVIEW_AGENT_SKILL = Path(__file__).resolve().parent / "defaults" / "review-agent" / "SKILL.md"
LOGGER = logging.getLogger(__name__)


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
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        retry_delay_seconds: float = 5.0,
        notification_lease_seconds: int = DEFAULT_NOTIFICATION_LEASE_SECONDS,
        notification_max_attempts: int = DEFAULT_NOTIFICATION_MAX_ATTEMPTS,
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
        self.lease_seconds = lease_seconds
        self.max_attempts = max_attempts
        self.retry_delay_seconds = retry_delay_seconds
        self.notification_lease_seconds = notification_lease_seconds
        self.notification_max_attempts = notification_max_attempts

    def process_one(self) -> WorkerResult | None:
        run = self.store.claim_next(
            lease_seconds=self.lease_seconds,
            max_attempts=self.max_attempts,
        )
        if run is None:
            return None

        try:
            merge_request = self.gitlab.get_merge_request(run.project_id, run.mr_iid)
            if merge_request.draft:
                self.store.requeue(
                    run.run_id,
                    "draft merge request",
                    delay_seconds=self.retry_delay_seconds,
                    claim_token=run.claim_token,
                )
                return WorkerResult(run.run_id, "deferred", detail="draft merge request")
            if run.kind == "autofix":
                parent = parse_auto_fix_branch(merge_request.source_branch)
                if parent is None:
                    raise ValueError("auto-fix MR source branch does not identify its parent MR")
                parent_merge_request = self.gitlab.get_merge_request(run.project_id, parent[0])
                self._notify_autofix(run, parent_merge_request, merge_request)
                self.store.finish(run.run_id, "succeeded", claim_token=run.claim_token)
                return WorkerResult(run.run_id, "succeeded", detail="auto-fix MR notification")

            if self.policy.pipeline_gate:
                pipeline = self.gitlab.get_latest_pipeline(run.project_id, run.sha)
                if pipeline is None or pipeline.status != "success":
                    self.store.requeue(
                        run.run_id,
                        "waiting for successful pipeline",
                        delay_seconds=self.retry_delay_seconds,
                        claim_token=run.claim_token,
                    )
                    return WorkerResult(run.run_id, "deferred", detail="pipeline gate")

            self._notify_humans(run, merge_request)
            if not self.policy.ai.enabled or self.store.is_suppressed_parent_sha(
                run.project_id, run.mr_iid, run.sha
            ):
                self.store.finish(run.run_id, "succeeded", claim_token=run.claim_token)
                return WorkerResult(run.run_id, "succeeded", detail="human path only")

            worktree_id = str(run.run_id) if run.attempts <= 1 else f"{run.run_id}-attempt-{run.attempts}"
            worktree = self.worktrees.create(run.sha, worktree_id)
            try:
                result = self._run_agent(run, merge_request, worktree)
                if result.returncode != 0:
                    self.store.finish(
                        run.run_id,
                        "failed",
                        result.stderr[-4000:] or "Agent failed",
                        claim_token=run.claim_token,
                    )
                    return WorkerResult(run.run_id, "failed", result, "Agent process failed")
                self._record_auto_fix_if_created(run, merge_request)
                self.store.finish(run.run_id, "succeeded", claim_token=run.claim_token)
                return WorkerResult(run.run_id, "succeeded", result)
            finally:
                self.worktrees.remove(worktree)
        except Exception as exc:  # noqa: BLE001 - a failed Run must be persisted
            self.store.finish(run.run_id, "failed", str(exc), claim_token=run.claim_token)
            return WorkerResult(run.run_id, "failed", detail=str(exc))

    def run_forever(self, poll_interval_seconds: float = 5.0) -> None:
        while True:
            result = self.process_one()
            self.process_notifications(limit=1)
            if result is None or result.status == "deferred":
                time.sleep(poll_interval_seconds)

    def process_notifications(
        self, limit: int = 1, delivery_ids: tuple[int, ...] | None = None
    ) -> int:
        """Deliver due outbox records without changing Review Run status."""

        if limit <= 0:
            raise ValueError("notification limit must be positive")
        processed = 0
        claim_ids = delivery_ids if delivery_ids is not None else (None,) * limit
        for delivery_id in claim_ids:
            try:
                if delivery_id is None:
                    delivery = self.store.claim_next_notification(
                        lease_seconds=self.notification_lease_seconds,
                        max_attempts=self.notification_max_attempts,
                    )
                else:
                    delivery = self.store.claim_notification(
                        delivery_id,
                        lease_seconds=self.notification_lease_seconds,
                        max_attempts=self.notification_max_attempts,
                    )
            except Exception:
                LOGGER.exception("failed to claim notification outbox record")
                break
            if delivery is None:
                break
            event = NotificationEvent(
                title=delivery.title,
                body=delivery.body,
                url=delivery.url,
                recipients=delivery.recipients,
                correlation_id=delivery.correlation_id,
            )
            try:
                result = self.notifier.notify_channel(event, delivery.channel_index)
            except Exception as exc:  # noqa: BLE001 - isolate one channel failure
                result = NotificationResult(delivery.channel, False, str(exc))
            try:
                self.store.finish_notification(
                    delivery.delivery_id,
                    result.delivered,
                    result.detail,
                    claim_token=delivery.claim_token,
                    max_attempts=self.notification_max_attempts,
                    retry_delay_seconds=self.retry_delay_seconds,
                )
            except Exception:
                LOGGER.exception(
                    "failed to update notification outbox record %s", delivery.delivery_id
                )
            processed += 1
        return processed

    def _run_agent(self, run: ReviewRun, merge_request, worktree: Path) -> AgentResult:
        command = self.policy.ai.command
        runner = LocalCliAgentRunner(command, self.agent_timeout_seconds)
        skill_path = DEFAULT_REVIEW_AGENT_SKILL
        if self.policy.ai.skill_path:
            configured = Path(self.policy.ai.skill_path)
            configured_path = configured if configured.is_absolute() else worktree / configured
            if configured_path.is_file():
                skill_path = configured_path
        return runner.run(
            AgentContext(
                project_id=run.project_id,
                mr_iid=run.mr_iid,
                run_id=run.run_id,
                sha=run.sha,
                gitlab_url=self.gitlab_url,
                worktree=worktree,
                skill_path=skill_path,
                gitlab_token=self.gitlab_token,
                auto_fix_enabled=self.policy.ai.auto_fix,
            )
        )

    def _notify_humans(self, run: ReviewRun, merge_request) -> None:
        if not self.policy.human.enabled:
            return
        self._queue_notification(
            NotificationEvent(
                title=f"Review requested: !{merge_request.iid} {merge_request.title}",
                body="A human review is requested.",
                url=merge_request.web_url,
                recipients=self.policy.human.reviewers,
                correlation_id=f"review-run:{run.run_id}:human",
            )
        )

    def _notify_autofix(self, run: ReviewRun, parent_merge_request, auto_fix_merge_request) -> None:
        self._queue_notification(
            NotificationEvent(
                title=f"Auto-fix review: !{auto_fix_merge_request.iid} {auto_fix_merge_request.title}",
                body="An Auto-fix Merge Request is ready for the original MR creator to review.",
                url=auto_fix_merge_request.web_url,
                recipients=(parent_merge_request.author_username,),
                correlation_id=f"review-run:{run.run_id}:autofix",
            )
        )

    def _queue_notification(self, event: NotificationEvent) -> None:
        channels = self.notifier.channel_specs()
        if not channels:
            return
        correlation_id = event.correlation_id or f"notification:{uuid4().hex}"
        try:
            deliveries = self.store.enqueue_notifications(
                correlation_id=correlation_id,
                title=event.title,
                body=event.body,
                url=event.url,
                recipients=event.recipients,
                channels=channels,
            )
            delivery_ids = tuple(
                delivery.delivery_id for delivery in deliveries if delivery.status == "pending"
            )
            self.process_notifications(delivery_ids=delivery_ids)
        except Exception:
            LOGGER.exception("failed to enqueue notification outbox record")

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
