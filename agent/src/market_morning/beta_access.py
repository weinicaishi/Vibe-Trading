"""Audited private-beta invitation and account-access lifecycle."""

from __future__ import annotations

import hashlib
import secrets
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.market_morning.db import get_session_factory
from src.market_morning.models import (
    AuditLog,
    PrivateBetaInvite,
    User,
    new_id,
    utc_now_naive,
)
from src.market_morning.session_ledger import revoke_all_sessions_for_subject

_MAX_INVITE_LIFETIME = timedelta(days=30)


class BetaAccessError(RuntimeError):
    """Base class for expected private-beta access failures."""


class BetaAccessValidationError(BetaAccessError):
    pass


class BetaAccessConflict(BetaAccessError):
    pass


class BetaInviteStatus(StrEnum):
    CREATED = "created"
    ACCEPTED = "accepted"
    ALREADY_ACCEPTED = "already_accepted"
    REVOKED = "revoked"
    ALREADY_REVOKED = "already_revoked"


class UserAccessMutationStatus(StrEnum):
    SUSPENDED = "suspended"
    ALREADY_SUSPENDED = "already_suspended"
    REACTIVATED = "reactivated"
    ALREADY_ACTIVE = "already_active"


@dataclass(frozen=True, slots=True)
class BetaInviteCreationResult:
    status: BetaInviteStatus
    invite_id: str
    raw_token: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class BetaInviteMutationResult:
    status: BetaInviteStatus
    invite_id: str
    user_id: str | None
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class UserAccessMutationResult:
    status: UserAccessMutationStatus
    user_id: str
    account_status: str


def _uuid(value: str, *, field: str) -> str:
    try:
        return str(UUID(value))
    except (ValueError, TypeError, AttributeError) as exc:
        raise BetaAccessValidationError(f"{field} must be a UUID") from exc


def _safe_reference(value: str, *, field: str, maximum: int = 255) -> str:
    normalized = unicodedata.normalize("NFKC", value).strip()
    if not normalized or len(normalized) > maximum:
        raise BetaAccessValidationError(f"{field} is invalid")
    if any(unicodedata.category(char).startswith("C") for char in normalized):
        raise BetaAccessValidationError(f"{field} is invalid")
    return normalized


def _token_hash(raw_token: str) -> str:
    if not isinstance(raw_token, str) or not 32 <= len(raw_token) <= 256:
        raise BetaAccessConflict("private beta invitation is unavailable")
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def _audit(
    *,
    action: str,
    entity_type: str,
    entity_id: str,
    details: dict[str, object],
    now: datetime,
    actor_user_id: str | None = None,
) -> AuditLog:
    return AuditLog(
        audit_id=new_id(),
        actor_user_id=actor_user_id,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        details=details,
        occurred_at=now,
    )


async def create_private_beta_invite(
    session: AsyncSession,
    *,
    actor_reference: str,
    expires_at: datetime,
    now: datetime | None = None,
    token_factory: Callable[[], str] | None = None,
) -> BetaInviteCreationResult:
    occurred_at = now or utc_now_naive()
    actor = _safe_reference(actor_reference, field="actor_reference")
    if expires_at <= occurred_at or expires_at - occurred_at > _MAX_INVITE_LIFETIME:
        raise BetaAccessValidationError("invite expiry must be within 30 days")
    raw_token = (token_factory or (lambda: secrets.token_urlsafe(32)))()
    token_sha256 = _token_hash(raw_token)
    invite = PrivateBetaInvite(
        invite_id=new_id(),
        token_sha256=token_sha256,
        status="pending",
        expires_at=expires_at,
        created_by_reference=actor,
        created_at=occurred_at,
        updated_at=occurred_at,
    )
    session.add(invite)
    session.add(
        _audit(
            action="private_beta_invite.created",
            entity_type="private_beta_invite",
            entity_id=invite.invite_id,
            details={
                "actor_reference": actor,
                "expires_at": expires_at.isoformat(),
            },
            now=occurred_at,
        )
    )
    await session.flush()
    return BetaInviteCreationResult(
        status=BetaInviteStatus.CREATED,
        invite_id=invite.invite_id,
        raw_token=raw_token,
        expires_at=expires_at,
    )


