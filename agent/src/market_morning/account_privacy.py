"""Authenticated data export and transactional account-deletion processing."""

from __future__ import annotations

import hashlib
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.market_morning.db import get_session_factory
from src.market_morning.models import (
    AccountDeletionRequest,
    AnalyticsEvent,
    AuditLog,
    DeliveryAttemptRecord,
    EditionEventStateRecord,
    EditionSourceOpenRecord,
    Issuer,
    IssuerResearchNoteRecord,
    JobRecord,
    MorningEditionRecord,
    PrivateBetaInvite,
    User,
    UserConsent,
    WatchlistItem,
    new_id,
    utc_now_naive,
)


class AccountPrivacyError(RuntimeError):
    """Base class for expected export/deletion failures."""


class AccountPrivacyValidationError(AccountPrivacyError):
    pass


class AccountPrivacyUnavailable(AccountPrivacyError):
    pass


class AccountPrivacyRetryable(AccountPrivacyError):
    pass


class AccountDeletionProcessStatus(StrEnum):
    COMPLETED = "completed"
    ALREADY_COMPLETED = "already_completed"


@dataclass(frozen=True, slots=True)
class AccountDataExport:
    schema_version: int
    generated_at: datetime
    data: dict[str, list[dict[str, object]]]


@dataclass(frozen=True, slots=True)
class AccountDeletionProcessResult:
    status: AccountDeletionProcessStatus
    request_id: str
    user_id: str
    completed_at: datetime


def _uuid(value: str, *, field: str) -> str:
    try:
        return str(UUID(value))
    except (ValueError, TypeError, AttributeError) as exc:
        raise AccountPrivacyValidationError(f"{field} must be a UUID") from exc


def _safe_reference(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).strip()
    if not normalized or len(normalized) > 255:
        raise AccountPrivacyValidationError("actor_reference is invalid")
    if any(unicodedata.category(char).startswith("C") for char in normalized):
        raise AccountPrivacyValidationError("actor_reference is invalid")
    return normalized


