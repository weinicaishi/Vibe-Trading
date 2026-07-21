"""EventBrief generation orchestration and transaction-boundary contracts."""

from __future__ import annotations

import asyncio
from collections import deque
from datetime import datetime, timezone

import pytest

NOW = datetime(2026, 7, 21, 0, 0, tzinfo=timezone.utc)
EVENT_ID = "11111111-1111-4111-8111-111111111111"
BRIEF_ID = "22222222-2222-4222-8222-222222222222"
SOURCE_ID = "33333333-3333-4333-8333-333333333333"


@pytest.fixture(autouse=True)
def _stub_model_usage_repository(monkeypatch) -> None:
    import src.market_morning.event_brief_service as service

    async def record(_session, *, event):
        return event

    monkeypatch.setattr(service, "record_model_usage", record)


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


def _spec():
    from src.market_morning.event_brief_generation import EventBriefGenerationSpec

    return EventBriefGenerationSpec(
        event_id=EVENT_ID,
        event_version=1,
        generation_key=(
            f"event-brief:{EVENT_ID}:v1:fixture-model-v1:prompt-v1:s1"
        ),
        model_version="fixture-model-v1",
        prompt_version="prompt-v1",
        started_at=NOW,
        max_attempts=3,
    )


def _context(*, lifecycle_status: str = "active", sources: bool = True):
    from src.market_morning.event_brief_service import (
        EventBriefGenerationInput,
        EventBriefSourceInput,
    )

    return EventBriefGenerationInput(
        event_id=EVENT_ID,
        event_version=1,
        title="業績予想の修正",
        occurred_at=NOW,
        lifecycle_status=lifecycle_status,
        sources=(
            (
                EventBriefSourceInput(
                    source_id=SOURCE_ID,
                    provider="tdnet",
                    original_url="https://example.com/source",
                    evidence_text="2026年7月21日に業績予想を修正した。",
                ),
            )
            if sources
            else ()
        ),
    )


def _model_payload():
    return {
        "schema_version": 1,
        "event_id": EVENT_ID,
        "event_version": 1,
        "title": "業績予想の修正",
        "occurred_at": NOW.isoformat(),
        "confirmed_facts": [
            {
                "text": "2026年7月21日に業績予想を修正した。",
                "source_ids": [SOURCE_ID],
            }
        ],
        "open_questions": ["影響は追加資料の確認が必要。"],
        "source_ids": [SOURCE_ID],
        "model_version": "fixture-model-v1",
        "review_status": "pending",
    }


def _start_result(*, attempt: int = 1):
    from src.market_morning.repositories.event_briefs import (
        EventBriefStartResult,
        EventBriefStartStatus,
    )

    return EventBriefStartResult(
        status=(
            EventBriefStartStatus.STARTED
            if attempt == 1
            else EventBriefStartStatus.RETRIED
        ),
        brief_id=BRIEF_ID,
        attempt_number=attempt,
        retry_permitted=attempt < 3,
        can_execute=True,
    )


def _publish_result(brief):
    from src.market_morning.repositories.event_briefs import (
        EventBriefPublishResult,
        EventBriefPublishResultStatus,
    )

    return EventBriefPublishResult(
        write_status=EventBriefPublishResultStatus.PUBLISHED,
        brief_id=BRIEF_ID,
        status=brief.status,
        payload_sha256="a" * 64,
    )


