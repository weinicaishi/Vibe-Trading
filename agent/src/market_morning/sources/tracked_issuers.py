"""Read-only resolver for issuer codes that licensed adapters must collect."""

from __future__ import annotations

from typing import Any

from sqlalchemy import distinct, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.market_morning.models import Issuer, User, WatchlistItem

_ELIGIBLE_PRODUCT_STATUSES = ("private_beta", "trialing", "active")


def build_tracked_issuer_codes_statement():
    """Select only active issuer codes, never user or watchlist identities."""

    return (
        select(distinct(Issuer.issuer_code))
        .select_from(WatchlistItem)
        .join(User, User.user_id == WatchlistItem.user_id)
        .join(Issuer, Issuer.issuer_id == WatchlistItem.issuer_id)
        .where(
            WatchlistItem.removed_at.is_(None),
            User.account_status == "active",
            User.deleted_at.is_(None),
            User.trial_or_subscription_status.in_(_ELIGIBLE_PRODUCT_STATUSES),
            Issuer.active_status == "active",
            Issuer.effective_to.is_(None),
        )
        .order_by(Issuer.issuer_code)
    )


class SqlAlchemyTrackedIssuerCodeResolver:
    """Open one short read transaction for each source discovery cycle."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession] | Any,
    ) -> None:
        if session_factory is None:
            raise ValueError("session_factory is required")
        self._session_factory = session_factory

    async def __call__(self) -> tuple[str, ...]:
        async with self._session_factory.begin() as session:
            result = await session.execute(build_tracked_issuer_codes_statement())
            return tuple(result.scalars().all())


__all__ = [
    "SqlAlchemyTrackedIssuerCodeResolver",
    "build_tracked_issuer_codes_statement",
]
