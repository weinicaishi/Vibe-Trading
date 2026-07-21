"""Durable job handlers for source ingestion and edition generation."""

from __future__ import annotations

import asyncio
from collections import deque
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import OperationalError

from src.market_morning.sources.base import (
    DiscoveryBatch,
    NormalizedSourceRecord,
    SourceDocumentRef,
    SourceLifecycleStatus,
    SourcePayload,
)

NOW = datetime(2026, 7, 20, 22, 0, tzinfo=timezone.utc)
USER_ID = "11111111-1111-4111-8111-111111111111"
EVENT_ID = "22222222-2222-4222-8222-222222222222"


class _Transaction:
    def __init__(self, session, entries):
        self.session = session
        self.entries = entries

    async def __aenter__(self):
        self.entries.append(("enter", self.session))
        return self.session

    async def __aexit__(self, exc_type, exc, traceback):
        self.entries.append(("rollback" if exc_type else "commit", self.session))


class _SessionFactory:
    def __init__(self, *sessions):
        self.sessions = deque(sessions)
        self.entries: list[tuple[str, object]] = []

    def begin(self):
        return _Transaction(self.sessions.popleft(), self.entries)


class _Adapter:
    provider = "fixture_tdnet"

    def __init__(self, *, fail: bool = False):
        self.fail = fail
        self.calls: list[tuple] = []

    async def discover(self, cursor):
        self.calls.append(("discover", cursor))
        return DiscoveryBatch(
            documents=(SourceDocumentRef("TD-001", "v1"),),
            next_cursor={"sequence": 1},
        )

    async def fetch(self, document):
        self.calls.append(("fetch", document.document_id))
        if self.fail:
            raise ConnectionError("secret upstream response")
        return SourcePayload(
            document=document,
            original_url="https://example.invalid/TD-001.pdf",
            fetched_at=NOW,
            raw_content=b"fixture",
        )

    def normalize(self, payload):
        self.calls.append(("normalize", payload.document.document_id))
        return NormalizedSourceRecord.from_payload(
            provider=self.provider,
            payload=payload,
            title="決算短信",
            document_type="earnings_release",
            published_at=NOW,
            lifecycle_status=SourceLifecycleStatus.ACTIVE,
            issuer_codes=("7203",),
        )


class _Repository:
    def __init__(self, session, lifecycle):
        self.session = session
        self.lifecycle = lifecycle

    async def load_cursor(self, provider):
        self.lifecycle.append(("load_cursor", self.session, provider))
        return {"sequence": 0}

    async def load_revision_identities(self, provider, document_ids):
        self.lifecycle.append(("load_revisions", self.session, provider, document_ids))
        return ()

    async def apply(self, plan):
        self.lifecycle.append(("apply", self.session, plan.next_cursor))
        return SimpleNamespace(
            inserted_records=1,
            duplicate_records=0,
            created_events=1,
            created_event_revisions=(
                SimpleNamespace(event_id=EVENT_ID, event_version=2),
            ),
            unresolved_issuer_codes=(),
            cursor=plan.next_cursor,
        )


def _edition_payload() -> dict:
    return {
        "user_id": USER_ID,
        "generation_key": "2026-07-21:scheduled-0700",
        "day_plan": {
            "edition_date": "2026-07-21",
            "generate": True,
            "status": "scheduled",
            "overnight_context": "us_session_available",
            "reason_code": None,
            "us_reason_code": None,
        },
        "generated_at": "2026-07-20T22:00:00+00:00",
        "published_at": "2026-07-20T22:05:00+00:00",
        "window": {
            "starts_at": "2026-07-19T22:00:00+00:00",
            "ends_at": "2026-07-20T22:00:00+00:00",
        },
        "budget": {
            "max_issuers": 10,
            "max_events_per_issuer": 5,
            "max_total_events": 30,
        },
        "source_coverage_policy": {
            "global_required_providers": ["tdnet", "edinet"],
            "issuer_required_providers": {"7203": ["company_ir:7203"]},
            "max_staleness_seconds": 1800,
        },
    }


