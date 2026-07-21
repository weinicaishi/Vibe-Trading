"""Privacy-safe content reporting and audited operator resolution."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.market_morning.db import get_session_factory
from src.market_morning.models import (
    AuditLog,
    ContentReportRecord,
    Issuer,
    MorningEditionRecord,
    NormalizedEvent,
    new_id,
)
from src.market_morning.repositories.morning_edition import (
    EDITION_PAYLOAD_SCHEMA_VERSION,
    decode_morning_edition,
    morning_edition_payload_sha256,
)

ContentReportReason = Literal[
    "fact_inaccurate",
    "source_mismatch",
    "outdated_or_corrected",
    "other_content_issue",
]
ContentReportDecision = Literal["resolve", "dismiss"]
ContentReportMutationStatus = Literal["reported", "already_reported"]
ContentReportReviewMutationStatus = Literal["reviewed", "already_reviewed"]

_REASONS = frozenset(
    {
        "fact_inaccurate",
        "source_mismatch",
        "outdated_or_corrected",
        "other_content_issue",
    }
)
_REPORT_STATUSES = frozenset({"pending", "resolved", "dismissed"})
_RESOLUTION_CODES: dict[str, frozenset[str]] = {
    "resolve": frozenset(
        {
            "brief_rejected",
            "source_corrected",
            "content_revised",
            "duplicate_report",
        }
    ),
    "dismiss": frozenset(
        {
            "no_issue_found",
            "insufficient_evidence",
            "duplicate_report",
        }
    ),
}


class ContentReportError(RuntimeError):
    pass


class ContentReportUnavailable(ContentReportError):
    pass


class ContentReportNotFound(ContentReportError):
    pass


class ContentReportConflict(ContentReportError):
    pass


@dataclass(frozen=True, slots=True)
class ContentReportResult:
    status: ContentReportMutationStatus
    report_id: str
    reason_code: ContentReportReason
    report_status: Literal["pending", "resolved", "dismissed"]
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ContentReportQueueItem:
    report_id: str
    edition_date: date
    event_id: str
    issuer_id: str
    issuer_code: str
    legal_name_ja: str
    event_title: str
    event_type: str
    reason_code: str
    report_status: str
    resolution_code: str | None
    reviewed_by: str | None
    reviewed_at: datetime | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class ContentReportReviewResult:
    status: ContentReportReviewMutationStatus
    report_id: str
    report_status: Literal["resolved", "dismissed"]
    resolution_code: str
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
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _result(
    record: ContentReportRecord,
    *,
    status: ContentReportMutationStatus,
) -> ContentReportResult:
    created_at = _utc_aware(record.created_at)
    assert created_at is not None
    return ContentReportResult(
        status=status,
        report_id=record.report_id,
        reason_code=record.reason_code,
        report_status=record.status,
        created_at=created_at,
    )


async def _load_owned_edition_event(
    session: AsyncSession,
    *,
    user_id: str,
    edition_id: str,
    event_id: str,
) -> None:
    record = (
        await session.execute(
            select(MorningEditionRecord)
            .where(
                MorningEditionRecord.edition_id == edition_id,
                MorningEditionRecord.user_id == user_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if record is None:
        raise ContentReportUnavailable("edition_unavailable")
    if (
        record.schema_version != EDITION_PAYLOAD_SCHEMA_VERSION
        or morning_edition_payload_sha256(record.payload) != record.payload_sha256
    ):
        raise ContentReportUnavailable("edition_invalid")
    edition = decode_morning_edition(record.payload)
    has_cited_event = any(
        fact.event_id == event_id and bool(fact.citations)
        for issuer in edition.issuers
        for fact in issuer.facts
    )
    if not has_cited_event:
        raise ContentReportUnavailable("edition_event_unavailable")


async def submit_content_report(
    session: AsyncSession,
    *,
    user_id: str,
    edition_id: str,
    event_id: str,
    reason_code: ContentReportReason | str,
    reported_at: datetime,
) -> ContentReportResult:
    canonical_user = _uuid(user_id, field_name="user_id")
    canonical_edition = _uuid(edition_id, field_name="edition_id")
    canonical_event = _uuid(event_id, field_name="event_id")
    canonical_reason = _required(
        reason_code,
        field_name="reason_code",
        maximum=32,
    )
    if canonical_reason not in _REASONS:
        raise ValueError("reason_code is unsupported")
    reported = _mysql_utc(reported_at, field_name="reported_at")
    await _load_owned_edition_event(
        session,
        user_id=canonical_user,
        edition_id=canonical_edition,
        event_id=canonical_event,
    )
    existing = (
        await session.execute(
            select(ContentReportRecord)
            .where(
                ContentReportRecord.user_id == canonical_user,
                ContentReportRecord.edition_id == canonical_edition,
                ContentReportRecord.event_id == canonical_event,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if existing is not None:
        return _result(existing, status="already_reported")
    record = ContentReportRecord(
        report_id=new_id(),
        user_id=canonical_user,
        edition_id=canonical_edition,
        event_id=canonical_event,
        reason_code=canonical_reason,
        status="pending",
        resolution_code=None,
        reviewed_by=None,
        reviewed_at=None,
        created_at=reported,
        updated_at=reported,
    )
    session.add(record)
    await session.flush()
    return _result(record, status="reported")


def build_content_report_queue_statement(
    *,
    report_status: str | None = None,
    limit: int = 50,
):
    if not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    statement = (
        select(
            ContentReportRecord.report_id,
            MorningEditionRecord.edition_date,
            ContentReportRecord.event_id,
            NormalizedEvent.issuer_id,
            Issuer.issuer_code,
            Issuer.legal_name_ja,
            NormalizedEvent.title.label("event_title"),
            NormalizedEvent.event_type,
            ContentReportRecord.reason_code,
            ContentReportRecord.status.label("report_status"),
            ContentReportRecord.resolution_code,
            ContentReportRecord.reviewed_by,
            ContentReportRecord.reviewed_at,
            ContentReportRecord.created_at,
            ContentReportRecord.updated_at,
        )
        .select_from(ContentReportRecord)
        .join(
            MorningEditionRecord,
            MorningEditionRecord.edition_id == ContentReportRecord.edition_id,
        )
        .join(
            NormalizedEvent,
            NormalizedEvent.event_id == ContentReportRecord.event_id,
        )
        .join(Issuer, Issuer.issuer_id == NormalizedEvent.issuer_id)
    )
    if report_status is not None:
        canonical_status = _required(
            report_status,
            field_name="report_status",
            maximum=16,
        )
        if canonical_status not in _REPORT_STATUSES:
            raise ValueError("report_status is unsupported")
        statement = statement.where(ContentReportRecord.status == canonical_status)
    return statement.order_by(
        ContentReportRecord.created_at.desc(),
        ContentReportRecord.report_id.desc(),
    ).limit(limit)


async def load_content_report_queue(
    session: AsyncSession,
    *,
    report_status: str | None = None,
    limit: int = 50,
) -> tuple[ContentReportQueueItem, ...]:
    rows = (
        await session.execute(
            build_content_report_queue_statement(
                report_status=report_status,
                limit=limit,
            )
        )
    ).mappings().all()
    return tuple(
        ContentReportQueueItem(
            report_id=row["report_id"],
            edition_date=row["edition_date"],
            event_id=row["event_id"],
            issuer_id=row["issuer_id"],
            issuer_code=row["issuer_code"],
            legal_name_ja=row["legal_name_ja"],
            event_title=row["event_title"],
            event_type=row["event_type"],
            reason_code=row["reason_code"],
            report_status=row["report_status"],
            resolution_code=row["resolution_code"],
            reviewed_by=row["reviewed_by"],
            reviewed_at=_utc_aware(row["reviewed_at"]),
            created_at=_utc_aware(row["created_at"]),
            updated_at=_utc_aware(row["updated_at"]),
        )
        for row in rows
    )


async def review_content_report(
    session: AsyncSession,
    *,
    report_id: str,
    decision: ContentReportDecision | str,
    resolution_code: str,
    actor_reference: str,
    reviewed_at: datetime,
) -> ContentReportReviewResult:
    canonical_report = _uuid(report_id, field_name="report_id")
    canonical_decision = _required(
        decision,
        field_name="decision",
        maximum=16,
    )
    if canonical_decision not in _RESOLUTION_CODES:
        raise ValueError("decision is unsupported")
    canonical_resolution = _required(
        resolution_code,
        field_name="resolution_code",
        maximum=64,
    )
    if canonical_resolution not in _RESOLUTION_CODES[canonical_decision]:
        raise ValueError("resolution_code is unsupported for decision")
    canonical_actor = _required(
        actor_reference,
        field_name="actor_reference",
        maximum=128,
    )
    reviewed = _mysql_utc(reviewed_at, field_name="reviewed_at")
    record = (
        await session.execute(
            select(ContentReportRecord)
            .where(ContentReportRecord.report_id == canonical_report)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if record is None:
        raise ContentReportNotFound("content report is unavailable")
    target_status = "resolved" if canonical_decision == "resolve" else "dismissed"
    if record.status == target_status and record.resolution_code == canonical_resolution:
        existing_reviewed = _utc_aware(record.reviewed_at)
        assert existing_reviewed is not None
        return ContentReportReviewResult(
            status="already_reviewed",
            report_id=record.report_id,
            report_status=target_status,
            resolution_code=canonical_resolution,
            reviewed_at=existing_reviewed,
        )
    if record.status != "pending":
        raise ContentReportConflict("content report resolution is terminal")
    record.status = target_status
    record.resolution_code = canonical_resolution
    record.reviewed_by = canonical_actor
    record.reviewed_at = reviewed
    record.updated_at = reviewed
    session.add(
        AuditLog(
            audit_id=new_id(),
            actor_user_id=None,
            action=f"content_report.{target_status}",
            entity_type="content_report",
            entity_id=record.report_id,
            details={
                "actor_reference": canonical_actor,
                "reason_code": record.reason_code,
                "resolution_code": canonical_resolution,
                "to_status": target_status,
            },
            occurred_at=reviewed,
        )
    )
    await session.flush()
    return ContentReportReviewResult(
        status="reviewed",
        report_id=record.report_id,
        report_status=target_status,
        resolution_code=canonical_resolution,
        reviewed_at=_utc_aware(reviewed),
    )


async def create_content_report(
    *,
    user_id: str,
    edition_id: str,
    event_id: str,
    reason_code: ContentReportReason | str,
) -> ContentReportResult:
    factory = get_session_factory()
    async with factory.begin() as session:
        return await submit_content_report(
            session,
            user_id=user_id,
            edition_id=edition_id,
            event_id=event_id,
            reason_code=reason_code,
            reported_at=datetime.now(timezone.utc),
        )


async def get_content_report_queue(
    *,
    report_status: str | None = None,
    limit: int = 50,
) -> tuple[ContentReportQueueItem, ...]:
    factory = get_session_factory()
    async with factory() as session:
        return await load_content_report_queue(
            session,
            report_status=report_status,
            limit=limit,
        )


async def apply_content_report_review(
    *,
    report_id: str,
    decision: ContentReportDecision | str,
    resolution_code: str,
    actor_reference: str,
) -> ContentReportReviewResult:
    factory = get_session_factory()
    async with factory.begin() as session:
        return await review_content_report(
            session,
            report_id=report_id,
            decision=decision,
            resolution_code=resolution_code,
            actor_reference=actor_reference,
            reviewed_at=datetime.now(timezone.utc),
        )


__all__ = [
    "ContentReportConflict",
    "ContentReportError",
    "ContentReportNotFound",
    "ContentReportQueueItem",
    "ContentReportResult",
    "ContentReportReviewResult",
    "ContentReportUnavailable",
    "apply_content_report_review",
    "build_content_report_queue_statement",
    "create_content_report",
    "get_content_report_queue",
    "load_content_report_queue",
    "review_content_report",
    "submit_content_report",
]
