from types import SimpleNamespace

from review_agent_flow.gitlab_client import PythonGitLabClient


class FakeNotes:
    def __init__(self, pages: list[list[SimpleNamespace]]) -> None:
        self.pages = pages
        self.requested_pages: list[int] = []

    def list(self, *, per_page: int, page: int) -> list[SimpleNamespace]:
        assert per_page == 100
        self.requested_pages.append(page)
        return self.pages[page - 1] if page <= len(self.pages) else []


class FakeMergeRequests:
    def __init__(self, notes: FakeNotes, listed: list[SimpleNamespace] | None = None) -> None:
        self.notes = notes
        self.listed = listed or []

    def get(self, iid: int) -> SimpleNamespace:
        return SimpleNamespace(notes=self.notes)

    def list(self, **kwargs: object) -> list[SimpleNamespace]:
        assert kwargs["state"] == "all"
        assert kwargs["source_branch"] == "autofix/MR-1/run-1"
        assert kwargs["target_branch"] == "feature"
        return self.listed


def _client(project: SimpleNamespace, username: str | None = "review-bot") -> PythonGitLabClient:
    client = object.__new__(PythonGitLabClient)
    client._projects = {"42": project}
    client._review_bot_username = username
    return client


def test_note_marker_paginates_and_requires_review_bot_author() -> None:
    marker = "ReviewFlow-Run-ID:1 SHA:abc"
    notes = FakeNotes(
        [
            [SimpleNamespace(body=marker, author={"username": "alice"}) for _ in range(100)],
            [SimpleNamespace(body=marker, author={"username": "review-bot"})],
        ]
    )
    project = SimpleNamespace(mergerequests=FakeMergeRequests(notes))
    client = _client(project)

    assert client.has_note_marker("42", 1, marker) is True
    assert notes.requested_pages == [1, 2]


def test_note_marker_is_not_trusted_without_review_bot_username() -> None:
    marker = "ReviewFlow-Run-ID:1 SHA:abc"
    notes = FakeNotes([[SimpleNamespace(body=marker, author={"username": "review-bot"})]])
    project = SimpleNamespace(mergerequests=FakeMergeRequests(notes))
    client = _client(project, username=None)

    assert client.has_note_marker("42", 1, marker) is False
    assert notes.requested_pages == []


def test_auto_fix_lookup_skips_old_same_branch_mr_without_current_marker() -> None:
    marker = "ReviewFlow-Auto-Fix-Run-ID:1 SHA:new-sha"
    notes = FakeNotes([[]])
    project = SimpleNamespace(
        mergerequests=FakeMergeRequests(
            notes,
            [
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
            ],
        )
    )
    client = _client(project)

    found = client.find_merge_request_by_source_branch(
        "42", "autofix/MR-1/run-1", "feature", marker
    )

    assert found is not None and found.iid == 9
