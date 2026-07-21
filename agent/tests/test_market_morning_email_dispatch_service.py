"""Dispatch eligible reminder jobs after one successful global edition run."""

from __future__ import annotations

import asyncio
from collections import deque
from datetime import date, datetime, timezone

import pytest

NOW = datetime(2026, 7, 21, 0, 0, tzinfo=timezone.utc)
USER_ID = "11111111-1111-4111-8111-111111111111"
RUN_ID = "22222222-2222-4222-8222-222222222222"
ATTEMPT_ID = "33333333-3333-4333-8333-333333333333"


class _Transaction:
    def __init__(self, session, entries):
        self.session = session
        self.entries = entries

    async def __aenter__(self):
        self.entries.append(("enter", self.session))
        return self.session

    async def __aexit__(self, exc_type, exc, traceback):
        self.entries.append(("rollback" if exc_type else "commit", self.session))


class _Factory:
    def __init__(self, *sessions):
        self.sessions = deque(sessions)
        self.entries = []

    def begin(self):
        return _Transaction(self.sessions.popleft(), self.entries)


def _eligible_user():
    from src.market_morning.delivery_eligibility import DeliveryEligibleUser

    return DeliveryEligibleUser(
        user_id=USER_ID,
        timezone="Asia/Tokyo",
        active_issuer_count=3,
        last_product_activity_at=NOW,
    )


def _command():
    from src.market_morning.email_dispatch_service import EmailDispatchCommand

    return EmailDispatchCommand(
        global_run_id=RUN_ID,
        edition_date=date(2026, 7, 21),
        dispatched_at=NOW,
    )


def test_email_dispatch_builds_digests_outside_transactions_and_atomically_enqueues(
    monkeypatch,
) -> None:
    import src.market_morning.email_dispatch_service as service
    from src.market_morning.repositories.email_delivery import (
        DeliveryCreateResult,
        DeliveryCreateStatus,
    )
    from src.market_morning.repositories.jobs import (
        EnqueuedJob,
        JobEnqueueStatus,
        MarketMorningJobType,
    )

    read_session = object()
    write_session = object()
    factory = _Factory(read_session, write_session)
    lifecycle = []

    async def load_run(session, **kwargs):
        lifecycle.append(("load_run", session))
        return True

    async def load_users(session, **kwargs):
        lifecycle.append(("load_users", session))
        return (_eligible_user(),)

    async def digest(user_id, global_run_id, edition_date):
        lifecycle.append(("digest", user_id))
        return "a" * 64

    async def create(session, **kwargs):
        lifecycle.append(("create", session, kwargs))
        return DeliveryCreateResult(
            status=DeliveryCreateStatus.CREATED,
            delivery_attempt_id=ATTEMPT_ID,
            idempotency_key="email-key",
        )

    async def enqueue(session, **kwargs):
        lifecycle.append(("enqueue", session, kwargs))
        return EnqueuedJob(
            status=JobEnqueueStatus.ENQUEUED,
            job_id="44444444-4444-4444-8444-444444444444",
            job_type=MarketMorningJobType.EMAIL_DELIVERY,
            idempotency_key=kwargs["idempotency_key"],
            available_at=NOW,
        )

    monkeypatch.setattr(service, "is_dispatchable_global_run", load_run)
    monkeypatch.setattr(service, "load_delivery_eligible_users", load_users)
    monkeypatch.setattr(service, "create_delivery_attempt", create)
    monkeypatch.setattr(service, "enqueue_job", enqueue)

    result = asyncio.run(
        service.dispatch_email_deliveries(
            _command(),
            token_digest_builder=digest,
            session_factory=factory,
        )
    )

    assert result.eligible_users == 1
    assert result.enqueued_jobs == 1
    assert lifecycle[:2] == [
        ("load_run", read_session),
        ("load_users", read_session),
    ]
    assert lifecycle[2] == ("digest", USER_ID)
    create_call = lifecycle[3]
    enqueue_call = lifecycle[4]
    assert create_call[0:2] == ("create", write_session)
    assert create_call[2]["deep_link_token_sha256"] == "a" * 64
    assert enqueue_call[0:2] == ("enqueue", write_session)
    assert enqueue_call[2]["payload"] == {
        "schema_version": 1,
        "delivery_attempt_id": ATTEMPT_ID,
    }
    assert "email" not in enqueue_call[2]["payload"]
    assert factory.entries == [
        ("enter", read_session),
        ("commit", read_session),
        ("enter", write_session),
        ("commit", write_session),
    ]


def test_email_dispatch_skips_run_that_no_longer_permits_email(monkeypatch) -> None:
    import src.market_morning.email_dispatch_service as service

    factory = _Factory(object())
    monkeypatch.setattr(
        service,
        "is_dispatchable_global_run",
        lambda *args, **kwargs: asyncio.sleep(0, result=False),
    )
    monkeypatch.setattr(
        service,
        "load_delivery_eligible_users",
        lambda *args, **kwargs: pytest.fail("users must not be loaded"),
    )

    result = asyncio.run(
        service.dispatch_email_deliveries(
            _command(),
            token_digest_builder=lambda *args: pytest.fail("digest must not run"),
            session_factory=factory,
        )
    )

    assert result.dispatch_status == "run_ineligible"
    assert result.enqueued_jobs == 0


def test_email_dispatch_digest_failure_happens_before_any_write(monkeypatch) -> None:
    import src.market_morning.email_dispatch_service as service

    factory = _Factory(object())
    monkeypatch.setattr(
        service,
        "is_dispatchable_global_run",
        lambda *args, **kwargs: asyncio.sleep(0, result=True),
    )
    monkeypatch.setattr(
        service,
        "load_delivery_eligible_users",
        lambda *args, **kwargs: asyncio.sleep(0, result=(_eligible_user(),)),
    )

    async def unavailable(*args):
        raise service.EmailDispatchPortUnavailable("secret signer response")

    with pytest.raises(service.EmailDispatchRetryable) as error:
        asyncio.run(
            service.dispatch_email_deliveries(
                _command(),
                token_digest_builder=unavailable,
                session_factory=factory,
            )
        )

    assert error.value.error_code == "delivery_token_signer_unavailable"
    assert "secret signer response" not in str(error.value)
    assert factory.entries == [
        ("enter", factory.entries[0][1]),
        ("commit", factory.entries[0][1]),
    ]
