"""Review Run worker orchestration."""

from __future__ import annotations

import logging
import os
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from queue import Empty, Queue
from threading import Event, Thread

from .auto_fix_reconciler import AutoFixReconciler
from .autofix_planner import (
    DiffEnvelopeError,
    build_publication_plan_payload,
    capture_diff_envelope,
    run_verifier,
)
from .autofix_publisher import AutoFixPublisher
from .events import parse_auto_fix_branch, parse_auto_fix_marker
from .failures import FailureInfo, classify_exception
from .gitlab import GitLabClient, canonical_note_marker, is_valid_sha
from .note_publisher import NotePublisher
from .notification_dispatcher import NotificationDispatcher
from .notifications import FanoutNotifier, NotificationEvent
from .operator_config import AutoFixPublicationConfig, ResolvedBackend
from .policy import ReviewPolicy
from .repository import GitShaEnsurer
from .runner import (
    AgentContext,
    AgentContractError,
    AgentResult,
    LeaseLostError,
    LocalCliAgentRunner,
)
from .security import redact
from .store import (
    DEFAULT_LEASE_SECONDS,
    DEFAULT_MAX_ATTEMPTS,
    DEFAULT_NOTIFICATION_LEASE_SECONDS,
    DEFAULT_NOTIFICATION_MAX_ATTEMPTS,
    DEFAULT_WORKTREE_CLEANUP_LEASE_SECONDS,
    DEFAULT_WORKTREE_CLEANUP_MAX_ATTEMPTS,
    AutoFixPublicationIntent,
    EffectReceipt,
    ReviewRun,
    SQLiteStore,
)
from .structured_logging import log_event
from .worktree import GitWorktreeManager, WorktreeError

