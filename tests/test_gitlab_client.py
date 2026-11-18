from types import SimpleNamespace

import pytest

from review_agent_flow.gitlab import (
    GitLabIdentityConflict,
    Note,
    NoteMarkerLookup,
    RepositoryAction,
    RepositoryCommitObservation,
    canonical_note_marker,
)
from review_agent_flow.gitlab_client import PythonGitLabClient

SHA = "a" * 40
NONCE = "b" * 32
AUTO_FIX_BRANCH = f"autofix/MR-1/sha-{SHA}/intent-{NONCE}"


class ProjectId(str):
    pass


class FakeGetError(RuntimeError):
    def __init__(self, response_code: int) -> None:
        super().__init__(f"GitLab HTTP {response_code}")
        self.response_code = response_code


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

    def get(self, note_id: int) -> SimpleNamespace:
        for note in self.notes:
            if getattr(note, "id", None) == note_id:
                return note
        raise FakeGetError(404)


class FakeMergeRequests:
    def __init__(self, notes: FakeNotes, listed: list[SimpleNamespace] | None = None) -> None:
        self.notes = notes
        self.listed = listed or []

    def get(self, _iid: int) -> SimpleNamespace:
        return SimpleNamespace(notes=self.notes)

    def list(self, **kwargs: object) -> list[SimpleNamespace]:
        assert kwargs["state"] == "all"
        assert kwargs["source_branch"] == AUTO_FIX_BRANCH
        assert kwargs["target_branch"] == "feature"
        return self.listed


def _client(
    value: FakeNotes | SimpleNamespace, username: str | None = "review-bot"
) -> PythonGitLabClient:
    client = object.__new__(PythonGitLabClient)
    if isinstance(value, FakeNotes):
        value = SimpleNamespace(mergerequests=FakeMergeRequests(value))
    client._projects = {"42": value}
    client._review_bot_username = username
    return client


def test_merge_request_preserves_source_project_identity() -> None:
    resource = SimpleNamespace(
        project_id=42,
        iid=1,
        title="Fork contribution",
        author={"username": "alice"},
        source_branch="feature",
        target_branch="main",
        source_project_id=99,
        diff_refs={"base_sha": "b" * 40, "start_sha": "c" * 40, "head_sha": SHA},
        web_url="https://mr",
        draft=False,
        work_in_progress=False,
        description="",
        state="opened",
        merged_at=None,
        merge_commit_sha=None,
        merged_commit_sha=None,
    )
    project = SimpleNamespace(mergerequests=SimpleNamespace(get=lambda _iid: resource))

    merge_request = _client(project).get_merge_request("42", 1)

    assert merge_request.source_project_id == "99"
    assert merge_request.base_sha == "b" * 40
    assert merge_request.start_sha == "c" * 40


@pytest.mark.parametrize(
    ("native_project_id", "native_iid"),
    [(99, 1), (42, 9), (None, 1), (42, None)],
)
def test_merge_request_rejects_native_identity_mismatch(
    native_project_id: int | None, native_iid: int | None
) -> None:
    resource = SimpleNamespace(project_id=native_project_id, iid=native_iid)
    project = SimpleNamespace(mergerequests=SimpleNamespace(get=lambda _iid: resource))

    with pytest.raises(
        GitLabIdentityConflict,
        match="outside the requested identity",
    ):
        _client(project).get_merge_request("42", 1)


def test_autofix_commit_uses_start_sha_and_never_forces() -> None:
    payloads: list[dict[str, object]] = []

    def create(payload: dict[str, object]) -> SimpleNamespace:
        payloads.append(payload)
        return SimpleNamespace(id="f" * 40)

    project = SimpleNamespace(commits=SimpleNamespace(create=create))
    client = _client(project)
    actions = ({"action": "delete", "file_path": "old.txt"},)

    commit = client.create_repository_commit(
        "42",
        "autofix/branch",
        SHA,
        "Auto-fix",
        actions,
        force=False,
    )

    assert commit.sha == "f" * 40
    assert payloads == [
        {
            "branch": "autofix/branch",
            "start_sha": SHA,
            "commit_message": "Auto-fix",
            "actions": list(actions),
            "force": False,
        }
    ]


