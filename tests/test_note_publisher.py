from __future__ import annotations

from pathlib import Path

import pytest

from review_agent_flow.gitlab import (
    MergeRequest,
    Note,
    NoteMarkerLookup,
    canonical_note_marker,
    has_exact_note_marker,
)
from review_agent_flow.note_publisher import NotePublisher
from review_agent_flow.store import NotePublicationRequest, SQLiteStore

SHA = "a" * 40
NEW_SHA = "b" * 40


class NoteGitLab:
    def __init__(self, sha: str = SHA) -> None:
        self.mr = MergeRequest(
            "42", 1, "Review", "alice", "feature", "main", sha, "https://mr"
        )
        self.notes: list[Note] = []
        self.create_calls = 0
        self.lookup_error: Exception | None = None
        self.raise_after_create = False
        self.raise_before_create = False

    def get_merge_request(self, project_id: str, mr_iid: int) -> MergeRequest:
        return self.mr

    def lookup_note_marker(
        self, project_id: str, mr_iid: int, marker: str
    ) -> NoteMarkerLookup:
        if self.lookup_error is not None:
            raise self.lookup_error
        candidates = tuple(note for note in self.notes if marker in note.body)
        return NoteMarkerLookup(
            tuple(note for note in candidates if has_exact_note_marker(note.body, marker)),
            tuple(note for note in candidates if not has_exact_note_marker(note.body, marker)),
        )

    def create_note(self, project_id: str, mr_iid: int, body: str) -> Note:
        self.create_calls += 1
        if self.raise_before_create:
            self.raise_before_create = False
            raise RuntimeError("connection lost before Note creation")
        note = Note(project_id, mr_iid, 100 + self.create_calls, body)
        self.notes.append(note)
        if self.raise_after_create:
            self.raise_after_create = False
            raise RuntimeError("connection lost after Note creation")
        return note

    def get_trusted_note(
        self, project_id: str, mr_iid: int, note_id: int
    ) -> Note | None:
        return next((note for note in self.notes if note.note_id == note_id), None)


def _request(database: Path, markdown: str = "Actionable review") -> tuple[
    SQLiteStore, NotePublicationRequest
]:
    store = SQLiteStore(database)
    run = store.enqueue("42", 1, SHA)
    claimed = store.claim_next()
    assert claimed is not None
    marker = canonical_note_marker("42", 1, SHA)
    assert store.freeze_requested_outputs(
        run.run_id,
        claimed.claim_token,
        human_review=False,
        ai_review=True,
        auto_fix=False,
        notification_channels=(),
    )
    assert store.freeze_effective_run_decision(
        run.run_id,
        claimed.claim_token,
        decision="execute",
        human_review=False,
        ai_review=True,
        auto_fix=False,
        suppressed=False,
        recipients=(),
        notification_channels=(),
        expectations=(("note", marker),),
    )
    receipt = store.create_effect_receipt(
        run.run_id, "note_expected", marker, state="planned"
    )
    request = store.create_note_publication_request(
        run.run_id, receipt.receipt_id, markdown, claimed.claim_token
    )
    assert request is not None
    assert store.finish(run.run_id, "succeeded", claim_token=claimed.claim_token)
    return store, request


def test_note_request_is_frozen_and_claim_fenced(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "review.db")
    run = store.enqueue("42", 1, SHA)
    claimed = store.claim_next()
    assert claimed is not None
    marker = canonical_note_marker("42", 1, SHA)
    receipt = store.create_effect_receipt(
        run.run_id, "note_expected", marker, state="planned"
    )

    request = store.create_note_publication_request(
        run.run_id, receipt.receipt_id, "Raw **Markdown**", claimed.claim_token
    )

    assert request is not None
    assert request.body == f"{marker}\nRaw **Markdown**"
    assert request.state == "pending"
    assert store.list_effect_receipts(run.run_id, "note_expected")[0].state == "pending"
    same = store.create_note_publication_request(
        run.run_id, receipt.receipt_id, "Raw **Markdown**", claimed.claim_token
    )
    assert same == request
    with pytest.raises(ValueError, match="immutable"):
        store.create_note_publication_request(
            run.run_id, receipt.receipt_id, "Different", claimed.claim_token
        )

    publication_claim = store.claim_note_publication(request_id=request.request_id)
    assert publication_claim is not None
    assert store.claim_note_publication(request_id=request.request_id) is None
    assert not store.finish_note_publication(
        request.request_id, "wrong-token", state="published", external_note_id=9
    )
    assert store.finish_note_publication(
        request.request_id,
        publication_claim.claim_token,
        state="published",
        external_note_id=9,
        detail="published",
    )
    assert store.list_effect_receipts(run.run_id, "note_expected")[0].state == "published"


