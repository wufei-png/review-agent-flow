"""Review Run worker orchestration."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from .gitlab import GitLabClient
from .notifications import FanoutNotifier, NotificationEvent, NotificationResult
from .policy import ReviewPolicy
from .repository import GitShaEnsurer
from .runner import AgentContext, AgentResult, LocalCliAgentRunner
from .store import (
    DEFAULT_LEASE_SECONDS,
    DEFAULT_MAX_ATTEMPTS,
    DEFAULT_NOTIFICATION_LEASE_SECONDS,
    DEFAULT_NOTIFICATION_MAX_ATTEMPTS,
    DEFAULT_WORKTREE_CLEANUP_LEASE_SECONDS,
    DEFAULT_WORKTREE_CLEANUP_MAX_ATTEMPTS,
    ReviewRun,
    SQLiteStore,
    auto_fix_intent_marker,
)
from .worktree import GitWorktreeManager, WorktreeError

DEFAULT_REVIEW_AGENT_SKILL = (
    Path(__file__).resolve().parent / "defaults" / "review-agent" / "SKILL.md"
)
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
        project_id: str | None = None,
        sha_ensurer: GitShaEnsurer | None = None,
        worktree_cleanup_lease_seconds: int = DEFAULT_WORKTREE_CLEANUP_LEASE_SECONDS,
        worktree_cleanup_max_attempts: int = DEFAULT_WORKTREE_CLEANUP_MAX_ATTEMPTS,
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
        if not project_id or not project_id.strip():
            raise ValueError("project_id is required for a ReviewWorker")
        self.project_id = project_id
        self.sha_ensurer = sha_ensurer or GitShaEnsurer(repository)
        self.worktree_cleanup_lease_seconds = worktree_cleanup_lease_seconds
        self.worktree_cleanup_max_attempts = worktree_cleanup_max_attempts

    def process_one(self) -> WorkerResult | None:
        run = self.store.claim_next(
            lease_seconds=self.lease_seconds,
            max_attempts=self.max_attempts,
        )
        if run is None:
            return None

        auto_fix_intent = None
        agent_started = False
        try:
            if run.project_id != self.project_id:
                raise ValueError("Review Run belongs to a different configured Project")
            merge_request = self.gitlab.get_merge_request(run.project_id, run.mr_iid)
            if merge_request.draft:
                self.store.requeue(
                    run.run_id,
                    "draft merge request",
                    delay_seconds=self.retry_delay_seconds,
                    claim_token=run.claim_token,
                )
                return WorkerResult(run.run_id, "deferred", detail="draft merge request")

            auto_fix_match = self.store.resolve_auto_fix_match(
                run.project_id,
                run.mr_iid,
                merge_request.source_branch,
                merge_request.target_branch,
                merge_request.description,
            )
            if run.kind == "autofix" or auto_fix_match is not None:
                if auto_fix_match is None:
                    raise ValueError("untrusted Auto-fix Run has no persisted relation")
                parent_merge_request = self.gitlab.get_merge_request(
                    auto_fix_match.parent_project_id,
                    auto_fix_match.parent_mr_iid,
                )
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

            self.sha_ensurer.ensure_sha(run.sha)
            if self.policy.ai.auto_fix:
                auto_fix_intent = self.store.create_auto_fix_intent(
                    parent_project_id=run.project_id,
                    parent_mr_iid=run.mr_iid,
                    parent_sha=run.sha,
                    run_id=run.run_id,
                    source_branch=f"autofix/MR-{merge_request.iid}/run-{run.run_id}",
                    target_branch=merge_request.source_branch,
                    title=f"[ReviewFlow Auto-Fix] {merge_request.title}",
                )

            worktree_id = str(run.run_id) if run.attempts <= 1 else f"{run.run_id}-attempt-{run.attempts}"
            worktree = self.worktrees.path_for(worktree_id)
            if worktree.exists():
                raise WorktreeError(f"worktree path already exists: {worktree}")
            cleanup_record = self.store.register_worktree(run.run_id, worktree, run.sha)
            agent_result: AgentResult | None = None
            run_status = "succeeded"
            detail = ""
            auto_fix_created = False
            auto_fix_reconciliation_complete = not self.policy.ai.auto_fix
            worktree_created = False
            retry_reason: str | None = None
            try:
                try:
                    self.worktrees.create(run.sha, worktree_id)
                except Exception as exc:
                    self.store.mark_worktree_not_created(cleanup_record.cleanup_id, str(exc))
                    raise
                worktree_created = True
                if self.policy.ai.auto_fix:
                    try:
                        auto_fix_created = self._record_auto_fix_if_created(run, merge_request)
                    except Exception as exc:  # noqa: BLE001 - external GitLab lookup is retryable
                        retry_reason = f"Auto-fix reconciliation lookup failed: {exc}"
                if retry_reason is None:
                    marker = f"ReviewFlow-Run-ID:{run.run_id} SHA:{run.sha}"
                    has_marker = False
                    has_note_marker = getattr(self.gitlab, "has_note_marker", None)
                    if callable(has_note_marker):
                        has_marker = bool(
                            has_note_marker(run.project_id, run.mr_iid, marker)
                        )
                    if has_marker and (not self.policy.ai.auto_fix or auto_fix_created):
                        agent_result = AgentResult(
                            returncode=0,
                            stdout="",
                            stderr=(
                                "review marker and Auto-fix MR already exist; "
                                "skipped duplicate Agent run"
                                if auto_fix_created
                                else "review marker already exists; skipped duplicate Agent run"
                            ),
                        )
                    else:
                        agent_started = True
                        agent_result = self._run_agent(
                            run,
                            merge_request,
                            worktree,
                            auto_fix_intent,
                            marker,
                        )
                    if agent_result.returncode != 0:
                        run_status = "failed"
                        detail = agent_result.stderr[-4000:] or "Agent failed"
                    if self.policy.ai.auto_fix and agent_started:
                        try:
                            auto_fix_created = (
                                self._record_auto_fix_if_created(run, merge_request)
                                or auto_fix_created
                            )
                        except Exception as exc:  # noqa: BLE001 - external GitLab lookup is retryable
                            retry_reason = f"Auto-fix reconciliation lookup failed: {exc}"
                    if retry_reason is None:
                        auto_fix_reconciliation_complete = True
                if retry_reason is not None:
                    run_status = "deferred"
                    detail = retry_reason
            except Exception as exc:
                run_status = "failed"
                detail = str(exc)
                LOGGER.exception("Agent side-effect reconciliation failed for Run %s", run.run_id)
            finally:
                if worktree_created:
                    try:
                        self.worktrees.remove(worktree)
                        self.store.mark_worktree_cleaned(cleanup_record.cleanup_id)
                    except Exception as exc:
                        cleanup_detail = str(exc)
                        self.store.mark_worktree_cleanup_pending(
                            cleanup_record.cleanup_id, cleanup_detail
                        )
                        detail = f"{detail}; worktree cleanup pending: {cleanup_detail}".strip("; ")
                        LOGGER.exception("worktree cleanup failed for Run %s", run.run_id)

            if retry_reason is not None:
                self.store.requeue(
                    run.run_id,
                    retry_reason,
                    delay_seconds=self.retry_delay_seconds,
                    claim_token=run.claim_token,
                )
                return WorkerResult(run.run_id, "deferred", agent_result, detail)

            if (
                auto_fix_intent is not None
                and (
                    (not agent_started and run_status == "failed")
                    or (auto_fix_reconciliation_complete and not auto_fix_created)
                )
            ):
                self.store.abandon_auto_fix_intent(run.run_id)

            self.store.finish(
                run.run_id,
                run_status,
                detail if run_status == "failed" else None,
                claim_token=run.claim_token,
            )
            return WorkerResult(run.run_id, run_status, agent_result, detail)
        except Exception as exc:  # noqa: BLE001 - a failed Run must be persisted
            if auto_fix_intent is not None and not agent_started:
                try:
                    self.store.abandon_auto_fix_intent(run.run_id)
                except Exception:
                    LOGGER.exception("failed to abandon unused Auto-fix intent for Run %s", run.run_id)
            self.store.finish(run.run_id, "failed", str(exc), claim_token=run.claim_token)
            return WorkerResult(run.run_id, "failed", detail=str(exc))

    def run_forever(self, poll_interval_seconds: float = 5.0) -> None:
        while True:
            result = self.process_one()
            self.process_worktree_cleanup(limit=1)
            self.process_notifications(limit=1)
            if result is None or result.status == "deferred":
                time.sleep(poll_interval_seconds)

    def process_worktree_cleanup(self, limit: int = 1) -> int:
        if limit <= 0:
            raise ValueError("worktree cleanup limit must be positive")
        processed = 0
        for _ in range(limit):
            record = self.store.claim_next_worktree_cleanup(
                lease_seconds=self.worktree_cleanup_lease_seconds,
                max_attempts=self.worktree_cleanup_max_attempts,
            )
            if record is None:
                break
            try:
                self.worktrees.remove(Path(record.path))
            except Exception as exc:  # noqa: BLE001 - isolate one cleanup record
                cleaned = False
                detail = str(exc)
            else:
                cleaned = True
                detail = None
            self.store.finish_worktree_cleanup(
                record.cleanup_id,
                cleaned,
                detail,
                claim_token=record.claim_token,
                max_attempts=self.worktree_cleanup_max_attempts,
                retry_delay_seconds=self.retry_delay_seconds,
            )
            processed += 1
        return processed

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
                result = self.notifier.notify_channel(event, delivery.channel_id)
            except Exception as exc:  # noqa: BLE001 - isolate one channel failure
                result = NotificationResult(delivery.channel_id, False, str(exc))
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

    def _run_agent(
        self,
        run: ReviewRun,
        merge_request,
        worktree: Path,
        auto_fix_intent,
        marker: str,
    ) -> AgentResult:
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
                auto_fix_branch=auto_fix_intent.source_branch if auto_fix_intent else None,
                auto_fix_target_branch=auto_fix_intent.target_branch if auto_fix_intent else None,
                auto_fix_title=auto_fix_intent.title if auto_fix_intent else None,
                auto_fix_marker=(
                    auto_fix_intent_marker(run.run_id, run.sha) if auto_fix_intent else None
                ),
                note_marker=marker,
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

    def _record_auto_fix_if_created(self, run: ReviewRun, merge_request) -> bool:
        if not self.policy.ai.auto_fix:
            return False
        branch = f"autofix/MR-{merge_request.iid}/run-{run.run_id}"
        marker = auto_fix_intent_marker(run.run_id, run.sha)
        auto_fix = self.gitlab.find_merge_request_by_source_branch(
            run.project_id, branch, merge_request.source_branch, marker
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
            return True
        return False