def test_successful_model_call_records_usage_with_terminal_brief_transaction(
    monkeypatch,
) -> None:
    import src.market_morning.event_brief_service as service
    from src.market_morning.model_usage import ModelUsageReport

    first_session = object()
    publish_session = object()
    factory = _Factory(first_session, publish_session)
    lifecycle = []

    async def record(session, *, event):
        lifecycle.append(("usage", session, event))

    async def publish(session, **kwargs):
        lifecycle.append(("publish", session))
        return _publish_result(kwargs["brief"])

    monkeypatch.setattr(
        service,
        "start_event_brief_generation",
        lambda *args, **kwargs: asyncio.sleep(0, result=_start_result()),
    )
    monkeypatch.setattr(
        service,
        "load_event_brief_generation_input",
        lambda *args, **kwargs: asyncio.sleep(0, result=_context()),
    )
    monkeypatch.setattr(service, "record_model_usage", record)
    monkeypatch.setattr(service, "publish_event_brief", publish)

    result = asyncio.run(
        service.run_event_brief_generation(
            service.EventBriefGenerationCommand(spec=_spec()),
            generator=lambda model_input: asyncio.sleep(
                0,
                result=service.EventBriefModelResponse(
                    payload=_model_payload(),
                    usage=ModelUsageReport(
                        provider="openai",
                        model="gpt-example",
                        input_tokens=100,
                        output_tokens=25,
                        billable_cost_micros=175,
                        currency="USD",
                        provider_request_id="request-secret-123",
                    ),
                ),
            ),
            reachability_checker=lambda url: asyncio.sleep(0, result=True),
            session_factory=factory,
            clock=lambda: NOW,
        )
    )

    assert result.status.value == "published"
    assert [item[0] for item in lifecycle] == ["usage", "publish"]
    usage_event = lifecycle[0][2]
    assert lifecycle[0][1] is publish_session
    assert usage_event.brief_id == BRIEF_ID
    assert usage_event.attempt_number == 1
    assert usage_event.total_tokens == 125
    assert usage_event.billable_cost_micros == 175
    assert usage_event.provider_request_id_sha256 is not None
    assert "request-secret-123" not in repr(usage_event)


def test_event_brief_service_keeps_reachability_and_model_outside_transactions(
    monkeypatch,
) -> None:
    import src.market_morning.event_brief_service as service

    first_session = object()
    publish_session = object()
    factory = _Factory(first_session, publish_session)
    lifecycle = []

    async def start(session, **kwargs):
        lifecycle.append(("start", session))
        return _start_result()

    async def load(session, **kwargs):
        lifecycle.append(("load", session))
        return _context()

    async def reachable(url):
        lifecycle.append(("reachability", url))
        return True

    async def generate(model_input):
        lifecycle.append(("model", model_input.event_id))
        return _model_payload()

    async def publish(session, **kwargs):
        lifecycle.append(("publish", session, kwargs["brief"].status.value))
        return _publish_result(kwargs["brief"])

    monkeypatch.setattr(service, "start_event_brief_generation", start)
    monkeypatch.setattr(service, "load_event_brief_generation_input", load)
    monkeypatch.setattr(service, "publish_event_brief", publish)

    result = asyncio.run(
        service.run_event_brief_generation(
            service.EventBriefGenerationCommand(spec=_spec()),
            generator=generate,
            reachability_checker=reachable,
            session_factory=factory,
            clock=lambda: NOW,
        )
    )

    assert result.status.value == "published"
    assert lifecycle == [
        ("start", first_session),
        ("load", first_session),
        ("reachability", "https://example.com/source"),
        ("model", EVENT_ID),
        ("publish", publish_session, "published"),
    ]
    assert factory.entries == [
        ("enter", first_session),
        ("commit", first_session),
        ("enter", publish_session),
        ("commit", publish_session),
    ]


def test_existing_terminal_event_brief_returns_its_durable_hash(monkeypatch) -> None:
    import src.market_morning.event_brief_service as service
    from src.market_morning.event_briefs import EventBriefPublishStatus
    from src.market_morning.repositories.event_briefs import (
        EventBriefStartResult,
        EventBriefStartStatus,
    )

    factory = _Factory(object())
    start = EventBriefStartResult(
        status=EventBriefStartStatus.ALREADY_TERMINAL,
        brief_id=BRIEF_ID,
        attempt_number=1,
        retry_permitted=False,
        can_execute=False,
        terminal_status=EventBriefPublishStatus.PUBLISHED,
        payload_sha256="c" * 64,
    )
    monkeypatch.setattr(
        service,
        "start_event_brief_generation",
        lambda *args, **kwargs: asyncio.sleep(0, result=start),
    )

    result = asyncio.run(
        service.run_event_brief_generation(
            service.EventBriefGenerationCommand(spec=_spec()),
            generator=lambda model_input: pytest.fail("model must not run"),
            reachability_checker=lambda url: pytest.fail("checker must not run"),
            session_factory=factory,
            clock=lambda: NOW,
        )
    )

    assert result.payload_sha256 == "c" * 64


