"""Small redaction helpers for values that may cross an audit boundary."""

from __future__ import annotations

import os
import re
from collections.abc import Iterable

_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(authorization|proxy-authorization|x-gitlab-token|gitlab[_-]?token|"
    r"private[_-]?token|access[_-]?token|refresh[_-]?token|api[_-]?key|secret|"
    r"password|token)\b\s*[:=]\s*(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)"
)
_BEARER_RE = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]+")
_QUERY_RE = re.compile(
    r"(?i)([?&](?:token|access_token|refresh_token|api_key|apikey|secret|password)="
    r")[^&#\s]+"
)
_KNOWN_TOKEN_RE = re.compile(
    r"(?i)\b(?:glpat-|gloas-|ghp_|github_pat_|sk-[A-Za-z0-9])[A-Za-z0-9_./=-]+"
)
_URL_CREDENTIAL_RE = re.compile(r"(?i)(https?://)[^/@\s]+:[^/@\s]+@")


def redact(
    value: object,
    max_length: int = 4000,
    secrets: Iterable[str] | None = None,
) -> str:
    """Return a bounded diagnostic with common credentials removed."""

    text = "" if value is None else str(value)
    configured_secrets = []
    for env_name in (
        "GITLAB_TOKEN",
        "GITLAB_WEBHOOK_SECRET",
        "FEISHU_WEBHOOK",
        "WECOM_WEBHOOK",
    ):
        configured = os.getenv(env_name)
        if configured and len(configured) >= 4:
            configured_secrets.append(configured)
    if secrets is None:
        runtime_secrets: tuple[str, ...] = ()
    elif isinstance(secrets, str):
        runtime_secrets = (secrets,)
    else:
        runtime_secrets = tuple(secrets)
    for configured in configured_secrets:
        text = text.replace(configured, "[REDACTED]")
    for secret in runtime_secrets:
        if secret and not secret.isspace():
            text = text.replace(secret, "[REDACTED]")
    text = _URL_CREDENTIAL_RE.sub(r"\1[REDACTED]@", text)
    text = _BEARER_RE.sub(lambda match: f"{match.group(1)} [REDACTED]", text)
    text = _ASSIGNMENT_RE.sub(lambda match: f"{match.group(1)}=[REDACTED]", text)
    text = _QUERY_RE.sub(r"\1[REDACTED]", text)
    text = _KNOWN_TOKEN_RE.sub("[REDACTED]", text)
    if len(text) > max_length:
        return f"{text[:max_length]}...[truncated]"
    return text
