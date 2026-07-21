"""Read-only source health and event audit query contracts."""

from __future__ import annotations

import importlib.util
from datetime import datetime

import pytest
from sqlalchemy.dialects import mysql


def test_source_query_service_is_isolated_in_market_morning_domain() -> None:
    try:
        spec = importlib.util.find_spec("src.market_morning.source_queries")
    except ModuleNotFoundError:
        spec = None

    assert spec is not None


def _mysql_sql(statement) -> str:
    return str(
        statement.compile(
            dialect=mysql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )


def test_source_health_query_is_bounded_and_provider_scoped() -> None:
    from src.market_morning.source_queries import build_source_health_statement

    sql = _mysql_sql(build_source_health_statement(provider="fixture_tdnet", limit=20))

    assert "FROM mm_source_cursors" in sql
    assert "source_provider = 'fixture_tdnet'" in sql
    assert "LIMIT 20" in sql


def test_event_history_query_joins_issuer_and_auditable_source_links() -> None:
    from src.market_morning.source_queries import build_event_history_statement

    sql = _mysql_sql(build_event_history_statement("family-7203", limit=25))

    assert "FROM mm_normalized_events" in sql
    assert "JOIN mm_issuers" in sql
    assert "JOIN mm_event_sources" in sql
    assert "JOIN mm_source_records" in sql
    assert "event_family_key = 'family-7203'" in sql
    assert "event_version DESC" in sql
    assert "normalized_payload" not in sql


def test_affected_event_query_tracks_all_revisions_of_one_source_document() -> None:
    from src.market_morning.source_queries import build_affected_events_statement

    sql = _mysql_sql(
        build_affected_events_statement(
            provider="fixture_tdnet",
            document_id="TD-7203-001",
            limit=50,
        )
    )

    assert "source_provider = 'fixture_tdnet'" in sql
    assert "provider_document_id = 'TD-7203-001'" in sql
    assert "provider_revision_key" in sql
    assert "lifecycle_status" in sql
    assert "normalized_payload" not in sql


def test_source_health_status_prefers_the_most_recent_outcome() -> None:
    from src.market_morning.source_queries import source_health_status

    success = datetime(2026, 7, 20, 1, 0, 0)
    later_error = datetime(2026, 7, 20, 2, 0, 0)

    assert source_health_status(None, None) == "never_run"
    assert source_health_status(success, None) == "healthy"
    assert source_health_status(success, later_error) == "error"
    assert source_health_status(later_error, success) == "healthy"


@pytest.mark.parametrize(
    ("builder", "args"),
    (
        ("health", {"provider": "x" * 65, "limit": 20}),
        ("history", {"event_family_key": "", "limit": 25}),
        (
            "affected",
            {"provider": "fixture_tdnet", "document_id": "", "limit": 50},
        ),
    ),
)
def test_source_queries_reject_unbounded_or_blank_identifiers(builder: str, args: dict) -> None:
    from src.market_morning.source_queries import (
        build_affected_events_statement,
        build_event_history_statement,
        build_source_health_statement,
    )

    function = {
        "health": build_source_health_statement,
        "history": build_event_history_statement,
        "affected": build_affected_events_statement,
    }[builder]

    with pytest.raises(ValueError):
        function(**args)
