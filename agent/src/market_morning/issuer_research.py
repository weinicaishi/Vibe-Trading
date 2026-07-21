"""Private issuer history and lightweight personal-note service."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.market_morning.db import get_session_factory
from src.market_morning.edition_generation import (
    _event_inputs,
    _generation_event_columns,
    _latest_event_brief_subquery,
)
from src.market_morning.models import (
    EventSource,
    Issuer,
    IssuerResearchNoteRecord,
    NormalizedEvent,
    SourceRecord,
    User,
    WatchlistItem,
    new_id,
)
from src.market_morning.pipeline.morning_edition import (
    EditionEventFactInput,
    EditionSourceCitation,
)

MAX_ISSUER_HISTORY_EVENTS = 100
MAX_ISSUER_NOTE_CHARACTERS = 1_000


class IssuerResearchUnavailable(RuntimeError):
    """Raised without revealing whether another user's issuer data exists."""


class IssuerResearchValidationError(ValueError):
    """Raised for invalid limits or private note content."""


@dataclass(frozen=True, slots=True)
class IssuerResearchNoteView:
    note_id: str
    text: str
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class IssuerResearchEventView:
    event_id: str
    event_family_key: str
    event_version: int
    title: str
    event_type: str
    occurred_at: datetime
    lifecycle_status: str
    supersedes_event_id: str | None
    facts: tuple[EditionEventFactInput, ...]
    citations: tuple[EditionSourceCitation, ...]
    warnings: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class IssuerResearchView:
    issuer_id: str
    issuer_code: str
    legal_name_ja: str
    market_segment: str
    note: IssuerResearchNoteView | None
    events: tuple[IssuerResearchEventView, ...]


def _uuid(value: str, *, field_name: str) -> str:
    try:
        return str(UUID(value))
    except (ValueError, AttributeError) as error:
        raise IssuerResearchValidationError(
            f"{field_name} must be a UUID"
        ) from error


def _bounded_limit(limit: int) -> int:
    if not 1 <= limit <= MAX_ISSUER_HISTORY_EVENTS:
        raise IssuerResearchValidationError(
            f"limit must be between 1 and {MAX_ISSUER_HISTORY_EVENTS}"
        )
    return limit