def test_source_handler_keeps_network_collection_outside_database_transactions() -> None:
    from src.market_morning.jobs.handlers import make_source_ingestion_handler

    read_session = object()
    write_session = object()
    factory = _SessionFactory(read_session, write_session)
    adapter = _Adapter()
    lifecycle: list[tuple] = []
    handler = make_source_ingestion_handler(
        adapters={adapter.provider: adapter},
        session_factory=factory,
        repository_factory=lambda session: _Repository(session, lifecycle),
        clock=lambda: NOW,
    )

    result = asyncio.run(handler({"provider": adapter.provider}))

    assert result == {
        "provider": "fixture_tdnet",
        "inserted_records": 1,
        "duplicate_records": 0,
        "created_events": 1,
        "dispatched_event_briefs": 0,
        "unresolved_issuer_count": 0,
    }
    assert factory.entries == [
        ("enter", read_session),
        ("commit", read_session),
        ("enter", write_session),
        ("commit", write_session),
    ]
    assert lifecycle == [
        ("load_cursor", read_session, "fixture_tdnet"),
        ("load_revisions", write_session, "fixture_tdnet", ("TD-001",)),
        ("apply", write_session, {"sequence": 1}),
    ]


def test_source_handler_records_sanitized_failure_in_an_independent_transaction(
    monkeypatch,
) -> None:
    import src.market_morning.jobs.handlers as handlers
    from src.market_morning.jobs.worker import RetryableJobError

    read_session = object()
    failure_session = object()
    factory = _SessionFactory(read_session, failure_session)
    adapter = _Adapter(fail=True)
    lifecycle: list[tuple] = []
    recorded: list[dict] = []

    async def record_source_failure(session, **kwargs):
        recorded.append({"session": session, **kwargs})

    monkeypatch.setattr(handlers, "record_source_failure", record_source_failure)
    handler = handlers.make_source_ingestion_handler(
        adapters={adapter.provider: adapter},
        session_factory=factory,
        repository_factory=lambda session: _Repository(session, lifecycle),
        clock=lambda: NOW,
    )

    with pytest.raises(RetryableJobError) as raised:
        asyncio.run(handler({"provider": adapter.provider}))

    assert raised.value.error_code == "source_unavailable"
    assert "secret upstream response" not in str(raised.value)
    assert recorded == [
        {
            "session": failure_session,
            "provider": "fixture_tdnet",
            "failed_at": NOW,
            "error_code": "source_unavailable",
        }
    ]
    assert factory.entries[-2:] == [
        ("enter", failure_session),
        ("commit", failure_session),
    ]


def test_source_handler_atomically_dispatches_created_event_briefs(monkeypatch) -> None:
    import src.market_morning.jobs.handlers as handlers

    read_session = object()
    write_session = object()
    factory = _SessionFactory(read_session, write_session)
    adapter = _Adapter()
    lifecycle: list[tuple] = []
    enqueued = []

    async def enqueue(session, **kwargs):
        enqueued.append((session, kwargs))
        return SimpleNamespace(status=SimpleNamespace(value="enqueued"))

    monkeypatch.setattr(handlers, "enqueue_job", enqueue)
    handler = handlers.make_source_ingestion_handler(
        adapters={adapter.provider: adapter},
        session_factory=factory,
        repository_factory=lambda session: _Repository(session, lifecycle),
        event_brief_dispatch=handlers.EventBriefDispatchConfig(
            model_version="fixture-model-v1",
            prompt_version="prompt-v1",
            max_attempts=3,
        ),
        clock=lambda: NOW,
    )

    result = asyncio.run(handler({"provider": adapter.provider}))

    assert result["dispatched_event_briefs"] == 1
    assert enqueued == [
        (
            write_session,
            {
                "job_type": handlers.MarketMorningJobType.EVENT_BRIEF_GENERATION,
                "idempotency_key": (
                    f"event-brief:{EVENT_ID}:v2:fixture-model-v1:prompt-v1:s1"
                ),
                "payload": _event_brief_payload(),
                "priority": 20,
                "available_at": NOW,
                "max_attempts": 3,
                "created_at": NOW,
            },
        )
    ]
    assert factory.entries[-2:] == [
        ("enter", write_session),
        ("commit", write_session),
    ]


def test_source_handler_rejects_unknown_or_extra_payload_without_database_access() -> None:
    from src.market_morning.jobs.handlers import make_source_ingestion_handler
    from src.market_morning.jobs.worker import PermanentJobError

    factory = _SessionFactory()
    handler = make_source_ingestion_handler(
        adapters={},
        session_factory=factory,
        clock=lambda: NOW,
    )

    with pytest.raises(PermanentJobError, match="invalid_job_payload"):
        asyncio.run(handler({"provider": "tdnet", "unexpected": True}))
    with pytest.raises(PermanentJobError, match="source_adapter_not_registered"):
        asyncio.run(handler({"provider": "tdnet"}))
    assert factory.entries == []


