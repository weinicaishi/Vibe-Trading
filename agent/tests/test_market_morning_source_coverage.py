"""Provider freshness and issuer source-coverage contracts."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.dialects import mysql


AS_OF = datetime(2026, 7, 20, 22, 0, tzinfo=timezone.utc)
ISSUER_ID = "22222222-2222-4222-8222-222222222222"


def _mysql_sql(statement) -> str:
    return str(
        statement.compile(
            dialect=mysql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )


def _policy(*, with_ir: bool = False):
    from src.market_morning.source_coverage import SourceCoveragePolicy

    return SourceCoveragePolicy(
        global_required_providers=("fixture_tdnet", "fixture_edinet"),
        issuer_required_providers=({"7203": ("fixture_company_ir_toyota",)} if with_ir else {}),
        max_staleness=timedelta(minutes=30),
    )


def _snapshot(
    provider: str,
    *,
    success_minutes_ago: int | None,
    error_minutes_ago: int | None = None,
):
    from src.market_morning.source_coverage import SourceCursorSnapshot

    return SourceCursorSnapshot(
        provider=provider,
        last_successful_discovery_at=(
            None if success_minutes_ago is None else AS_OF - timedelta(minutes=success_minutes_ago)
        ),
        last_error_at=(None if error_minutes_ago is None else AS_OF - timedelta(minutes=error_minutes_ago)),
        last_error_code=None if error_minutes_ago is None else "fixture_fetch_failed",
    )


def test_coverage_policy_requires_at_least_one_global_provider() -> None:
    from src.market_morning.source_coverage import SourceCoveragePolicy

    with pytest.raises(ValueError, match="global_required_providers"):
        SourceCoveragePolicy(global_required_providers=())


def test_source_cursor_query_is_bounded_to_required_providers() -> None:
    from src.market_morning.source_coverage import build_source_coverage_statement

    sql = _mysql_sql(build_source_coverage_statement(("fixture_tdnet", "fixture_edinet", "fixture_company_ir_toyota")))

    assert "FROM mm_source_cursors" in sql
    assert "source_provider IN" in sql
    assert "fixture_tdnet" in sql
    assert "fixture_edinet" in sql
    assert "fixture_company_ir_toyota" in sql
    assert "last_successful_discovery_at" in sql
    assert "last_error_at" in sql
    assert "cursor_value" not in sql


def test_all_required_providers_fresh_makes_issuer_complete() -> None:
    from src.market_morning.source_coverage import (
        CoverageIssuer,
        assess_issuer_source_coverage,
    )

    coverage = assess_issuer_source_coverage(
        CoverageIssuer(issuer_id=ISSUER_ID, issuer_code="7203"),
        snapshots={
            "fixture_tdnet": _snapshot("fixture_tdnet", success_minutes_ago=5),
            "fixture_edinet": _snapshot("fixture_edinet", success_minutes_ago=10),
        },
        policy=_policy(),
        as_of=AS_OF,
    )

    assert coverage.status == "complete"
    assert coverage.error_code is None
    assert coverage.required_providers == ("fixture_tdnet", "fixture_edinet")
    assert [item.state for item in coverage.providers] == ["fresh", "fresh"]


def test_one_fresh_and_one_stale_provider_makes_issuer_partial() -> None:
    from src.market_morning.source_coverage import (
        CoverageIssuer,
        assess_issuer_source_coverage,
    )

    coverage = assess_issuer_source_coverage(
        CoverageIssuer(issuer_id=ISSUER_ID, issuer_code="7203"),
        snapshots={
            "fixture_tdnet": _snapshot("fixture_tdnet", success_minutes_ago=5),
            "fixture_edinet": _snapshot("fixture_edinet", success_minutes_ago=31),
        },
        policy=_policy(),
        as_of=AS_OF,
    )

    assert coverage.status == "partial"
    assert coverage.error_code == "source_coverage_incomplete"
    assert [item.state for item in coverage.providers] == ["fresh", "stale"]


def test_no_fresh_provider_makes_issuer_unavailable() -> None:
    from src.market_morning.source_coverage import (
        CoverageIssuer,
        assess_issuer_source_coverage,
    )

    coverage = assess_issuer_source_coverage(
        CoverageIssuer(issuer_id=ISSUER_ID, issuer_code="7203"),
        snapshots={
            "fixture_tdnet": _snapshot("fixture_tdnet", success_minutes_ago=45),
        },
        policy=_policy(),
        as_of=AS_OF,
    )

    assert coverage.status == "unavailable"
    assert coverage.error_code == "source_coverage_unavailable"
    assert [item.state for item in coverage.providers] == ["stale", "never_run"]


def test_newer_error_overrides_an_earlier_success() -> None:
    from src.market_morning.source_coverage import evaluate_provider_coverage

    result = evaluate_provider_coverage(
        _snapshot(
            "fixture_tdnet",
            success_minutes_ago=10,
            error_minutes_ago=2,
        ),
        as_of=AS_OF,
        max_staleness=timedelta(minutes=30),
    )

    assert result.state == "error"
    assert result.detail_code == "fixture_fetch_failed"


def test_company_ir_is_required_only_for_the_configured_issuer() -> None:
    from src.market_morning.source_coverage import (
        CoverageIssuer,
        assess_issuer_source_coverage,
    )

    snapshots = {
        "fixture_tdnet": _snapshot("fixture_tdnet", success_minutes_ago=5),
        "fixture_edinet": _snapshot("fixture_edinet", success_minutes_ago=5),
    }
    toyota = assess_issuer_source_coverage(
        CoverageIssuer(issuer_id=ISSUER_ID, issuer_code="7203"),
        snapshots=snapshots,
        policy=_policy(with_ir=True),
        as_of=AS_OF,
    )
    other = assess_issuer_source_coverage(
        CoverageIssuer(
            issuer_id="33333333-3333-4333-8333-333333333333",
            issuer_code="6758",
        ),
        snapshots=snapshots,
        policy=_policy(with_ir=True),
        as_of=AS_OF,
    )

    assert toyota.status == "partial"
    assert toyota.required_providers[-1] == "fixture_company_ir_toyota"
    assert other.status == "complete"
    assert other.required_providers == ("fixture_tdnet", "fixture_edinet")


class _Mappings:
    def __init__(self, rows):
        self.rows = rows

    def all(self):
        return self.rows


class _Result:
    def __init__(self, rows):
        self.rows = rows

    def mappings(self):
        return _Mappings(self.rows)


class _Session:
    def __init__(self, rows):
        self.rows = rows
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)
        return _Result(self.rows)


def test_loading_coverages_converts_mysql_times_and_accounts_for_missing_rows() -> None:
    from src.market_morning.source_coverage import (
        CoverageIssuer,
        load_issuer_source_coverages,
    )

    session = _Session(
        (
            {
                "provider": "fixture_tdnet",
                "last_successful_discovery_at": datetime(2026, 7, 20, 21, 55),
                "last_error_at": None,
                "last_error_code": None,
            },
        )
    )

    result = asyncio.run(
        load_issuer_source_coverages(
            session,
            issuers=(CoverageIssuer(issuer_id=ISSUER_ID, issuer_code="7203"),),
            policy=_policy(),
            as_of=AS_OF,
        )
    )

    assert result[ISSUER_ID].status == "partial"
    assert result[ISSUER_ID].providers[0].last_successful_discovery_at.tzinfo == timezone.utc
    assert result[ISSUER_ID].providers[1].state == "never_run"


def test_source_failure_upsert_preserves_the_previous_cursor_and_success_time() -> None:
    from src.market_morning.repositories.source_ingestion import (
        build_cursor_failure_upsert_statement,
    )

    statement = build_cursor_failure_upsert_statement(
        provider="fixture_tdnet",
        failed_at=AS_OF,
        error_code="fixture_fetch_failed",
    )
    sql = str(statement.compile(dialect=mysql.dialect()))
    update_clause = sql.split("ON DUPLICATE KEY UPDATE", maxsplit=1)[1]

    assert "last_error_at" in update_clause
    assert "last_error_code" in update_clause
    assert "updated_at" in update_clause
    assert "cursor_value" not in update_clause
    assert "last_successful_discovery_at" not in update_clause


def test_record_source_failure_executes_and_flushes_the_health_update() -> None:
    from src.market_morning.repositories.source_ingestion import record_source_failure

    class Session:
        def __init__(self):
            self.statements = []
            self.flush_count = 0

        async def execute(self, statement):
            self.statements.append(statement)

        async def flush(self):
            self.flush_count += 1

    session = Session()

    asyncio.run(
        record_source_failure(
            session,
            provider="fixture_tdnet",
            failed_at=AS_OF,
            error_code="fixture_fetch_failed",
        )
    )

    assert len(session.statements) == 1
    assert session.flush_count == 1
