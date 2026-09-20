import json

from review_agent_flow import notifications
from review_agent_flow.notifications import (
    FeishuDirectNotifier,
    NotificationEvent,
    WebhookNotifier,
    notifiers_from_operator,
)
from review_agent_flow.operator_config import ResolvedNotificationChannel


def test_feishu_payload_escapes_user_text_and_keeps_mentions() -> None:
    notifier = WebhookNotifier("feishu", "https://example.invalid", {"alice": "ou_alice"})
    payload = notifier._payload("<untrusted>", ("alice",))
    text = payload["content"]["text"]
    assert "&lt;untrusted&gt;" in text
    assert '<at user_id="ou_alice"></at>' in text


def test_notification_title_is_sent_to_webhook(monkeypatch) -> None:
    captured = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            return False

        def read(self, size):
            return b'{"code": 0}'

    def fake_urlopen(request, timeout):
        captured["payload"] = json.loads(request.data)
        return Response()

    monkeypatch.setattr(notifications, "urlopen", fake_urlopen)
    notifier = WebhookNotifier("feishu", "https://example.invalid")
    result = notifier.notify(NotificationEvent(title="MR title", body="review body"))
    assert result.delivered is True
    assert "MR title" in captured["payload"]["content"]["text"]


def test_notification_requires_provider_business_success(monkeypatch) -> None:
    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            return False

        def read(self, size):
            return b'{"code": 99, "msg": "invalid token"}'

    monkeypatch.setattr(notifications, "urlopen", lambda request, timeout: Response())
    result = WebhookNotifier("feishu", "https://example.invalid").notify(
        NotificationEvent(title="MR title", body="review body")
    )
    assert result.delivered is False
    assert "provider rejected" in result.detail


def test_notification_rejects_an_oversized_provider_response(monkeypatch) -> None:
    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            return False

        def read(self, size):
            return b"x" * size

    monkeypatch.setattr(notifications, "urlopen", lambda request, timeout: Response())

    result = WebhookNotifier("feishu", "https://example.invalid").notify(
        NotificationEvent(title="MR title", body="review body")
    )

    assert result.delivered is False
    assert "exceeds" in result.detail


def test_feishu_direct_message_is_bound_to_one_owner_and_open_id(monkeypatch) -> None:
    calls = []

    class Response:
        status = 200

        def __init__(self, body):
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            return False

        def read(self, size):
            return json.dumps(self.body).encode()

    def fake_urlopen(request, timeout):
        calls.append((request.full_url, dict(request.header_items()), json.loads(request.data)))
        if len(calls) == 1:
            return Response({"code": 0, "tenant_access_token": "private-token"})
        return Response({"code": 0, "data": {"message_id": "msg-1"}})

    monkeypatch.setattr(notifications, "urlopen", fake_urlopen)
    notifier = FeishuDirectNotifier("owner", "app-id", "app-secret", "ou_owner", "wufei-png")
    for recipients in ((), ("someone-else",), ("wufei-png", "someone-else")):
        assert not notifier.notify(
            NotificationEvent("Title", "Body", recipients=recipients)
        ).delivered
    assert calls == []

    result = notifier.notify(
        NotificationEvent("Title", "Body", "https://example.invalid/mr", ("wufei-png",))
    )

    assert result.delivered
    assert len(calls) == 2
    assert calls[0][0].endswith("/auth/v3/tenant_access_token/internal")
    assert calls[1][0].endswith("/im/v1/messages?receive_id_type=open_id")
    assert calls[1][2]["receive_id"] == "ou_owner"
    assert calls[1][2]["msg_type"] == "text"
    assert "Title" in json.loads(calls[1][2]["content"])["text"]
    assert not any("chat_id" in str(call) for call in calls)


def test_feishu_direct_message_requires_provider_acceptance(monkeypatch) -> None:
    class Response:
        status = 200

        def __init__(self, body):
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            return False

        def read(self, size):
            return json.dumps(self.body).encode()

    responses = iter(
        [
            Response({"code": 0, "tenant_access_token": "private-token"}),
            Response({"code": 230002, "msg": "send rejected"}),
        ]
    )
    monkeypatch.setattr(notifications, "urlopen", lambda request, timeout: next(responses))
    notifier = FeishuDirectNotifier("owner", "app-id", "app-secret", "ou_owner", "wufei-png")

    result = notifier.notify(NotificationEvent("Title", "Body", recipients=("wufei-png",)))

    assert not result.delivered
    assert "230002" in result.detail
    assert "private-token" not in result.detail


def test_resolved_feishu_direct_channel_redacts_app_secret() -> None:
    fanout = notifiers_from_operator(
        (
            ResolvedNotificationChannel(
                "owner", "feishu_dm", None, {}, "cli_app", "private-secret", "ou_owner", "wufei-png"
            ),
        )
    )

    assert fanout.channel_specs()[0][0] == "owner"
    assert fanout.channel_specs()[0][1].startswith("feishu_dm:")
    assert fanout.channel_specs() == notifiers_from_operator(
        (
            ResolvedNotificationChannel(
                "owner", "feishu_dm", None, {}, "cli_app", "rotated", "ou_owner", "wufei-png"
            ),
        )
    ).channel_specs()
    assert fanout.channel_specs() != notifiers_from_operator(
        (
            ResolvedNotificationChannel(
                "owner", "feishu_dm", None, {}, "other_app", "private-secret", "ou_owner", "wufei-png"
            ),
        )
    ).channel_specs()
    assert fanout.redaction_secrets() == ("private-secret",)
    assert isinstance(fanout.notifiers[0], FeishuDirectNotifier)
