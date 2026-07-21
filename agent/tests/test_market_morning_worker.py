"""One-job Market Morning worker orchestration contracts."""

from __future__ import annotations

import asyncio
from collections import deque
from datetime import datetime, timedelta, timezone

import pytest

NOW = datetime(2026, 7, 20, 22, 0, tzinfo=timezone.utc)
JOB_ID = "11111111-1111-4111-8111-111111111111"
ATTEMPT_ID = "22222222-2222-4222-8222-222222222222"


class _Transaction:
    def __init__(self, session, entries):
        self.session = session
        self.entries = entries

    async def __aenter__(self):
        self.entries.append("enter")
        return self.session

    async def __aexit__(self, exc_type, exc, traceback):
        self.entries.append("rollback" if exc_type else "commit")


class _SessionFactory:
    def __init__(self, *sessions):
        self.sessions = deque(sessions)
        self.entries: list[str] = []

    def begin(self):
        return _Transaction(self.sessions.popleft(), self.entries)


def _claim():
    from src.market_morning.repositories.jobs import ClaimedJob, MarketMorningJobType

    return ClaimedJob(
        job_id=JOB_ID,
        job_type=MarketMorningJobType.EDITION_GENERATION,
        payload={"user_id": "user-1"},
        attempt_id=ATTEMPT_ID,
        attempt_number=1,
        worker_id="worker-a",
        lease_expires_at=NOW + timedelta(minutes=2),
    )


def test_worker_returns_idle_without_opening_an_execution_transaction(monkeypatch) -> None:
    import src.market_morning.jobs.worker as worker

    factory = _SessionFactory(object())

    async def claim_next_job(session, **kwargs):
        return None

    monkeypatch.setattr(worker, "claim_next_job", claim_next_job)

    result = asyncio.run(
        worker.run_one_job(
            worker_id="worker-a",
            handlers={},
            session_factory=factory,
            clock=lambda: NOW,
        )
    )

    assert result.status is worker.WorkerRunStatus.IDLE
    assert result.job_id is None
    assert factory.entries == ["enter", "commit"]


def test_worker_commits_claim_then_runs_handler_then_completes(monkeypatch) -> None:
    import src.market_morning.jobs.worker as worker

    claim = _claim()
    factory = _SessionFactory(object(), object())
    lifecycle: list[tuple] = []

    async def claim_next_job(session, **kwargs):
        lifecycle.append(("claim", kwargs))
        return claim

    async def complete_claimed_job(session, **kwargs):
        lifecycle.append(("complete", kwargs))

    async def handler(payload):
        lifecycle.append(("handler", payload))
        return {"edition_id": "edition-1"}

    monkeypatch.setattr(worker, "claim_next_job", claim_next_job)
    monkeypatch.setattr(worker, "complete_claimed_job", complete_claimed_job)

    result = asyncio.run(
        worker.run_one_job(
            worker_id="worker-a",
            handlers={claim.job_type: handler},
            session_factory=factory,
            clock=lambda: NOW,
        )
    )

    assert result.status is worker.WorkerRunStatus.SUCCEEDED
    assert result.job_id == JOB_ID
    assert result.attempt_number == 1
    assert lifecycle[0][0] == "claim"
    assert lifecycle[1] == ("handler", {"user_id": "user-1"})
    assert lifecycle[2][0] == "complete"
    assert lifecycle[2][1]["details"] == {"edition_id": "edition-1"}
    assert factory.entries == ["enter", "commit", "enter", "commit"]


def test_worker_schedules_retry_for_retryable_handler_error(monkeypatch) -> None:
    import src.market_morning.jobs.worker as worker
    from src.market_morning.repositories.jobs import JobStatus

    claim = _claim()
    factory = _SessionFactory(object(), object())
    failed: list[dict] = []

    async def claim_next_job(session, **kwargs):
        return claim

    async def fail_claimed_job(session, **kwargs):
        failed.append(kwargs)
        return JobStatus.RETRY_SCHEDULED

    async def handler(payload):
        raise worker.RetryableJobError(
            "source_temporarily_unavailable",
            retry_delay=timedelta(minutes=5),
        )

    monkeypatch.setattr(worker, "claim_next_job", claim_next_job)
    monkeypatch.setattr(worker, "fail_claimed_job", fail_claimed_job)
    times = iter((NOW, NOW + timedelta(seconds=30)))

    result = asyncio.run(
        worker.run_one_job(
            worker_id="worker-a",
            handlers={claim.job_type: handler},
            session_factory=factory,
            clock=lambda: next(times),
        )
    )

    assert result.status is worker.WorkerRunStatus.RETRY_SCHEDULED
    assert failed[0]["error_code"] == "source_temporarily_unavailable"
    assert failed[0]["failed_at"] == NOW + timedelta(seconds=30)
    assert failed[0]["retry_at"] == NOW + timedelta(minutes=5, seconds=30)


