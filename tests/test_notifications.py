import json

from review_agent_flow import notifications
from review_agent_flow.notifications import NotificationEvent, WebhookNotifier


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