def build_account_export_statements(*, user_id: str):
    """Build explicit-column export queries; operational hashes are never selected."""
    canonical_user_id = _uuid(user_id, field="user_id")
    return (
        (
            "account",
            select(
                User.user_id,
                User.external_subject,
                User.timezone,
                User.email_opt_in,
                User.account_status,
                User.trial_or_subscription_status,
                User.last_product_activity_at,
                User.created_at,
                User.updated_at,
                User.deleted_at,
            ).where(User.user_id == canonical_user_id),
        ),
        (
            "consents",
            select(
                UserConsent.consent_type,
                UserConsent.consent_version,
                UserConsent.accepted_at,
                UserConsent.revoked_at,
            )
            .where(UserConsent.user_id == canonical_user_id)
            .order_by(UserConsent.accepted_at, UserConsent.consent_id),
        ),
        (
            "watchlist",
            select(
                WatchlistItem.watchlist_item_id,
                Issuer.issuer_code,
                Issuer.legal_name_ja,
                WatchlistItem.user_label,
                WatchlistItem.sort_order,
                WatchlistItem.created_at,
                WatchlistItem.updated_at,
                WatchlistItem.removed_at,
            )
            .join(Issuer, Issuer.issuer_id == WatchlistItem.issuer_id)
            .where(WatchlistItem.user_id == canonical_user_id)
            .order_by(WatchlistItem.created_at, WatchlistItem.watchlist_item_id),
        ),
        (
            "editions",
            select(
                MorningEditionRecord.edition_id,
                MorningEditionRecord.edition_date,
                MorningEditionRecord.edition_version,
                MorningEditionRecord.schema_version,
                MorningEditionRecord.status,
                MorningEditionRecord.payload,
                MorningEditionRecord.generated_at,
                MorningEditionRecord.published_at,
                MorningEditionRecord.created_at,
            )
            .where(MorningEditionRecord.user_id == canonical_user_id)
            .order_by(
                MorningEditionRecord.edition_date,
                MorningEditionRecord.edition_version,
            ),
        ),
        (
            "edition_event_states",
            select(
                EditionEventStateRecord.edition_id,
                EditionEventStateRecord.event_id,
                EditionEventStateRecord.state,
                EditionEventStateRecord.first_read_at,
                EditionEventStateRecord.created_at,
                EditionEventStateRecord.updated_at,
            )
            .where(EditionEventStateRecord.user_id == canonical_user_id)
            .order_by(EditionEventStateRecord.created_at),
        ),
        (
            "source_opens",
            select(
                EditionSourceOpenRecord.edition_id,
                EditionSourceOpenRecord.event_id,
                EditionSourceOpenRecord.source_record_id,
                EditionSourceOpenRecord.opened_at,
            )
            .where(EditionSourceOpenRecord.user_id == canonical_user_id)
            .order_by(EditionSourceOpenRecord.opened_at),
        ),
        (
            "research_notes",
            select(
                IssuerResearchNoteRecord.note_id,
                Issuer.issuer_code,
                Issuer.legal_name_ja,
                IssuerResearchNoteRecord.note_text,
                IssuerResearchNoteRecord.created_at,
                IssuerResearchNoteRecord.updated_at,
            )
            .join(Issuer, Issuer.issuer_id == IssuerResearchNoteRecord.issuer_id)
            .where(IssuerResearchNoteRecord.user_id == canonical_user_id)
            .order_by(IssuerResearchNoteRecord.updated_at),
        ),
        (
            "deliveries",
            select(
                DeliveryAttemptRecord.delivery_attempt_id,
                DeliveryAttemptRecord.edition_date,
                DeliveryAttemptRecord.channel,
                DeliveryAttemptRecord.status,
                DeliveryAttemptRecord.attempt_count,
                DeliveryAttemptRecord.requested_at,
                DeliveryAttemptRecord.sent_at,
                DeliveryAttemptRecord.delivered_at,
                DeliveryAttemptRecord.clicked_at,
                DeliveryAttemptRecord.failed_at,
                DeliveryAttemptRecord.updated_at,
            )
            .where(DeliveryAttemptRecord.user_id == canonical_user_id)
            .order_by(DeliveryAttemptRecord.requested_at),
        ),
        (
            "analytics",
            select(
                AnalyticsEvent.event_name,
                AnalyticsEvent.schema_version,
                AnalyticsEvent.properties,
                AnalyticsEvent.occurred_at,
            )
            .where(AnalyticsEvent.user_id == canonical_user_id)
            .order_by(AnalyticsEvent.occurred_at),
        ),
        (
            "audit_history",
            select(
                AuditLog.action,
                AuditLog.entity_type,
                AuditLog.entity_id,
                AuditLog.details,
                AuditLog.occurred_at,
            )
            .where(AuditLog.actor_user_id == canonical_user_id)
            .order_by(AuditLog.occurred_at),
        ),
        (
            "deletion_requests",
            select(
                AccountDeletionRequest.request_id,
                AccountDeletionRequest.status,
                AccountDeletionRequest.requested_at,
                AccountDeletionRequest.processing_started_at,
                AccountDeletionRequest.completed_at,
                AccountDeletionRequest.updated_at,
            )
            .where(AccountDeletionRequest.user_id == canonical_user_id)
            .order_by(AccountDeletionRequest.requested_at),
        ),
        (
            "private_beta_invites",
            select(
                PrivateBetaInvite.invite_id,
                PrivateBetaInvite.status,
                PrivateBetaInvite.expires_at,
                PrivateBetaInvite.accepted_at,
                PrivateBetaInvite.revoked_at,
                PrivateBetaInvite.created_at,
            )
            .where(PrivateBetaInvite.accepted_by_user_id == canonical_user_id)
            .order_by(PrivateBetaInvite.created_at),
        ),
    )


async def export_account_data(
    session: AsyncSession,
    *,
    user_id: str,
    now: datetime | None = None,
) -> AccountDataExport:
    canonical_user_id = _uuid(user_id, field="user_id")
    user = (
        await session.execute(select(User).where(User.user_id == canonical_user_id))
    ).scalar_one_or_none()
    if user is None or user.deleted_at is not None or user.account_status != "active":
        raise AccountPrivacyUnavailable("active Market Morning user is required")
    data: dict[str, list[dict[str, object]]] = {}
    for section, statement in build_account_export_statements(
        user_id=canonical_user_id
    ):
        rows = (await session.execute(statement)).mappings().all()
        data[section] = [dict(row) for row in rows]
    return AccountDataExport(
        schema_version=1,
        generated_at=now or utc_now_naive(),
        data=data,
    )


