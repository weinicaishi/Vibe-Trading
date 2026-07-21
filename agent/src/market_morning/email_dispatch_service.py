"""Create privacy-safe reminder attempts and durable jobs after publication."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.market_morning.db import get_session_factory
from src.market_morning.delivery_eligibility import (
    DeliveryEligibleUser,
    load_delivery_eligible_users,
)
from src.market_morning.models import GlobalEditionRunRecord
from src.market_morning.repositories.email_delivery import (
    DeliveryCreateStatus,
    create_delivery_attempt,
)
from src.market_morning.repositories.jobs import (
    JobEnqueueStatus,
    MarketMorningJobType,
    enqueue_job,
)


class EmailDispatchError(RuntimeError):
    pass


class EmailDispatchRetryable(EmailDispatchError):
    def __init__(self, error_code: str) -> None:
        self.error_code = error_code
        super().__init__(error_code)


class EmailDispatchPortUnavailable(RuntimeError):
    """Deployment-owned token signer failed; details must remain ephemeral."""


def _uuid(value: str, *, field_name: str) -> str:
    try:
        return str(UUID(value))
    except (ValueError, AttributeError) as error:
        raise ValueError(f"{field_name} must be a UUID") from error


def _aware(value: datetime, *, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value


def _token_digest(value: str) -> str:
    normalized = value.strip().lower()
    if len(normalized) != 64 or any(
        character not in "0123456789abcdef" for character in normalized
    ):
        raise ValueError("token digest must be a SHA-256 hex digest")
    return normalized


@dataclass(frozen=True, slots=True)
class EmailDispatchCommand:
    global_run_id: str
    edition_date: date
    dispatched_at: datetime
    max_attempts: int = 3
    priority: int = 10
    max_users: int = 1_000

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "global_run_id",
            _uuid(self.global_run_id, field_name="global_run_id"),
        )
        if type(self.edition_date) is not date:
            raise ValueError("edition_date must be a date")
        _aware(self.dispatched_at, field_name="dispatched_at")
        if not 1 <= self.max_attempts <= 10:
            raise ValueError("max_attempts must be between 1 and 10")
        if not -100 <= self.priority <= 100:
            raise ValueError("priority must be between -100 and 100")
        if not 1 <= self.max_users <= 1_000:
            raise ValueError("max_users must be between 1 and 1000")


@dataclass(frozen=True, slots=True)
class EmailDispatchResult:
    global_run_id: str
    edition_date: date
    dispatch_status: str
    eligible_users: int
    created_attempts: int
    existing_attempts: int
    enqueued_jobs: int
    existing_jobs: int


TokenDigestBuilder = Callable[[str, str, date], Awaitable[str]]


def build_dispatchable_global_run_statement(
    *,
    global_run_id: str,
    edition_date: date,
):
    canonical_id = _uuid(global_run_id, field_name="global_run_id")
    if type(edition_date) is not date:
        raise ValueError("edition_date must be a date")
    return select(GlobalEditionRunRecord.run_id).where(
        GlobalEditionRunRecord.run_id == canonical_id,
        GlobalEditionRunRecord.edition_date == edition_date,
        GlobalEditionRunRecord.status.in_(("complete", "partial")),
        GlobalEditionRunRecord.is_current.is_(True),
        GlobalEditionRunRecord.email_permitted.is_(True),
        GlobalEditionRunRecord.late.is_(False),
    )


async def is_dispatchable_global_run(
    session: AsyncSession,
    *,
    global_run_id: str,
    edition_date: date,
) -> bool:
    run_id = (
        await session.execute(
            build_dispatchable_global_run_statement(
                global_run_id=global_run_id,
                edition_date=edition_date,
            )
        )
    ).scalar_one_or_none()
    return run_id is not None


async def _build_digests(
    users: tuple[DeliveryEligibleUser, ...],
    *,
    command: EmailDispatchCommand,
    token_digest_builder: TokenDigestBuilder,
) -> tuple[tuple[DeliveryEligibleUser, str], ...]:
    try:
        raw_digests = await asyncio.gather(
            *(
                token_digest_builder(
                    user.user_id,
                    command.global_run_id,
                    command.edition_date,
                )
                for user in users
            )
        )
        digests = tuple(_token_digest(value) for value in raw_digests)
    except Exception:
        raise EmailDispatchRetryable(
            "delivery_token_signer_unavailable"
        ) from None
    return tuple(zip(users, digests, strict=True))


def _result(
    command: EmailDispatchCommand,
    *,
    status: str,
    eligible_users: int = 0,
    created_attempts: int = 0,
    existing_attempts: int = 0,
    enqueued_jobs: int = 0,
    existing_jobs: int = 0,
) -> EmailDispatchResult:
    return EmailDispatchResult(
        global_run_id=command.global_run_id,
        edition_date=command.edition_date,
        dispatch_status=status,
        eligible_users=eligible_users,
        created_attempts=created_attempts,
        existing_attempts=existing_attempts,
        enqueued_jobs=enqueued_jobs,
        existing_jobs=existing_jobs,
    )


async def dispatch_email_deliveries(
    command: EmailDispatchCommand,
    *,
    token_digest_builder: TokenDigestBuilder,
    session_factory: async_sessionmaker[AsyncSession] | Any | None = None,
) -> EmailDispatchResult:
    """Dispatch a bounded audience without keeping signer calls in a transaction."""

    factory = session_factory or get_session_factory()
    async with factory.begin() as session:
        if not await is_dispatchable_global_run(
            session,
            global_run_id=command.global_run_id,
            edition_date=command.edition_date,
        ):
            return _result(command, status="run_ineligible")
        users = await load_delivery_eligible_users(
            session,
            as_of=command.dispatched_at,
            limit=command.max_users,
        )
    if not users:
        return _result(command, status="no_eligible_users")

    users_and_digests = await _build_digests(
        users,
        command=command,
        token_digest_builder=token_digest_builder,
    )
    created_attempts = 0
    existing_attempts = 0
    enqueued_jobs = 0
    existing_jobs = 0
    async with factory.begin() as session:
        for user, digest in users_and_digests:
            attempt = await create_delivery_attempt(
                session,
                user_id=user.user_id,
                global_run_id=command.global_run_id,
                edition_date=command.edition_date,
                deep_link_token_sha256=digest,
                max_attempts=command.max_attempts,
                requested_at=command.dispatched_at,
            )
            if attempt.status is DeliveryCreateStatus.CREATED:
                created_attempts += 1
            else:
                existing_attempts += 1
            job = await enqueue_job(
                session,
                job_type=MarketMorningJobType.EMAIL_DELIVERY,
                idempotency_key=(
                    f"email-delivery:{attempt.delivery_attempt_id}"
                ),
                payload={
                    "schema_version": 1,
                    "delivery_attempt_id": attempt.delivery_attempt_id,
                },
                priority=command.priority,
                available_at=command.dispatched_at,
                max_attempts=command.max_attempts,
                created_at=command.dispatched_at,
            )
            if job.status is JobEnqueueStatus.ENQUEUED:
                enqueued_jobs += 1
            else:
                existing_jobs += 1
    return _result(
        command,
        status="dispatched",
        eligible_users=len(users),
        created_attempts=created_attempts,
        existing_attempts=existing_attempts,
        enqueued_jobs=enqueued_jobs,
        existing_jobs=existing_jobs,
    )


__all__ = [
    "EmailDispatchCommand",
    "EmailDispatchError",
    "EmailDispatchPortUnavailable",
    "EmailDispatchResult",
    "EmailDispatchRetryable",
    "TokenDigestBuilder",
    "build_dispatchable_global_run_statement",
    "dispatch_email_deliveries",
    "is_dispatchable_global_run",
]