@pytest.mark.parametrize(
    ("handler_factory", "expected_error"),
    [
        (
            lambda worker: worker.PermanentJobError("invalid_edition_payload"),
            "invalid_edition_payload",
        ),
        (lambda worker: RuntimeError("secret provider response"), "unhandled_job_error"),
    ],
)
def test_worker_marks_permanent_or_unexpected_errors_terminal(
    monkeypatch,
    handler_factory,
    expected_error,
) -> None:
    import src.market_morning.jobs.worker as worker
    from src.market_morning.repositories.jobs import JobStatus

    claim = _claim()
    factory = _SessionFactory(object(), object())
    failed: list[dict] = []

    async def claim_next_job(session, **kwargs):
        return claim

    async def fail_claimed_job(session, **kwargs):
        failed.append(kwargs)
        return JobStatus.FAILED

    async def handler(payload):
        raise handler_factory(worker)

    monkeypatch.setattr(worker, "claim_next_job", claim_next_job)
    monkeypatch.setattr(worker, "fail_claimed_job", fail_claimed_job)

    result = asyncio.run(
        worker.run_one_job(
            worker_id="worker-a",
            handlers={claim.job_type: handler},
            session_factory=factory,
            clock=lambda: NOW,
        )
    )

    assert result.status is worker.WorkerRunStatus.FAILED
    assert failed[0]["error_code"] == expected_error
    assert failed[0]["retry_at"] is None
    assert "secret provider response" not in repr(failed[0])


def test_worker_treats_missing_handler_as_a_terminal_configuration_error(monkeypatch) -> None:
    import src.market_morning.jobs.worker as worker
    from src.market_morning.repositories.jobs import JobStatus

    claim = _claim()
    factory = _SessionFactory(object(), object())
    failed: list[dict] = []

    async def claim_next_job(session, **kwargs):
        return claim

    async def fail_claimed_job(session, **kwargs):
        failed.append(kwargs)
        return JobStatus.FAILED

    monkeypatch.setattr(worker, "claim_next_job", claim_next_job)
    monkeypatch.setattr(worker, "fail_claimed_job", fail_claimed_job)

    result = asyncio.run(
        worker.run_one_job(
            worker_id="worker-a",
            handlers={},
            session_factory=factory,
            clock=lambda: NOW,
        )
    )

    assert result.status is worker.WorkerRunStatus.FAILED
    assert failed[0]["error_code"] == "job_handler_missing"


def test_worker_renews_the_claim_while_a_long_handler_is_running(monkeypatch) -> None:
    import src.market_morning.jobs.worker as worker

    claim = _claim()
    factory = _SessionFactory(object(), object(), object())
    renewed = asyncio.Event()
    waits = 0
    renewals: list[dict] = []
    completions: list[dict] = []

    async def claim_next_job(session, **kwargs):
        return claim

    async def renew_claimed_job_lease(session, **kwargs):
        renewals.append(kwargs)
        renewed.set()
        return NOW + timedelta(minutes=2)

    async def complete_claimed_job(session, **kwargs):
        completions.append(kwargs)

    async def wait_for_heartbeat(stop_event, timeout_seconds):
        nonlocal waits
        waits += 1
        if waits == 1:
            return False
        await stop_event.wait()
        return True

    async def handler(payload):
        await renewed.wait()
        return {"edition_id": "edition-1"}

    monkeypatch.setattr(worker, "claim_next_job", claim_next_job)
    monkeypatch.setattr(worker, "renew_claimed_job_lease", renew_claimed_job_lease)
    monkeypatch.setattr(worker, "complete_claimed_job", complete_claimed_job)
    monkeypatch.setattr(worker, "_wait_for_heartbeat", wait_for_heartbeat)

    result = asyncio.run(
        worker.run_one_job(
            worker_id="worker-a",
            handlers={claim.job_type: handler},
            session_factory=factory,
            clock=lambda: NOW,
            lease_duration=timedelta(minutes=2),
        )
    )

    assert result.status is worker.WorkerRunStatus.SUCCEEDED
    assert len(renewals) == 1
    assert renewals[0]["job_id"] == JOB_ID
    assert renewals[0]["worker_id"] == "worker-a"
    assert renewals[0]["lease_duration"] == timedelta(minutes=2)
    assert len(completions) == 1
    assert factory.entries == [
        "enter",
        "commit",
        "enter",
        "commit",
        "enter",
        "commit",
    ]


