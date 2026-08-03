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
from .gitlab import GitLabClient, canonical_note_marker, has_exact_note_marker, is_valid_sha
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
from .structured_logging import log_event
from .worktree import GitWorktreeManager

DEFAULT_REVIEW_AGENT_SKILL = Path(__file__).resolve().parent / "defaults" / "review-agent" / "SKILL.md"
DEFAULT_RECONCILIATION_MAX_ATTEMPTS = 3
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
        reconciliation_max_attempts: int = DEFAULT_RECONCILIATION_MAX_ATTEMPTS,
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
        if reconciliation_max_attempts <= 0:
            raise ValueError("reconciliation_max_attempts must be positive")
        self.max_retry_delay_seconds = max_retry_delay_seconds
        self.reconciliation_max_attempts = reconciliation_max_attempts
        self.project_id = configured_project_id.strip()

    def _redaction_secrets(self) -> tuple[str, ...]:
        return tuple(
            secret
            for secret in (
                self.gitlab_token,
                os.getenv("GITLAB_WEBHOOK_SECRET"),
                os.getenv("FEISHU_WEBHOOK"),
                os.getenv("WECOM_WEBHOOK"),
            )
            if secret and not secret.isspace()
        )

    def _redact(self, value: object) -> str:
        return redact(value, secrets=self._redaction_secrets())

    @staticmethod
    def _agent_note_marker(run: ReviewRun) -> str:
        return canonical_note_marker(run.run_id)

    def _require_active_lease(self, run: ReviewRun, detail: str) -> None:
        if not self.store.has_active_lease(run.run_id, run.claim_token):
            raise LeaseLostError(detail)

    def _update_run_effect_receipt(
        self,
        run: ReviewRun,
        receipt: EffectReceipt,
        *,
        state: str,
        external_identity: str | None = None,
        detail: str | None = None,
        error: str | None = None,
        next_attempt_at: str | None = None,
    ) -> None:
        updated = self.store.update_effect_receipt(
            receipt.receipt_id,
            state=state,
            external_identity=external_identity,
            detail=detail,
            error=error,
            next_attempt_at=next_attempt_at,
            run_id=run.run_id,
            run_claim_token=run.claim_token,
        )
        if not updated:
            raise LeaseLostError("Review Run lease was lost before updating an effect receipt")

    def _lease_recovery_result(self, run: ReviewRun, detail: str) -> WorkerResult:
        self._log_run(
            run,
            status="deferred",
            level=logging.WARNING,
            diagnostic=detail,
            failure_class="lease_lost",
            persisted=False,
            reclaim_required=True,
        )
        return WorkerResult(run.run_id, "deferred", detail="Review Run lease lost; reclaim will recover")

    def _log_run(
        self,
        run: ReviewRun,
        *,
        status: str,
        event: str = "worker.run",
        level: int = logging.INFO,
        diagnostic: object | None = None,
        **fields: object,
    ) -> None:
        log_event(
            LOGGER,
            level,
            secrets=self._redaction_secrets(),
            event=event,
            status=status,
            correlation_id=f"review-run:{run.run_id}",
            run_id=run.run_id,
            project_id=run.project_id,
            mr_iid=run.mr_iid,
            diagnostic=self._redact(diagnostic) if diagnostic is not None else None,
            attempt=run.attempts,
            **fields,
        )

    def process_one(self) -> WorkerResult | None:
        run = self.store.claim_next(
            lease_seconds=self.lease_seconds,
            max_attempts=self.max_attempts,
            project_id=self.project_id,
        )
        if run is None:
            reconciliation = self.reconcile_auto_fixes(limit=1)
            log_event(
                LOGGER,
                logging.INFO,
                secrets=self._redaction_secrets(),
                event="worker.poll",
                status="reconciled" if reconciliation is not None else "idle",
            )
            return reconciliation

        self._log_run(run, status="running", event="worker.run.claimed")
        receipts: dict[str, EffectReceipt] = {}
        agent_finished = False

        try:
            merge_request = self.gitlab.get_merge_request(run.project_id, run.mr_iid)
            if (
                merge_request.project_id != self.project_id
                or merge_request.iid != run.mr_iid
            ):
                detail = "GitLab Merge Request identity does not match the Review Run"
                if not self._finish_terminal(run, "cancelled", detail, "project_mismatch"):
                    return self._lease_recovery_result(run, detail)
                return WorkerResult(run.run_id, "cancelled", detail=detail)

            state = str(merge_request.state or "opened").lower()
            if state in {"closed", "merged"}:
                detail = f"merge request is {state}"
                if not self._finish_terminal(run, "cancelled", detail, "closed_or_merged"):
                    return self._lease_recovery_result(run, detail)
                return WorkerResult(run.run_id, "cancelled", detail=detail)

            if not is_valid_sha(merge_request.sha):
                detail = "GitLab returned an invalid current Merge Request head SHA"
                if not self._finish_terminal(run, "cancelled", detail, "invalid_sha"):
                    return self._lease_recovery_result(run, detail)
                return WorkerResult(run.run_id, "cancelled", detail=detail)

            if merge_request.sha != run.sha:
                detail = f"stale run SHA; current Merge Request head is {merge_request.sha}"
                current_run = self.store.cancel_and_enqueue_latest(
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
                if current_run is None:
                    return self._lease_recovery_result(run, detail)
                self._log_run(
                    run,
                    status="cancelled",
                    diagnostic=detail,
                    failure_class="stale_sha",
                )
                return WorkerResult(run.run_id, "cancelled", detail=detail)

            if merge_request.draft:
                requeued = self.store.requeue(
                    run.run_id,
                    "draft merge request",
                    delay_seconds=self.retry_delay_seconds,
                    claim_token=run.claim_token,
                )
                if not requeued:
                    return self._lease_recovery_result(run, "Review Run lease lost before draft requeue")
                self._log_run(run, status="deferred", diagnostic="draft merge request")
                return WorkerResult(run.run_id, "deferred", detail="draft merge request")
            if run.kind == "autofix":
                parent = parse_auto_fix_branch(merge_request.source_branch)
                if parent is None:
                    return self._reject_autofix_run(
                        run, "auto-fix MR source branch does not identify its parent MR"
                    )
                parent_mr_iid, branch_run_id = parent
                if branch_run_id != run.run_id:
                    return self._reject_autofix_run(
                        run, "auto-fix MR source branch run id does not match Review Run"
                    )
                expected_branch = f"autofix/MR-{parent_mr_iid}/run-{run.run_id}"
                if (
                    merge_request.iid != run.mr_iid
                    or merge_request.source_branch != expected_branch
                ):
                    return self._reject_autofix_run(
                        run, "auto-fix MR source branch or IID does not match Review Run"
                    )
                parent_merge_request = self.gitlab.get_merge_request(run.project_id, parent[0])
                identity_error = self._autofix_identity_error(
                    run,
                    parent_merge_request,
                    merge_request,
                    expected_branch=expected_branch,
                )
                if identity_error is not None:
                    return self._reject_autofix_run(run, identity_error)
                self._require_active_lease(run, "Review Run lease lost before Auto-fix notification")
                self._notify_autofix(run, parent_merge_request, merge_request)
                if not self.store.finish(run.run_id, "succeeded", claim_token=run.claim_token):
                    return self._lease_recovery_result(
                        run, "Review Run lease lost before Auto-fix notification completion"
                    )
                self._log_run(run, status="succeeded", diagnostic="auto-fix MR notification")
                return WorkerResult(run.run_id, "succeeded", detail="auto-fix MR notification")

            if self.policy.pipeline_gate:
                pipeline = self.gitlab.get_latest_pipeline(run.project_id, run.sha)
                if pipeline is None or pipeline.status != "success":
                    requeued = self.store.requeue(
                        run.run_id,
                        "waiting for successful pipeline",
                        delay_seconds=self.retry_delay_seconds,
                        claim_token=run.claim_token,
                    )
                    if not requeued:
                        return self._lease_recovery_result(
                            run, "Review Run lease lost before pipeline requeue"
                        )
                    self._log_run(run, status="deferred", diagnostic="pipeline gate")
                    return WorkerResult(run.run_id, "deferred", detail="pipeline gate")

            self._notify_humans(run, merge_request)
            if not self.policy.ai.enabled or self.store.is_suppressed_parent_sha(
                run.project_id, run.mr_iid, run.sha
            ):
                if not self.store.finish(run.run_id, "succeeded", claim_token=run.claim_token):
                    return self._lease_recovery_result(
                        run, "Review Run lease lost before human-path completion"
                    )
                self._log_run(run, status="succeeded", diagnostic="human path only")
                return WorkerResult(run.run_id, "succeeded", detail="human path only")

            worktree_id = str(run.run_id) if run.attempts <= 1 else f"{run.run_id}-attempt-{run.attempts}"
            worktree = self.worktrees.create(run.sha, worktree_id)
            try:
                receipts = self._prepare_agent_effects(run, merge_request)
                result = self._run_agent(run, merge_request, worktree)
                agent_finished = True
                agent_receipt = receipts["agent_execution"]
                if result.timed_out:
                    detail = self._redact(result.stderr[-4000:] or "Agent timed out")
                    self._update_run_effect_receipt(
                        run,
                        agent_receipt,
                        state="failed",
                        detail="Agent process timed out",
                        error=detail,
                    )
                    self._mark_unobserved_effects(run, receipts, detail)
                    if not self._finish_terminal(run, "failed", detail, "agent_timeout"):
                        return self._lease_recovery_result(
                            run, "Review Run lease lost before Agent timeout completion"
                        )
                    self._log_run(
                        run,
                        status="failed",
                        level=logging.WARNING,
                        diagnostic=detail,
                        failure_class="agent_timeout",
                    )
                    return WorkerResult(run.run_id, "failed", result, "Agent timed out")
                if result.returncode != 0:
                    detail = self._redact(result.stderr[-4000:] or "Agent failed")
                    self._update_run_effect_receipt(
                        run,
                        agent_receipt,
                        state="failed",
                        detail=f"Agent exited with code {result.returncode}",
                        error=detail,
                    )
                    self._mark_unobserved_effects(run, receipts, detail)
                    if not self.store.finish(
                        run.run_id,
                        "failed",
                        detail,
                        claim_token=run.claim_token,
                        failure_class="agent_exit",
                        retryable=False,
                    ):
                        return self._lease_recovery_result(
                            run, "Review Run lease lost before Agent failure completion"
                        )
                    self._log_run(
                        run,
                        status="failed",
                        level=logging.WARNING,
                        diagnostic=detail,
                        failure_class="agent_exit",
                    )
                    return WorkerResult(run.run_id, "failed", result, "Agent process failed")
                self._update_run_effect_receipt(
                    run,
                    agent_receipt,
                    state="succeeded",
                    detail="Agent process exited successfully",
                )
                self._record_observed_note(run, receipts["note_expected"])
                identity_error = self._record_auto_fix_if_created(run, merge_request, receipts)
                if identity_error is not None:
                    if not self._finish_terminal(
                        run, "failed", identity_error, "autofix_identity"
                    ):
                        return self._lease_recovery_result(
                            run, "Review Run lease lost before Auto-fix identity failure completion"
                        )
                    return WorkerResult(run.run_id, "failed", result, identity_error)
                if not self.store.finish(run.run_id, "succeeded", claim_token=run.claim_token):
                    raise LeaseLostError("Review Run lease lost before Agent result completion")
                self._log_run(run, status="succeeded", diagnostic="Agent process exited successfully")
                return WorkerResult(run.run_id, "succeeded", result)
            finally:
                self.worktrees.remove(worktree)
        except LeaseLostError as exc:
            if receipts and not agent_finished:
                self._cleanup_agent_effects(run, receipts, exc)
            failure = FailureInfo(
                failure_class="lease_lost",
                retryable=True,
                detail=self._redact(str(exc) or "Review Run lease lost"),
            )
            requeued = self.store.retry_or_fail(
                run.run_id,
                failure.failure_class,
                failure.detail,
                delay_seconds=self._backoff(run.attempts),
                max_attempts=self.max_attempts,
                claim_token=run.claim_token,
            )
            if requeued:
                self._log_run(
                    run,
                    status="deferred",
                    level=logging.WARNING,
                    diagnostic=failure.detail,
                    failure_class=failure.failure_class,
                    retryable=True,
                    persisted=True,
                )
                return WorkerResult(run.run_id, "deferred", detail=failure.detail)

            current = self.store.get_review_run(run.run_id)
            if (
                current is not None
                and current.status == "failed"
                and current.failure_class == failure.failure_class
                and current.claim_token is None
            ):
                self._log_run(
                    run,
                    status="failed",
                    level=logging.WARNING,
                    diagnostic=failure.detail,
                    failure_class=failure.failure_class,
                    retryable=True,
                    persisted=True,
                )
                return WorkerResult(run.run_id, "failed", detail=failure.detail)

            self._log_run(
                run,
                status="deferred",
                level=logging.WARNING,
                diagnostic=failure.detail,
                failure_class=failure.failure_class,
                retryable=True,
                persisted=False,
                reclaim_required=True,
            )
            return WorkerResult(
                run.run_id,
                "deferred",
                detail="Review Run lease lost; reclaim will recover",
            )
        except Exception as exc:  # noqa: BLE001 - a failed Run must be persisted
            if receipts and not agent_finished:
                self._cleanup_agent_effects(run, receipts, exc)
            failure = classify_exception(exc, secrets=self._redaction_secrets())
            if failure.retryable:
                requeued = self._retry_or_fail(run, failure)
                return WorkerResult(
                    run.run_id,
                    "deferred" if requeued else "failed",
                    detail=failure.detail,
                )
            if not self._finish_terminal(run, "failed", failure.detail, failure.failure_class):
                return self._lease_recovery_result(run, failure.detail)
            return WorkerResult(run.run_id, "failed", detail=failure.detail)

    def _finish_terminal(
        self,
        run: ReviewRun,
        status: str,
        detail: str,
        failure_class: str,
        retryable: bool = False,
    ) -> bool:
        updated = self.store.finish(
            run.run_id,
            status,
            self._redact(detail),
            claim_token=run.claim_token,
            failure_class=failure_class,
            retryable=retryable,
        )
        self._log_run(
            run,
            status=status if updated else "not_updated",
            level=logging.WARNING if status == "failed" else logging.INFO,
            diagnostic=detail,
            failure_class=failure_class,
            persisted=updated,
        )
        return updated

    def _retry_or_fail(self, run: ReviewRun, failure: FailureInfo) -> bool:
        delay = self._backoff(run.attempts)
        requeued = self.store.retry_or_fail(
            run.run_id,
            failure.failure_class,
            failure.detail,
            delay_seconds=delay,
            max_attempts=self.max_attempts,
            claim_token=run.claim_token,
        )
        self._log_run(
            run,
            status="deferred" if requeued else "failed",
            level=logging.WARNING,
            diagnostic=failure.detail,
            failure_class=failure.failure_class,
            retryable=True,
            persisted=requeued,
        )
        return requeued

    def _backoff(self, attempt: int) -> float:
        base = max(0.0, self.retry_delay_seconds)
        return min(base * (2 ** max(attempt - 1, 0)), self.max_retry_delay_seconds)

    def _prepare_agent_effects(
        self, run: ReviewRun, merge_request
    ) -> dict[str, EffectReceipt]:
        attempt = max(run.attempts, 1)
        receipts: dict[str, EffectReceipt] = {}
        try:
            receipts["agent_execution"] = self.store.create_effect_receipt(
                run.run_id,
                "agent_execution",
                f"review-run:{run.run_id}:agent",
                attempt=attempt,
                state="running",
                detail="Local Agent execution; no JSON output is required",
            )
            receipts["note_expected"] = self.store.create_effect_receipt(
                run.run_id,
                "note_expected",
                f"review-run:{run.run_id}:note",
                attempt=attempt,
                state="planned",
                detail="Agent-owned GitLab Note; observation uses the deterministic run marker",
            )
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
                receipts["autofix_reconciliation"] = self.store.create_effect_receipt(
                    run.run_id,
                    "autofix_reconciliation",
                    branch,
                    attempt=1,
                    state="pending",
                    detail=(
                        "Reconcile Agent-owned Auto-fix MR across opened, closed and merged states"
                    ),
                    next_attempt_at=datetime.now(UTC).isoformat(),
                )
        except Exception as exc:
            if receipts:
                self._cleanup_agent_effects(run, receipts, exc)
            raise
        return receipts

    def _cleanup_agent_effects(
        self, run: ReviewRun, receipts: dict[str, EffectReceipt], error: object
    ) -> None:
        safe_error = self._redact(error)
        for kind, receipt in receipts.items():
            if kind == "autofix_reconciliation":
                continue
            self.store.update_effect_receipt(
                receipt.receipt_id,
                state="failed" if kind == "agent_execution" else "not_observed",
                detail=(
                    "Agent execution failed before completion"
                    if kind == "agent_execution"
                    else "Effect was not observed because Agent execution did not complete"
                ),
                error=safe_error,
                run_id=run.run_id,
                run_claim_token=run.claim_token,
            )

    def _mark_unobserved_effects(
        self, run: ReviewRun, receipts: dict[str, EffectReceipt], error: str
    ) -> None:
        for kind, receipt in receipts.items():
            if kind in {"agent_execution", "autofix_reconciliation"}:
                continue
            self._update_run_effect_receipt(
                run,
                receipt,
                state="not_observed",
                error=self._redact(error),
            )

    def _record_observed_note(self, run: ReviewRun, receipt: EffectReceipt) -> None:
        """Record a Note only after the adapter verifies its scoped marker and native ID."""

        marker = self._agent_note_marker(run)
        observer = getattr(self.gitlab, "find_note_by_marker", None)
        if not callable(observer):
            self._update_run_effect_receipt(
                run,
                receipt,
                state="not_observed",
                detail="GitLab adapter cannot verify the Agent-owned Note",
            )
            return
        try:
            note = observer(run.project_id, run.mr_iid, marker)
        except Exception as exc:  # noqa: BLE001 - external observation is independent
            self._update_run_effect_receipt(
                run,
                receipt,
                state="not_observed",
                detail="GitLab Note observation failed; Agent process status is independent",
                error=self._redact(exc),
            )
            return

        note_project_id = getattr(note, "project_id", None)
        note_mr_iid = getattr(note, "mr_iid", None)
        note_id = getattr(note, "note_id", None)
        if (
            note is None
            or type(note_project_id) is not str
            or note_project_id != run.project_id
            or type(note_mr_iid) is not int
            or note_mr_iid <= 0
            or note_mr_iid != run.mr_iid
            or type(note_id) is not int
            or note_id <= 0
            or not has_exact_note_marker(getattr(note, "body", None), marker)
        ):
            self._update_run_effect_receipt(
                run,
                receipt,
                state="not_observed",
                detail="No valid scoped GitLab Note was observed for the Agent run",
            )
            return

        external_identity = f"gitlab:{run.project_id}!{run.mr_iid}#note-{note_id}"
        self._update_run_effect_receipt(
            run,
            receipt,
            state="observed",
            external_identity=external_identity,
            detail=f"Agent-owned GitLab Note observed with native id={note_id}",
        )

    def _heartbeat(self, run: ReviewRun) -> bool:
        return self.store.extend_lease(
            run.run_id,
            run.claim_token,
            lease_seconds=self.lease_seconds,
        )

    def _reconciliation_delay(self, attempt: int) -> float:
        return self._backoff(max(attempt, 1))

    def _autofix_identity_error(
        self,
        run: ReviewRun,
        parent_merge_request,
        auto_fix,
        *,
        parent_mr_iid: int,
        expected_branch: str,
    ) -> str | None:
        parsed = parse_auto_fix_branch(expected_branch)
        if parsed != (parent_mr_iid, run.run_id):
            return "Auto-fix expected branch does not match its parent MR and Review Run"
        if (
            getattr(parent_merge_request, "project_id", None) != run.project_id
            or getattr(parent_merge_request, "iid", None) != parent_mr_iid
        ):
            return "Auto-fix parent Merge Request identity does not match the Review Run"
        if getattr(parent_merge_request, "source_branch", None) in {None, ""}:
            return "Auto-fix parent Merge Request has no source branch"
        if getattr(auto_fix, "project_id", None) != run.project_id:
            return "Auto-fix Merge Request belongs to a different project"
        if getattr(auto_fix, "iid", None) is None or getattr(auto_fix, "iid", 0) <= 0:
            return "Auto-fix Merge Request has an invalid IID"
        if getattr(auto_fix, "source_branch", None) != expected_branch:
            return "Auto-fix Merge Request source branch does not match its receipt"
        if getattr(auto_fix, "target_branch", None) != parent_merge_request.source_branch:
            return "Auto-fix Merge Request target branch does not match its parent"
        state = str(getattr(auto_fix, "state", "") or "").lower()
        if state not in {"opened", "closed", "merged"}:
            return "Auto-fix Merge Request returned an unknown state"
        if state == "merged" and not is_valid_sha(getattr(auto_fix, "merged_sha", None)):
            return "Merged Auto-fix Merge Request has no valid merged SHA"
        return None

    def _reject_autofix_run(self, run: ReviewRun, detail: str) -> WorkerResult:
        self.store.create_effect_receipt(
            run.run_id,
            "autofix_identity",
            f"review-run:{run.run_id}:autofix-identity",
            attempt=max(run.attempts, 1),
            state="failed",
            detail="Auto-fix identity validation failed",
            error=detail,
        )
        if not self._finish_terminal(run, "failed", detail, "autofix_identity"):
            return self._lease_recovery_result(run, detail)
        return WorkerResult(run.run_id, "failed", detail=detail)

    def _reject_reconciliation(
        self, run: ReviewRun, receipt: EffectReceipt, detail: str
    ) -> WorkerResult:
        updated = self.store.update_effect_receipt(
            receipt.receipt_id,
            state="failed",
            detail="Auto-fix identity validation failed",
            error=detail,
            claim_token=receipt.claim_token,
        )
        if not updated:
            return WorkerResult(
                run.run_id,
                "deferred",
                detail="Auto-fix reconciliation lease lost; reclaim will recover",
            )
        self._log_run(
            run,
            event="worker.autofix_reconcile",
            status="failed",
            level=logging.ERROR,
            diagnostic=detail,
            failure_class="autofix_identity",
        )
        return WorkerResult(run.run_id, "failed", detail=detail)

    def _record_observed_auto_fix(
        self,
        run: ReviewRun,
        auto_fix,
        reconciliation: EffectReceipt,
        claim_token: str | None = None,
        run_claim_token: str | None = None,
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
            merged_sha=(
                merged_sha
                if relation_status == "merged" and is_valid_sha(merged_sha)
                else None
            ),
            run_claim_token=run_claim_token,
            effect_receipt_id=reconciliation.receipt_id if claim_token is not None else None,
            effect_claim_token=claim_token,
        )
        if relation is None:
            raise LeaseLostError("Auto-fix reconciliation lease was lost before recording the MR")
        external_identity = f"{auto_fix.project_id}!{auto_fix.iid}"
        detail = (
            f"Auto-fix MR observed with state={relation.status}; "
            f"url={self._redact(getattr(auto_fix, 'web_url', None))}"
        )
        if run_claim_token is not None:
            self._update_run_effect_receipt(
                run,
                reconciliation,
                state="observed",
                external_identity=external_identity,
                detail=detail,
            )
        elif not self.store.update_effect_receipt(
            reconciliation.receipt_id,
            state="observed",
            external_identity=external_identity,
            detail=detail,
            claim_token=claim_token,
        ):
            raise LeaseLostError("Auto-fix reconciliation lease was lost before receipt completion")
        for receipt in self.store.list_effect_receipts(run.run_id, "autofix_expected"):
            if run_claim_token is not None:
                self._update_run_effect_receipt(
                    run,
                    receipt,
                    state="observed",
                    external_identity=external_identity,
                    detail=detail,
                )
            else:
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
        run: ReviewRun | None = None,
    ) -> bool:
        safe_detail = self._redact(detail)
        if receipt.attempt >= self.reconciliation_max_attempts:
            if run is not None:
                self._update_run_effect_receipt(
                    run,
                    receipt,
                    state="not_observed",
                    detail=(
                        "Auto-fix MR was not observed after "
                        f"{self.reconciliation_max_attempts} reconciliation attempts"
                    ),
                    error=safe_detail,
                )
            else:
                self.store.update_effect_receipt(
                    receipt.receipt_id,
                    state="not_observed",
                    detail=(
                        "Auto-fix MR was not observed after "
                        f"{self.reconciliation_max_attempts} reconciliation attempts"
                    ),
                    error=safe_detail,
                    next_attempt_at=None,
                    claim_token=claim_token,
                )
            return False
        next_attempt_at = (
            datetime.now(UTC)
            + timedelta(seconds=self._reconciliation_delay(receipt.attempt))
        ).isoformat()
        if run is not None:
            self._update_run_effect_receipt(
                run,
                receipt,
                state="pending",
                detail="Auto-fix MR not yet observable; reconciliation will retry",
                error=safe_detail,
                next_attempt_at=next_attempt_at,
            )
        else:
            self.store.update_effect_receipt(
                receipt.receipt_id,
                state="pending",
                detail="Auto-fix MR not yet observable; reconciliation will retry",
                error=safe_detail,
                next_attempt_at=next_attempt_at,
                claim_token=claim_token,
            )
        return True

    def _record_auto_fix_if_created(
        self,
        run: ReviewRun,
        merge_request,
        receipts: dict[str, EffectReceipt],
    ) -> str | None:
        if not self.policy.ai.auto_fix:
            return None
        branch = f"autofix/MR-{merge_request.iid}/run-{run.run_id}"
        reconciliation = receipts.get("autofix_reconciliation")
        if reconciliation is None:
            detail = "Auto-fix reconciliation receipt is missing"
            return detail
        if reconciliation.dedupe_key != branch:
            detail = "Auto-fix reconciliation receipt branch does not match the Review Run"
            self._update_run_effect_receipt(
                run,
                reconciliation,
                state="failed",
                detail="Auto-fix identity validation failed",
                error=detail,
            )
            return detail
        self._require_active_lease(run, "Review Run lease lost before Auto-fix reconciliation")
        try:
            auto_fix = self.gitlab.find_merge_request_by_source_branch(
                run.project_id, branch, merge_request.source_branch
            )
        except Exception as exc:  # noqa: BLE001 - receipt retains retryable API failures
            failure = classify_exception(exc, secrets=self._redaction_secrets())
            if failure.retryable:
                self._schedule_reconciliation(reconciliation, failure.detail, run=run)
            else:
                self._update_run_effect_receipt(
                    run,
                    reconciliation,
                    state="failed",
                    error=failure.detail,
                    detail="Auto-fix reconciliation failed terminally",
                )
            return None
        if auto_fix is None:
            self._schedule_reconciliation(
                reconciliation,
                "Auto-fix MR was not observed yet",
                run=run,
            )
            return None
        identity_error = self._autofix_identity_error(
            run,
            merge_request,
            auto_fix,
            parent_mr_iid=run.mr_iid,
            expected_branch=branch,
        )
        if identity_error is not None:
            self._update_run_effect_receipt(
                run,
                reconciliation,
                state="failed",
                detail="Auto-fix identity validation failed",
                error=identity_error,
            )
            return identity_error
        self._record_observed_auto_fix(
            run,
            auto_fix,
            reconciliation,
            run_claim_token=run.claim_token,
        )
        return None

    def reconcile_auto_fixes(self, limit: int = 1) -> WorkerResult | None:
        receipts = self.store.claim_pending_effect_receipts(
            "autofix_reconciliation",
            limit=1,
            lease_seconds=min(DEFAULT_EFFECT_LEASE_SECONDS, max(self.lease_seconds, 1)),
            max_attempts=self.reconciliation_max_attempts,
            project_id=self.project_id,
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
            log_event(
                LOGGER,
                logging.ERROR,
                secrets=self._redaction_secrets(),
                event="worker.autofix_reconcile",
                status="failed",
                correlation_id=f"review-run:{receipt.run_id}",
                run_id=receipt.run_id,
                diagnostic="Review Run no longer exists",
            )
            return WorkerResult(receipt.run_id, "failed", detail="Review Run no longer exists")
        if run.project_id != self.project_id:
            detail = "Auto-fix reconciliation receipt belongs to a different project"
            self.store.update_effect_receipt(
                receipt.receipt_id,
                state="failed",
                detail="Auto-fix reconciliation project scope mismatch",
                error=detail,
                claim_token=receipt.claim_token,
            )
            self._log_run(
                run,
                event="worker.autofix_reconcile",
                status="failed",
                level=logging.ERROR,
                diagnostic=detail,
                failure_class="project_mismatch",
            )
            return WorkerResult(receipt.run_id, "failed", detail=detail)
        parsed_branch = parse_auto_fix_branch(receipt.dedupe_key)
        expected_branch = f"autofix/MR-{run.mr_iid}/run-{run.run_id}"
        expected_receipt = next(
            (
                item
                for item in self.store.list_effect_receipts(run.run_id, "autofix_expected")
                if item.dedupe_key == expected_branch
            ),
            None,
        )
        if parsed_branch != (run.mr_iid, run.run_id) or receipt.dedupe_key != expected_branch:
            return self._reject_reconciliation(
                run,
                receipt,
                "Auto-fix reconciliation branch does not match the Review Run",
            )
        if expected_receipt is None:
            return self._reject_reconciliation(
                run,
                receipt,
                "Auto-fix expected receipt is missing for reconciliation",
            )
        try:
            parent_merge_request = self.gitlab.get_merge_request(run.project_id, run.mr_iid)
            parent_identity_error = None
            if (
                getattr(parent_merge_request, "project_id", None) != run.project_id
                or getattr(parent_merge_request, "iid", None) != run.mr_iid
                or getattr(parent_merge_request, "source_branch", None) in {None, ""}
            ):
                parent_identity_error = (
                    "Auto-fix parent Merge Request identity does not match the Review Run"
                )
            if parent_identity_error is not None:
                return self._reject_reconciliation(run, receipt, parent_identity_error)
            auto_fix = self.gitlab.find_merge_request_by_source_branch(
                run.project_id,
                receipt.dedupe_key,
                parent_merge_request.source_branch,
            )
            if auto_fix is None:
                scheduled = self._schedule_reconciliation(
                    receipt,
                    "Auto-fix MR is still not observable",
                    claim_token=receipt.claim_token,
                )
                self._log_run(
                    run,
                    event="worker.autofix_reconcile",
                    status="deferred" if scheduled else "succeeded",
                    diagnostic=(
                    "Auto-fix reconciliation pending"
                        if scheduled
                        else "Auto-fix reconciliation exhausted; not observed"
                    ),
                )
                return WorkerResult(
                    run.run_id,
                    "deferred" if scheduled else "succeeded",
                    detail=(
                        "Auto-fix reconciliation pending"
                        if scheduled
                        else "Auto-fix reconciliation exhausted; not observed"
                    ),
                )
            identity_error = self._autofix_identity_error(
                run,
                parent_merge_request,
                auto_fix,
                parent_mr_iid=run.mr_iid,
                expected_branch=expected_branch,
            )
            if identity_error is not None:
                return self._reject_reconciliation(run, receipt, identity_error)
            self._record_observed_auto_fix(
                run,
                auto_fix,
                receipt,
                claim_token=receipt.claim_token,
            )
            self._log_run(
                run,
                event="worker.autofix_reconcile",
                status="succeeded",
                diagnostic="Auto-fix reconciliation observed",
            )
            return WorkerResult(run.run_id, "succeeded", detail="Auto-fix reconciliation observed")
        except LeaseLostError:
            return WorkerResult(
                run.run_id,
                "deferred",
                detail="Auto-fix reconciliation lease lost; reclaim will recover",
            )
        except Exception as exc:  # noqa: BLE001 - keep reconciliation independently retryable
            failure = classify_exception(exc, secrets=self._redaction_secrets())
            if failure.retryable:
                scheduled = self._schedule_reconciliation(
                    receipt, failure.detail, claim_token=receipt.claim_token
                )
                self._log_run(
                    run,
                    event="worker.autofix_reconcile",
                    status="deferred" if scheduled else "succeeded",
                    level=logging.WARNING,
                    diagnostic=(
                        failure.detail
                        if scheduled
                        else "Auto-fix reconciliation exhausted; not observed"
                    ),
                    failure_class=failure.failure_class,
                )
                return WorkerResult(
                    run.run_id,
                    "deferred" if scheduled else "succeeded",
                    detail=(
                        "Auto-fix reconciliation retry scheduled"
                        if scheduled
                        else "Auto-fix reconciliation exhausted; not observed"
                    ),
                )
            self.store.update_effect_receipt(
                receipt.receipt_id,
                state="failed",
                detail="Auto-fix reconciliation failed terminally",
                error=failure.detail,
                claim_token=receipt.claim_token,
            )
            self._log_run(
                run,
                event="worker.autofix_reconcile",
                status="failed",
                level=logging.ERROR,
                diagnostic=failure.detail,
                failure_class=failure.failure_class,
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
                log_event(
                    LOGGER,
                    logging.ERROR,
                    secrets=self._redaction_secrets(),
                    event="notification.claim",
                    status="failed",
                    diagnostic=self._redact(exc),
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
                log_event(
                    LOGGER,
                    logging.INFO if result.delivered else logging.WARNING,
                    secrets=self._redaction_secrets(),
                    event="notification.delivery",
                    status="delivered" if result.delivered else "failed",
                    correlation_id=delivery.correlation_id,
                    delivery_id=delivery.delivery_id,
                    channel=delivery.channel,
                    attempt=delivery.attempts,
                    diagnostic=result.detail or None,
                )
            except Exception as exc:  # noqa: BLE001 - isolate notification update failures
                log_event(
                    LOGGER,
                    logging.ERROR,
                    secrets=self._redaction_secrets(),
                    event="notification.update",
                    status="failed",
                    correlation_id=delivery.correlation_id,
                    delivery_id=delivery.delivery_id,
                    diagnostic=self._redact(exc),
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
                note_marker=self._agent_note_marker(run),
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
            log_event(
                LOGGER,
                logging.INFO,
                secrets=self._redaction_secrets(),
                event="notification.queue",
                status="queued",
                correlation_id=correlation_id,
                channel_count=len(deliveries),
            )
            delivery_ids = tuple(
                delivery.delivery_id for delivery in deliveries if delivery.status == "pending"
            )
            self.process_notifications(delivery_ids=delivery_ids)
        except Exception as exc:  # noqa: BLE001 - isolate notification enqueue failures
            log_event(
                LOGGER,
                logging.ERROR,
                secrets=self._redaction_secrets(),
                event="notification.queue",
                status="failed",
                correlation_id=correlation_id,
                diagnostic=self._redact(exc),
            )
