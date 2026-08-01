from review_agent_flow.provider import messages_from_dicts


def test_provider_messages_keep_openai_compatible_shape() -> None:
    messages = messages_from_dicts([{"role": "user", "content": "Review this"}])
    assert messages[0].role == "user"
    assert messages[0].content == "Review this"