def test_source_handler_retries_when_another_worker_advanced_the_cursor(
    monkeypatch,
) -> None:
    import src.market_morning.jobs.handlers as handlers
    from src.market_morning.jobs.worker import RetryableJobError
    from src.market_morning.repositories.source_ingestion import SourceCursorConflict

    read_session = object()
    write_session = object()
    failure_session = object()
    factory = _SessionFactory(read_session, write_session, failure_session)
    adapter = _Adapter()
    lifecycle: list[tuple] = []

    class ConflictingRepository(_Repository):
        async def apply(self, plan):
            raise SourceCursorConflict("cursor advanced")

    async def record_source_failure(session, **kwargs):
        return None

    monkeypatch.setattr(handlers, "record_source_failure", record_source_failure)
    handler = handlers.make_source_ingestion_handler(
        adapters={adapter.provider: adapter},
        session_factory=factory,
        repository_factory=lambda session: ConflictingRepository(session, lifecycle),
        clock=lambda: NOW,
    )

    with pytest.raises(RetryableJobError) as raised:
        asyncio.run(handler({"provider": adapter.provider}))

    assert raised.value.error_code == "source_cursor_conflict"
    assert factory.entries == [
        ("enter", read_session),
        ("commit", read_session),
        ("enter", write_session),
        ("rollback", write_session),
        ("enter", failure_session),
        ("commit", failure_session),
    ]


def test_edition_handler_decodes_the_versioned_command_and_returns_durable_ids() -> None:
    from src.market_morning.jobs.handlers import make_edition_generation_handler

    commands = []

    async def runner(command):
        commands.append(command)
        return SimpleNamespace(
            status=SimpleNamespace(value="published"),
            edition_id="edition-1",
            edition_version=2,
        )

    result = asyncio.run(make_edition_generation_handler(runner=runner)(_edition_payload()))

    assert result == {
        "edition_id": "edition-1",
        "edition_version": 2,
        "publish_status": "published",
    }
    command = commands[0]
    assert command.user_id == USER_ID
    assert command.day_plan.edition_date == date(2026, 7, 21)
    assert command.window.starts_at == datetime(2026, 7, 19, 22, 0, tzinfo=timezone.utc)
    assert command.source_coverage_policy.global_required_providers == ("tdnet", "edinet")
    assert command.source_coverage_policy.max_staleness == timedelta(minutes=30)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload.update({"unexpected": True}),
        lambda payload: payload["day_plan"].update({"generate": "yes"}),
        lambda payload: payload.update({"generated_at": "2026-07-20T22:00:00"}),
        lambda payload: payload["source_coverage_policy"].update(
            {"max_staleness_seconds": 0}
        ),
    ],
)
def test_edition_handler_rejects_malformed_payload_as_permanent(mutate) -> None:
    from src.market_morning.jobs.handlers import make_edition_generation_handler
    from src.market_morning.jobs.worker import PermanentJobError

    payload = _edition_payload()
    mutate(payload)

    async def runner(command):
        raise AssertionError("runner must not be called")

    with pytest.raises(PermanentJobError, match="invalid_job_payload"):
        asyncio.run(make_edition_generation_handler(runner=runner)(payload))


def test_edition_handler_classifies_expected_and_transient_failures() -> None:
    from src.market_morning.edition_generation import EditionGenerationSourceRowsExceeded
    from src.market_morning.jobs.handlers import make_edition_generation_handler
    from src.market_morning.jobs.worker import PermanentJobError, RetryableJobError

    async def over_budget(command):
        raise EditionGenerationSourceRowsExceeded("too many rows")

    with pytest.raises(PermanentJobError, match="edition_source_budget_exceeded"):
        asyncio.run(make_edition_generation_handler(runner=over_budget)(_edition_payload()))

    async def database_down(command):
        raise OperationalError("SELECT 1", {}, ConnectionError("secret DSN"))

    with pytest.raises(RetryableJobError) as raised:
        asyncio.run(make_edition_generation_handler(runner=database_down)(_edition_payload()))
    assert raised.value.error_code == "edition_database_unavailable"
    assert "secret DSN" not in str(raised.value)


