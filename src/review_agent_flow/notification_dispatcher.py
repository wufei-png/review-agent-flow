"""Independent notification outbox dispatcher."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from threading import Event
from uuid import uuid4

from .notifications import FanoutNotifier, NotificationEvent, NotificationResult
from .security import redact
from .store import (
    DEFAULT_NOTIFICATION_LEASE_SECONDS,
    DEFAULT_NOTIFICATION_MAX_ATTEMPTS,
    NotificationDelivery,
    SQLiteStore,
)
from .structured_logging import log_event

LOGGER = logging.getLogger(__name__)


class NotificationDispatcher:
    """Queue and independently deliver notification outbox records."""

    def __init__(
        self,
        store: SQLiteStore,
        notifier: FanoutNotifier | None = None,
        *,
        lease_seconds: int = DEFAULT_NOTIFICATION_LEASE_SECONDS,
        max_attempts: int = DEFAULT_NOTIFICATION_MAX_ATTEMPTS,
        retry_delay_seconds: float = 5.0,
        secret_provider: Callable[[], Iterable[str]] | None = None,
    ) -> None:
        if lease_seconds <= 0:
            raise ValueError("notification lease_seconds must be positive")
        if max_attempts <= 0:
            raise ValueError("notification max_attempts must be positive")
        if retry_delay_seconds < 0:
            raise ValueError("notification retry_delay_seconds cannot be negative")
        self.store = store
        self.notifier = notifier or FanoutNotifier()
        self.lease_seconds = lease_seconds
        self.max_attempts = max_attempts
        self.retry_delay_seconds = retry_delay_seconds
        self._secret_provider = secret_provider or (lambda: ())
        register_secret = getattr(self.store, "add_redaction_secret", None)
        if callable(register_secret):
            for secret in self.notifier.redaction_secrets():
                register_secret(secret)

    def _secrets(self) -> tuple[str, ...]:
        return tuple(
            secret
            for secret in (*self._secret_provider(), *self.notifier.redaction_secrets())
            if secret and not secret.isspace()
        )

    def _redact(self, value: object) -> str:
        return redact(value, secrets=self._secrets())

    def enqueue(self, event: NotificationEvent) -> tuple[NotificationDelivery, ...]:
        """Persist one delivery per configured channel without sending HTTP."""

        channels = self.notifier.channel_specs()
        if not channels:
            return ()
        correlation_id = event.correlation_id or f"notification:{uuid4().hex}"
        try:
            deliveries = self.store.enqueue_notifications(
                correlation_id=correlation_id,
                title=event.title,
                body=event.body,
                url=event.url,
                recipients=event.recipients,
                channels=channels,
            )
            log_event(
                LOGGER,
                logging.INFO,
                secrets=self._secrets(),
                event="notification.queue",
                status="queued",
                correlation_id=correlation_id,
                channel_count=len(deliveries),
            )
            return deliveries
        except Exception as exc:  # noqa: BLE001 - notification failure must not fail the Run
            log_event(
                LOGGER,
                logging.ERROR,
                secrets=self._secrets(),
                event="notification.queue",
                status="failed",
                correlation_id=correlation_id,
                diagnostic=self._redact(exc),
            )
            return ()

    def _claim(
        self,
        delivery_id: int | None,
        excluded_delivery_ids: tuple[int, ...] = (),
        excluded_channel_ids: tuple[str, ...] = (),
    ) -> NotificationDelivery | None:
        channel_ids = tuple(channel_id for channel_id, _channel in self.notifier.channel_specs())
        if not channel_ids:
            return None
        if delivery_id is None:
            return self.store.claim_next_notification(
                lease_seconds=self.lease_seconds,
                max_attempts=self.max_attempts,
                excluded_delivery_ids=excluded_delivery_ids,
                excluded_channel_ids=excluded_channel_ids,
                allowed_channel_ids=channel_ids,
            )
        return self.store.claim_notification(
            delivery_id,
            lease_seconds=self.lease_seconds,
            max_attempts=self.max_attempts,
            allowed_channel_ids=channel_ids,
        )

    def process(
        self,
        limit: int = 1,
        delivery_ids: tuple[int, ...] | None = None,
        *,
        distinct_channels: bool = False,
        stop_event: Event | None = None,
    ) -> int:
        """Deliver claimed outbox records without changing Review Run lifecycle."""

        if limit <= 0:
            raise ValueError("notification limit must be positive")
        processed = 0
        attempted_delivery_ids: list[int] = []
        attempted_channel_ids: list[str] = []
        claim_ids = (
            tuple(dict.fromkeys(delivery_ids))
            if delivery_ids is not None
            else (None,) * limit
        )
        for delivery_id in claim_ids:
            if stop_event is not None and stop_event.is_set():
                break
            if processed >= limit:
                break
            try:
                delivery = self._claim(
                    delivery_id,
                    tuple(attempted_delivery_ids),
                    tuple(attempted_channel_ids) if distinct_channels else (),
                )
            except Exception as exc:  # noqa: BLE001 - isolate notification claim failures
                log_event(
                    LOGGER,
                    logging.ERROR,
                    secrets=self._secrets(),
                    event="notification.claim",
                    status="failed",
                    diagnostic=self._redact(exc),
                )
                break
            if delivery is None:
                if delivery_ids is None:
                    break
                continue
            attempted_delivery_ids.append(delivery.delivery_id)
            attempted_channel_ids.append(delivery.channel_id)
            event = NotificationEvent(
                title=delivery.title,
                body=delivery.body,
                url=delivery.url,
                recipients=delivery.recipients,
                correlation_id=delivery.correlation_id,
            )
            try:
                result = self.notifier.notify_channel(event, delivery.channel_id)
            except Exception as exc:  # noqa: BLE001 - isolate one channel failure
                result = NotificationResult(delivery.channel_id, False, str(exc))
            try:
                updated = self.store.finish_notification(
                    delivery.delivery_id,
                    result.delivered,
                    self._redact(result.detail),
                    claim_token=delivery.claim_token,
                    max_attempts=self.max_attempts,
                    retry_delay_seconds=self.retry_delay_seconds,
                )
                if not updated:
                    log_event(
                        LOGGER,
                        logging.WARNING,
                        secrets=self._secrets(),
                        event="notification.update",
                        status="stale",
                        correlation_id=delivery.correlation_id,
                        delivery_id=delivery.delivery_id,
                        channel=delivery.channel,
                        attempt=delivery.attempts,
                        diagnostic="notification lease was lost before completion",
                    )
                else:
                    log_event(
                        LOGGER,
                        logging.INFO if result.delivered else logging.WARNING,
                        secrets=self._secrets(),
                        event="notification.delivery",
                        status="delivered" if result.delivered else "failed",
                        correlation_id=delivery.correlation_id,
                        delivery_id=delivery.delivery_id,
                        channel=delivery.channel,
                        attempt=delivery.attempts,
                        diagnostic=result.detail or None,
                    )
            except Exception as exc:  # noqa: BLE001 - isolate notification update failures
                log_event(
                    LOGGER,
                    logging.ERROR,
                    secrets=self._secrets(),
                    event="notification.update",
                    status="failed",
                    correlation_id=delivery.correlation_id,
                    delivery_id=delivery.delivery_id,
                    diagnostic=self._redact(exc),
                )
            processed += 1
        return processed

    def process_channel_batch(self, stop_event: Event | None = None) -> int:
        """Attempt at most one distinct delivery per configured channel."""

        channel_count = len(self.notifier.channel_specs())
        if channel_count == 0:
            return 0
        return self.process(
            limit=channel_count,
            distinct_channels=True,
            stop_event=stop_event,
        )

    def run_forever(
        self,
        poll_interval_seconds: float = 1.0,
        stop_event: Event | None = None,
    ) -> None:
        """Advance notification delivery independently from Review Run execution."""

        if poll_interval_seconds <= 0:
            raise ValueError("notification poll_interval_seconds must be positive")
        stop = stop_event or Event()
        while not stop.is_set():
            try:
                processed = self.process_channel_batch(stop_event=stop)
            except Exception as exc:  # noqa: BLE001 - keep the independent loop alive
                log_event(
                    LOGGER,
                    logging.ERROR,
                    secrets=self._secrets(),
                    event="notification.dispatcher",
                    status="failed",
                    diagnostic=self._redact(exc),
                )
                processed = 0
            if processed == 0:
                stop.wait(poll_interval_seconds)
