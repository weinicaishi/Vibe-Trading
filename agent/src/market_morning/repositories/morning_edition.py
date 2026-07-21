"""MySQL read-model repository for immutable user-scoped morning editions."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime, timezone
from enum import StrEnum
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.market_morning.models import MorningEditionRecord, User, new_id
from src.market_morning.pipeline.morning_edition import (
    EditionDayPlan,
    EditionDayStatus,
    EditionSourceCitation,
    EditionStatus,
    FactStatement,
    GenerationBudget,
    InferenceStatement,
    IssuerBrief,
    IssuerBriefStatus,
    MorningEdition,
    OvernightMarketContext,
    StatementKind,
)

EDITION_PAYLOAD_SCHEMA_VERSION = 1


class EditionRepositoryError(RuntimeError):
    """Base class for expected persisted-edition failures."""


class EditionUserUnavailable(EditionRepositoryError):
    pass


class EditionGenerationConflict(EditionRepositoryError):
    pass


class EditionPayloadInvalid(EditionRepositoryError):
    pass


class EditionPublishStatus(StrEnum):
    PUBLISHED = "published"
    ALREADY_PUBLISHED = "already_published"


@dataclass(frozen=True, slots=True)
class PublishedMorningEdition:
    status: EditionPublishStatus
    edition_id: str
    edition_version: int
    edition: MorningEdition
    published_at: datetime


def _canonical_user_id(user_id: str) -> str:
    try:
        return str(UUID(user_id))
    except (ValueError, AttributeError) as error:
        raise ValueError("user_id must be a UUID") from error


def _canonical_generation_key(generation_key: str) -> str:
    normalized = generation_key.strip()
    if not normalized or len(normalized) > 128:
        raise ValueError("generation_key must contain 1 to 128 characters")
    return normalized


def _aware(value: datetime, *, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value


def _mysql_utc(value: datetime, *, field_name: str) -> datetime:
    return _aware(value, field_name=field_name).astimezone(timezone.utc).replace(
        tzinfo=None
    )


def _published_at(value: datetime) -> datetime:
    if value.tzinfo is not None and value.utcoffset() is not None:
        return value.astimezone(timezone.utc)
    return value.replace(tzinfo=timezone.utc)


def encode_morning_edition(edition: MorningEdition) -> dict[str, Any]:
    """Encode the domain object without importing the HTTP response layer."""
    _aware(edition.generated_at, field_name="edition.generated_at")
    return {
        "edition_date": edition.edition_date.isoformat(),
        "generated_at": edition.generated_at.isoformat(),
        "status": edition.status.value,
        "day_plan": {
            "edition_date": edition.day_plan.edition_date.isoformat(),
            "generate": edition.day_plan.generate,
            "status": edition.day_plan.status.value,
            "overnight_context": edition.day_plan.overnight_context.value,
            "reason_code": edition.day_plan.reason_code,
            "us_reason_code": edition.day_plan.us_reason_code,
        },
        "issuers": [
            {
                "issuer_id": issuer.issuer_id,
                "issuer_code": issuer.issuer_code,
                "legal_name_ja": issuer.legal_name_ja,
                "status": issuer.status.value,
                "facts": [
                    {
                        "kind": fact.kind.value,
                        "text": fact.text,
                        "event_id": fact.event_id,
                        "event_family_key": fact.event_family_key,
                        "event_version": fact.event_version,
                        "event_type": fact.event_type,
                        "occurred_at": fact.occurred_at.isoformat(),
                        "lifecycle_status": fact.lifecycle_status,
                        "citations": [
                            {
                                "provider": citation.provider,
                                "document_id": citation.document_id,
                                "revision_key": citation.revision_key,
                                "original_url": citation.original_url,
                                "published_at": citation.published_at.isoformat(),
                            }
                            for citation in fact.citations
                        ],
                    }
                    for fact in issuer.facts
                ],
                "assessment": {
                    "kind": issuer.assessment.kind.value,
                    "text": issuer.assessment.text,
                    "evidence_quality": issuer.assessment.evidence_quality,
                },
                "omitted_event_count": issuer.omitted_event_count,
                "warnings": list(issuer.warnings),
                "error_code": issuer.error_code,
            }
            for issuer in edition.issuers
        ],
        "consumed_event_units": edition.consumed_event_units,
        "budget": {
            "max_issuers": edition.budget.max_issuers,
            "max_events_per_issuer": edition.budget.max_events_per_issuer,
            "max_total_events": edition.budget.max_total_events,
        },
        "budget_exhausted": edition.budget_exhausted,
        "omitted_issuer_count": edition.omitted_issuer_count,
    }


def decode_morning_edition(payload: dict[str, Any]) -> MorningEdition:
    """Restore a validated domain object from the versioned JSON snapshot."""
    try:
        day_payload = payload["day_plan"]
        issuers = tuple(
            IssuerBrief(
                issuer_id=issuer["issuer_id"],
                issuer_code=issuer["issuer_code"],
                legal_name_ja=issuer["legal_name_ja"],
                status=IssuerBriefStatus(issuer["status"]),
                facts=tuple(
                    FactStatement(
                        kind=StatementKind(fact["kind"]),
                        text=fact["text"],
                        event_id=fact["event_id"],
                        event_family_key=fact["event_family_key"],
                        event_version=int(fact["event_version"]),
                        event_type=fact["event_type"],
                        occurred_at=datetime.fromisoformat(fact["occurred_at"]),
                        lifecycle_status=fact["lifecycle_status"],
                        citations=tuple(
                            EditionSourceCitation(
                                provider=citation["provider"],
                                document_id=citation["document_id"],
                                revision_key=citation["revision_key"],
                                original_url=citation["original_url"],
                                published_at=datetime.fromisoformat(
                                    citation["published_at"]
                                ),
                            )
                            for citation in fact["citations"]
                        ),
                    )
                    for fact in issuer["facts"]
                ),
                assessment=InferenceStatement(
                    kind=StatementKind(issuer["assessment"]["kind"]),
                    text=issuer["assessment"]["text"],
                    evidence_quality=issuer["assessment"]["evidence_quality"],
                ),
                omitted_event_count=int(issuer["omitted_event_count"]),
                warnings=tuple(issuer["warnings"]),
                error_code=issuer.get("error_code"),
            )
            for issuer in payload["issuers"]
        )
        budget_payload = payload["budget"]
        return MorningEdition(
            edition_date=date.fromisoformat(payload["edition_date"]),
            generated_at=_aware(
                datetime.fromisoformat(payload["generated_at"]),
                field_name="payload.generated_at",
            ),
            status=EditionStatus(payload["status"]),
            day_plan=EditionDayPlan(
                edition_date=date.fromisoformat(day_payload["edition_date"]),
                generate=bool(day_payload["generate"]),
                status=EditionDayStatus(day_payload["status"]),
                overnight_context=OvernightMarketContext(
                    day_payload["overnight_context"]
                ),
                reason_code=day_payload.get("reason_code"),
                us_reason_code=day_payload.get("us_reason_code"),
            ),
            issuers=issuers,
            consumed_event_units=int(payload["consumed_event_units"]),
            budget=GenerationBudget(
                max_issuers=int(budget_payload["max_issuers"]),
                max_events_per_issuer=int(
                    budget_payload["max_events_per_issuer"]
                ),
                max_total_events=int(budget_payload["max_total_events"]),
            ),
            budget_exhausted=bool(payload["budget_exhausted"]),
            omitted_issuer_count=int(payload["omitted_issuer_count"]),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise EditionPayloadInvalid("morning edition payload is invalid") from error


def morning_edition_payload_sha256(payload: dict[str, Any]) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def build_latest_edition_statement(user_id: str, edition_date: date):
    canonical_user_id = _canonical_user_id(user_id)
    if not isinstance(edition_date, date):
        raise ValueError("edition_date must be a date")
    return (
        select(MorningEditionRecord)
        .where(
            MorningEditionRecord.user_id == canonical_user_id,
            MorningEditionRecord.edition_date == edition_date,
        )
        .order_by(MorningEditionRecord.edition_version.desc())
        .limit(1)
    )


def _published_view(
    record: MorningEditionRecord,
    *,
    status: EditionPublishStatus,
) -> PublishedMorningEdition:
    if record.schema_version != EDITION_PAYLOAD_SCHEMA_VERSION:
        raise EditionPayloadInvalid("morning edition schema version is unsupported")
    payload_hash = morning_edition_payload_sha256(record.payload)
    if payload_hash != record.payload_sha256:
        raise EditionPayloadInvalid("morning edition payload checksum does not match")
    return PublishedMorningEdition(
        status=status,
        edition_id=record.edition_id,
        edition_version=record.edition_version,
        edition=decode_morning_edition(record.payload),
        published_at=_published_at(record.published_at),
    )


async def get_latest_morning_edition(
    session: AsyncSession,
    *,
    user_id: str,
    edition_date: date,
) -> PublishedMorningEdition | None:
    record = (
        await session.execute(build_latest_edition_statement(user_id, edition_date))
    ).scalar_one_or_none()
    if record is None:
        return None
    return _published_view(record, status=EditionPublishStatus.PUBLISHED)


async def publish_morning_edition(
    session: AsyncSession,
    *,
    user_id: str,
    generation_key: str,
    edition: MorningEdition,
    published_at: datetime,
) -> PublishedMorningEdition:
    """Publish once under a user-row lock and retain prior daily revisions."""
    canonical_user_id = _canonical_user_id(user_id)
    canonical_key = _canonical_generation_key(generation_key)
    published_utc = _mysql_utc(published_at, field_name="published_at")
    generated_utc = _mysql_utc(
        edition.generated_at,
        field_name="edition.generated_at",
    )
    payload = encode_morning_edition(edition)
    payload_hash = morning_edition_payload_sha256(payload)

    user = (
        await session.execute(
            select(User)
            .where(User.user_id == canonical_user_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if user is None or user.deleted_at is not None or user.account_status != "active":
        raise EditionUserUnavailable("active Market Morning user is required")

    existing = (
        await session.execute(
            select(MorningEditionRecord).where(
                MorningEditionRecord.user_id == canonical_user_id,
                MorningEditionRecord.generation_key == canonical_key,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        if existing.payload_sha256 != payload_hash:
            raise EditionGenerationConflict(
                "generation_key already belongs to a different edition payload"
            )
        return _published_view(
            existing,
            status=EditionPublishStatus.ALREADY_PUBLISHED,
        )

    latest = (
        await session.execute(
            build_latest_edition_statement(canonical_user_id, edition.edition_date)
        )
    ).scalar_one_or_none()
    edition_version = 1 if latest is None else latest.edition_version + 1
    record = MorningEditionRecord(
        edition_id=new_id(),
        user_id=canonical_user_id,
        edition_date=edition.edition_date,
        edition_version=edition_version,
        generation_key=canonical_key,
        schema_version=EDITION_PAYLOAD_SCHEMA_VERSION,
        status=edition.status.value,
        payload=payload,
        payload_sha256=payload_hash,
        generated_at=generated_utc,
        published_at=published_utc,
        supersedes_edition_id=None if latest is None else latest.edition_id,
        created_at=published_utc,
    )
    session.add(record)
    await session.flush()
    return _published_view(record, status=EditionPublishStatus.PUBLISHED)


__all__ = [
    "EDITION_PAYLOAD_SCHEMA_VERSION",
    "EditionGenerationConflict",
    "EditionPayloadInvalid",
    "EditionPublishStatus",
    "EditionRepositoryError",
    "EditionUserUnavailable",
    "PublishedMorningEdition",
    "build_latest_edition_statement",
    "decode_morning_edition",
    "encode_morning_edition",
    "get_latest_morning_edition",
    "morning_edition_payload_sha256",
    "publish_morning_edition",
]
