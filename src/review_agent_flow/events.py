"""Normalize GitLab webhook payloads into workflow decisions."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .gitlab import is_valid_sha

AUTO_FIX_TITLE_PREFIX = "[ReviewFlow Auto-Fix]"
AUTO_FIX_BRANCH_RE = re.compile(
    r"^autofix/MR-(?P<mr_iid>\d+)/sha-(?P<sha>[0-9a-fA-F]{40}|[0-9a-fA-F]{64})/"
    r"intent-(?P<nonce>[0-9a-f]{32})$"
)
AUTO_FIX_MARKER_RE = re.compile(
    r"<!-- reviewflow:v1;project=(?P<project>[^;\s]+);mr=(?P<mr_iid>\d+);"
    r"sha=(?P<sha>[0-9a-fA-F]{40}|[0-9a-fA-F]{64});effect=autofix;"
    r"intent=(?P<nonce>[0-9a-f]{32});plan_sha256=(?P<digest>[0-9a-f]{64}) -->"
)


@dataclass(frozen=True)
class WebhookDecision:
    accepted: bool
    reason: str
    kind: str | None = None
    project_id: str | None = None
    mr_iid: int | None = None
    sha: str | None = None
    author_username: str | None = None
    title: str | None = None
    source_branch: str | None = None
    target_branch: str | None = None
    description: str | None = None
    parent_mr_iid: int | None = None
    merged_sha: str | None = None
    action: str | None = None


def parse_auto_fix_branch(branch: str) -> tuple[int, str, str] | None:
    match = AUTO_FIX_BRANCH_RE.fullmatch(branch)
    if match is None:
        return None
    return (
        int(match.group("mr_iid")),
        match.group("sha").lower(),
        match.group("nonce"),
    )


def parse_auto_fix_marker(
    body: str | None,
) -> tuple[str, int, str, str, str] | None:
    for line in (body or "").splitlines():
        match = AUTO_FIX_MARKER_RE.fullmatch(line)
        if match is not None:
            return (
                match.group("project"),
                int(match.group("mr_iid")),
                match.group("sha").lower(),
                match.group("nonce"),
                match.group("digest"),
            )
    return None


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _project_id(payload: dict[str, Any]) -> str | None:
    project = payload.get("project") or {}
    value = project.get("id") or payload.get("project_id")
    return str(value) if value is not None else None


def _user_username(payload: dict[str, Any], attrs: dict[str, Any]) -> str | None:
    for value in (
        (payload.get("user") or {}).get("username"),
        (attrs.get("last_commit") or {}).get("author_name"),
        (attrs.get("author") or {}).get("username"),
    ):
        if value:
            return str(value)
    return None


def parse_webhook(event_name: str | None, payload: dict[str, Any]) -> WebhookDecision:
    decisions = parse_webhooks(event_name, payload)
    for decision in decisions:
        if decision.accepted:
            return decision
    return decisions[0]


def parse_webhooks(event_name: str | None, payload: dict[str, Any]) -> tuple[WebhookDecision, ...]:
    normalized = (event_name or "Merge Request Hook").strip()
    if normalized == "Merge Request Hook":
        return (_parse_merge_request(payload),)
    if normalized == "Pipeline Hook":
        return _parse_pipeline(payload)
    return (WebhookDecision(False, f"unsupported event: {normalized}"),)


def _parse_merge_request(payload: dict[str, Any]) -> WebhookDecision:
    attrs = payload.get("object_attributes") or {}
    project_id = _project_id(payload)
    mr_iid = _int_or_none(attrs.get("iid"))
    if not project_id or mr_iid is None:
        return WebhookDecision(False, "missing project, MR IID, or SHA")

    is_draft = attrs.get("work_in_progress") is True or attrs.get("draft") is True

    title = str(attrs.get("title") or "")
    source_branch = attrs.get("source_branch")
    target_branch = attrs.get("target_branch")
    description = attrs.get("description")
    action = attrs.get("action")
    common = {
        "project_id": project_id,
        "mr_iid": mr_iid,
        "title": title,
        "source_branch": str(source_branch) if source_branch else None,
        "target_branch": str(target_branch) if target_branch else None,
        "description": str(description) if description is not None else None,
        "action": str(action) if action is not None else None,
    }

    if action == "merge":
        merged_sha = attrs.get("merge_commit_sha")
        if not is_valid_sha(merged_sha):
            return WebhookDecision(
                False,
                "merge event has an invalid merge_commit_sha",
                **common,
            )
        return WebhookDecision(
            True,
            "merge request merged",
            kind="merge",
            sha=merged_sha,
            merged_sha=merged_sha,
            **common,
        )

    if action and action not in {"open", "reopen", "update", "close"}:
        return WebhookDecision(
            False,
            f"ignored merge request action: {action}",
            project_id=project_id,
            mr_iid=mr_iid,
        )

    last_commit = attrs.get("last_commit") or {}
    if not isinstance(last_commit, dict):
        last_commit = {}
    sha = last_commit.get("id") or attrs.get("last_commit_id")
    if sha is None:
        return WebhookDecision(False, "missing project, MR IID, or SHA")
    if not is_valid_sha(sha):
        return WebhookDecision(
            False,
            "invalid Merge Request head SHA; expected 40 or 64 hexadecimal characters",
            project_id=project_id,
            mr_iid=mr_iid,
        )
    return WebhookDecision(
        True,
        "merge request",
        kind="close" if action == "close" else ("draft" if is_draft else "normal"),
        sha=str(sha),
        author_username=_user_username(payload, attrs),
        **common,
    )


def _parse_pipeline(payload: dict[str, Any]) -> tuple[WebhookDecision, ...]:
    attrs = payload.get("object_attributes") or {}
    project_id = _project_id(payload)
    status = attrs.get("status")
    sha = attrs.get("sha") or attrs.get("commit_sha")
    if status != "success":
        return (WebhookDecision(False, f"pipeline status is not success: {status}"),)
    if not project_id or not sha:
        return (WebhookDecision(False, "pipeline has no project or SHA"),)
    if not is_valid_sha(sha):
        return (
            WebhookDecision(
                False,
                "invalid pipeline SHA; expected 40 or 64 hexadecimal characters",
                project_id=project_id,
            ),
        )

    raw_merge_requests = payload.get("merge_request") or attrs.get("merge_request")
    if isinstance(raw_merge_requests, dict):
        merge_requests = [raw_merge_requests]
    elif isinstance(raw_merge_requests, list):
        merge_requests = [item for item in raw_merge_requests if isinstance(item, dict)]
    else:
        merge_requests = []
    if not merge_requests:
        return (WebhookDecision(False, "pipeline has no Merge Request"),)

    decisions: list[WebhookDecision] = []
    for merge_request in merge_requests:
        mr_iid = _int_or_none(
            merge_request.get("iid")
            or attrs.get("merge_request_iid")
            or payload.get("merge_request_iid")
        )
        if mr_iid is None:
            decisions.append(WebhookDecision(False, "pipeline has no MR IID", project_id=project_id))
            continue
        if merge_request.get("work_in_progress") is True or merge_request.get("draft") is True:
            decisions.append(
                WebhookDecision(False, "draft merge request", project_id=project_id, mr_iid=mr_iid)
            )
            continue
        title = str(merge_request.get("title") or attrs.get("title") or "")
        source_branch = merge_request.get("source_branch") or attrs.get("ref")
        description = merge_request.get("description") or attrs.get("description")
        decisions.append(
            WebhookDecision(
                True,
                "successful pipeline",
                kind="pipeline",
                project_id=project_id,
                mr_iid=mr_iid,
                sha=str(sha),
                title=title,
                source_branch=str(source_branch) if source_branch else None,
                target_branch=str(merge_request.get("target_branch") or attrs.get("target_branch") or "") or None,
                description=str(description) if description is not None else None,
            )
        )
    return tuple(decisions)
