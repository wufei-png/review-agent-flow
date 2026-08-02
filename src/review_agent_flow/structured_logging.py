"""Small JSON logging helper for ReviewFlow operational events."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from typing import Any

from .security import redact

_SENSITIVE_FIELD_NAMES = {
    "authorization",
    "claim_token",
    "gitlab_token",
    "password",
    "payload",
    "private_token",
    "raw_payload",
    "secret",
    "token",
    "webhook_secret",
}


def _safe_value(value: object, secrets: tuple[str, ...]) -> object:
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    return redact(value, max_length=1000, secrets=secrets)


def log_event(
    logger: logging.Logger,
    level: int,
    *,
    event: str,
    status: str,
    correlation_id: str | None = None,
    run_id: int | None = None,
    project_id: str | None = None,
    mr_iid: int | None = None,
    diagnostic: object | None = None,
    secrets: Iterable[str] | None = None,
    **fields: Any,
) -> None:
    """Emit one bounded JSON object without accepting raw payload fields.

    Callers pass only normalized identifiers and stable operational fields.
    Sensitive-looking field names are dropped defensively, while diagnostics
    go through the same redaction helper used by the failure ledger.
    """

    if secrets is None:
        runtime_secrets: tuple[str, ...] = ()
    elif isinstance(secrets, str):
        runtime_secrets = (secrets,)
    else:
        runtime_secrets = tuple(secret for secret in secrets if secret and not secret.isspace())
    record: dict[str, object] = {
        "event": _safe_value(event, runtime_secrets),
        "status": _safe_value(status, runtime_secrets),
    }
    stable_fields = {
        "correlation_id": correlation_id,
        "run_id": run_id,
        "project_id": project_id,
        "mr_iid": mr_iid,
    }
    for name, value in stable_fields.items():
        if value is not None:
            record[name] = _safe_value(value, runtime_secrets)
    if diagnostic is not None:
        record["diagnostic"] = redact(
            diagnostic,
            max_length=2000,
            secrets=runtime_secrets,
        )
    for name, value in fields.items():
        if name.lower() in _SENSITIVE_FIELD_NAMES or "payload" in name.lower():
            continue
        if value is not None:
            record[name] = _safe_value(value, runtime_secrets)
    logger.log(level, json.dumps(record, ensure_ascii=False, sort_keys=True))