def test_handler_registry_is_lazy_and_only_registers_implemented_job_types(
    monkeypatch,
) -> None:
    import src.market_morning.jobs.handlers as handlers
    from src.market_morning.repositories.jobs import MarketMorningJobType

    def database_must_remain_lazy():
        raise AssertionError("registry construction must not initialize MySQL")

    monkeypatch.setattr(handlers, "get_session_factory", database_must_remain_lazy)

    registry = handlers.build_job_handlers(adapters={})

    assert set(registry) == {
        MarketMorningJobType.ACCOUNT_DELETION,
        MarketMorningJobType.SOURCE_INGESTION,
        MarketMorningJobType.EDITION_GENERATION,
    }


class _MarketDataAdapter:
    provider = "fixture_market_data"

    def __init__(self, snapshot=None, error: Exception | None = None):
        from src.market_morning.market_snapshots import MarketInstrument

        self.instrument = MarketInstrument.NIKKEI_225
        self.snapshot = snapshot
        self.error = error
        self.calls = []

    async def fetch(self, *, at):
        self.calls.append(at)
        if self.error is not None:
            raise self.error
        return self.snapshot


def _market_snapshot(*, instrument="nikkei_225"):
    from decimal import Decimal

    from src.market_morning.market_snapshots import MarketSnapshot

    return MarketSnapshot(
        instrument=instrument,
        provider="fixture_market_data",
        session_date=date(2026, 7, 20),
        as_of=NOW - timedelta(minutes=5),
        value=Decimal("39819.11"),
        previous_close=Decimal("39780.00"),
        currency="JPY",
        delay_status="eod",
        fetched_at=NOW,
    )


def _market_snapshot_payload() -> dict:
    return {
        "schema_version": 1,
        "instrument": "nikkei_225",
        "scheduled_at": "2026-07-20T22:00:00+00:00",
    }


def _event_brief_payload() -> dict:
    event_id = "22222222-2222-4222-8222-222222222222"
    return {
        "schema_version": 1,
        "event_id": event_id,
        "event_version": 2,
        "generation_key": (
            f"event-brief:{event_id}:v2:fixture-model-v1:prompt-v1:s1"
        ),
        "model_version": "fixture-model-v1",
        "prompt_version": "prompt-v1",
        "started_at": "2026-07-20T22:00:00+00:00",
        "max_attempts": 3,
    }


def _email_delivery_payload() -> dict:
    return {
        "schema_version": 1,
        "delivery_attempt_id": "33333333-3333-4333-8333-333333333333",
    }


def test_event_brief_handler_decodes_command_and_returns_terminal_result() -> None:
    from src.market_morning.event_briefs import EventBriefPublishStatus
    from src.market_morning.jobs.handlers import make_event_brief_generation_handler

    commands = []

    async def runner(command):
        commands.append(command)
        return SimpleNamespace(
            brief_id="brief-1",
            status=EventBriefPublishStatus.PUBLISHED,
            attempt_number=2,
            payload_sha256="a" * 64,
        )

    result = asyncio.run(
        make_event_brief_generation_handler(runner=runner)(
            _event_brief_payload()
        )
    )

    assert commands[0].spec.event_version == 2
    assert commands[0].spec.started_at == NOW
    assert result == {
        "brief_id": "brief-1",
        "publish_status": "published",
        "attempt_number": 2,
        "payload_sha256": "a" * 64,
    }


def test_event_brief_handler_rejects_payload_and_sanitizes_retry(monkeypatch) -> None:
    from src.market_morning.event_brief_service import (
        EventBriefGenerationRetryable,
    )
    from src.market_morning.jobs.handlers import make_event_brief_generation_handler
    from src.market_morning.jobs.worker import PermanentJobError, RetryableJobError

    async def should_not_run(command):
        raise AssertionError("runner must not be called")

    invalid = _event_brief_payload() | {"unexpected": True}
    with pytest.raises(PermanentJobError, match="invalid_job_payload"):
        asyncio.run(make_event_brief_generation_handler(runner=should_not_run)(invalid))

    async def transient(command):
        raise EventBriefGenerationRetryable("event_brief_model_unavailable")

    with pytest.raises(RetryableJobError) as error:
        asyncio.run(
            make_event_brief_generation_handler(runner=transient)(
                _event_brief_payload()
            )
        )
    assert error.value.error_code == "event_brief_model_unavailable"


