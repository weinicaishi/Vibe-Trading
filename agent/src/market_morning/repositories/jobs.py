"""MySQL-backed durable jobs and scheduler leases for Market Morning."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.market_morning.models import (
    JobAttemptRecord,
    JobRecord,
    SchedulerLeaseRecord,
    new_id,
)


class MarketMorningJobType(StrEnum):
    ACCOUNT_DELETION = "account_deletion"
    SOURCE_INGESTION = "source_ingestion"
    MARKET_SNAPSHOT = "market_snapshot"
    GLOBAL_EDITION_RUN = "global_edition_run"
    EVENT_BRIEF_GENERATION = "event_brief_generation"
    EDITION_GENERATION = "edition_generation"
    EMAIL_DELIVERY = "email_delivery"


class JobStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    RETRY_SCHEDULED = "retry_scheduled"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class JobAttemptStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class SchedulerLeaseStatus(StrEnum):
    ACQUIRED = "acquired"
    RENEWED = "renewed"
    BUSY = "busy"


class JobEnqueueStatus(StrEnum):
    ENQUEUED = "enqueued"
    ALREADY_ENQUEUED = "already_enqueued"


class JobRepositoryError(RuntimeError):
    pass


class JobClaimConflict(JobRepositoryError):
    pass


class JobPayloadConflict(JobRepositoryError):
    pass


_CLAIM_CANDIDATE_LIMIT = 64


@dataclass(frozen=True, slots=True)
class ClaimedJob:
    job_id: str
    job_type: MarketMorningJobType
    payload: dict[str, Any]
    attempt_id: str
    attempt_number: int
    worker_id: str
    lease_expires_at: datetime


@dataclass(frozen=True, slots=True)
class EnqueuedJob:
    status: JobEnqueueStatus
    job_id: str
    job_type: MarketMorningJobType
    idempotency_key: str
    available_at: datetime


def _aware(value: datetime, *, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value


def _mysql_utc(value: datetime, *, field_name: str) -> datetime:
    return _aware(value, field_name=field_name).astimezone(timezone.utc).replace(tzinfo=None)


def _utc_aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _required(value: str, *, field_name: str, max_length: int) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > max_length:
        raise ValueError(f"{field_name} must contain 1 to {max_length} characters")
    return normalized


def _uuid(value: str, *, field_name: str) -> str:
    try:
        return str(UUID(value))
    except (ValueError, AttributeError) as error:
        raise ValueError(f"{field_name} must be a UUID") from error


def _lease_duration(value: timedelta) -> timedelta:
    if not timedelta(seconds=5) <= value <= timedelta(minutes=10):
        raise ValueError("lease_duration must be between 5 seconds and 10 minutes")
    return value


def job_payload_sha256(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(encoded) > 16_384:
        raise ValueError("job payload cannot exceed 16384 bytes")
    return hashlib.sha256(encoded).hexdigest()


def build_job_enqueue_statement(
    *,
    job_id: str,
    job_type: MarketMorningJobType | str,
    idempotency_key: str,
    payload: dict[str, Any],
    priority: int,
    available_at: datetime,
    max_attempts: int,
    created_at: datetime,
):
    canonical_job_id = _uuid(job_id, field_name="job_id")
    canonical_type = MarketMorningJobType(job_type)
    canonical_key = _required(
        idempotency_key,
        field_name="idempotency_key",
        max_length=191,
    )
    if not -100 <= priority <= 100:
        raise ValueError("priority must be between -100 and 100")
    if not 1 <= max_attempts <= 10:
        raise ValueError("max_attempts must be between 1 and 10")
    available = _mysql_utc(available_at, field_name="available_at")
    created = _mysql_utc(created_at, field_name="created_at")
    statement = mysql_insert(JobRecord.__table__).values(
        job_id=canonical_job_id,
        job_type=canonical_type.value,
        idempotency_key=canonical_key,
        payload=dict(payload),
        payload_sha256=job_payload_sha256(payload),
        status=JobStatus.PENDING.value,
        priority=priority,
        available_at=available,
        max_attempts=max_attempts,
        attempt_count=0,
        lease_owner=None,
        lease_expires_at=None,
        last_error_code=None,
        completed_at=None,
        created_at=created,
        updated_at=created,
    )
    return statement.on_duplicate_key_update(job_id=JobRecord.__table__.c.job_id)


def build_claimable_job_candidates_statement(*, now: datetime):
    """Return an ordered, non-locking shortlist for exact-row claims.

    MySQL may lock every qualifying row visited by a filesort-backed locking
    query, even when the query has ``LIMIT 1``.  Selecting only identifiers in
    this first phase lets concurrent workers share the same deterministic
    shortlist without expanding a row lock across the whole ready queue.
    """

    current = _mysql_utc(now, field_name="now")
    return (
        select(JobRecord.job_id)
        .where(
            JobRecord.status.in_((JobStatus.PENDING.value, JobStatus.RETRY_SCHEDULED.value)),
            JobRecord.available_at <= current,
        )
        .order_by(
            JobRecord.priority.desc(),
            JobRecord.available_at,
            JobRecord.created_at,
            JobRecord.job_id,
        )
        .limit(_CLAIM_CANDIDATE_LIMIT)
    )


def build_claimable_job_statement(*, job_id: str, now: datetime):
    """Lock one exact candidate while rechecking that it remains claimable."""

    canonical_job_id = _uuid(job_id, field_name="job_id")
    current = _mysql_utc(now, field_name="now")
    return (
        select(JobRecord)
        .where(
            JobRecord.job_id == canonical_job_id,
            JobRecord.status.in_((JobStatus.PENDING.value, JobStatus.RETRY_SCHEDULED.value)),
            JobRecord.available_at <= current,
        )
        .limit(1)
        .with_for_update(skip_locked=True)
    )


async def enqueue_job(
    session: AsyncSession,
    *,
    job_type: MarketMorningJobType | str,
    idempotency_key: str,
    payload: dict[str, Any],
    priority: int,
    available_at: datetime,
    max_attempts: int,
    created_at: datetime,
) -> EnqueuedJob:
    candidate_job_id = new_id()
    canonical_type = MarketMorningJobType(job_type)
    canonical_key = _required(
        idempotency_key,
        field_name="idempotency_key",
        max_length=191,
    )
    payload_hash = job_payload_sha256(payload)
    await session.execute(
        build_job_enqueue_statement(
            job_id=candidate_job_id,
            job_type=canonical_type,
            idempotency_key=canonical_key,
            payload=payload,
            priority=priority,
            available_at=available_at,
            max_attempts=max_attempts,
            created_at=created_at,
        )
    )
    record = (
        await session.execute(select(JobRecord).where(JobRecord.idempotency_key == canonical_key))
    ).scalar_one_or_none()
    if record is None:
        raise JobRepositoryError("enqueued job could not be reloaded")
    if record.job_type != canonical_type.value or record.payload_sha256 != payload_hash:
        raise JobPayloadConflict("idempotency_key already belongs to a different job payload")
    status = JobEnqueueStatus.ENQUEUED if record.job_id == candidate_job_id else JobEnqueueStatus.ALREADY_ENQUEUED
    return EnqueuedJob(
        status=status,
        job_id=record.job_id,
        job_type=canonical_type,
        idempotency_key=canonical_key,
        available_at=_utc_aware(record.available_at),
    )


async def claim_next_job(
    session: AsyncSession,
    *,
    worker_id: str,
    now: datetime,
    lease_duration: timedelta,
) -> ClaimedJob | None:
    canonical_worker = _required(worker_id, field_name="worker_id", max_length=128)
    duration = _lease_duration(lease_duration)
    current = _mysql_utc(now, field_name="now")
    candidate_ids = (
        await session.execute(build_claimable_job_candidates_statement(now=now))
    ).scalars().all()
    job = None
    for candidate_id in candidate_ids:
        job = (
            await session.execute(
                build_claimable_job_statement(job_id=candidate_id, now=now)
            )
        ).scalar_one_or_none()
        if job is not None:
            break
    if job is None:
        return None
    attempt_number = job.attempt_count + 1
    if attempt_number > job.max_attempts:
        raise JobClaimConflict("job attempt budget is already exhausted")
    attempt_id = new_id()
    lease_expires_at = current + duration
    job.status = JobStatus.RUNNING.value
    job.attempt_count = attempt_number
    job.lease_owner = canonical_worker
    job.lease_expires_at = lease_expires_at
    job.last_error_code = None
    job.updated_at = current
    session.add(
        JobAttemptRecord(
            attempt_id=attempt_id,
            job_id=job.job_id,
            attempt_number=attempt_number,
            worker_id=canonical_worker,
            status=JobAttemptStatus.RUNNING.value,
            started_at=current,
            finished_at=None,
            error_code=None,
            details=None,
        )
    )
    await session.flush()
    return ClaimedJob(
        job_id=job.job_id,
        job_type=MarketMorningJobType(job.job_type),
        payload=dict(job.payload),
        attempt_id=attempt_id,
        attempt_number=attempt_number,
        worker_id=canonical_worker,
        lease_expires_at=_utc_aware(lease_expires_at),
    )


def _locked_job_statement(job_id: str):
    return select(JobRecord).where(JobRecord.job_id == _uuid(job_id, field_name="job_id")).with_for_update()


def _locked_attempt_statement(attempt_id: str):
    return (
        select(JobAttemptRecord)
        .where(JobAttemptRecord.attempt_id == _uuid(attempt_id, field_name="attempt_id"))
        .with_for_update()
    )


def _verify_claim(job, *, worker_id: str, at: datetime) -> str:
    canonical_worker = _required(worker_id, field_name="worker_id", max_length=128)
    if job is None or job.status != JobStatus.RUNNING.value:
        raise JobClaimConflict("job is not running")
    if job.lease_owner != canonical_worker:
        raise JobClaimConflict("job belongs to another worker")
    if job.lease_expires_at is None or job.lease_expires_at <= at:
        raise JobClaimConflict("job lease has expired")
    return canonical_worker


async def complete_claimed_job(
    session: AsyncSession,
    *,
    job_id: str,
    attempt_id: str,
    worker_id: str,
    completed_at: datetime,
    details: dict[str, Any] | None = None,
) -> None:
    completed = _mysql_utc(completed_at, field_name="completed_at")
    job = (await session.execute(_locked_job_statement(job_id))).scalar_one_or_none()
    canonical_worker = _verify_claim(job, worker_id=worker_id, at=completed)
    attempt = (await session.execute(_locked_attempt_statement(attempt_id))).scalar_one_or_none()
    if (
        attempt is None
        or attempt.job_id != job.job_id
        or attempt.worker_id != canonical_worker
        or attempt.status != JobAttemptStatus.RUNNING.value
    ):
        raise JobClaimConflict("job attempt does not match the active claim")
    if details is not None:
        job_payload_sha256(details)
    job.status = JobStatus.SUCCEEDED.value
    job.lease_owner = None
    job.lease_expires_at = None
    job.last_error_code = None
    job.completed_at = completed
    job.updated_at = completed
    attempt.status = JobAttemptStatus.SUCCEEDED.value
    attempt.finished_at = completed
    attempt.error_code = None
    attempt.details = None if details is None else dict(details)
    await session.flush()


async def renew_claimed_job_lease(
    session: AsyncSession,
    *,
    job_id: str,
    worker_id: str,
    now: datetime,
    lease_duration: timedelta,
) -> datetime:
    current = _mysql_utc(now, field_name="now")
    duration = _lease_duration(lease_duration)
    job = (await session.execute(_locked_job_statement(job_id))).scalar_one_or_none()
    _verify_claim(job, worker_id=worker_id, at=current)
    lease_expires_at = current + duration
    job.lease_expires_at = lease_expires_at
    job.updated_at = current
    await session.flush()
    return _utc_aware(lease_expires_at)


async def fail_claimed_job(
    session: AsyncSession,
    *,
    job_id: str,
    attempt_id: str,
    worker_id: str,
    failed_at: datetime,
    error_code: str,
    retry_at: datetime | None,
) -> JobStatus:
    failed = _mysql_utc(failed_at, field_name="failed_at")
    canonical_error = _required(
        error_code,
        field_name="error_code",
        max_length=64,
    )
    job = (await session.execute(_locked_job_statement(job_id))).scalar_one_or_none()
    canonical_worker = _verify_claim(job, worker_id=worker_id, at=failed)
    attempt = (await session.execute(_locked_attempt_statement(attempt_id))).scalar_one_or_none()
    if (
        attempt is None
        or attempt.job_id != job.job_id
        or attempt.worker_id != canonical_worker
        or attempt.status != JobAttemptStatus.RUNNING.value
    ):
        raise JobClaimConflict("job attempt does not match the active claim")
    should_retry = job.attempt_count < job.max_attempts and retry_at is not None
    if should_retry:
        retry = _mysql_utc(retry_at, field_name="retry_at")
        if retry < failed:
            raise ValueError("retry_at cannot precede failed_at")
        next_status = JobStatus.RETRY_SCHEDULED
        job.available_at = retry
        job.completed_at = None
    else:
        next_status = JobStatus.FAILED
        job.completed_at = failed
    job.status = next_status.value
    job.lease_owner = None
    job.lease_expires_at = None
    job.last_error_code = canonical_error
    job.updated_at = failed
    attempt.status = JobAttemptStatus.FAILED.value
    attempt.finished_at = failed
    attempt.error_code = canonical_error
    await session.flush()
    return next_status


def _expired_job_statement(*, now: datetime):
    current = _mysql_utc(now, field_name="now")
    return (
        select(JobRecord)
        .where(
            JobRecord.status == JobStatus.RUNNING.value,
            JobRecord.lease_expires_at.is_not(None),
            JobRecord.lease_expires_at <= current,
        )
        .order_by(JobRecord.lease_expires_at, JobRecord.job_id)
        .limit(1)
        .with_for_update(skip_locked=True)
    )


async def recover_one_expired_job(
    session: AsyncSession,
    *,
    now: datetime,
    retry_at: datetime,
) -> str | None:
    current = _mysql_utc(now, field_name="now")
    retry = _mysql_utc(retry_at, field_name="retry_at")
    if retry < current:
        raise ValueError("retry_at cannot precede now")
    job = (await session.execute(_expired_job_statement(now=now))).scalar_one_or_none()
    if job is None:
        return None
    attempt = (
        await session.execute(
            select(JobAttemptRecord)
            .where(
                JobAttemptRecord.job_id == job.job_id,
                JobAttemptRecord.status == JobAttemptStatus.RUNNING.value,
            )
            .order_by(JobAttemptRecord.attempt_number.desc())
            .limit(1)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if attempt is None:
        raise JobClaimConflict("expired job has no running attempt")
    error_code = "job_lease_expired"
    if job.attempt_count < job.max_attempts:
        job.status = JobStatus.RETRY_SCHEDULED.value
        job.available_at = retry
        job.completed_at = None
    else:
        job.status = JobStatus.FAILED.value
        job.completed_at = current
    job.lease_owner = None
    job.lease_expires_at = None
    job.last_error_code = error_code
    job.updated_at = current
    attempt.status = JobAttemptStatus.FAILED.value
    attempt.finished_at = current
    attempt.error_code = error_code
    await session.flush()
    return job.job_id


def build_scheduler_lease_insert_statement(
    *,
    lease_name: str,
    owner_id: str,
    now: datetime,
    lease_duration: timedelta,
):
    canonical_name = _required(lease_name, field_name="lease_name", max_length=128)
    canonical_owner = _required(owner_id, field_name="owner_id", max_length=128)
    duration = _lease_duration(lease_duration)
    current = _mysql_utc(now, field_name="now")
    statement = mysql_insert(SchedulerLeaseRecord.__table__).values(
        lease_name=canonical_name,
        owner_id=canonical_owner,
        lease_until=current + duration,
        heartbeat_at=current,
        acquired_at=current,
        updated_at=current,
    )
    return statement.on_duplicate_key_update(
        lease_name=SchedulerLeaseRecord.__table__.c.lease_name
    )


async def acquire_scheduler_lease(
    session: AsyncSession,
    *,
    lease_name: str,
    owner_id: str,
    now: datetime,
    lease_duration: timedelta,
) -> SchedulerLeaseStatus:
    canonical_name = _required(
        lease_name,
        field_name="lease_name",
        max_length=128,
    )
    canonical_owner = _required(owner_id, field_name="owner_id", max_length=128)
    duration = _lease_duration(lease_duration)
    current = _mysql_utc(now, field_name="now")
    await session.execute(
        build_scheduler_lease_insert_statement(
            lease_name=canonical_name,
            owner_id=canonical_owner,
            now=now,
            lease_duration=duration,
        )
    )
    lease = (
        await session.execute(
            select(SchedulerLeaseRecord).where(SchedulerLeaseRecord.lease_name == canonical_name).with_for_update()
        )
    ).scalar_one_or_none()
    if lease is None:
        raise JobRepositoryError("scheduler lease could not be reloaded")
    lease_until = current + duration
    if lease.owner_id == canonical_owner:
        created_now = lease.acquired_at == current and lease.heartbeat_at == current
        lease.lease_until = lease_until
        lease.heartbeat_at = current
        lease.updated_at = current
        await session.flush()
        return SchedulerLeaseStatus.ACQUIRED if created_now else SchedulerLeaseStatus.RENEWED
    if lease.lease_until <= current:
        lease.owner_id = canonical_owner
        lease.lease_until = lease_until
        lease.heartbeat_at = current
        lease.acquired_at = current
        lease.updated_at = current
        await session.flush()
        return SchedulerLeaseStatus.ACQUIRED
    return SchedulerLeaseStatus.BUSY


__all__ = [
    "ClaimedJob",
    "EnqueuedJob",
    "JobAttemptStatus",
    "JobClaimConflict",
    "JobEnqueueStatus",
    "JobPayloadConflict",
    "JobRepositoryError",
    "JobStatus",
    "MarketMorningJobType",
    "SchedulerLeaseStatus",
    "acquire_scheduler_lease",
    "build_claimable_job_candidates_statement",
    "build_claimable_job_statement",
    "build_job_enqueue_statement",
    "build_scheduler_lease_insert_statement",
    "claim_next_job",
    "complete_claimed_job",
    "enqueue_job",
    "fail_claimed_job",
    "job_payload_sha256",
    "recover_one_expired_job",
    "renew_claimed_job_lease",
]
