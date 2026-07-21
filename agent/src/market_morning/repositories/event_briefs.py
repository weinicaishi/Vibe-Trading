"""MySQL state transitions for versioned EventBrief generation."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.market_morning.event_brief_generation import (
    EventBriefGenerationSpec,
    encode_event_brief_generation_spec,
    event_brief_generation_spec_sha256,
)
from src.market_morning.event_briefs import (
    EventBrief,
    EventBriefPublishStatus,
    encode_event_brief,
)
from src.market_morning.models import (
    EventBriefRecord,
    EventBriefSourceLinkRecord,
    new_id,
)


class EventBriefPersistenceConflict(RuntimeError):
    pass


class EventBriefStartStatus(StrEnum):
    STARTED = "started"
    RETRIED = "retried"
    ALREADY_RUNNING = "already_running"
    ALREADY_TERMINAL = "already_terminal"


class EventBriefPublishResultStatus(StrEnum):
    PUBLISHED = "published"
    ALREADY_PUBLISHED = "already_published"


@dataclass(frozen=True, slots=True)
class EventBriefStartResult:
    status: EventBriefStartStatus
    brief_id: str
    attempt_number: int
    retry_permitted: bool
    can_execute: bool
    terminal_status: EventBriefPublishStatus | None = None
    payload_sha256: str | None = None


@dataclass(frozen=True, slots=True)
class EventBriefPublishResult:
    write_status: EventBriefPublishResultStatus
    brief_id: str
    status: EventBriefPublishStatus
    payload_sha256: str


@dataclass(frozen=True, slots=True)
class EventBriefFailureResult:
    brief_id: str
    attempt_number: int
    retry_permitted: bool
    error_code: str


def _uuid(value: str, *, field_name: str) -> str:
    try:
        return str(UUID(value))
    except (ValueError, AttributeError) as error:
        raise ValueError(f"{field_name} must be a UUID") from error


def _mysql_utc(value: datetime, *, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _error_code(value: str) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > 64:
        raise ValueError("error_code must contain 1 to 64 characters")
    return normalized


def event_brief_payload_sha256(payload: dict) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def build_event_brief_insert_statement(
    *,
    brief_id: str,
    spec: EventBriefGenerationSpec,
):
    started = _mysql_utc(spec.started_at, field_name="spec.started_at")
    statement = mysql_insert(EventBriefRecord.__table__).values(
        brief_id=_uuid(brief_id, field_name="brief_id"),
        event_id=spec.event_id,
        event_version=spec.event_version,
        generation_key=spec.generation_key,
        schema_version=spec.schema_version,
        model_version=spec.model_version,
        prompt_version=spec.prompt_version,
        status="draft",
        review_status="pending",
        attempt_count=0,
        max_attempts=spec.max_attempts,
        spec=encode_event_brief_generation_spec(spec),
        spec_sha256=event_brief_generation_spec_sha256(spec),
        payload=None,
        payload_sha256=None,
        source_ids=[],
        validation_errors=[],
        primary_source_record_id=None,
        last_failure_code=None,
        last_failed_at=None,
        started_at=started,
        completed_at=None,
        published_at=None,
        created_at=started,
        updated_at=started,
    )
    return statement.on_duplicate_key_update(
        brief_id=EventBriefRecord.__table__.c.brief_id
    )


def _generation_lock_statement(generation_key: str):
    return (
        select(EventBriefRecord)
        .where(EventBriefRecord.generation_key == generation_key)
        .with_for_update()
    )


def _brief_lock_statement(brief_id: str):
    return (
        select(EventBriefRecord)
        .where(EventBriefRecord.brief_id == brief_id)
        .with_for_update()
    )


def _verify_record(record, *, spec: EventBriefGenerationSpec) -> None:
    if record is None:
        raise EventBriefPersistenceConflict("event brief record could not be loaded")
    if record.spec_sha256 != event_brief_generation_spec_sha256(spec):
        raise EventBriefPersistenceConflict(
            "generation key already belongs to a different spec"
        )


async def start_event_brief_generation(
    session: AsyncSession,
    *,
    spec: EventBriefGenerationSpec,
) -> EventBriefStartResult:
    candidate_id = new_id()
    current = _mysql_utc(spec.started_at, field_name="spec.started_at")
    await session.execute(
        build_event_brief_insert_statement(brief_id=candidate_id, spec=spec)
    )
    record = (
        await session.execute(_generation_lock_statement(spec.generation_key))
    ).scalar_one_or_none()
    _verify_record(record, spec=spec)

    if record.status in {
        EventBriefPublishStatus.PUBLISHED.value,
        EventBriefPublishStatus.DEGRADED.value,
        EventBriefPublishStatus.BLOCKED.value,
    }:
        if not isinstance(record.payload_sha256, str) or len(record.payload_sha256) != 64:
            raise EventBriefPersistenceConflict(
                "terminal event brief is missing its payload hash"
            )
        return EventBriefStartResult(
            status=EventBriefStartStatus.ALREADY_TERMINAL,
            brief_id=record.brief_id,
            attempt_number=record.attempt_count,
            retry_permitted=False,
            can_execute=False,
            terminal_status=EventBriefPublishStatus(record.status),
            payload_sha256=record.payload_sha256,
        )
    if record.status == "running":
        return EventBriefStartResult(
            status=EventBriefStartStatus.ALREADY_RUNNING,
            brief_id=record.brief_id,
            attempt_number=record.attempt_count,
            retry_permitted=record.attempt_count < record.max_attempts,
            can_execute=True,
        )
    if record.status not in {"draft", "failed"}:
        raise EventBriefPersistenceConflict("event brief has an invalid status")
    if record.attempt_count >= record.max_attempts:
        return EventBriefStartResult(
            status=EventBriefStartStatus.ALREADY_TERMINAL,
            brief_id=record.brief_id,
            attempt_number=record.attempt_count,
            retry_permitted=False,
            can_execute=False,
        )

    was_retry = record.status == "failed"
    record.status = "running"
    record.attempt_count += 1
    record.started_at = current
    record.completed_at = None
    record.updated_at = current
    await session.flush()
    return EventBriefStartResult(
        status=(
            EventBriefStartStatus.RETRIED
            if was_retry
            else EventBriefStartStatus.STARTED
        ),
        brief_id=record.brief_id,
        attempt_number=record.attempt_count,
        retry_permitted=record.attempt_count < record.max_attempts,
        can_execute=True,
    )


def _validated_source_ids(brief: EventBrief) -> tuple[str, ...]:
    if brief.status not in {
        EventBriefPublishStatus.PUBLISHED,
        EventBriefPublishStatus.DEGRADED,
        EventBriefPublishStatus.BLOCKED,
    }:
        raise ValueError("event brief has an unsupported terminal status")
    source_ids = tuple(brief.source_ids)
    if len(set(source_ids)) != len(source_ids):
        raise ValueError("event brief must have unique source_ids")
    if brief.status is not EventBriefPublishStatus.BLOCKED and not source_ids:
        raise ValueError("publishable brief must have source_ids")
    if tuple(source.source_id for source in brief.sources) != source_ids:
        raise ValueError("brief source manifest does not match source_ids")
    return source_ids


async def publish_event_brief(
    session: AsyncSession,
    *,
    brief_id: str,
    spec: EventBriefGenerationSpec,
    brief: EventBrief,
    completed_at: datetime,
) -> EventBriefPublishResult:
    canonical_id = _uuid(brief_id, field_name="brief_id")
    if brief.event_id != spec.event_id or brief.event_version != spec.event_version:
        raise ValueError("brief identity does not match generation spec")
    if brief.model_version not in {None, spec.model_version}:
        raise ValueError("brief model_version does not match generation spec")
    source_ids = _validated_source_ids(brief)
    payload = encode_event_brief(brief)
    payload_hash = event_brief_payload_sha256(payload)
    completed = _mysql_utc(completed_at, field_name="completed_at")

    record = (
        await session.execute(_brief_lock_statement(canonical_id))
    ).scalar_one_or_none()
    _verify_record(record, spec=spec)
    if record.status in {
        EventBriefPublishStatus.PUBLISHED.value,
        EventBriefPublishStatus.DEGRADED.value,
        EventBriefPublishStatus.BLOCKED.value,
    }:
        if record.status != brief.status.value or record.payload_sha256 != payload_hash:
            raise EventBriefPersistenceConflict(
                "terminal event brief cannot be rewritten"
            )
        return EventBriefPublishResult(
            write_status=EventBriefPublishResultStatus.ALREADY_PUBLISHED,
            brief_id=record.brief_id,
            status=brief.status,
            payload_sha256=payload_hash,
        )
    if record.status != "running":
        raise EventBriefPersistenceConflict("event brief is not publishable")

    for position, source_id in enumerate(source_ids):
        session.add(
            EventBriefSourceLinkRecord(
                brief_source_id=new_id(),
                brief_id=record.brief_id,
                source_record_id=source_id,
                position=position,
                created_at=completed,
            )
        )
    record.status = brief.status.value
    record.review_status = brief.review_status.value
    record.payload = payload
    record.payload_sha256 = payload_hash
    record.source_ids = list(source_ids)
    record.validation_errors = list(brief.error_codes)
    record.primary_source_record_id = source_ids[0] if source_ids else None
    if brief.status is EventBriefPublishStatus.DEGRADED and brief.error_codes:
        record.last_failure_code = brief.error_codes[0]
        record.last_failed_at = completed
    record.completed_at = completed
    record.published_at = (
        completed
        if brief.status
        in {
            EventBriefPublishStatus.PUBLISHED,
            EventBriefPublishStatus.DEGRADED,
        }
        else None
    )
    record.updated_at = completed
    await session.flush()
    return EventBriefPublishResult(
        write_status=EventBriefPublishResultStatus.PUBLISHED,
        brief_id=record.brief_id,
        status=brief.status,
        payload_sha256=payload_hash,
    )


async def fail_event_brief_generation(
    session: AsyncSession,
    *,
    brief_id: str,
    spec: EventBriefGenerationSpec,
    error_code: str,
    failed_at: datetime,
) -> EventBriefFailureResult:
    canonical_id = _uuid(brief_id, field_name="brief_id")
    canonical_error = _error_code(error_code)
    failed = _mysql_utc(failed_at, field_name="failed_at")
    record = (
        await session.execute(_brief_lock_statement(canonical_id))
    ).scalar_one_or_none()
    _verify_record(record, spec=spec)
    if record.status == "failed":
        if record.last_failure_code != canonical_error:
            raise EventBriefPersistenceConflict(
                "failed event brief cannot change its failure reason"
            )
    elif record.status == "running":
        record.status = "failed"
        record.last_failure_code = canonical_error
        record.last_failed_at = failed
        record.completed_at = failed
        record.updated_at = failed
        await session.flush()
    else:
        raise EventBriefPersistenceConflict(
            "terminal event brief cannot be marked failed"
        )
    return EventBriefFailureResult(
        brief_id=record.brief_id,
        attempt_number=record.attempt_count,
        retry_permitted=record.attempt_count < record.max_attempts,
        error_code=canonical_error,
    )


__all__ = [
    "EventBriefFailureResult",
    "EventBriefPersistenceConflict",
    "EventBriefPublishResult",
    "EventBriefPublishResultStatus",
    "EventBriefStartResult",
    "EventBriefStartStatus",
    "build_event_brief_insert_statement",
    "event_brief_payload_sha256",
    "fail_event_brief_generation",
    "publish_event_brief",
    "start_event_brief_generation",
]
