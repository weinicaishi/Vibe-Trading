"""MySQL persistence adapter for collected official-source batches."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.market_morning.models import (
    EventMergeCandidate,
    EventSource,
    Issuer,
    NormalizedEvent,
    SourceCursor,
    SourceRecord,
    new_id,
    utc_now_naive,
)
from src.market_morning.pipeline.event_normalization import (
    EventCandidateInput,
    build_event_family_key,
    normalize_event_title,
    propose_merge_candidate,
)
from src.market_morning.pipeline.source_ingestion import (
    PlannedSourceRecord,
    SourcePersistencePlan,
    SourceRevisionIdentity,
)
from src.market_morning.sources.base import SourceLifecycleStatus


class SourceCursorConflict(RuntimeError):
    """The durable cursor changed while a batch was collected."""


def _utc_naive(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("source timestamps must be timezone-aware")
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def build_source_record_values(
    item: PlannedSourceRecord,
    *,
    source_record_id: str,
    supersedes_record_id: str | None = None,
    created_at: datetime | None = None,
) -> dict[str, Any]:
    """Map a normalized record to durable columns without raw source bytes."""
    record = item.record
    created = _utc_naive(created_at) if created_at is not None else _utc_naive(record.fetched_at)
    normalized_payload: dict[str, Any] = {
        "issuer_codes": list(record.issuer_codes),
        "metadata": dict(record.metadata),
    }
    if record.evidence_text is not None:
        normalized_payload["evidence_text"] = record.evidence_text
    return {
        "source_record_id": source_record_id,
        "source_provider": record.provider,
        "provider_document_id": record.document_id,
        "provider_revision_key": record.revision_key,
        "original_url": record.original_url,
        "title": record.title,
        "document_type": record.document_type,
        "published_at": _utc_naive(record.published_at),
        "fetched_at": _utc_naive(record.fetched_at),
        "lifecycle_status": record.lifecycle_status.value,
        "content_hash_sha256": record.content_hash_sha256,
        "normalized_payload": normalized_payload,
        "supersedes_record_id": supersedes_record_id,
        "created_at": created,
    }


def build_source_record_upsert_statement(values: dict[str, Any]):
    """Use the database unique key as the final concurrent-idempotency guard."""
    statement = mysql_insert(SourceRecord.__table__).values(**values)
    return statement.on_duplicate_key_update(source_record_id=SourceRecord.__table__.c.source_record_id)


def build_cursor_upsert_statement(
    *,
    provider: str,
    cursor: dict[str, Any] | None,
    completed_at: datetime,
):
    completed = _utc_naive(completed_at) if completed_at.tzinfo is not None else completed_at
    statement = mysql_insert(SourceCursor.__table__).values(
        source_provider=provider,
        cursor_value=cursor,
        last_successful_discovery_at=completed,
        last_error_at=None,
        last_error_code=None,
        created_at=completed,
        updated_at=completed,
    )
    return statement.on_duplicate_key_update(
        cursor_value=statement.inserted.cursor_value,
        last_successful_discovery_at=statement.inserted.last_successful_discovery_at,
        last_error_at=None,
        last_error_code=None,
        updated_at=statement.inserted.updated_at,
    )


def build_cursor_failure_upsert_statement(
    *,
    provider: str,
    failed_at: datetime,
    error_code: str,
):
    """Record health failure without advancing or clearing a durable cursor."""
    canonical_provider = provider.strip()
    canonical_error = error_code.strip()
    if not canonical_provider or len(canonical_provider) > 64:
        raise ValueError("provider must contain 1 to 64 characters")
    if not canonical_error or len(canonical_error) > 64:
        raise ValueError("error_code must contain 1 to 64 characters")
    failed = _utc_naive(failed_at)
    statement = mysql_insert(SourceCursor.__table__).values(
        source_provider=canonical_provider,
        cursor_value=None,
        last_successful_discovery_at=None,
        last_error_at=failed,
        last_error_code=canonical_error,
        created_at=failed,
        updated_at=failed,
    )
    return statement.on_duplicate_key_update(
        last_error_at=statement.inserted.last_error_at,
        last_error_code=statement.inserted.last_error_code,
        updated_at=statement.inserted.updated_at,
    )


async def record_source_failure(
    session: AsyncSession,
    *,
    provider: str,
    failed_at: datetime,
    error_code: str,
) -> None:
    await session.execute(
        build_cursor_failure_upsert_statement(
            provider=provider,
            failed_at=failed_at,
            error_code=error_code,
        )
    )
    await session.flush()


def source_relation_type(status: SourceLifecycleStatus) -> str:
    return {
        SourceLifecycleStatus.ACTIVE: "primary",
        SourceLifecycleStatus.CORRECTED: "correction",
        SourceLifecycleStatus.WITHDRAWN: "withdrawal",
    }[status]


@dataclass(frozen=True, slots=True)
class CreatedEventRevision:
    event_id: str
    event_version: int


@dataclass(frozen=True, slots=True)
class SourceApplyResult:
    inserted_records: int
    duplicate_records: int
    created_events: int
    unresolved_issuer_codes: tuple[str, ...]
    cursor: dict[str, Any] | None
    created_event_revisions: tuple[CreatedEventRevision, ...] = ()


class SqlAlchemySourceBatchRepository:
    """Apply one collected page and its cursor in the caller's transaction."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def load_cursor(self, provider: str) -> dict[str, Any] | None:
        value = (
            await self._session.execute(
                select(SourceCursor.cursor_value).where(SourceCursor.source_provider == provider)
            )
        ).scalar_one_or_none()
        return None if value is None else dict(value)

    async def load_revision_identities(
        self,
        provider: str,
        document_ids: tuple[str, ...],
    ) -> tuple[SourceRevisionIdentity, ...]:
        if not document_ids:
            return ()
        rows = (
            await self._session.execute(
                select(
                    SourceRecord.source_provider,
                    SourceRecord.provider_document_id,
                    SourceRecord.provider_revision_key,
                )
                .where(
                    SourceRecord.source_provider == provider,
                    SourceRecord.provider_document_id.in_(document_ids),
                )
                .order_by(
                    SourceRecord.provider_document_id,
                    SourceRecord.published_at,
                    SourceRecord.created_at,
                )
            )
        ).all()
        return tuple(SourceRevisionIdentity(*row) for row in rows)

    async def _record_id(self, identity: SourceRevisionIdentity) -> str | None:
        return (
            await self._session.execute(
                select(SourceRecord.source_record_id).where(
                    SourceRecord.source_provider == identity.provider,
                    SourceRecord.provider_document_id == identity.document_id,
                    SourceRecord.provider_revision_key == identity.revision_key,
                )
            )
        ).scalar_one_or_none()

    async def _issuer(self, issuer_code: str) -> Issuer | None:
        return (
            await self._session.execute(
                select(Issuer).where(
                    Issuer.issuer_code == issuer_code,
                    Issuer.active_status == "active",
                    Issuer.effective_to.is_(None),
                )
            )
        ).scalar_one_or_none()

    async def _create_event(
        self,
        *,
        item: PlannedSourceRecord,
        source_record_id: str,
        issuer: Issuer,
        now: datetime,
    ) -> NormalizedEvent:
        family_key = build_event_family_key(
            provider=item.record.provider,
            provider_document_id=item.record.document_id,
            issuer_id=issuer.issuer_id,
        )
        latest = (
            await self._session.execute(
                select(NormalizedEvent)
                .where(NormalizedEvent.event_family_key == family_key)
                .order_by(NormalizedEvent.event_version.desc())
                .limit(1)
                .with_for_update()
            )
        ).scalar_one_or_none()
        event = NormalizedEvent(
            event_id=new_id(),
            event_family_key=family_key,
            event_version=1 if latest is None else latest.event_version + 1,
            issuer_id=issuer.issuer_id,
            title=item.record.title,
            normalized_title=normalize_event_title(item.record.title),
            event_type=item.record.document_type,
            occurred_at=_utc_naive(item.record.published_at),
            lifecycle_status=item.record.lifecycle_status.value,
            supersedes_event_id=None if latest is None else latest.event_id,
            created_at=now,
        )
        self._session.add(event)
        self._session.add(
            EventSource(
                event_source_id=new_id(),
                event_id=event.event_id,
                source_record_id=source_record_id,
                relation_type=source_relation_type(item.record.lifecycle_status),
                linked_at=now,
            )
        )
        await self._session.flush()
        await self._queue_merge_candidates(
            event=event,
            source_provider=item.record.provider,
            now=now,
        )
        return event

    async def _queue_merge_candidates(
        self,
        *,
        event: NormalizedEvent,
        source_provider: str,
        now: datetime,
    ) -> None:
        window_start = event.occurred_at - timedelta(hours=24)
        window_end = event.occurred_at + timedelta(hours=24)
        rows = (
            await self._session.execute(
                select(NormalizedEvent, SourceRecord.source_provider)
                .join(EventSource, EventSource.event_id == NormalizedEvent.event_id)
                .join(
                    SourceRecord,
                    SourceRecord.source_record_id == EventSource.source_record_id,
                )
                .where(
                    NormalizedEvent.issuer_id == event.issuer_id,
                    NormalizedEvent.event_id != event.event_id,
                    NormalizedEvent.occurred_at >= window_start,
                    NormalizedEvent.occurred_at <= window_end,
                )
            )
        ).all()
        current = EventCandidateInput(
            event_id=event.event_id,
            issuer_id=event.issuer_id,
            source_provider=source_provider,
            title=event.title,
            occurred_at=event.occurred_at.replace(tzinfo=timezone.utc),
        )
        for other, other_provider in rows:
            proposal = propose_merge_candidate(
                current,
                EventCandidateInput(
                    event_id=other.event_id,
                    issuer_id=other.issuer_id,
                    source_provider=other_provider,
                    title=other.title,
                    occurred_at=other.occurred_at.replace(tzinfo=timezone.utc),
                ),
            )
            if proposal is None:
                continue
            statement = mysql_insert(EventMergeCandidate.__table__).values(
                candidate_id=new_id(),
                left_event_id=proposal.left_event_id,
                right_event_id=proposal.right_event_id,
                reason=proposal.reason,
                title_similarity=proposal.title_similarity,
                time_distance_seconds=proposal.time_distance_seconds,
                review_status=proposal.review_status,
                reviewed_by=None,
                reviewed_at=None,
                created_at=now,
            )
            await self._session.execute(
                statement.on_duplicate_key_update(candidate_id=EventMergeCandidate.__table__.c.candidate_id)
            )

    async def apply(self, plan: SourcePersistencePlan) -> SourceApplyResult:
        durable_cursor = (
            await self._session.execute(
                select(SourceCursor.cursor_value)
                .where(SourceCursor.source_provider == plan.provider)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if durable_cursor != plan.previous_cursor:
            raise SourceCursorConflict(
                "source cursor advanced while the batch was collected"
            )
        identity_to_record_id: dict[SourceRevisionIdentity, str] = {}
        inserted = 0
        duplicates = 0
        created_event_revisions: list[CreatedEventRevision] = []
        unresolved: set[str] = set()
        now = utc_now_naive()

        for item in plan.items:
            if item.action == "duplicate":
                existing_id = await self._record_id(item.identity)
                if existing_id is None:
                    raise RuntimeError("planned duplicate source revision no longer exists")
                identity_to_record_id[item.identity] = existing_id
                duplicates += 1
                continue

            supersedes_id = None
            if item.supersedes_identity is not None:
                supersedes_id = identity_to_record_id.get(item.supersedes_identity)
                if supersedes_id is None:
                    supersedes_id = await self._record_id(item.supersedes_identity)
                if supersedes_id is None:
                    raise RuntimeError("source revision predecessor no longer exists")

            proposed_id = new_id()
            values = build_source_record_values(
                item,
                source_record_id=proposed_id,
                supersedes_record_id=supersedes_id,
            )
            await self._session.execute(build_source_record_upsert_statement(values))
            stored_id = await self._record_id(item.identity)
            if stored_id is None:
                raise RuntimeError("source revision upsert did not produce a durable row")
            identity_to_record_id[item.identity] = stored_id
            if stored_id != proposed_id:
                duplicates += 1
                continue

            inserted += 1
            for issuer_code in item.record.issuer_codes:
                issuer = await self._issuer(issuer_code)
                if issuer is None:
                    unresolved.add(issuer_code)
                    continue
                event = await self._create_event(
                    item=item,
                    source_record_id=stored_id,
                    issuer=issuer,
                    now=now,
                )
                created_event_revisions.append(
                    CreatedEventRevision(
                        event_id=event.event_id,
                        event_version=event.event_version,
                    )
                )

        await self._session.execute(
            build_cursor_upsert_statement(
                provider=plan.provider,
                cursor=plan.next_cursor,
                completed_at=now,
            )
        )
        await self._session.flush()
        return SourceApplyResult(
            inserted_records=inserted,
            duplicate_records=duplicates,
            created_events=len(created_event_revisions),
            unresolved_issuer_codes=tuple(sorted(unresolved)),
            cursor=None if plan.next_cursor is None else dict(plan.next_cursor),
            created_event_revisions=tuple(created_event_revisions),
        )


__all__ = [
    "build_cursor_failure_upsert_statement",
    "build_cursor_upsert_statement",
    "build_source_record_upsert_statement",
    "build_source_record_values",
    "CreatedEventRevision",
    "record_source_failure",
    "SourceApplyResult",
    "SourceCursorConflict",
    "SqlAlchemySourceBatchRepository",
    "source_relation_type",
]
