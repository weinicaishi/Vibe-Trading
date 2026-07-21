"""Verified, provider-agnostic orchestration for email delivery webhooks."""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.market_morning.db import get_session_factory
from src.market_morning.repositories.delivery_webhooks import (
    DeliveryProviderEventResult,
    DeliveryProviderEventType,
    record_delivery_provider_event,
)

MAX_WEBHOOK_BODY_BYTES = 1_000_000


class EmailWebhookRejected(RuntimeError):
    def __init__(self, error_code: str) -> None:
        self.error_code = error_code
        super().__init__(error_code)


def _required(value: str, *, field_name: str, maximum: int) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > maximum:
        raise ValueError(f"{field_name} must contain 1 to {maximum} characters")
    return normalized


@dataclass(frozen=True, slots=True)
class NormalizedDeliveryProviderEvent:
    provider: str
    provider_event_id: str
    provider_message_id: str
    event_type: DeliveryProviderEventType | str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "provider",
            _required(self.provider, field_name="provider", maximum=64),
        )
        object.__setattr__(
            self,
            "provider_event_id",
            _required(
                self.provider_event_id,
                field_name="provider_event_id",
                maximum=191,
            ),
        )
        object.__setattr__(
            self,
            "provider_message_id",
            _required(
                self.provider_message_id,
                field_name="provider_message_id",
                maximum=191,
            ),
        )
        object.__setattr__(
            self,
            "event_type",
            DeliveryProviderEventType(self.event_type),
        )


class DeliveryWebhookSignatureVerifier(Protocol):
    async def __call__(
        self,
        raw_body: bytes,
        headers: Mapping[str, str],
    ) -> bool: ...


DeliveryWebhookEventParser = Callable[
    [bytes, Mapping[str, str]], Awaitable[NormalizedDeliveryProviderEvent]
]


async def process_email_delivery_webhook(
    *,
    provider: str,
    raw_body: bytes,
    headers: Mapping[str, str],
    signature_verifier: DeliveryWebhookSignatureVerifier,
    event_parser: DeliveryWebhookEventParser,
    session_factory: async_sessionmaker[AsyncSession] | Any | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> DeliveryProviderEventResult:
    """Verify and normalize outside the transaction, then atomically persist."""

    try:
        canonical_provider = _required(
            provider,
            field_name="provider",
            maximum=64,
        )
        if not isinstance(raw_body, bytes) or not raw_body:
            raise ValueError("raw_body must contain bytes")
        if len(raw_body) > MAX_WEBHOOK_BODY_BYTES:
            raise ValueError("raw_body is too large")
    except (TypeError, ValueError) as error:
        raise EmailWebhookRejected("delivery_webhook_invalid") from error

    try:
        verified = await signature_verifier(raw_body, headers)
    except Exception as error:
        raise EmailWebhookRejected(
            "delivery_webhook_signature_invalid"
        ) from error
    if verified is not True:
        raise EmailWebhookRejected("delivery_webhook_signature_invalid")

    try:
        normalized = await event_parser(raw_body, headers)
        if not isinstance(normalized, NormalizedDeliveryProviderEvent):
            raise TypeError("parser returned an invalid event")
        if normalized.provider != canonical_provider:
            raise ValueError("provider does not match the verified endpoint")
    except Exception as error:
        raise EmailWebhookRejected("delivery_webhook_invalid") from error

    received_at = clock()
    if received_at.tzinfo is None or received_at.utcoffset() is None:
        raise ValueError("clock must return a timezone-aware datetime")
    payload_sha256 = hashlib.sha256(raw_body).hexdigest()
    factory = session_factory or get_session_factory()
    async with factory.begin() as session:
        return await record_delivery_provider_event(
            session,
            provider=normalized.provider,
            provider_event_id=normalized.provider_event_id,
            provider_message_id=normalized.provider_message_id,
            event_type=normalized.event_type,
            payload_sha256=payload_sha256,
            received_at=received_at,
        )


__all__ = [
    "DeliveryWebhookEventParser",
    "DeliveryWebhookSignatureVerifier",
    "EmailWebhookRejected",
    "MAX_WEBHOOK_BODY_BYTES",
    "NormalizedDeliveryProviderEvent",
    "process_email_delivery_webhook",
]