DEFAULT_REVIEW_AGENT_SKILL = (
    Path(__file__).resolve().parent / "defaults" / "review-agent" / "SKILL.md"
)
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
        sha_ensurer: GitShaEnsurer | None = None,
        worktree_cleanup_lease_seconds: int = DEFAULT_WORKTREE_CLEANUP_LEASE_SECONDS,
        worktree_cleanup_max_attempts: int = DEFAULT_WORKTREE_CLEANUP_MAX_ATTEMPTS,
        max_retry_delay_seconds: float = 300.0,
        reconciliation_max_attempts: int = DEFAULT_RECONCILIATION_MAX_ATTEMPTS,
        agent_backend: ResolvedBackend | None = None,
        autofix_publication: AutoFixPublicationConfig | None = None,
    ) -> None:
        configured_project_id = (
            project_id if project_id is not None else os.getenv("REVIEW_AGENT_FLOW_PROJECT_ID")
        )
        if configured_project_id is None or not configured_project_id.strip():
            raise ValueError(
                "ReviewFlow startup configuration error: "
                "project_id (REVIEW_AGENT_FLOW_PROJECT_ID) must be set"
            )
        self.store = store
        self.gitlab = gitlab
        self.worktrees = worktrees
        self.policy = policy
        if policy.ai.enabled and agent_backend is None:
            raise ValueError("AI Review requires an Operator-authorized Agent Backend/Profile")
        self.agent_backend = agent_backend
        self.repository = repository
        self.gitlab_url = gitlab_url
        self.gitlab_token = gitlab_token
        register_secret = getattr(self.store, "add_redaction_secret", None)
        if callable(register_secret):
            register_secret(gitlab_token)
        self.notifier = notifier or FanoutNotifier()
        self.agent_timeout_seconds = (
            agent_backend.timeout_seconds if agent_backend is not None else agent_timeout_seconds
        )
        self.lease_seconds = lease_seconds
        self.max_attempts = max_attempts
        self.retry_delay_seconds = retry_delay_seconds
        self.notification_dispatcher = NotificationDispatcher(
            store,
            self.notifier,
            lease_seconds=notification_lease_seconds,
            max_attempts=notification_max_attempts,
            retry_delay_seconds=retry_delay_seconds,
            secret_provider=self._redaction_secrets,
        )
        if max_retry_delay_seconds < 0:
            raise ValueError("max_retry_delay_seconds cannot be negative")
        if reconciliation_max_attempts <= 0:
            raise ValueError("reconciliation_max_attempts must be positive")
        self.max_retry_delay_seconds = max_retry_delay_seconds
        self.reconciliation_max_attempts = reconciliation_max_attempts
        self.project_id = configured_project_id.strip()
        self.autofix_publication = autofix_publication or AutoFixPublicationConfig()
        self.auto_fix_enabled = bool(
            policy.ai.auto_fix
            and self.autofix_publication.enabled
            and self.project_id in self.autofix_publication.allowed_projects
        )
        if policy.ai.auto_fix and not self.auto_fix_enabled:
            raise ValueError(
                "Auto-fix requires Operator-owned publication authorization for this Project"
            )
        self.note_publisher = NotePublisher(
            store,
            gitlab,
            self.project_id,
            retry_delay_seconds=retry_delay_seconds,
            max_retry_delay_seconds=max_retry_delay_seconds,
        )
        self.auto_fix_publisher = AutoFixPublisher(
            store,
            gitlab,
            self.project_id,
            self.autofix_publication,
            self.notifier,
            retry_delay_seconds=retry_delay_seconds,
            max_retry_delay_seconds=max_retry_delay_seconds,
            secrets=self._redaction_secrets(),
        )
        self.auto_fix_reconciler = AutoFixReconciler(
            store,
            gitlab,
            self.project_id,
            run_lease_seconds=lease_seconds,
            max_attempts=reconciliation_max_attempts,
            retry_delay_seconds=retry_delay_seconds,
            max_retry_delay_seconds=max_retry_delay_seconds,
            notifier=self.notifier,
            secret_provider=self._redaction_secrets,
        )
        self.sha_ensurer = sha_ensurer or GitShaEnsurer(repository)
        self.worktree_cleanup_lease_seconds = worktree_cleanup_lease_seconds
        self.worktree_cleanup_max_attempts = worktree_cleanup_max_attempts

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
        return canonical_note_marker(run.project_id, run.mr_iid, run.sha)

    def _resume_from_frozen_note(
        self,
        run: ReviewRun,
        intent: AutoFixPublicationIntent | None,
        note_request_id: int,
    ) -> WorkerResult:
        """Complete a crashed Run from its durable independent Note output."""

        plan = self.store.get_auto_fix_publication_plan(run.run_id)
        if (
            intent is not None
            and plan is None
            and not self.store.block_incomplete_auto_fix_after_frozen_note(
                run.run_id,
                run.claim_token,
                "Worker crashed after freezing the Note but before the Auto-fix candidate was durable",
            )
        ):
            return self._lease_recovery_result(
                run, "Review Run lease lost while recovering its frozen Note"
            )
        if not self.store.finish(run.run_id, "succeeded", claim_token=run.claim_token):
            return self._lease_recovery_result(
                run, "Review Run lease lost before frozen Note recovery completion"
            )
        self._log_run(
            run,
            status="succeeded",
            diagnostic="Recovered durable Note output after an expired Worker lease",
        )
        try:
            self.note_publisher.process(request_id=note_request_id)
        except Exception as exc:  # noqa: BLE001 - Note state is independently durable
            self._log_run(
                run,
                status="note_publication_deferred",
                level=logging.WARNING,
                diagnostic=exc,
            )
        if plan is not None and plan.state == "frozen":
            try:
                self.auto_fix_publisher.process(plan_id=plan.plan_id)
            except Exception as exc:  # noqa: BLE001 - Plan state is independently durable
                self._log_run(
                    run,
                    status="autofix_publication_deferred",
                    level=logging.WARNING,
                    diagnostic=exc,
                )
        return WorkerResult(
            run.run_id,
            "succeeded",
            detail="recovered durable Agent effects after Worker lease expiry",
        )

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
        auto_fix_publication = self.auto_fix_publisher.process()
        if auto_fix_publication is not None:
            return WorkerResult(
                auto_fix_publication.run_id,
                auto_fix_publication.status,
                detail=auto_fix_publication.detail,
            )
        run = self.store.claim_next(
            lease_seconds=self.lease_seconds,
            max_attempts=self.max_attempts,
            project_id=self.project_id,
        )
        if run is None:
            note_results = self.note_publisher.process(limit=1)
            if note_results:
                note_result = note_results[-1]
                return WorkerResult(
                    note_result.run_id,
                    note_result.status,
                    detail=note_result.detail,
                )
            note_reconciliation = self.note_publisher.reconcile_published()
            if note_reconciliation is not None:
                return WorkerResult(
                    note_reconciliation.run_id,
                    note_reconciliation.status,
                    detail=note_reconciliation.detail,
                )
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
        note_request_id: int | None = None
        auto_fix_plan_id: int | None = None

        auto_fix_intent: AutoFixPublicationIntent | None = None
        try:
            if run.project_id != self.project_id:
                raise ValueError("Review Run belongs to a different configured Project")
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

            auto_fix_relation = self.store.get_program_auto_fix_relation_by_child(
                run.project_id, run.mr_iid
            )
            if (
                auto_fix_relation is not None
                or parse_auto_fix_branch(merge_request.source_branch) is not None
                or parse_auto_fix_marker(merge_request.description) is not None
            ):
                detail = "Auto-fix child does not create or execute a Review Run"
                if not self._finish_terminal(run, "cancelled", detail, "autofix_child"):
                    return self._lease_recovery_result(run, detail)
                return WorkerResult(run.run_id, "cancelled", detail=detail)

            current_sha = merge_request.sha.lower()
            if current_sha != run.sha:
                detail = f"stale run SHA; current Merge Request head is {current_sha}"
                current_run = self.store.cancel_and_enqueue_latest(
                    run_id=run.run_id,
                    project_id=run.project_id,
                    mr_iid=run.mr_iid,
                    sha=current_sha,
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

            frozen_note = self.store.get_note_publication_for_run(run.run_id)
            if frozen_note is not None:
                return self._resume_from_frozen_note(
                    run,
                    self.store.get_auto_fix_publication_intent(run.run_id),
                    frozen_note.request_id,
                )

            self.sha_ensurer.ensure_sha(run.sha)
            if self.auto_fix_enabled:
                auto_fix_intent = self.store.create_auto_fix_publication_intent(
                    run.run_id, run.claim_token
                )
                if auto_fix_intent is None:
                    raise LeaseLostError(
                        "Review Run lease was lost before creating the Auto-fix Intent"
                    )

            worktree_id = str(run.run_id) if run.attempts <= 1 else f"{run.run_id}-attempt-{run.attempts}"
            worktree = self.worktrees.path_for(worktree_id)
            if worktree.exists():
                raise WorktreeError(f"worktree path already exists: {worktree}")
            cleanup_record = self.store.register_worktree(run.run_id, worktree, run.sha)
            detail = ""
            worktree_created = False
            completed_result: WorkerResult | None = None
            try:
                try:
                    self.worktrees.create(run.sha, worktree_id)
                except Exception as exc:
                    self.store.mark_worktree_not_created(cleanup_record.cleanup_id, str(exc))
                    raise
                worktree_created = True
                receipts = self._prepare_agent_effects(run, auto_fix_intent)
                if (
                    auto_fix_intent is not None
                    and merge_request.source_project_id != run.project_id
                ):
                    result = self._run_agent(run, worktree, auto_fix_enabled=False)
                else:
                    result = self._run_agent(run, worktree)
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
                if result.output is None:
                    raise AgentContractError(
                        "successful Agent result did not contain parsed output"
                    )
                try:
                    note_request = self.store.create_note_publication_request(
                        run.run_id,
                        receipts["note_expected"].receipt_id,
                        result.output.review_markdown,
                        run.claim_token,
                    )
                except ValueError as exc:
                    raise AgentContractError(str(exc)) from exc
                if note_request is None:
                    raise LeaseLostError(
                        "Review Run lease was lost before freezing the Note publication request"
                    )
                note_request_id = note_request.request_id
                if auto_fix_intent is not None:
                    self._freeze_auto_fix_plan(
                        run,
                        merge_request,
                        worktree,
                        result,
                        auto_fix_intent,
                        receipts["autofix_plan"],
                    )
                    frozen_plan = self.store.get_auto_fix_publication_plan(run.run_id)
                    auto_fix_plan_id = (
                        frozen_plan.plan_id
                        if frozen_plan is not None and frozen_plan.state == "frozen"
                        else None
                    )
                if not self.store.finish(run.run_id, "succeeded", claim_token=run.claim_token):
                    raise LeaseLostError("Review Run lease lost before Agent result completion")
                self._log_run(run, status="succeeded", diagnostic="Agent process exited successfully")
                completed_result = WorkerResult(run.run_id, "succeeded", result)
            finally:
                if worktree_created:
                    try:
                        self.worktrees.remove(worktree)
                        self.store.mark_worktree_cleaned(cleanup_record.cleanup_id)
                    except Exception as exc:
                        cleanup_detail = self._redact(exc)
                        self.store.mark_worktree_cleanup_pending(
                            cleanup_record.cleanup_id, cleanup_detail
                        )
                        detail = f"{detail}; worktree cleanup pending: {cleanup_detail}".strip("; ")
                        LOGGER.exception("worktree cleanup failed for Run %s", run.run_id)
            if completed_result is not None:
                if note_request_id is not None:
                    try:
                        self.note_publisher.process(request_id=note_request_id)
                    except Exception as exc:  # noqa: BLE001 - Note state is independently durable
                        self._log_run(
                            run,
                            status="note_publication_deferred",
                            level=logging.WARNING,
                            diagnostic=exc,
                        )
                if auto_fix_plan_id is not None:
                    try:
                        self.auto_fix_publisher.process(plan_id=auto_fix_plan_id)
                    except Exception as exc:  # noqa: BLE001 - Plan state is independently durable
                        self._log_run(
                            run,
                            status="autofix_publication_deferred",
                            level=logging.WARNING,
                            diagnostic=exc,
                        )
                return WorkerResult(
                    completed_result.run_id,
                    completed_result.status,
                    completed_result.agent_result,
                    detail,
                )
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
                delay_seconds=self._backoff(run.failure_attempts + 1),
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
        delay = self._backoff(run.failure_attempts + 1)
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

    def _resolve_auto_fix_plan(
        self,
        run: ReviewRun,
        receipt: EffectReceipt,
        state: str,
        detail: str,
        *,
        error: str | None = None,
    ) -> None:
        if not self.store.resolve_auto_fix_publication_intent(
            run.run_id,
            receipt.receipt_id,
            state,
            detail,
            run.claim_token,
            error=error,
        ):
            raise LeaseLostError(
                "Review Run lease was lost before resolving the Auto-fix Intent"
            )

    def _freeze_auto_fix_plan(
        self,
        run: ReviewRun,
        merge_request,
        worktree: Path,
        result: AgentResult,
        intent: AutoFixPublicationIntent,
        receipt: EffectReceipt,
    ) -> None:
        assert result.output is not None
        proposal = result.output.autofix_proposal
        if merge_request.source_project_id != run.project_id:
            self._resolve_auto_fix_plan(
                run,
                receipt,
                "blocked",
                "Auto-fix is unsupported for a fork-source Merge Request",
                error="parent Merge Request source Project differs from the bound Project",
            )
            return
        try:
            before = capture_diff_envelope(worktree, run.sha)
            if proposal is None:
                if before.actions:
                    raise DiffEnvelopeError(
                        "Agent changed the workspace without declaring an Auto-fix proposal"
                    )
                self._resolve_auto_fix_plan(
                    run,
                    receipt,
                    "no_proposal",
                    "Agent returned no Auto-fix proposal and left no workspace diff",
                )
                return
            if not before.actions:
                raise DiffEnvelopeError(
                    "Agent declared an Auto-fix proposal without a workspace diff"
                )
            verification = self.agent_backend.verification if self.agent_backend else None
            if verification is None:
                raise DiffEnvelopeError(
                    "Auto-fix publication requires an Operator-owned verifier"
                )
            evidence = run_verifier(
                verification.command,
                worktree,
                self.agent_backend.environment,
                before.candidate_sha256,
                timeout_seconds=self.agent_timeout_seconds,
                heartbeat=lambda: self._heartbeat(run),
                heartbeat_interval_seconds=max(
                    0.1, min(30.0, self.lease_seconds / 3)
                ),
            )
            after = capture_diff_envelope(worktree, run.sha)
            if after != before:
                raise DiffEnvelopeError(
                    "Operator verifier mutated the Auto-fix candidate"
                )
        except DiffEnvelopeError as exc:
            self._resolve_auto_fix_plan(
                run,
                receipt,
                "blocked",
                "Auto-fix proposal was blocked before publication planning",
                error=str(exc),
            )
            return

        payload = build_publication_plan_payload(
            project_id=run.project_id,
            mr_iid=run.mr_iid,
            sha=run.sha,
            generation=intent.generation,
            nonce=intent.nonce,
            proposal_summary=proposal.summary,
            envelope=after,
            verification=evidence,
            target_branch=merge_request.source_branch,
            parent_title=merge_request.title,
            initial_readiness=self.autofix_publication.initial_readiness,
            ci_trigger=self.autofix_publication.ci_trigger,
        )
        plan = self.store.freeze_auto_fix_publication_plan(
            run.run_id,
            receipt.receipt_id,
            payload,
            run.claim_token,
        )
        if plan is None:
            raise LeaseLostError(
                "Review Run lease was lost before freezing the Auto-fix Publication Plan"
            )

    def _prepare_agent_effects(
        self,
        run: ReviewRun,
        auto_fix_intent: AutoFixPublicationIntent | None,
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
                detail="Local Agent execution requires strict RESULT_PATH v1 output",
            )
            receipts["note_expected"] = self.store.create_effect_receipt(
                run.run_id,
                "note_expected",
                self._agent_note_marker(run),
                attempt=1,
                state="planned",
                detail="Program-owned GitLab Note awaits a valid Agent result",
            )
            if auto_fix_intent is not None:
                receipts["autofix_plan"] = self.store.create_effect_receipt(
                    run.run_id,
                    "autofix_plan",
                    f"autofix-intent:{auto_fix_intent.nonce}",
                    attempt=attempt,
                    state="planned",
                    detail="Program-owned Auto-fix Intent awaits a verified workspace diff",
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
            if kind in {"agent_execution", "autofix_plan"}:
                continue
            self._update_run_effect_receipt(
                run,
                receipt,
                state="not_observed",
                error=self._redact(error),
            )

    def _heartbeat(self, run: ReviewRun) -> bool:
        return self.store.extend_lease(
            run.run_id,
            run.claim_token,
            lease_seconds=self.lease_seconds,
        )

    def reconcile_auto_fixes(self, limit: int = 1) -> WorkerResult | None:
        results = self.auto_fix_reconciler.process(limit=limit)
        if not results:
            return None
        result = results[-1]
        return WorkerResult(
            result.run_id,
            result.status,
            detail=result.detail or "",
        )

    def run_forever(self, poll_interval_seconds: float = 5.0) -> None:
        if poll_interval_seconds <= 0:
            raise ValueError("Worker poll interval must be positive")
        stop_event = Event()
        failures: Queue[tuple[str, BaseException]] = Queue()

        def review_publication_loop() -> None:
            while not stop_event.is_set():
                result = self.process_one()
                if result is None or result.status == "deferred":
                    stop_event.wait(poll_interval_seconds)

        def reconciliation_cleanup_loop() -> None:
            while not stop_event.is_set():
                cleanup_count = self.process_worktree_cleanup(limit=1)
                reconciliation = self.reconcile_auto_fixes(limit=1)
                if cleanup_count == 0 and reconciliation is None:
                    stop_event.wait(poll_interval_seconds)

        def supervise(name: str, target: Callable[[], None]) -> None:
            try:
                target()
            except BaseException as exc:  # noqa: BLE001 - any loop exit restarts the Worker
                failures.put((name, exc))
                stop_event.set()
            else:
                if not stop_event.is_set():
                    failures.put((name, RuntimeError(f"{name} exited unexpectedly")))
                    stop_event.set()

        loops = (
            (
                "Review/Publication loop",
                review_publication_loop,
            ),
            (
                "Notification loop",
                lambda: self.notification_dispatcher.run_forever(
                    poll_interval_seconds=min(max(poll_interval_seconds, 0.1), 1.0),
                    stop_event=stop_event,
                ),
            ),
            (
                "Reconciliation/Cleanup loop",
                reconciliation_cleanup_loop,
            ),
        )
        threads = [
            Thread(
                target=supervise,
                args=(name, target),
                name=f"reviewflow-{name.lower().replace('/', '-').replace(' ', '-')}",
                daemon=True,
            )
            for name, target in loops
        ]
        for thread in threads:
            thread.start()
        try:
            while True:
                try:
                    name, error = failures.get(timeout=0.5)
                except Empty:
                    continue
                raise RuntimeError(f"{name} failed; supervised Worker must restart") from error
        finally:
            stop_event.set()
            for thread in threads:
                thread.join(timeout=1.0)

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

    def _run_agent(
        self,
        run: ReviewRun,
        worktree: Path,
        *,
        auto_fix_enabled: bool | None = None,
    ) -> AgentResult:
        if self.agent_backend is None:
            raise ValueError("AI Review has no Operator-authorized Agent Backend/Profile")
        runner = LocalCliAgentRunner(self.agent_backend.command, self.agent_timeout_seconds)
        skill_path = DEFAULT_REVIEW_AGENT_SKILL
        if self.policy.ai.skill_path:
            configured = Path(self.policy.ai.skill_path)
            configured_path = configured if configured.is_absolute() else worktree / configured
            if configured_path.is_file():
                skill_path = configured_path
        if not self._heartbeat(run):
            raise LeaseLostError("Review Run lease could not be extended before Agent execution")
        result_root = self.worktrees.root / "agent-results"
        result_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(result_root, 0o700)
        with tempfile.TemporaryDirectory(prefix=f"run-{run.run_id}-", dir=result_root) as directory:
            os.chmod(directory, 0o700)
            return runner.run(
                AgentContext(
                    project_id=run.project_id,
                    mr_iid=run.mr_iid,
                    run_id=run.run_id,
                    sha=run.sha,
                    worktree=worktree,
                    result_path=Path(directory) / "result.json",
                    skill_path=skill_path,
                    auto_fix_enabled=(
                        self.auto_fix_enabled
                        if auto_fix_enabled is None
                        else auto_fix_enabled
                    ),
                ),
                heartbeat=lambda: self._heartbeat(run),
                heartbeat_interval_seconds=max(
                    0.1, min(30.0, self.lease_seconds / 3)
                ),
                extra_env=self.agent_backend.environment,
            )

    def _notify_humans(self, run: ReviewRun, merge_request) -> None:
        if not self.policy.human.enabled:
            return
        self.notification_dispatcher.enqueue(
            NotificationEvent(
                title=f"Review requested: !{merge_request.iid} {merge_request.title}",
                body="A human review is requested.",
                url=merge_request.web_url,
                recipients=self.policy.human.reviewers,
                correlation_id=f"review-run:{run.run_id}:human",
            )
        )

    def _notify_autofix(self, run: ReviewRun, parent_merge_request, auto_fix_merge_request) -> None:
        self.notification_dispatcher.enqueue(
            NotificationEvent(
                title=f"Auto-fix review: !{auto_fix_merge_request.iid} {auto_fix_merge_request.title}",
                body="An Auto-fix Merge Request is ready for the original MR creator to review.",
                url=auto_fix_merge_request.web_url,
                recipients=(parent_merge_request.author_username,),
                correlation_id=f"review-run:{run.run_id}:autofix",
            )
        )
