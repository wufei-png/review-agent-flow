import logging
from threading import Event, Thread

from review_agent_flow.notification_dispatcher import NotificationDispatcher
from review_agent_flow.notifications import (
    FanoutNotifier,
    NotificationEvent,
    NotificationResult,
    notifiers_from_operator,
)
from review_agent_flow.operator_config import ResolvedNotificationChannel
from review_agent_flow.store import SQLiteStore


class RecordingNotifier(FanoutNotifier):
    def __init__(self, failures: int = 0) -> None:
        super().__init__()
        self.failures = failures
        self.calls: list[str] = []

    def channel_specs(self) -> tuple[tuple[str, str], ...]:
        return (("reviewers", "feishu"),)

    def notify_channel(self, event: NotificationEvent, channel_id: str) -> NotificationResult:
        self.calls.append(channel_id)
        if len(self.calls) <= self.failures:
            return NotificationResult(channel_id, False, "temporary outage")
        return NotificationResult(channel_id, True)


class TwoChannelNotifier(RecordingNotifier):
    def channel_specs(self) -> tuple[tuple[str, str], ...]:
        return (("failing", "feishu"), ("working", "wecom"))

    def notify_channel(self, event: NotificationEvent, channel_id: str) -> NotificationResult:
        self.calls.append(channel_id)
        return NotificationResult(channel_id, channel_id == "working", "temporary outage")


def test_enqueue_is_async_and_dispatcher_retries(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "review.db")
    notifier = RecordingNotifier(failures=1)
    dispatcher = NotificationDispatcher(store, notifier, retry_delay_seconds=0)

    deliveries = dispatcher.enqueue(
        NotificationEvent(
            "Review requested",
            "Please review",
            "https://gitlab/merge_requests/1",
            ("alice",),
            "review-run:1:human",
        )
    )

    assert len(deliveries) == 1
    assert notifier.calls == []
    assert store.get_notification_delivery(deliveries[0].delivery_id).status == "pending"

    assert dispatcher.process() == 1
    assert store.get_notification_delivery(deliveries[0].delivery_id).status == "pending"
    assert dispatcher.process() == 1
    assert store.get_notification_delivery(deliveries[0].delivery_id).status == "delivered"
    assert notifier.calls == ["reviewers", "reviewers"]


def test_queued_direct_message_cannot_be_rebound_to_group_webhook(tmp_path, monkeypatch) -> None:
    store = SQLiteStore(tmp_path / "review.db")
    direct = notifiers_from_operator(
        (
            ResolvedNotificationChannel(
                "owner", "feishu_dm", None, {}, "cli_app", "private-secret", "ou_owner", "wufei-png"
            ),
        )
    )
    delivery = NotificationDispatcher(store, direct).enqueue(
        NotificationEvent("Review", "Private body", recipients=("wufei-png",))
    )[0]
    webhook = notifiers_from_operator(
        (ResolvedNotificationChannel("owner", "feishu", "https://hooks.example.invalid/group", {}),)
    )
    calls = []
    monkeypatch.setattr(webhook.notifiers[0], "notify", lambda event: calls.append(event))

    assert NotificationDispatcher(store, webhook, max_attempts=1).process() == 1

    assert calls == []
    recorded = store.get_notification_delivery(delivery.delivery_id)
    assert recorded.status == "failed"
    assert "channel binding changed" in recorded.last_error


def test_queued_direct_message_cannot_be_rebound_to_another_open_id(tmp_path, monkeypatch) -> None:
    store = SQLiteStore(tmp_path / "review.db")
    original = notifiers_from_operator(
        (
            ResolvedNotificationChannel(
                "owner", "feishu_dm", None, {}, "cli_app", "secret", "ou_owner", "wufei-png"
            ),
        )
    )
    delivery = NotificationDispatcher(store, original).enqueue(
        NotificationEvent("Review", "Private body", recipients=("wufei-png",))
    )[0]
    rebound = notifiers_from_operator(
        (
            ResolvedNotificationChannel(
                "owner", "feishu_dm", None, {}, "cli_app", "secret", "ou_other", "wufei-png"
            ),
        )
    )
    calls = []
    monkeypatch.setattr(rebound.notifiers[0], "notify", lambda event: calls.append(event))

    assert NotificationDispatcher(store, rebound, max_attempts=1).process() == 1

    assert calls == []
    recorded = store.get_notification_delivery(delivery.delivery_id)
    assert recorded.status == "failed"
    assert "channel binding changed" in recorded.last_error


