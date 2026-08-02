"""Stable Review Run failure classes and transient-error classification."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from urllib.error import URLError

from .policy import PolicyError
from .security import redact
from .worktree import WorktreeError


@dataclass(frozen=True)
class FailureInfo:
    failure_class: str
    retryable: bool
    detail: str


_STATUS_RE = re.compile(r"\b([45]\d\d)\b")


def _status_code(error: BaseException) -> int | None:
    for attribute in ("response_code", "status_code", "http_status", "code"):
        value = getattr(error, attribute, None)
        try:
            if value is not None:
                return int(value)
        except (TypeError, ValueError):
            continue
    response = getattr(error, "response", None)
    value = getattr(response, "status_code", None)
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def classify_exception(
    error: BaseException,
    secrets: Iterable[str] | None = None,
) -> FailureInfo:
    """Classify an exception without making arbitrary failures retryable."""

    detail = redact(str(error) or error.__class__.__name__, secrets=secrets)
    error_name = f"{error.__class__.__module__}.{error.__class__.__name__}".lower()
    status = _status_code(error)
    if status == 429:
        return FailureInfo("gitlab_rate_limited", True, detail)
    if status is not None and 400 <= status <= 499:
        return FailureInfo("gitlab_api", False, detail)
    if status is not None and 500 <= status <= 599:
        return FailureInfo("gitlab_server_error", True, detail)
    if status is None and (
        "too many requests" in detail.lower() or "rate limit" in detail.lower()
    ):
        return FailureInfo("gitlab_rate_limited", True, detail)
    if isinstance(error, URLError):
        return FailureInfo("gitlab_network", True, detail)
    if isinstance(error, WorktreeError):
        return FailureInfo("worktree_transport", True, detail)
    if isinstance(error, (FileNotFoundError, PermissionError)):
        return FailureInfo("agent_configuration", False, detail)
    if isinstance(error, PolicyError):
        return FailureInfo("policy_error", False, detail)
    if isinstance(error, ValueError):
        return FailureInfo("policy_error", False, detail)
    if isinstance(error, (ConnectionError, TimeoutError)):
        return FailureInfo("gitlab_network", True, detail)
    if any(
        marker in error_name
        for marker in (
            "gitlabconnection",
            "gitlabtimeout",
            "requests.exceptions.connection",
            "requests.exceptions.timeout",
            "httpx.",
        )
    ):
        return FailureInfo("gitlab_network", True, detail)
    if status is None:
        match = _STATUS_RE.search(detail)
        status = int(match.group(1)) if match else None
        if status == 429:
            return FailureInfo("gitlab_rate_limited", True, detail)
        if status is not None and 500 <= status <= 599:
            return FailureInfo("gitlab_server_error", True, detail)
    if "gitlab" in error_name or "gitlab" in detail.lower():
        return FailureInfo("gitlab_api", False, detail)
    return FailureInfo("internal_error", False, detail)
