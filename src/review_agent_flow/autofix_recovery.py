"""Explicit, audited Operator recovery for Program-owned Auto-fix artifacts."""

from __future__ import annotations

from dataclasses import dataclass

from .events import parse_auto_fix_marker
from .gitlab import GitLabClient, is_valid_sha
from .publication_plan import PublicationPlanError, PublicationPlanV1
from .store import (
    NotificationRequest,
    OperatorPlanAction,
    ProgramAutoFixRelation,
    SQLiteStore,
)


@dataclass(frozen=True)
class OperatorContext:
    recovery_id: str
    reason: str
    principal: str
    hostname: str
    tool_version: str


class AutoFixRecoveryError(ValueError):
    """Raised when explicit recovery evidence is incomplete or conflicting."""


class AutoFixPlanOperator:
    """Apply explicit local Plan lifecycle decisions with immutable audit."""

    def __init__(self, store: SQLiteStore) -> None:
        self.store = store

    def transition(
        self,
        *,
        context: OperatorContext,
        plan_id: int,
        action: str,
    ) -> OperatorPlanAction:
        AutoFixRecoveryService._operator_context(context)
        audit = self.store.transition_auto_fix_publication_plan(
            plan_id,
            action_id=context.recovery_id,
            action=action,
            reason=context.reason,
            principal=context.principal,
            hostname=context.hostname,
            tool_version=context.tool_version,
        )
        if audit.result != "succeeded":
            raise AutoFixRecoveryError(audit.diagnostic or "Operator Plan action rejected")
        return audit


