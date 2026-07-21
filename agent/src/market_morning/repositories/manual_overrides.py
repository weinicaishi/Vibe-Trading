"""MySQL repository for audited publication halt overrides."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from enum import StrEnum
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.market_morning.calendar.service import PublicationHaltOverride
from src.market_morning.models import AuditLog, ManualOverrideRecord, new_id

PUBLICATION_HALT = "publication_halt"


class PublicationOverrideError(RuntimeError):
    pass


class PublicationOverrideConflict(PublicationOverrideError):
    pass


class OverrideMutationStatus(StrEnum):
    CREATED = "created"
    ALREADY_ACTIVE = "already_active"
    REVOKED = "revoked"
    NOT_FOUND = "not_found"


@dataclass(frozen=True, slots=True)
class OverrideMutationResult:
    status: OverrideMutationStatus
    override: PublicationHaltOverride | None


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


def _edition_date(value: date) -> date:
    if type(value) is not date:
        raise ValueError("edition_date must be a date")
    return value


def _mysql_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _view(record: ManualOverrideRecord) -> PublicationHaltOverride:
    return PublicationHaltOverride(
        override_id=record.override_id,
        edition_date=record.edition_date,
        reason_code=record.reason_code,
    )


def _active_halt_statement(edition_date: date, *, lock: bool):
    statement = select(ManualOverrideRecord).where(
        ManualOverrideRecord.override_type == PUBLICATION_HALT,
        ManualOverrideRecord.active_edition_date == _edition_date(edition_date),
    )
    return statement.with_for_update() if lock else statement


def build_publication_halt_insert_statement(
    *,
    override_id: str,
    edition_date: date,
    reason_code: str,
    actor_reference: str,
    created_at: datetime,
):
    canonical_id = _uuid(override_id, field_name="override_id")
    canonical_reason = _required(reason_code, field_name="reason_code", maximum=64)
    canonical_actor = _required(
        actor_reference,
        field_name="actor_reference",
        maximum=128,
    )
    created = _mysql_utc(created_at)
    statement = mysql_insert(ManualOverrideRecord.__table__).values(
        override_id=canonical_id,
        override_type=PUBLICATION_HALT,
        edition_date=_edition_date(edition_date),
        reason_code=canonical_reason,
        status="active",
        created_by=canonical_actor,
        created_at=created,
        revoked_by=None,
        revoked_at=None,
        updated_at=created,
    )
    return statement.on_duplicate_key_update(
        override_id=ManualOverrideRecord.__table__.c.override_id
    )


def _audit(
    *,
    audit_id: str,
    action: str,
    record: ManualOverrideRecord,
    actor_reference: str,
    occurred_at: datetime,
) -> AuditLog:
    return AuditLog(
        audit_id=audit_id,
        actor_user_id=None,
        action=action,
        entity_type="manual_override",
        entity_id=record.override_id,
        details={
            "actor_reference": actor_reference,
            "edition_date": record.edition_date.isoformat(),
            "reason_code": record.reason_code,
        },
        occurred_at=occurred_at,
    )


async def load_publication_halt(
    session: AsyncSession,
    *,
    edition_date: date,
) -> PublicationHaltOverride | None:
    record = (
        await session.execute(_active_halt_statement(edition_date, lock=False))
    ).scalar_one_or_none()
    return None if record is None else _view(record)


async def create_publication_halt(
    session: AsyncSession,
    *,
    edition_date: date,
    reason_code: str,
    actor_reference: str,
    now: datetime,
) -> OverrideMutationResult:
    canonical_reason = _required(reason_code, field_name="reason_code", maximum=64)
    canonical_actor = _required(
        actor_reference,
        field_name="actor_reference",
        maximum=128,
    )
    occurred_at = _mysql_utc(now)
    candidate_id = new_id()
    await session.execute(
        build_publication_halt_insert_statement(
            override_id=candidate_id,
            edition_date=edition_date,
            reason_code=canonical_reason,
            actor_reference=canonical_actor,
            created_at=now,
        )
    )
    record = (
        await session.execute(_active_halt_statement(edition_date, lock=True))
    ).scalar_one_or_none()
    if record is None:
        raise PublicationOverrideError("publication halt could not be reloaded")
    if record.reason_code != canonical_reason:
        raise PublicationOverrideConflict(
            "an active publication halt already exists with a different reason"
        )
    if record.override_id != candidate_id:
        return OverrideMutationResult(
            status=OverrideMutationStatus.ALREADY_ACTIVE,
            override=_view(record),
        )
    session.add(
        _audit(
            audit_id=new_id(),
            action="publication_halt.created",
            record=record,
            actor_reference=canonical_actor,
            occurred_at=occurred_at,
        )
    )
    await session.flush()
    return OverrideMutationResult(
        status=OverrideMutationStatus.CREATED,
        override=_view(record),
    )


async def revoke_publication_halt(
    session: AsyncSession,
    *,
    edition_date: date,
    actor_reference: str,
    now: datetime,
) -> OverrideMutationResult:
    canonical_actor = _required(
        actor_reference,
        field_name="actor_reference",
        maximum=128,
    )
    occurred_at = _mysql_utc(now)
    record = (
        await session.execute(_active_halt_statement(edition_date, lock=True))
    ).scalar_one_or_none()
    if record is None:
        return OverrideMutationResult(
            status=OverrideMutationStatus.NOT_FOUND,
            override=None,
        )
    view = _view(record)
    record.status = "revoked"
    record.revoked_by = canonical_actor
    record.revoked_at = occurred_at
    record.updated_at = occurred_at
    session.add(
        _audit(
            audit_id=new_id(),
            action="publication_halt.revoked",
            record=record,
            actor_reference=canonical_actor,
            occurred_at=occurred_at,
        )
    )
    await session.flush()
    return OverrideMutationResult(
        status=OverrideMutationStatus.REVOKED,
        override=view,
    )


__all__ = [
    "OverrideMutationResult",
    "OverrideMutationStatus",
    "PublicationOverrideConflict",
    "PublicationOverrideError",
    "build_publication_halt_insert_statement",
    "create_publication_halt",
    "load_publication_halt",
    "revoke_publication_halt",
]
