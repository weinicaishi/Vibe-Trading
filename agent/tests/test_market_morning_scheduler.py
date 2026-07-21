"""Single-active, calendar-aware scheduling contracts for Market Morning."""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone

import pytest


def _evaluation(*, generate: bool = True):
    from src.market_morning.calendar.service import (
        CalendarScenario,
        MarketDayEvaluation,
    )
    from src.market_morning.pipeline.morning_edition import (
        EditionDayInput,
        build_edition_day_plan,
    )

    return MarketDayEvaluation(
        edition_date=date(2026, 7, 21),
        scenario=(
            CalendarScenario.A_STANDARD
            if generate
            else CalendarScenario.E_MANUAL_HALT_OR_UNAVAILABLE
        ),
        day_plan=build_edition_day_plan(
            EditionDayInput(
                edition_date=date(2026, 7, 21),
                jp_market_open=generate,
                us_previous_session_available=True,
                jp_closure_reason=None if generate else "emergency_halt",
            )
        ),
        collect_sources=True,
        generate_edition=generate,
        email_permitted=generate,
        us_reference_date=date(2026, 7, 20),
        us_last_valid_session_date=date(2026, 7, 20),
        jp_last_valid_session_date=date(2026, 7, 20),
        next_jp_session_date=None,
        reason_code=None if generate else "emergency_halt",
        override_id=None if generate else "override-1",
    )


def test_restart_inside_window_selects_latest_due_attempt() -> None:
    from src.market_morning.calendar.scheduler import build_global_edition_action
    from src.market_morning.calendar.service import publication_window
    from src.market_morning.repositories.jobs import MarketMorningJobType

    now = datetime.fromisoformat("2026-07-21T07:20:00+09:00")
    action = build_global_edition_action(
        evaluation=_evaluation(),
        window=publication_window(now),
        edition_already_complete=False,
    )

    assert action is not None
    assert action.job_type is MarketMorningJobType.GLOBAL_EDITION_RUN
    assert action.idempotency_key == "global-edition-run:2026-07-21:0715"
    assert action.payload["attempt_key"] == "0715"
    assert action.payload["scenario"] == "a_standard"
    assert action.payload["email_permitted"] is True
    assert action.payload["us_last_valid_session_date"] == "2026-07-20"
    assert action.payload["expected_market_sessions"] == {
        "nikkei_225": "2026-07-20",
        "sp_500": "2026-07-20",
        "nasdaq_composite": "2026-07-20",
        "djia": "2026-07-20",
        "usd_jpy": "2026-07-21",
    }
    assert action.payload["day_plan"]["overnight_context"] == "us_session_available"
    assert action.available_at == datetime(2026, 7, 20, 22, 15, tzinfo=timezone.utc)
    assert action.payload["scheduled_at"] == "2026-07-20T22:15:00+00:00"
    assert action.max_attempts == 1


def test_snapshot_actions_start_at_0630_and_are_stable_across_restart_ticks() -> None:
    from src.market_morning.calendar.scheduler import build_market_snapshot_actions
    from src.market_morning.calendar.service import publication_window
    from src.market_morning.market_snapshots import MarketInstrument
    from src.market_morning.repositories.jobs import MarketMorningJobType

    before = build_market_snapshot_actions(
        evaluation=_evaluation(),
        window=publication_window(
            datetime.fromisoformat("2026-07-21T06:29:59+09:00")
        ),
        edition_already_complete=False,
    )
    first = build_market_snapshot_actions(
        evaluation=_evaluation(),
        window=publication_window(
            datetime.fromisoformat("2026-07-21T06:30:00+09:00")
        ),
        edition_already_complete=False,
    )
    restarted = build_market_snapshot_actions(
        evaluation=_evaluation(),
        window=publication_window(
            datetime.fromisoformat("2026-07-21T07:20:00+09:00")
        ),
        edition_already_complete=False,
    )

    assert before == ()
    assert len(first) == len(MarketInstrument) == 5
    assert first == restarted
    assert tuple(action.payload["instrument"] for action in first) == tuple(
        instrument.value for instrument in MarketInstrument
    )
    assert all(
        action.job_type is MarketMorningJobType.MARKET_SNAPSHOT
        for action in first
    )
    assert all(action.priority == 100 for action in first)
    assert all(action.max_attempts == 5 for action in first)
    assert all(
        action.available_at
        == datetime(2026, 7, 20, 21, 30, tzinfo=timezone.utc)
        for action in first
    )
    assert first[0].idempotency_key == (
        "market-snapshot:2026-07-21:nikkei_225"
    )


