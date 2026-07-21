"""Privacy-safe, audited operator review queues for catalog records."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from src.market_morning.db import get_session_factory
from src.market_morning.models import (
    AuditLog,
    EventMergeCandidate,
    Issuer,
    IssuerAlias,
    NormalizedEvent,
    new_id,
)

_REVIEW_STATUSES = frozenset({"pending", "approved", "rejected"})
ReviewDecision = Literal["approve", "reject"]
ReviewMutationStatus = Literal["reviewed", "already_reviewed"]


class ReviewQueueError(RuntimeError):
    pass


class ReviewQueueNotFound(ReviewQueueError):
    pass


class ReviewQueueConflict(ReviewQueueError):
    pass


@dataclass(frozen=True, slots=True)
class IssuerAliasReviewQueueItem:
    alias_id: str
    issuer_id: str
    issuer_code: str
    legal_name_ja: str
    display_alias: str
    normalized_alias: str
    source_type: str
    review_status: str
    effective_from: date
    effective_to: date | None
    reviewed_by: str | None
    reviewed_at: datetime | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class IssuerAliasReviewResult:
    status: ReviewMutationStatus
    alias_id: str
    review_status: Literal["approved", "rejected"]
    reviewed_at: datetime


@dataclass(frozen=True, slots=True)
class EventMergeReviewQueueItem:
    candidate_id: str
    issuer_id: str
    issuer_code: str
    legal_name_ja: str
    left_event_id: str
    left_event_title: str
    left_event_type: str
    left_occurred_at: datetime
    right_event_id: str
    right_event_title: str
    right_event_type: str
    right_occurred_at: datetime
    reason: str
    title_similarity: float
    time_distance_seconds: int
    review_status: str
    reviewed_by: str | None
    reviewed_at: datetime | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class EventMergeReviewResult:
    status: ReviewMutationStatus
    candidate_id: str
    review_status: Literal["approved", "rejected"]
    reviewed_at: datetime


def _required(value: str, *, field_name: str, maximum: int) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > maximum:
        raise ValueError(f"{field_name} must contain 1 to {maximum} characters")
    return normalized


def _uuid(value: str, *, field_name: str) -> str:
    try:
        return str(UUID(value))
    except (ValueError, AttributeError) as error:
        raise ValueError(f"{field_name} must be a UUID") from error


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


def build_issuer_alias_review_queue_statement(
    *, review_status: str | None = None, limit: int = 50
):
    bounded = _bounded(limit)
    statement = (
        select(
            IssuerAlias.alias_id,
            IssuerAlias.issuer_id,
            Issuer.issuer_code,
            Issuer.legal_name_ja,
            IssuerAlias.display_alias,
            IssuerAlias.normalized_alias,
            IssuerAlias.source_type,
            IssuerAlias.review_status,
            IssuerAlias.effective_from,
            IssuerAlias.effective_to,
            IssuerAlias.reviewed_by,
            IssuerAlias.reviewed_at,
            IssuerAlias.created_at,
        )
        .select_from(IssuerAlias)
        .join(Issuer, Issuer.issuer_id == IssuerAlias.issuer_id)
    )
    if review_status is not None:
        canonical_status = _required(
            review_status,
            field_name="review_status",
            maximum=32,
        )
        if canonical_status not in _REVIEW_STATUSES:
            raise ValueError("review_status is unsupported")
        statement = statement.where(IssuerAlias.review_status == canonical_status)
    return statement.order_by(
        IssuerAlias.created_at.asc(),
        IssuerAlias.alias_id.asc(),
    ).limit(bounded)


async def load_issuer_alias_review_queue(
    session: AsyncSession,
    *,
    review_status: str | None = None,
    limit: int = 50,
) -> tuple[IssuerAliasReviewQueueItem, ...]:
    rows = (
        await session.execute(
            build_issuer_alias_review_queue_statement(
                review_status=review_status,
                limit=limit,
            )
        )
    ).mappings().all()
    return tuple(
        IssuerAliasReviewQueueItem(
            alias_id=row["alias_id"],
            issuer_id=row["issuer_id"],
            issuer_code=row["issuer_code"],
            legal_name_ja=row["legal_name_ja"],
            display_alias=row["display_alias"],
            normalized_alias=row["normalized_alias"],
            source_type=row["source_type"],
            review_status=row["review_status"],
            effective_from=row["effective_from"],
            effective_to=row["effective_to"],
            reviewed_by=row["reviewed_by"],
            reviewed_at=_utc_aware(row["reviewed_at"]),
            created_at=_utc_aware(row["created_at"]),
        )
        for row in rows
    )


def _issuer_alias_lock_statement(alias_id: str):
    return (
        select(IssuerAlias)
        .where(IssuerAlias.alias_id == _uuid(alias_id, field_name="alias_id"))
        .with_for_update()
    )


def _approved_alias_conflict_statement(record: IssuerAlias):
    return (
        select(IssuerAlias.alias_id)
        .where(
            IssuerAlias.normalized_alias == record.normalized_alias,
            IssuerAlias.review_status == "approved",
            IssuerAlias.effective_to.is_(None),
            IssuerAlias.alias_id != record.alias_id,
        )
        .limit(1)
        .with_for_update()
    )


async def review_issuer_alias(
    session: AsyncSession,
    *,
    alias_id: str,
    decision: ReviewDecision,
    reason_code: str,
    actor_reference: str,
    reviewed_at: datetime,
) -> IssuerAliasReviewResult:
    canonical_decision = _required(decision, field_name="decision", maximum=16)
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
        await session.execute(_issuer_alias_lock_statement(alias_id))
    ).scalar_one_or_none()
    if record is None:
        raise ReviewQueueNotFound("issuer alias is unavailable")

    target_status = "approved" if canonical_decision == "approve" else "rejected"
    if record.review_status == target_status:
        return IssuerAliasReviewResult(
            status="already_reviewed",
            alias_id=record.alias_id,
            review_status=target_status,
            reviewed_at=_utc_aware(record.reviewed_at or record.created_at),
        )
    if record.review_status != "pending":
        raise ReviewQueueConflict("issuer alias review is terminal")
    if canonical_decision == "approve":
        if record.effective_to is not None:
            raise ReviewQueueConflict("expired issuer aliases cannot be approved")
        conflicting_alias_id = (
            await session.execute(_approved_alias_conflict_statement(record))
        ).scalar_one_or_none()
        if conflicting_alias_id is not None:
            raise ReviewQueueConflict("alias is already approved for another issuer")

    previous_status = record.review_status
    record.review_status = target_status
    record.reviewed_by = canonical_actor
    record.reviewed_at = occurred_at
    session.add(
        AuditLog(
            audit_id=new_id(),
            actor_user_id=None,
            action=f"issuer_alias.{target_status}",
            entity_type="issuer_alias",
            entity_id=record.alias_id,
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
    return IssuerAliasReviewResult(
        status="reviewed",
        alias_id=record.alias_id,
        review_status=target_status,
        reviewed_at=_utc_aware(occurred_at),
    )


def build_event_merge_review_queue_statement(
    *, review_status: str | None = None, limit: int = 50
):
    bounded = _bounded(limit)
    left_event = aliased(NormalizedEvent, name="left_event")
    right_event = aliased(NormalizedEvent, name="right_event")
    statement = (
        select(
            EventMergeCandidate.candidate_id,
            left_event.issuer_id.label("issuer_id"),
            Issuer.issuer_code,
            Issuer.legal_name_ja,
            EventMergeCandidate.left_event_id,
            left_event.title.label("left_event_title"),
            left_event.event_type.label("left_event_type"),
            left_event.occurred_at.label("left_occurred_at"),
            EventMergeCandidate.right_event_id,
            right_event.title.label("right_event_title"),
            right_event.event_type.label("right_event_type"),
            right_event.occurred_at.label("right_occurred_at"),
            EventMergeCandidate.reason,
            EventMergeCandidate.title_similarity,
            EventMergeCandidate.time_distance_seconds,
            EventMergeCandidate.review_status,
            EventMergeCandidate.reviewed_by,
            EventMergeCandidate.reviewed_at,
            EventMergeCandidate.created_at,
        )
        .select_from(EventMergeCandidate)
        .join(left_event, left_event.event_id == EventMergeCandidate.left_event_id)
        .join(
            right_event,
            (right_event.event_id == EventMergeCandidate.right_event_id)
            & (right_event.issuer_id == left_event.issuer_id),
        )
        .join(Issuer, Issuer.issuer_id == left_event.issuer_id)
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
            EventMergeCandidate.review_status == canonical_status
        )
    return statement.order_by(
        EventMergeCandidate.created_at.asc(),
        EventMergeCandidate.candidate_id.asc(),
    ).limit(bounded)


async def load_event_merge_review_queue(
    session: AsyncSession,
    *,
    review_status: str | None = None,
    limit: int = 50,
) -> tuple[EventMergeReviewQueueItem, ...]:
    rows = (
        await session.execute(
            build_event_merge_review_queue_statement(
                review_status=review_status,
                limit=limit,
            )
        )
    ).mappings().all()
    return tuple(
        EventMergeReviewQueueItem(
            candidate_id=row["candidate_id"],
            issuer_id=row["issuer_id"],
            issuer_code=row["issuer_code"],
            legal_name_ja=row["legal_name_ja"],
            left_event_id=row["left_event_id"],
            left_event_title=row["left_event_title"],
            left_event_type=row["left_event_type"],
            left_occurred_at=_utc_aware(row["left_occurred_at"]),
            right_event_id=row["right_event_id"],
            right_event_title=row["right_event_title"],
            right_event_type=row["right_event_type"],
            right_occurred_at=_utc_aware(row["right_occurred_at"]),
            reason=row["reason"],
            title_similarity=float(row["title_similarity"]),
            time_distance_seconds=int(row["time_distance_seconds"]),
            review_status=row["review_status"],
            reviewed_by=row["reviewed_by"],
            reviewed_at=_utc_aware(row["reviewed_at"]),
            created_at=_utc_aware(row["created_at"]),
        )
        for row in rows
    )


def _event_merge_candidate_lock_statement(candidate_id: str):
    return (
        select(EventMergeCandidate)
        .where(
            EventMergeCandidate.candidate_id
            == _uuid(candidate_id, field_name="candidate_id")
        )
        .with_for_update()
    )


def _candidate_event_issuers_statement(record: EventMergeCandidate):
    return (
        select(NormalizedEvent.issuer_id)
        .where(
            NormalizedEvent.event_id.in_(
                (record.left_event_id, record.right_event_id)
            )
        )
        .order_by(NormalizedEvent.event_id.asc())
        .with_for_update()
    )


async def review_event_merge_candidate(
    session: AsyncSession,
    *,
    candidate_id: str,
    decision: ReviewDecision,
    reason_code: str,
    actor_reference: str,
    reviewed_at: datetime,
) -> EventMergeReviewResult:
    canonical_decision = _required(decision, field_name="decision", maximum=16)
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
        await session.execute(_event_merge_candidate_lock_statement(candidate_id))
    ).scalar_one_or_none()
    if record is None:
        raise ReviewQueueNotFound("event merge candidate is unavailable")

    target_status = "approved" if canonical_decision == "approve" else "rejected"
    if record.review_status == target_status:
        return EventMergeReviewResult(
            status="already_reviewed",
            candidate_id=record.candidate_id,
            review_status=target_status,
            reviewed_at=_utc_aware(record.reviewed_at or record.created_at),
        )
    if record.review_status != "pending":
        raise ReviewQueueConflict("event merge candidate review is terminal")
    if canonical_decision == "approve":
        issuer_ids = (
            await session.execute(_candidate_event_issuers_statement(record))
        ).scalars().all()
        if len(issuer_ids) != 2 or len(set(issuer_ids)) != 1:
            raise ReviewQueueConflict(
                "event merge candidate no longer satisfies the issuer invariant"
            )

    previous_status = record.review_status
    record.review_status = target_status
    record.reviewed_by = canonical_actor
    record.reviewed_at = occurred_at
    session.add(
        AuditLog(
            audit_id=new_id(),
            actor_user_id=None,
            action=f"event_merge_candidate.{target_status}",
            entity_type="event_merge_candidate",
            entity_id=record.candidate_id,
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
    return EventMergeReviewResult(
        status="reviewed",
        candidate_id=record.candidate_id,
        review_status=target_status,
        reviewed_at=_utc_aware(occurred_at),
    )


async def get_issuer_alias_review_queue(
    *, review_status: str | None = "pending", limit: int = 50
) -> tuple[IssuerAliasReviewQueueItem, ...]:
    factory = get_session_factory()
    async with factory() as session:
        return await load_issuer_alias_review_queue(
            session,
            review_status=review_status,
            limit=limit,
        )


async def apply_issuer_alias_review(
    *,
    alias_id: str,
    decision: ReviewDecision,
    reason_code: str,
    actor_reference: str,
) -> IssuerAliasReviewResult:
    factory = get_session_factory()
    async with factory.begin() as session:
        return await review_issuer_alias(
            session,
            alias_id=alias_id,
            decision=decision,
            reason_code=reason_code,
            actor_reference=actor_reference,
            reviewed_at=datetime.now(timezone.utc),
        )


async def get_event_merge_review_queue(
    *, review_status: str | None = "pending", limit: int = 50
) -> tuple[EventMergeReviewQueueItem, ...]:
    factory = get_session_factory()
    async with factory() as session:
        return await load_event_merge_review_queue(
            session,
            review_status=review_status,
            limit=limit,
        )


async def apply_event_merge_candidate_review(
    *,
    candidate_id: str,
    decision: ReviewDecision,
    reason_code: str,
    actor_reference: str,
) -> EventMergeReviewResult:
    factory = get_session_factory()
    async with factory.begin() as session:
        return await review_event_merge_candidate(
            session,
            candidate_id=candidate_id,
            decision=decision,
            reason_code=reason_code,
            actor_reference=actor_reference,
            reviewed_at=datetime.now(timezone.utc),
        )


__all__ = [
    "EventMergeReviewQueueItem",
    "EventMergeReviewResult",
    "IssuerAliasReviewQueueItem",
    "IssuerAliasReviewResult",
    "ReviewQueueConflict",
    "ReviewQueueError",
    "ReviewQueueNotFound",
    "apply_event_merge_candidate_review",
    "apply_issuer_alias_review",
    "build_issuer_alias_review_queue_statement",
    "get_event_merge_review_queue",
    "get_issuer_alias_review_queue",
    "build_event_merge_review_queue_statement",
    "load_event_merge_review_queue",
    "load_issuer_alias_review_queue",
    "review_event_merge_candidate",
    "review_issuer_alias",
]
