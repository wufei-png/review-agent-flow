"""Exceptional reconciliation for Program-owned Auto-fix publication."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .failures import classify_exception
from .gitlab import GitLabClient, MergeRequest, is_valid_sha
from .notifications import FanoutNotifier
from .policy import policy_from_json
from .security import redact
from .store import (
    AUTO_FIX_MR_ABSENCE_CONFIRMATION_SECONDS,
    DEFAULT_AUTO_FIX_RECONCILIATION_LEASE_SECONDS,
    AutoFixPublicationPlan,
    ProgramAutoFixRelation,
    SQLiteStore,
)


@dataclass(frozen=True)
class AutoFixReconciliationResult:
    run_id: int
    status: str
    detail: str | None = None


class AutoFixReconciler:
    """Recover uncertain writes and refresh only already-authorized Relations."""

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
        relation_refresh_seconds: float = 300.0,
        notifier: FanoutNotifier | None = None,
        secret_provider: Callable[[], Iterable[str]] | None = None,
    ) -> None:
        if not project_id.strip() or run_lease_seconds <= 0 or max_attempts <= 0:
            raise ValueError("invalid Auto-fix Reconciler configuration")
        if min(retry_delay_seconds, max_retry_delay_seconds, relation_refresh_seconds) < 0:
            raise ValueError("Auto-fix Reconciler delays cannot be negative")
        self.store = store
        self.gitlab = gitlab
        self.project_id = project_id.strip()
        self.lease_seconds = min(
            DEFAULT_AUTO_FIX_RECONCILIATION_LEASE_SECONDS,
            max(run_lease_seconds, 1),
        )
        self.max_attempts = max_attempts
        self.retry_delay_seconds = retry_delay_seconds
        self.max_retry_delay_seconds = max_retry_delay_seconds
        self.relation_refresh_seconds = relation_refresh_seconds
        self.notifier = notifier or FanoutNotifier()
        self._secret_provider = secret_provider or (lambda: ())

    def _secrets(self) -> tuple[str, ...]:
        return tuple(
            value
            for value in self._secret_provider()
            if value and not value.isspace()
        )

    def _redact(self, value: object) -> str:
        return redact(value, secrets=self._secrets())

    def _delay(self, attempt: int) -> float:
        return min(
            self.retry_delay_seconds * (2 ** max(attempt - 1, 0)),
            self.max_retry_delay_seconds,
        )

    @staticmethod
    def _payload(plan: AutoFixPublicationPlan) -> dict[str, object]:
        if plan.plan_json is None:
            raise ValueError("uncertain Auto-fix Plan payload was already purged")
        value = json.loads(plan.plan_json)
        if not isinstance(value, dict):
            raise TypeError("uncertain Auto-fix Plan payload is invalid")
        return value

    def _defer_plan(
        self,
        plan: AutoFixPublicationPlan,
        detail: str,
        *,
        confirm_absent: bool = False,
    ) -> AutoFixReconciliationResult:
        retrying = self.store.defer_auto_fix_reconciliation_plan(
            plan.plan_id,
            plan.claim_token,
            detail,
            delay_seconds=self._delay(plan.attempts),
            max_attempts=self.max_attempts,
            confirm_absent=confirm_absent,
        )
        return AutoFixReconciliationResult(
            plan.run_id,
            "deferred" if retrying else "blocked",
            self._redact(detail),
        )

    def _block_plan(
        self, plan: AutoFixPublicationPlan, detail: str
    ) -> AutoFixReconciliationResult:
        persisted = self.store.finish_auto_fix_publication_plan(
            plan.plan_id,
            plan.claim_token,
            state="blocked",
            error=detail,
        )
        return AutoFixReconciliationResult(
            plan.run_id,
            "blocked" if persisted else "deferred",
            self._redact(detail),
        )

    def _validate_child(
        self,
        plan: AutoFixPublicationPlan,
        child: MergeRequest,
        *,
        branch: str,
        target_branch: str,
    ) -> str | None:
        if (
            child.project_id != self.project_id
            or child.iid <= 0
            or child.source_branch != branch
            or child.target_branch != target_branch
            or plan.marker not in child.description
            or child.sha != plan.branch_commit_sha
            or not child.author_username.strip()
            or not child.web_url
        ):
            return "reconciled Auto-fix MR identity conflicts with the frozen Plan"
        state = str(child.state or "opened").lower()
        if state not in {"opened", "closed", "merged"}:
            return "reconciled Auto-fix MR returned an unknown state"
        if state == "merged" and not is_valid_sha(child.merged_sha):
            return "reconciled merged Auto-fix MR has no valid merge SHA"
        return None

    def _complete_plan(
        self,
        plan: AutoFixPublicationPlan,
        child: MergeRequest,
        *,
        branch: str,
        target_branch: str,
        parent_author: str,
    ) -> AutoFixReconciliationResult:
        snapshot = self.store.get_policy_snapshot(plan.run_id)
        requested_channels = (
            policy_from_json(snapshot.policy_json).notifications.channels
            if snapshot is not None
            else None
        )
        try:
            channel_specs = self.notifier.channel_specs(requested_channels)
        except ValueError:
            # A recovered remote MR is stronger evidence than a now-disabled
            # notification binding. Persist the Relation and leave delivery empty.
            channel_specs = ()
        completed = self.store.complete_auto_fix_publication(
            plan.plan_id,
            plan.claim_token,
            auto_fix_project_id=child.project_id,
            auto_fix_mr_iid=child.iid,
            source_branch=child.source_branch,
            target_branch=child.target_branch,
            marker=plan.marker,
            title=f"Auto-fix review: !{child.iid} {child.title}",
            body="A Program-owned Auto-fix Merge Request is ready for review.",
            url=child.web_url,
            recipients=(parent_author,),
            channels=channel_specs,
        )
        if completed is None:
            return AutoFixReconciliationResult(
                plan.run_id,
                "deferred",
                "Auto-fix reconciliation lease was lost before Relation persistence",
            )
        state = str(child.state or "opened").lower()
        self.store.observe_program_auto_fix_webhook(
            child.project_id,
            child.iid,
            status=state,
            merged_sha=child.merged_sha,
            source_branch=branch,
            target_branch=target_branch,
            description=child.description,
        )
        return AutoFixReconciliationResult(
            plan.run_id,
            "succeeded",
            f"reconciled Auto-fix Merge Request !{child.iid}",
        )

    def _reconcile_plan(
        self, plan: AutoFixPublicationPlan
    ) -> AutoFixReconciliationResult:
        try:
            payload = self._payload(plan)
            review_key = payload["review_key"]
            if not isinstance(review_key, dict):
                raise TypeError("uncertain Auto-fix ReviewKey is invalid")
            project_id = str(review_key["project_id"])
            mr_iid = int(review_key["mr_iid"])
            sha = str(review_key["sha"])
            branch = str(payload["branch"])
            target_branch = str(payload["target_branch"])
            title = str(payload["title"])
            readiness = str(payload["initial_readiness"])
            if project_id != self.project_id:
                raise ValueError("uncertain Auto-fix Plan belongs to another Project")
            if not is_valid_sha(sha):
                raise ValueError("uncertain Auto-fix ReviewKey SHA is invalid")
            if readiness not in {"draft", "ready"}:
                raise ValueError("uncertain Auto-fix readiness is invalid")
            parent = self.gitlab.get_merge_request(project_id, mr_iid)
            if (
                parent.project_id != project_id
                or parent.iid != mr_iid
                or not parent.author_username.strip()
            ):
                raise ValueError("Auto-fix parent identity is invalid during reconciliation")
            if plan.external_mr_iid is not None:
                child = self.gitlab.get_merge_request(project_id, plan.external_mr_iid)
            else:
                candidate = self.gitlab.find_merge_request_by_source_branch(
                    project_id, branch, target_branch, plan.marker
                )
                child = (
                    self.gitlab.get_merge_request(project_id, candidate.iid)
                    if candidate is not None
                    else None
                )
            if child is None:
                if plan.branch_commit_sha is None:
                    return self._defer_plan(
                        plan,
                        "uncertain Auto-fix commit has no confirmed MR or persisted commit identity",
                    )
                if plan.absence_confirmations < 1:
                    return self._defer_plan(
                        plan,
                        "Auto-fix MR absence requires a second delayed confirmation",
                        confirm_absent=True,
                    )
                if (
                    str(parent.state or "opened").lower() != "opened"
                    or parent.source_project_id != project_id
                    or parent.source_branch != target_branch
                    or parent.sha != sha
                ):
                    return self._block_plan(
                        plan,
                        "parent MR no longer permits recovery of a missing Auto-fix MR",
                    )
                if plan.write_phase == "mr_write_started":
                    observed_at = (
                        datetime.fromisoformat(plan.absence_observed_at)
                        if plan.absence_observed_at is not None
                        else None
                    )
                    if observed_at is None or observed_at > datetime.now(UTC) - timedelta(
                        seconds=AUTO_FIX_MR_ABSENCE_CONFIRMATION_SECONDS
                    ):
                        return self._defer_plan(
                            plan,
                            "Auto-fix MR absence requires the minimum safety interval",
                        )
                    if not self.store.confirm_auto_fix_merge_request_absent(
                        plan.plan_id, plan.claim_token
                    ):
                        return AutoFixReconciliationResult(
                            plan.run_id,
                            "deferred",
                            "Auto-fix reconciliation lease was lost while confirming MR absence",
                        )
                if not self.store.extend_auto_fix_publication_lease(
                    plan.plan_id, plan.claim_token, self.lease_seconds
                ):
                    return AutoFixReconciliationResult(
                        plan.run_id,
                        "deferred",
                        "Auto-fix reconciliation lease was lost before MR recovery",
                    )
                if not self.store.mark_auto_fix_write_started(
                    plan.plan_id, plan.claim_token, operation="merge_request"
                ):
                    return AutoFixReconciliationResult(
                        plan.run_id,
                        "deferred",
                        "Auto-fix reconciliation lease was lost before recording the MR write boundary",
                    )
                child = self.gitlab.create_merge_request(
                    project_id,
                    title,
                    branch,
                    target_branch,
                    plan.marker,
                    draft=readiness == "draft",
                )
                child = self.gitlab.get_merge_request(project_id, child.iid)
            identity_error = self._validate_child(
                plan, child, branch=branch, target_branch=target_branch
            )
            if identity_error is not None:
                return self._block_plan(plan, identity_error)
            if plan.external_mr_iid is None and not self.store.record_auto_fix_merge_request(
                plan.plan_id, plan.claim_token, child.iid
            ):
                return AutoFixReconciliationResult(
                    plan.run_id,
                    "deferred",
                    "Auto-fix reconciliation lease was lost after confirming the MR identity",
                )
            return self._complete_plan(
                plan,
                child,
                branch=branch,
                target_branch=target_branch,
                parent_author=parent.author_username,
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            return self._block_plan(plan, str(exc))
        except Exception as exc:  # noqa: BLE001 - exceptional work remains durable
            failure = classify_exception(exc, secrets=self._secrets())
            if failure.retryable:
                return self._defer_plan(plan, failure.detail)
            return self._block_plan(plan, failure.detail)

    def _defer_relation(
        self, relation: ProgramAutoFixRelation, detail: str
    ) -> AutoFixReconciliationResult:
        persisted = self.store.defer_program_auto_fix_relation_reconciliation(
            relation.relation_id,
            relation.reconcile_claim_token,
            detail,
            retry_delay_seconds=self._delay(relation.reconcile_attempts),
        )
        return AutoFixReconciliationResult(
            relation.run_id,
            "deferred",
            self._redact(detail) if persisted else "Relation reconciliation lease was lost",
        )

    def _reconcile_relation(
        self, relation: ProgramAutoFixRelation
    ) -> AutoFixReconciliationResult:
        try:
            child = self.gitlab.get_merge_request(
                relation.auto_fix_project_id, relation.auto_fix_mr_iid
            )
            state = str(child.state or "opened").lower()
            if (
                child.project_id != relation.auto_fix_project_id
                or child.iid != relation.auto_fix_mr_iid
                or not child.author_username.strip()
                or state not in {"opened", "closed", "merged"}
                or (state == "merged" and not is_valid_sha(child.merged_sha))
            ):
                raise ValueError("known Auto-fix Relation returned conflicting identity")
            persisted = self.store.finish_program_auto_fix_relation_reconciliation(
                relation.relation_id,
                relation.reconcile_claim_token,
                status=state,
                merged_sha=child.merged_sha,
                source_branch=child.source_branch,
                target_branch=child.target_branch,
                description=child.description,
                source_sha=child.sha,
                retry_delay_seconds=self.relation_refresh_seconds,
            )
            if not persisted:
                return AutoFixReconciliationResult(
                    relation.run_id,
                    "deferred",
                    "Relation reconciliation lease was lost",
                )
            current = self.store.get_program_auto_fix_relation(relation.plan_id)
            detail = "Auto-fix Relation refreshed"
            if current is not None and current.divergence:
                detail = f"Auto-fix Relation diverged: {current.divergence}"
            return AutoFixReconciliationResult(relation.run_id, "succeeded", detail)
        except Exception as exc:  # noqa: BLE001 - relation refresh is independently retried
            return self._defer_relation(relation, str(exc))

    def process(self, limit: int = 1) -> tuple[AutoFixReconciliationResult, ...]:
        if limit <= 0:
            raise ValueError("Auto-fix reconciliation limit must be positive")
        results: list[AutoFixReconciliationResult] = []
        for _ in range(limit):
            plan = self.store.claim_auto_fix_reconciliation_plan(
                project_id=self.project_id,
                lease_seconds=self.lease_seconds,
            )
            if plan is not None:
                results.append(self._reconcile_plan(plan))
                continue
            relation = self.store.claim_program_auto_fix_relation(
                project_id=self.project_id,
                lease_seconds=self.lease_seconds,
            )
            if relation is None:
                break
            results.append(self._reconcile_relation(relation))
        return tuple(results)