def test_explicit_recovery_observes_exact_remote_actions_and_parent() -> None:
    commit_sha = "f" * 40
    diffs = [
        {
            "old_path": "created.txt",
            "new_path": "created.txt",
            "a_mode": "000000",
            "b_mode": "100644",
            "new_file": True,
            "deleted_file": False,
            "renamed_file": False,
            "diff": "created",
        },
        {
            "old_path": "deleted.txt",
            "new_path": "deleted.txt",
            "a_mode": "100644",
            "b_mode": "000000",
            "new_file": False,
            "deleted_file": True,
            "renamed_file": False,
            "diff": "deleted",
        },
        {
            "old_path": "mode.sh",
            "new_path": "mode.sh",
            "a_mode": "100644",
            "b_mode": "100755",
            "new_file": False,
            "deleted_file": False,
            "renamed_file": False,
            "diff": "",
        },
        {
            "old_path": "updated.txt",
            "new_path": "updated.txt",
            "a_mode": "100644",
            "b_mode": "100644",
            "new_file": False,
            "deleted_file": False,
            "renamed_file": False,
            "diff": "updated",
        },
    ]
    commit = SimpleNamespace(
        id=commit_sha,
        parent_ids=[SHA],
        diff=lambda **kwargs: diffs,
    )
    contents = {
        "created.txt": b"created\n",
        "mode.sh": b"#!/bin/sh\n",
        "updated.txt": b"updated\n",
    }
    files = SimpleNamespace(
        get=lambda *, file_path, ref: SimpleNamespace(decode=lambda: contents[file_path])
    )
    project = SimpleNamespace(
        commits=SimpleNamespace(get=lambda sha: commit),
        files=files,
    )

    observed = _client(project).observe_repository_commit("42", commit_sha)

    assert observed == RepositoryCommitObservation(
        "42",
        commit_sha,
        (SHA,),
        (
            RepositoryAction("create", "created.txt", "created\n", False),
            RepositoryAction("delete", "deleted.txt"),
            RepositoryAction("chmod", "mode.sh", execute_filemode=True),
            RepositoryAction("update", "updated.txt", "updated\n", False),
        ),
    )


def test_explicit_recovery_rejects_incomplete_remote_diff() -> None:
    commit_sha = "f" * 40
    commit = SimpleNamespace(
        id=commit_sha,
        parent_ids=[SHA],
        diff=lambda **kwargs: [
            {
                "old_path": "large.txt",
                "new_path": "large.txt",
                "a_mode": "100644",
                "b_mode": "100644",
                "collapsed": True,
            }
        ],
    )
    project = SimpleNamespace(commits=SimpleNamespace(get=lambda sha: commit))

    with pytest.raises(ValueError, match="omitted"):
        _client(project).observe_repository_commit("42", commit_sha)


def test_explicit_recovery_does_not_charge_chmod_files_as_content_actions() -> None:
    commit_sha = "f" * 40
    paths = [f"script-{index}.sh" for index in range(6)]
    diffs = [
        {
            "old_path": path,
            "new_path": path,
            "a_mode": "100644",
            "b_mode": "100755",
            "new_file": False,
            "deleted_file": False,
            "renamed_file": False,
            "diff": "",
        }
        for path in paths
    ]
    commit = SimpleNamespace(
        id=commit_sha,
        parent_ids=[SHA],
        diff=lambda **kwargs: diffs,
    )
    unchanged_content = b"x" * (900 * 1024)
    files = SimpleNamespace(
        get=lambda *, file_path, ref: SimpleNamespace(decode=lambda: unchanged_content)
    )
    project = SimpleNamespace(
        commits=SimpleNamespace(get=lambda sha: commit),
        files=files,
    )

    observed = _client(project).observe_repository_commit("42", commit_sha)

    assert observed.actions == tuple(
        RepositoryAction("chmod", path, execute_filemode=True) for path in paths
    )


def test_autofix_merge_request_freezes_creation_readiness_and_bot_author() -> None:
    payloads: list[dict[str, object]] = []

    def create(payload: dict[str, object]) -> SimpleNamespace:
        payloads.append(payload)
        return SimpleNamespace(
            iid=9,
            title=payload["title"],
            source_branch=payload["source_branch"],
            target_branch=payload["target_branch"],
            description=payload["description"],
            web_url="https://mr/9",
            author={"username": "review-bot"},
            draft=True,
            work_in_progress=False,
        )

    project = SimpleNamespace(mergerequests=SimpleNamespace(create=create))
    client = _client(project)

    merge_request = client.create_merge_request(
        "42",
        "Draft: [ReviewFlow Auto-Fix] Parent",
        "autofix/branch",
        "feature",
        "marker",
        draft=True,
    )

    assert merge_request.author_username == "review-bot"
    assert merge_request.draft is True
    assert payloads[0]["title"] == "Draft: [ReviewFlow Auto-Fix] Parent"
    assert "draft" not in payloads[0]


def test_note_marker_paginates_and_requires_review_bot_author() -> None:
    marker = canonical_note_marker("42", 1, SHA)
    notes = FakeNotes(
        [
            [
                SimpleNamespace(id=index + 1, body=marker, author={"username": "alice"})
                for index in range(100)
            ],
            [SimpleNamespace(id=101, body=marker, author={"username": "review-bot"})],
        ]
    )
    project = SimpleNamespace(mergerequests=FakeMergeRequests(notes))

    assert _client(project).has_note_marker("42", 1, marker) is True
    assert notes.requested_pages == [1, 2]


