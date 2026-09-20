"""Notification ports and Feishu/WeCom delivery adapters."""

from __future__ import annotations

import json
from dataclasses import dataclass
from hashlib import sha256
from html import escape
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .operator_config import ResolvedNotificationChannel

MAX_PROVIDER_RESPONSE_BYTES = 64 * 1024


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
                raw_body = response.read(MAX_PROVIDER_RESPONSE_BYTES + 1)
        except (OSError, TimeoutError, URLError) as exc:  # pragma: no cover
            return NotificationResult(self.channel_id, False, str(exc))
        if status < 200 or status >= 300:
            return NotificationResult(
                self.channel_id,
                False,
                f"HTTP status {status}: {raw_body[:500].decode('utf-8', errors='replace')}",
            )
        if len(raw_body) > MAX_PROVIDER_RESPONSE_BYTES:
            return NotificationResult(
                self.channel_id,
                False,
                f"provider response exceeds {MAX_PROVIDER_RESPONSE_BYTES} bytes",
            )
        try:
            response_body = json.loads(raw_body or b"{}")
        except (TypeError, ValueError) as exc:
            return NotificationResult(self.channel_id, False, f"invalid response JSON: {exc}")
        if not isinstance(response_body, dict):
            return NotificationResult(self.channel_id, False, "response JSON is not an object")
        success_code = (
            response_body.get("code") if self.channel == "feishu" else response_body.get("errcode")
        )
        if success_code != 0:
            detail = json.dumps(response_body, ensure_ascii=False, separators=(",", ":"))[:500]
            return NotificationResult(
                self.channel_id, False, f"provider rejected message: {detail}"
            )
        return NotificationResult(self.channel_id, True)

    def _payload(self, content: str, recipients: tuple[str, ...]) -> dict[str, object]:
        ids = [self.user_ids[user] for user in recipients if user in self.user_ids]
        if self.channel == "feishu":
            content = escape(content)
            for user_id in ids:
                content = f'<at user_id="{user_id}"></at> {content}'
            return {"msg_type": "text", "content": {"text": content}}
        return {"msgtype": "text", "text": {"content": content, "mentioned_list": ids}}