def test_stale_completion_is_not_logged_as_delivered(tmp_path, monkeypatch, caplog) -> None:
    store = SQLiteStore(tmp_path / "review.db")
    notifier = RecordingNotifier()
    dispatcher = NotificationDispatcher(store, notifier)
    delivery = dispatcher.enqueue(NotificationEvent("Review", "Body"))[0]
    monkeypatch.setattr(store, "finish_notification", lambda *args, **kwargs: False)

    with caplog.at_level(logging.INFO):
        assert dispatcher.process() == 1

    assert store.get_notification_delivery(delivery.delivery_id).status == "running"
    assert "notification lease was lost before completion" in caplog.text
    assert '"event":"notification.delivery"' not in caplog.text


def test_channel_batch_attempts_each_configured_channel_before_retry(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "review.db")
    notifier = TwoChannelNotifier()
    dispatcher = NotificationDispatcher(store, notifier, retry_delay_seconds=0)
    dispatcher.enqueue(NotificationEvent("Review", "Body", correlation_id="review:1"))

    assert dispatcher.process_channel_batch() == 2
    assert notifier.calls == ["failing", "working"]
    assert store.get_notification_delivery(1).status == "pending"
    assert store.get_notification_delivery(2).status == "delivered"

    dispatcher.enqueue(NotificationEvent("Later review", "Body", correlation_id="review:2"))
    notifier.calls.clear()
    assert dispatcher.process_channel_batch() == 2
    assert notifier.calls == ["failing", "working"]
    assert store.get_notification_delivery(3).status == "pending"
    assert store.get_notification_delivery(4).status == "delivered"


def test_explicit_delivery_ids_skip_unavailable_records(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "review.db")
    notifier = RecordingNotifier()
    dispatcher = NotificationDispatcher(store, notifier)
    first = dispatcher.enqueue(NotificationEvent("First", "Body", correlation_id="review:1"))[0]
    second = dispatcher.enqueue(NotificationEvent("Second", "Body", correlation_id="review:2"))[0]
    assert dispatcher.process(delivery_ids=(first.delivery_id,)) == 1

    assert dispatcher.process(delivery_ids=(first.delivery_id, second.delivery_id)) == 1
    assert notifier.calls == ["reviewers", "reviewers"]
    assert store.get_notification_delivery(second.delivery_id).status == "delivered"


def test_explicit_delivery_ids_respect_limit(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "review.db")
    notifier = RecordingNotifier()
    dispatcher = NotificationDispatcher(store, notifier)
    first = dispatcher.enqueue(NotificationEvent("First", "Body", correlation_id="review:1"))[0]
    second = dispatcher.enqueue(NotificationEvent("Second", "Body", correlation_id="review:2"))[0]

    assert dispatcher.process(
        limit=1,
        delivery_ids=(first.delivery_id, second.delivery_id),
    ) == 1
    assert store.get_notification_delivery(first.delivery_id).status == "delivered"
    assert store.get_notification_delivery(second.delivery_id).status == "pending"


def test_empty_notifier_does_not_claim_existing_delivery(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "review.db")
    seeded = NotificationDispatcher(store, RecordingNotifier())
    delivery = seeded.enqueue(NotificationEvent("Review", "Body"))[0]

    dispatcher = NotificationDispatcher(store)
    assert dispatcher.process_channel_batch() == 0
    assert dispatcher.process() == 0
    assert store.get_notification_delivery(delivery.delivery_id).status == "pending"


def test_dispatch_loop_runs_independently_until_stopped(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "review.db")
    notifier = RecordingNotifier()
    dispatcher = NotificationDispatcher(store, notifier)
    delivery = dispatcher.enqueue(NotificationEvent("Review", "Body"))[0]
    stopped = Event()

    class StoppingNotifier(RecordingNotifier):
        def notify_channel(
            self, event: NotificationEvent, channel_id: str
        ) -> NotificationResult:
            result = super().notify_channel(event, channel_id)
            stopped.set()
            return result

    dispatcher.notifier = StoppingNotifier()
    thread = Thread(
        target=dispatcher.run_forever,
        kwargs={"poll_interval_seconds": 0.01, "stop_event": stopped},
    )
    thread.start()
    thread.join(timeout=1.0)

    assert not thread.is_alive()
    assert store.get_notification_delivery(delivery.delivery_id).status == "delivered"


