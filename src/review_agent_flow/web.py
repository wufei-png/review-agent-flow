"""FastAPI webhook, health, probe, and project-bound status surfaces."""

from __future__ import annotations

import hmac
import logging
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import uuid4

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse

from .events import parse_webhooks
from .security import redact
from .store import SQLiteStore
from .structured_logging import log_event as _emit_log_event

LOGGER = logging.getLogger(__name__)


class StartupConfigurationError(RuntimeError):
    """Raised when a production entry point is missing required configuration."""


@dataclass(frozen=True)
class LiveProbeResult:
    """Normalized result returned by an explicitly injected provider probe."""

    ok: bool
    diagnostic: str | None = None


class LiveGitLabProbe(Protocol):
    """Callable contract for the explicit, opt-in GitLab probe endpoint."""

    def __call__(self, project_id: str) -> LiveProbeResult: ...


def _required_config(value: str | None, env_name: str) -> str:
    configured = value if value is not None else os.getenv(env_name)
    if configured is None or not configured.strip():
        raise StartupConfigurationError(
            f"ReviewFlow startup configuration error: {env_name} must be set"
        )
    return configured


def _correlation_id(request_id: str | None, event_uuid: str | None) -> str:
    return (request_id or event_uuid or uuid4().hex)[:128]


def _normalize_probe_result(value: object) -> LiveProbeResult:
    if isinstance(value, LiveProbeResult):
        return value
    if isinstance(value, bool):
        return LiveProbeResult(value)
    if isinstance(value, Mapping):
        raw_ok = value.get("ok")
        if raw_ok is None:
            raw_ok = value.get("ready")
        if raw_ok is None:
            raw_ok = value.get("status") in {"ok", "ready", "success"}
        return LiveProbeResult(bool(raw_ok), str(value.get("diagnostic")) if value.get("diagnostic") else None)
    return LiveProbeResult(False, "live probe returned an unsupported result")


def _run_probe(probe: object, project_id: str) -> LiveProbeResult:
    if callable(probe):
        return _normalize_probe_result(probe(project_id))
    probe_method = getattr(probe, "probe", None)
    if callable(probe_method):
        return _normalize_probe_result(probe_method(project_id))
    return LiveProbeResult(False, "live probe does not implement the probe contract")


def _storage_probe(
    store: object,
    secrets: tuple[str, ...] = (),
) -> tuple[bool, str | None]:
    probe = getattr(store, "readiness_probe", None)
    if not callable(probe):
        return False, "storage readiness probe is not configured"
    try:
        result = probe()
    except Exception as exc:  # noqa: BLE001 - readiness must fail closed
        return False, redact(exc, max_length=1000, secrets=secrets)
    if isinstance(result, bool):
        return result, None if result else "storage readiness probe failed"
    if isinstance(result, Mapping):
        status = str(result.get("status", "not_ready")).lower()
        ok = result.get("ok") is True or status in {"ok", "ready", "healthy"}
        diagnostic = result.get("diagnostic")
        return (
            ok,
            redact(diagnostic, max_length=1000, secrets=secrets) if diagnostic else None,
        )
    return False, "storage readiness probe returned an unsupported result"


