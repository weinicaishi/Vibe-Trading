"""Audited, fail-closed operator review for immutable EventBrief payloads."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.market_morning.db import get_session_factory
from src.market_morning.models import (
    AuditLog,
    EventBriefRecord,
    Issuer,
    NormalizedEvent,
    new_id,
)

EventBriefReviewDecision = Literal["approve", "reject"]
EventBriefReviewMutationStatus = Literal["reviewed", "already_reviewed"]
_REVIEW_STATUSES = frozenset({"pending", "auto_validated", "approved", "rejected"})


class EventBriefReviewError(RuntimeError):
    pass


class EventBriefReviewNotFound(EventBriefReviewError):
    pass


class EventBriefReviewConflict(EventBriefReviewError):
    pass


@dataclass(frozen=True, slots=True)
class EventBriefReviewQueueItem:
    brief_id: str
    event_id: str
    event_version: int
    issuer_id: str
    issuer_code: str
    legal_name_ja: str
    event_title: str
    status: str
    review_status: str
    model_version: str
    prompt_version: str
    attempt_count: int
    source_count: int
    validation_errors: tuple[str, ...]
    last_failure_code: str | None
    completed_at: datetime | None
    published_at: datetime | None
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class EventBriefReviewResult:
    status: EventBriefReviewMutationStatus
    brief_id: str
    review_status: Literal["approved", "rejected"]
    reviewed_at: datetime


def _uuid(value: str, *, field_name: str) -> str:
    try:
        return str(UUID(value))
    except (ValueError, AttributeError) as error:
        raise ValueError(f"{field_name} must be a UUID") from error


def _required(value: str, *, field_name: str, maximum: int) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > maximum:
        raise ValueError(f"{field_name} must contain 1 to {maximum} characters")
    return normalized


def _mysql_utc(value: datetime, *, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _utc_aware(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=timezone.utc)


def _bounded(limit: int) -> int:
    if not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    return limit


def build_event_brief_review_queue_statement(
    *, review_status: str | None = None, limit: int = 50
):
    bounded = _bounded(limit)
    statement = (
        select(
            EventBriefRecord.brief_id,
            EventBriefRecord.event_id,
            EventBriefRecord.event_version,
            NormalizedEvent.issuer_id,
            Issuer.issuer_code,
            Issuer.legal_name_ja,
            NormalizedEvent.title.label("event_title"),
            EventBriefRecord.status,
            EventBriefRecord.review_status,
            EventBriefRecord.model_version,
            EventBriefRecord.prompt_version,
            EventBriefRecord.attempt_count,
            func.json_length(EventBriefRecord.source_ids).label("source_count"),
            EventBriefRecord.validation_errors,
            EventBriefRecord.last_failure_code,
            EventBriefRecord.completed_at,
            EventBriefRecord.published_at,
            EventBriefRecord.updated_at,
        )
        .select_from(EventBriefRecord)
        .join(NormalizedEvent, NormalizedEvent.event_id == EventBriefRecord.event_id)
        .join(Issuer, Issuer.issuer_id == NormalizedEvent.issuer_id)
    )
    if review_status is not None:
        canonical_status = _required(
            review_status,
            field_name="review_status",
            maximum=32,
        )
        if canonical_status not in _REVIEW_STATUSES:
            raise ValueError("review_status is unsupported")
        statement = statement.where(
            EventBriefRecord.review_status == canonical_status
        )
    return statement.order_by(
        EventBriefRecord.updated_at.desc(),
        EventBriefRecord.brief_id.desc(),
    ).limit(bounded)


def _brief_lock_statement(brief_id: str):
    return (
        select(EventBriefRecord)
        .where(EventBriefRecord.brief_id == _uuid(brief_id, field_name="brief_id"))
        .with_for_update()
    )


async def load_event_brief_review_queue(
    session: AsyncSession,
    *,
    review_status: str | None = None,
    limit: int = 50,
) -> tuple[EventBriefReviewQueueItem, ...]:
    rows = (
        await session.execute(
            build_event_brief_review_queue_statement(
                review_status=review_status,
                limit=limit,
            )
        )
    ).mappings().all()
    return tuple(
        EventBriefReviewQueueItem(
            brief_id=row["brief_id"],
            event_id=row["event_id"],
            event_version=row["event_version"],
            issuer_id=row["issuer_id"],
            issuer_code=row["issuer_code"],
            legal_name_ja=row["legal_name_ja"],
            event_title=row["event_title"],
            status=row["status"],
            review_status=row["review_status"],
            model_version=row["model_version"],
            prompt_version=row["prompt_version"],
            attempt_count=row["attempt_count"],
            source_count=int(row["source_count"] or 0),
            validation_errors=tuple(row["validation_errors"] or ()),
            last_failure_code=row["last_failure_code"],
            completed_at=_utc_aware(row["completed_at"]),
            published_at=_utc_aware(row["published_at"]),
            updated_at=_utc_aware(row["updated_at"]),
        )
        for row in rows
    )


async def review_event_brief(
    session: AsyncSession,
    *,
    brief_id: str,
    decision: EventBriefReviewDecision,
    reason_code: str,
    actor_reference: str,
    reviewed_at: datetime,
) -> EventBriefReviewResult:
    canonical_decision = _required(
        decision,
        field_name="decision",
        maximum=16,
    )
    if canonical_decision not in {"approve", "reject"}:
        raise ValueError("decision is unsupported")
    canonical_reason = _required(
        reason_code,
        field_name="reason_code",
        maximum=64,
    )
    canonical_actor = _required(
        actor_reference,
        field_name="actor_reference",
        maximum=128,
    )
    occurred_at = _mysql_utc(reviewed_at, field_name="reviewed_at")
    record = (
        await session.execute(_brief_lock_statement(brief_id))
    ).scalar_one_or_none()
    if record is None:
        raise EventBriefReviewNotFound("event brief is unavailable")

    target_status = "approved" if canonical_decision == "approve" else "rejected"
    if record.review_status == target_status:
        return EventBriefReviewResult(
            status="already_reviewed",
            brief_id=record.brief_id,
            review_status=target_status,
            reviewed_at=_utc_aware(record.updated_at),
        )
    if canonical_decision == "approve" and (
        record.status != "published" or record.review_status != "auto_validated"
    ):
        raise EventBriefReviewConflict(
            "only auto-validated published briefs can be approved"
        )
    if record.review_status == "rejected":
        raise EventBriefReviewConflict(
            "rejected briefs require a new generated revision"
        )

    previous_status = record.review_status
    record.review_status = target_status
    record.updated_at = occurred_at
    session.add(
        AuditLog(
            audit_id=new_id(),
            actor_user_id=None,
            action=f"event_brief.{target_status}",
            entity_type="event_brief",
            entity_id=record.brief_id,
            details={
                "actor_reference": canonical_actor,
                "from_review_status": previous_status,
                "reason_code": canonical_reason,
                "to_review_status": target_status,
            },
            occurred_at=occurred_at,
        )
    )
    await session.flush()
    return EventBriefReviewResult(
        status="reviewed",
        brief_id=record.brief_id,
        review_status=target_status,
        reviewed_at=_utc_aware(occurred_at),
    )


async def get_event_brief_review_queue(
    *, review_status: str | None = None, limit: int = 50
) -> tuple[EventBriefReviewQueueItem, ...]:
    factory = get_session_factory()
    async with factory() as session:
        return await load_event_brief_review_queue(
            session,
            review_status=review_status,
            limit=limit,
        )


async def apply_event_brief_review(
    *,
    brief_id: str,
    decision: EventBriefReviewDecision,
    reason_code: str,
    actor_reference: str,
) -> EventBriefReviewResult:
    factory = get_session_factory()
    async with factory.begin() as session:
        return await review_event_brief(
            session,
            brief_id=brief_id,
            decision=decision,
            reason_code=reason_code,
            actor_reference=actor_reference,
            reviewed_at=datetime.now(timezone.utc),
        )


__all__ = [
    "EventBriefReviewConflict",
    "EventBriefReviewError",
    "EventBriefReviewNotFound",
    "EventBriefReviewQueueItem",
    "EventBriefReviewResult",
    "apply_event_brief_review",
    "build_event_brief_review_queue_statement",
    "get_event_brief_review_queue",
    "load_event_brief_review_queue",
    "review_event_brief",
]