async def accept_private_beta_invite(
    session: AsyncSession,
    *,
    raw_token: str,
    external_subject: str,
    now: datetime | None = None,
) -> BetaInviteMutationResult:
    occurred_at = now or utc_now_naive()
    subject = _safe_reference(external_subject, field="external_subject")
    invite = (
        await session.execute(
            select(PrivateBetaInvite).where(PrivateBetaInvite.token_sha256 == _token_hash(raw_token)).with_for_update()
        )
    ).scalar_one_or_none()
    if invite is None:
        raise BetaAccessConflict("private beta invitation is unavailable")
    if invite.status == "accepted":
        accepted_user = (
            await session.execute(select(User).where(User.user_id == invite.accepted_by_user_id).with_for_update())
        ).scalar_one_or_none()
        if accepted_user is None or accepted_user.external_subject != subject:
            raise BetaAccessConflict("private beta invitation is unavailable")
        return BetaInviteMutationResult(
            status=BetaInviteStatus.ALREADY_ACCEPTED,
            invite_id=invite.invite_id,
            user_id=accepted_user.user_id,
            expires_at=invite.expires_at,
        )
    if invite.status != "pending":
        raise BetaAccessConflict("private beta invitation is unavailable")
    if invite.expires_at <= occurred_at:
        invite.status = "expired"
        invite.updated_at = occurred_at
        raise BetaAccessConflict("private beta invitation is unavailable")

    user = (
        await session.execute(select(User).where(User.external_subject == subject).with_for_update())
    ).scalar_one_or_none()
    if user is None:
        user = User(
            user_id=new_id(),
            external_subject=subject,
            timezone="Asia/Tokyo",
            email_opt_in=False,
            account_status="active",
            trial_or_subscription_status="private_beta",
            created_at=occurred_at,
            updated_at=occurred_at,
        )
        session.add(user)
    elif user.deleted_at is not None or user.account_status in {"deleted", "suspended"}:
        raise BetaAccessConflict("private beta account is unavailable")
    else:
        user.account_status = "active"
        user.trial_or_subscription_status = "private_beta"
        user.updated_at = occurred_at

    invite.status = "accepted"
    invite.accepted_by_user_id = user.user_id
    invite.accepted_at = occurred_at
    invite.updated_at = occurred_at
    session.add(
        _audit(
            action="private_beta_invite.accepted",
            entity_type="private_beta_invite",
            entity_id=invite.invite_id,
            details={"user_id": user.user_id},
            now=occurred_at,
            actor_user_id=user.user_id,
        )
    )
    await session.flush()
    return BetaInviteMutationResult(
        status=BetaInviteStatus.ACCEPTED,
        invite_id=invite.invite_id,
        user_id=user.user_id,
        expires_at=invite.expires_at,
    )


async def revoke_private_beta_invite(
    session: AsyncSession,
    *,
    invite_id: str,
    actor_reference: str,
    now: datetime | None = None,
) -> BetaInviteMutationResult:
    canonical_invite_id = _uuid(invite_id, field="invite_id")
    actor = _safe_reference(actor_reference, field="actor_reference")
    occurred_at = now or utc_now_naive()
    invite = (
        await session.execute(
            select(PrivateBetaInvite).where(PrivateBetaInvite.invite_id == canonical_invite_id).with_for_update()
        )
    ).scalar_one_or_none()
    if invite is None:
        raise BetaAccessConflict("private beta invitation is unavailable")
    if invite.status == "revoked":
        return BetaInviteMutationResult(
            status=BetaInviteStatus.ALREADY_REVOKED,
            invite_id=invite.invite_id,
            user_id=invite.accepted_by_user_id,
            expires_at=invite.expires_at,
        )
    if invite.status != "pending":
        raise BetaAccessConflict("private beta invitation cannot be revoked")
    invite.status = "revoked"
    invite.revoked_at = occurred_at
    invite.updated_at = occurred_at
    session.add(
        _audit(
            action="private_beta_invite.revoked",
            entity_type="private_beta_invite",
            entity_id=invite.invite_id,
            details={"actor_reference": actor},
            now=occurred_at,
        )
    )
    await session.flush()
    return BetaInviteMutationResult(
        status=BetaInviteStatus.REVOKED,
        invite_id=invite.invite_id,
        user_id=invite.accepted_by_user_id,
        expires_at=invite.expires_at,
    )


