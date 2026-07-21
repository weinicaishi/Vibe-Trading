"""Synthetic source adapter used before external data-rights gates open."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from src.market_morning.sources.base import (
    DiscoveryBatch,
    NormalizedSourceRecord,
    SourceDocumentRef,
    SourceHealth,
    SourceLifecycleStatus,
    SourcePayload,
    SourceValidationError,
)


@dataclass(frozen=True, slots=True)
class FixtureSourceDocument:
    document_id: str
    revision_key: str
    original_url: str
    raw_content: bytes
    title: str
    document_type: str
    published_at: datetime
    lifecycle_status: SourceLifecycleStatus
    issuer_codes: tuple[str, ...]
    evidence_text: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def reference(self) -> SourceDocumentRef:
        return SourceDocumentRef(self.document_id, self.revision_key)


class FixtureSourceAdapter:
    """In-memory adapter that cannot make network calls by construction."""

    def __init__(
        self,
        *,
        provider: str,
        documents: tuple[FixtureSourceDocument, ...],
        fetched_at: datetime,
    ) -> None:
        if not provider.startswith("fixture_"):
            raise SourceValidationError("fixture provider names must start with fixture_")
        self.provider = provider
        self._documents = documents
        self._by_reference = {
            (document.document_id, document.revision_key): document for document in documents
        }
        if len(self._by_reference) != len(documents):
            raise SourceValidationError("fixture contains duplicate document revisions")
        self._fetched_at = fetched_at

    async def discover(self, cursor: dict[str, Any] | None) -> DiscoveryBatch:
        offset = 0 if cursor is None else cursor.get("offset")
        if not isinstance(offset, int) or offset < 0 or offset > len(self._documents):
            raise SourceValidationError("fixture cursor offset is invalid")
        remaining = self._documents[offset:]
        return DiscoveryBatch(
            documents=tuple(document.reference for document in remaining),
            next_cursor={"offset": len(self._documents)},
        )

    async def fetch(self, document: SourceDocumentRef) -> SourcePayload:
        fixture = self._by_reference.get((document.document_id, document.revision_key))
        if fixture is None:
            raise SourceValidationError("fixture document revision was not discovered")
        return SourcePayload(
            document=document,
            original_url=fixture.original_url,
            fetched_at=self._fetched_at,
            raw_content=fixture.raw_content,
            metadata=dict(fixture.metadata),
        )

    def normalize(self, payload: SourcePayload) -> NormalizedSourceRecord:
        fixture = self._by_reference.get(
            (payload.document.document_id, payload.document.revision_key)
        )
        if fixture is None:
            raise SourceValidationError("fixture payload has no normalization contract")
        return NormalizedSourceRecord.from_payload(
            provider=self.provider,
            payload=payload,
            title=fixture.title,
            document_type=fixture.document_type,
            published_at=fixture.published_at,
            lifecycle_status=fixture.lifecycle_status,
            issuer_codes=fixture.issuer_codes,
            evidence_text=fixture.evidence_text,
            metadata=fixture.metadata,
        )

    async def health(self) -> SourceHealth:
        return SourceHealth(
            ok=True,
            checked_at=self._fetched_at,
            detail_code="synthetic_fixture_ready",
        )


__all__ = ["FixtureSourceAdapter", "FixtureSourceDocument"]