def test_note_marker_is_not_trusted_without_review_bot_username() -> None:
    marker = canonical_note_marker("42", 1, SHA)
    notes = FakeNotes([[SimpleNamespace(body=marker, author={"username": "review-bot"})]])
    project = SimpleNamespace(mergerequests=FakeMergeRequests(notes))

    assert _client(project, username=None).has_note_marker("42", 1, marker) is False
    assert notes.requested_pages == []


def test_note_marker_requires_one_standalone_canonical_line() -> None:
    marker = canonical_note_marker("42", 1, SHA)
    notes = FakeNotes(
        [
            [
                SimpleNamespace(body=f"prefix {marker}"),
                SimpleNamespace(body=f"{marker}\n{marker}"),
            ]
        ]
    )
    project = SimpleNamespace(mergerequests=FakeMergeRequests(notes))

    assert _client(project).has_note_marker("42", 1, marker) is False


def test_canonical_note_marker_uses_only_the_review_key() -> None:
    assert canonical_note_marker("42", 7, SHA) == (
        f"<!-- reviewflow:v1;project=42;mr=7;sha={SHA};effect=note -->"
    )


@pytest.mark.parametrize(
    ("project_id", "mr_iid", "sha"),
    [
        ("", 1, SHA),
        ("42;effect=autofix", 1, SHA),
        ("42", True, SHA),
        ("42", 0, SHA),
        ("42", 1, "short"),
    ],
)
def test_canonical_note_marker_rejects_noncanonical_review_keys(
    project_id: object, mr_iid: object, sha: object
) -> None:
    with pytest.raises(ValueError):
        canonical_note_marker(project_id, mr_iid, sha)  # type: ignore[arg-type]


def test_auto_fix_lookup_skips_old_same_branch_mr_without_current_marker() -> None:
    marker = (
        f"<!-- reviewflow:v1;project=42;mr=1;sha={SHA};effect=autofix;"
        f"intent={NONCE};plan_sha256={'e' * 64} -->"
    )
    notes = FakeNotes([[]])
    listed = [
        SimpleNamespace(
            iid=8,
            title="old",
            author={"username": "review-bot"},
            source_branch=AUTO_FIX_BRANCH,
            target_branch="feature",
            web_url="old-url",
            description="old marker",
        ),
        SimpleNamespace(
            iid=9,
            title="new",
            author={"username": "review-bot"},
            source_branch=AUTO_FIX_BRANCH,
            target_branch="feature",
            web_url="new-url",
            description=marker,
        ),
    ]
    project = SimpleNamespace(mergerequests=FakeMergeRequests(notes, listed))

    found = _client(project).find_merge_request_by_source_branch(
        "42", AUTO_FIX_BRANCH, "feature", marker
    )
    assert found is not None and found.iid == 9


def test_auto_fix_lookup_paginates_before_proving_unique_identity() -> None:
    marker = (
        f"<!-- reviewflow:v1;project=42;mr=1;sha={SHA};effect=autofix;"
        f"intent={NONCE};plan_sha256={'e' * 64} -->"
    )

    def resource(iid: int, description: str) -> SimpleNamespace:
        return SimpleNamespace(
            iid=iid,
            title="Auto-fix",
            author={"username": "review-bot"},
            source_branch=AUTO_FIX_BRANCH,
            target_branch="feature",
            web_url=f"https://mr/{iid}",
            description=description,
        )

    class PaginatedMergeRequests:
        def list(self, **kwargs: object) -> list[SimpleNamespace]:
            if kwargs.get("get_all"):
                raise TypeError("pagination-only fake")
            page = int(kwargs["page"])
            if page == 1:
                return [resource(1, marker)] + [
                    resource(index, "old marker") for index in range(2, 101)
                ]
            if page == 2:
                return [resource(101, marker)]
            return []

    project = SimpleNamespace(mergerequests=PaginatedMergeRequests())

    with pytest.raises(ValueError, match="multiple"):
        _client(project).find_merge_request_by_source_branch(
            "42", AUTO_FIX_BRANCH, "feature", marker
        )


def test_python_gitlab_note_adapter_returns_and_observes_native_id() -> None:
    marker = canonical_note_marker("42", 1, SHA)
    notes = FakeNotes([SimpleNamespace(id=17, body=f"{marker}\nFinding")])
    client = _client(notes)

    created = client.create_note("42", 1, "created")
    observed = client.find_note_by_marker("42", 1, marker)

    assert created == Note("42", 1, 99, "created")
    assert observed == Note("42", 1, 17, f"{marker}\nFinding")
    assert notes.created_payloads == [{"body": "created"}]


