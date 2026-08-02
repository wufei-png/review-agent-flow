"""Review Run worker orchestration."""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from .events import parse_auto_fix_branch
from .failures import FailureInfo, classify_exception
from .gitlab import GitLabClient, is_valid_sha
from .notifications import FanoutNotifier, NotificationEvent, NotificationResult
from .policy import ReviewPolicy
from .runner import AgentContext, AgentResult, LeaseLostError, LocalCliAgentRunner
from .security import redact
from .store import (
    DEFAULT_EFFECT_LEASE_SECONDS,
    DEFAULT_LEASE_SECONDS,
    DEFAULT_MAX_ATTEMPTS,
    DEFAULT_NOTIFICATION_LEASE_SECONDS,
    DEFAULT_NOTIFICATION_MAX_ATTEMPTS,
    EffectReceipt,
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
        project_id: str | None = None,
        max_retry_delay_seconds: float = 300.0,
    ) -> None:
        configured_project_id = (
            project_id if project_id is not None else os.getenv("REVIEW_AGENT_FLOW_PROJECT_ID")
        )
        if configured_project_id is None or not configured_project_id.strip():
            raise RuntimeError(
                "ReviewFlow startup configuration error: "
                "REVIEW_AGENT_FLOW_PROJECT_ID must be set"
            )
        self.store = store
        self.gitlab = gitlab
        self.worktrees = worktrees
        self.policy = policy
        self.repository = repository
        self.gitlab_url = gitlab_url
        self.gitlab_token = gitlab_token
        register_secret = getattr(self.store, "add_redaction_secret", None)
        if callable(register_secret):
            register_secret(gitlab_token)
        self.notifier = notifier or FanoutNotifier()
        self.agent_timeout_seconds = agent_timeout_seconds
        self.lease_seconds = lease_seconds
        self.max_attempts = max_attempts
        self.retry_delay_seconds = retry_delay_seconds
        self.notification_lease_seconds = notification_lease_seconds
        self.notification_max_attempts = notification_max_attempts
        if max_retry_delay_seconds < 0:
            raise ValueError("max_retry_delay_seconds cannot be negative")
        self.max_retry_delay_seconds = max_retry_delay_seconds
        self.project_id = configured_project_id.strip()

    def _redaction_secrets(self) -> tuple[str, ...]:
        return (self.gitlab_token,) if self.gitlab_token else ()

    def _redact(self, value: object) -> str:
        return redact(value, secrets=self._redaction_secrets())

    def process_one(self) -> WorkerResult | None:
        run = self.store.claim_next(
            lease_seconds=self.lease_seconds,
            max_attempts=self.max_attempts,
            project_id=self.project_id,
        )
        if run is None:
            return self.reconcile_auto_fixes(limit=1)

        try:
            merge_request = self.gitlab.get_merge_request(run.project_id, run.mr_iid)
            if merge_request.project_id != self.project_id:
                detail = "GitLab Merge Request belongs to a different project"
                self._finish_terminal(run, "cancelled", detail, "project_mismatch")
                return WorkerResult(run.run_id, "cancelled", detail=detail)

            state = str(merge_request.state or "opened").lower()
            if state in {"closed", "merged"}:
                detail = f"merge request is {state}"
                self._finish_terminal(run, "cancelled", detail, "closed_or_merged")
                return WorkerResult(run.run_id, "cancelled", detail=detail)

            if not is_valid_sha(merge_request.sha):
                detail = "GitLab returned an invalid current Merge Request head SHA"
                self._finish_terminal(run, "cancelled", detail, "invalid_sha")
                return WorkerResult(run.run_id, "cancelled", detail=detail)

            if merge_request.sha != run.sha:
                detail = f"stale run SHA; current Merge Request head is {merge_request.sha}"
                self.store.cancel_and_enqueue_latest(
                    run_id=run.run_id,
                    project_id=run.project_id,
                    mr_iid=run.mr_iid,
                    sha=merge_request.sha,
                    kind=run.kind,
                    reason=detail,
                    claim_token=run.claim_token,
                    failure_class="stale_sha",
                    retryable=False,
                )
                return WorkerResult(run.run_id, "cancelled", detail=detail)

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
                    detail = "auto-fix MR source branch does not identify its parent MR"
                    self._finish_terminal(run, "failed", detail, "policy_error")
                    return WorkerResult(run.run_id, "failed", detail=detail)
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
                receipts = self._prepare_agent_effects(run, merge_request)
                result = self._run_agent(run, merge_request, worktree)
                agent_receipt = receipts["agent_execution"]
                if result.timed_out:
                    detail = self._redact(result.stderr[-4000:] or "Agent timed out")
                    self.store.update_effect_receipt(
                        agent_receipt.receipt_id,
                        state="failed",
                        detail="Agent process timed out",
                        error=detail,
                    )
                    self._mark_unobserved_effects(receipts, detail)
                    self._finish_terminal(run, "failed", detail, "agent_timeout")
                    return WorkerResult(run.run_id, "failed", result, "Agent timed out")
                if result.returncode != 0:
                    detail = self._redact(result.stderr[-4000:] or "Agent failed")
                    self.store.update_effect_receipt(
                        agent_receipt.receipt_id,
                        state="failed",
                        detail=f"Agent exited with code {result.returncode}",
                        error=detail,
                    )
                    self._mark_unobserved_effects(receipts, detail)
                    self.store.finish(
                        run.run_id,
                        "failed",
                        detail,
                        claim_token=run.claim_token,
                        failure_class="agent_exit",
                        retryable=False,
                    )
                    return WorkerResult(run.run_id, "failed", result, "Agent process failed")
                self.store.update_effect_receipt(
                    agent_receipt.receipt_id,
                    state="succeeded",
                    detail="Agent process exited successfully",
                )
                self._record_auto_fix_if_created(run, merge_request)
                self.store.finish(run.run_id, "succeeded", claim_token=run.claim_token)
                return WorkerResult(run.run_id, "succeeded", result)
            finally:
                self.worktrees.remove(worktree)
        except LeaseLostError as exc:
            LOGGER.warning("Review Run %s lost its lease: %s", run.run_id, self._redact(exc))
            return WorkerResult(run.run_id, "deferred", detail="Review Run lease lost")
        except Exception as exc:  # noqa: BLE001 - a failed Run must be persisted
            failure = classify_exception(exc, secrets=self._redaction_secrets())
            if failure.retryable:
                requeued = self._retry_or_fail(run, failure)
                return WorkerResult(
                    run.run_id,
                    "deferred" if requeued else "failed",
                    detail=failure.detail,
                )
            self._finish_terminal(run, "failed", failure.detail, failure.failure_class)
            return WorkerResult(run.run_id, "failed", detail=failure.detail)

    def _finish_terminal(
        self,
        run: ReviewRun,
        status: str,
        detail: str,
        failure_class: str,
        retryable: bool = False,
    ) -> bool:
        return self.store.finish(
            run.run_id,
            status,
            self._redact(detail),
            claim_token=run.claim_token,
            failure_class=failure_class,
            retryable=retryable,
        )

    def _retry_or_fail(self, run: ReviewRun, failure: FailureInfo) -> bool:
        delay = self._backoff(run.attempts)
        return self.store.retry_or_fail(
            run.run_id,
            failure.failure_class,
            failure.detail,
            delay_seconds=delay,
            max_attempts=self.max_attempts,
            claim_token=run.claim_token,
        )

    def _backoff(self, attempt: int) -> float:
        base = max(0.0, self.retry_delay_seconds)
        return min(base * (2 ** max(attempt - 1, 0)), self.max_retry_delay_seconds)

    def _prepare_agent_effects(
        self, run: ReviewRun, merge_request
    ) -> dict[str, EffectReceipt]:
        attempt = max(run.attempts, 1)
        receipts = {
            "agent_execution": self.store.create_effect_receipt(
                run.run_id,
                "agent_execution",
                f"review-run:{run.run_id}:agent",
                attempt=attempt,
                state="running",
                detail="Local Agent execution; no JSON output is required",
            ),
            "note_expected": self.store.create_effect_receipt(
                run.run_id,
                "note_expected",
                f"review-run:{run.run_id}:note",
                attempt=attempt,
                state="planned",
                detail="Agent-owned GitLab Note; the Worker does not require a JSON receipt",
            ),
        }
        if self.policy.ai.auto_fix:
            branch = f"autofix/MR-{merge_request.iid}/run-{run.run_id}"
            receipts["autofix_expected"] = self.store.create_effect_receipt(
                run.run_id,
                "autofix_expected",
                branch,
                attempt=attempt,
                state="planned",
                detail="Agent-owned Auto-fix MR; reconciliation observes the platform state",
            )
        return receipts

    def _mark_unobserved_effects(
        self, receipts: dict[str, EffectReceipt], error: str
    ) -> None:
        for kind, receipt in receipts.items():
            if kind == "agent_execution":
                continue
            self.store.update_effect_receipt(
                receipt.receipt_id,
                state="not_observed",
                error=self._redact(error),
            )

    def _heartbeat(self, run: ReviewRun) -> bool:
        return self.store.extend_lease(
            run.run_id,
            run.claim_token,
            lease_seconds=self.lease_seconds,
        )

    def _reconciliation_delay(self, attempt: int) -> float:
        return self._backoff(max(attempt, 1))

    def _record_observed_auto_fix(
        self,
        run: ReviewRun,
        auto_fix,
        reconciliation: EffectReceipt,
        claim_token: str | None = None,
    ) -> None:
        state = str(getattr(auto_fix, "state", "opened") or "opened").lower()
        relation_status = state if state in {"opened", "closed", "merged"} else "opened"
        merged_sha = getattr(auto_fix, "merged_sha", None)
        relation = self.store.record_auto_fix(
            parent_project_id=run.project_id,
            parent_mr_iid=run.mr_iid,
            parent_sha=run.sha,
            run_id=run.run_id,
            auto_fix_project_id=auto_fix.project_id,
            auto_fix_mr_iid=auto_fix.iid,
            status=relation_status,
            merged_sha=merged_sha if is_valid_sha(merged_sha) else None,
        )
        external_identity = f"{auto_fix.project_id}!{auto_fix.iid}"
        detail = (
            f"Auto-fix MR observed with state={relation.status}; "
            f"url={self._redact(getattr(auto_fix, 'web_url', None))}"
        )
        self.store.update_effect_receipt(
            reconciliation.receipt_id,
            state="observed",
            external_identity=external_identity,
            detail=detail,
            claim_token=claim_token,
        )
        for receipt in self.store.list_effect_receipts(run.run_id, "autofix_expected"):
            self.store.update_effect_receipt(
                receipt.receipt_id,
                state="observed",
                external_identity=external_identity,
                detail=detail,
            )

    def _schedule_reconciliation(
        self,
        receipt: EffectReceipt,
        detail: str,
        claim_token: str | None = None,
    ) -> None:
        next_attempt_at = (
            datetime.now(UTC)
            + timedelta(seconds=self._reconciliation_delay(receipt.attempt))
        ).isoformat()
        self.store.update_effect_receipt(
            receipt.receipt_id,
            state="pending",
            detail="Auto-fix MR not yet observable; reconciliation will retry",
            error=self._redact(detail),
            next_attempt_at=next_attempt_at,
            claim_token=claim_token,
        )

    def _record_auto_fix_if_created(self, run: ReviewRun, merge_request) -> None:
        if not self.policy.ai.auto_fix:
            return
        branch = f"autofix/MR-{merge_request.iid}/run-{run.run_id}"
        reconciliation = self.store.create_effect_receipt(
            run.run_id,
            "autofix_reconciliation",
            branch,
            attempt=1,
            state="pending",
            detail="Reconcile Agent-owned Auto-fix MR across opened, closed and merged states",
            next_attempt_at=datetime.now(UTC).isoformat(),
        )
        try:
            auto_fix = self.gitlab.find_merge_request_by_source_branch(
                run.project_id, branch, merge_request.source_branch
            )
        except Exception as exc:  # noqa: BLE001 - receipt retains retryable API failures
            failure = classify_exception(exc, secrets=self._redaction_secrets())
            if failure.retryable:
                self._schedule_reconciliation(reconciliation, failure.detail)
            else:
                self.store.update_effect_receipt(
                    reconciliation.receipt_id,
                    state="failed",
                    error=failure.detail,
                    detail="Auto-fix reconciliation failed terminally",
                )
            return
        if auto_fix is None:
            self._schedule_reconciliation(reconciliation, "Auto-fix MR was not observed yet")
            return
        self._record_observed_auto_fix(run, auto_fix, reconciliation)

    def reconcile_auto_fixes(self, limit: int = 1) -> WorkerResult | None:
        receipts = self.store.claim_pending_effect_receipts(
            "autofix_reconciliation",
            limit=1,
            lease_seconds=min(DEFAULT_EFFECT_LEASE_SECONDS, max(self.lease_seconds, 1)),
        )
        if not receipts:
            return None
        receipt = receipts[0]
        run = self.store.get_review_run(receipt.run_id)
        if run is None:
            self.store.update_effect_receipt(
                receipt.receipt_id,
                state="failed",
                error="Review Run no longer exists",
                claim_token=receipt.claim_token,
            )
            return WorkerResult(receipt.run_id, "failed", detail="Review Run no longer exists")
        try:
            parent_merge_request = self.gitlab.get_merge_request(run.project_id, run.mr_iid)
            auto_fix = self.gitlab.find_merge_request_by_source_branch(
                run.project_id,
                receipt.dedupe_key,
                parent_merge_request.source_branch,
            )
            if auto_fix is None:
                self._schedule_reconciliation(
                    receipt,
                    "Auto-fix MR is still not observable",
                    claim_token=receipt.claim_token,
                )
                return WorkerResult(
                    run.run_id, "deferred", detail="Auto-fix reconciliation pending"
                )
            self._record_observed_auto_fix(
                run,
                auto_fix,
                receipt,
                claim_token=receipt.claim_token,
            )
            return WorkerResult(run.run_id, "succeeded", detail="Auto-fix reconciliation observed")
        except Exception as exc:  # noqa: BLE001 - keep reconciliation independently retryable
            failure = classify_exception(exc, secrets=self._redaction_secrets())
            if failure.retryable:
                self._schedule_reconciliation(
                    receipt, failure.detail, claim_token=receipt.claim_token
                )
                return WorkerResult(
                    run.run_id, "deferred", detail="Auto-fix reconciliation retry scheduled"
                )
            self.store.update_effect_receipt(
                receipt.receipt_id,
                state="failed",
                detail="Auto-fix reconciliation failed terminally",
                error=failure.detail,
                claim_token=receipt.claim_token,
            )
            return WorkerResult(run.run_id, "failed", detail=failure.detail)

    def run_forever(self, poll_interval_seconds: float = 5.0) -> None:
        while True:
            result = self.process_one()
            reconciliation = self.reconcile_auto_fixes(limit=1)
            self.process_notifications(limit=1)
            if (
                result is None
                or result.status == "deferred"
                or (reconciliation is not None and reconciliation.status == "deferred")
            ):
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
            except Exception as exc:  # noqa: BLE001 - isolate notification claim failures
                LOGGER.error(
                    "failed to claim notification outbox record: %s",
                    self._redact(exc),
                )
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
            except Exception as exc:  # noqa: BLE001 - isolate notification update failures
                LOGGER.error(
                    "failed to update notification outbox record %s: %s",
                    delivery.delivery_id,
                    self._redact(exc),
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
        if not self._heartbeat(run):
            raise LeaseLostError("Review Run lease could not be extended before Agent execution")
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
            ),
            heartbeat=lambda: self._heartbeat(run),
            heartbeat_interval_seconds=max(
                0.1, min(30.0, self.lease_seconds / 3)
            ),
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
        except Exception as exc:  # noqa: BLE001 - isolate notification enqueue failures
            LOGGER.error(
                "failed to enqueue notification outbox record: %s",
                self._redact(exc),
            )