def test_handler_registry_adds_event_brief_only_with_configured_runner() -> None:
    from src.market_morning.jobs.handlers import (
        EventBriefDispatchConfig,
        build_job_handlers,
    )
    from src.market_morning.repositories.jobs import MarketMorningJobType

    async def runner(command):
        raise AssertionError

    default = build_job_handlers(adapters={})
    with pytest.raises(ValueError, match="event_brief_dispatch"):
        build_job_handlers(adapters={}, event_brief_runner=runner)
    configured = build_job_handlers(
        adapters={},
        event_brief_runner=runner,
        event_brief_dispatch=EventBriefDispatchConfig(
            model_version="fixture-model-v1",
            prompt_version="prompt-v1",
        ),
    )

    assert MarketMorningJobType.EVENT_BRIEF_GENERATION not in default
    assert MarketMorningJobType.EVENT_BRIEF_GENERATION in configured


def test_email_delivery_handler_decodes_private_attempt_and_returns_result() -> None:
    from src.market_morning.email_delivery_service import EmailDeliveryResult
    from src.market_morning.jobs.handlers import make_email_delivery_handler
    from src.market_morning.repositories.email_delivery import (
        DeliveryLifecycleStatus,
    )

    commands = []

    async def runner(command):
        commands.append(command)
        return EmailDeliveryResult(
            delivery_attempt_id=_email_delivery_payload()["delivery_attempt_id"],
            status=DeliveryLifecycleStatus.SENT,
            attempt_number=1,
            provider_message_id="provider-message-1",
        )

    result = asyncio.run(
        make_email_delivery_handler(runner=runner)(_email_delivery_payload())
    )

    assert commands[0].delivery_attempt_id == _email_delivery_payload()[
        "delivery_attempt_id"
    ]
    assert result == {
        "delivery_attempt_id": _email_delivery_payload()["delivery_attempt_id"],
        "delivery_status": "sent",
        "attempt_number": 1,
        "provider_message_id": "provider-message-1",
        "reason_code": None,
    }


def test_email_delivery_handler_rejects_secrets_and_sanitizes_retry() -> None:
    from src.market_morning.email_delivery_service import EmailDeliveryRetryable
    from src.market_morning.jobs.handlers import make_email_delivery_handler
    from src.market_morning.jobs.worker import PermanentJobError, RetryableJobError

    async def should_not_run(command):
        raise AssertionError("runner must not be called")

    secret_payload = _email_delivery_payload() | {
        "email": "private@example.jp",
    }
    with pytest.raises(PermanentJobError, match="invalid_job_payload"):
        asyncio.run(make_email_delivery_handler(runner=should_not_run)(secret_payload))

    async def transient(command):
        raise EmailDeliveryRetryable("email_provider_unavailable")

    with pytest.raises(RetryableJobError) as error:
        asyncio.run(
            make_email_delivery_handler(runner=transient)(
                _email_delivery_payload()
            )
        )
    assert error.value.error_code == "email_provider_unavailable"


def test_handler_registry_adds_email_only_with_configured_runner() -> None:
    from src.market_morning.jobs.handlers import build_job_handlers
    from src.market_morning.repositories.jobs import MarketMorningJobType

    async def runner(command):
        raise AssertionError

    default = build_job_handlers(adapters={})
    with pytest.raises(ValueError, match="email_delivery_runner"):
        build_job_handlers(adapters={}, email_delivery_runner=runner)

    async def dispatcher(command):
        raise AssertionError

    configured = build_job_handlers(
        adapters={},
        email_delivery_runner=runner,
        email_dispatch_runner=dispatcher,
    )

    assert MarketMorningJobType.EMAIL_DELIVERY not in default
    assert MarketMorningJobType.EMAIL_DELIVERY in configured


def test_market_snapshot_handler_fetches_outside_transaction_then_persists(
    monkeypatch,
) -> None:
    import src.market_morning.jobs.handlers as handlers
    from src.market_morning.repositories.market_snapshots import (
        MarketSnapshotWriteResult,
        MarketSnapshotWriteStatus,
    )

    snapshot = _market_snapshot()
    adapter = _MarketDataAdapter(snapshot=snapshot)
    write_session = object()
    factory = _SessionFactory(write_session)
    persisted = []

    async def persist(session, **kwargs):
        persisted.append((session, kwargs))
        return MarketSnapshotWriteResult(
            status=MarketSnapshotWriteStatus.INSERTED,
            snapshot_id="snapshot-1",
        )

    monkeypatch.setattr(handlers, "persist_market_snapshot", persist)
    handler = handlers.make_market_snapshot_handler(
        adapters={adapter.instrument: adapter},
        session_factory=factory,
        clock=lambda: NOW,
    )

    result = asyncio.run(handler(_market_snapshot_payload()))

    assert adapter.calls == [NOW]
    assert factory.entries == [
        ("enter", write_session),
        ("commit", write_session),
    ]
    assert persisted == [
        (write_session, {"snapshot": snapshot, "created_at": NOW})
    ]
    assert result == {
        "snapshot_id": "snapshot-1",
        "instrument": "nikkei_225",
        "provider": "fixture_market_data",
        "write_status": "inserted",
        "session_date": "2026-07-20",
    }