def test_source_actions_start_at_0600_and_continue_on_jp_market_holidays() -> None:
    from src.market_morning.calendar.scheduler import build_source_ingestion_actions
    from src.market_morning.calendar.service import publication_window
    from src.market_morning.repositories.jobs import MarketMorningJobType

    before = build_source_ingestion_actions(
        evaluation=_evaluation(),
        window=publication_window(
            datetime.fromisoformat("2026-07-21T05:59:59+09:00")
        ),
        source_providers=("tdnet", "edinet"),
    )
    due = build_source_ingestion_actions(
        evaluation=_evaluation(),
        window=publication_window(
            datetime.fromisoformat("2026-07-21T06:00:00+09:00")
        ),
        source_providers=("tdnet", "edinet"),
    )
    holiday = build_source_ingestion_actions(
        evaluation=_evaluation(generate=False),
        window=publication_window(
            datetime.fromisoformat("2026-07-21T07:00:00+09:00")
        ),
        source_providers=("tdnet", "edinet"),
    )

    assert before == ()
    assert due == holiday
    assert tuple(action.payload for action in due) == (
        {"provider": "edinet"},
        {"provider": "tdnet"},
    )
    assert all(
        action.job_type is MarketMorningJobType.SOURCE_INGESTION for action in due
    )
    assert all(action.priority == 120 for action in due)
    assert all(action.max_attempts == 5 for action in due)
    assert all(
        action.available_at
        == datetime(2026, 7, 20, 21, 0, tzinfo=timezone.utc)
        for action in due
    )
    assert due[0].idempotency_key == "source-ingestion:2026-07-21:0600:edinet"


def test_source_provider_contract_rejects_duplicates_and_unsafe_names() -> None:
    from src.market_morning.calendar.scheduler import canonical_source_providers

    assert canonical_source_providers(("tdnet", "edinet")) == (
        "edinet",
        "tdnet",
    )
    with pytest.raises(ValueError, match="unique"):
        canonical_source_providers(("edinet", "EDINET"))
    with pytest.raises(ValueError, match="invalid"):
        canonical_source_providers(("edinet:secret",))


def test_snapshot_actions_are_suppressed_for_closed_or_complete_editions() -> None:
    from src.market_morning.calendar.scheduler import build_market_snapshot_actions
    from src.market_morning.calendar.service import publication_window

    window = publication_window(
        datetime.fromisoformat("2026-07-21T06:30:00+09:00")
    )

    assert (
        build_market_snapshot_actions(
            evaluation=_evaluation(generate=False),
            window=window,
            edition_already_complete=False,
        )
        == ()
    )
    assert (
        build_market_snapshot_actions(
            evaluation=_evaluation(),
            window=window,
            edition_already_complete=True,
        )
        == ()
    )


def test_same_retry_slot_has_stable_payload_across_repeated_ticks() -> None:
    from src.market_morning.calendar.scheduler import build_global_edition_action
    from src.market_morning.calendar.service import publication_window

    first = build_global_edition_action(
        evaluation=_evaluation(),
        window=publication_window(
            datetime.fromisoformat("2026-07-21T07:20:00+09:00")
        ),
        edition_already_complete=False,
    )
    second = build_global_edition_action(
        evaluation=_evaluation(),
        window=publication_window(
            datetime.fromisoformat("2026-07-21T07:29:59+09:00")
        ),
        edition_already_complete=False,
    )

    assert first is not None and second is not None
    assert first.idempotency_key == second.idempotency_key
    assert first.payload == second.payload
    assert first.available_at == second.available_at


