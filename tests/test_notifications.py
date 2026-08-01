from review_agent_flow.notifications import WebhookNotifier


def test_feishu_payload_escapes_user_text_and_keeps_mentions() -> None:
    notifier = WebhookNotifier("feishu", "https://example.invalid", {"alice": "ou_alice"})
    payload = notifier._payload("<untrusted>", ("alice",))
    text = payload["content"]["text"]
    assert "&lt;untrusted&gt;" in text
    assert '<at user_id="ou_alice"></at>' in text
