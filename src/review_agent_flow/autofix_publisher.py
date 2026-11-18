"""Program-owned publication of frozen Auto-fix plans."""

from __future__ import annotations

from dataclasses import dataclass

from .gitlab import CreatedCommit, CreatedMergeRequest, GitLabClient, is_valid_sha
from .notifications import FanoutNotifier
from .operator_config import AutoFixPublicationConfig
from .policy import policy_from_json
from .publication_plan import PublicationPlanV1
from .security import redact
from .store import (
    DEFAULT_AUTO_FIX_PUBLICATION_LEASE_SECONDS,
    AutoFixPublicationPlan,
    SQLiteStore,
)

DEFAULT_AUTO_FIX_PUBLICATION_MAX_ATTEMPTS = 3


@dataclass(frozen=True)
class AutoFixPublicationResult:
    plan_id: int
    run_id: int
    status: str
    detail: str


class AutoFixPublisher:
    """Execute exactly one frozen plan without searching for remote artifacts."""

    def __init__(
        self,
        store: SQLiteStore,
        gitlab: GitLabClient,
        project_id: str,
        config: AutoFixPublicationConfig,
        notifier: FanoutNotifier | None = None,
        *,
        lease_seconds: int = DEFAULT_AUTO_FIX_PUBLICATION_LEASE_SECONDS,
        max_attempts: int = DEFAULT_AUTO_FIX_PUBLICATION_MAX_ATTEMPTS,
        retry_delay_seconds: float = 5.0,
        max_retry_delay_seconds: float = 300.0,
        secrets: tuple[str, ...] = (),
    ) -> None:
        if not project_id.strip():
            raise ValueError("AutoFixPublisher project_id cannot be empty")
        if lease_seconds <= 0 or max_attempts <= 0:
            raise ValueError("Auto-fix publication lease and attempts must be positive")
        if retry_delay_seconds < 0 or max_retry_delay_seconds < 0:
            raise ValueError("Auto-fix publication retry delays cannot be negative")
        self.store = store
        self.gitlab = gitlab
        self.project_id = project_id
        self.config = config
        self.notifier = notifier or FanoutNotifier()
        self.lease_seconds = lease_seconds
        self.max_attempts = max_attempts
        self.retry_delay_seconds = retry_delay_seconds
        self.max_retry_delay_seconds = max_retry_delay_seconds
        self.secrets = secrets

    def _redact(self, value: object) -> str:
        return redact(value, secrets=self.secrets)

    def _retry_delay(self, attempt: int) -> float:
        return min(
            self.retry_delay_seconds * (2 ** max(attempt - 1, 0)),
            self.max_retry_delay_seconds,
        )

    def _finish(
        self,
        plan: AutoFixPublicationPlan,
        state: str,
        detail: str,
        *,
        uncertain: bool = False,
    ) -> AutoFixPublicationResult:
        persisted = self.store.finish_auto_fix_publication_plan(
            plan.plan_id,
            plan.claim_token,
            state=state,
            error=detail,
            write_uncertain=uncertain,
        )
        return AutoFixPublicationResult(
            plan.plan_id,
            plan.run_id,
            state if persisted else "deferred",
            self._redact(detail),
        )

    def _retry(self, plan: AutoFixPublicationPlan, detail: str) -> AutoFixPublicationResult:
        retrying = self.store.retry_auto_fix_publication_plan(
            plan.plan_id,
            plan.claim_token,
            detail,
            delay_seconds=self._retry_delay(plan.attempts),
            max_attempts=self.max_attempts,
        )
        return AutoFixPublicationResult(
            plan.plan_id,
            plan.run_id,
            "deferred" if retrying else "blocked",
            self._redact(detail),
        )

    @staticmethod
    def _deferred(
        plan: AutoFixPublicationPlan,
        detail: str,
    ) -> AutoFixPublicationResult:
        return AutoFixPublicationResult(plan.plan_id, plan.run_id, "deferred", detail)

    @staticmethod
    def _payload(plan: AutoFixPublicationPlan) -> PublicationPlanV1:
        if plan.plan_json is None:
            raise ValueError("frozen Auto-fix Publication Plan payload was purged")
        return PublicationPlanV1.from_json(plan.plan_json, plan.plan_sha256)

    def process(
        self,
        *,
        plan_id: int | None = None,
    ) -> AutoFixPublicationResult | None:
        plan = self.store.claim_auto_fix_publication_plan(
            plan_id=plan_id,
            project_id=self.project_id,
            lease_seconds=self.lease_seconds,
        )
        if plan is None:
            return None
        if not self.config.enabled or self.project_id not in self.config.allowed_projects:
            return self._finish(
                plan,
                "blocked",
                "Operator Auto-fix publication kill switch denies the next external write",
            )
        try:
            payload = self._payload(plan)
            project_id = payload.review_key.project_id
            mr_iid = payload.review_key.mr_iid
            sha = payload.review_key.sha
            branch = payload.branch
            target_branch = payload.target_branch
            title = payload.title
            commit_message = payload.commit_message
            readiness = payload.initial_readiness
            actions = payload.actions
            if project_id != self.project_id:
                raise ValueError("frozen Auto-fix Plan belongs to another Project")
            snapshot = self.store.get_policy_snapshot(plan.run_id)
            requested_channels = (
                policy_from_json(snapshot.policy_json).notifications.channels
                if snapshot is not None
                else None
            )
            channel_specs = self.notifier.channel_specs(requested_channels)
        except (KeyError, TypeError, ValueError) as exc:
            return self._finish(plan, "blocked", str(exc))

        try:
            parent = self.gitlab.get_merge_request(project_id, mr_iid)
        except Exception as exc:  # noqa: BLE001 - no external write has started
            return self._retry(plan, f"unable to validate parent before publication: {exc}")
        if (
            parent.project_id != project_id
            or parent.iid != mr_iid
            or parent.source_project_id != project_id
            or parent.source_branch != target_branch
            or not parent.author_username.strip()
            or str(parent.state or "opened").lower() != "opened"
            or parent.sha != sha
        ):
            return self._finish(
                plan,
                "superseded",
                "parent Merge Request no longer matches the frozen ReviewKey and target",
            )

        if not self.store.extend_auto_fix_publication_lease(
            plan.plan_id, plan.claim_token, self.lease_seconds
        ):
            return self._deferred(
                plan,
                "publication lease was lost before the Auto-fix commit write",
            )
        if not self.store.mark_auto_fix_write_started(
            plan.plan_id, plan.claim_token, operation="commit"
        ):
            return self._deferred(
                plan,
                "publication lease was lost before recording the commit write boundary",
            )

        try:
            commit = self.gitlab.create_repository_commit(
                project_id,
                branch,
                sha,
                commit_message,
                actions,
                force=False,
            )
        except Exception as exc:  # noqa: BLE001 - remote commit result may be uncertain
            return self._finish(
                plan,
                "needs_reconciliation",
                f"Auto-fix branch/commit result is uncertain: {exc}",
                uncertain=True,
            )
        if not isinstance(commit, CreatedCommit) or (
            commit.project_id != project_id
            or commit.branch != branch
            or not is_valid_sha(commit.sha)
        ):
            return self._finish(
                plan,
                "needs_reconciliation",
                "GitLab returned a conflicting Auto-fix commit identity",
                uncertain=True,
            )
        if not self.store.record_auto_fix_branch_commit(plan.plan_id, plan.claim_token, commit.sha):
            return self._deferred(
                plan,
                "publication lease was lost after the Auto-fix commit write",
            )

        try:
            current_parent = self.gitlab.get_merge_request(project_id, mr_iid)
        except Exception as exc:  # noqa: BLE001 - branch already exists
            return self._finish(
                plan,
                "needs_reconciliation",
                f"parent revalidation after Auto-fix commit is uncertain: {exc}",
                uncertain=True,
            )
        if (
            current_parent.project_id != project_id
            or current_parent.iid != mr_iid
            or current_parent.source_project_id != project_id
            or current_parent.source_branch != target_branch
            or str(current_parent.state or "opened").lower() != "opened"
            or current_parent.sha != sha
        ):
            return self._finish(
                plan,
                "needs_reconciliation",
                "parent changed after the Auto-fix commit was created",
                uncertain=True,
            )

        if not self.store.extend_auto_fix_publication_lease(
            plan.plan_id, plan.claim_token, self.lease_seconds
        ):
            return self._deferred(
                plan,
                "publication lease was lost before the Auto-fix Merge Request write",
            )
        if not self.store.mark_auto_fix_write_started(
            plan.plan_id, plan.claim_token, operation="merge_request"
        ):
            return self._deferred(
                plan,
                "publication lease was lost before recording the Merge Request write boundary",
            )

        try:
            child = self.gitlab.create_merge_request(
                project_id,
                title,
                branch,
                target_branch,
                plan.marker,
                draft=readiness == "draft",
            )
        except Exception as exc:  # noqa: BLE001 - remote MR result may be uncertain
            return self._finish(
                plan,
                "needs_reconciliation",
                f"Auto-fix Merge Request result is uncertain: {exc}",
                uncertain=True,
            )
        if not isinstance(child, CreatedMergeRequest) or (
            child.project_id != project_id
            or child.iid <= 0
            or child.title != title
            or child.source_branch != branch
            or child.target_branch != target_branch
            or child.description != plan.marker
            or not child.author_username.strip()
            or str(child.state or "opened").lower() != "opened"
            or child.draft != (readiness == "draft")
            or not child.web_url
        ):
            return self._finish(
                plan,
                "needs_reconciliation",
                "GitLab returned a conflicting Auto-fix Merge Request identity",
                uncertain=True,
            )
        try:
            observed_child = self.gitlab.get_merge_request(project_id, child.iid)
        except Exception as exc:  # noqa: BLE001 - the MR write already succeeded
            return self._finish(
                plan,
                "needs_reconciliation",
                f"unable to verify the Auto-fix Merge Request head: {exc}",
                uncertain=True,
            )
        if (
            observed_child.project_id != project_id
            or observed_child.iid != child.iid
            or observed_child.source_project_id != project_id
            or observed_child.source_branch != branch
            or observed_child.target_branch != target_branch
            or observed_child.sha != commit.sha
            or plan.marker not in observed_child.description
            or not observed_child.author_username.strip()
        ):
            return self._finish(
                plan,
                "needs_reconciliation",
                "Auto-fix Merge Request head conflicts with the confirmed commit",
                uncertain=True,
            )
        if not self.store.record_auto_fix_merge_request(plan.plan_id, plan.claim_token, child.iid):
            return self._deferred(
                plan,
                "publication lease was lost after the Auto-fix Merge Request write",
            )
        completed = self.store.complete_auto_fix_publication(
            plan.plan_id,
            plan.claim_token,
            auto_fix_project_id=child.project_id,
            auto_fix_mr_iid=child.iid,
            source_branch=child.source_branch,
            target_branch=child.target_branch,
            marker=plan.marker,
            title=f"Auto-fix review: !{child.iid} {child.title}",
            body="A Program-owned Auto-fix Merge Request is ready for review.",
            url=child.web_url,
            recipients=(parent.author_username,),
            channels=channel_specs,
        )
        if completed is None:
            return self._deferred(
                plan,
                "publication lease was lost after the Auto-fix Merge Request write",
            )
        return AutoFixPublicationResult(
            plan.plan_id,
            plan.run_id,
            "published",
            f"published Auto-fix Merge Request !{child.iid}",
        )