async def set_user_access(
    session: AsyncSession,
    *,
    user_id: str,
    action: str,
    reason_code: str,
    actor_reference: str,
    now: datetime | None = None,
) -> UserAccessMutationResult:
    canonical_user_id = _uuid(user_id, field="user_id")
    if action not in {"suspend", "reactivate"}:
        raise BetaAccessValidationError("unsupported access action")
    reason = _safe_reference(reason_code, field="reason_code", maximum=64)
    actor = _safe_reference(actor_reference, field="actor_reference")
    occurred_at = now or utc_now_naive()
    user = (
        await session.execute(select(User).where(User.user_id == canonical_user_id).with_for_update())
    ).scalar_one_or_none()
    if user is None or user.deleted_at is not None or user.account_status == "deleted":
        raise BetaAccessConflict("private beta account is unavailable")

    if action == "suspend":
        await revoke_all_sessions_for_subject(
            session,
            external_subject=user.external_subject,
            reason="account_suspended",
            now=occurred_at,
        )

    target = "suspended" if action == "suspend" else "active"
    if user.account_status == target:
        return UserAccessMutationResult(
            status=(
                UserAccessMutationStatus.ALREADY_SUSPENDED
                if target == "suspended"
                else UserAccessMutationStatus.ALREADY_ACTIVE
            ),
            user_id=user.user_id,
            account_status=user.account_status,
        )
    if action == "reactivate" and user.account_status != "suspended":
        raise BetaAccessConflict("private beta account transition is unavailable")
    if user.account_status not in {"active", "invited", "suspended"}:
        raise BetaAccessConflict("private beta account transition is unavailable")

    previous_status = user.account_status
    user.account_status = target
    user.updated_at = occurred_at
    if target == "suspended":
        user.email_opt_in = False
        user.last_product_activity_at = None
    session.add(
        _audit(
            action=f"user_access.{action}",
            entity_type="user",
            entity_id=user.user_id,
            details={
                "actor_reference": actor,
                "from_status": previous_status,
                "to_status": target,
                "reason_code": reason,
            },
            now=occurred_at,
        )
    )
    await session.flush()
    return UserAccessMutationResult(
        status=(UserAccessMutationStatus.SUSPENDED if target == "suspended" else UserAccessMutationStatus.REACTIVATED),
        user_id=user.user_id,
        account_status=user.account_status,
    )


async def create_invite(**kwargs) -> BetaInviteCreationResult:
    factory = get_session_factory()
    async with factory.begin() as session:
        return await create_private_beta_invite(session, **kwargs)


async def accept_invite(**kwargs) -> BetaInviteMutationResult:
    factory = get_session_factory()
    async with factory.begin() as session:
        return await accept_private_beta_invite(session, **kwargs)


async def revoke_invite(**kwargs) -> BetaInviteMutationResult:
    factory = get_session_factory()
    async with factory.begin() as session:
        return await revoke_private_beta_invite(session, **kwargs)


async def mutate_user_access(**kwargs) -> UserAccessMutationResult:
    factory = get_session_factory()
    async with factory.begin() as session:
        return await set_user_access(session, **kwargs)


__all__ = [
    "BetaAccessConflict",
    "BetaAccessError",
    "BetaAccessValidationError",
    "BetaInviteCreationResult",
    "BetaInviteMutationResult",
    "BetaInviteStatus",
    "UserAccessMutationResult",
    "UserAccessMutationStatus",
    "accept_private_beta_invite",
    "accept_invite",
    "create_invite",
    "create_private_beta_invite",
    "mutate_user_access",
    "revoke_invite",
    "revoke_private_beta_invite",
    "set_user_access",
]
