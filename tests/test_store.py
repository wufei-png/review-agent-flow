from review_agent_flow.store import SQLiteStore


def test_enqueue_is_idempotent_and_claimable(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "review.db")
    first = store.enqueue("42", 1, "abc")
    duplicate = store.enqueue("42", 1, "abc")
    assert duplicate.run_id == first.run_id
    assert store.claim_next() == first.__class__(first.run_id, "42", 1, "abc", "normal", "running")
    assert store.claim_next() is None


def test_auto_fix_merge_suppresses_only_the_merged_parent_sha(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "review.db")
    run = store.enqueue("42", 1, "parent-sha")
    relation = store.record_auto_fix("42", 1, "parent-sha", run.run_id, "42", 9)
    assert relation.parent_mr_iid == 1
    assert store.is_suppressed_parent_sha("42", 1, "parent-sha") is False
    store.mark_auto_fix_merged("42", 9, "merged-sha")
    assert store.is_suppressed_parent_sha("42", 1, "merged-sha") is True
    assert store.is_suppressed_parent_sha("42", 1, "later-user-sha") is False
