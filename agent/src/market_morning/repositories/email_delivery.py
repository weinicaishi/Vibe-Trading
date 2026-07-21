"""MySQL state transitions for privacy-safe idempotent email reminders."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from enum import StrEnum
from string import hexdigits
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.market_morning.models import DeliveryAttemptRecord, new_id


class DeliveryPersistenceConflict(RuntimeError):
    pass


class DeliveryCreateStatus(StrEnum):
    CREATED = "created"
    ALREADY_CREATED = "already_created"


class DeliveryLifecycleStatus(StrEnum):
    PENDING = "pending"
    SENDING = "sending"
    SENT = "sent"
    DELIVERED = "delivered"
    FAILED = "failed"
    CLICKED = "clicked"
    SUPPRESSED = "suppressed"


@dataclass(frozen=True, slots=True)
class DeliveryCreateResult:
    status: DeliveryCreateStatus
    delivery_attempt_id: str
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class DeliveryStartResult:
    delivery_attempt_id: str
    status: DeliveryLifecycleStatus
    attempt_number: int
    retry_permitted: bool
    can_execute: bool


@dataclass(frozen=True, slots=True)
class DeliveryCompletionResult:
    delivery_attempt_id: str
    status: DeliveryLifecycleStatus
    provider_message_id: str


@dataclass(frozen=True, slots=True)
class DeliveryFailureResult:
    delivery_attempt_id: str
    attempt_number: int
    retry_permitted: bool
    error_code: str


@dataclass(frozen=True, slots=True)
class DeliverySuppressionResult:
    delivery_attempt_id: str
    status: DeliveryLifecycleStatus
    reason_code: str


def _uuid(value: str, *, field_name: str) -> str:
    try:
        return str(UUID(value))
    except (ValueError, AttributeError) as error:
        raise ValueError(f"{field_name} must be a UUID") from error


def _edition_date(value: date) -> date:
    if type(value) is not date:
        raise ValueError("edition_date must be a date")
    return value


def _mysql_utc(value: datetime, *, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _sha256(value: str, *, field_name: str) -> str:
    normalized = value.strip().lower()
    if len(normalized) != 64 or any(character not in hexdigits for character in normalized):
        raise ValueError(f"{field_name} must be a SHA-256 hex digest")
    return normalized


def _required(value: str, *, field_name: str, maximum: int) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > maximum:
        raise ValueError(f"{field_name} must contain 1 to {maximum} characters")
    return normalized


def delivery_idempotency_key(
    *,
    user_id: str,
    edition_date: date,
    global_run_id: str,
) -> str:
    canonical_user = _uuid(user_id, field_name="user_id")
    canonical_run = _uuid(global_run_id, field_name="global_run_id")
    canonical_date = _edition_date(edition_date)
    return (
        f"email:{canonical_date.isoformat()}:{canonical_user}:"
        f"global-run:{canonical_run}"
    )


def build_delivery_attempt_insert_statement(
    *,
    delivery_attempt_id: str,
    user_id: str,
    global_run_id: str,
    edition_date: date,
    idempotency_key: str,
    deep_link_token_sha256: str,
    max_attempts: int,
    requested_at: datetime,
):
    canonical_attempt = _uuid(
        delivery_attempt_id, field_name="delivery_attempt_id"
    )
    canonical_user = _uuid(user_id, field_name="user_id")
    canonical_run = _uuid(global_run_id, field_name="global_run_id")
    canonical_date = _edition_date(edition_date)
    expected_key = delivery_idempotency_key(
        user_id=canonical_user,
        edition_date=canonical_date,
        global_run_id=canonical_run,
    )
    if idempotency_key != expected_key:
        raise ValueError("idempotency_key does not match delivery identity")
    if not 1 <= max_attempts <= 10:
        raise ValueError("max_attempts must be between 1 and 10")
    requested = _mysql_utc(requested_at, field_name="requested_at")
    statement = mysql_insert(DeliveryAttemptRecord.__table__).values(
        delivery_attempt_id=canonical_attempt,
        user_id=canonical_user,
        global_run_id=canonical_run,
        edition_date=canonical_date,
        channel="email",
        idempotency_key=expected_key,
        status=DeliveryLifecycleStatus.PENDING.value,
        provider_message_id=None,
        deep_link_token_sha256=_sha256(
            deep_link_token_sha256,
            field_name="deep_link_token_sha256",
        ),
        attempt_count=0,
        max_attempts=max_attempts,
        last_error_code=None,
        requested_at=requested,
        sent_at=None,
        delivered_at=None,
        clicked_at=None,
        failed_at=None,
        updated_at=requested,
    )
    return statement.on_duplicate_key_update(
        delivery_attempt_id=(
            DeliveryAttemptRecord.__table__.c.delivery_attempt_id
        )
    )


def _identity_statement(*, user_id: str, edition_date: date):
    return select(DeliveryAttemptRecord).where(
        DeliveryAttemptRecord.user_id == user_id,
        DeliveryAttemptRecord.edition_date == edition_date,
        DeliveryAttemptRecord.channel == "email",
    )


def _lock_statement(delivery_attempt_id: str):
    return (
        select(DeliveryAttemptRecord)
        .where(
            DeliveryAttemptRecord.delivery_attempt_id == delivery_attempt_id
        )
        .with_for_update()
    )


async def create_delivery_attempt(
    session: AsyncSession,
    *,
    user_id: str,
    global_run_id: str,
    edition_date: date,
    deep_link_token_sha256: str,
    max_attempts: int,
    requested_at: datetime,
) -> DeliveryCreateResult:
    canonical_user = _uuid(user_id, field_name="user_id")
    canonical_run = _uuid(global_run_id, field_name="global_run_id")
    canonical_date = _edition_date(edition_date)
    key = delivery_idempotency_key(
        user_id=canonical_user,
        edition_date=canonical_date,
        global_run_id=canonical_run,
    )
    candidate_id = new_id()
    digest = _sha256(
        deep_link_token_sha256,
        field_name="deep_link_token_sha256",
    )
    await session.execute(
        build_delivery_attempt_insert_statement(
            delivery_attempt_id=candidate_id,
            user_id=canonical_user,
            global_run_id=canonical_run,
            edition_date=canonical_date,
            idempotency_key=key,
            deep_link_token_sha256=digest,
            max_attempts=max_attempts,
            requested_at=requested_at,
        )
    )
    record = (
        await session.execute(
            _identity_statement(
                user_id=canonical_user,
                edition_date=canonical_date,
            )
        )
    ).scalar_one_or_none()
    if record is None:
        raise DeliveryPersistenceConflict(
            "delivery attempt could not be reloaded"
        )
    if (
        record.global_run_id != canonical_run
        or record.idempotency_key != key
        or record.deep_link_token_sha256 != digest
        or record.max_attempts != max_attempts
    ):
        raise DeliveryPersistenceConflict(
            "daily email delivery already has a different immutable identity"
        )
    await session.flush()
    return DeliveryCreateResult(
        status=(
            DeliveryCreateStatus.CREATED
            if record.delivery_attempt_id == candidate_id
            else DeliveryCreateStatus.ALREADY_CREATED
        ),
        delivery_attempt_id=record.delivery_attempt_id,
        idempotency_key=key,
    )


async def start_delivery_attempt(
    session: AsyncSession,
    *,
    delivery_attempt_id: str,
    started_at: datetime,
) -> DeliveryStartResult:
    canonical_id = _uuid(
        delivery_attempt_id, field_name="delivery_attempt_id"
    )
    started = _mysql_utc(started_at, field_name="started_at")
    record = (
        await session.execute(_lock_statement(canonical_id))
    ).scalar_one_or_none()
    if record is None:
        raise DeliveryPersistenceConflict("delivery attempt does not exist")
    status = DeliveryLifecycleStatus(record.status)
    if status in {
        DeliveryLifecycleStatus.SENT,
        DeliveryLifecycleStatus.DELIVERED,
        DeliveryLifecycleStatus.CLICKED,
        DeliveryLifecycleStatus.SUPPRESSED,
    } or record.attempt_count >= record.max_attempts:
        return DeliveryStartResult(
            delivery_attempt_id=canonical_id,
            status=status,
            attempt_number=record.attempt_count,
            retry_permitted=False,
            can_execute=False,
        )
    if status not in {
        DeliveryLifecycleStatus.PENDING,
        DeliveryLifecycleStatus.FAILED,
        DeliveryLifecycleStatus.SENDING,
    }:
        raise DeliveryPersistenceConflict("delivery attempt has invalid status")
    record.status = DeliveryLifecycleStatus.SENDING.value
    record.attempt_count += 1
    record.updated_at = started
    await session.flush()
    return DeliveryStartResult(
        delivery_attempt_id=canonical_id,
        status=DeliveryLifecycleStatus.SENDING,
        attempt_number=record.attempt_count,
        retry_permitted=record.attempt_count < record.max_attempts,
        can_execute=True,
    )


async def complete_delivery_attempt(
    session: AsyncSession,
    *,
    delivery_attempt_id: str,
    provider_message_id: str,
    sent_at: datetime,
) -> DeliveryCompletionResult:
    canonical_id = _uuid(
        delivery_attempt_id, field_name="delivery_attempt_id"
    )
    provider_id = _required(
        provider_message_id,
        field_name="provider_message_id",
        maximum=191,
    )
    sent = _mysql_utc(sent_at, field_name="sent_at")
    record = (
        await session.execute(_lock_statement(canonical_id))
    ).scalar_one_or_none()
    if record is None:
        raise DeliveryPersistenceConflict("delivery attempt does not exist")
    if record.status in {
        DeliveryLifecycleStatus.SENT.value,
        DeliveryLifecycleStatus.DELIVERED.value,
        DeliveryLifecycleStatus.CLICKED.value,
    }:
        if record.provider_message_id != provider_id:
            raise DeliveryPersistenceConflict(
                "sent delivery provider identity cannot be rewritten"
            )
        return DeliveryCompletionResult(
            delivery_attempt_id=canonical_id,
            status=DeliveryLifecycleStatus(record.status),
            provider_message_id=provider_id,
        )
    if record.status != DeliveryLifecycleStatus.SENDING.value:
        raise DeliveryPersistenceConflict("delivery attempt is not sending")
    record.status = DeliveryLifecycleStatus.SENT.value
    record.provider_message_id = provider_id
    record.sent_at = sent
    record.updated_at = sent
    await session.flush()
    return DeliveryCompletionResult(
        delivery_attempt_id=canonical_id,
        status=DeliveryLifecycleStatus.SENT,
        provider_message_id=provider_id,
    )


async def fail_delivery_attempt(
    session: AsyncSession,
    *,
    delivery_attempt_id: str,
    error_code: str,
    failed_at: datetime,
) -> DeliveryFailureResult:
    canonical_id = _uuid(
        delivery_attempt_id, field_name="delivery_attempt_id"
    )
    failure_code = _required(error_code, field_name="error_code", maximum=64)
    failed = _mysql_utc(failed_at, field_name="failed_at")
    record = (
        await session.execute(_lock_statement(canonical_id))
    ).scalar_one_or_none()
    if record is None:
        raise DeliveryPersistenceConflict("delivery attempt does not exist")
    if record.status != DeliveryLifecycleStatus.SENDING.value:
        raise DeliveryPersistenceConflict("delivery attempt is not sending")
    record.status = DeliveryLifecycleStatus.FAILED.value
    record.last_error_code = failure_code
    record.failed_at = failed
    record.updated_at = failed
    await session.flush()
    return DeliveryFailureResult(
        delivery_attempt_id=canonical_id,
        attempt_number=record.attempt_count,
        retry_permitted=record.attempt_count < record.max_attempts,
        error_code=failure_code,
    )


async def suppress_delivery_attempt(
    session: AsyncSession,
    *,
    delivery_attempt_id: str,
    reason_code: str,
    suppressed_at: datetime,
) -> DeliverySuppressionResult:
    canonical_id = _uuid(
        delivery_attempt_id, field_name="delivery_attempt_id"
    )
    reason = _required(reason_code, field_name="reason_code", maximum=64)
    suppressed = _mysql_utc(suppressed_at, field_name="suppressed_at")
    record = (
        await session.execute(_lock_statement(canonical_id))
    ).scalar_one_or_none()
    if record is None:
        raise DeliveryPersistenceConflict("delivery attempt does not exist")
    if record.status == DeliveryLifecycleStatus.SUPPRESSED.value:
        if record.last_error_code != reason:
            raise DeliveryPersistenceConflict(
                "suppressed delivery reason cannot be rewritten"
            )
        return DeliverySuppressionResult(
            delivery_attempt_id=canonical_id,
            status=DeliveryLifecycleStatus.SUPPRESSED,
            reason_code=reason,
        )
    if record.status not in {
        DeliveryLifecycleStatus.PENDING.value,
        DeliveryLifecycleStatus.FAILED.value,
        DeliveryLifecycleStatus.SENDING.value,
    }:
        raise DeliveryPersistenceConflict(
            "sent delivery attempt cannot be suppressed"
        )
    record.status = DeliveryLifecycleStatus.SUPPRESSED.value
    record.last_error_code = reason
    record.updated_at = suppressed
    await session.flush()
    return DeliverySuppressionResult(
        delivery_attempt_id=canonical_id,
        status=DeliveryLifecycleStatus.SUPPRESSED,
        reason_code=reason,
    )


__all__ = [
    "DeliveryCompletionResult",
    "DeliveryCreateResult",
    "DeliveryCreateStatus",
    "DeliveryFailureResult",
    "DeliveryLifecycleStatus",
    "DeliveryPersistenceConflict",
    "DeliveryStartResult",
    "DeliverySuppressionResult",
    "build_delivery_attempt_insert_statement",
    "complete_delivery_attempt",
    "create_delivery_attempt",
    "delivery_idempotency_key",
    "fail_delivery_attempt",
    "start_delivery_attempt",
    "suppress_delivery_attempt",
]
