"""python-gitlab implementation kept behind the core GitLab protocol."""

from __future__ import annotations

import os
from typing import Any

from .gitlab import CreatedMergeRequest, GitLabClient, MergeRequest, Pipeline


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
        return MergeRequest(
            project_id=str(project_id),
            iid=mr_iid,
            title=str(mr.title),
            author_username=str(author.get("username") or ""),
            source_branch=str(mr.source_branch),
            target_branch=str(mr.target_branch),
            sha=str(diff_refs.get("head_sha") or mr.sha),
            web_url=str(mr.web_url),
            draft=bool(getattr(mr, "draft", False) or getattr(mr, "work_in_progress", False)),
            description=str(getattr(mr, "description", "") or ""),
        )

    def get_latest_pipeline(self, project_id: str, sha: str) -> Pipeline | None:
        pipelines = self._project(project_id).pipelines.list(sha=sha, per_page=20)
        if not pipelines:
            return None
        pipeline = pipelines[0]
        return Pipeline(str(project_id), int(pipeline.id), sha, str(pipeline.status))

    def create_note(self, project_id: str, mr_iid: int, body: str) -> None:
        self._project(project_id).mergerequests.get(mr_iid).notes.create({"body": body})

    def has_note_marker(self, project_id: str, mr_iid: int, marker: str) -> bool:
        if not self._review_bot_username:
            return False
        notes_manager = self._project(project_id).mergerequests.get(mr_iid).notes
        page = 1
        while True:
            notes = notes_manager.list(per_page=100, page=page)
            for note in notes:
                author = getattr(note, "author", {}) or {}
                if isinstance(author, dict):
                    username = author.get("username")
                else:
                    username = getattr(author, "username", None)
                if username != self._review_bot_username:
                    continue
                if marker in str(getattr(note, "body", "")):
                    return True
            if len(notes) < 100:
                return False
            page += 1

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
            state="all", source_branch=source_branch, target_branch=target_branch
        )
        for mr in mrs:
            description = str(getattr(mr, "description", "") or "")
            if marker is not None and marker not in description:
                continue
            return CreatedMergeRequest(
                project_id=str(project_id),
                iid=int(mr.iid),
                title=str(mr.title),
                source_branch=str(mr.source_branch),
                target_branch=str(mr.target_branch),
                web_url=str(mr.web_url),
                description=description,
            )
        return None
