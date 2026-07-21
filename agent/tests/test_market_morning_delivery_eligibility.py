"""Fail-closed eligibility for Market Morning reminder delivery."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.dialects import mysql

NOW = datetime(2026, 7, 21, 0, 0, tzinfo=timezone.utc)


def test_delivery_eligibility_query_requires_activity_opt_in_and_three_issuers() -> None:
    from src.market_morning.delivery_eligibility import (
        build_delivery_eligible_users_statement,
    )

    statement = build_delivery_eligible_users_statement(as_of=NOW, limit=250)
    compiled = statement.compile(
        dialect=mysql.dialect(),
        compile_kwargs={"literal_binds": True},
    )
    sql = str(compiled)

    assert "mm_users.email_opt_in IS true" in sql
    assert "mm_users.account_status = 'active'" in sql
    assert "mm_users.deleted_at IS NULL" in sql
    assert "mm_users.last_product_activity_at >= '2026-07-14 00:00:00'" in sql
    assert "mm_watchlist_items.removed_at IS NULL" in sql
    assert "mm_issuers.active_status = 'active'" in sql
    assert "mm_issuers.effective_to IS NULL" in sql
    assert "count(DISTINCT mm_watchlist_items.issuer_id) >= 3" in sql
    assert "LIMIT 250" in sql


def test_delivery_eligibility_validates_time_and_limit() -> None:
    from src.market_morning.delivery_eligibility import (
        build_delivery_eligible_users_statement,
    )

    with pytest.raises(ValueError, match="timezone-aware"):
        build_delivery_eligible_users_statement(as_of=NOW.replace(tzinfo=None))
    with pytest.raises(ValueError, match="limit"):
        build_delivery_eligible_users_statement(as_of=NOW, limit=0)


def test_load_delivery_eligible_users_returns_bounded_identity_contract() -> None:
    from src.market_morning.delivery_eligibility import (
        DeliveryEligibleUser,
        load_delivery_eligible_users,
    )

    class Result:
        def mappings(self):
            return self

        def all(self):
            return [
                {
                    "user_id": "11111111-1111-4111-8111-111111111111",
                    "timezone": "Asia/Tokyo",
                    "active_issuer_count": 4,
                    "last_product_activity_at": NOW.replace(tzinfo=None),
                }
            ]

    class Session:
        async def execute(self, statement):
            return Result()

    users = asyncio.run(
        load_delivery_eligible_users(Session(), as_of=NOW, limit=100)
    )

    assert users == (
        DeliveryEligibleUser(
            user_id="11111111-1111-4111-8111-111111111111",
            timezone="Asia/Tokyo",
            active_issuer_count=4,
            last_product_activity_at=NOW,
        ),
    )
    assert users[0].last_product_activity_at >= NOW - timedelta(days=7)
