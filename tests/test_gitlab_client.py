from types import SimpleNamespace

import pytest

from review_agent_flow.gitlab import Note, canonical_note_marker
from review_agent_flow.gitlab_client import PythonGitLabClient


class ProjectId(str):
    pass


class FakeNotes:
    def __init__(self, notes_or_pages: list[SimpleNamespace] | list[list[SimpleNamespace]]) -> None:
        self.pages_mode = bool(notes_or_pages and isinstance(notes_or_pages[0], list))
        if self.pages_mode:
            self.pages = notes_or_pages  # type: ignore[assignment]
            self.notes = [note for page in self.pages for note in page]
        else:
            self.pages = [notes_or_pages]  # type: ignore[list-item]
            self.notes = notes_or_pages  # type: ignore[assignment]
        for note in self.notes:
            if not hasattr(note, "author"):
                note.author = {"username": "review-bot"}
        self.requested_pages: list[int] = []
        self.created_payloads: list[dict[str, str]] = []

    def create(self, payload: dict[str, str]) -> SimpleNamespace:
        self.created_payloads.append(payload)
        note = SimpleNamespace(
            id=99,
            body=payload["body"],
            author={"username": "review-bot"},
        )
        self.notes.append(note)
        return note

    def list(self, **kwargs: object) -> list[SimpleNamespace]:
        if self.pages_mode:
            if kwargs.get("get_all"):
                raise TypeError("pagination-only fake")
            assert kwargs["per_page"] == 100
            page = int(kwargs["page"])
            self.requested_pages.append(page)
            pages = self.pages
            return pages[page - 1] if page <= len(pages) else []
        assert kwargs == {"get_all": True}
        return self.notes


class FakeMergeRequests:
    def __init__(self, notes: FakeNotes, listed: list[SimpleNamespace] | None = None) -> None:
        self.notes = notes
        self.listed = listed or []

    def get(self, _iid: int) -> SimpleNamespace:
        return SimpleNamespace(notes=self.notes)

    def list(self, **kwargs: object) -> list[SimpleNamespace]:
        assert kwargs["state"] == "all"
        assert kwargs["source_branch"] == "autofix/MR-1/run-1"
        assert kwargs["target_branch"] == "feature"
        return self.listed


def _client(value: FakeNotes | SimpleNamespace, username: str | None = "review-bot") -> PythonGitLabClient:
    client = object.__new__(PythonGitLabClient)
    if isinstance(value, FakeNotes):
        value = SimpleNamespace(mergerequests=FakeMergeRequests(value))
    client._projects = {"42": value}
    client._review_bot_username = username
    return client


def test_note_marker_paginates_and_requires_review_bot_author() -> None:
    marker = canonical_note_marker(1)
    notes = FakeNotes(
        [
            [SimpleNamespace(body=marker, author={"username": "alice"}) for _ in range(100)],
            [SimpleNamespace(body=marker, author={"username": "review-bot"})],
        ]
    )
    project = SimpleNamespace(mergerequests=FakeMergeRequests(notes))

    assert _client(project).has_note_marker("42", 1, marker) is True
    assert notes.requested_pages == [1, 2]


def test_note_marker_is_not_trusted_without_review_bot_username() -> None:
    marker = canonical_note_marker(1)
    notes = FakeNotes([[SimpleNamespace(body=marker, author={"username": "review-bot"})]])
    project = SimpleNamespace(mergerequests=FakeMergeRequests(notes))

    assert _client(project, username=None).has_note_marker("42", 1, marker) is False
    assert notes.requested_pages == []


def test_note_marker_requires_one_standalone_canonical_line() -> None:
    marker = canonical_note_marker(1)
    notes = FakeNotes(
        [[
            SimpleNamespace(body=f"prefix {marker}"),
            SimpleNamespace(body=f"{marker}\n{marker}"),
        ]]
    )
    project = SimpleNamespace(mergerequests=FakeMergeRequests(notes))

    assert _client(project).has_note_marker("42", 1, marker) is False


