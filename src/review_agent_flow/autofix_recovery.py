"""Explicit, audited Operator recovery for Program-owned Auto-fix artifacts."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import PurePosixPath

from .autofix_planner import canonical_json
from .events import parse_auto_fix_branch, parse_auto_fix_marker
from .gitlab import GitLabClient, is_valid_sha
from .store import (
    NotificationRequest,
    OperatorPlanAction,
    ProgramAutoFixRelation,
    SQLiteStore,
)

_NONCE_RE = re.compile(r"^[0-9a-f]{32}$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_PLAN_KEYS = {
    "version",
    "review_key",
    "intent",
    "proposal_summary",
    "candidate_sha256",
    "actions",
    "verification",
    "branch",
    "marker_template",
    "commit_message",
    "title",
    "target_branch",
    "initial_readiness",
    "ci_trigger",
}


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
    def _path(value: object) -> str:
        if not isinstance(value, str):
            raise AutoFixRecoveryError("Publication Plan action path must be text")
        path = PurePosixPath(value)
        if (
            not value
            or str(path) != value
            or path.is_absolute()
            or "\\" in value
            or any(part in {"", ".", ".."} for part in path.parts)
        ):
            raise AutoFixRecoveryError("Publication Plan action path is not canonical")
        return value

    @classmethod
    def _actions(cls, value: object) -> tuple[dict[str, object], ...]:
        if not isinstance(value, list) or not 1 <= len(value) <= 100:
            raise AutoFixRecoveryError("Publication Plan actions are outside the bounded envelope")
        actions: list[dict[str, object]] = []
        paths: list[str] = []
        total_bytes = 0
        for raw in value:
            if not isinstance(raw, dict):
                raise AutoFixRecoveryError("Publication Plan action must be an object")
            action = raw.get("action")
            path = cls._path(raw.get("file_path"))
            if action == "delete":
                expected_keys = {"action", "file_path"}
            elif action == "chmod":
                expected_keys = {"action", "file_path", "execute_filemode"}
                if type(raw.get("execute_filemode")) is not bool:
                    raise AutoFixRecoveryError("chmod action requires an executable mode")
            elif action in {"create", "update"}:
                expected_keys = {
                    "action",
                    "file_path",
                    "content",
                    "execute_filemode",
                }
                content = raw.get("content")
                if not isinstance(content, str) or type(raw.get("execute_filemode")) is not bool:
                    raise AutoFixRecoveryError("content action has an invalid payload")
                content_bytes = content.encode("utf-8")
                if len(content_bytes) > 1024 * 1024 or "\0" in content:
                    raise AutoFixRecoveryError("content action exceeds the text envelope")
                total_bytes += len(content_bytes)
            else:
                raise AutoFixRecoveryError("Publication Plan action type is unsupported")
            if set(raw) != expected_keys:
                raise AutoFixRecoveryError("Publication Plan action fields are not canonical")
            paths.append(path)
            actions.append(dict(raw))
        if paths != sorted(paths) or len(set(paths)) != len(paths):
            raise AutoFixRecoveryError("Publication Plan actions must have unique sorted paths")
        if total_bytes > 5 * 1024 * 1024:
            raise AutoFixRecoveryError("Publication Plan actions exceed the aggregate text bound")
        return tuple(actions)

    @classmethod
    def _plan(
        cls,
        plan_json: str,
        *,
        project_id: str,
        parent_mr_iid: int,
        parent_sha: str,
        intent_nonce: str,
        plan_sha256: str,
    ) -> tuple[dict[str, object], tuple[dict[str, object], ...], str, str, str]:
        try:
            payload = json.loads(plan_json)
        except json.JSONDecodeError as exc:
            raise AutoFixRecoveryError("Publication Plan is not valid JSON") from exc
        if not isinstance(payload, dict) or set(payload) != _PLAN_KEYS:
            raise AutoFixRecoveryError("Publication Plan fields do not match version 1")
        if canonical_json(payload) != plan_json:
            raise AutoFixRecoveryError("Publication Plan must use canonical JSON")
        if _DIGEST_RE.fullmatch(plan_sha256) is None or hashlib.sha256(
            plan_json.encode("utf-8")
        ).hexdigest() != plan_sha256:
            raise AutoFixRecoveryError("Publication Plan digest does not match the exact payload")
        if (
            payload.get("version") != 1
            or payload.get("review_key")
            != {"project_id": project_id, "mr_iid": parent_mr_iid, "sha": parent_sha}
        ):
            raise AutoFixRecoveryError("Publication Plan ReviewKey conflicts with the command")
        intent = payload.get("intent")
        if (
            not isinstance(intent, dict)
            or set(intent) != {"generation", "nonce"}
            or type(intent.get("generation")) is not int
            or int(intent["generation"]) <= 0
            or intent.get("nonce") != intent_nonce
            or _NONCE_RE.fullmatch(intent_nonce) is None
        ):
            raise AutoFixRecoveryError("Publication Plan Intent conflicts with the command")
        actions = cls._actions(payload.get("actions"))
        candidate_sha256 = payload.get("candidate_sha256")
        candidate_value = {"version": 1, "base_sha": parent_sha, "actions": list(actions)}
        if (
            not isinstance(candidate_sha256, str)
            or _DIGEST_RE.fullmatch(candidate_sha256) is None
            or hashlib.sha256(canonical_json(candidate_value).encode("utf-8")).hexdigest()
            != candidate_sha256
        ):
            raise AutoFixRecoveryError("Publication Plan candidate digest is invalid")
        verification = payload.get("verification")
        if (
            not isinstance(verification, dict)
            or set(verification) != {"candidate_sha256", "command_sha256"}
            or verification.get("candidate_sha256") != candidate_sha256
            or not isinstance(verification.get("command_sha256"), str)
            or _DIGEST_RE.fullmatch(str(verification["command_sha256"])) is None
        ):
            raise AutoFixRecoveryError("Publication Plan verification evidence is invalid")
        branch = f"autofix/MR-{parent_mr_iid}/sha-{parent_sha}/intent-{intent_nonce}"
        marker_template = (
            f"<!-- reviewflow:v1;project={project_id};mr={parent_mr_iid};sha={parent_sha};"
            f"effect=autofix;intent={intent_nonce};plan_sha256={{plan_sha256}} -->"
        )
        marker = marker_template.replace("{plan_sha256}", plan_sha256)
        if (
            payload.get("branch") != branch
            or payload.get("marker_template") != marker_template
            or parse_auto_fix_branch(branch) != (parent_mr_iid, parent_sha, intent_nonce)
            or parse_auto_fix_marker(marker)
            != (project_id, parent_mr_iid, parent_sha, intent_nonce, plan_sha256)
        ):
            raise AutoFixRecoveryError("Publication Plan branch or marker identity is invalid")
        target_branch = payload.get("target_branch")
        if not isinstance(target_branch, str) or not target_branch.strip():
            raise AutoFixRecoveryError("Publication Plan target branch is invalid")
        if (
            not isinstance(payload.get("proposal_summary"), str)
            or not isinstance(payload.get("commit_message"), str)
            or not str(payload["commit_message"]).strip()
            or not isinstance(payload.get("title"), str)
            or not str(payload["title"]).strip()
            or payload.get("initial_readiness") not in {"draft", "ready"}
            or payload.get("ci_trigger") not in {"manual", "automatic"}
            or (
                payload.get("initial_readiness") == "ready"
                and payload.get("ci_trigger") == "manual"
            )
        ):
            raise AutoFixRecoveryError("Publication Plan publication settings are invalid")
        return payload, actions, branch, marker, target_branch

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
            _payload, actions, branch, marker, target_branch = self._plan(
                plan_json,
                project_id=project_id,
                parent_mr_iid=parent_mr_iid,
                parent_sha=parent_sha,
                intent_nonce=intent_nonce,
                plan_sha256=plan_sha256,
            )
            parent = self.gitlab.get_merge_request(project_id, parent_mr_iid)
            if (
                parent.project_id != project_id
                or parent.iid != parent_mr_iid
                or parent.source_project_id != project_id
                or parent.source_branch != target_branch
                or not parent.author_username.strip()
            ):
                raise AutoFixRecoveryError("parent Merge Request conflicts with the Publication Plan")
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
                raise AutoFixRecoveryError("child Merge Request conflicts with the Publication Plan")
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
                raise AutoFixRecoveryError("remote commit actions conflict with the Publication Plan")
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
        relation = self.store.get_program_auto_fix_relation_by_child(
            project_id, child_mr_iid
        )
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
