"""python-gitlab implementation kept behind the core GitLab protocol."""

from __future__ import annotations

import os
from pathlib import PurePosixPath
from typing import Any

from .gitlab import (
    CreatedCommit,
    CreatedMergeRequest,
    GitLabClient,
    GitLabIdentityConflict,
    MergeRequest,
    Note,
    NoteMarkerLookup,
    Pipeline,
    RepositoryAction,
    RepositoryCommitObservation,
    RepositoryFile,
    has_exact_note_marker,
    is_valid_sha,
    normalize_sha,
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
            source_project_id=str(getattr(mr, "source_project_id", "") or ""),
        )

    def get_latest_pipeline(self, project_id: str, sha: str) -> Pipeline | None:
        pipelines = self._project(project_id).pipelines.list(sha=sha, per_page=20)
        if not pipelines:
            return None
        pipeline = pipelines[0]
        return Pipeline(str(project_id), int(pipeline.id), sha, str(pipeline.status))

    def get_repository_file(
        self, project_id: str, file_path: str, branch: str
    ) -> RepositoryFile:
        project = self._project(project_id)
        branch_resource = project.branches.get(branch)
        commit = getattr(branch_resource, "commit", {}) or {}
        raw_sha = commit.get("id") if isinstance(commit, dict) else getattr(commit, "id", None)
        ref_sha = normalize_sha(raw_sha)
        try:
            resource = project.files.get(file_path=file_path, ref=ref_sha)
        except Exception as exc:
            status = getattr(exc, "response_code", None) or getattr(exc, "status_code", None)
            if status == 404:
                return RepositoryFile(str(project_id), file_path, ref_sha, None)
            raise
        content = resource.decode()
        if isinstance(content, bytes):
            try:
                decoded = content.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ValueError("Project Policy must be UTF-8 text") from exc
        elif isinstance(content, str):
            decoded = content
        else:
            raise TypeError("GitLab returned an invalid repository file payload")
        return RepositoryFile(str(project_id), file_path, ref_sha, decoded)

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

    def create_note(self, project_id: str, mr_iid: int, body: str) -> Note:
        if not self._review_bot_username:
            raise RuntimeError("GITLAB_REVIEW_BOT_USERNAME is required for Note publication")
        resource = self._project(project_id).mergerequests.get(mr_iid).notes.create({"body": body})
        if self._note_author_username(resource) != self._review_bot_username:
            raise GitLabIdentityConflict(
                "created Note author does not match GITLAB_REVIEW_BOT_USERNAME"
            )
        note = self._note_from_resource(project_id, mr_iid, resource)
        if note is None:
            raise RuntimeError("GitLab returned an invalid created Note identity")
        return note

    def lookup_note_marker(
        self, project_id: str, mr_iid: int, marker: str
    ) -> NoteMarkerLookup:
        if not self._review_bot_username:
            raise RuntimeError("GITLAB_REVIEW_BOT_USERNAME is required for Note publication")
        notes_manager = self._project(project_id).mergerequests.get(mr_iid).notes
        exact: list[Note] = []
        conflicts: list[Note] = []
        for resource in self._list_all_notes(notes_manager):
            if self._note_author_username(resource) != self._review_bot_username:
                continue
            body = getattr(resource, "body", None)
            if not isinstance(body, str) or marker not in body:
                continue
            note = self._note_from_resource(project_id, mr_iid, resource)
            if note is None:
                raise RuntimeError("GitLab returned an invalid trusted Note identity")
            if has_exact_note_marker(body, marker):
                exact.append(note)
            else:
                conflicts.append(note)
        return NoteMarkerLookup(tuple(exact), tuple(conflicts))

    def get_trusted_note(
        self, project_id: str, mr_iid: int, note_id: int
    ) -> Note | None:
        if not self._review_bot_username:
            raise RuntimeError("GITLAB_REVIEW_BOT_USERNAME is required for Note reconciliation")
        notes_manager = self._project(project_id).mergerequests.get(mr_iid).notes
        try:
            resource = notes_manager.get(note_id)
        except Exception as exc:
            status = getattr(exc, "response_code", None) or getattr(exc, "status_code", None)
            if status == 404:
                return None
            raise
        if self._note_author_username(resource) != self._review_bot_username:
            raise GitLabIdentityConflict(
                "persisted Note author does not match GITLAB_REVIEW_BOT_USERNAME"
            )
        note = self._note_from_resource(project_id, mr_iid, resource)
        if note is None:
            raise GitLabIdentityConflict("GitLab returned an invalid persisted Note identity")
        return note

    def has_note_marker(self, project_id: str, mr_iid: int, marker: str) -> bool:
        try:
            lookup = self.lookup_note_marker(project_id, mr_iid, marker)
        except RuntimeError:
            return False
        return bool(lookup.exact)

    def find_note_by_marker(
        self, project_id: str, mr_iid: int, marker: str
    ) -> Note | None:
        if (
            type(project_id) is not str
            or not project_id.strip()
            or type(mr_iid) is not int
            or mr_iid <= 0
        ):
            return None
        try:
            lookup = self.lookup_note_marker(project_id, mr_iid, marker)
        except RuntimeError:
            return None
        if lookup.conflicts or len(lookup.exact) != 1:
            return None
        return lookup.exact[0]

    def create_branch(self, project_id: str, branch: str, ref: str) -> None:
        self._project(project_id).branches.create({"branch": branch, "ref": ref})

    def create_repository_commit(
        self,
        project_id: str,
        branch: str,
        start_sha: str,
        commit_message: str,
        actions: tuple[dict[str, object], ...],
        *,
        force: bool = False,
    ) -> CreatedCommit:
        if not self._review_bot_username:
            raise RuntimeError(
                "GITLAB_REVIEW_BOT_USERNAME is required for Auto-fix publication"
            )
        commit = self._project(project_id).commits.create(
            {
                "branch": branch,
                "start_sha": start_sha,
                "commit_message": commit_message,
                "actions": list(actions),
                "force": force,
            }
        )
        commit_sha = str(getattr(commit, "id", "") or "")
        if not is_valid_sha(commit_sha):
            raise GitLabIdentityConflict("GitLab returned an invalid Auto-fix commit identity")
        return CreatedCommit(str(project_id), commit_sha, branch)

    def create_merge_request(
        self,
        project_id: str,
        title: str,
        source_branch: str,
        target_branch: str,
        description: str,
        *,
        draft: bool = False,
    ) -> CreatedMergeRequest:
        if not self._review_bot_username:
            raise RuntimeError(
                "GITLAB_REVIEW_BOT_USERNAME is required for Auto-fix publication"
            )
        published_title = title
        if draft and not title.startswith(("Draft:", "[Draft]", "(Draft)")):
            published_title = f"Draft: {title}"
        mr = self._project(project_id).mergerequests.create(
            {
                "title": published_title,
                "source_branch": source_branch,
                "target_branch": target_branch,
                "description": description,
            }
        )
        author = getattr(mr, "author", {}) or {}
        author_username = str(
            author.get("username", "")
            if isinstance(author, dict)
            else getattr(author, "username", "")
        )
        if (
            self._review_bot_username
            and author_username != self._review_bot_username
        ):
            raise GitLabIdentityConflict(
                "created Auto-fix Merge Request author does not match the Review Bot"
            )
        return CreatedMergeRequest(
            project_id=str(project_id),
            iid=int(mr.iid),
            title=str(mr.title),
            source_branch=str(mr.source_branch),
            target_branch=str(mr.target_branch),
            web_url=str(mr.web_url),
            description=str(getattr(mr, "description", "") or ""),
            author_username=author_username,
            draft=bool(
                getattr(mr, "draft", False)
                or getattr(mr, "work_in_progress", False)
            ),
        )

    def find_merge_request_by_source_branch(
        self,
        project_id: str,
        source_branch: str,
        target_branch: str,
        marker: str | None = None,
    ) -> CreatedMergeRequest | None:
        manager = self._project(project_id).mergerequests
        filters = {
            "state": "all",
            "source_branch": source_branch,
            "target_branch": target_branch,
        }
        try:
            mrs = list(manager.list(get_all=True, per_page=100, **filters))
        except TypeError:  # pragma: no cover - compatibility with pagination-only adapters
            mrs = []
            page = 1
            while True:
                page_values = list(manager.list(per_page=100, page=page, **filters))
                mrs.extend(page_values)
                if len(page_values) < 100:
                    break
                page += 1
        matches: list[CreatedMergeRequest] = []
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
            author = getattr(mr, "author", {}) or {}
            author_username = str(
                author.get("username", "")
                if isinstance(author, dict)
                else getattr(author, "username", "")
            )
            if self._review_bot_username and author_username != self._review_bot_username:
                continue
            matches.append(
                CreatedMergeRequest(
                    project_id=str(project_id),
                    iid=int(mr.iid),
                    title=str(mr.title),
                    source_branch=str(mr.source_branch),
                    target_branch=str(mr.target_branch),
                    web_url=str(mr.web_url),
                    description=description,
                    state=state,
                    merged_sha=str(merged_sha) if merged_sha else None,
                    author_username=author_username,
                    draft=bool(
                        getattr(mr, "draft", False)
                        or getattr(mr, "work_in_progress", False)
                    ),
                )
            )
        if len(matches) > 1:
            raise GitLabIdentityConflict(
                "multiple Auto-fix Merge Requests match one frozen Publication Plan"
            )
        return matches[0] if matches else None

    @staticmethod
    def _resource_value(resource: Any, name: str, default: Any = None) -> Any:
        if isinstance(resource, dict):
            return resource.get(name, default)
        return getattr(resource, name, default)

    @staticmethod
    def _canonical_repository_path(value: object) -> str:
        if not isinstance(value, str):
            raise GitLabIdentityConflict("GitLab returned a non-text repository path")
        path = PurePosixPath(value)
        if (
            not value
            or str(path) != value
            or path.is_absolute()
            or "\\" in value
            or any(part in {"", ".", ".."} for part in path.parts)
        ):
            raise GitLabIdentityConflict("GitLab returned a non-canonical repository path")
        return value

    def observe_repository_commit(
        self, project_id: str, commit_sha: str
    ) -> RepositoryCommitObservation:
        """Reconstruct a bounded semantic action list for explicit recovery."""

        if not is_valid_sha(commit_sha):
            raise ValueError("invalid repository commit SHA")
        project = self._project(project_id)
        commit = project.commits.get(commit_sha)
        observed_sha = str(getattr(commit, "id", "") or "")
        if observed_sha.lower() != commit_sha.lower():
            raise GitLabIdentityConflict("GitLab returned a conflicting repository commit")
        raw_parents = getattr(commit, "parent_ids", None)
        if not isinstance(raw_parents, (list, tuple)) or not all(
            is_valid_sha(parent) for parent in raw_parents
        ):
            raise GitLabIdentityConflict("GitLab returned invalid commit parents")
        diffs = list(commit.diff(get_all=True))
        if len(diffs) > 100:
            raise GitLabIdentityConflict("repository commit exceeds the recovery action bound")

        actions: list[RepositoryAction] = []
        total_content_bytes = 0
        for diff in diffs:
            if bool(self._resource_value(diff, "collapsed", False)) or bool(
                self._resource_value(diff, "too_large", False)
            ):
                raise GitLabIdentityConflict("GitLab omitted part of the repository commit diff")
            old_path = self._canonical_repository_path(
                self._resource_value(diff, "old_path")
            )
            new_path = self._canonical_repository_path(
                self._resource_value(diff, "new_path")
            )
            if old_path != new_path or bool(self._resource_value(diff, "renamed_file", False)):
                raise GitLabIdentityConflict("recovery rejects renamed repository paths")
            old_mode = str(self._resource_value(diff, "a_mode", "") or "")
            new_mode = str(self._resource_value(diff, "b_mode", "") or "")
            if old_mode not in {"0", "000000", "100644", "100755"} or new_mode not in {
                "0",
                "000000",
                "100644",
                "100755",
            }:
                raise GitLabIdentityConflict("recovery rejects non-regular repository entries")
            created = bool(self._resource_value(diff, "new_file", False))
            deleted = bool(self._resource_value(diff, "deleted_file", False))
            if created and deleted:
                raise GitLabIdentityConflict("GitLab returned contradictory repository diff flags")
            if deleted:
                actions.append(RepositoryAction("delete", old_path))
                continue

            file_resource = project.files.get(file_path=new_path, ref=commit_sha)
            try:
                raw_content = file_resource.decode()
            except Exception as exc:
                raise GitLabIdentityConflict("GitLab returned unreadable repository content") from exc
            if not isinstance(raw_content, bytes) or b"\0" in raw_content:
                raise GitLabIdentityConflict("recovery rejects binary repository content")
            if len(raw_content) > 1024 * 1024:
                raise GitLabIdentityConflict("repository file exceeds the recovery size bound")
            try:
                content = raw_content.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise GitLabIdentityConflict("recovery rejects non-UTF-8 repository content") from exc
            executable = new_mode == "100755"
            if created:
                total_content_bytes += len(raw_content)
                actions.append(RepositoryAction("create", new_path, content, executable))
            elif old_mode != new_mode and not str(
                self._resource_value(diff, "diff", "") or ""
            ):
                actions.append(
                    RepositoryAction("chmod", new_path, execute_filemode=executable)
                )
            else:
                total_content_bytes += len(raw_content)
                actions.append(RepositoryAction("update", new_path, content, executable))
            if total_content_bytes > 5 * 1024 * 1024:
                raise GitLabIdentityConflict("repository commit exceeds the recovery size bound")
        actions.sort(key=lambda action: action.file_path)
        return RepositoryCommitObservation(
            str(project_id), observed_sha.lower(), tuple(str(parent).lower() for parent in raw_parents), tuple(actions)
        )