def test_python_gitlab_created_note_requires_configured_bot_author() -> None:
    notes = FakeNotes([])

    def create_as_alice(payload: dict[str, str]) -> SimpleNamespace:
        return SimpleNamespace(
            id=99,
            body=payload["body"],
            author={"username": "alice"},
        )

    notes.create = create_as_alice  # type: ignore[method-assign]
    with pytest.raises(ValueError, match="author"):
        _client(notes).create_note("42", 1, "created")


def test_python_gitlab_get_trusted_note_distinguishes_404_and_author_conflict() -> None:
    notes = FakeNotes([SimpleNamespace(id=17, body="body", author={"username": "review-bot"})])
    client = _client(notes)

    assert client.get_trusted_note("42", 1, 17) == Note("42", 1, 17, "body")
    assert client.get_trusted_note("42", 1, 18) is None
    notes.notes[0].author = {"username": "alice"}
    with pytest.raises(ValueError, match="author"):
        client.get_trusted_note("42", 1, 17)


def test_python_gitlab_note_lookup_reports_exact_and_conflicting_bot_notes() -> None:
    marker = canonical_note_marker("42", 1, SHA)
    notes = FakeNotes(
        [
            SimpleNamespace(id=17, body=f"{marker}\nFinding"),
            SimpleNamespace(id=18, body=f"prefix {marker}"),
            SimpleNamespace(id=19, body=marker, author={"username": "alice"}),
        ]
    )

    assert _client(notes).lookup_note_marker("42", 1, marker) == NoteMarkerLookup(
        (Note("42", 1, 17, f"{marker}\nFinding"),),
        (Note("42", 1, 18, f"prefix {marker}"),),
    )


@pytest.mark.parametrize(
    "body",
    ["", "prefix {marker}", "{marker} suffix", "{marker}\n{marker}"],
)
def test_python_gitlab_note_observer_requires_one_exact_marker_line(body: str) -> None:
    marker = canonical_note_marker("42", 1, SHA)
    notes = FakeNotes([SimpleNamespace(id=17, body=body.format(marker=marker))])
    assert _client(notes).find_note_by_marker("42", 1, marker) is None


def test_python_gitlab_note_observer_rejects_multiple_exact_matches() -> None:
    marker = canonical_note_marker("42", 1, SHA)
    notes = FakeNotes(
        [
            SimpleNamespace(id=17, body=marker),
            SimpleNamespace(id=18, body=f"Finding\n{marker}"),
        ]
    )
    assert _client(notes).find_note_by_marker("42", 1, marker) is None


@pytest.mark.parametrize("project_id", ["", "   ", True, 1.0, ProjectId("42")])
def test_python_gitlab_note_observer_rejects_invalid_project_id_types(project_id: object) -> None:
    marker = canonical_note_marker("42", 1, SHA)
    notes = FakeNotes([SimpleNamespace(id=17, body=marker)])
    assert _client(notes).find_note_by_marker(project_id, 1, marker) is None  # type: ignore[arg-type]


@pytest.mark.parametrize("mr_iid", [True, 1.0, "1", ProjectId("1"), 0, -1])
def test_python_gitlab_note_observer_rejects_invalid_mr_iid_types(mr_iid: object) -> None:
    marker = canonical_note_marker("42", 1, SHA)
    notes = FakeNotes([SimpleNamespace(id=17, body=marker)])
    assert _client(notes).find_note_by_marker("42", mr_iid, marker) is None  # type: ignore[arg-type]


@pytest.mark.parametrize("project_id", ["", "   ", True, 1.0, ProjectId("42")])
def test_python_gitlab_note_resource_rejects_invalid_project_id_types(project_id: object) -> None:
    marker = canonical_note_marker("42", 1, SHA)
    resource = SimpleNamespace(id=17, body=marker)
    assert PythonGitLabClient._note_from_resource(project_id, 1, resource) is None  # type: ignore[arg-type]


@pytest.mark.parametrize("mr_iid", [True, 1.0, "1", ProjectId("1"), 0, -1])
def test_python_gitlab_note_resource_rejects_invalid_mr_iid_types(mr_iid: object) -> None:
    marker = canonical_note_marker("42", 1, SHA)
    resource = SimpleNamespace(id=17, body=marker)
    assert PythonGitLabClient._note_from_resource("42", mr_iid, resource) is None  # type: ignore[arg-type]


@pytest.mark.parametrize("native_id", [None, 0, -1, True, 1.0, "1"])
def test_python_gitlab_note_observer_rejects_non_positive_integer_ids(native_id: object) -> None:
    marker = canonical_note_marker("42", 1, SHA)
    notes = FakeNotes([SimpleNamespace(id=native_id, body=marker)])
    assert _client(notes).find_note_by_marker("42", 1, marker) is None
