from review_agent_flow.store import SQLiteStore


def test_enqueue_is_idempotent_and_claimable(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "review.db")
    first = store.enqueue("42", 1, "abc")
    duplicate = store.enqueue("42", 1, "abc")
    assert duplicate.run_id == first.run_id
    assert store.claim_next() == first.__class__(first.run_id, "42", 1, "abc", "normal", "running")
    assert store.claim_next() is None
