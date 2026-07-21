"""Private interaction state layered over immutable morning-edition snapshots."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.market_morning.db import get_session_factory
from src.market_morning.models import (
    EditionEventStateRecord,
    EditionSourceOpenRecord,
    MorningEditionRecord,
    SourceRecord,
    new_id,
)
from src.market_morning.pipeline.morning_edition import (
    EditionSourceCitation,
    MorningEdition,
)
from src.market_morning.repositories.morning_edition import (
    EDITION_PAYLOAD_SCHEMA_VERSION,
    EditionPayloadInvalid,
    decode_morning_edition,
    morning_edition_payload_sha256,
)
from src.market_morning.telemetry import AnalyticsEventName, append_analytics_event


class EditionEventState(StrEnum):
    READ = "read"
    LATER = "later"
    IRRELEVANT = "irrelevant"


class EditionSourceOpenStatus(StrEnum):
    RECORDED = "recorded"
    ALREADY_RECORDED = "already_recorded"


class EditionInteractionUnavailable(RuntimeError):
    def __init__(self, error_code: str) -> None:
        self.error_code = error_code
        super().__init__(error_code)


@dataclass(frozen=True, slots=True)
class EditionEventStateView:
    edition_id: str
    event_id: str
    state: EditionEventState
    first_read_at: datetime | None
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class EditionSourceOpenResult:
    status: EditionSourceOpenStatus
    source_open_id: str
    original_url: str


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


async def _load_owned_edition(
    session: AsyncSession,
    *,
    user_id: str,
    edition_id: str,
    lock: bool = True,
) -> tuple[MorningEditionRecord, MorningEdition]:
    statement = select(MorningEditionRecord).where(
        MorningEditionRecord.edition_id == edition_id,
        MorningEditionRecord.user_id == user_id,
    )
    if lock:
        statement = statement.with_for_update()
    record = (await session.execute(statement)).scalar_one_or_none()
    if record is None:
        raise EditionInteractionUnavailable("edition_unavailable")
    if (
        record.schema_version != EDITION_PAYLOAD_SCHEMA_VERSION
        or morning_edition_payload_sha256(record.payload) != record.payload_sha256
    ):
        raise EditionPayloadInvalid("morning edition payload is invalid")
    return record, decode_morning_edition(record.payload)


def _event_citations(
    edition: MorningEdition,
    *,
    event_id: str,
) -> tuple[EditionSourceCitation, ...]:
    citations = tuple(
        citation
        for issuer in edition.issuers
        for fact in issuer.facts
        if fact.event_id == event_id
        for citation in fact.citations
    )
    if not citations:
        raise EditionInteractionUnavailable("edition_event_unavailable")
    return citations


def _state_view(record: EditionEventStateRecord) -> EditionEventStateView:
    updated_at = _utc_aware(record.updated_at)
    assert updated_at is not None
    return EditionEventStateView(
        edition_id=record.edition_id,
        event_id=record.event_id,
        state=EditionEventState(record.state),
        first_read_at=_utc_aware(record.first_read_at),
        updated_at=updated_at,
    )


async def set_edition_event_state(
    session: AsyncSession,
    *,
    user_id: str,
    edition_id: str,
    event_id: str,
    state: EditionEventState | str,
    changed_at: datetime,
) -> EditionEventStateView:
    canonical_user = _uuid(user_id, field_name="user_id")
    canonical_edition = _uuid(edition_id, field_name="edition_id")
    canonical_event = _required(event_id, field_name="event_id", maximum=64)
    canonical_state = EditionEventState(state)
    changed = _mysql_utc(changed_at, field_name="changed_at")
    _, edition = await _load_owned_edition(
        session,
        user_id=canonical_user,
        edition_id=canonical_edition,
    )
    _event_citations(edition, event_id=canonical_event)
    record = (
        await session.execute(
            select(EditionEventStateRecord)
            .where(
                EditionEventStateRecord.user_id == canonical_user,
                EditionEventStateRecord.edition_id == canonical_edition,
                EditionEventStateRecord.event_id == canonical_event,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if record is None:
        record = EditionEventStateRecord(
            event_state_id=new_id(),
            user_id=canonical_user,
            edition_id=canonical_edition,
            event_id=canonical_event,
            state=canonical_state.value,
            first_read_at=(
                changed if canonical_state is EditionEventState.READ else None
            ),
            created_at=changed,
            updated_at=changed,
        )
        session.add(record)
    else:
        record.state = canonical_state.value
        if canonical_state is EditionEventState.READ and record.first_read_at is None:
            record.first_read_at = changed
        record.updated_at = changed
    await session.flush()
    return _state_view(record)


async def list_edition_event_states(
    session: AsyncSession,
    *,
    user_id: str,
    edition_id: str,
) -> tuple[EditionEventStateView, ...]:
    canonical_user = _uuid(user_id, field_name="user_id")
    canonical_edition = _uuid(edition_id, field_name="edition_id")
    _, edition = await _load_owned_edition(
        session,
        user_id=canonical_user,
        edition_id=canonical_edition,
        lock=False,
    )
    edition_event_ids = {
        fact.event_id
        for issuer in edition.issuers
        for fact in issuer.facts
    }
    records = (
        await session.execute(
            select(EditionEventStateRecord)
            .where(
                EditionEventStateRecord.user_id == canonical_user,
                EditionEventStateRecord.edition_id == canonical_edition,
            )
            .order_by(EditionEventStateRecord.updated_at.desc())
        )
    ).scalars().all()
    return tuple(
        _state_view(record)
        for record in records
        if record.event_id in edition_event_ids
    )


def build_source_open_insert_statement(
    *,
    source_open_id: str,
    user_id: str,
    edition_id: str,
    event_id: str,
    source_record_id: str,
    request_id: str,
    opened_at: datetime,
):
    statement = mysql_insert(EditionSourceOpenRecord.__table__).values(
        source_open_id=source_open_id,
        user_id=user_id,
        edition_id=edition_id,
        event_id=event_id,
        source_record_id=source_record_id,
        request_id=request_id,
        opened_at=opened_at,
    )
    return statement.on_duplicate_key_update(
        source_open_id=EditionSourceOpenRecord.__table__.c.source_open_id
    )


def _source_kind(provider: str) -> str:
    if provider == "tdnet":
        return "tdnet"
    if provider == "edinet":
        return "edinet"
    if provider.startswith("company_ir"):
        return "issuer_ir"
    return "other"


async def record_edition_source_open(
    session: AsyncSession,
    *,
    user_id: str,
    edition_id: str,
    event_id: str,
    provider: str,
    document_id: str,
    revision_key: str,
    request_id: str,
    opened_at: datetime,
) -> EditionSourceOpenResult:
    canonical_user = _uuid(user_id, field_name="user_id")
    canonical_edition = _uuid(edition_id, field_name="edition_id")
    canonical_request = _uuid(request_id, field_name="request_id")
    canonical_event = _required(event_id, field_name="event_id", maximum=64)
    canonical_provider = _required(provider, field_name="provider", maximum=64)
    canonical_document = _required(
        document_id, field_name="document_id", maximum=191
    )
    canonical_revision = _required(
        revision_key, field_name="revision_key", maximum=128
    )
    opened = _mysql_utc(opened_at, field_name="opened_at")
    _, edition = await _load_owned_edition(
        session,
        user_id=canonical_user,
        edition_id=canonical_edition,
    )
    citations = _event_citations(edition, event_id=canonical_event)
    citation = next(
        (
            item
            for item in citations
            if item.provider == canonical_provider
            and item.document_id == canonical_document
            and item.revision_key == canonical_revision
        ),
        None,
    )
    if citation is None:
        raise EditionInteractionUnavailable("edition_source_unavailable")
    source = (
        await session.execute(
            select(SourceRecord).where(
                SourceRecord.source_provider == canonical_provider,
                SourceRecord.provider_document_id == canonical_document,
                SourceRecord.provider_revision_key == canonical_revision,
            )
        )
    ).scalar_one_or_none()
    if source is None or source.original_url != citation.original_url:
        raise EditionInteractionUnavailable("edition_source_unavailable")

    candidate_id = new_id()
    await session.execute(
        build_source_open_insert_statement(
            source_open_id=candidate_id,
            user_id=canonical_user,
            edition_id=canonical_edition,
            event_id=canonical_event,
            source_record_id=source.source_record_id,
            request_id=canonical_request,
            opened_at=opened,
        )
    )
    record = (
        await session.execute(
            select(EditionSourceOpenRecord).where(
                EditionSourceOpenRecord.user_id == canonical_user,
                EditionSourceOpenRecord.request_id == canonical_request,
            )
        )
    ).scalar_one_or_none()
    if record is None:
        raise EditionInteractionUnavailable("source_open_persistence_failed")
    created = record.source_open_id == candidate_id
    if created:
        append_analytics_event(
            session,
            AnalyticsEventName.SOURCE_OPENED,
            user_id=canonical_user,
            properties={"source_kind": _source_kind(canonical_provider)},
            request_id=canonical_request,
            occurred_at=opened,
        )
        await session.flush()
    return EditionSourceOpenResult(
        status=(
            EditionSourceOpenStatus.RECORDED
            if created
            else EditionSourceOpenStatus.ALREADY_RECORDED
        ),
        source_open_id=record.source_open_id,
        original_url=citation.original_url,
    )


async def update_edition_event_state(
    *,
    user_id: str,
    edition_id: str,
    event_id: str,
    state: EditionEventState | str,
) -> EditionEventStateView:
    factory = get_session_factory()
    async with factory.begin() as session:
        return await set_edition_event_state(
            session,
            user_id=user_id,
            edition_id=edition_id,
            event_id=event_id,
            state=state,
            changed_at=datetime.now(timezone.utc),
        )


async def get_edition_event_states(
    *,
    user_id: str,
    edition_id: str,
) -> tuple[EditionEventStateView, ...]:
    factory = get_session_factory()
    async with factory() as session:
        return await list_edition_event_states(
            session,
            user_id=user_id,
            edition_id=edition_id,
        )


async def open_edition_source(
    *,
    user_id: str,
    edition_id: str,
    event_id: str,
    provider: str,
    document_id: str,
    revision_key: str,
    request_id: str,
) -> EditionSourceOpenResult:
    factory = get_session_factory()
    async with factory.begin() as session:
        return await record_edition_source_open(
            session,
            user_id=user_id,
            edition_id=edition_id,
            event_id=event_id,
            provider=provider,
            document_id=document_id,
            revision_key=revision_key,
            request_id=request_id,
            opened_at=datetime.now(timezone.utc),
        )


__all__ = [
    "EditionEventState",
    "EditionEventStateView",
    "EditionInteractionUnavailable",
    "EditionSourceOpenResult",
    "EditionSourceOpenStatus",
    "build_source_open_insert_statement",
    "get_edition_event_states",
    "list_edition_event_states",
    "open_edition_source",
    "record_edition_source_open",
    "set_edition_event_state",
    "update_edition_event_state",
]
