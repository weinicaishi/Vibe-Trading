"""Read-only operational and audit queries for official-source ingestion."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.market_morning.db import get_session_factory
from src.market_morning.models import (
    EventSource,
    Issuer,
    NormalizedEvent,
    SourceCursor,
    SourceRecord,
)

SourceHealthStatus = Literal["healthy", "error", "never_run"]


@dataclass(frozen=True, slots=True)
class SourceHealthView:
    provider: str
    status: SourceHealthStatus
    cursor: dict | None
    last_successful_discovery_at: datetime | None
    last_error_at: datetime | None
    last_error_code: str | None


@dataclass(frozen=True, slots=True)
class EventAuditView:
    event_id: str
    event_family_key: str
    event_version: int
    issuer_id: str
    issuer_code: str
    legal_name_ja: str
    title: str
    event_type: str
    occurred_at: datetime
    event_lifecycle_status: str
    supersedes_event_id: str | None
    source_record_id: str
    source_provider: str
    provider_document_id: str
    provider_revision_key: str
    original_url: str
    source_published_at: datetime
    source_fetched_at: datetime
    source_lifecycle_status: str
    relation_type: str


def _required_identifier(value: str, *, field_name: str, max_length: int) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > max_length:
        raise ValueError(f"{field_name} must contain 1 to {max_length} characters")
    return normalized


def _bounded_limit(limit: int, *, maximum: int = 100) -> int:
    if not 1 <= limit <= maximum:
        raise ValueError(f"limit must be between 1 and {maximum}")
    return limit


def _utc_aware(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=timezone.utc)


def source_health_status(
    last_successful_discovery_at: datetime | None,
    last_error_at: datetime | None,
) -> SourceHealthStatus:
    if last_successful_discovery_at is None and last_error_at is None:
        return "never_run"
    if last_error_at is not None and (
        last_successful_discovery_at is None
        or last_error_at >= last_successful_discovery_at
    ):
        return "error"
    return "healthy"


def build_source_health_statement(
    *, provider: str | None = None, limit: int = 50
):
    bounded = _bounded_limit(limit)
    statement = select(
        SourceCursor.source_provider.label("provider"),
        SourceCursor.cursor_value.label("cursor"),
        SourceCursor.last_successful_discovery_at,
        SourceCursor.last_error_at,
        SourceCursor.last_error_code,
    )
    if provider is not None:
        canonical_provider = _required_identifier(
            provider, field_name="provider", max_length=64
        )
        statement = statement.where(SourceCursor.source_provider == canonical_provider)
    return statement.order_by(SourceCursor.source_provider).limit(bounded)


def _event_audit_columns():
    return (
        NormalizedEvent.event_id,
        NormalizedEvent.event_family_key,
        NormalizedEvent.event_version,
        NormalizedEvent.issuer_id,
        Issuer.issuer_code,
        Issuer.legal_name_ja,
        NormalizedEvent.title,
        NormalizedEvent.event_type,
        NormalizedEvent.occurred_at,
        NormalizedEvent.lifecycle_status.label("event_lifecycle_status"),
        NormalizedEvent.supersedes_event_id,
        SourceRecord.source_record_id,
        SourceRecord.source_provider,
        SourceRecord.provider_document_id,
        SourceRecord.provider_revision_key,
        SourceRecord.original_url,
        SourceRecord.published_at.label("source_published_at"),
        SourceRecord.fetched_at.label("source_fetched_at"),
        SourceRecord.lifecycle_status.label("source_lifecycle_status"),
        EventSource.relation_type,
    )


def _event_audit_statement():
    return (
        select(*_event_audit_columns())
        .join(Issuer, Issuer.issuer_id == NormalizedEvent.issuer_id)
        .join(EventSource, EventSource.event_id == NormalizedEvent.event_id)
        .join(
            SourceRecord,
            SourceRecord.source_record_id == EventSource.source_record_id,
        )
    )


def build_event_history_statement(event_family_key: str, *, limit: int = 50):
    canonical_key = _required_identifier(
        event_family_key, field_name="event_family_key", max_length=191
    )
    bounded = _bounded_limit(limit)
    return (
        _event_audit_statement()
        .where(NormalizedEvent.event_family_key == canonical_key)
        .order_by(
            NormalizedEvent.event_version.desc(),
            SourceRecord.published_at.desc(),
            SourceRecord.provider_revision_key.desc(),
        )
        .limit(bounded)
    )


def build_affected_events_statement(
    *, provider: str, document_id: str, limit: int = 100
):
    canonical_provider = _required_identifier(
        provider, field_name="provider", max_length=64
    )
    canonical_document_id = _required_identifier(
        document_id, field_name="document_id", max_length=191
    )
    bounded = _bounded_limit(limit)
    return (
        _event_audit_statement()
        .where(
            SourceRecord.source_provider == canonical_provider,
            SourceRecord.provider_document_id == canonical_document_id,
        )
        .order_by(
            NormalizedEvent.occurred_at.desc(),
            NormalizedEvent.event_version.desc(),
            SourceRecord.provider_revision_key.desc(),
        )
        .limit(bounded)
    )


def _event_view(row) -> EventAuditView:
    return EventAuditView(
        event_id=row["event_id"],
        event_family_key=row["event_family_key"],
        event_version=row["event_version"],
        issuer_id=row["issuer_id"],
        issuer_code=row["issuer_code"],
        legal_name_ja=row["legal_name_ja"],
        title=row["title"],
        event_type=row["event_type"],
        occurred_at=_utc_aware(row["occurred_at"]),
        event_lifecycle_status=row["event_lifecycle_status"],
        supersedes_event_id=row["supersedes_event_id"],
        source_record_id=row["source_record_id"],
        source_provider=row["source_provider"],
        provider_document_id=row["provider_document_id"],
        provider_revision_key=row["provider_revision_key"],
        original_url=row["original_url"],
        source_published_at=_utc_aware(row["source_published_at"]),
        source_fetched_at=_utc_aware(row["source_fetched_at"]),
        source_lifecycle_status=row["source_lifecycle_status"],
        relation_type=row["relation_type"],
    )


async def query_source_health(
    session: AsyncSession,
    *,
    provider: str | None = None,
    limit: int = 50,
) -> tuple[SourceHealthView, ...]:
    rows = (
        await session.execute(
            build_source_health_statement(provider=provider, limit=limit)
        )
    ).mappings().all()
    return tuple(
        SourceHealthView(
            provider=row["provider"],
            status=source_health_status(
                row["last_successful_discovery_at"], row["last_error_at"]
            ),
            cursor=row["cursor"],
            last_successful_discovery_at=_utc_aware(
                row["last_successful_discovery_at"]
            ),
            last_error_at=_utc_aware(row["last_error_at"]),
            last_error_code=row["last_error_code"],
        )
        for row in rows
    )


async def query_event_history(
    session: AsyncSession,
    *,
    event_family_key: str,
    limit: int = 50,
) -> tuple[EventAuditView, ...]:
    rows = (
        await session.execute(
            build_event_history_statement(event_family_key, limit=limit)
        )
    ).mappings().all()
    return tuple(_event_view(row) for row in rows)


async def query_affected_events(
    session: AsyncSession,
    *,
    provider: str,
    document_id: str,
    limit: int = 100,
) -> tuple[EventAuditView, ...]:
    rows = (
        await session.execute(
            build_affected_events_statement(
                provider=provider,
                document_id=document_id,
                limit=limit,
            )
        )
    ).mappings().all()
    return tuple(_event_view(row) for row in rows)


async def list_source_health(
    *, provider: str | None = None, limit: int = 50
) -> tuple[SourceHealthView, ...]:
    factory = get_session_factory()
    async with factory() as session:
        return await query_source_health(session, provider=provider, limit=limit)


async def get_event_history(
    *, event_family_key: str, limit: int = 50
) -> tuple[EventAuditView, ...]:
    factory = get_session_factory()
    async with factory() as session:
        return await query_event_history(
            session,
            event_family_key=event_family_key,
            limit=limit,
        )


async def get_affected_events(
    *, provider: str, document_id: str, limit: int = 100
) -> tuple[EventAuditView, ...]:
    factory = get_session_factory()
    async with factory() as session:
        return await query_affected_events(
            session,
            provider=provider,
            document_id=document_id,
            limit=limit,
        )


__all__ = [
    "EventAuditView",
    "SourceHealthView",
    "build_affected_events_statement",
    "build_event_history_statement",
    "build_source_health_statement",
    "get_affected_events",
    "get_event_history",
    "list_source_health",
    "query_affected_events",
    "query_event_history",
    "query_source_health",
    "source_health_status",
]
