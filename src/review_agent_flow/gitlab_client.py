"""python-gitlab implementation kept behind the core GitLab protocol."""

from __future__ import annotations

import os
from typing import Any

from .gitlab import (
    CreatedMergeRequest,
    GitLabClient,
    MergeRequest,
    Note,
    Pipeline,
    has_exact_note_marker,
)


class PythonGitLabClient(GitLabClient):
    def __init__(
        self,
        url: str,
        private_token: str,
        review_bot_username: str | None = None,
    ) -> None:
        try:
            import gitlab
        except ImportError as exc:  # pragma: no cover - exercised in deployment
            raise RuntimeError("install the 'python-gitlab' dependency to use GitLabClient") from exc
        self._gitlab = gitlab.Gitlab(url, private_token=private_token)
        self._projects: dict[str, Any] = {}
        self._review_bot_username = review_bot_username or os.getenv(
            "GITLAB_REVIEW_BOT_USERNAME"
        )

    def _project(self, project_id: str) -> Any:
        if project_id not in self._projects:
            self._projects[project_id] = self._gitlab.projects.get(project_id)
        return self._projects[project_id]

    def get_merge_request(self, project_id: str, mr_iid: int) -> MergeRequest:
        mr = self._project(project_id).mergerequests.get(mr_iid)
        diff_refs = getattr(mr, "diff_refs", {}) or {}
        author = getattr(mr, "author", {}) or {}
        state = str(getattr(mr, "state", "opened") or "opened")
        if state == "opened" and getattr(mr, "merged_at", None):
            state = "merged"
        merged_sha = getattr(mr, "merge_commit_sha", None) or getattr(
            mr, "merged_commit_sha", None
        )
        return MergeRequest(
            project_id=str(project_id),
            iid=mr_iid,
            title=str(mr.title),
            author_username=str(author.get("username") or ""),
            source_branch=str(mr.source_branch),
            target_branch=str(mr.target_branch),
            sha=str(diff_refs.get("head_sha") or getattr(mr, "sha", "")),
            web_url=str(mr.web_url),
            draft=bool(getattr(mr, "draft", False) or getattr(mr, "work_in_progress", False)),
            description=str(getattr(mr, "description", "") or ""),
            state=state,
            merged_sha=str(merged_sha) if merged_sha else None,
        )

    def get_latest_pipeline(self, project_id: str, sha: str) -> Pipeline | None:
        pipelines = self._project(project_id).pipelines.list(sha=sha, per_page=20)
        if not pipelines:
            return None
        pipeline = pipelines[0]
        return Pipeline(str(project_id), int(pipeline.id), sha, str(pipeline.status))

    @staticmethod
    def _note_from_resource(project_id: str, mr_iid: int, note: Any) -> Note | None:
        if type(project_id) is not str or not project_id.strip():
            return None
        if type(mr_iid) is not int or mr_iid <= 0:
            return None
        native_id = getattr(note, "id", None)
        if type(native_id) is not int or native_id <= 0:
            return None
        body = getattr(note, "body", None)
        if not isinstance(body, str):
            return None
        return Note(project_id=project_id, mr_iid=mr_iid, note_id=native_id, body=body)

    @staticmethod
    def _note_author_username(note: Any) -> str | None:
        author = getattr(note, "author", {}) or {}
        if isinstance(author, dict):
            username = author.get("username")
        else:
            username = getattr(author, "username", None)
        return username if isinstance(username, str) else None

    @staticmethod
    def _list_all_notes(notes_manager: Any) -> list[Any]:
        try:
            return list(notes_manager.list(get_all=True))
        except TypeError:
            collected: list[Any] = []
            page = 1
            while True:
                notes = list(notes_manager.list(per_page=100, page=page))
                collected.extend(notes)
                if len(notes) < 100:
                    return collected
                page += 1

    def create_note(self, project_id: str, mr_iid: int, body: str) -> None:
        self._project(project_id).mergerequests.get(mr_iid).notes.create({"body": body})

    def has_note_marker(self, project_id: str, mr_iid: int, marker: str) -> bool:
        if not self._review_bot_username:
            return False
        notes_manager = self._project(project_id).mergerequests.get(mr_iid).notes
        return any(
            self._note_author_username(note) == self._review_bot_username
            and marker in str(getattr(note, "body", ""))
            for note in self._list_all_notes(notes_manager)
        )

    def find_note_by_marker(
        self, project_id: str, mr_iid: int, marker: str
    ) -> Note | None:
        if (
            not self._review_bot_username
            or type(project_id) is not str
            or not project_id.strip()
            or type(mr_iid) is not int
            or mr_iid <= 0
        ):
            return None
        notes_manager = self._project(project_id).mergerequests.get(mr_iid).notes
        matching = [
            note
            for note in self._list_all_notes(notes_manager)
            if self._note_author_username(note) == self._review_bot_username
            and has_exact_note_marker(getattr(note, "body", None), marker)
        ]
        if len(matching) != 1:
            return None
        return self._note_from_resource(project_id, mr_iid, matching[0])

    def create_branch(self, project_id: str, branch: str, ref: str) -> None:
        self._project(project_id).branches.create({"branch": branch, "ref": ref})

    def create_merge_request(
        self,
        project_id: str,
        title: str,
        source_branch: str,
        target_branch: str,
        description: str,
    ) -> CreatedMergeRequest:
        mr = self._project(project_id).mergerequests.create(
            {
                "title": title,
                "source_branch": source_branch,
                "target_branch": target_branch,
                "description": description,
            }
        )
        return CreatedMergeRequest(
            project_id=str(project_id),
            iid=int(mr.iid),
            title=str(mr.title),
            source_branch=str(mr.source_branch),
            target_branch=str(mr.target_branch),
            web_url=str(mr.web_url),
            description=str(getattr(mr, "description", "") or ""),
        )

    def find_merge_request_by_source_branch(
        self,
        project_id: str,
        source_branch: str,
        target_branch: str,
        marker: str | None = None,
    ) -> CreatedMergeRequest | None:
        mrs = self._project(project_id).mergerequests.list(
            state="all", source_branch=source_branch, target_branch=target_branch, per_page=100
        )
        for mr in mrs:
            description = str(getattr(mr, "description", "") or "")
            if marker is not None and marker not in description:
                continue
            state = str(getattr(mr, "state", "opened") or "opened")
            if state == "opened" and getattr(mr, "merged_at", None):
                state = "merged"
            merged_sha = getattr(mr, "merge_commit_sha", None) or getattr(
                mr, "merged_commit_sha", None
            )
            return CreatedMergeRequest(
                project_id=str(project_id),
                iid=int(mr.iid),
                title=str(mr.title),
                source_branch=str(mr.source_branch),
                target_branch=str(mr.target_branch),
                web_url=str(mr.web_url),
                description=description,
                state=state,
                merged_sha=str(merged_sha) if merged_sha else None,
            )
        return None