class AutoFixRecoveryService:
    """Validate GitLab evidence before creating any local recovery authority."""

    def __init__(
        self,
        store: SQLiteStore,
        gitlab: GitLabClient,
        review_bot_username: str,
    ) -> None:
        if not review_bot_username.strip():
            raise ValueError("Auto-fix recovery requires the configured Review Bot username")
        self.store = store
        self.gitlab = gitlab
        self.review_bot_username = review_bot_username.strip()

    @staticmethod
    def _operator_context(context: OperatorContext) -> None:
        if any(
            not value.strip()
            for value in (
                context.recovery_id,
                context.reason,
                context.principal,
                context.hostname,
                context.tool_version,
            )
        ):
            raise AutoFixRecoveryError("Operator recovery context cannot be empty")

    @staticmethod
    def _has_unique_marker(description: str, marker: str) -> bool:
        parsed = [
            value
            for line in description.splitlines()
            if (value := parse_auto_fix_marker(line)) is not None
        ]
        return len(parsed) == 1 and sum(line == marker for line in description.splitlines()) == 1

    @staticmethod
    def _plan(
        plan_json: str,
        *,
        project_id: str,
        parent_mr_iid: int,
        parent_sha: str,
        intent_nonce: str,
        plan_sha256: str,
    ) -> PublicationPlanV1:
        try:
            plan = PublicationPlanV1.from_json(plan_json, plan_sha256)
        except PublicationPlanError as exc:
            raise AutoFixRecoveryError(str(exc)) from exc
        if (
            plan.review_key.project_id != project_id
            or plan.review_key.mr_iid != parent_mr_iid
            or plan.review_key.sha != parent_sha
        ):
            raise AutoFixRecoveryError("Publication Plan ReviewKey conflicts with the command")
        if plan.intent.nonce != intent_nonce:
            raise AutoFixRecoveryError("Publication Plan Intent conflicts with the command")
        return plan

    def _audit_failure(
        self,
        context: OperatorContext,
        *,
        action: str,
        project_id: str,
        parent_mr_iid: int,
        parent_sha: str,
        intent_nonce: str,
        plan_sha256: str,
        child_mr_iid: int,
        diagnostic: str,
    ) -> None:
        try:
            self.store.record_operator_recovery_failure(
                recovery_id=context.recovery_id,
                action=action,
                project_id=project_id,
                parent_mr_iid=parent_mr_iid,
                parent_sha=parent_sha,
                intent_nonce=intent_nonce,
                plan_sha256=plan_sha256,
                child_project_id=project_id,
                child_mr_iid=child_mr_iid,
                reason=context.reason,
                principal=context.principal,
                hostname=context.hostname,
                tool_version=context.tool_version,
                diagnostic=diagnostic,
            )
        except ValueError:
            # A recovery ID is immutable; preserve the validation error from a replay.
            pass

    def recover_relation(
        self,
        *,
        context: OperatorContext,
        project_id: str,
        parent_mr_iid: int,
        parent_sha: str,
        intent_nonce: str,
        plan_json: str,
        plan_sha256: str,
        child_mr_iid: int,
    ) -> ProgramAutoFixRelation:
        self._operator_context(context)
        try:
            if (
                not project_id.strip()
                or parent_mr_iid <= 0
                or child_mr_iid <= 0
                or not is_valid_sha(parent_sha)
            ):
                raise AutoFixRecoveryError("Operator recovery identity is invalid")
            parent_sha = parent_sha.lower()
            plan = self._plan(
                plan_json,
                project_id=project_id,
                parent_mr_iid=parent_mr_iid,
                parent_sha=parent_sha,
                intent_nonce=intent_nonce,
                plan_sha256=plan_sha256,
            )
            actions = plan.actions
            branch = plan.branch
            marker = plan.marker
            target_branch = plan.target_branch
            parent = self.gitlab.get_merge_request(project_id, parent_mr_iid)
            if (
                parent.project_id != project_id
                or parent.iid != parent_mr_iid
                or parent.source_project_id != project_id
                or parent.source_branch != target_branch
                or not parent.author_username.strip()
            ):
                raise AutoFixRecoveryError(
                    "parent Merge Request conflicts with the Publication Plan"
                )
            child = self.gitlab.get_merge_request(project_id, child_mr_iid)
            state = str(child.state or "opened").lower()
            if (
                child.project_id != project_id
                or child.source_project_id != project_id
                or child.iid != child_mr_iid
                or child.author_username != self.review_bot_username
                or child.source_branch != branch
                or child.target_branch != target_branch
                or not self._has_unique_marker(child.description, marker)
                or not is_valid_sha(child.sha)
                or not child.web_url
                or state not in {"opened", "closed", "merged"}
                or (state == "merged" and not is_valid_sha(child.merged_sha))
            ):
                raise AutoFixRecoveryError(
                    "child Merge Request conflicts with the Publication Plan"
                )
            unique = self.gitlab.find_merge_request_by_source_branch(
                project_id, branch, target_branch, marker
            )
            if (
                unique is None
                or unique.project_id != project_id
                or unique.iid != child_mr_iid
                or unique.author_username != self.review_bot_username
                or unique.source_branch != branch
                or unique.target_branch != target_branch
                or not self._has_unique_marker(unique.description, marker)
            ):
                raise AutoFixRecoveryError("child Merge Request is not the unique Review Bot match")
            observation = self.gitlab.observe_repository_commit(project_id, child.sha)
            observed_actions = tuple(action.as_dict() for action in observation.actions)
            if (
                observation.project_id != project_id
                or observation.sha != child.sha.lower()
                or observation.parent_ids != (parent_sha,)
                or observed_actions != actions
            ):
                raise AutoFixRecoveryError(
                    "remote commit actions conflict with the Publication Plan"
                )
            return self.store.recover_program_auto_fix_relation(
                recovery_id=context.recovery_id,
                project_id=project_id,
                parent_mr_iid=parent_mr_iid,
                parent_sha=parent_sha,
                intent_nonce=intent_nonce,
                plan_json=plan_json,
                plan_sha256=plan_sha256,
                marker=marker,
                source_branch=branch,
                target_branch=target_branch,
                child_project_id=project_id,
                child_mr_iid=child_mr_iid,
                published_commit_sha=child.sha.lower(),
                status=state,
                merged_sha=child.merged_sha,
                reason=context.reason,
                principal=context.principal,
                hostname=context.hostname,
                tool_version=context.tool_version,
            )
        except Exception as exc:
            self._audit_failure(
                context,
                action="relation_recovery",
                project_id=project_id,
                parent_mr_iid=parent_mr_iid,
                parent_sha=parent_sha,
                intent_nonce=intent_nonce,
                plan_sha256=plan_sha256,
                child_mr_iid=child_mr_iid,
                diagnostic=str(exc),
            )
            raise

    def resend_notification(
        self,
        *,
        context: OperatorContext,
        project_id: str,
        child_mr_iid: int,
        channels: tuple[tuple[str, str], ...],
    ) -> NotificationRequest:
        self._operator_context(context)
        relation = self.store.get_program_auto_fix_relation_by_child(project_id, child_mr_iid)
        if relation is None:
            raise AutoFixRecoveryError("notification resend requires an existing local Relation")
        intent = self.store.get_auto_fix_publication_intent(relation.run_id)
        assert intent is not None
        try:
            child = self.gitlab.get_merge_request(project_id, child_mr_iid)
            parent = self.gitlab.get_merge_request(project_id, relation.parent_mr_iid)
            if (
                child.project_id != relation.auto_fix_project_id
                or child.iid != relation.auto_fix_mr_iid
                or child.author_username != self.review_bot_username
                or child.source_branch != relation.source_branch
                or child.target_branch != relation.target_branch
                or not self._has_unique_marker(child.description, relation.marker)
                or child.sha.lower() != relation.published_commit_sha
                or parent.project_id != relation.parent_project_id
                or parent.iid != relation.parent_mr_iid
                or not child.web_url
                or not parent.author_username.strip()
            ):
                raise AutoFixRecoveryError("current GitLab identity conflicts with the Relation")
            return self.store.create_auto_fix_notification_resend(
                relation.relation_id,
                recovery_id=context.recovery_id,
                title=f"Auto-fix review: !{child.iid} {child.title}",
                body="A Program-owned Auto-fix Merge Request is ready for review.",
                url=child.web_url,
                recipients=(parent.author_username,),
                channels=channels,
                reason=context.reason,
                principal=context.principal,
                hostname=context.hostname,
                tool_version=context.tool_version,
            )
        except Exception as exc:
            self._audit_failure(
                context,
                action="notification_resend",
                project_id=relation.parent_project_id,
                parent_mr_iid=relation.parent_mr_iid,
                parent_sha=relation.parent_sha,
                intent_nonce=intent.nonce,
                plan_sha256=relation.plan_sha256,
                child_mr_iid=relation.auto_fix_mr_iid,
                diagnostic=str(exc),
            )
            raise
