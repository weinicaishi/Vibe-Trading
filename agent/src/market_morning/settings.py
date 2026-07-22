"""User settings, disclosure consent, and deletion-request workflows."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.market_morning.db import get_session_factory
from src.market_morning.models import (
    AccountDeletionRequest,
    AuditLog,
    User,
    UserConsent,
    new_id,
    utc_now_naive,
)
from src.market_morning.repositories.jobs import MarketMorningJobType, enqueue_job
from src.market_morning.session_ledger import revoke_all_sessions_for_subject
from src.market_morning.telemetry import AnalyticsEventName, append_analytics_event

MARKET_MORNING_TIMEZONE = "Asia/Tokyo"
_SAFE_CONSENT_VERSION = re.compile(r"[A-Za-z0-9._:-]+")


class SettingsError(RuntimeError):
    """Base class for expected settings failures."""


class SettingsValidationError(SettingsError):
    pass


class SettingsUserUnavailable(SettingsError):
    pass


class ConsentType(StrEnum):
    RISK_DISCLOSURE = "risk_disclosure"
    DATA_DISCLOSURE = "data_disclosure"


class ConsentMutationStatus(StrEnum):
    ACCEPTED = "accepted"
    ALREADY_ACCEPTED = "already_accepted"
    REVOKED = "revoked"
    ALREADY_REVOKED = "already_revoked"


class DeletionRequestStatus(StrEnum):
    REQUESTED = "requested"
    ALREADY_REQUESTED = "already_requested"


@dataclass(frozen=True, slots=True)
class ConsentStateView:
    consent_type: ConsentType
    accepted: bool
    consent_version: str | None
    accepted_at: datetime | None
    revoked_at: datetime | None


@dataclass(frozen=True, slots=True)
class UserSettingsView:
    user_id: str
    timezone: str
    email_opt_in: bool
    risk_disclosure: ConsentStateView
    data_disclosure: ConsentStateView


@dataclass(frozen=True, slots=True)
class ConsentMutationResult:
    status: ConsentMutationStatus
    consent: ConsentStateView


@dataclass(frozen=True, slots=True)
class DeletionRequestResult:
    status: DeletionRequestStatus
    request_id: str
    request_status: str
    requested_at: datetime


def _canonical_uuid(value: str, *, field: str) -> str:
    try:
        return str(UUID(value))
    except (ValueError, AttributeError) as exc:
        raise SettingsValidationError(f"{field} must be a UUID") from exc


def _consent_type(value: ConsentType | str) -> ConsentType:
    try:
        return ConsentType(value)
    except ValueError as exc:
        raise SettingsValidationError("unsupported consent_type") from exc


def _consent_version(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).strip()
    if not normalized or len(normalized) > 64:
        raise SettingsValidationError("consent_version must contain 1 to 64 characters")
    if any(unicodedata.category(char).startswith("C") for char in normalized):
        raise SettingsValidationError("consent_version contains control characters")
    if _SAFE_CONSENT_VERSION.fullmatch(normalized) is None:
        raise SettingsValidationError("consent_version must be a safe version identifier")
    return normalized


async def _active_user(session: AsyncSession, user_id: str, *, lock: bool) -> User:
    statement = select(User).where(User.user_id == user_id)
    if lock:
        statement = statement.with_for_update()
    user = (await session.execute(statement)).scalar_one_or_none()
    if user is None or user.deleted_at is not None or user.account_status != "active":
        raise SettingsUserUnavailable("active Market Morning user is required")
    return user


def _empty_consent(consent_type: ConsentType) -> ConsentStateView:
    return ConsentStateView(
        consent_type=consent_type,
        accepted=False,
        consent_version=None,
        accepted_at=None,
        revoked_at=None,
    )


def _consent_view(row: UserConsent) -> ConsentStateView:
    return ConsentStateView(
        consent_type=ConsentType(row.consent_type),
        accepted=row.revoked_at is None,
        consent_version=row.consent_version,
        accepted_at=row.accepted_at,
        revoked_at=row.revoked_at,
    )


def _audit(
    *,
    user_id: str,
    action: str,
    entity_type: str,
    entity_id: str,
    details: dict[str, object],
    now: datetime,
) -> AuditLog:
    return AuditLog(
        audit_id=new_id(),
        actor_user_id=user_id,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        details=details,
        occurred_at=now,
    )


async def read_user_settings(session: AsyncSession, *, user_id: str) -> UserSettingsView:
    canonical_user_id = _canonical_uuid(user_id, field="user_id")
    user = await _active_user(session, canonical_user_id, lock=False)
    rows = (
        (
            await session.execute(
                select(UserConsent)
                .where(
                    UserConsent.user_id == canonical_user_id,
                    UserConsent.consent_type.in_(tuple(item.value for item in ConsentType)),
                    UserConsent.revoked_at.is_(None),
                )
                .order_by(UserConsent.accepted_at.desc(), UserConsent.consent_id.desc())
            )
        )
        .scalars()
        .all()
    )
    latest: dict[ConsentType, UserConsent] = {}
    for row in rows:
        consent_type = ConsentType(row.consent_type)
        latest.setdefault(consent_type, row)
    return UserSettingsView(
        user_id=user.user_id,
        timezone=user.timezone,
        email_opt_in=user.email_opt_in,
        risk_disclosure=(
            _consent_view(latest[ConsentType.RISK_DISCLOSURE])
            if ConsentType.RISK_DISCLOSURE in latest
            else _empty_consent(ConsentType.RISK_DISCLOSURE)
        ),
        data_disclosure=(
            _consent_view(latest[ConsentType.DATA_DISCLOSURE])
            if ConsentType.DATA_DISCLOSURE in latest
            else _empty_consent(ConsentType.DATA_DISCLOSURE)
        ),
    )


async def update_user_settings(
    session: AsyncSession,
    *,
    user_id: str,
    timezone: str | None = None,
    email_opt_in: bool | None = None,
    now: datetime | None = None,
) -> User:
    canonical_user_id = _canonical_uuid(user_id, field="user_id")
    if timezone is None and email_opt_in is None:
        raise SettingsValidationError("at least one setting must be supplied")
    if timezone is not None and timezone != MARKET_MORNING_TIMEZONE:
        raise SettingsValidationError("timezone must be Asia/Tokyo for the MVP")

    occurred_at = now or utc_now_naive()
    user = await _active_user(session, canonical_user_id, lock=True)
    changed: dict[str, object] = {}
    if timezone is not None and user.timezone != timezone:
        user.timezone = timezone
        changed["timezone"] = timezone
    if email_opt_in is not None and user.email_opt_in != email_opt_in:
        user.email_opt_in = email_opt_in
        changed["email_opt_in"] = email_opt_in
    if not changed:
        return user

    user.updated_at = occurred_at
    session.add(
        _audit(
            user_id=canonical_user_id,
            action="settings.updated",
            entity_type="user",
            entity_id=canonical_user_id,
            details={"changed_fields": sorted(changed)},
            now=occurred_at,
        )
    )
    append_analytics_event(
        session,
        AnalyticsEventName.SETTINGS_UPDATED,
        user_id=canonical_user_id,
        properties=changed,
        occurred_at=occurred_at,
    )
    await session.flush()
    return user


async def set_consent_acceptance(
    session: AsyncSession,
    *,
    user_id: str,
    consent_type: ConsentType | str,
    consent_version: str,
    accepted: bool,
    now: datetime | None = None,
) -> ConsentMutationResult:
    canonical_user_id = _canonical_uuid(user_id, field="user_id")
    canonical_type = _consent_type(consent_type)
    canonical_version = _consent_version(consent_version)
    occurred_at = now or utc_now_naive()
    await _active_user(session, canonical_user_id, lock=True)
    row = (
        await session.execute(
            select(UserConsent)
            .where(
                UserConsent.user_id == canonical_user_id,
                UserConsent.consent_type == canonical_type.value,
                UserConsent.consent_version == canonical_version,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()

    if accepted and row is not None and row.revoked_at is None:
        return ConsentMutationResult(
            status=ConsentMutationStatus.ALREADY_ACCEPTED,
            consent=_consent_view(row),
        )
    if not accepted and (row is None or row.revoked_at is not None):
        state = _consent_view(row) if row else _empty_consent(canonical_type)
        return ConsentMutationResult(
            status=ConsentMutationStatus.ALREADY_REVOKED,
            consent=state,
        )

    if row is None:
        row = UserConsent(
            consent_id=new_id(),
            user_id=canonical_user_id,
            consent_type=canonical_type.value,
            consent_version=canonical_version,
            accepted_at=occurred_at,
            revoked_at=None,
        )
        session.add(row)
    elif accepted:
        row.accepted_at = occurred_at
        row.revoked_at = None
    else:
        row.revoked_at = occurred_at

    mutation_status = ConsentMutationStatus.ACCEPTED if accepted else ConsentMutationStatus.REVOKED
    session.add(
        _audit(
            user_id=canonical_user_id,
            action=f"consent.{mutation_status.value}",
            entity_type="user_consent",
            entity_id=row.consent_id,
            details={
                "consent_type": canonical_type.value,
                "consent_version": canonical_version,
            },
            now=occurred_at,
        )
    )
    append_analytics_event(
        session,
        AnalyticsEventName.CONSENT_RECORDED,
        user_id=canonical_user_id,
        properties={
            "consent_type": canonical_type.value,
            "consent_version": canonical_version,
            "status": mutation_status.value,
        },
        occurred_at=occurred_at,
    )
    await session.flush()
    return ConsentMutationResult(
        status=mutation_status,
        consent=_consent_view(row),
    )


async def create_account_deletion_request(
    session: AsyncSession,
    *,
    user_id: str,
    now: datetime | None = None,
) -> DeletionRequestResult:
    canonical_user_id = _canonical_uuid(user_id, field="user_id")
    occurred_at = now or utc_now_naive()
    user = (
        await session.execute(select(User).where(User.user_id == canonical_user_id).with_for_update())
    ).scalar_one_or_none()
    if user is None or user.deleted_at is not None or user.account_status not in {"active", "deletion_pending"}:
        raise SettingsUserUnavailable("active Market Morning user is required")
    await revoke_all_sessions_for_subject(
        session,
        external_subject=user.external_subject,
        reason="account_deletion_requested",
        now=occurred_at,
    )
    existing = (
        await session.execute(
            select(AccountDeletionRequest)
            .where(
                AccountDeletionRequest.user_id == canonical_user_id,
                AccountDeletionRequest.status.in_(("pending", "processing")),
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if existing is not None:
        user.account_status = "deletion_pending"
        user.email_opt_in = False
        user.last_product_activity_at = None
        user.updated_at = occurred_at
        return DeletionRequestResult(
            status=DeletionRequestStatus.ALREADY_REQUESTED,
            request_id=existing.request_id,
            request_status=existing.status,
            requested_at=existing.requested_at,
        )
    if user.account_status != "active":
        raise SettingsUserUnavailable("active Market Morning user is required")

    request = AccountDeletionRequest(
        request_id=new_id(),
        user_id=canonical_user_id,
        status="pending",
        requested_at=occurred_at,
        updated_at=occurred_at,
    )
    session.add(request)
    session.add(
        _audit(
            user_id=canonical_user_id,
            action="account_deletion.requested",
            entity_type="account_deletion_request",
            entity_id=request.request_id,
            details={"status": request.status},
            now=occurred_at,
        )
    )
    append_analytics_event(
        session,
        AnalyticsEventName.ACCOUNT_DELETION_REQUESTED,
        user_id=canonical_user_id,
        properties={"request_status": request.status},
        occurred_at=occurred_at,
    )
    job_time = (
        occurred_at.replace(tzinfo=timezone.utc) if occurred_at.tzinfo is None else occurred_at.astimezone(timezone.utc)
    )
    await enqueue_job(
        session,
        job_type=MarketMorningJobType.ACCOUNT_DELETION,
        idempotency_key=f"account-deletion:{request.request_id}",
        payload={"schema_version": 1, "request_id": request.request_id},
        priority=100,
        available_at=job_time,
        max_attempts=5,
        created_at=job_time,
    )
    user.account_status = "deletion_pending"
    user.email_opt_in = False
    user.last_product_activity_at = None
    user.updated_at = occurred_at
    await session.flush()
    return DeletionRequestResult(
        status=DeletionRequestStatus.REQUESTED,
        request_id=request.request_id,
        request_status=request.status,
        requested_at=request.requested_at,
    )


async def get_settings(*, user_id: str) -> UserSettingsView:
    factory = get_session_factory()
    async with factory() as session:
        return await read_user_settings(session, user_id=user_id)


async def update_settings(**kwargs) -> UserSettingsView:
    factory = get_session_factory()
    async with factory.begin() as session:
        await update_user_settings(session, **kwargs)
        return await read_user_settings(session, user_id=kwargs["user_id"])


async def record_consent(**kwargs) -> ConsentMutationResult:
    factory = get_session_factory()
    async with factory.begin() as session:
        return await set_consent_acceptance(session, **kwargs)


async def request_account_deletion(**kwargs) -> DeletionRequestResult:
    factory = get_session_factory()
    async with factory.begin() as session:
        return await create_account_deletion_request(session, **kwargs)


__all__ = [
    "ConsentMutationResult",
    "ConsentMutationStatus",
    "ConsentStateView",
    "ConsentType",
    "DeletionRequestResult",
    "DeletionRequestStatus",
    "MARKET_MORNING_TIMEZONE",
    "SettingsError",
    "SettingsUserUnavailable",
    "SettingsValidationError",
    "UserSettingsView",
    "create_account_deletion_request",
    "get_settings",
    "read_user_settings",
    "record_consent",
    "request_account_deletion",
    "set_consent_acceptance",
    "update_settings",
    "update_user_settings",
]