def _mysql_utc(value: datetime, *, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise IssuerResearchValidationError(
            f"{field_name} must be timezone-aware"
        )
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _utc_aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def build_issuer_research_access_statement(*, user_id: str, issuer_id: str):
    canonical_user = _uuid(user_id, field_name="user_id")
    canonical_issuer = _uuid(issuer_id, field_name="issuer_id")
    return (
        select(
            Issuer.issuer_id,
            Issuer.issuer_code,
            Issuer.legal_name_ja,
            Issuer.market_segment,
        )
        .select_from(WatchlistItem)
        .join(User, User.user_id == WatchlistItem.user_id)
        .join(Issuer, Issuer.issuer_id == WatchlistItem.issuer_id)
        .where(
            WatchlistItem.user_id == canonical_user,
            WatchlistItem.issuer_id == canonical_issuer,
            WatchlistItem.removed_at.is_(None),
            User.account_status == "active",
            User.deleted_at.is_(None),
            Issuer.active_status == "active",
            Issuer.effective_to.is_(None),
        )
    )


def build_issuer_history_statement(*, issuer_id: str, limit: int = 50):
    canonical_issuer = _uuid(issuer_id, field_name="issuer_id")
    bounded = _bounded_limit(limit)
    selected_events = (
        select(NormalizedEvent.event_id)
        .where(NormalizedEvent.issuer_id == canonical_issuer)
        .order_by(
            NormalizedEvent.occurred_at.desc(),
            NormalizedEvent.event_version.desc(),
            NormalizedEvent.event_id,
        )
        .limit(bounded)
        .subquery("issuer_history_events")
    )
    latest_brief = _latest_event_brief_subquery()
    return (
        select(
            *_generation_event_columns(latest_brief),
            NormalizedEvent.supersedes_event_id,
        )
        .select_from(NormalizedEvent)
        .join(
            selected_events,
            selected_events.c.event_id == NormalizedEvent.event_id,
        )
        .join(
            EventSource,
            EventSource.event_id == NormalizedEvent.event_id,
        )
        .join(
            SourceRecord,
            SourceRecord.source_record_id == EventSource.source_record_id,
        )
        .outerjoin(
            latest_brief,
            (latest_brief.c.event_id == NormalizedEvent.event_id)
            & (latest_brief.c.event_version == NormalizedEvent.event_version),
        )
        .order_by(
            NormalizedEvent.occurred_at.desc(),
            NormalizedEvent.event_version.desc(),
            NormalizedEvent.event_id,
            SourceRecord.source_provider,
            SourceRecord.provider_document_id,
            SourceRecord.provider_revision_key,
        )
    )


async def _load_access(
    session: AsyncSession,
    *,
    user_id: str,
    issuer_id: str,
) -> dict:
    row = (
        await session.execute(
            build_issuer_research_access_statement(
                user_id=user_id,
                issuer_id=issuer_id,
            )
        )
    ).mappings().one_or_none()
    if row is None:
        raise IssuerResearchUnavailable("issuer_research_unavailable")
    return row


def _note_view(record: IssuerResearchNoteRecord) -> IssuerResearchNoteView:
    return IssuerResearchNoteView(
        note_id=record.note_id,
        text=record.note_text,
        created_at=_utc_aware(record.created_at),
        updated_at=_utc_aware(record.updated_at),
    )


async def load_issuer_research(
    session: AsyncSession,
    *,
    user_id: str,
    issuer_id: str,
    limit: int = 50,
) -> IssuerResearchView:
    canonical_user = _uuid(user_id, field_name="user_id")
    canonical_issuer = _uuid(issuer_id, field_name="issuer_id")
    issuer = await _load_access(
        session,
        user_id=canonical_user,
        issuer_id=canonical_issuer,
    )
    rows = (
        await session.execute(
            build_issuer_history_statement(
                issuer_id=canonical_issuer,
                limit=limit,
            )
        )
    ).mappings().all()
    inputs = _event_inputs(tuple(rows)).get(canonical_issuer, ())
    supersedes_by_event = {
        row["event_id"]: row.get("supersedes_event_id") for row in rows
    }
    events = tuple(
        IssuerResearchEventView(
            event_id=event.event_id,
            event_family_key=event.event_family_key,
            event_version=event.event_version,
            title=event.title,
            event_type=event.event_type,
            occurred_at=event.occurred_at,
            lifecycle_status=event.lifecycle_status,
            supersedes_event_id=supersedes_by_event.get(event.event_id),
            facts=event.facts,
            citations=event.citations,
            warnings=event.warnings,
        )
        for event in inputs
    )
    note = (
        await session.execute(
            select(IssuerResearchNoteRecord).where(
                IssuerResearchNoteRecord.user_id == canonical_user,
                IssuerResearchNoteRecord.issuer_id == canonical_issuer,
            )
        )
    ).scalar_one_or_none()
    return IssuerResearchView(
        issuer_id=issuer["issuer_id"],
        issuer_code=issuer["issuer_code"],
        legal_name_ja=issuer["legal_name_ja"],
        market_segment=issuer["market_segment"],
        note=_note_view(note) if note is not None else None,
        events=events,
    )


def build_issuer_note_upsert_statement(
    *,
    note_id: str,
    user_id: str,
    issuer_id: str,
    text: str,
    changed_at: datetime,
):
    statement = mysql_insert(IssuerResearchNoteRecord.__table__).values(
        note_id=note_id,
        user_id=user_id,
        issuer_id=issuer_id,
        note_text=text,
        created_at=changed_at,
        updated_at=changed_at,
    )
    return statement.on_duplicate_key_update(
        note_text=statement.inserted.note_text,
        updated_at=statement.inserted.updated_at,
    )


async def set_issuer_research_note(
    session: AsyncSession,
    *,
    user_id: str,
    issuer_id: str,
    text: str,
    changed_at: datetime,
) -> IssuerResearchNoteView:
    canonical_user = _uuid(user_id, field_name="user_id")
    canonical_issuer = _uuid(issuer_id, field_name="issuer_id")
    normalized = text.strip()
    if not normalized or len(normalized) > MAX_ISSUER_NOTE_CHARACTERS:
        raise IssuerResearchValidationError(
            f"text must contain 1 to {MAX_ISSUER_NOTE_CHARACTERS} characters"
        )
    changed = _mysql_utc(changed_at, field_name="changed_at")
    await _load_access(
        session,
        user_id=canonical_user,
        issuer_id=canonical_issuer,
    )
    await session.execute(
        build_issuer_note_upsert_statement(
            note_id=new_id(),
            user_id=canonical_user,
            issuer_id=canonical_issuer,
            text=normalized,
            changed_at=changed,
        )
    )
    record = (
        await session.execute(
            select(IssuerResearchNoteRecord).where(
                IssuerResearchNoteRecord.user_id == canonical_user,
                IssuerResearchNoteRecord.issuer_id == canonical_issuer,
            )
        )
    ).scalar_one_or_none()
    if record is None:
        raise IssuerResearchUnavailable("issuer_note_persistence_failed")
    await session.flush()
    return _note_view(record)


async def clear_issuer_research_note(
    session: AsyncSession,
    *,
    user_id: str,
    issuer_id: str,
) -> bool:
    canonical_user = _uuid(user_id, field_name="user_id")
    canonical_issuer = _uuid(issuer_id, field_name="issuer_id")
    await _load_access(
        session,
        user_id=canonical_user,
        issuer_id=canonical_issuer,
    )
    record = (
        await session.execute(
            select(IssuerResearchNoteRecord)
            .where(
                IssuerResearchNoteRecord.user_id == canonical_user,
                IssuerResearchNoteRecord.issuer_id == canonical_issuer,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if record is None:
        return False
    await session.delete(record)
    await session.flush()
    return True


async def get_issuer_research(
    *, user_id: str, issuer_id: str, limit: int = 50
) -> IssuerResearchView:
    factory = get_session_factory()
    async with factory() as session:
        return await load_issuer_research(
            session,
            user_id=user_id,
            issuer_id=issuer_id,
            limit=limit,
        )


async def update_issuer_research_note(
    *, user_id: str, issuer_id: str, text: str
) -> IssuerResearchNoteView:
    factory = get_session_factory()
    async with factory.begin() as session:
        return await set_issuer_research_note(
            session,
            user_id=user_id,
            issuer_id=issuer_id,
            text=text,
            changed_at=datetime.now(timezone.utc),
        )


async def delete_issuer_research_note(
    *, user_id: str, issuer_id: str
) -> bool:
    factory = get_session_factory()
    async with factory.begin() as session:
        return await clear_issuer_research_note(
            session,
            user_id=user_id,
            issuer_id=issuer_id,
        )


__all__ = [
    "IssuerResearchEventView",
    "IssuerResearchNoteView",
    "IssuerResearchUnavailable",
    "IssuerResearchValidationError",
    "IssuerResearchView",
    "MAX_ISSUER_HISTORY_EVENTS",
    "MAX_ISSUER_NOTE_CHARACTERS",
    "build_issuer_history_statement",
    "build_issuer_note_upsert_statement",
    "build_issuer_research_access_statement",
    "clear_issuer_research_note",
    "delete_issuer_research_note",
    "get_issuer_research",
    "load_issuer_research",
    "set_issuer_research_note",
    "update_issuer_research_note",
]
