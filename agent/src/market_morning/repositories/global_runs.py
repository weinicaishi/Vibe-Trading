"""MySQL statements for the serialized global edition aggregate."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from enum import StrEnum
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.market_morning.global_runs import (
    GlobalEditionRunSpec,
    GlobalEditionSnapshotItem,
    build_global_run_manifest,
    encode_global_run_spec,
    global_run_manifest_sha256,
    global_run_spec_sha256,
    scheduled_run_version,
)
from src.market_morning.market_snapshots import MarketInstrument
from src.market_morning.models import (
    GlobalEditionDayRecord,
    GlobalEditionItemRecord,
    GlobalEditionRunRecord,
    new_id,
)


class GlobalRunConflict(RuntimeError):
    pass


class GlobalRunStartStatus(StrEnum):
    STARTED = "started"
    ALREADY_STARTED = "already_started"


class GlobalRunPublishStatus(StrEnum):
    PUBLISHED = "published"
    ALREADY_PUBLISHED = "already_published"


class GlobalRunTerminalStatus(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    LATE = "late"


@dataclass(frozen=True, slots=True)
class GlobalRunStartResult:
    status: GlobalRunStartStatus
    run_id: str
    run_version: int


@dataclass(frozen=True, slots=True)
class GlobalRunPublishResult:
    status: GlobalRunPublishStatus
    run_id: str
    run_version: int
    publication_status: GlobalRunTerminalStatus


def _mysql_utc(value: datetime, *, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _uuid(value: str) -> str:
    try:
        return str(UUID(value))
    except (ValueError, AttributeError) as error:
        raise ValueError("run_id must be a UUID") from error


def build_global_day_insert_statement(*, edition_date: date, now: datetime):
    current = _mysql_utc(now, field_name="now")
    statement = mysql_insert(GlobalEditionDayRecord.__table__).values(
        edition_date=edition_date,
        created_at=current,
        updated_at=current,
    )
    return statement.on_duplicate_key_update(
        edition_date=GlobalEditionDayRecord.__table__.c.edition_date
    )


def build_global_run_insert_statement(*, run_id: str, spec: GlobalEditionRunSpec):
    started = _mysql_utc(spec.started_at, field_name="spec.started_at")
    statement = mysql_insert(GlobalEditionRunRecord.__table__).values(
        run_id=_uuid(run_id),
        edition_date=spec.edition_date,
        run_version=scheduled_run_version(spec.attempt_key),
        generation_key=spec.generation_key,
        attempt_key=spec.attempt_key,
        scenario=spec.scenario.value,
        status="draft",
        is_current=False,
        email_permitted=spec.email_permitted,
        late=spec.late,
        reason_code=spec.reason_code,
        spec=encode_global_run_spec(spec),
        spec_sha256=global_run_spec_sha256(spec),
        manifest=None,
        manifest_sha256=None,
        started_at=started,
        completed_at=None,
        created_at=started,
        updated_at=started,
    )
    return statement.on_duplicate_key_update(
        run_id=GlobalEditionRunRecord.__table__.c.run_id
    )


def _day_lock_statement(edition_date: date):
    return (
        select(GlobalEditionDayRecord)
        .where(GlobalEditionDayRecord.edition_date == edition_date)
        .with_for_update()
    )


def _run_by_generation_statement(generation_key: str):
    return (
        select(GlobalEditionRunRecord)
        .where(GlobalEditionRunRecord.generation_key == generation_key)
        .with_for_update()
    )


async def start_global_edition_run(
    session: AsyncSession,
    *,
    spec: GlobalEditionRunSpec,
) -> GlobalRunStartResult:
    candidate_id = new_id()
    current = _mysql_utc(spec.started_at, field_name="spec.started_at")
    await session.execute(
        build_global_day_insert_statement(
            edition_date=spec.edition_date,
            now=spec.started_at,
        )
    )
    day = (await session.execute(_day_lock_statement(spec.edition_date))).scalar_one_or_none()
    if day is None:
        raise GlobalRunConflict("global edition day lock could not be loaded")
    day.updated_at = current
    await session.execute(
        build_global_run_insert_statement(run_id=candidate_id, spec=spec)
    )
    record = (
        await session.execute(_run_by_generation_statement(spec.generation_key))
    ).scalar_one_or_none()
    if record is None:
        raise GlobalRunConflict("global edition run could not be reloaded")
    if record.spec_sha256 != global_run_spec_sha256(spec):
        raise GlobalRunConflict(
            "generation key already belongs to a different spec"
        )
    if record.status == "draft":
        record.status = "running"
        record.updated_at = current
    elif record.status not in {
        "running",
        "complete",
        "partial",
        "failed",
        "late",
    }:
        raise GlobalRunConflict("global edition run has an invalid status")
    await session.flush()
    return GlobalRunStartResult(
        status=(
            GlobalRunStartStatus.STARTED
            if record.run_id == candidate_id
            else GlobalRunStartStatus.ALREADY_STARTED
        ),
        run_id=record.run_id,
        run_version=record.run_version,
    )


def _validated_items(
    items: tuple[GlobalEditionSnapshotItem, ...],
) -> tuple[GlobalEditionSnapshotItem, ...]:
    grouped = {item.instrument: item for item in items}
    if len(items) != len(MarketInstrument) or set(grouped) != set(MarketInstrument):
        raise ValueError("snapshot_items must contain exactly one required instrument")
    positions = {instrument: index for index, instrument in enumerate(MarketInstrument)}
    return tuple(sorted(items, key=lambda item: positions[item.instrument]))


async def publish_global_edition_run(
    session: AsyncSession,
    *,
    run_id: str,
    spec: GlobalEditionRunSpec,
    snapshot_items: tuple[GlobalEditionSnapshotItem, ...],
    publication_status: GlobalRunTerminalStatus | str,
    completed_at: datetime,
) -> GlobalRunPublishResult:
    canonical_run_id = _uuid(run_id)
    terminal = GlobalRunTerminalStatus(publication_status)
    if spec.late != (terminal is GlobalRunTerminalStatus.LATE):
        raise ValueError("late spec and publication status must agree")
    ordered_items = _validated_items(snapshot_items)
    completed = _mysql_utc(completed_at, field_name="completed_at")
    manifest = build_global_run_manifest(spec, snapshot_items=ordered_items)
    manifest_hash = global_run_manifest_sha256(manifest)

    day = (await session.execute(_day_lock_statement(spec.edition_date))).scalar_one_or_none()
    if day is None:
        raise GlobalRunConflict("global edition day lock could not be loaded")
    run = (
        await session.execute(
            select(GlobalEditionRunRecord)
            .where(GlobalEditionRunRecord.run_id == canonical_run_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if run is None or run.spec_sha256 != global_run_spec_sha256(spec):
        raise GlobalRunConflict("global edition run does not match the spec")
    if run.status in {status.value for status in GlobalRunTerminalStatus}:
        if run.status != terminal.value or run.manifest_sha256 != manifest_hash:
            raise GlobalRunConflict("terminal global edition run cannot be rewritten")
        return GlobalRunPublishResult(
            status=GlobalRunPublishStatus.ALREADY_PUBLISHED,
            run_id=run.run_id,
            run_version=run.run_version,
            publication_status=terminal,
        )
    if run.status != "running":
        raise GlobalRunConflict("global edition run is not publishable")
    previous = (
        await session.execute(
            select(GlobalEditionRunRecord)
            .where(
                GlobalEditionRunRecord.edition_date == spec.edition_date,
                GlobalEditionRunRecord.is_current.is_(True),
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if previous is not None and previous.run_id != run.run_id:
        previous.is_current = False
        previous.updated_at = completed
        # MySQL evaluates the generated-column unique constraint per row
        # update. Flush the demotion before promoting the new run so the
        # database never observes two current successes, even when the ORM
        # would otherwise order same-table updates by primary key.
        await session.flush()
    for position, item in enumerate(ordered_items):
        session.add(
            GlobalEditionItemRecord(
                item_id=new_id(),
                run_id=run.run_id,
                position=position,
                item_type="market_snapshot",
                snapshot_id=item.snapshot_id,
                instrument=item.instrument.value,
                created_at=completed,
            )
        )
    run.status = terminal.value
    run.is_current = True
    run.manifest = manifest
    run.manifest_sha256 = manifest_hash
    run.completed_at = completed
    run.updated_at = completed
    day.updated_at = completed
    await session.flush()
    return GlobalRunPublishResult(
        status=GlobalRunPublishStatus.PUBLISHED,
        run_id=run.run_id,
        run_version=run.run_version,
        publication_status=terminal,
    )


async def fail_global_edition_run(
    session: AsyncSession,
    *,
    run_id: str,
    spec: GlobalEditionRunSpec,
    reason_code: str,
    completed_at: datetime,
) -> None:
    canonical_run_id = _uuid(run_id)
    canonical_reason = reason_code.strip()
    if not canonical_reason or len(canonical_reason) > 64:
        raise ValueError("reason_code must contain 1 to 64 characters")
    completed = _mysql_utc(completed_at, field_name="completed_at")
    day = (await session.execute(_day_lock_statement(spec.edition_date))).scalar_one_or_none()
    if day is None:
        raise GlobalRunConflict("global edition day lock could not be loaded")
    run = (
        await session.execute(
            select(GlobalEditionRunRecord)
            .where(GlobalEditionRunRecord.run_id == canonical_run_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if run is None or run.spec_sha256 != global_run_spec_sha256(spec):
        raise GlobalRunConflict("global edition run does not match the spec")
    if run.status == "failed":
        if run.reason_code != canonical_reason:
            raise GlobalRunConflict("failed global edition run cannot be rewritten")
        return
    if run.status != "running":
        raise GlobalRunConflict("successful global edition run cannot be failed")
    run.status = "failed"
    run.is_current = False
    run.reason_code = canonical_reason
    run.completed_at = completed
    run.updated_at = completed
    day.updated_at = completed
    await session.flush()


async def has_current_global_edition(
    session: AsyncSession,
    *,
    edition_date: date,
) -> bool:
    run_id = (
        await session.execute(
            select(GlobalEditionRunRecord.run_id)
            .where(
                GlobalEditionRunRecord.edition_date == edition_date,
                GlobalEditionRunRecord.is_current.is_(True),
                GlobalEditionRunRecord.status.in_(
                    tuple(status.value for status in GlobalRunTerminalStatus)
                ),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    return run_id is not None


__all__ = [
    "GlobalEditionSnapshotItem",
    "GlobalRunConflict",
    "GlobalRunPublishResult",
    "GlobalRunPublishStatus",
    "GlobalRunStartResult",
    "GlobalRunStartStatus",
    "GlobalRunTerminalStatus",
    "build_global_day_insert_statement",
    "build_global_run_insert_statement",
    "fail_global_edition_run",
    "has_current_global_edition",
    "publish_global_edition_run",
    "start_global_edition_run",
]
