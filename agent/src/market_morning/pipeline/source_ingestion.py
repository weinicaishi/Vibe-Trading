"""Collect a complete source batch before any durable cursor is advanced."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Protocol, TypeVar

from src.market_morning.sources.base import NormalizedSourceRecord, SourceAdapter, SourceValidationError


@dataclass(frozen=True, slots=True)
class CollectedSourceBatch:
    provider: str
    previous_cursor: dict[str, Any] | None
    next_cursor: dict[str, Any] | None
    records: tuple[NormalizedSourceRecord, ...]


@dataclass(frozen=True, slots=True)
class SourceRevisionIdentity:
    provider: str
    document_id: str
    revision_key: str


@dataclass(frozen=True, slots=True)
class PlannedSourceRecord:
    action: Literal["insert", "duplicate"]
    identity: SourceRevisionIdentity
    record: NormalizedSourceRecord
    supersedes_identity: SourceRevisionIdentity | None


@dataclass(frozen=True, slots=True)
class SourcePersistencePlan:
    provider: str
    previous_cursor: dict[str, Any] | None
    next_cursor: dict[str, Any] | None
    items: tuple[PlannedSourceRecord, ...]

    @property
    def insert_count(self) -> int:
        return sum(item.action == "insert" for item in self.items)

    @property
    def duplicate_count(self) -> int:
        return sum(item.action == "duplicate" for item in self.items)


RepositoryResult = TypeVar("RepositoryResult")


class SourceBatchRepository(Protocol[RepositoryResult]):
    async def load_cursor(self, provider: str) -> dict[str, Any] | None: ...

    async def load_revision_identities(
        self,
        provider: str,
        document_ids: tuple[str, ...],
    ) -> tuple[SourceRevisionIdentity, ...]: ...

    async def apply(self, plan: SourcePersistencePlan) -> RepositoryResult: ...


async def collect_source_batch(
    adapter: SourceAdapter,
    cursor: dict[str, Any] | None,
) -> CollectedSourceBatch:
    """Fetch and normalize the full discovery page without persisting state.

    If any document fails, the function raises before returning a next cursor.
    The caller can therefore persist the returned records and cursor in one
    transaction, while a failed fetch leaves the previous durable cursor intact.
    """
    discovery = await adapter.discover(cursor)
    records: list[NormalizedSourceRecord] = []
    for document in discovery.documents:
        payload = await adapter.fetch(document)
        if payload.document != document:
            raise SourceValidationError("fetched payload does not match the discovered document")
        record = adapter.normalize(payload)
        if record.provider != adapter.provider:
            raise SourceValidationError("normalized record provider does not match adapter")
        if (record.document_id, record.revision_key) != (
            document.document_id,
            document.revision_key,
        ):
            raise SourceValidationError("normalized record does not match the discovered revision")
        records.append(record)
    return CollectedSourceBatch(
        provider=adapter.provider,
        previous_cursor=cursor,
        next_cursor=discovery.next_cursor,
        records=tuple(records),
    )


def build_source_persistence_plan(
    batch: CollectedSourceBatch,
    *,
    existing_revisions: tuple[SourceRevisionIdentity, ...],
) -> SourcePersistencePlan:
    """Plan append-only revision writes before the repository opens a transaction.

    ``existing_revisions`` must be ordered from oldest to newest per document.
    Exact revision identities are duplicates. New revisions chain to the most
    recent stored or earlier-in-this-batch revision; no previous row is updated.
    """
    known: set[SourceRevisionIdentity] = set()
    latest_by_document: dict[str, SourceRevisionIdentity] = {}
    for identity in existing_revisions:
        if identity.provider != batch.provider:
            raise SourceValidationError("revision history provider does not match batch")
        known.add(identity)
        latest_by_document[identity.document_id] = identity

    items: list[PlannedSourceRecord] = []
    for record in batch.records:
        identity = SourceRevisionIdentity(
            provider=record.provider,
            document_id=record.document_id,
            revision_key=record.revision_key,
        )
        if identity.provider != batch.provider:
            raise SourceValidationError("record provider does not match batch")
        if identity in known:
            items.append(
                PlannedSourceRecord(
                    action="duplicate",
                    identity=identity,
                    record=record,
                    supersedes_identity=None,
                )
            )
            continue
        previous = latest_by_document.get(identity.document_id)
        items.append(
            PlannedSourceRecord(
                action="insert",
                identity=identity,
                record=record,
                supersedes_identity=previous,
            )
        )
        known.add(identity)
        latest_by_document[identity.document_id] = identity

    return SourcePersistencePlan(
        provider=batch.provider,
        previous_cursor=batch.previous_cursor,
        next_cursor=batch.next_cursor,
        items=tuple(items),
    )


async def run_source_ingestion(
    adapter: SourceAdapter,
    repository: SourceBatchRepository[RepositoryResult],
) -> RepositoryResult:
    """Collect completely, then let one repository transaction apply the plan."""
    cursor = await repository.load_cursor(adapter.provider)
    batch = await collect_source_batch(adapter, cursor)
    document_ids = tuple(dict.fromkeys(record.document_id for record in batch.records))
    existing = await repository.load_revision_identities(adapter.provider, document_ids)
    plan = build_source_persistence_plan(batch, existing_revisions=existing)
    return await repository.apply(plan)


__all__ = [
    "CollectedSourceBatch",
    "PlannedSourceRecord",
    "SourceBatchRepository",
    "SourcePersistencePlan",
    "SourceRevisionIdentity",
    "build_source_persistence_plan",
    "collect_source_batch",
    "run_source_ingestion",
]