def _private_data_removal_statements(*, user_id: str):
    return (
        update(MorningEditionRecord)
        .where(MorningEditionRecord.user_id == user_id)
        .values(supersedes_edition_id=None),
        delete(EditionEventStateRecord).where(
            EditionEventStateRecord.user_id == user_id
        ),
        delete(EditionSourceOpenRecord).where(
            EditionSourceOpenRecord.user_id == user_id
        ),
        delete(IssuerResearchNoteRecord).where(
            IssuerResearchNoteRecord.user_id == user_id
        ),
        delete(DeliveryAttemptRecord).where(DeliveryAttemptRecord.user_id == user_id),
        delete(MorningEditionRecord).where(MorningEditionRecord.user_id == user_id),
        delete(WatchlistItem).where(WatchlistItem.user_id == user_id),
        delete(UserConsent).where(UserConsent.user_id == user_id),
        delete(JobRecord).where(
            func.json_unquote(func.json_extract(JobRecord.payload, "$.user_id"))
            == user_id
        ),
        update(AnalyticsEvent)
        .where(AnalyticsEvent.user_id == user_id)
        .values(user_id=None),
        update(AuditLog)
        .where(AuditLog.actor_user_id == user_id)
        .values(actor_user_id=None),
        update(PrivateBetaInvite)
        .where(PrivateBetaInvite.accepted_by_user_id == user_id)
        .values(accepted_by_user_id=None),
    )


async def process_account_deletion(
    session: AsyncSession,
    *,
    request_id: str,
    actor_reference: str,
    now: datetime | None = None,
) -> AccountDeletionProcessResult:
    canonical_request_id = _uuid(request_id, field="request_id")
    actor = _safe_reference(actor_reference)
    occurred_at = now or utc_now_naive()
    request = (
        await session.execute(
            select(AccountDeletionRequest)
            .where(AccountDeletionRequest.request_id == canonical_request_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if request is None:
        raise AccountPrivacyUnavailable("account deletion request is unavailable")
    if request.status == "completed":
        return AccountDeletionProcessResult(
            status=AccountDeletionProcessStatus.ALREADY_COMPLETED,
            request_id=request.request_id,
            user_id=request.user_id,
            completed_at=request.completed_at or request.updated_at,
        )
    if request.status not in {"pending", "processing"}:
        raise AccountPrivacyUnavailable("account deletion request is unavailable")
    user = (
        await session.execute(
            select(User).where(User.user_id == request.user_id).with_for_update()
        )
    ).scalar_one_or_none()
    if user is None:
        raise AccountPrivacyUnavailable("account deletion user is unavailable")

    running_user_job = (
        await session.execute(
            select(JobRecord.job_id)
            .where(
                func.json_unquote(func.json_extract(JobRecord.payload, "$.user_id"))
                == user.user_id,
                JobRecord.status == "running",
            )
            .limit(1)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if running_user_job is not None:
        raise AccountPrivacyRetryable("user-scoped job is still running")

    request.status = "processing"
    request.processing_started_at = request.processing_started_at or occurred_at
    request.updated_at = occurred_at
    for statement in _private_data_removal_statements(user_id=user.user_id):
        await session.execute(statement)

    pseudonym = hashlib.sha256(
        f"{user.user_id}:{request.request_id}".encode("utf-8")
    ).hexdigest()
    user.external_subject = f"deleted:{pseudonym}"
    user.timezone = "Asia/Tokyo"
    user.email_opt_in = False
    user.account_status = "deleted"
    user.trial_or_subscription_status = "none"
    user.last_product_activity_at = None
    user.updated_at = occurred_at
    user.deleted_at = occurred_at

    request.status = "completed"
    request.completed_at = occurred_at
    request.updated_at = occurred_at
    session.add(
        AuditLog(
            audit_id=new_id(),
            actor_user_id=None,
            action="account_deletion.completed",
            entity_type="account_deletion_request",
            entity_id=request.request_id,
            details={"actor_reference": actor, "status": "completed"},
            occurred_at=occurred_at,
        )
    )
    await session.flush()
    return AccountDeletionProcessResult(
        status=AccountDeletionProcessStatus.COMPLETED,
        request_id=request.request_id,
        user_id=user.user_id,
        completed_at=occurred_at,
    )


async def get_account_data_export(*, user_id: str) -> AccountDataExport:
    factory = get_session_factory()
    async with factory() as session:
        return await export_account_data(session, user_id=user_id)


async def process_account_deletion_request(**kwargs) -> AccountDeletionProcessResult:
    factory = get_session_factory()
    async with factory.begin() as session:
        return await process_account_deletion(session, **kwargs)


__all__ = [
    "AccountDataExport",
    "AccountDeletionProcessResult",
    "AccountDeletionProcessStatus",
    "AccountPrivacyError",
    "AccountPrivacyRetryable",
    "AccountPrivacyUnavailable",
    "AccountPrivacyValidationError",
    "build_account_export_statements",
    "export_account_data",
    "get_account_data_export",
    "process_account_deletion",
    "process_account_deletion_request",
]