def test_worker_cancels_handler_and_does_not_complete_after_lease_loss(monkeypatch) -> None:
    import src.market_morning.jobs.worker as worker
    from src.market_morning.repositories.jobs import JobClaimConflict

    claim = _claim()
    factory = _SessionFactory(object(), object())
    cancelled: list[bool] = []

    async def claim_next_job(session, **kwargs):
        return claim

    async def renew_claimed_job_lease(session, **kwargs):
        raise JobClaimConflict("job lease has expired")

    async def wait_for_heartbeat(stop_event, timeout_seconds):
        return False

    async def handler(payload):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(True)

    monkeypatch.setattr(worker, "claim_next_job", claim_next_job)
    monkeypatch.setattr(worker, "renew_claimed_job_lease", renew_claimed_job_lease)
    monkeypatch.setattr(worker, "_wait_for_heartbeat", wait_for_heartbeat)

    with pytest.raises(worker.WorkerLeaseLost, match="lease renewal failed"):
        asyncio.run(
            worker.run_one_job(
                worker_id="worker-a",
                handlers={claim.job_type: handler},
                session_factory=factory,
                clock=lambda: NOW,
            )
        )

    assert cancelled == [True]
    assert factory.entries == ["enter", "commit", "enter", "rollback"]


def test_worker_loop_stops_cleanly_after_an_idle_poll(monkeypatch) -> None:
    import src.market_morning.jobs.worker as worker

    stop_event = asyncio.Event()
    factory = _SessionFactory(object())

    async def recover_one_expired_job(session, **kwargs):
        return None

    async def run_one_job(**kwargs):
        return worker.WorkerRunResult(status=worker.WorkerRunStatus.IDLE)

    async def wait_for_poll(event, timeout_seconds):
        event.set()
        return True

    monkeypatch.setattr(worker, "recover_one_expired_job", recover_one_expired_job)
    monkeypatch.setattr(worker, "run_one_job", run_one_job)
    monkeypatch.setattr(worker, "_wait_for_poll", wait_for_poll)

    summary = asyncio.run(
        worker.run_worker(
            worker_id="worker-a",
            handlers={},
            stop_event=stop_event,
            session_factory=factory,
            clock=lambda: NOW,
        )
    )

    assert summary.iterations == 1
    assert summary.idle_polls == 1
    assert summary.succeeded_jobs == 0
    assert summary.infrastructure_failures == 0
    assert factory.entries == ["enter", "commit"]


def test_worker_loop_recovers_an_expired_job_before_claiming_work(monkeypatch) -> None:
    import src.market_morning.jobs.worker as worker

    stop_event = asyncio.Event()
    factory = _SessionFactory(object())
    lifecycle: list[tuple] = []

    async def recover_one_expired_job(session, **kwargs):
        lifecycle.append(("recover", kwargs))
        return JOB_ID

    async def run_one_job(**kwargs):
        lifecycle.append(("run", kwargs))
        stop_event.set()
        return worker.WorkerRunResult(
            status=worker.WorkerRunStatus.SUCCEEDED,
            job_id=JOB_ID,
            attempt_number=2,
        )

    monkeypatch.setattr(worker, "recover_one_expired_job", recover_one_expired_job)
    monkeypatch.setattr(worker, "run_one_job", run_one_job)

    summary = asyncio.run(
        worker.run_worker(
            worker_id="worker-a",
            handlers={},
            stop_event=stop_event,
            session_factory=factory,
            clock=lambda: NOW,
            expired_retry_delay=timedelta(minutes=1),
        )
    )

    assert [item[0] for item in lifecycle] == ["recover", "run"]
    assert lifecycle[0][1]["now"] == NOW
    assert lifecycle[0][1]["retry_at"] == NOW + timedelta(minutes=1)
    assert summary.recovered_jobs == 1
    assert summary.succeeded_jobs == 1


def test_worker_loop_contains_infrastructure_errors_and_logs_no_secret(
    monkeypatch,
    caplog,
) -> None:
    import src.market_morning.jobs.worker as worker

    stop_event = asyncio.Event()
    factory = _SessionFactory(object())

    async def recover_one_expired_job(session, **kwargs):
        return None

    async def run_one_job(**kwargs):
        raise RuntimeError("secret database response")

    async def wait_for_poll(event, timeout_seconds):
        event.set()
        return True

    monkeypatch.setattr(worker, "recover_one_expired_job", recover_one_expired_job)
    monkeypatch.setattr(worker, "run_one_job", run_one_job)
    monkeypatch.setattr(worker, "_wait_for_poll", wait_for_poll)

    summary = asyncio.run(
        worker.run_worker(
            worker_id="worker-a",
            handlers={},
            stop_event=stop_event,
            session_factory=factory,
            clock=lambda: NOW,
        )
    )

    assert summary.infrastructure_failures == 1
    assert "RuntimeError" in caplog.text
    assert "secret database response" not in caplog.text
