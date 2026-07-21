"""Transactional watchlist service for the Market Morning product."""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.market_morning.db import get_session_factory
from src.market_morning.models import (
    AuditLog,
    Issuer,
    User,
    WatchlistItem,
    new_id,
    utc_now_naive,
)
from src.market_morning.telemetry import AnalyticsEventName, append_analytics_event

MAX_ACTIVE_WATCHLIST_ITEMS = 10


class WatchlistError(RuntimeError):
    """Base class for expected watchlist failures."""


class WatchlistValidationError(WatchlistError):
    pass


class WatchlistUserUnavailable(WatchlistError):
    pass


class WatchlistIssuerUnavailable(WatchlistError):
    pass


class WatchlistItemNotFound(WatchlistError):
    pass


class WatchlistLimitReached(WatchlistError):
    def __init__(self, limit: int = MAX_ACTIVE_WATCHLIST_ITEMS) -> None:
        self.limit = limit
        super().__init__(f"watchlist is limited to {limit} active issuers")


class WatchlistMutationStatus(StrEnum):
    ADDED = "added"
    ALREADY_ACTIVE = "already_active"
    UPDATED = "updated"
    REMOVED = "removed"
    ALREADY_REMOVED = "already_removed"


@dataclass(frozen=True, slots=True)
class WatchlistItemView:
    watchlist_item_id: str
    issuer_id: str
    issuer_code: str
    legal_name_ja: str
    market_segment: str
    issuer_status: str
    user_label: str | None
    sort_order: int
    created_at: datetime


@dataclass(frozen=True, slots=True)
class WatchlistMutationResult:
    status: WatchlistMutationStatus
    item: WatchlistItemView | None
    active_count: int
    limit: int = MAX_ACTIVE_WATCHLIST_ITEMS


def normalize_watchlist_label(value: str | None) -> str | None:
    """Normalize a short private label without interpreting its meaning."""
    if value is None:
        return None
    normalized = " ".join(unicodedata.normalize("NFKC", value).split()).strip()
    if not normalized:
        return None
    if len(normalized) > 40:
        raise WatchlistValidationError("watchlist label must not exceed 40 characters")
    if any(unicodedata.category(char).startswith("C") for char in normalized):
        raise WatchlistValidationError("watchlist label contains unsupported control characters")
    return normalized


def _entity_id(value: str, *, field: str) -> str:
    try:
        return str(UUID(value))
    except (ValueError, AttributeError) as exc:
        raise WatchlistValidationError(f"{field} must be a UUID") from exc


async def _active_user(session: AsyncSession, user_id: str, *, lock: bool) -> User:
    statement = select(User).where(User.user_id == user_id)
    if lock:
        statement = statement.with_for_update()
    user = (await session.execute(statement)).scalar_one_or_none()
    if user is None or user.deleted_at is not None or user.account_status != "active":
        raise WatchlistUserUnavailable("active Market Morning user is required")
    return user


async def _lock_active_user(session: AsyncSession, user_id: str) -> User:
    return await _active_user(session, user_id, lock=True)


async def _active_issuer(session: AsyncSession, issuer_id: str) -> Issuer:
    issuer = (
        await session.execute(
            select(Issuer).where(
                Issuer.issuer_id == issuer_id,
                Issuer.active_status == "active",
                Issuer.effective_to.is_(None),
            )
        )
    ).scalar_one_or_none()
    if issuer is None:
        raise WatchlistIssuerUnavailable("active issuer is required")
    return issuer


def _view(item: WatchlistItem, issuer: Issuer) -> WatchlistItemView:
    return WatchlistItemView(
        watchlist_item_id=item.watchlist_item_id,
        issuer_id=issuer.issuer_id,
        issuer_code=issuer.issuer_code,
        legal_name_ja=issuer.legal_name_ja,
        market_segment=issuer.market_segment,
        issuer_status=issuer.active_status,
        user_label=item.user_label,
        sort_order=item.sort_order,
        created_at=item.created_at,
    )


def _audit(*, user_id: str, action: str, item_id: str, issuer_id: str, now: datetime) -> AuditLog:
    return AuditLog(
        audit_id=new_id(),
        actor_user_id=user_id,
        action=action,
        entity_type="watchlist_item",
        entity_id=item_id,
        details={"issuer_id": issuer_id},
        occurred_at=now,
    )


