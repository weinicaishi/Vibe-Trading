"""Source adapter contracts shared by official-source implementations.

The domain contract intentionally knows nothing about an endpoint, API key or
provider-specific response shape.  Real adapters are enabled only after their
data-rights gate is approved; synthetic fixture adapters can implement the
same protocol during T0 development.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol


class SourceValidationError(ValueError):
    """Raised when a provider payload cannot become an auditable record."""


class SourceLifecycleStatus(StrEnum):
    ACTIVE = "active"
    CORRECTED = "corrected"
    WITHDRAWN = "withdrawn"


def _required(value: str, *, field_name: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise SourceValidationError(f"{field_name} is required")
    return normalized


def _aware(value: datetime, *, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise SourceValidationError(f"{field_name} must be timezone-aware")
    return value


@dataclass(frozen=True, slots=True)
class SourceDocumentRef:
    document_id: str
    revision_key: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "document_id", _required(self.document_id, field_name="document_id"))
        object.__setattr__(self, "revision_key", _required(self.revision_key, field_name="revision_key"))


@dataclass(frozen=True, slots=True)
class DiscoveryBatch:
    documents: tuple[SourceDocumentRef, ...]
    next_cursor: dict[str, Any] | None


@dataclass(frozen=True, slots=True)
class SourcePayload:
    document: SourceDocumentRef
    original_url: str
    fetched_at: datetime
    raw_content: bytes
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "original_url", _required(self.original_url, field_name="original URL"))
        _aware(self.fetched_at, field_name="fetched_at")
        if not self.raw_content:
            raise SourceValidationError("source payload content must not be empty")


@dataclass(frozen=True, slots=True)
class NormalizedSourceRecord:
    provider: str
    document_id: str
    revision_key: str
    original_url: str
    title: str
    document_type: str
    published_at: datetime
    fetched_at: datetime
    lifecycle_status: SourceLifecycleStatus
    issuer_codes: tuple[str, ...]
    content_hash_sha256: str
    evidence_text: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for field_name in (
            "provider",
            "document_id",
            "revision_key",
            "title",
            "document_type",
        ):
            object.__setattr__(
                self,
                field_name,
                _required(getattr(self, field_name), field_name=field_name),
            )
        object.__setattr__(
            self,
            "original_url",
            _required(self.original_url, field_name="original URL"),
        )
        _aware(self.published_at, field_name="published_at")
        _aware(self.fetched_at, field_name="fetched_at")
        if len(self.content_hash_sha256) != 64:
            raise SourceValidationError("content_hash_sha256 must be a SHA-256 hex digest")
        if self.evidence_text is not None:
            if not isinstance(self.evidence_text, str):
                raise SourceValidationError("evidence_text must be text when provided")
            evidence_text = " ".join(self.evidence_text.split())
            if not evidence_text:
                evidence_text = None
            elif len(evidence_text) > 50_000:
                raise SourceValidationError(
                    "evidence_text must contain at most 50000 characters"
                )
            object.__setattr__(self, "evidence_text", evidence_text)

    @classmethod
    def from_payload(
        cls,
        *,
        provider: str,
        payload: SourcePayload,
        title: str,
        document_type: str,
        published_at: datetime,
        lifecycle_status: SourceLifecycleStatus,
        issuer_codes: tuple[str, ...],
        evidence_text: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> NormalizedSourceRecord:
        return cls(
            provider=provider,
            document_id=payload.document.document_id,
            revision_key=payload.document.revision_key,
            original_url=payload.original_url,
            title=title,
            document_type=document_type,
            published_at=published_at,
            fetched_at=payload.fetched_at,
            lifecycle_status=lifecycle_status,
            issuer_codes=issuer_codes,
            content_hash_sha256=hashlib.sha256(payload.raw_content).hexdigest(),
            evidence_text=evidence_text,
            metadata=dict(payload.metadata if metadata is None else metadata),
        )


@dataclass(frozen=True, slots=True)
class SourceHealth:
    ok: bool
    checked_at: datetime
    detail_code: str

    def __post_init__(self) -> None:
        _aware(self.checked_at, field_name="checked_at")
        object.__setattr__(
            self,
            "detail_code",
            _required(self.detail_code, field_name="detail_code"),
        )


class SourceAdapter(Protocol):
    provider: str

    async def discover(self, cursor: dict[str, Any] | None) -> DiscoveryBatch: ...

    async def fetch(self, document: SourceDocumentRef) -> SourcePayload: ...

    def normalize(self, payload: SourcePayload) -> NormalizedSourceRecord: ...

    async def health(self) -> SourceHealth: ...


__all__ = [
    "DiscoveryBatch",
    "NormalizedSourceRecord",
    "SourceAdapter",
    "SourceDocumentRef",
    "SourceHealth",
    "SourceLifecycleStatus",
    "SourcePayload",
    "SourceValidationError",
]