def test_market_snapshot_handler_rejects_payload_and_adapter_contract_drift() -> None:
    from src.market_morning.jobs.handlers import make_market_snapshot_handler
    from src.market_morning.jobs.worker import PermanentJobError

    factory = _SessionFactory()
    adapter = _MarketDataAdapter(snapshot=_market_snapshot(instrument="sp_500"))
    handler = make_market_snapshot_handler(
        adapters={adapter.instrument: adapter},
        session_factory=factory,
        clock=lambda: NOW,
    )

    invalid = _market_snapshot_payload() | {"unexpected": True}
    with pytest.raises(PermanentJobError, match="invalid_job_payload"):
        asyncio.run(handler(invalid))
    with pytest.raises(PermanentJobError, match="market_snapshot_adapter_contract"):
        asyncio.run(handler(_market_snapshot_payload()))
    assert factory.entries == []


@pytest.mark.parametrize(
    ("error", "error_code"),
    [
        (ConnectionError("secret upstream"), "market_snapshot_unavailable"),
        (
            OperationalError("INSERT", {}, ConnectionError("secret DSN")),
            "market_snapshot_database_unavailable",
        ),
    ],
)
def test_market_snapshot_handler_sanitizes_retryable_failures(
    monkeypatch,
    error,
    error_code,
) -> None:
    import src.market_morning.jobs.handlers as handlers
    from src.market_morning.jobs.worker import RetryableJobError

    snapshot = _market_snapshot()
    adapter = _MarketDataAdapter(
        snapshot=snapshot,
        error=error if isinstance(error, ConnectionError) else None,
    )
    factory = _SessionFactory(object())

    if isinstance(error, OperationalError):
        async def persist(*args, **kwargs):
            raise error

        monkeypatch.setattr(handlers, "persist_market_snapshot", persist)

    handler = handlers.make_market_snapshot_handler(
        adapters={adapter.instrument: adapter},
        session_factory=factory,
        clock=lambda: NOW,
    )
    with pytest.raises(RetryableJobError) as raised:
        asyncio.run(handler(_market_snapshot_payload()))

    assert raised.value.error_code == error_code
    assert "secret" not in str(raised.value)


def test_handler_registry_adds_market_snapshot_only_when_adapters_are_configured() -> None:
    from src.market_morning.jobs.handlers import build_job_handlers
    from src.market_morning.repositories.jobs import MarketMorningJobType

    adapter = _MarketDataAdapter(snapshot=_market_snapshot())
    registry = build_job_handlers(
        adapters={},
        market_data_adapters={adapter.instrument: adapter},
    )

    assert MarketMorningJobType.MARKET_SNAPSHOT in registry


def _global_run_payload() -> dict:
    return {
        "schema_version": 1,
        "edition_date_jst": "2026-07-21",
        "attempt_key": "0700",
        "scheduled_at": "2026-07-20T22:00:00+00:00",
        "scenario": "a_standard",
        "email_permitted": True,
        "late": False,
        "us_reference_date": "2026-07-20",
        "us_last_valid_session_date": "2026-07-20",
        "next_jp_session_date": None,
        "reason_code": None,
        "expected_market_sessions": {
            "nikkei_225": "2026-07-20",
            "sp_500": "2026-07-20",
            "nasdaq_composite": "2026-07-20",
            "djia": "2026-07-20",
            "usd_jpy": "2026-07-21",
        },
        "day_plan": {
            "edition_date": "2026-07-21",
            "generate": True,
            "status": "scheduled",
            "overnight_context": "us_session_available",
            "reason_code": None,
            "us_reason_code": None,
        },
    }


