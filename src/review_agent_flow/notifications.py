"""Notification ports and Feishu/WeCom webhook adapters."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from html import escape
from typing import Protocol
from urllib.error import URLError
from urllib.request import Request, urlopen


@dataclass(frozen=True)
class NotificationEvent:
    title: str
    body: str
    url: str | None = None
    recipients: tuple[str, ...] = ()
    correlation_id: str | None = None


@dataclass(frozen=True)
class NotificationResult:
    channel: str
    delivered: bool
    detail: str = ""


class Notifier(Protocol):
    def notify(self, event: NotificationEvent) -> NotificationResult: ...


class WebhookNotifier:
    def __init__(
        self,
        channel: str,
        webhook_url: str,
        user_ids: dict[str, str] | None = None,
        channel_id: str | None = None,
    ) -> None:
        if channel not in {"feishu", "wecom"}:
            raise ValueError(f"unsupported notification channel: {channel}")
        self.channel = channel
        self.channel_id = channel_id or channel
        self.webhook_url = webhook_url
        self.user_ids = user_ids or {}

    def notify(self, event: NotificationEvent) -> NotificationResult:
        content = f"{event.title}\n{event.body}" if event.title else event.body
        if event.url:
            content = f"{content}\n{event.url}"
        payload = self._payload(content, event.recipients)
        request = Request(
            self.webhook_url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=10) as response:
                status = int(getattr(response, "status", 200))
                raw_body = response.read()
        except (OSError, TimeoutError, URLError) as exc:  # pragma: no cover
            return NotificationResult(self.channel_id, False, str(exc))
        if status < 200 or status >= 300:
            return NotificationResult(
                self.channel_id,
                False,
                f"HTTP status {status}: {raw_body[:500].decode('utf-8', errors='replace')}",
            )
        try:
            response_body = json.loads(raw_body or b"{}")
        except (TypeError, ValueError) as exc:
            return NotificationResult(self.channel_id, False, f"invalid response JSON: {exc}")
        if not isinstance(response_body, dict):
            return NotificationResult(self.channel_id, False, "response JSON is not an object")
        success_code = response_body.get("code") if self.channel == "feishu" else response_body.get("errcode")
        if success_code != 0:
            detail = json.dumps(response_body, ensure_ascii=False, separators=(",", ":"))[:500]
            return NotificationResult(self.channel_id, False, f"provider rejected message: {detail}")
        return NotificationResult(self.channel_id, True)

    def _payload(self, content: str, recipients: tuple[str, ...]) -> dict[str, object]:
        ids = [self.user_ids[user] for user in recipients if user in self.user_ids]
        if self.channel == "feishu":
            content = escape(content)
            for user_id in ids:
                content = f'<at user_id="{user_id}"></at> {content}'
            return {"msg_type": "text", "content": {"text": content}}
        return {"msgtype": "text", "text": {"content": content, "mentioned_list": ids}}


class FanoutNotifier:
    def __init__(self, notifiers: tuple[Notifier, ...] = ()) -> None:
        channel_ids = [
            str(getattr(notifier, "channel_id", getattr(notifier, "channel", "")))
            for notifier in notifiers
        ]
        if any(not channel_id for channel_id in channel_ids):
            raise ValueError("notification channels require stable IDs")
        if len(set(channel_ids)) != len(channel_ids):
            raise ValueError("notification channel IDs must be unique")
        self.notifiers = notifiers

    def channel_specs(self) -> tuple[tuple[str, str], ...]:
        return tuple(
            (
                str(getattr(notifier, "channel_id", getattr(notifier, "channel", f"channel-{index}"))),
                str(getattr(notifier, "channel", f"channel-{index}")),
            )
            for index, notifier in enumerate(self.notifiers)
        )

    def notify_channel(self, event: NotificationEvent, channel_id: str) -> NotificationResult:
        notifier = next(
            (
                notifier
                for notifier in self.notifiers
                if str(
                    getattr(notifier, "channel_id", getattr(notifier, "channel", ""))
                )
                == channel_id
            ),
            None,
        )
        if notifier is None:
            return NotificationResult(channel_id, False, "notification channel is not configured")
        try:
            return notifier.notify(event)
        except Exception as exc:  # noqa: BLE001 - one channel must not stop fanout
            return NotificationResult(channel_id, False, str(exc))

    def notify(self, event: NotificationEvent) -> tuple[NotificationResult, ...]:
        return tuple(
            self.notify_channel(event, channel_id)
            for channel_id, _channel in self.channel_specs()
        )


def notifiers_from_policy(channels: tuple[dict[str, object], ...]) -> FanoutNotifier:
    notifiers: list[Notifier] = []
    for channel in channels:
        channel_type = channel.get("type")
        webhook_env = channel.get("webhook_env")
        if not isinstance(channel_type, str) or not isinstance(webhook_env, str):
            raise TypeError("notification channel requires type and webhook_env")
        channel_id = channel.get("id")
        if not isinstance(channel_id, str) or not channel_id:
            raise TypeError("notification channel requires a non-empty id")
        webhook_url = os.getenv(webhook_env)
        if not webhook_url:
            raise RuntimeError(f"notification webhook environment variable is missing: {webhook_env}")
        raw_user_ids = channel.get("user_ids", {})
        user_ids = raw_user_ids if isinstance(raw_user_ids, dict) else {}
        notifiers.append(WebhookNotifier(channel_type, webhook_url, user_ids, channel_id))
    if len({str(notifier.channel_id) for notifier in notifiers}) != len(notifiers):
        raise ValueError("notification channel IDs must be unique")
    return FanoutNotifier(tuple(notifiers))
