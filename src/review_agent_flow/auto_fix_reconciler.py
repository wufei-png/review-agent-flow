"""Independent Auto-fix Merge Request reconciliation."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .events import parse_auto_fix_branch
from .failures import classify_exception
from .gitlab import GitLabClient, is_valid_sha
from .runner import LeaseLostError
from .security import redact
from .store import (
    DEFAULT_EFFECT_LEASE_SECONDS,
    EffectReceipt,
    ReviewRun,
    SQLiteStore,
    auto_fix_intent_marker,
)
from .structured_logging import log_event

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class AutoFixReconciliationResult:
    run_id: int
    status: str
    detail: str | None = None


class AutoFixReconciler:
    """Observe Agent-owned Auto-fix MRs and advance their Effect Receipts."""

    def __init__(
        self,
        store: SQLiteStore,
        gitlab: GitLabClient,
        project_id: str,
        *,
        run_lease_seconds: int,
        max_attempts: int = 6,
        retry_delay_seconds: float = 5.0,
        max_retry_delay_seconds: float = 300.0,
        secret_provider: Callable[[], Iterable[str]] | None = None,
    ) -> None:
        if not project_id or not project_id.strip():
            raise ValueError("Auto-fix reconciler project_id cannot be empty")
        if run_lease_seconds <= 0:
            raise ValueError("Auto-fix reconciler run_lease_seconds must be positive")
        if max_attempts <= 0:
            raise ValueError("Auto-fix reconciler max_attempts must be positive")
        if retry_delay_seconds < 0:
            raise ValueError("Auto-fix reconciler retry_delay_seconds cannot be negative")
        if max_retry_delay_seconds < 0:
            raise ValueError("Auto-fix reconciler max_retry_delay_seconds cannot be negative")
        self.store = store
        self.gitlab = gitlab
        self.project_id = project_id.strip()
        self.run_lease_seconds = run_lease_seconds
        self.max_attempts = max_attempts
        self.retry_delay_seconds = retry_delay_seconds
        self.max_retry_delay_seconds = max_retry_delay_seconds
        self._secret_provider = secret_provider or (lambda: ())

    def _secrets(self) -> tuple[str, ...]:
        return tuple(
            secret
            for secret in self._secret_provider()
            if secret and not secret.isspace()
        )

    def _redact(self, value: object) -> str:
        return redact(value, secrets=self._secrets())

    def _log_run(
        self,
        run: ReviewRun,
        *,
        status: str,
        level: int = logging.INFO,
        diagnostic: object | None = None,
        **fields: object,
    ) -> None:
        log_event(
            LOGGER,
            level,
            secrets=self._secrets(),
            event="worker.autofix_reconcile",
            status=status,
            correlation_id=f"review-run:{run.run_id}",
            run_id=run.run_id,
            project_id=run.project_id,
            mr_iid=run.mr_iid,
            diagnostic=self._redact(diagnostic) if diagnostic is not None else None,
            attempt=run.attempts,
            **fields,
        )

    def _update_run_receipt(
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
            raise LeaseLostError(
                "Review Run lease was lost before updating an Auto-fix receipt"
            )

    def _backoff(self, attempt: int) -> float:
        return min(
            self.retry_delay_seconds * (2 ** max(attempt - 1, 0)),
            self.max_retry_delay_seconds,
        )

    def validate_identity(
        self,
        run: ReviewRun,
        parent_merge_request,
        auto_fix,
        *,
        parent_mr_iid: int,
        expected_branch: str,
        expected_run_id: int | None = None,
    ) -> str | None:
        """Return a failure detail unless every persisted Auto-fix identity agrees."""

        parsed = parse_auto_fix_branch(expected_branch)
        branch_run_id = run.run_id if expected_run_id is None else expected_run_id
        if parsed != (parent_mr_iid, branch_run_id):
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

    def record_observed(
        self,
        run: ReviewRun,
        auto_fix,
        reconciliation: EffectReceipt,
        *,
        claim_token: str | None = None,
        run_claim_token: str | None = None,
    ) -> None:
        """Persist the observed relation behind the active Run or receipt lease."""

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
            raise LeaseLostError(
                "Auto-fix reconciliation lease was lost before recording the MR"
            )
        external_identity = f"{auto_fix.project_id}!{auto_fix.iid}"
        detail = (
            f"Auto-fix MR observed with state={relation.status}; "
            f"url={self._redact(getattr(auto_fix, 'web_url', None))}"
        )
        if run_claim_token is not None:
            self._update_run_receipt(
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
            raise LeaseLostError(
                "Auto-fix reconciliation lease was lost before receipt completion"
            )
        for receipt in self.store.list_effect_receipts(run.run_id, "autofix_expected"):
            if run_claim_token is not None:
                self._update_run_receipt(
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

    def schedule(
        self,
        receipt: EffectReceipt,
        detail: str,
        *,
        claim_token: str | None = None,
        run: ReviewRun | None = None,
    ) -> bool:
        """Persist the next observation attempt, or exhaust as not observed."""

        safe_detail = self._redact(detail)
        if receipt.attempt >= self.max_attempts:
            terminal_detail = (
                "Auto-fix MR was not observed after "
                f"{self.max_attempts} reconciliation attempts"
            )
            if run is not None:
                self._update_run_receipt(
                    run,
                    receipt,
                    state="not_observed",
                    detail=terminal_detail,
                    error=safe_detail,
                )
            elif not self.store.update_effect_receipt(
                receipt.receipt_id,
                state="not_observed",
                detail=terminal_detail,
                error=safe_detail,
                next_attempt_at=None,
                claim_token=claim_token,
            ):
                raise LeaseLostError(
                    "Auto-fix reconciliation lease was lost before exhaustion"
                )
            return False
        next_attempt_at = (
            datetime.now(UTC) + timedelta(seconds=self._backoff(receipt.attempt))
        ).isoformat()
        if run is not None:
            self._update_run_receipt(
                run,
                receipt,
                state="pending",
                detail="Auto-fix MR not yet observable; reconciliation will retry",
                error=safe_detail,
                next_attempt_at=next_attempt_at,
            )
        elif not self.store.update_effect_receipt(
            receipt.receipt_id,
            state="pending",
            detail="Auto-fix MR not yet observable; reconciliation will retry",
            error=safe_detail,
            next_attempt_at=next_attempt_at,
            claim_token=claim_token,
        ):
            raise LeaseLostError(
                "Auto-fix reconciliation lease was lost before scheduling retry"
            )
        return True

    def record_if_created(
        self,
        run: ReviewRun,
        merge_request,
        receipts: dict[str, EffectReceipt],
    ) -> str | None:
        """Observe an Auto-fix MR immediately after the Agent returns."""

        branch = f"autofix/MR-{merge_request.iid}/run-{run.run_id}"
        reconciliation = receipts.get("autofix_reconciliation")
        if reconciliation is None:
            return "Auto-fix reconciliation receipt is missing"
        if reconciliation.dedupe_key != branch:
            detail = "Auto-fix reconciliation receipt branch does not match the Review Run"
            self._update_run_receipt(
                run,
                reconciliation,
                state="failed",
                detail="Auto-fix identity validation failed",
                error=detail,
            )
            return detail
        if not self.store.has_active_lease(run.run_id, run.claim_token):
            raise LeaseLostError("Review Run lease lost before Auto-fix reconciliation")
        try:
            auto_fix = self.gitlab.find_merge_request_by_source_branch(
                run.project_id,
                branch,
                merge_request.source_branch,
                auto_fix_intent_marker(run.run_id, run.sha),
            )
        except Exception as exc:  # noqa: BLE001 - receipt retains retryable API failures
            failure = classify_exception(exc, secrets=self._secrets())
            if failure.retryable:
                self.schedule(reconciliation, failure.detail, run=run)
            else:
                self._update_run_receipt(
                    run,
                    reconciliation,
                    state="failed",
                    error=failure.detail,
                    detail="Auto-fix reconciliation failed terminally",
                )
            return None
        if auto_fix is None:
            self.schedule(reconciliation, "Auto-fix MR was not observed yet", run=run)
            return None
        identity_error = self.validate_identity(
            run,
            merge_request,
            auto_fix,
            parent_mr_iid=run.mr_iid,
            expected_branch=branch,
        )
        if identity_error is not None:
            self._update_run_receipt(
                run,
                reconciliation,
                state="failed",
                detail="Auto-fix identity validation failed",
                error=identity_error,
            )
            return identity_error
        self.record_observed(
            run,
            auto_fix,
            reconciliation,
            run_claim_token=run.claim_token,
        )
        return None

    def _reject(
        self,
        run: ReviewRun,
        receipt: EffectReceipt,
        detail: str,
    ) -> AutoFixReconciliationResult:
        updated = self.store.update_effect_receipt(
            receipt.receipt_id,
            state="failed",
            detail="Auto-fix identity validation failed",
            error=detail,
            claim_token=receipt.claim_token,
        )
        if not updated:
            return AutoFixReconciliationResult(
                run.run_id,
                "deferred",
                "Auto-fix reconciliation lease lost; reclaim will recover",
            )
        self.store.abandon_auto_fix_intent(run.run_id)
        self._log_run(
            run,
            status="failed",
            level=logging.ERROR,
            diagnostic=detail,
            failure_class="autofix_identity",
        )
        return AutoFixReconciliationResult(run.run_id, "failed", detail)

    def _complete_missing(
        self,
        run: ReviewRun,
        receipt: EffectReceipt,
        detail: str,
    ) -> AutoFixReconciliationResult:
        scheduled = self.schedule(
            receipt,
            detail,
            claim_token=receipt.claim_token,
        )
        if not scheduled:
            self.store.abandon_auto_fix_intent(run.run_id)
        result_detail = (
            "Auto-fix reconciliation pending"
            if scheduled
            else "Auto-fix reconciliation exhausted; not observed"
        )
        self._log_run(
            run,
            status="deferred" if scheduled else "succeeded",
            diagnostic=result_detail,
        )
        return AutoFixReconciliationResult(
            run.run_id,
            "deferred" if scheduled else "succeeded",
            result_detail,
        )

    def _reconcile_receipt(
        self,
        receipt: EffectReceipt,
    ) -> AutoFixReconciliationResult:
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
                secrets=self._secrets(),
                event="worker.autofix_reconcile",
                status="failed",
                correlation_id=f"review-run:{receipt.run_id}",
                run_id=receipt.run_id,
                diagnostic="Review Run no longer exists",
            )
            return AutoFixReconciliationResult(
                receipt.run_id,
                "failed",
                "Review Run no longer exists",
            )
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
                status="failed",
                level=logging.ERROR,
                diagnostic=detail,
                failure_class="project_mismatch",
            )
            return AutoFixReconciliationResult(receipt.run_id, "failed", detail)
        expected_branch = f"autofix/MR-{run.mr_iid}/run-{run.run_id}"
        expected_receipt = next(
            (
                item
                for item in self.store.list_effect_receipts(
                    run.run_id, "autofix_expected"
                )
                if item.dedupe_key == expected_branch
            ),
            None,
        )
        if (
            parse_auto_fix_branch(receipt.dedupe_key) != (run.mr_iid, run.run_id)
            or receipt.dedupe_key != expected_branch
        ):
            return self._reject(
                run,
                receipt,
                "Auto-fix reconciliation branch does not match the Review Run",
            )
        if expected_receipt is None:
            return self._reject(
                run,
                receipt,
                "Auto-fix expected receipt is missing for reconciliation",
            )
        try:
            parent_merge_request = self.gitlab.get_merge_request(
                run.project_id, run.mr_iid
            )
            if (
                getattr(parent_merge_request, "project_id", None) != run.project_id
                or getattr(parent_merge_request, "iid", None) != run.mr_iid
                or getattr(parent_merge_request, "source_branch", None) in {None, ""}
            ):
                return self._reject(
                    run,
                    receipt,
                    "Auto-fix parent Merge Request identity does not match the Review Run",
                )
            auto_fix = self.gitlab.find_merge_request_by_source_branch(
                run.project_id,
                receipt.dedupe_key,
                parent_merge_request.source_branch,
                auto_fix_intent_marker(run.run_id, run.sha),
            )
            if auto_fix is None:
                return self._complete_missing(
                    run,
                    receipt,
                    "Auto-fix MR is still not observable",
                )
            identity_error = self.validate_identity(
                run,
                parent_merge_request,
                auto_fix,
                parent_mr_iid=run.mr_iid,
                expected_branch=expected_branch,
            )
            if identity_error is not None:
                return self._reject(run, receipt, identity_error)
            self.record_observed(
                run,
                auto_fix,
                receipt,
                claim_token=receipt.claim_token,
            )
            self._log_run(
                run,
                status="succeeded",
                diagnostic="Auto-fix reconciliation observed",
            )
            return AutoFixReconciliationResult(
                run.run_id,
                "succeeded",
                "Auto-fix reconciliation observed",
            )
        except LeaseLostError:
            return AutoFixReconciliationResult(
                run.run_id,
                "deferred",
                "Auto-fix reconciliation lease lost; reclaim will recover",
            )
        except Exception as exc:  # noqa: BLE001 - keep reconciliation independently retryable
            failure = classify_exception(exc, secrets=self._secrets())
            if failure.retryable:
                try:
                    result = self._complete_missing(run, receipt, failure.detail)
                except LeaseLostError:
                    return AutoFixReconciliationResult(
                        run.run_id,
                        "deferred",
                        "Auto-fix reconciliation lease lost; reclaim will recover",
                    )
                self._log_run(
                    run,
                    status=result.status,
                    level=logging.WARNING,
                    diagnostic=(
                        failure.detail
                        if result.status == "deferred"
                        else "Auto-fix reconciliation exhausted; not observed"
                    ),
                    failure_class=failure.failure_class,
                )
                return AutoFixReconciliationResult(
                    run.run_id,
                    result.status,
                    (
                        "Auto-fix reconciliation retry scheduled"
                        if result.status == "deferred"
                        else "Auto-fix reconciliation exhausted; not observed"
                    ),
                )
            updated = self.store.update_effect_receipt(
                receipt.receipt_id,
                state="failed",
                detail="Auto-fix reconciliation failed terminally",
                error=failure.detail,
                claim_token=receipt.claim_token,
            )
            if updated:
                self.store.abandon_auto_fix_intent(run.run_id)
            self._log_run(
                run,
                status="failed",
                level=logging.ERROR,
                diagnostic=failure.detail,
                failure_class=failure.failure_class,
            )
            return AutoFixReconciliationResult(run.run_id, "failed", failure.detail)

    def process(self, limit: int = 1) -> tuple[AutoFixReconciliationResult, ...]:
        """Claim and reconcile a bounded batch of pending Auto-fix receipts."""

        if limit <= 0:
            raise ValueError("Auto-fix reconciliation limit must be positive")
        receipts = self.store.claim_pending_effect_receipts(
            "autofix_reconciliation",
            limit=limit,
            lease_seconds=min(
                DEFAULT_EFFECT_LEASE_SECONDS,
                max(self.run_lease_seconds, 1),
            ),
            max_attempts=self.max_attempts,
            project_id=self.project_id,
        )
        return tuple(self._reconcile_receipt(receipt) for receipt in receipts)