def test_event_brief_input_loader_reconstructs_deduplicated_source_evidence() -> None:
    import src.market_morning.event_brief_service as service

    second_source_id = "44444444-4444-4444-8444-444444444444"
    rows = [
        {
            "event_id": EVENT_ID,
            "event_version": 1,
            "event_title": "業績予想の修正",
            "occurred_at": NOW.replace(tzinfo=None),
            "lifecycle_status": "active",
            "source_record_id": SOURCE_ID,
            "source_provider": "tdnet",
            "original_url": "https://example.com/source-1",
            "source_title": "適時開示",
            "normalized_payload": {"evidence_text": "  修正内容の根拠  "},
        },
        {
            "event_id": EVENT_ID,
            "event_version": 1,
            "event_title": "業績予想の修正",
            "occurred_at": NOW.replace(tzinfo=None),
            "lifecycle_status": "active",
            "source_record_id": SOURCE_ID,
            "source_provider": "tdnet",
            "original_url": "https://example.com/source-1",
            "source_title": "適時開示",
            "normalized_payload": {},
        },
        {
            "event_id": EVENT_ID,
            "event_version": 1,
            "event_title": "業績予想の修正",
            "occurred_at": NOW.replace(tzinfo=None),
            "lifecycle_status": "active",
            "source_record_id": second_source_id,
            "source_provider": "company_ir",
            "original_url": "https://example.com/source-2",
            "source_title": "  会社IR資料  ",
            "normalized_payload": {},
        },
    ]

    class Result:
        def mappings(self):
            return self

        def all(self):
            return rows

    class Session:
        async def execute(self, statement):
            return Result()

    context = asyncio.run(
        service.load_event_brief_generation_input(Session(), spec=_spec())
    )

    assert context.occurred_at == NOW
    assert tuple(source.source_id for source in context.sources) == (
        SOURCE_ID,
        second_source_id,
    )
    assert context.sources[0].evidence_text == "修正内容の根拠"
    assert context.sources[1].evidence_text == "会社IR資料"


def test_transient_model_failure_is_recorded_and_retried(monkeypatch) -> None:
    import src.market_morning.event_brief_service as service

    factory = _Factory(object(), object())
    failures = []
    usage_events = []

    async def generate(model_input):
        raise service.EventBriefModelUnavailable("secret provider response")

    async def fail(session, **kwargs):
        failures.append(kwargs)
        return type(
            "Failure",
            (),
            {"retry_permitted": True, "attempt_number": 1},
        )()

    async def record_usage(session, *, event):
        usage_events.append(event)

    monkeypatch.setattr(
        service,
        "start_event_brief_generation",
        lambda *args, **kwargs: asyncio.sleep(0, result=_start_result()),
    )
    monkeypatch.setattr(
        service,
        "load_event_brief_generation_input",
        lambda *args, **kwargs: asyncio.sleep(0, result=_context()),
    )
    monkeypatch.setattr(service, "fail_event_brief_generation", fail)
    monkeypatch.setattr(service, "record_model_usage", record_usage)

    with pytest.raises(service.EventBriefGenerationRetryable) as error:
        asyncio.run(
            service.run_event_brief_generation(
                service.EventBriefGenerationCommand(spec=_spec()),
                generator=generate,
                reachability_checker=lambda url: asyncio.sleep(0, result=True),
                session_factory=factory,
                clock=lambda: NOW,
            )
        )

    assert error.value.error_code == "event_brief_model_unavailable"
    assert "secret provider response" not in str(error.value)
    assert failures[0]["error_code"] == "event_brief_model_unavailable"
    assert len(usage_events) == 1
    assert usage_events[0].usage_status == "missing"
    assert usage_events[0].cost_status == "unpriced"
    assert usage_events[0].provider == "unknown"
    assert "secret provider response" not in repr(usage_events)


def test_final_model_failure_publishes_source_only_degraded_card(monkeypatch) -> None:
    import src.market_morning.event_brief_service as service

    factory = _Factory(object(), object())
    published = []

    async def generate(model_input):
        raise service.EventBriefModelUnavailable("secret provider response")

    async def publish(session, **kwargs):
        published.append(kwargs["brief"])
        return _publish_result(kwargs["brief"])

    monkeypatch.setattr(
        service,
        "start_event_brief_generation",
        lambda *args, **kwargs: asyncio.sleep(0, result=_start_result(attempt=3)),
    )
    monkeypatch.setattr(
        service,
        "load_event_brief_generation_input",
        lambda *args, **kwargs: asyncio.sleep(0, result=_context()),
    )
    monkeypatch.setattr(service, "publish_event_brief", publish)

    result = asyncio.run(
        service.run_event_brief_generation(
            service.EventBriefGenerationCommand(spec=_spec()),
            generator=generate,
            reachability_checker=lambda url: asyncio.sleep(0, result=True),
            session_factory=factory,
            clock=lambda: NOW,
        )
    )

    assert result.status.value == "degraded"
    assert published[0].confirmed_facts == ()
    assert published[0].open_questions == ()
    assert published[0].error_codes == ("event_brief_model_unavailable",)
    assert published[0].sources[0].original_url == "https://example.com/source"