def test_auto_fix_lookup_skips_old_same_branch_mr_without_current_marker() -> None:
    marker = "ReviewFlow-Auto-Fix-Run-ID:1 SHA:new-sha"
    notes = FakeNotes([[]])
    listed = [
        SimpleNamespace(
            iid=8,
            title="old",
            source_branch="autofix/MR-1/run-1",
            target_branch="feature",
            web_url="old-url",
            description="ReviewFlow-Auto-Fix-Run-ID:1 SHA:old-sha",
        ),
        SimpleNamespace(
            iid=9,
            title="new",
            source_branch="autofix/MR-1/run-1",
            target_branch="feature",
            web_url="new-url",
            description=marker,
        ),
    ]
    project = SimpleNamespace(mergerequests=FakeMergeRequests(notes, listed))

    found = _client(project).find_merge_request_by_source_branch(
        "42", "autofix/MR-1/run-1", "feature", marker
    )
    assert found is not None and found.iid == 9


def test_python_gitlab_note_adapter_returns_and_observes_native_id() -> None:
    marker = canonical_note_marker(7)
    notes = FakeNotes([SimpleNamespace(id=17, body=f"{marker}\nFinding")])
    client = _client(notes)

    created = client.create_note("42", 1, "created")
    observed = client.find_note_by_marker("42", 1, marker)

    assert created is None
    assert observed == Note("42", 1, 17, f"{marker}\nFinding")
    assert notes.created_payloads == [{"body": "created"}]


@pytest.mark.parametrize(
    "body",
    ["", "prefix {marker}", "{marker} suffix", "{marker}\n{marker}"],
)
def test_python_gitlab_note_observer_requires_one_exact_marker_line(body: str) -> None:
    marker = canonical_note_marker(7)
    notes = FakeNotes([SimpleNamespace(id=17, body=body.format(marker=marker))])
    assert _client(notes).find_note_by_marker("42", 1, marker) is None


def test_python_gitlab_note_observer_rejects_multiple_exact_matches() -> None:
    marker = canonical_note_marker(7)
    notes = FakeNotes(
        [
            SimpleNamespace(id=17, body=marker),
            SimpleNamespace(id=18, body=f"Finding\n{marker}"),
        ]
    )
    assert _client(notes).find_note_by_marker("42", 1, marker) is None


@pytest.mark.parametrize("project_id", ["", "   ", True, 1.0, ProjectId("42")])
def test_python_gitlab_note_observer_rejects_invalid_project_id_types(project_id: object) -> None:
    marker = canonical_note_marker(7)
    notes = FakeNotes([SimpleNamespace(id=17, body=marker)])
    assert _client(notes).find_note_by_marker(project_id, 1, marker) is None  # type: ignore[arg-type]


@pytest.mark.parametrize("mr_iid", [True, 1.0, "1", ProjectId("1"), 0, -1])
def test_python_gitlab_note_observer_rejects_invalid_mr_iid_types(mr_iid: object) -> None:
    marker = canonical_note_marker(7)
    notes = FakeNotes([SimpleNamespace(id=17, body=marker)])
    assert _client(notes).find_note_by_marker("42", mr_iid, marker) is None  # type: ignore[arg-type]


@pytest.mark.parametrize("project_id", ["", "   ", True, 1.0, ProjectId("42")])
def test_python_gitlab_note_resource_rejects_invalid_project_id_types(project_id: object) -> None:
    marker = canonical_note_marker(7)
    resource = SimpleNamespace(id=17, body=marker)
    assert PythonGitLabClient._note_from_resource(project_id, 1, resource) is None  # type: ignore[arg-type]


@pytest.mark.parametrize("mr_iid", [True, 1.0, "1", ProjectId("1"), 0, -1])
def test_python_gitlab_note_resource_rejects_invalid_mr_iid_types(mr_iid: object) -> None:
    marker = canonical_note_marker(7)
    resource = SimpleNamespace(id=17, body=marker)
    assert PythonGitLabClient._note_from_resource("42", mr_iid, resource) is None  # type: ignore[arg-type]


@pytest.mark.parametrize("native_id", [None, 0, -1, True, 1.0, "1"])
def test_python_gitlab_note_observer_rejects_non_positive_integer_ids(native_id: object) -> None:
    marker = canonical_note_marker(7)
    notes = FakeNotes([SimpleNamespace(id=native_id, body=marker)])
    assert _client(notes).find_note_by_marker("42", 1, marker) is None