def test_note_request_blocks_marker_in_agent_markdown_without_external_write(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "review.db")
    run = store.enqueue("42", 1, SHA)
    claimed = store.claim_next()
    assert claimed is not None
    marker = canonical_note_marker("42", 1, SHA)
    receipt = store.create_effect_receipt(run.run_id, "note_expected", marker)

    request = store.create_note_publication_request(
        run.run_id, receipt.receipt_id, marker, claimed.claim_token
    )

    assert request is not None and request.state == "blocked"
    assert "conflicts" in (request.last_error or "")
    assert store.list_effect_receipts(run.run_id, "note_expected")[0].state == "blocked"


def test_note_publisher_creates_frozen_body_and_persists_identity(tmp_path: Path) -> None:
    store, request = _request(tmp_path / "review.db")
    gitlab = NoteGitLab()

    results = NotePublisher(store, gitlab, "42").process(request_id=request.request_id)

    assert results[0].status == "published"
    assert gitlab.create_calls == 1
    saved = store.get_note_publication_request(request.request_id)
    assert saved is not None
    assert saved.state == "published"
    assert saved.external_note_id == 101
    assert gitlab.notes == [Note("42", 1, 101, request.body)]
    receipt = store.list_effect_receipts(request.run_id, "note_expected")[0]
    assert receipt.external_identity == "gitlab:42!1#note-101"


def test_published_note_reconciliation_claim_is_token_fenced(tmp_path: Path) -> None:
    store, request = _request(tmp_path / "review.db")
    gitlab = NoteGitLab()
    publisher = NotePublisher(store, gitlab, "42", reconciliation_interval_seconds=0)
    assert publisher.process(request_id=request.request_id)[0].status == "published"
    with store._connect() as connection:
        retention_at = connection.execute(
            "SELECT payload_retention_at FROM note_publication_requests WHERE id = ?",
            (request.request_id,),
        ).fetchone()[0]
    assert retention_at is not None

    claimed = store.claim_published_note_reconciliation(request_id=request.request_id)
    assert claimed is not None
    assert store.claim_published_note_reconciliation(request_id=request.request_id) is None
    assert not store.finish_published_note_reconciliation(
        request.request_id,
        "wrong-token",
        state="published",
        detail="still present",
    )
    assert store.finish_published_note_reconciliation(
        request.request_id,
        claimed.reconcile_claim_token,
        state="published",
        detail="still present",
    )
    with store._connect() as connection:
        reconciled_retention_at = connection.execute(
            "SELECT payload_retention_at FROM note_publication_requests WHERE id = ?",
            (request.request_id,),
        ).fetchone()[0]
    assert reconciled_retention_at == retention_at


def test_note_publisher_adopts_exact_existing_note_after_uncertain_create(
    tmp_path: Path,
) -> None:
    store, request = _request(tmp_path / "review.db")
    gitlab = NoteGitLab()
    gitlab.raise_after_create = True
    publisher = NotePublisher(store, gitlab, "42", retry_delay_seconds=0)

    first = publisher.process(request_id=request.request_id)
    second = publisher.process(request_id=request.request_id)

    assert first[0].status == "pending"
    assert second[0].status == "published"
    assert gitlab.create_calls == 1
    assert store.get_note_publication_request(request.request_id).external_note_id == 101  # type: ignore[union-attr]


def test_final_uncertain_create_gets_lookup_only_recovery_and_adopts_after_head_change(
    tmp_path: Path,
) -> None:
    store, request = _request(tmp_path / "review.db")
    gitlab = NoteGitLab()
    gitlab.raise_after_create = True
    publisher = NotePublisher(
        store, gitlab, "42", max_attempts=1, retry_delay_seconds=0
    )

    first = publisher.process(request_id=request.request_id)[0]
    gitlab.mr = MergeRequest(
        "42", 1, "Review", "alice", "feature", "main", NEW_SHA, "https://mr"
    )
    second = publisher.process(request_id=request.request_id)[0]

    assert first.status == "pending"
    assert second.status == "published"
    assert gitlab.create_calls == 1