def test_late_first_completion_is_site_only_and_prepublication_is_noop() -> None:
    from src.market_morning.calendar.scheduler import build_global_edition_action
    from src.market_morning.calendar.service import publication_window

    late = build_global_edition_action(
        evaluation=_evaluation(),
        window=publication_window(
            datetime.fromisoformat("2026-07-21T08:30:00.000001+09:00")
        ),
        edition_already_complete=False,
    )
    before = build_global_edition_action(
        evaluation=_evaluation(),
        window=publication_window(
            datetime.fromisoformat("2026-07-21T06:59:59+09:00")
        ),
        edition_already_complete=False,
    )

    assert late is not None
    assert late.idempotency_key == "global-edition-run:2026-07-21:late"
    assert late.payload["late"] is True
    assert late.payload["email_permitted"] is False
    assert before is None


def test_closed_or_already_complete_edition_never_enqueues_another_run() -> None:
    from src.market_morning.calendar.scheduler import build_global_edition_action
    from src.market_morning.calendar.service import publication_window

    window = publication_window(
        datetime.fromisoformat("2026-07-21T07:00:00+09:00")
    )

    assert (
        build_global_edition_action(
            evaluation=_evaluation(generate=False),
            window=window,
            edition_already_complete=False,
        )
        is None
    )
    assert (
        build_global_edition_action(
            evaluation=_evaluation(),
            window=window,
            edition_already_complete=True,
        )
        is None
    )


def test_scheduler_tick_refuses_work_when_lease_is_busy(monkeypatch) -> None:
    import src.market_morning.calendar.scheduler as scheduler
    from src.market_morning.repositories.jobs import SchedulerLeaseStatus

    async def busy(*args, **kwargs):
        return SchedulerLeaseStatus.BUSY

    async def must_not_load(*args, **kwargs):
        raise AssertionError("a busy scheduler must not evaluate publication state")

    monkeypatch.setattr(scheduler, "acquire_scheduler_lease", busy)
    monkeypatch.setattr(scheduler, "load_publication_halt", must_not_load)

    result = asyncio.run(
        scheduler.run_scheduler_tick(
            object(),
            owner_id="scheduler-a",
            now=datetime.fromisoformat("2026-07-21T07:00:00+09:00"),
            jp_calendar=object(),
            us_calendar=object(),
            edition_already_complete=False,
        )
    )

    assert result.status is scheduler.SchedulerTickStatus.BUSY
    assert result.action is None
    assert result.enqueued_job is None


def test_repeated_tick_relies_on_durable_job_idempotency(monkeypatch) -> None:
    import src.market_morning.calendar.scheduler as scheduler
    from src.market_morning.repositories.jobs import (
        EnqueuedJob,
        JobEnqueueStatus,
        SchedulerLeaseStatus,
    )

    keys: list[str] = []
    key_counts: dict[str, int] = {}

    async def acquired(*args, **kwargs):
        return SchedulerLeaseStatus.ACQUIRED

    async def no_halt(*args, **kwargs):
        return None

    async def enqueue(*args, **kwargs):
        key = kwargs["idempotency_key"]
        keys.append(key)
        key_counts[key] = key_counts.get(key, 0) + 1
        return EnqueuedJob(
            status=(
                JobEnqueueStatus.ENQUEUED
                if key_counts[key] == 1
                else JobEnqueueStatus.ALREADY_ENQUEUED
            ),
            job_id="11111111-1111-4111-8111-111111111111",
            job_type=kwargs["job_type"],
            idempotency_key=key,
            available_at=kwargs["available_at"],
        )

    monkeypatch.setattr(scheduler, "acquire_scheduler_lease", acquired)
    monkeypatch.setattr(scheduler, "load_publication_halt", no_halt)
    monkeypatch.setattr(scheduler, "enqueue_job", enqueue)
    monkeypatch.setattr(scheduler, "evaluate_market_day", lambda *a, **k: _evaluation())

    inputs = dict(
        owner_id="scheduler-a",
        now=datetime.fromisoformat("2026-07-21T07:20:00+09:00"),
        jp_calendar=object(),
        us_calendar=object(),
        source_providers=("tdnet", "edinet"),
        edition_already_complete=False,
    )
    first = asyncio.run(scheduler.run_scheduler_tick(object(), **inputs))
    second = asyncio.run(scheduler.run_scheduler_tick(object(), **inputs))

    expected_tick_keys = [
        "source-ingestion:2026-07-21:0600:edinet",
        "source-ingestion:2026-07-21:0600:tdnet",
        "market-snapshot:2026-07-21:nikkei_225",
        "market-snapshot:2026-07-21:sp_500",
        "market-snapshot:2026-07-21:nasdaq_composite",
        "market-snapshot:2026-07-21:djia",
        "market-snapshot:2026-07-21:usd_jpy",
        "global-edition-run:2026-07-21:0715",
    ]
    assert keys == expected_tick_keys + expected_tick_keys
    assert first.status is scheduler.SchedulerTickStatus.ENQUEUED
    assert second.status is scheduler.SchedulerTickStatus.ALREADY_ENQUEUED


