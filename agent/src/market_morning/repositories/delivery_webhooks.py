"""Atomic, idempotent state transitions for verified email provider events."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from string import hexdigits

from sqlalchemy import select
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.market_morning.models import (
    DeliveryAttemptRecord,
    DeliveryProviderEventRecord,
    new_id,
)
from src.market_morning.repositories.email_delivery import (
    DeliveryLifecycleStatus,
    DeliveryPersistenceConflict,
)


class DeliveryProviderEventType(StrEnum):
    DELIVERED = "delivered"
    FAILED = "failed"
    CLICKED = "clicked"


class DeliveryProviderEventOutcome(StrEnum):
    PENDING = "pending"
    APPLIED = "applied"
    STALE = "stale"
    UNMATCHED = "unmatched"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class DeliveryProviderEventResult:
    provider_event_row_id: str
    provider_event_id: str
    outcome: DeliveryProviderEventOutcome
    duplicate: bool
    delivery_attempt_id: str | None
    delivery_status: DeliveryLifecycleStatus | None


def _required(value: str, *, field_name: str, maximum: int) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > maximum:
        raise ValueError(f"{field_name} must contain 1 to {maximum} characters")
    return normalized


def _sha256(value: str) -> str:
    normalized = value.strip().lower()
    if len(normalized) != 64 or any(
        character not in hexdigits for character in normalized
    ):
        raise ValueError("payload_sha256 must be a SHA-256 hex digest")
    return normalized


def _mysql_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("received_at must be timezone-aware")
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def build_delivery_provider_event_insert_statement(
    *,
    provider_event_row_id: str,
    provider: str,
    provider_event_id: str,
    provider_message_id: str,
    event_type: DeliveryProviderEventType | str,
    payload_sha256: str,
    received_at: datetime,
):
    event_kind = DeliveryProviderEventType(event_type)
    received = _mysql_utc(received_at)
    statement = mysql_insert(DeliveryProviderEventRecord.__table__).values(
        provider_event_row_id=provider_event_row_id,
        delivery_attempt_id=None,
        provider=_required(provider, field_name="provider", maximum=64),
        provider_event_id=_required(
            provider_event_id,
            field_name="provider_event_id",
            maximum=191,
        ),
        provider_message_id=_required(
            provider_message_id,
            field_name="provider_message_id",
            maximum=191,
        ),
        event_type=event_kind.value,
        payload_sha256=_sha256(payload_sha256),
        outcome=DeliveryProviderEventOutcome.PENDING.value,
        error_code=None,
        received_at=received,
        processed_at=None,
    )
    return statement.on_duplicate_key_update(
        provider_event_row_id=(
            DeliveryProviderEventRecord.__table__.c.provider_event_row_id
        )
    )


def _event_lock_statement(*, provider: str, provider_event_id: str):
    return (
        select(DeliveryProviderEventRecord)
        .where(
            DeliveryProviderEventRecord.provider == provider,
            DeliveryProviderEventRecord.provider_event_id == provider_event_id,
        )
        .with_for_update()
    )


def _delivery_lock_statement(provider_message_id: str):
    return (
        select(DeliveryAttemptRecord)
        .where(
            DeliveryAttemptRecord.provider_message_id == provider_message_id
        )
        .with_for_update()
    )


def _result(
    event,
    *,
    duplicate: bool,
    delivery_status: DeliveryLifecycleStatus | None,
) -> DeliveryProviderEventResult:
    return DeliveryProviderEventResult(
        provider_event_row_id=event.provider_event_row_id,
        provider_event_id=event.provider_event_id,
        outcome=DeliveryProviderEventOutcome(event.outcome),
        duplicate=duplicate,
        delivery_attempt_id=event.delivery_attempt_id,
        delivery_status=delivery_status,
    )


async def record_delivery_provider_event(
    session: AsyncSession,
    *,
    provider: str,
    provider_event_id: str,
    provider_message_id: str,
    event_type: DeliveryProviderEventType | str,
    payload_sha256: str,
    received_at: datetime,
) -> DeliveryProviderEventResult:
    """Record a verified event and monotonically update its delivery attempt."""

    canonical_provider = _required(provider, field_name="provider", maximum=64)
    canonical_event_id = _required(
        provider_event_id,
        field_name="provider_event_id",
        maximum=191,
    )
    canonical_message_id = _required(
        provider_message_id,
        field_name="provider_message_id",
        maximum=191,
    )
    event_kind = DeliveryProviderEventType(event_type)
    digest = _sha256(payload_sha256)
    received = _mysql_utc(received_at)
    candidate_id = new_id()

    await session.execute(
        build_delivery_provider_event_insert_statement(
            provider_event_row_id=candidate_id,
            provider=canonical_provider,
            provider_event_id=canonical_event_id,
            provider_message_id=canonical_message_id,
            event_type=event_kind,
            payload_sha256=digest,
            received_at=received_at,
        )
    )
    event = (
        await session.execute(
            _event_lock_statement(
                provider=canonical_provider,
                provider_event_id=canonical_event_id,
            )
        )
    ).scalar_one_or_none()
    if event is None:
        raise DeliveryPersistenceConflict(
            "delivery provider event could not be reloaded"
        )
    if (
        event.provider_message_id != canonical_message_id
        or event.event_type != event_kind.value
        or event.payload_sha256 != digest
    ):
        raise DeliveryPersistenceConflict(
            "delivery provider event identity cannot be rewritten"
        )
    if event.outcome != DeliveryProviderEventOutcome.PENDING.value:
        return _result(event, duplicate=True, delivery_status=None)

    delivery = (
        await session.execute(_delivery_lock_statement(canonical_message_id))
    ).scalar_one_or_none()
    if delivery is None:
        event.outcome = DeliveryProviderEventOutcome.UNMATCHED.value
        event.error_code = "delivery_message_unmatched"
        event.processed_at = received
        await session.flush()
        return _result(event, duplicate=False, delivery_status=None)

    event.delivery_attempt_id = delivery.delivery_attempt_id
    current = DeliveryLifecycleStatus(delivery.status)
    applied = False

    if current is DeliveryLifecycleStatus.SENT:
        if event_kind is DeliveryProviderEventType.DELIVERED:
            delivery.status = DeliveryLifecycleStatus.DELIVERED.value
            delivery.delivered_at = received
            applied = True
        elif event_kind is DeliveryProviderEventType.CLICKED:
            delivery.status = DeliveryLifecycleStatus.CLICKED.value
            delivery.clicked_at = received
            applied = True
        elif event_kind is DeliveryProviderEventType.FAILED:
            delivery.status = DeliveryLifecycleStatus.FAILED.value
            delivery.failed_at = received
            delivery.last_error_code = "delivery_provider_failed"
            applied = True
    elif (
        current is DeliveryLifecycleStatus.DELIVERED
        and event_kind is DeliveryProviderEventType.CLICKED
    ):
        delivery.status = DeliveryLifecycleStatus.CLICKED.value
        delivery.clicked_at = received
        applied = True

    if applied:
        event.outcome = DeliveryProviderEventOutcome.APPLIED.value
        if event_kind is DeliveryProviderEventType.FAILED:
            event.error_code = "delivery_provider_failed"
        delivery.updated_at = received
        final_status = DeliveryLifecycleStatus(delivery.status)
    else:
        event.outcome = DeliveryProviderEventOutcome.STALE.value
        event.error_code = "delivery_event_stale"
        final_status = current
    event.processed_at = received
    await session.flush()
    return _result(
        event,
        duplicate=False,
        delivery_status=final_status,
    )


__all__ = [
    "DeliveryProviderEventOutcome",
    "DeliveryProviderEventResult",
    "DeliveryProviderEventType",
    "build_delivery_provider_event_insert_statement",
    "record_delivery_provider_event",
]