def test_global_run_handler_decodes_scheduler_contract_and_returns_run_result() -> None:
    from src.market_morning.global_run_service import GlobalRunExecutionResult
    from src.market_morning.jobs.handlers import make_global_edition_run_handler

    commands = []

    async def runner(command):
        commands.append(command)
        return GlobalRunExecutionResult(
            run_id="run-1",
            run_version=1,
            publication_status="complete",
            publish_status="published",
            email_permitted=True,
        )

    result = asyncio.run(
        make_global_edition_run_handler(runner=runner, clock=lambda: NOW)(
            _global_run_payload()
        )
    )

    assert commands[0].spec.generation_key == "global-edition-run:2026-07-21:0700"
    assert commands[0].spec.started_at == NOW
    assert commands[0].expected_market_sessions["nikkei_225"] == date(2026, 7, 20)
    assert result == {
        "run_id": "run-1",
        "run_version": 1,
        "publication_status": "complete",
        "publish_status": "published",
        "email_permitted": True,
        "email_dispatch_status": "not_configured",
        "email_enqueued_jobs": 0,
    }


def test_global_run_handler_dispatches_email_after_successful_publication() -> None:
    from src.market_morning.email_dispatch_service import EmailDispatchResult
    from src.market_morning.global_run_service import GlobalRunExecutionResult
    from src.market_morning.jobs.handlers import make_global_edition_run_handler

    dispatched = []

    async def runner(command):
        return GlobalRunExecutionResult(
            run_id="22222222-2222-4222-8222-222222222222",
            run_version=1,
            publication_status="complete",
            publish_status="published",
            email_permitted=True,
        )

    async def dispatcher(command):
        dispatched.append(command)
        return EmailDispatchResult(
            global_run_id=command.global_run_id,
            edition_date=command.edition_date,
            dispatch_status="dispatched",
            eligible_users=2,
            created_attempts=2,
            existing_attempts=0,
            enqueued_jobs=2,
            existing_jobs=0,
        )

    result = asyncio.run(
        make_global_edition_run_handler(
            runner=runner,
            email_dispatcher=dispatcher,
            clock=lambda: NOW,
        )(_global_run_payload())
    )

    assert dispatched[0].global_run_id == "22222222-2222-4222-8222-222222222222"
    assert dispatched[0].edition_date == date(2026, 7, 21)
    assert dispatched[0].dispatched_at == NOW
    assert result["email_dispatch_status"] == "dispatched"
    assert result["email_enqueued_jobs"] == 2


def test_global_run_handler_rejects_missing_market_session_and_retries_database() -> None:
    from src.market_morning.jobs.handlers import make_global_edition_run_handler
    from src.market_morning.jobs.worker import PermanentJobError, RetryableJobError

    async def should_not_run(command):
        raise AssertionError

    payload = _global_run_payload()
    payload["expected_market_sessions"]["nikkei_225"] = None
    with pytest.raises(PermanentJobError, match="invalid_job_payload"):
        asyncio.run(make_global_edition_run_handler(runner=should_not_run)(payload))

    async def database_down(command):
        raise OperationalError("SELECT", {}, ConnectionError("secret DSN"))

    with pytest.raises(RetryableJobError) as raised:
        asyncio.run(
            make_global_edition_run_handler(runner=database_down)(
                _global_run_payload()
            )
        )
    assert raised.value.error_code == "global_run_database_unavailable"
    assert "secret" not in str(raised.value)


def test_handler_registry_adds_global_run_only_with_complete_runner() -> None:
    from src.market_morning.jobs.handlers import build_job_handlers
    from src.market_morning.repositories.jobs import MarketMorningJobType

    async def runner(command):
        raise AssertionError

    registry = build_job_handlers(adapters={}, global_run_runner=runner)

    assert MarketMorningJobType.GLOBAL_EDITION_RUN in registry


def test_handler_registry_builds_real_global_runner_from_provider_policy() -> None:
    from src.market_morning.jobs.handlers import build_job_handlers
    from src.market_morning.market_snapshots import MarketInstrument
    from src.market_morning.repositories.jobs import MarketMorningJobType

    providers = {
        instrument: f"fixture_{instrument.value}" for instrument in MarketInstrument
    }
    registry = build_job_handlers(
        adapters={},
        global_market_data_providers=providers,
        session_factory=_SessionFactory(),
        clock=lambda: NOW,
    )

    assert MarketMorningJobType.GLOBAL_EDITION_RUN in registry
