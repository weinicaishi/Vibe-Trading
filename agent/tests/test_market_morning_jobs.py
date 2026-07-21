"""Durable Market Morning job, attempt, and scheduler-lease contracts."""

from __future__ import annotations

import asyncio
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import mysql
from sqlalchemy.schema import CreateTable

REPO_ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 7, 20, 22, 0, tzinfo=timezone.utc)
JOB_ID = "11111111-1111-4111-8111-111111111111"
ATTEMPT_ID = "22222222-2222-4222-8222-222222222222"


def _mysql_sql(statement) -> str:
    return str(
        statement.compile(
            dialect=mysql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )


class _Scalars:
    def __init__(self, rows):
        self.rows = rows

    def all(self):
        return self.rows


class _Result:
    def __init__(self, *, scalar=None, rows=()):
        self.scalar = scalar
        self.rows = rows

    def scalar_one_or_none(self):
        return self.scalar

    def scalars(self):
        return _Scalars(self.rows)


class _Session:
    def __init__(self, *results):
        self.results = deque(results)
        self.statements = []
        self.added = []
        self.flush_count = 0

    async def execute(self, statement):
        self.statements.append(statement)
        if not self.results:
            return _Result()
        return self.results.popleft()

    def add(self, value):
        self.added.append(value)

    async def flush(self):
        self.flush_count += 1


def _pending_job(*, attempt_count: int = 0, max_attempts: int = 3):
    return SimpleNamespace(
        job_id=JOB_ID,
        job_type="edition_generation",
        idempotency_key="edition:2026-07-21:user-1",
        payload={"user_id": "user-1"},
        payload_sha256="a" * 64,
        status="pending",
        priority=10,
        available_at=NOW.replace(tzinfo=None),
        max_attempts=max_attempts,
        attempt_count=attempt_count,
        lease_owner=None,
        lease_expires_at=None,
        last_error_code=None,
        completed_at=None,
        created_at=NOW.replace(tzinfo=None),
        updated_at=NOW.replace(tzinfo=None),
    )


def _running_job(*, attempt_count: int = 1, max_attempts: int = 3):
    job = _pending_job(attempt_count=attempt_count, max_attempts=max_attempts)
    job.status = "running"
    job.lease_owner = "worker-a"
    job.lease_expires_at = (NOW + timedelta(minutes=2)).replace(tzinfo=None)
    return job


def _running_attempt(*, attempt_number: int = 1):
    return SimpleNamespace(
        attempt_id=ATTEMPT_ID,
        job_id=JOB_ID,
        attempt_number=attempt_number,
        worker_id="worker-a",
        status="running",
        started_at=NOW.replace(tzinfo=None),
        finished_at=None,
        error_code=None,
        details=None,
    )


def test_job_schema_supports_idempotency_attempt_history_and_scheduler_leases() -> None:
    from src.market_morning.models import (
        JobAttemptRecord,
        JobRecord,
        SchedulerLeaseRecord,
    )

    job_ddl = str(CreateTable(JobRecord.__table__).compile(dialect=mysql.dialect()))
    attempt_ddl = str(CreateTable(JobAttemptRecord.__table__).compile(dialect=mysql.dialect()))
    lease_ddl = str(CreateTable(SchedulerLeaseRecord.__table__).compile(dialect=mysql.dialect()))

    assert "uq_mm_job_idempotency_key" in job_ddl
    assert "payload_sha256 VARCHAR(64) NOT NULL" in job_ddl
    assert "lease_expires_at DATETIME(6)" in job_ddl
    assert "uq_mm_job_attempt_number" in attempt_ddl
    assert "FOREIGN KEY(job_id) REFERENCES mm_jobs" in attempt_ddl
    assert "PRIMARY KEY (lease_name)" in lease_ddl
    assert "lease_until DATETIME(6) NOT NULL" in lease_ddl
    assert "ix_mm_job_claim" in {index.name for index in JobRecord.__table__.indexes}


def test_jobs_have_a_migration_after_morning_editions() -> None:
    migration = REPO_ROOT / "agent" / "migrations" / "market_morning" / "versions" / "0006_market_morning_jobs.py"

    assert migration.exists()
    text = migration.read_text(encoding="utf-8")
    assert 'down_revision: str | None = "0005_market_morning_editions"' in text
    assert '"mm_jobs"' in text
    assert '"mm_job_attempts"' in text
    assert '"mm_scheduler_leases"' in text
    assert "skip_locked" not in text


def test_enqueue_uses_mysql_idempotency_key_as_concurrency_backstop() -> None:
    from src.market_morning.repositories.jobs import build_job_enqueue_statement

    statement = build_job_enqueue_statement(
        job_id=JOB_ID,
        job_type="edition_generation",
        idempotency_key="edition:2026-07-21:user-1",
        payload={"user_id": "user-1"},
        priority=10,
        available_at=NOW,
        max_attempts=3,
        created_at=NOW,
    )
    sql = str(statement.compile(dialect=mysql.dialect()))

    assert "INSERT INTO mm_jobs" in sql
    assert "ON DUPLICATE KEY UPDATE" in sql
    update_clause = sql.split("ON DUPLICATE KEY UPDATE", maxsplit=1)[1]
    assert "job_id = mm_jobs.job_id" in update_clause
    assert "payload" not in update_clause
    assert "status" not in update_clause


def test_claim_uses_ordered_shortlist_then_exact_skip_locked_row() -> None:
    from src.market_morning.repositories.jobs import (
        build_claimable_job_candidates_statement,
        build_claimable_job_statement,
    )

    candidates_sql = _mysql_sql(build_claimable_job_candidates_statement(now=NOW))
    lock_sql = _mysql_sql(build_claimable_job_statement(job_id=JOB_ID, now=NOW))

    assert "status IN ('pending', 'retry_scheduled')" in candidates_sql
    assert "available_at <= '2026-07-20 22:00:00'" in candidates_sql
    assert "priority DESC" in candidates_sql
    assert "available_at" in candidates_sql and "created_at" in candidates_sql
    assert "LIMIT 64" in candidates_sql
    assert "FOR UPDATE" not in candidates_sql
    assert f"job_id = '{JOB_ID}'" in lock_sql
    assert "status IN ('pending', 'retry_scheduled')" in lock_sql
    assert "available_at <= '2026-07-20 22:00:00'" in lock_sql
    assert "LIMIT 1" in lock_sql
    assert "FOR UPDATE SKIP LOCKED" in lock_sql


def test_enqueue_returns_the_durable_record_created_for_the_idempotency_key(
    monkeypatch,
) -> None:
    import src.market_morning.repositories.jobs as jobs

    monkeypatch.setattr(jobs, "new_id", lambda: JOB_ID)
    payload = {"user_id": "user-1"}
    record = _pending_job()
    record.payload_sha256 = jobs.job_payload_sha256(payload)
    session = _Session(_Result(), _Result(scalar=record))

    result = asyncio.run(
        jobs.enqueue_job(
            session,
            job_type="edition_generation",
            idempotency_key="edition:2026-07-21:user-1",
            payload=payload,
            priority=10,
            available_at=NOW,
            max_attempts=3,
            created_at=NOW,
        )
    )

    assert result.status is jobs.JobEnqueueStatus.ENQUEUED
    assert result.job_id == JOB_ID
    assert len(session.statements) == 2


def test_enqueue_rejects_idempotency_key_reuse_with_different_payload(
    monkeypatch,
) -> None:
    import src.market_morning.repositories.jobs as jobs

    monkeypatch.setattr(
        jobs,
        "new_id",
        lambda: "33333333-3333-4333-8333-333333333333",
    )
    record = _pending_job()
    record.payload_sha256 = jobs.job_payload_sha256({"user_id": "other-user"})
    session = _Session(_Result(), _Result(scalar=record))

    with pytest.raises(jobs.JobPayloadConflict, match="idempotency_key"):
        asyncio.run(
            jobs.enqueue_job(
                session,
                job_type="edition_generation",
                idempotency_key="edition:2026-07-21:user-1",
                payload={"user_id": "user-1"},
                priority=10,
                available_at=NOW,
                max_attempts=3,
                created_at=NOW,
            )
        )


def test_claim_creates_attempt_and_assigns_a_bounded_worker_lease() -> None:
    from src.market_morning.models import JobAttemptRecord
    from src.market_morning.repositories.jobs import claim_next_job

    job = _pending_job()
    session = _Session(_Result(rows=[JOB_ID]), _Result(scalar=job))

    claim = asyncio.run(
        claim_next_job(
            session,
            worker_id="worker-a",
            now=NOW,
            lease_duration=timedelta(minutes=2),
        )
    )

    assert claim is not None
    assert claim.job_id == JOB_ID
    assert claim.attempt_number == 1
    assert claim.worker_id == "worker-a"
    assert job.status == "running"
    assert job.attempt_count == 1
    assert job.lease_owner == "worker-a"
    assert job.lease_expires_at == datetime(2026, 7, 20, 22, 2)
    assert isinstance(session.added[0], JobAttemptRecord)
    assert session.added[0].status == "running"
    assert session.flush_count == 1


def test_success_closes_the_job_and_matching_attempt() -> None:
    from src.market_morning.repositories.jobs import complete_claimed_job

    job = _running_job()
    attempt = _running_attempt()
    session = _Session(_Result(scalar=job), _Result(scalar=attempt))

    asyncio.run(
        complete_claimed_job(
            session,
            job_id=JOB_ID,
            attempt_id=ATTEMPT_ID,
            worker_id="worker-a",
            completed_at=NOW + timedelta(minutes=1),
            details={"edition_id": "edition-1"},
        )
    )

    assert job.status == "succeeded"
    assert job.lease_owner is None and job.lease_expires_at is None
    assert job.completed_at == datetime(2026, 7, 20, 22, 1)
    assert attempt.status == "succeeded"
    assert attempt.details == {"edition_id": "edition-1"}
    assert session.flush_count == 1


def test_failure_schedules_retry_when_attempt_budget_remains() -> None:
    from src.market_morning.repositories.jobs import fail_claimed_job

    job = _running_job(attempt_count=1, max_attempts=3)
    attempt = _running_attempt()
    session = _Session(_Result(scalar=job), _Result(scalar=attempt))

    result = asyncio.run(
        fail_claimed_job(
            session,
            job_id=JOB_ID,
            attempt_id=ATTEMPT_ID,
            worker_id="worker-a",
            failed_at=NOW + timedelta(minutes=1),
            error_code="source_unavailable",
            retry_at=NOW + timedelta(minutes=5),
        )
    )

    assert result == "retry_scheduled"
    assert job.status == "retry_scheduled"
    assert job.available_at == datetime(2026, 7, 20, 22, 5)
    assert job.completed_at is None
    assert attempt.status == "failed"
    assert attempt.error_code == "source_unavailable"


def test_failure_is_terminal_when_attempt_budget_is_exhausted() -> None:
    from src.market_morning.repositories.jobs import fail_claimed_job

    job = _running_job(attempt_count=3, max_attempts=3)
    attempt = _running_attempt(attempt_number=3)
    session = _Session(_Result(scalar=job), _Result(scalar=attempt))

    result = asyncio.run(
        fail_claimed_job(
            session,
            job_id=JOB_ID,
            attempt_id=ATTEMPT_ID,
            worker_id="worker-a",
            failed_at=NOW + timedelta(minutes=1),
            error_code="generation_failed",
            retry_at=NOW + timedelta(minutes=5),
        )
    )

    assert result == "failed"
    assert job.status == "failed"
    assert job.completed_at == datetime(2026, 7, 20, 22, 1)
    assert job.available_at == datetime(2026, 7, 20, 22, 0)


def test_wrong_worker_cannot_complete_another_workers_claim() -> None:
    from src.market_morning.repositories.jobs import JobClaimConflict
    from src.market_morning.repositories.jobs import complete_claimed_job

    session = _Session(_Result(scalar=_running_job()))

    with pytest.raises(JobClaimConflict, match="worker"):
        asyncio.run(
            complete_claimed_job(
                session,
                job_id=JOB_ID,
                attempt_id=ATTEMPT_ID,
                worker_id="worker-b",
                completed_at=NOW,
            )
        )


def test_claim_cannot_complete_at_the_exact_lease_expiry() -> None:
    from src.market_morning.repositories.jobs import JobClaimConflict
    from src.market_morning.repositories.jobs import complete_claimed_job

    session = _Session(_Result(scalar=_running_job()))

    with pytest.raises(JobClaimConflict, match="expired"):
        asyncio.run(
            complete_claimed_job(
                session,
                job_id=JOB_ID,
                attempt_id=ATTEMPT_ID,
                worker_id="worker-a",
                completed_at=NOW + timedelta(minutes=2),
            )
        )


def test_claim_owner_can_renew_a_live_job_lease() -> None:
    from src.market_morning.repositories.jobs import renew_claimed_job_lease

    job = _running_job()
    session = _Session(_Result(scalar=job))

    lease_until = asyncio.run(
        renew_claimed_job_lease(
            session,
            job_id=JOB_ID,
            worker_id="worker-a",
            now=NOW + timedelta(minutes=1),
            lease_duration=timedelta(minutes=3),
        )
    )

    assert lease_until == datetime(2026, 7, 20, 22, 4, tzinfo=timezone.utc)
    assert job.lease_expires_at == datetime(2026, 7, 20, 22, 4)
    assert session.flush_count == 1


def test_expired_running_job_is_recovered_for_retry() -> None:
    from src.market_morning.repositories.jobs import recover_one_expired_job

    job = _running_job(attempt_count=1, max_attempts=3)
    job.lease_expires_at = (NOW - timedelta(seconds=1)).replace(tzinfo=None)
    attempt = _running_attempt()
    session = _Session(_Result(scalar=job), _Result(scalar=attempt))

    recovered = asyncio.run(
        recover_one_expired_job(
            session,
            now=NOW,
            retry_at=NOW + timedelta(minutes=1),
        )
    )

    assert recovered == JOB_ID
    assert job.status == "retry_scheduled"
    assert job.last_error_code == "job_lease_expired"
    assert attempt.status == "failed"
    assert attempt.error_code == "job_lease_expired"


def test_scheduler_lease_insert_uses_mysql_idempotency_backstop() -> None:
    from src.market_morning.repositories.jobs import build_scheduler_lease_insert_statement

    sql = str(
        build_scheduler_lease_insert_statement(
            lease_name="market-morning-daily",
            owner_id="scheduler-a",
            now=NOW,
            lease_duration=timedelta(minutes=1),
        ).compile(dialect=mysql.dialect())
    )

    assert "INSERT INTO mm_scheduler_leases" in sql
    assert "ON DUPLICATE KEY UPDATE" in sql
    update_clause = sql.split("ON DUPLICATE KEY UPDATE", maxsplit=1)[1]
    assert "owner_id" not in update_clause
    assert "lease_until" not in update_clause


def test_scheduler_lease_can_be_acquired_renewed_or_refused() -> None:
    from src.market_morning.repositories.jobs import (
        SchedulerLeaseStatus,
        acquire_scheduler_lease,
    )

    inserted = SimpleNamespace(
        lease_name="market-morning-daily",
        owner_id="scheduler-a",
        lease_until=(NOW + timedelta(minutes=1)).replace(tzinfo=None),
        heartbeat_at=NOW.replace(tzinfo=None),
        acquired_at=NOW.replace(tzinfo=None),
        updated_at=NOW.replace(tzinfo=None),
    )
    missing_session = _Session(_Result(), _Result(scalar=inserted))
    acquired = asyncio.run(
        acquire_scheduler_lease(
            missing_session,
            lease_name="market-morning-daily",
            owner_id="scheduler-a",
            now=NOW,
            lease_duration=timedelta(minutes=1),
        )
    )
    assert acquired is SchedulerLeaseStatus.ACQUIRED

    existing = SimpleNamespace(
        lease_name="market-morning-daily",
        owner_id="scheduler-a",
        lease_until=(NOW + timedelta(seconds=30)).replace(tzinfo=None),
        heartbeat_at=NOW.replace(tzinfo=None),
        acquired_at=(NOW - timedelta(minutes=5)).replace(tzinfo=None),
        updated_at=NOW.replace(tzinfo=None),
    )
    renewed = asyncio.run(
        acquire_scheduler_lease(
            _Session(_Result(), _Result(scalar=existing)),
            lease_name="market-morning-daily",
            owner_id="scheduler-a",
            now=NOW,
            lease_duration=timedelta(minutes=1),
        )
    )
    assert renewed is SchedulerLeaseStatus.RENEWED

    existing.owner_id = "scheduler-b"
    busy = asyncio.run(
        acquire_scheduler_lease(
            _Session(_Result(), _Result(scalar=existing)),
            lease_name="market-morning-daily",
            owner_id="scheduler-a",
            now=NOW,
            lease_duration=timedelta(minutes=1),
        )
    )
    assert busy is SchedulerLeaseStatus.BUSY
