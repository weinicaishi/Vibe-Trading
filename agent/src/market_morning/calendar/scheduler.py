"""Single-active scheduling policy for global Market Morning edition runs."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from enum import StrEnum
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from src.market_morning.calendar.service import (
    MarketDayEvaluation,
    JST,
    PublicationPhase,
    PublicationWindow,
    TradingCalendar,
    evaluate_market_day,
    publication_window,
)
from src.market_morning.market_snapshots import MarketInstrument
from src.market_morning.repositories.jobs import (
    EnqueuedJob,
    JobEnqueueStatus,
    MarketMorningJobType,
    SchedulerLeaseStatus,
    acquire_scheduler_lease,
    enqueue_job,
)
from src.market_morning.repositories.global_runs import has_current_global_edition
from src.market_morning.repositories.manual_overrides import load_publication_halt

SCHEDULER_LEASE_NAME = "market-morning-global-edition"
SOURCE_COLLECTION_TIME = time(6, 0)
_SOURCE_PROVIDER_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")


class SchedulerTickStatus(StrEnum):
    BUSY = "busy"
    NO_ACTION = "no_action"
    ENQUEUED = "enqueued"
    ALREADY_ENQUEUED = "already_enqueued"


@dataclass(frozen=True, slots=True)
class GlobalEditionAction:
    job_type: MarketMorningJobType
    idempotency_key: str
    payload: dict[str, Any]
    priority: int
    available_at: datetime
    max_attempts: int


@dataclass(frozen=True, slots=True)
class MarketSnapshotAction:
    job_type: MarketMorningJobType
    idempotency_key: str
    payload: dict[str, Any]
    priority: int
    available_at: datetime
    max_attempts: int


@dataclass(frozen=True, slots=True)
class SourceIngestionAction:
    job_type: MarketMorningJobType
    idempotency_key: str
    payload: dict[str, Any]
    priority: int
    available_at: datetime
    max_attempts: int


@dataclass(frozen=True, slots=True)
class SchedulerTickResult:
    status: SchedulerTickStatus
    lease_status: SchedulerLeaseStatus
    evaluation: MarketDayEvaluation | None
    source_actions: tuple[SourceIngestionAction, ...]
    snapshot_actions: tuple[MarketSnapshotAction, ...]
    action: GlobalEditionAction | None
    enqueued_source_jobs: tuple[EnqueuedJob, ...]
    enqueued_snapshot_jobs: tuple[EnqueuedJob, ...]
    enqueued_job: EnqueuedJob | None


def _day_plan_payload(evaluation: MarketDayEvaluation) -> dict[str, Any]:
    plan = evaluation.day_plan
    return {
        "edition_date": plan.edition_date.isoformat(),
        "generate": plan.generate,
        "status": plan.status.value,
        "overnight_context": plan.overnight_context.value,
        "reason_code": plan.reason_code,
        "us_reason_code": plan.us_reason_code,
    }


def build_global_edition_action(
    *,
    evaluation: MarketDayEvaluation,
    window: PublicationWindow,
    edition_already_complete: bool,
) -> GlobalEditionAction | None:
    """Return the one durable action due for this tick, or a safe no-op."""

    if evaluation.edition_date != window.edition_date:
        raise ValueError("evaluation and publication window dates must match")
    if (
        edition_already_complete
        or not evaluation.generate_edition
        or window.phase not in {PublicationPhase.PUBLISH, PublicationPhase.LATE}
        or window.attempt_key is None
    ):
        return None
    email_permitted = evaluation.email_permitted and window.email_permitted
    attempt_key = window.attempt_key
    if window.attempt_at is None:
        raise ValueError("publishable window must have a stable attempt time")
    scheduled_at = window.attempt_at.astimezone(timezone.utc)
    payload = {
        "schema_version": 1,
        "edition_date_jst": evaluation.edition_date.isoformat(),
        "attempt_key": attempt_key,
        "scheduled_at": scheduled_at.isoformat(),
        "scenario": evaluation.scenario.value,
        "email_permitted": email_permitted,
        "late": window.late,
        "us_reference_date": evaluation.us_reference_date.isoformat(),
        "us_last_valid_session_date": (
            evaluation.us_last_valid_session_date.isoformat()
            if evaluation.us_last_valid_session_date is not None
            else None
        ),
        "next_jp_session_date": (
            evaluation.next_jp_session_date.isoformat()
            if evaluation.next_jp_session_date is not None
            else None
        ),
        "reason_code": evaluation.reason_code,
        "expected_market_sessions": {
            MarketInstrument.NIKKEI_225.value: (
                evaluation.jp_last_valid_session_date.isoformat()
                if evaluation.jp_last_valid_session_date is not None
                else None
            ),
            MarketInstrument.SP_500.value: (
                evaluation.us_last_valid_session_date.isoformat()
                if evaluation.us_last_valid_session_date is not None
                else None
            ),
            MarketInstrument.NASDAQ_COMPOSITE.value: (
                evaluation.us_last_valid_session_date.isoformat()
                if evaluation.us_last_valid_session_date is not None
                else None
            ),
            MarketInstrument.DJIA.value: (
                evaluation.us_last_valid_session_date.isoformat()
                if evaluation.us_last_valid_session_date is not None
                else None
            ),
            MarketInstrument.USD_JPY.value: evaluation.edition_date.isoformat(),
        },
        "day_plan": _day_plan_payload(evaluation),
    }
    return GlobalEditionAction(
        job_type=MarketMorningJobType.GLOBAL_EDITION_RUN,
        idempotency_key=(
            f"global-edition-run:{evaluation.edition_date.isoformat()}:{attempt_key}"
        ),
        payload=payload,
        priority=50,
        available_at=scheduled_at,
        # Calendar retry slots are explicit durable jobs. A failed slot must not
        # create overlapping hidden retries before the next scheduled decision.
        max_attempts=1,
    )


def build_market_snapshot_actions(
    *,
    evaluation: MarketDayEvaluation,
    window: PublicationWindow,
    edition_already_complete: bool,
) -> tuple[MarketSnapshotAction, ...]:
    """Return the five stable 06:30 snapshot jobs needed by the publish Gate."""

    if evaluation.edition_date != window.edition_date:
        raise ValueError("evaluation and publication window dates must match")
    if (
        edition_already_complete
        or not evaluation.generate_edition
        or window.phase is PublicationPhase.COLLECTING
    ):
        return ()
    scheduled_at = datetime.combine(
        evaluation.edition_date,
        time(6, 30),
        tzinfo=JST,
    ).astimezone(timezone.utc)
    return tuple(
        MarketSnapshotAction(
            job_type=MarketMorningJobType.MARKET_SNAPSHOT,
            idempotency_key=(
                f"market-snapshot:{evaluation.edition_date.isoformat()}:{instrument.value}"
            ),
            payload={
                "schema_version": 1,
                "instrument": instrument.value,
                "scheduled_at": scheduled_at.isoformat(),
            },
            priority=100,
            available_at=scheduled_at,
            max_attempts=5,
        )
        for instrument in MarketInstrument
    )


def canonical_source_providers(values: tuple[str, ...]) -> tuple[str, ...]:
    if not isinstance(values, tuple):
        raise ValueError("source_providers must be a tuple")
    normalized: list[str] = []
    for value in values:
        if not isinstance(value, str):
            raise ValueError("source provider is invalid")
        provider = value.strip().lower()
        if _SOURCE_PROVIDER_PATTERN.fullmatch(provider) is None:
            raise ValueError("source provider is invalid")
        normalized.append(provider)
    if len(set(normalized)) != len(normalized):
        raise ValueError("source providers must be unique")
    return tuple(sorted(normalized))


def build_source_ingestion_actions(
    *,
    evaluation: MarketDayEvaluation,
    window: PublicationWindow,
    source_providers: tuple[str, ...],
) -> tuple[SourceIngestionAction, ...]:
    """Schedule one recoverable, daily 06:00 collection per licensed source."""

    if evaluation.edition_date != window.edition_date:
        raise ValueError("evaluation and publication window dates must match")
    providers = canonical_source_providers(source_providers)
    scheduled_at = datetime.combine(
        evaluation.edition_date,
        SOURCE_COLLECTION_TIME,
        tzinfo=JST,
    )
    if not evaluation.collect_sources or window.local_time < scheduled_at:
        return ()
    available_at = scheduled_at.astimezone(timezone.utc)
    return tuple(
        SourceIngestionAction(
            job_type=MarketMorningJobType.SOURCE_INGESTION,
            idempotency_key=(
                f"source-ingestion:{evaluation.edition_date.isoformat()}:0600:{provider}"
            ),
            payload={"provider": provider},
            priority=120,
            available_at=available_at,
            max_attempts=5,
        )
        for provider in providers
    )


async def run_scheduler_tick(
    session: AsyncSession,
    *,
    owner_id: str,
    now: datetime,
    jp_calendar: TradingCalendar,
    us_calendar: TradingCalendar,
    source_providers: tuple[str, ...] = (),
    edition_already_complete: bool | None = None,
    lease_duration: timedelta = timedelta(minutes=1),
) -> SchedulerTickResult:
    """Acquire the DB lease, evaluate policy, then idempotently enqueue one run.

    The caller owns the transaction boundary. Production wiring must invoke this
    in a short transaction and must derive ``edition_already_complete`` from the
    durable global-edition aggregate before enabling the process entrypoint.
    """

    window = publication_window(now)
    lease_status = await acquire_scheduler_lease(
        session,
        lease_name=SCHEDULER_LEASE_NAME,
        owner_id=owner_id,
        now=now,
        lease_duration=lease_duration,
    )
    if lease_status is SchedulerLeaseStatus.BUSY:
        return SchedulerTickResult(
            status=SchedulerTickStatus.BUSY,
            lease_status=lease_status,
            evaluation=None,
            source_actions=(),
            snapshot_actions=(),
            action=None,
            enqueued_source_jobs=(),
            enqueued_snapshot_jobs=(),
            enqueued_job=None,
        )
    override = await load_publication_halt(
        session,
        edition_date=window.edition_date,
    )
    evaluation = evaluate_market_day(
        window.edition_date,
        jp_calendar=jp_calendar,
        us_calendar=us_calendar,
        publication_override=override,
    )
    if edition_already_complete is None:
        edition_already_complete = await has_current_global_edition(
            session,
            edition_date=window.edition_date,
        )
    source_actions = build_source_ingestion_actions(
        evaluation=evaluation,
        window=window,
        source_providers=source_providers,
    )
    action = build_global_edition_action(
        evaluation=evaluation,
        window=window,
        edition_already_complete=edition_already_complete,
    )
    snapshot_actions = build_market_snapshot_actions(
        evaluation=evaluation,
        window=window,
        edition_already_complete=edition_already_complete,
    )
    if action is None and not snapshot_actions and not source_actions:
        return SchedulerTickResult(
            status=SchedulerTickStatus.NO_ACTION,
            lease_status=lease_status,
            evaluation=evaluation,
            source_actions=(),
            snapshot_actions=(),
            action=None,
            enqueued_source_jobs=(),
            enqueued_snapshot_jobs=(),
            enqueued_job=None,
        )
    enqueued_source_list: list[EnqueuedJob] = []
    for source_action in source_actions:
        enqueued_source_list.append(
            await enqueue_job(
                session,
                job_type=source_action.job_type,
                idempotency_key=source_action.idempotency_key,
                payload=source_action.payload,
                priority=source_action.priority,
                available_at=source_action.available_at,
                max_attempts=source_action.max_attempts,
                created_at=now,
            )
        )
    enqueued_sources = tuple(enqueued_source_list)
    enqueued_snapshot_list: list[EnqueuedJob] = []
    for snapshot_action in snapshot_actions:
        enqueued_snapshot_list.append(
            await enqueue_job(
                session,
                job_type=snapshot_action.job_type,
                idempotency_key=snapshot_action.idempotency_key,
                payload=snapshot_action.payload,
                priority=snapshot_action.priority,
                available_at=snapshot_action.available_at,
                max_attempts=snapshot_action.max_attempts,
                created_at=now,
            )
        )
    enqueued_snapshots = tuple(enqueued_snapshot_list)
    enqueued = (
        await enqueue_job(
            session,
            job_type=action.job_type,
            idempotency_key=action.idempotency_key,
            payload=action.payload,
            priority=action.priority,
            available_at=action.available_at,
            max_attempts=action.max_attempts,
            created_at=now,
        )
        if action is not None
        else None
    )
    all_enqueued = (
        enqueued_sources + enqueued_snapshots + ((enqueued,) if enqueued else ())
    )
    status = (
        SchedulerTickStatus.ENQUEUED
        if any(
            item.status is JobEnqueueStatus.ENQUEUED for item in all_enqueued
        )
        else SchedulerTickStatus.ALREADY_ENQUEUED
    )
    return SchedulerTickResult(
        status=status,
        lease_status=lease_status,
        evaluation=evaluation,
        source_actions=source_actions,
        snapshot_actions=snapshot_actions,
        action=action,
        enqueued_source_jobs=enqueued_sources,
        enqueued_snapshot_jobs=enqueued_snapshots,
        enqueued_job=enqueued,
    )


__all__ = [
    "GlobalEditionAction",
    "MarketSnapshotAction",
    "SCHEDULER_LEASE_NAME",
    "SOURCE_COLLECTION_TIME",
    "SchedulerTickResult",
    "SchedulerTickStatus",
    "SourceIngestionAction",
    "build_global_edition_action",
    "build_market_snapshot_actions",
    "build_source_ingestion_actions",
    "canonical_source_providers",
    "run_scheduler_tick",
]
