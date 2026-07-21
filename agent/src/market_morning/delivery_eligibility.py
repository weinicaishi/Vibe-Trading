"""Fail-closed audience selection for one Market Morning reminder run."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import distinct, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.market_morning.models import Issuer, User, WatchlistItem

DELIVERY_ACTIVITY_WINDOW = timedelta(days=7)
DELIVERY_MINIMUM_ACTIVE_ISSUERS = 3
DELIVERY_ELIGIBLE_SUBSCRIPTION_STATUSES = (
    "active",
    "private_beta",
    "trialing",
)


def _aware(value: datetime, *, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value


def _utc_naive(value: datetime, *, field_name: str) -> datetime:
    return _aware(value, field_name=field_name).astimezone(timezone.utc).replace(
        tzinfo=None
    )


def _utc_aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _bounded_limit(value: int) -> int:
    if isinstance(value, bool) or not 1 <= value <= 1_000:
        raise ValueError("limit must be between 1 and 1000")
    return value


@dataclass(frozen=True, slots=True)
class DeliveryEligibleUser:
    user_id: str
    timezone: str
    active_issuer_count: int
    last_product_activity_at: datetime

    def __post_init__(self) -> None:
        try:
            canonical_id = str(UUID(self.user_id))
        except (ValueError, AttributeError) as error:
            raise ValueError("user_id must be a UUID") from error
        object.__setattr__(self, "user_id", canonical_id)
        try:
            ZoneInfo(self.timezone)
        except (TypeError, ZoneInfoNotFoundError) as error:
            raise ValueError("timezone must be a valid IANA timezone") from error
        if self.active_issuer_count < DELIVERY_MINIMUM_ACTIVE_ISSUERS:
            raise ValueError("active_issuer_count is below the delivery minimum")
        object.__setattr__(
            self,
            "last_product_activity_at",
            _utc_aware(self.last_product_activity_at),
        )


def build_delivery_eligible_users_statement(
    *,
    as_of: datetime,
    limit: int = 1_000,
):
    """Select only users that satisfy every MVP reminder eligibility gate."""

    current = _utc_naive(as_of, field_name="as_of")
    cutoff = current - DELIVERY_ACTIVITY_WINDOW
    active_issuer_count = func.count(
        distinct(WatchlistItem.issuer_id)
    ).label("active_issuer_count")
    return (
        select(
            User.user_id,
            User.timezone,
            active_issuer_count,
            User.last_product_activity_at,
        )
        .select_from(User)
        .join(WatchlistItem, WatchlistItem.user_id == User.user_id)
        .join(Issuer, Issuer.issuer_id == WatchlistItem.issuer_id)
        .where(
            User.email_opt_in.is_(True),
            User.account_status == "active",
            User.deleted_at.is_(None),
            User.trial_or_subscription_status.in_(
                DELIVERY_ELIGIBLE_SUBSCRIPTION_STATUSES
            ),
            User.last_product_activity_at.is_not(None),
            User.last_product_activity_at >= cutoff,
            WatchlistItem.removed_at.is_(None),
            Issuer.active_status == "active",
            Issuer.effective_to.is_(None),
        )
        .group_by(
            User.user_id,
            User.timezone,
            User.last_product_activity_at,
        )
        .having(active_issuer_count >= DELIVERY_MINIMUM_ACTIVE_ISSUERS)
        .order_by(User.user_id)
        .limit(_bounded_limit(limit))
    )


async def load_delivery_eligible_users(
    session: AsyncSession,
    *,
    as_of: datetime,
    limit: int = 1_000,
) -> tuple[DeliveryEligibleUser, ...]:
    rows = (
        await session.execute(
            build_delivery_eligible_users_statement(as_of=as_of, limit=limit)
        )
    ).mappings().all()
    return tuple(
        DeliveryEligibleUser(
            user_id=row["user_id"],
            timezone=row["timezone"],
            active_issuer_count=row["active_issuer_count"],
            last_product_activity_at=row["last_product_activity_at"],
        )
        for row in rows
    )


__all__ = [
    "DELIVERY_ACTIVITY_WINDOW",
    "DELIVERY_ELIGIBLE_SUBSCRIPTION_STATUSES",
    "DELIVERY_MINIMUM_ACTIVE_ISSUERS",
    "DeliveryEligibleUser",
    "build_delivery_eligible_users_statement",
    "load_delivery_eligible_users",
]
