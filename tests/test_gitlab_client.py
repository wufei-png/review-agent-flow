from types import SimpleNamespace

import pytest

from review_agent_flow.gitlab import Note, canonical_note_marker
from review_agent_flow.gitlab_client import PythonGitLabClient


class ProjectId(str):
    pass


class FakeNotes:
    def __init__(self, notes: list[SimpleNamespace]) -> None:
        self.notes = notes
        self.created_payloads: list[dict[str, str]] = []

    def create(self, payload: dict[str, str]) -> SimpleNamespace:
        self.created_payloads.append(payload)
        note = SimpleNamespace(id=99, body=payload["body"])
        self.notes.append(note)
        return note

    def list(self, **kwargs: object) -> list[SimpleNamespace]:
        assert kwargs == {"get_all": True}
        return self.notes


def _client(notes: FakeNotes) -> PythonGitLabClient:
    client = object.__new__(PythonGitLabClient)
    client._projects = {
        "42": SimpleNamespace(
            mergerequests=SimpleNamespace(
                get=lambda _mr_iid: SimpleNamespace(notes=notes),
            )
        )
    }
    return client


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
    [
        "",
        "prefix {marker}",
        "{marker} suffix",
        "{marker}\n{marker}",
    ],
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
def test_python_gitlab_note_observer_rejects_invalid_project_id_types(
    project_id: object,
) -> None:
    marker = canonical_note_marker(7)
    notes = FakeNotes([SimpleNamespace(id=17, body=marker)])

    assert _client(notes).find_note_by_marker(project_id, 1, marker) is None  # type: ignore[arg-type]


@pytest.mark.parametrize("mr_iid", [True, 1.0, "1", ProjectId("1"), 0, -1])
def test_python_gitlab_note_observer_rejects_invalid_mr_iid_types(mr_iid: object) -> None:
    marker = canonical_note_marker(7)
    notes = FakeNotes([SimpleNamespace(id=17, body=marker)])

    assert _client(notes).find_note_by_marker("42", mr_iid, marker) is None  # type: ignore[arg-type]


@pytest.mark.parametrize("project_id", ["", "   ", True, 1.0, ProjectId("42")])
def test_python_gitlab_note_resource_rejects_invalid_project_id_types(
    project_id: object,
) -> None:
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