def test_final_uncertain_create_reserves_a_lookup_only_attempt(tmp_path: Path) -> None:
    store, request = _request(tmp_path / "review.db")
    gitlab = NoteGitLab()
    gitlab.raise_before_create = True
    publisher = NotePublisher(
        store, gitlab, "42", max_attempts=1, retry_delay_seconds=0
    )

    first = publisher.process(request_id=request.request_id)[0]
    second = publisher.process(request_id=request.request_id)[0]

    assert first.status == "pending"
    assert second.status == "blocked"
    assert gitlab.create_calls == 1


@pytest.mark.parametrize("conflict", ["duplicate", "malformed", "content"])
def test_note_publisher_blocks_ambiguous_or_conflicting_identity(
    tmp_path: Path, conflict: str
) -> None:
    store, request = _request(tmp_path / "review.db")
    gitlab = NoteGitLab()
    if conflict == "duplicate":
        gitlab.notes = [Note("42", 1, 1, request.body), Note("42", 1, 2, request.body)]
    elif conflict == "malformed":
        gitlab.notes = [Note("42", 1, 1, f"prefix {request.marker}")]
    else:
        gitlab.notes = [Note("42", 1, 1, f"{request.marker}\nDifferent review")]

    result = NotePublisher(store, gitlab, "42").process(request_id=request.request_id)[0]

    assert result.status == "blocked"
    assert gitlab.create_calls == 0


def test_stale_parent_supersedes_note_without_creating_it(tmp_path: Path) -> None:
    store, request = _request(tmp_path / "review.db")
    gitlab = NoteGitLab(NEW_SHA)

    result = NotePublisher(store, gitlab, "42").process(request_id=request.request_id)[0]

    assert result.status == "superseded"
    assert gitlab.create_calls == 0
    assert store.project_review_outcome(request.run_id) == "no_effect"
    status = store.get_project_bound_review_run_status("42", request.run_id)
    assert status is not None and status["review_outcome"] == "no_effect"


def test_confirmed_deleted_note_is_marked_removed_and_never_recreated(tmp_path: Path) -> None:
    store, request = _request(tmp_path / "review.db")
    gitlab = NoteGitLab()
    publisher = NotePublisher(store, gitlab, "42", reconciliation_interval_seconds=0)
    assert publisher.process(request_id=request.request_id)[0].status == "published"
    gitlab.notes.clear()

    reconciled = publisher.reconcile_published(request.request_id)
    retried = publisher.process(request_id=request.request_id)

    assert reconciled is not None and reconciled.status == "removed"
    assert retried == ()
    assert gitlab.create_calls == 1


def test_reconciliation_outage_does_not_claim_note_deletion(tmp_path: Path) -> None:
    store, request = _request(tmp_path / "review.db")
    gitlab = NoteGitLab()
    publisher = NotePublisher(
        store,
        gitlab,
        "42",
        retry_delay_seconds=0,
        reconciliation_interval_seconds=0,
    )
    assert publisher.process(request_id=request.request_id)[0].status == "published"
    gitlab.lookup_error = RuntimeError("lookup unavailable")

    result = publisher.reconcile_published(request.request_id)

    assert result is not None and result.status == "deferred"
    assert store.get_note_publication_request(request.request_id).state == "published"  # type: ignore[union-attr]


def test_existing_native_note_with_removed_marker_is_blocked_not_removed(
    tmp_path: Path,
) -> None:
    store, request = _request(tmp_path / "review.db")
    gitlab = NoteGitLab()
    publisher = NotePublisher(store, gitlab, "42", reconciliation_interval_seconds=0)
    assert publisher.process(request_id=request.request_id)[0].status == "published"
    gitlab.notes[0] = Note("42", 1, 101, "user edited the marker away")

    result = publisher.reconcile_published(request.request_id)

    assert result is not None and result.status == "blocked"
    assert store.get_note_publication_request(request.request_id).state == "blocked"  # type: ignore[union-attr]


def test_one_batch_does_not_reclaim_the_same_retryable_note(tmp_path: Path) -> None:
    store, request = _request(tmp_path / "review.db")
    gitlab = NoteGitLab()
    gitlab.lookup_error = RuntimeError("lookup unavailable")
    publisher = NotePublisher(store, gitlab, "42", retry_delay_seconds=0)

    results = publisher.process(limit=3)

    assert len(results) == 1
    saved = store.get_note_publication_request(request.request_id)
    assert saved is not None and saved.attempts == 1 and saved.state == "pending"