def test_invalid_model_content_is_blocked_without_persisting_its_claims(
    monkeypatch,
) -> None:
    import src.market_morning.event_brief_service as service

    factory = _Factory(object(), object())
    published = []
    invalid = _model_payload()
    invalid["confirmed_facts"][0]["text"] = "今すぐ買うべきだ。"

    async def publish(session, **kwargs):
        published.append(kwargs["brief"])
        return _publish_result(kwargs["brief"])

    monkeypatch.setattr(
        service,
        "start_event_brief_generation",
        lambda *args, **kwargs: asyncio.sleep(0, result=_start_result()),
    )
    monkeypatch.setattr(
        service,
        "load_event_brief_generation_input",
        lambda *args, **kwargs: asyncio.sleep(0, result=_context()),
    )
    monkeypatch.setattr(service, "publish_event_brief", publish)

    result = asyncio.run(
        service.run_event_brief_generation(
            service.EventBriefGenerationCommand(spec=_spec()),
            generator=lambda model_input: asyncio.sleep(0, result=invalid),
            reachability_checker=lambda url: asyncio.sleep(0, result=True),
            session_factory=factory,
            clock=lambda: NOW,
        )
    )

    assert result.status.value == "blocked"
    assert published[0].confirmed_facts == ()
    assert "prohibited_recommendation_language" in published[0].error_codes
    assert "今すぐ買うべきだ" not in repr(published[0])


def test_unreachable_source_blocks_before_model_call(monkeypatch) -> None:
    import src.market_morning.event_brief_service as service

    factory = _Factory(object(), object())
    model_called = False
    published = []

    async def generate(model_input):
        nonlocal model_called
        model_called = True
        return _model_payload()

    async def publish(session, **kwargs):
        published.append(kwargs["brief"])
        return _publish_result(kwargs["brief"])

    monkeypatch.setattr(
        service,
        "start_event_brief_generation",
        lambda *args, **kwargs: asyncio.sleep(0, result=_start_result()),
    )
    monkeypatch.setattr(
        service,
        "load_event_brief_generation_input",
        lambda *args, **kwargs: asyncio.sleep(0, result=_context()),
    )
    monkeypatch.setattr(service, "publish_event_brief", publish)

    result = asyncio.run(
        service.run_event_brief_generation(
            service.EventBriefGenerationCommand(spec=_spec()),
            generator=generate,
            reachability_checker=lambda url: asyncio.sleep(0, result=False),
            session_factory=factory,
            clock=lambda: NOW,
        )
    )

    assert model_called is False
    assert result.status.value == "blocked"
    assert published[0].error_codes == ("source_unreachable",)


def test_withdrawn_event_degrades_without_model_call(monkeypatch) -> None:
    import src.market_morning.event_brief_service as service

    factory = _Factory(object(), object())
    published = []

    async def publish(session, **kwargs):
        published.append(kwargs["brief"])
        return _publish_result(kwargs["brief"])

    monkeypatch.setattr(
        service,
        "start_event_brief_generation",
        lambda *args, **kwargs: asyncio.sleep(0, result=_start_result()),
    )
    monkeypatch.setattr(
        service,
        "load_event_brief_generation_input",
        lambda *args, **kwargs: asyncio.sleep(
            0,
            result=_context(lifecycle_status="withdrawn"),
        ),
    )
    monkeypatch.setattr(service, "publish_event_brief", publish)

    result = asyncio.run(
        service.run_event_brief_generation(
            service.EventBriefGenerationCommand(spec=_spec()),
            generator=lambda model_input: pytest.fail("model must not run"),
            reachability_checker=lambda url: asyncio.sleep(0, result=True),
            session_factory=factory,
            clock=lambda: NOW,
        )
    )

    assert result.status.value == "degraded"
    assert published[0].error_codes == ("event_withdrawn",)