def test_dispatch_loop_recovers_after_unexpected_batch_failure(
    tmp_path, monkeypatch, caplog
) -> None:
    store = SQLiteStore(tmp_path / "review.db")
    notifier = RecordingNotifier()
    dispatcher = NotificationDispatcher(store, notifier)
    delivery = dispatcher.enqueue(NotificationEvent("Review", "Body"))[0]
    stopped = Event()
    original = dispatcher.process_channel_batch
    attempts = 0

    def flaky_batch(stop_event=None):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("temporary dispatcher failure")
        processed = original(stop_event=stop_event)
        stopped.set()
        return processed

    monkeypatch.setattr(dispatcher, "process_channel_batch", flaky_batch)
    thread = Thread(
        target=dispatcher.run_forever,
        kwargs={"poll_interval_seconds": 0.01, "stop_event": stopped},
    )
    with caplog.at_level(logging.ERROR):
        thread.start()
        thread.join(timeout=1.0)

    assert not thread.is_alive()
    assert attempts == 2
    assert "temporary dispatcher failure" in caplog.text
    assert store.get_notification_delivery(delivery.delivery_id).status == "delivered"


def test_channel_batch_stops_before_claiming_another_delivery(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "review.db")
    stopped = Event()

    class StoppingTwoChannelNotifier(TwoChannelNotifier):
        def notify_channel(
            self, event: NotificationEvent, channel_id: str
        ) -> NotificationResult:
            result = super().notify_channel(event, channel_id)
            stopped.set()
            return result

    notifier = StoppingTwoChannelNotifier()
    dispatcher = NotificationDispatcher(store, notifier)
    dispatcher.enqueue(NotificationEvent("Review", "Body"))

    assert dispatcher.process_channel_batch(stop_event=stopped) == 1
    assert notifier.calls == ["failing"]
    assert store.get_notification_delivery(1).status == "pending"
    assert store.get_notification_delivery(2).status == "pending"


def test_custom_webhook_secret_is_redacted_from_log_and_store(
    tmp_path, monkeypatch, caplog
) -> None:
    secret = "https://hooks.example.invalid/custom-secret"
    notifier = notifiers_from_operator(
        (
            ResolvedNotificationChannel("custom", "feishu", secret, {}),
        )
    )
    store = SQLiteStore(tmp_path / "review.db")
    dispatcher = NotificationDispatcher(store, notifier, retry_delay_seconds=0)
    delivery = dispatcher.enqueue(NotificationEvent("Review", "Body"))[0]
    monkeypatch.setattr(
        notifier,
        "notify_channel",
        lambda event, channel_id: NotificationResult(channel_id, False, f"failed: {secret}"),
    )

    with caplog.at_level(logging.INFO):
        assert dispatcher.process() == 1

    recorded = store.get_notification_delivery(delivery.delivery_id)
    assert secret not in caplog.text
    assert secret not in recorded.last_error
    assert "[REDACTED]" in caplog.text
    assert "[REDACTED]" in recorded.last_error


def test_rotated_webhook_secret_is_redacted_before_store_write(
    tmp_path, monkeypatch
) -> None:
    original = "https://hooks.example.invalid/original"
    rotated = "https://hooks.example.invalid/rotated"
    notifier = notifiers_from_operator(
        (
            ResolvedNotificationChannel("custom", "feishu", original, {}),
        )
    )
    store = SQLiteStore(tmp_path / "review.db")
    dispatcher = NotificationDispatcher(store, notifier)
    delivery = dispatcher.enqueue(NotificationEvent("Review", "Body"))[0]
    notifier.notifiers[0].webhook_url = rotated
    monkeypatch.setattr(
        notifier,
        "notify_channel",
        lambda event, channel_id: NotificationResult(channel_id, False, f"failed: {rotated}"),
    )

    assert dispatcher.process() == 1
    assert rotated not in store.get_notification_delivery(delivery.delivery_id).last_error
    assert "[REDACTED]" in store.get_notification_delivery(delivery.delivery_id).last_error
