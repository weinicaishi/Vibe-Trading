"""Transactional single-job runner for durable Market Morning work."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.market_morning.db import get_session_factory
from src.market_morning.repositories.jobs import (
    JobStatus,
    MarketMorningJobType,
    claim_next_job,
    complete_claimed_job,
    fail_claimed_job,
    recover_one_expired_job,
    renew_claimed_job_lease,
)

logger = logging.getLogger(__name__)

JobHandler = Callable[[dict[str, Any]], Awaitable[dict[str, Any] | None]]


class WorkerRunStatus(StrEnum):
    IDLE = "idle"
    SUCCEEDED = "succeeded"
    RETRY_SCHEDULED = "retry_scheduled"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class WorkerRunResult:
    status: WorkerRunStatus
    job_id: str | None = None
    attempt_number: int | None = None


@dataclass(frozen=True, slots=True)
class WorkerLoopSummary:
    iterations: int
    recovered_jobs: int
    succeeded_jobs: int
    retry_scheduled_jobs: int
    failed_jobs: int
    idle_polls: int
    infrastructure_failures: int


def _error_code(value: str) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > 64:
        raise ValueError("error_code must contain 1 to 64 characters")
    return normalized


class PermanentJobError(RuntimeError):
    """Expected handler failure that must not be retried."""

    def __init__(self, error_code: str) -> None:
        self.error_code = _error_code(error_code)
        super().__init__(self.error_code)


class RetryableJobError(RuntimeError):
    """Expected transient handler failure with an explicit retry delay."""

    def __init__(self, error_code: str, *, retry_delay: timedelta) -> None:
        if not timedelta(seconds=1) <= retry_delay <= timedelta(hours=24):
            raise ValueError("retry_delay must be between 1 second and 24 hours")
        self.error_code = _error_code(error_code)
        self.retry_delay = retry_delay
        super().__init__(self.error_code)


class WorkerLeaseLost(RuntimeError):
    """Raised when a running handler can no longer prove ownership of its job."""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _worker_status(status: JobStatus) -> WorkerRunStatus:
    if status is JobStatus.RETRY_SCHEDULED:
        return WorkerRunStatus.RETRY_SCHEDULED
    return WorkerRunStatus.FAILED


async def _wait_for_heartbeat(
    stop_event: asyncio.Event,
    timeout_seconds: float,
) -> bool:
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=timeout_seconds)
    except TimeoutError:
        return False
    return True


async def _wait_for_poll(
    stop_event: asyncio.Event,
    timeout_seconds: float,
) -> bool:
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=timeout_seconds)
    except TimeoutError:
        return False
    return True


def _heartbeat_interval(
    lease_duration: timedelta,
    heartbeat_interval: timedelta | None,
) -> timedelta:
    interval = heartbeat_interval or lease_duration / 3
    if not timedelta(seconds=1) <= interval < lease_duration:
        raise ValueError(
            "heartbeat_interval must be at least 1 second and shorter than lease_duration"
        )
    return interval


async def _maintain_job_lease(
    *,
    stop_event: asyncio.Event,
    interval: timedelta,
    session_factory: async_sessionmaker[AsyncSession] | Any,
    job_id: str,
    worker_id: str,
    lease_duration: timedelta,
    clock: Callable[[], datetime],
) -> None:
    while not await _wait_for_heartbeat(stop_event, interval.total_seconds()):
        async with session_factory.begin() as session:
            await renew_claimed_job_lease(
                session,
                job_id=job_id,
                worker_id=worker_id,
                now=clock(),
                lease_duration=lease_duration,
            )


async def _execute_with_lease_heartbeat(
    *,
    handler: JobHandler,
    payload: dict[str, Any],
    session_factory: async_sessionmaker[AsyncSession] | Any,
    job_id: str,
    worker_id: str,
    lease_duration: timedelta,
    heartbeat_interval: timedelta,
    clock: Callable[[], datetime],
) -> dict[str, Any] | None:
    stop_event = asyncio.Event()
    handler_task = asyncio.create_task(handler(payload))
    heartbeat_task = asyncio.create_task(
        _maintain_job_lease(
            stop_event=stop_event,
            interval=heartbeat_interval,
            session_factory=session_factory,
            job_id=job_id,
            worker_id=worker_id,
            lease_duration=lease_duration,
            clock=clock,
        )
    )
    try:
        done, _ = await asyncio.wait(
            {handler_task, heartbeat_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if heartbeat_task in done:
            heartbeat_error = heartbeat_task.exception()
            if heartbeat_error is not None:
                handler_task.cancel()
                await asyncio.gather(handler_task, return_exceptions=True)
                raise WorkerLeaseLost("job lease renewal failed") from heartbeat_error
        stop_event.set()
        await heartbeat_task
        return await handler_task
    finally:
        stop_event.set()
        for task in (handler_task, heartbeat_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(handler_task, heartbeat_task, return_exceptions=True)


async def run_one_job(
    *,
    worker_id: str,
    handlers: Mapping[MarketMorningJobType, JobHandler],
    session_factory: async_sessionmaker[AsyncSession] | Any | None = None,
    clock: Callable[[], datetime] = _utc_now,
    lease_duration: timedelta = timedelta(minutes=2),
    heartbeat_interval: timedelta | None = None,
) -> WorkerRunResult:
    """Claim at most one job, run it outside a transaction, then persist outcome.

    Claim and outcome use separate short transactions. A handler therefore never
    holds a database row lock while performing network or model work.
    """
    factory = session_factory or get_session_factory()
    heartbeat = _heartbeat_interval(lease_duration, heartbeat_interval)
    claimed_at = clock()
    async with factory.begin() as session:
        claim = await claim_next_job(
            session,
            worker_id=worker_id,
            now=claimed_at,
            lease_duration=lease_duration,
        )
    if claim is None:
        return WorkerRunResult(status=WorkerRunStatus.IDLE)

    handler = handlers.get(claim.job_type)
    error_code: str | None = None
    retry_delay: timedelta | None = None
    details: dict[str, Any] | None = None
    try:
        if handler is None:
            raise PermanentJobError("job_handler_missing")
        details = await _execute_with_lease_heartbeat(
            handler=handler,
            payload=dict(claim.payload),
            session_factory=factory,
            job_id=claim.job_id,
            worker_id=claim.worker_id,
            lease_duration=lease_duration,
            heartbeat_interval=heartbeat,
            clock=clock,
        )
    except RetryableJobError as error:
        error_code = error.error_code
        retry_delay = error.retry_delay
    except PermanentJobError as error:
        error_code = error.error_code
    except WorkerLeaseLost:
        raise
    except Exception as error:  # handlers are isolated; persist no raw exception text
        error_code = "unhandled_job_error"
        logger.error(
            "Market Morning job handler failed: job_id=%s job_type=%s exception_type=%s",
            claim.job_id,
            claim.job_type.value,
            type(error).__name__,
        )

    finished_at = clock()
    retry_at = finished_at + retry_delay if retry_delay is not None else None
    if error_code is None:
        async with factory.begin() as session:
            await complete_claimed_job(
                session,
                job_id=claim.job_id,
                attempt_id=claim.attempt_id,
                worker_id=claim.worker_id,
                completed_at=finished_at,
                details=details,
            )
        return WorkerRunResult(
            status=WorkerRunStatus.SUCCEEDED,
            job_id=claim.job_id,
            attempt_number=claim.attempt_number,
        )

    async with factory.begin() as session:
        job_status = await fail_claimed_job(
            session,
            job_id=claim.job_id,
            attempt_id=claim.attempt_id,
            worker_id=claim.worker_id,
            failed_at=finished_at,
            error_code=error_code,
            retry_at=retry_at,
        )
    return WorkerRunResult(
        status=_worker_status(job_status),
        job_id=claim.job_id,
        attempt_number=claim.attempt_number,
    )


async def run_worker(
    *,
    worker_id: str,
    handlers: Mapping[MarketMorningJobType, JobHandler],
    stop_event: asyncio.Event,
    session_factory: async_sessionmaker[AsyncSession] | Any | None = None,
    clock: Callable[[], datetime] = _utc_now,
    lease_duration: timedelta = timedelta(minutes=2),
    heartbeat_interval: timedelta | None = None,
    poll_interval: timedelta = timedelta(seconds=1),
    expired_retry_delay: timedelta = timedelta(minutes=1),
) -> WorkerLoopSummary:
    """Run jobs until stopped, containing infrastructure failures per iteration."""
    if not timedelta(milliseconds=50) <= poll_interval <= timedelta(minutes=1):
        raise ValueError("poll_interval must be between 50 milliseconds and 1 minute")
    if not timedelta(seconds=1) <= expired_retry_delay <= timedelta(hours=24):
        raise ValueError("expired_retry_delay must be between 1 second and 24 hours")
    heartbeat = _heartbeat_interval(lease_duration, heartbeat_interval)
    if stop_event.is_set():
        return WorkerLoopSummary(0, 0, 0, 0, 0, 0, 0)
    factory = session_factory or get_session_factory()
    iterations = 0
    recovered_jobs = 0
    succeeded_jobs = 0
    retry_scheduled_jobs = 0
    failed_jobs = 0
    idle_polls = 0
    infrastructure_failures = 0

    while not stop_event.is_set():
        iterations += 1
        wait_after_iteration = False
        try:
            now = clock()
            async with factory.begin() as session:
                recovered = await recover_one_expired_job(
                    session,
                    now=now,
                    retry_at=now + expired_retry_delay,
                )
            if recovered is not None:
                recovered_jobs += 1
            result = await run_one_job(
                worker_id=worker_id,
                handlers=handlers,
                session_factory=factory,
                clock=clock,
                lease_duration=lease_duration,
                heartbeat_interval=heartbeat,
            )
            if result.status is WorkerRunStatus.SUCCEEDED:
                succeeded_jobs += 1
            elif result.status is WorkerRunStatus.RETRY_SCHEDULED:
                retry_scheduled_jobs += 1
            elif result.status is WorkerRunStatus.FAILED:
                failed_jobs += 1
            else:
                idle_polls += 1
                wait_after_iteration = True
        except Exception as error:
            infrastructure_failures += 1
            wait_after_iteration = True
            logger.error(
                "Market Morning worker iteration failed: worker_id=%s exception_type=%s",
                worker_id,
                type(error).__name__,
            )
        if wait_after_iteration and not stop_event.is_set():
            await _wait_for_poll(stop_event, poll_interval.total_seconds())

    return WorkerLoopSummary(
        iterations=iterations,
        recovered_jobs=recovered_jobs,
        succeeded_jobs=succeeded_jobs,
        retry_scheduled_jobs=retry_scheduled_jobs,
        failed_jobs=failed_jobs,
        idle_polls=idle_polls,
        infrastructure_failures=infrastructure_failures,
    )


__all__ = [
    "JobHandler",
    "PermanentJobError",
    "RetryableJobError",
    "WorkerLeaseLost",
    "WorkerLoopSummary",
    "WorkerRunResult",
    "WorkerRunStatus",
    "run_one_job",
    "run_worker",
]
