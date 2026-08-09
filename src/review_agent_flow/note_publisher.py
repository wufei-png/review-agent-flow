"""Durable Program-owned GitLab Note publication and reconciliation."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

from .gitlab import (
    GitLabClient,
    GitLabIdentityConflict,
    Note,
    NoteMarkerLookup,
    has_exact_note_marker,
)
from .store import (
    DEFAULT_NOTE_PUBLICATION_LEASE_SECONDS,
    DEFAULT_NOTE_PUBLICATION_MAX_ATTEMPTS,
    DEFAULT_NOTE_UNCERTAIN_LOOKUP_ATTEMPTS,
    NotePublicationRequest,
    SQLiteStore,
)


@dataclass(frozen=True)
class NotePublicationResult:
    request_id: int
    run_id: int
    status: str
    detail: str


class NotePublisher:
    """Publish only frozen Note requests and adopt only trusted exact identities."""

    def __init__(
        self,
        store: SQLiteStore,
        gitlab: GitLabClient,
        project_id: str,
        *,
        lease_seconds: int = DEFAULT_NOTE_PUBLICATION_LEASE_SECONDS,
        max_attempts: int = DEFAULT_NOTE_PUBLICATION_MAX_ATTEMPTS,
        retry_delay_seconds: float = 5.0,
        max_retry_delay_seconds: float = 300.0,
        reconciliation_interval_seconds: float = 300.0,
    ) -> None:
        if not project_id or not project_id.strip():
            raise ValueError("Note Publisher project_id cannot be empty")
        if lease_seconds <= 0 or max_attempts <= 0:
            raise ValueError("Note publication lease and attempt limits must be positive")
        if (
            retry_delay_seconds < 0
            or max_retry_delay_seconds < 0
            or reconciliation_interval_seconds < 0
        ):
            raise ValueError("Note publication retry delays cannot be negative")
        self.store = store
        self.gitlab = gitlab
        self.project_id = project_id
        self.lease_seconds = lease_seconds
        self.max_attempts = max_attempts
        self.retry_delay_seconds = retry_delay_seconds
        self.max_retry_delay_seconds = max_retry_delay_seconds
        self.reconciliation_interval_seconds = reconciliation_interval_seconds

    @staticmethod
    def _valid_note(request: NotePublicationRequest, note: object) -> bool:
        return (
            isinstance(note, Note)
            and type(note.project_id) is str
            and note.project_id == request.project_id
            and type(note.mr_iid) is int
            and note.mr_iid == request.mr_iid
            and type(note.note_id) is int
            and note.note_id > 0
            and note.body == request.body
            and has_exact_note_marker(note.body, request.marker)
        )

    def _finish(
        self,
        request: NotePublicationRequest,
        *,
        state: str,
        detail: str,
        external_note_id: int | None = None,
        error: str | None = None,
        next_attempt_at: str | None = None,
    ) -> NotePublicationResult:
        reconcile_next_at = None
        if state == "published":
            reconcile_next_at = (
                datetime.now(UTC)
                + timedelta(seconds=self.reconciliation_interval_seconds)
            ).isoformat()
        updated = self.store.finish_note_publication(
            request.request_id,
            request.claim_token,
            state=state,
            external_note_id=external_note_id,
            detail=detail,
            error=error,
            next_attempt_at=next_attempt_at,
            reconcile_next_at=reconcile_next_at,
        )
        return NotePublicationResult(
            request.request_id,
            request.run_id,
            state if updated else "stale",
            detail if updated else "Note publication lease was lost before completion",
        )

    def _retry(
        self, request: NotePublicationRequest, detail: str
    ) -> NotePublicationResult:
        attempt_limit = self.max_attempts + (
            DEFAULT_NOTE_UNCERTAIN_LOOKUP_ATTEMPTS if request.write_uncertain else 0
        )
        if request.attempts >= attempt_limit:
            return self._finish(
                request,
                state="blocked",
                detail="Note publication attempt limit reached",
                error=detail,
            )
        delay = min(
            self.retry_delay_seconds * (2 ** max(request.attempts - 1, 0)),
            self.max_retry_delay_seconds,
        )
        next_attempt = (datetime.now(UTC) + timedelta(seconds=delay)).isoformat()
        return self._finish(
            request,
            state="pending",
            detail="Note publication will be retried",
            error=detail,
            next_attempt_at=next_attempt,
        )

    def _lookup(self, request: NotePublicationRequest) -> NoteMarkerLookup:
        lookup = self.gitlab.lookup_note_marker(
            request.project_id, request.mr_iid, request.marker
        )
        if not isinstance(lookup, NoteMarkerLookup):
            raise TypeError("GitLab adapter returned an invalid Note marker lookup")
        return lookup

    def _publish_claimed(self, request: NotePublicationRequest) -> NotePublicationResult:
        try:
            lookup = self._lookup(request)
            if lookup.conflicts or len(lookup.exact) > 1:
                return self._finish(
                    request,
                    state="blocked",
                    detail="Trusted Review Bot Note marker is conflicting or duplicated",
                )
            if len(lookup.exact) == 1:
                note = lookup.exact[0]
                if not self._valid_note(request, note):
                    return self._finish(
                        request,
                        state="blocked",
                        detail="Trusted Review Bot Note content conflicts with the frozen request",
                    )
                return self._finish(
                    request,
                    state="published",
                    external_note_id=note.note_id,
                    detail="Existing Program-owned GitLab Note adopted",
                )
            parent = self.gitlab.get_merge_request(request.project_id, request.mr_iid)
            if (
                parent.project_id != request.project_id
                or parent.iid != request.mr_iid
                or str(parent.state or "opened").lower() != "opened"
                or parent.sha != request.sha
            ):
                return self._finish(
                    request,
                    state="superseded",
                    detail="Parent Merge Request no longer matches the Note ReviewKey",
                )
            if request.attempts > self.max_attempts:
                return self._finish(
                    request,
                    state="blocked",
                    detail="Uncertain Note write was not found within the lookup-only budget",
                )
            if not self.store.mark_note_write_uncertain(
                request.request_id, request.claim_token
            ):
                return NotePublicationResult(
                    request.request_id,
                    request.run_id,
                    "stale",
                    "Note publication lease was lost before the GitLab write",
                )
            request = replace(request, write_uncertain=True)
            created = self.gitlab.create_note(
                request.project_id, request.mr_iid, request.body
            )
            if not self._valid_note(request, created):
                return self._finish(
                    request,
                    state="blocked",
                    detail="GitLab returned a conflicting created Note identity",
                )
            return self._finish(
                request,
                state="published",
                external_note_id=created.note_id,
                detail="Program-owned GitLab Note published",
            )
        except GitLabIdentityConflict as exc:
            return self._finish(
                request,
                state="blocked",
                detail="GitLab Note identity conflicts with the configured Review Bot",
                error=str(exc),
            )
        except Exception as exc:  # noqa: BLE001 - uncertain writes must remain recoverable
            return self._retry(request, str(exc))

    def process(
        self, *, request_id: int | None = None, limit: int = 1
    ) -> tuple[NotePublicationResult, ...]:
        if limit <= 0:
            raise ValueError("Note publication limit must be positive")
        results: list[NotePublicationResult] = []
        attempted_request_ids: list[int] = []
        for _ in range(limit):
            request = self.store.claim_note_publication(
                request_id=request_id,
                project_id=self.project_id,
                excluded_request_ids=tuple(attempted_request_ids),
                lease_seconds=self.lease_seconds,
                max_attempts=self.max_attempts,
            )
            if request is None:
                break
            attempted_request_ids.append(request.request_id)
            results.append(self._publish_claimed(request))
            if request_id is not None:
                break
        return tuple(results)

    def _finish_reconciliation(
        self,
        request: NotePublicationRequest,
        *,
        state: str,
        status: str | None = None,
        detail: str,
        error: str | None = None,
        delay_seconds: float | None = None,
    ) -> NotePublicationResult:
        next_at = None
        if state == "published":
            delay = (
                self.reconciliation_interval_seconds
                if delay_seconds is None
                else delay_seconds
            )
            next_at = (datetime.now(UTC) + timedelta(seconds=delay)).isoformat()
        updated = self.store.finish_published_note_reconciliation(
            request.request_id,
            request.reconcile_claim_token,
            state=state,
            detail=detail,
            error=error,
            reconcile_next_at=next_at,
        )
        return NotePublicationResult(
            request.request_id,
            request.run_id,
            (status or state) if updated else "stale",
            detail if updated else "Note reconciliation lease was lost before completion",
        )

    def reconcile_published(
        self, request_id: int | None = None
    ) -> NotePublicationResult | None:
        """Observe an already-published Note without ever recreating a missing Note."""

        request = self.store.claim_published_note_reconciliation(
            request_id=request_id,
            project_id=self.project_id,
            lease_seconds=self.lease_seconds,
        )
        if request is None or request.external_note_id is None:
            return None
        try:
            persisted = self.gitlab.get_trusted_note(
                request.project_id, request.mr_iid, request.external_note_id
            )
            lookup = self._lookup(request)
        except GitLabIdentityConflict as exc:
            return self._finish_reconciliation(
                request,
                state="blocked",
                detail="Published GitLab Note author or native identity diverged",
                error=str(exc),
            )
        except Exception as exc:  # noqa: BLE001 - observation failure is not deletion
            delay = min(
                self.retry_delay_seconds
                * (2 ** min(max(request.reconcile_attempts - 1, 0), 16)),
                self.max_retry_delay_seconds,
            )
            return self._finish_reconciliation(
                request,
                state="published",
                status="deferred",
                detail="Published GitLab Note reconciliation will be retried",
                error=str(exc),
                delay_seconds=delay,
            )
        exact_ids = tuple(note.note_id for note in lookup.exact)
        if persisted is None and not lookup.exact and not lookup.conflicts:
            state = "removed"
            detail = "Published GitLab Note was confirmed removed; it will not be recreated"
        elif (
            persisted is None
            or not self._valid_note(request, persisted)
            or lookup.conflicts
            or len(lookup.exact) != 1
            or exact_ids != (request.external_note_id,)
            or not self._valid_note(request, lookup.exact[0])
        ):
            state = "blocked"
            detail = "Published GitLab Note identity or content diverged"
        else:
            state = "published"
            detail = "Published GitLab Note remains present"
        return self._finish_reconciliation(
            request,
            state=state,
            detail=detail,
        )