async def add_watchlist_item(
    session: AsyncSession,
    *,
    user_id: str,
    issuer_id: str,
    user_label: str | None = None,
    now: datetime | None = None,
) -> WatchlistMutationResult:
    """Add once under a per-user row lock and enforce the active-item limit."""
    canonical_user_id = _entity_id(user_id, field="user_id")
    canonical_issuer_id = _entity_id(issuer_id, field="issuer_id")
    label = normalize_watchlist_label(user_label)
    occurred_at = now or utc_now_naive()
    await _lock_active_user(session, canonical_user_id)
    issuer = await _active_issuer(session, canonical_issuer_id)

    existing = (
        await session.execute(
            select(WatchlistItem).where(
                WatchlistItem.user_id == canonical_user_id,
                WatchlistItem.issuer_id == canonical_issuer_id,
                WatchlistItem.removed_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    count_and_max = (
        await session.execute(
            select(
                func.count(WatchlistItem.watchlist_item_id),
                func.coalesce(func.max(WatchlistItem.sort_order), -1),
            ).where(
                WatchlistItem.user_id == canonical_user_id,
                WatchlistItem.removed_at.is_(None),
            )
        )
    ).one()
    active_count, max_sort_order = int(count_and_max[0]), int(count_and_max[1])
    if existing is not None:
        return WatchlistMutationResult(
            status=WatchlistMutationStatus.ALREADY_ACTIVE,
            item=_view(existing, issuer),
            active_count=active_count,
        )
    if active_count >= MAX_ACTIVE_WATCHLIST_ITEMS:
        raise WatchlistLimitReached()

    item = WatchlistItem(
        watchlist_item_id=new_id(),
        user_id=canonical_user_id,
        issuer_id=canonical_issuer_id,
        user_label=label,
        sort_order=max_sort_order + 1,
        created_at=occurred_at,
        updated_at=occurred_at,
    )
    session.add(item)
    session.add(
        _audit(
            user_id=canonical_user_id,
            action="watchlist.added",
            item_id=item.watchlist_item_id,
            issuer_id=canonical_issuer_id,
            now=occurred_at,
        )
    )
    append_analytics_event(
        session,
        AnalyticsEventName.WATCHLIST_ADDED,
        user_id=canonical_user_id,
        properties={
            "active_count": active_count + 1,
            "operation_status": WatchlistMutationStatus.ADDED.value,
        },
        occurred_at=occurred_at,
    )
    await session.flush()
    return WatchlistMutationResult(
        status=WatchlistMutationStatus.ADDED,
        item=_view(item, issuer),
        active_count=active_count + 1,
    )


async def remove_watchlist_item(
    session: AsyncSession,
    *,
    user_id: str,
    issuer_id: str,
    now: datetime | None = None,
) -> WatchlistMutationResult:
    canonical_user_id = _entity_id(user_id, field="user_id")
    canonical_issuer_id = _entity_id(issuer_id, field="issuer_id")
    occurred_at = now or utc_now_naive()
    await _lock_active_user(session, canonical_user_id)
    item = (
        await session.execute(
            select(WatchlistItem)
            .where(
                WatchlistItem.user_id == canonical_user_id,
                WatchlistItem.issuer_id == canonical_issuer_id,
                WatchlistItem.removed_at.is_(None),
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    active_count = int(
        (
            await session.execute(
                select(func.count(WatchlistItem.watchlist_item_id)).where(
                    WatchlistItem.user_id == canonical_user_id,
                    WatchlistItem.removed_at.is_(None),
                )
            )
        ).scalar_one()
    )
    if item is None:
        return WatchlistMutationResult(
            status=WatchlistMutationStatus.ALREADY_REMOVED,
            item=None,
            active_count=active_count,
        )

    item.removed_at = occurred_at
    item.updated_at = occurred_at
    session.add(
        _audit(
            user_id=canonical_user_id,
            action="watchlist.removed",
            item_id=item.watchlist_item_id,
            issuer_id=canonical_issuer_id,
            now=occurred_at,
        )
    )
    append_analytics_event(
        session,
        AnalyticsEventName.WATCHLIST_REMOVED,
        user_id=canonical_user_id,
        properties={
            "active_count": max(0, active_count - 1),
            "operation_status": WatchlistMutationStatus.REMOVED.value,
        },
        occurred_at=occurred_at,
    )
    await session.flush()
    return WatchlistMutationResult(
        status=WatchlistMutationStatus.REMOVED,
        item=None,
        active_count=max(0, active_count - 1),
    )


async def update_watchlist_item(
    session: AsyncSession,
    *,
    user_id: str,
    issuer_id: str,
    user_label: str | None,
    sort_order: int,
    now: datetime | None = None,
) -> WatchlistMutationResult:
    canonical_user_id = _entity_id(user_id, field="user_id")
    canonical_issuer_id = _entity_id(issuer_id, field="issuer_id")
    if not 0 <= sort_order < MAX_ACTIVE_WATCHLIST_ITEMS:
        raise WatchlistValidationError("sort_order must be between 0 and 9")
    label = normalize_watchlist_label(user_label)
    occurred_at = now or utc_now_naive()
    await _lock_active_user(session, canonical_user_id)
    issuer = await _active_issuer(session, canonical_issuer_id)
    item = (
        await session.execute(
            select(WatchlistItem)
            .where(
                WatchlistItem.user_id == canonical_user_id,
                WatchlistItem.issuer_id == canonical_issuer_id,
                WatchlistItem.removed_at.is_(None),
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if item is None:
        raise WatchlistItemNotFound("active watchlist item was not found")

    item.user_label = label
    item.sort_order = sort_order
    item.updated_at = occurred_at
    session.add(
        _audit(
            user_id=canonical_user_id,
            action="watchlist.updated",
            item_id=item.watchlist_item_id,
            issuer_id=canonical_issuer_id,
            now=occurred_at,
        )
    )
    active_count = int(
        (
            await session.execute(
                select(func.count(WatchlistItem.watchlist_item_id)).where(
                    WatchlistItem.user_id == canonical_user_id,
                    WatchlistItem.removed_at.is_(None),
                )
            )
        ).scalar_one()
    )
    append_analytics_event(
        session,
        AnalyticsEventName.WATCHLIST_UPDATED,
        user_id=canonical_user_id,
        properties={
            "active_count": active_count,
            "operation_status": WatchlistMutationStatus.UPDATED.value,
        },
        occurred_at=occurred_at,
    )
    await session.flush()
    return WatchlistMutationResult(
        status=WatchlistMutationStatus.UPDATED,
        item=_view(item, issuer),
        active_count=active_count,
    )


async def list_watchlist_items(
    session: AsyncSession, *, user_id: str
) -> tuple[WatchlistItemView, ...]:
    canonical_user_id = _entity_id(user_id, field="user_id")
    await _active_user(session, canonical_user_id, lock=False)
    rows = (
        await session.execute(
            select(WatchlistItem, Issuer)
            .join(Issuer, Issuer.issuer_id == WatchlistItem.issuer_id)
            .where(
                WatchlistItem.user_id == canonical_user_id,
                WatchlistItem.removed_at.is_(None),
            )
            .order_by(
                WatchlistItem.sort_order,
                WatchlistItem.created_at,
                WatchlistItem.watchlist_item_id,
            )
        )
    ).all()
    return tuple(_view(item, issuer) for item, issuer in rows)


async def add_to_watchlist(**kwargs) -> WatchlistMutationResult:
    factory = get_session_factory()
    async with factory.begin() as session:
        return await add_watchlist_item(session, **kwargs)


async def remove_from_watchlist(**kwargs) -> WatchlistMutationResult:
    factory = get_session_factory()
    async with factory.begin() as session:
        return await remove_watchlist_item(session, **kwargs)


async def update_in_watchlist(**kwargs) -> WatchlistMutationResult:
    factory = get_session_factory()
    async with factory.begin() as session:
        return await update_watchlist_item(session, **kwargs)


async def get_watchlist(*, user_id: str) -> tuple[WatchlistItemView, ...]:
    factory = get_session_factory()
    async with factory() as session:
        return await list_watchlist_items(session, user_id=user_id)


__all__ = [
    "MAX_ACTIVE_WATCHLIST_ITEMS",
    "WatchlistError",
    "WatchlistIssuerUnavailable",
    "WatchlistItemNotFound",
    "WatchlistItemView",
    "WatchlistLimitReached",
    "WatchlistMutationResult",
    "WatchlistMutationStatus",
    "WatchlistUserUnavailable",
    "WatchlistValidationError",
    "add_to_watchlist",
    "add_watchlist_item",
    "get_watchlist",
    "list_watchlist_items",
    "normalize_watchlist_label",
    "remove_from_watchlist",
    "remove_watchlist_item",
    "update_in_watchlist",
    "update_watchlist_item",
]