def create_app(
    store: SQLiteStore | None = None,
    webhook_secret: str | None = None,
    project_id: str | None = None,
    live_probe: LiveGitLabProbe | Callable[[str], object] | None = None,
) -> FastAPI:
    webhook_secret = _required_config(webhook_secret, "GITLAB_WEBHOOK_SECRET")
    project_id = _required_config(project_id, "REVIEW_AGENT_FLOW_PROJECT_ID").strip()
    store = store or SQLiteStore(os.getenv("REVIEW_AGENT_FLOW_DB", "review-agent-flow.sqlite3"))
    register_secret = getattr(store, "add_redaction_secret", None)
    if callable(register_secret):
        register_secret(webhook_secret)
    internal_token = webhook_secret
    runtime_secrets = tuple(
        secret
        for secret in (
            webhook_secret,
            os.getenv("GITLAB_TOKEN"),
            os.getenv("FEISHU_WEBHOOK"),
            os.getenv("WECOM_WEBHOOK"),
        )
        if secret and not secret.isspace()
    )

    def log_event(logger: logging.Logger, level: int, **fields: Any) -> None:
        _emit_log_event(logger, level, secrets=runtime_secrets, **fields)

    def _safe_diagnostic(value: object, max_length: int = 1000) -> str:
        return redact(value, max_length=max_length, secrets=runtime_secrets)

    def _require_internal_token(
        candidate: str | None,
        *,
        endpoint: str,
        correlation_id: str | None = None,
        requested_project_id: str | None = None,
        run_id: int | None = None,
    ) -> None:
        if hmac.compare_digest(candidate or "", internal_token):
            return
        log_event(
            LOGGER,
            logging.WARNING,
            event="internal_auth",
            status="unauthorized",
            correlation_id=correlation_id or f"internal:{endpoint}",
            project_id=requested_project_id or project_id,
            run_id=run_id,
            endpoint=endpoint,
        )
        raise HTTPException(status_code=401, detail="invalid ReviewFlow internal token")

    app = FastAPI(title="Review Agent Flow", version="0.1.0")

    @app.get("/health")
    def health() -> dict[str, str]:
        """Legacy compatibility endpoint; keep its original response shape."""

        return {"status": "ok"}

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "alive"}

    @app.get("/readyz")
    def readyz() -> JSONResponse:
        sqlite_ok, sqlite_diagnostic = _storage_probe(store, runtime_secrets)
        checks: dict[str, object] = {
            "config": {
                "status": "ok",
                "required": {
                    "GITLAB_WEBHOOK_SECRET": "configured",
                    "REVIEW_AGENT_FLOW_PROJECT_ID": "configured",
                },
            },
            "project": {"status": "ok", "bound": True},
            "sqlite": {"status": "ok" if sqlite_ok else "not_ready"},
        }
        if sqlite_diagnostic:
            checks["sqlite"] = {"status": "not_ready", "diagnostic": sqlite_diagnostic}
        if sqlite_ok:
            return JSONResponse(status_code=200, content={"status": "ready", "checks": checks})
        return JSONResponse(status_code=503, content={"status": "not_ready", "checks": checks})

    @app.get("/probes/gitlab")
    def gitlab_probe(
        x_request_id: str | None = Header(default=None),
        x_reviewflow_internal_token: str | None = Header(
            default=None,
            alias="X-ReviewFlow-Internal-Token",
        ),
    ) -> JSONResponse:
        """Run only an explicitly injected live provider probe.

        The default is a deterministic ``not_configured`` response.  In
        particular, this endpoint is never called by ``/readyz``.
        """

        correlation_id = _correlation_id(x_request_id, None)
        _require_internal_token(
            x_reviewflow_internal_token,
            endpoint="gitlab_probe",
            correlation_id=correlation_id,
        )
        if live_probe is None:
            log_event(
                LOGGER,
                logging.WARNING,
                event="gitlab_probe",
                status="not_configured",
                correlation_id=correlation_id,
                project_id=project_id,
            )
            return JSONResponse(
                status_code=503,
                content={
                    "status": "not_ready",
                    "probe": "gitlab",
                    "reason": "not_configured",
                },
            )
        try:
            result = _run_probe(live_probe, project_id)
        except Exception as exc:  # noqa: BLE001 - probe failures are safe diagnostics
            diagnostic = _safe_diagnostic(exc)
            log_event(
                LOGGER,
                logging.ERROR,
                event="gitlab_probe",
                status="not_ready",
                correlation_id=correlation_id,
                project_id=project_id,
                diagnostic=diagnostic,
            )
            return JSONResponse(
                status_code=503,
                content={
                    "status": "not_ready",
                    "probe": "gitlab",
                    "reason": "probe_failed",
                    "diagnostic": diagnostic,
                },
            )
        if not result.ok:
            diagnostic = _safe_diagnostic(result.diagnostic) if result.diagnostic else None
            log_event(
                LOGGER,
                logging.WARNING,
                event="gitlab_probe",
                status="not_ready",
                correlation_id=correlation_id,
                project_id=project_id,
                diagnostic=diagnostic,
            )
            content: dict[str, object] = {
                "status": "not_ready",
                "probe": "gitlab",
                "reason": "probe_failed",
            }
            if diagnostic:
                content["diagnostic"] = diagnostic
            return JSONResponse(status_code=503, content=content)
        log_event(
            LOGGER,
            logging.INFO,
            event="gitlab_probe",
            status="ready",
            correlation_id=correlation_id,
            project_id=project_id,
        )
        return JSONResponse(status_code=200, content={"status": "ready", "probe": "gitlab"})

    def _review_run_status(run_id: int, requested_project_id: str = project_id) -> dict[str, object]:
        if requested_project_id != project_id:
            log_event(
                LOGGER,
                logging.WARNING,
                event="review_run_status",
                status="not_found",
                project_id=requested_project_id,
                run_id=run_id,
            )
            raise HTTPException(status_code=404, detail="review run not found")
        query = getattr(store, "get_project_bound_review_run_status", None)
        if not callable(query):
            query = getattr(store, "get_review_run_status", None)
        snapshot = query(project_id, run_id) if callable(query) else None
        if snapshot is None:
            log_event(
                LOGGER,
                logging.INFO,
                event="review_run_status",
                status="not_found",
                project_id=project_id,
                run_id=run_id,
            )
            raise HTTPException(status_code=404, detail="review run not found")
        log_event(
            LOGGER,
            logging.INFO,
            event="review_run_status",
            status=str(snapshot.get("status", "unknown")),
            correlation_id=f"review-run:{run_id}",
            project_id=project_id,
            mr_iid=int(snapshot["mr_iid"]) if snapshot.get("mr_iid") is not None else None,
            run_id=run_id,
        )
        return snapshot

    @app.get("/review-runs/{run_id}")
    def review_run_status(
        run_id: int,
        x_request_id: str | None = Header(default=None),
        x_reviewflow_internal_token: str | None = Header(
            default=None,
            alias="X-ReviewFlow-Internal-Token",
        ),
    ) -> dict[str, object]:
        _require_internal_token(
            x_reviewflow_internal_token,
            endpoint="review_run_status",
            correlation_id=_correlation_id(x_request_id, None),
            run_id=run_id,
        )
        return _review_run_status(run_id)

    @app.get("/projects/{requested_project_id}/review-runs/{run_id}")
    def project_review_run_status(
        requested_project_id: str,
        run_id: int,
        x_request_id: str | None = Header(default=None),
        x_reviewflow_internal_token: str | None = Header(
            default=None,
            alias="X-ReviewFlow-Internal-Token",
        ),
    ) -> dict[str, object]:
        _require_internal_token(
            x_reviewflow_internal_token,
            endpoint="project_review_run_status",
            correlation_id=_correlation_id(x_request_id, None),
            requested_project_id=requested_project_id,
            run_id=run_id,
        )
        return _review_run_status(run_id, requested_project_id)

    @app.post("/webhooks/gitlab")
    def gitlab_webhook(
        payload: dict[str, Any],
        x_gitlab_token: str | None = Header(default=None),
        x_gitlab_event: str | None = Header(default=None),
        x_gitlab_event_uuid: str | None = Header(default=None),
        x_request_id: str | None = Header(default=None),
    ) -> dict[str, Any]:
        correlation_id = _correlation_id(x_request_id, x_gitlab_event_uuid)
        if not hmac.compare_digest(x_gitlab_token or "", webhook_secret):
            log_event(
                LOGGER,
                logging.WARNING,
                event="webhook.gitlab",
                status="unauthorized",
                correlation_id=correlation_id,
                gitlab_event=x_gitlab_event or "unknown",
            )
            raise HTTPException(status_code=401, detail="invalid GitLab webhook token")
        decisions = parse_webhooks(x_gitlab_event, payload)
        if any(decision.project_id and decision.project_id != project_id for decision in decisions):
            log_event(
                LOGGER,
                logging.WARNING,
                event="webhook.gitlab",
                status="project_mismatch",
                correlation_id=correlation_id,
                project_id=project_id,
                gitlab_event=x_gitlab_event or "unknown",
            )
            raise HTTPException(status_code=403, detail="project is not bound to this deployment")
        responses: list[dict[str, Any]] = []
        for decision in decisions:
            if not decision.accepted:
                responses.append({"accepted": False, "reason": decision.reason})
                log_event(
                    LOGGER,
                    logging.INFO,
                    event="webhook.decision",
                    status="rejected",
                    correlation_id=correlation_id,
                    project_id=decision.project_id or project_id,
                    mr_iid=decision.mr_iid,
                    gitlab_event=x_gitlab_event or "unknown",
                    diagnostic=decision.reason,
                )
                continue
            assert decision.project_id is not None
            assert decision.mr_iid is not None
            assert decision.sha is not None
            if decision.kind == "autofix_merged":
                store.mark_auto_fix_merged(decision.project_id, decision.mr_iid, decision.sha)
                responses.append(
                    {
                        "accepted": True,
                        "kind": decision.kind,
                        "merged_sha": decision.sha,
                    }
                )
                log_event(
                    LOGGER,
                    logging.INFO,
                    event="webhook.decision",
                    status="accepted",
                    correlation_id=correlation_id,
                    project_id=decision.project_id,
                    mr_iid=decision.mr_iid,
                    gitlab_event=x_gitlab_event or "unknown",
                    kind=decision.kind,
                )
                continue
            run = store.enqueue(
                decision.project_id,
                decision.mr_iid,
                decision.sha,
                decision.kind or "normal",
            )
            responses.append(
                {
                    "accepted": True,
                    "kind": decision.kind,
                    "run_id": run.run_id,
                    "status": run.status,
                }
            )
            log_event(
                LOGGER,
                logging.INFO,
                event="webhook.decision",
                status=run.status,
                correlation_id=correlation_id,
                project_id=run.project_id,
                mr_iid=run.mr_iid,
                run_id=run.run_id,
                gitlab_event=x_gitlab_event or "unknown",
                kind=run.kind,
            )
        if len(responses) == 1:
            return responses[0]
        return {"accepted": any(item["accepted"] for item in responses), "decisions": responses}

    return app