def test_manual_halt_is_loaded_before_calendar_decision_and_blocks_enqueue(
    monkeypatch,
) -> None:
    import src.market_morning.calendar.scheduler as scheduler
    from src.market_morning.calendar.service import PublicationHaltOverride
    from src.market_morning.repositories.jobs import SchedulerLeaseStatus

    halt = PublicationHaltOverride(
        override_id="override-1",
        edition_date=date(2026, 7, 21),
        reason_code="emergency_halt",
    )

    async def acquired(*args, **kwargs):
        return SchedulerLeaseStatus.ACQUIRED

    async def load(*args, **kwargs):
        return halt

    def evaluate(*args, **kwargs):
        assert kwargs["publication_override"] is halt
        return _evaluation(generate=False)

    async def must_not_enqueue(*args, **kwargs):
        raise AssertionError("manual halt must block global edition enqueue")

    monkeypatch.setattr(scheduler, "acquire_scheduler_lease", acquired)
    monkeypatch.setattr(scheduler, "load_publication_halt", load)
    monkeypatch.setattr(scheduler, "evaluate_market_day", evaluate)
    monkeypatch.setattr(scheduler, "enqueue_job", must_not_enqueue)

    result = asyncio.run(
        scheduler.run_scheduler_tick(
            object(),
            owner_id="scheduler-a",
            now=datetime.fromisoformat("2026-07-21T07:00:00+09:00"),
            jp_calendar=object(),
            us_calendar=object(),
            edition_already_complete=False,
        )
    )

    assert result.status is scheduler.SchedulerTickStatus.NO_ACTION
    assert result.evaluation is not None
    assert result.evaluation.reason_code == "emergency_halt"


def test_scheduler_reads_durable_current_success_when_caller_does_not_override(
    monkeypatch,
) -> None:
    import src.market_morning.calendar.scheduler as scheduler
    from src.market_morning.repositories.jobs import SchedulerLeaseStatus

    async def acquired(*args, **kwargs):
        return SchedulerLeaseStatus.ACQUIRED

    async def no_halt(*args, **kwargs):
        return None

    async def already_complete(session, *, edition_date):
        assert edition_date == date(2026, 7, 21)
        return True

    async def must_not_enqueue(*args, **kwargs):
        raise AssertionError("current successful run must stop retry enqueue")

    monkeypatch.setattr(scheduler, "acquire_scheduler_lease", acquired)
    monkeypatch.setattr(scheduler, "load_publication_halt", no_halt)
    monkeypatch.setattr(scheduler, "has_current_global_edition", already_complete)
    monkeypatch.setattr(scheduler, "evaluate_market_day", lambda *a, **k: _evaluation())
    monkeypatch.setattr(scheduler, "enqueue_job", must_not_enqueue)

    result = asyncio.run(
        scheduler.run_scheduler_tick(
            object(),
            owner_id="scheduler-a",
            now=datetime.fromisoformat("2026-07-21T07:30:00+09:00"),
            jp_calendar=object(),
            us_calendar=object(),
        )
    )

    assert result.status is scheduler.SchedulerTickStatus.NO_ACTION