class FeishuDirectNotifier:
    """Send only to one Operator-bound Feishu user, never to a chat ID."""

    channel = "feishu_dm"
    _token_url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
    _message_url = "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=open_id"

    def __init__(
        self,
        channel_id: str,
        app_id: str,
        app_secret: str,
        recipient_open_id: str,
        recipient_username: str,
    ) -> None:
        self.channel_id = channel_id
        self.app_id = app_id
        self.app_secret = app_secret
        self.recipient_open_id = recipient_open_id
        self.recipient_username = recipient_username
        binding = json.dumps(
            (app_id, recipient_open_id, recipient_username),
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        # Persist this fingerprint with queued deliveries to reject later rebinding.
        self.delivery_binding = f"feishu_dm:{sha256(binding).hexdigest()}"

    def _post(self, url: str, payload: dict[str, object], token: str | None = None) -> dict | str:
        headers = {"Content-Type": "application/json; charset=utf-8"}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        request = Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urlopen(request, timeout=10) as response:
                status = int(getattr(response, "status", 200))
                raw = response.read(MAX_PROVIDER_RESPONSE_BYTES + 1)
        except HTTPError as exc:
            return f"HTTP status {exc.code}"
        except (OSError, TimeoutError, URLError):
            return "Feishu request failed"
        if status < 200 or status >= 300:
            return f"HTTP status {status}"
        if len(raw) > MAX_PROVIDER_RESPONSE_BYTES:
            return "Feishu response exceeds size limit"
        try:
            result = json.loads(raw)
        except (TypeError, ValueError):
            return "invalid Feishu response JSON"
        return result if isinstance(result, dict) else "invalid Feishu response object"

    def notify(self, event: NotificationEvent) -> NotificationResult:
        if event.recipients != (self.recipient_username,):
            return NotificationResult(self.channel_id, False, "recipient is not the bound owner")
        token_response = self._post(
            self._token_url, {"app_id": self.app_id, "app_secret": self.app_secret}
        )
        if isinstance(token_response, str):
            return NotificationResult(self.channel_id, False, token_response)
        if token_response.get("code") != 0:
            return NotificationResult(
                self.channel_id, False, f"Feishu token rejected: {token_response.get('code')}"
            )
        token = token_response.get("tenant_access_token")
        if not isinstance(token, str) or not token:
            return NotificationResult(self.channel_id, False, "Feishu token missing")
        content = f"{event.title}\n{event.body}" if event.title else event.body
        if event.url:
            content = f"{content}\n{event.url}"
        message_response = self._post(
            self._message_url,
            {
                "receive_id": self.recipient_open_id,
                "msg_type": "text",
                "content": json.dumps({"text": content}, ensure_ascii=False),
            },
            token,
        )
        if isinstance(message_response, str):
            return NotificationResult(self.channel_id, False, message_response)
        if message_response.get("code") != 0:
            return NotificationResult(
                self.channel_id, False, f"Feishu message rejected: {message_response.get('code')}"
            )
        return NotificationResult(self.channel_id, True)


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

    def redaction_secrets(self) -> tuple[str, ...]:
        """Return runtime credentials that must not cross audit boundaries."""

        return tuple(
            secret
            for notifier in self.notifiers
            for secret in (
                getattr(notifier, "webhook_url", None),
                getattr(notifier, "app_secret", None),
            )
            if secret and isinstance(secret, str) and not secret.isspace()
        )

    def channel_specs(
        self, channel_ids: tuple[str, ...] | None = None
    ) -> tuple[tuple[str, str], ...]:
        specs = tuple(
            (
                str(
                    getattr(
                        notifier, "channel_id", getattr(notifier, "channel", f"channel-{index}")
                    )
                ),
                str(
                    getattr(
                        notifier,
                        "delivery_binding",
                        getattr(notifier, "channel", f"channel-{index}"),
                    )
                ),
            )
            for index, notifier in enumerate(self.notifiers)
        )
        if channel_ids is None:
            return specs
        requested = set(channel_ids)
        selected = tuple(spec for spec in specs if spec[0] in requested)
        if len(selected) != len(requested):
            missing = sorted(requested - {channel_id for channel_id, _ in selected})
            raise ValueError(f"notification channels are not configured: {', '.join(missing)}")
        return selected

    def notify_channel(self, event: NotificationEvent, channel_id: str) -> NotificationResult:
        notifier = next(
            (
                notifier
                for notifier in self.notifiers
                if str(getattr(notifier, "channel_id", getattr(notifier, "channel", "")))
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
            self.notify_channel(event, channel_id) for channel_id, _channel in self.channel_specs()
        )


def notifiers_from_operator(
    channels: tuple[ResolvedNotificationChannel, ...],
) -> FanoutNotifier:
    """Construct adapters only from trusted, already-resolved Operator config."""

    notifiers: list[Notifier] = []
    for channel in channels:
        if channel.channel_type == "feishu_dm":
            if not all(
                (
                    channel.app_id,
                    channel.app_secret,
                    channel.recipient_open_id,
                    channel.recipient_username,
                )
            ):
                raise ValueError("Feishu direct channel is missing owner binding")
            notifiers.append(
                FeishuDirectNotifier(
                    channel.channel_id,
                    str(channel.app_id),
                    str(channel.app_secret),
                    str(channel.recipient_open_id),
                    str(channel.recipient_username),
                )
            )
        else:
            if channel.webhook_url is None:
                raise ValueError("webhook channel is missing URL")
            notifiers.append(
                WebhookNotifier(
                    channel.channel_type,
                    channel.webhook_url,
                    dict(channel.user_ids),
                    channel.channel_id,
                )
            )
    if len({str(notifier.channel_id) for notifier in notifiers}) != len(notifiers):
        raise ValueError("notification channel IDs must be unique")
    return FanoutNotifier(tuple(notifiers))
