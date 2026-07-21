from __future__ import annotations

import asyncio

from sqlalchemy.dialects import mysql

from src.market_morning.sources.tracked_issuers import (
    SqlAlchemyTrackedIssuerCodeResolver,
    build_tracked_issuer_codes_statement,
)


def test_tracked_issuer_query_filters_inactive_product_and_issuer_state() -> None:
    sql = str(
        build_tracked_issuer_codes_statement().compile(
            dialect=mysql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    assert "DISTINCT mm_issuers.issuer_code" in sql
    assert "mm_watchlist_items.removed_at IS NULL" in sql
    assert "mm_users.account_status = 'active'" in sql
    assert "mm_users.deleted_at IS NULL" in sql
    assert "mm_users.trial_or_subscription_status IN ('private_beta', 'trialing', 'active')" in sql
    assert "mm_issuers.active_status = 'active'" in sql
    assert "mm_issuers.effective_to IS NULL" in sql
    assert "user_id" not in sql.split("FROM", 1)[0]


def test_tracked_issuer_resolver_uses_one_short_read_transaction() -> None:
    calls: list[str] = []

    class Scalars:
        def all(self):
            return ["6758", "7203"]

    class Result:
        def scalars(self):
            return Scalars()

    class Session:
        async def execute(self, statement):
            calls.append("execute")
            return Result()

    class Context:
        async def __aenter__(self):
            calls.append("begin")
            return Session()

        async def __aexit__(self, exc_type, exc, traceback):
            calls.append("end")

    class Factory:
        def begin(self):
            return Context()

    resolver = SqlAlchemyTrackedIssuerCodeResolver(Factory())
    assert asyncio.run(resolver()) == ("6758", "7203")
    assert calls == ["begin", "execute", "end"]
