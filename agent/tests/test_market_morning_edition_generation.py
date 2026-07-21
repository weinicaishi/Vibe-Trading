"""Persisted-event to immutable-edition generation contracts."""

from __future__ import annotations

import asyncio
from collections import deque
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import mysql

from src.market_morning.pipeline.morning_edition import (
    EditionDayInput,
    IssuerBriefStatus,
    build_edition_day_plan,
)


USER_ID = "11111111-1111-4111-8111-111111111111"
ISSUER_ID = "22222222-2222-4222-8222-222222222222"
OTHER_ISSUER_ID = "33333333-3333-4333-8333-333333333333"
GENERATED_AT = datetime(2026, 7, 20, 22, 0, tzinfo=timezone.utc)
PUBLISHED_AT = datetime(2026, 7, 20, 22, 5, tzinfo=timezone.utc)


def _mysql_sql(statement) -> str:
    return str(
        statement.compile(
            dialect=mysql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )


class _Mappings:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _Result:
    def __init__(self, *, scalar=None, rows=()):
        self._scalar = scalar
        self._rows = rows

    def scalar_one_or_none(self):
        return self._scalar

    def mappings(self):
        return _Mappings(self._rows)


class _Session:
    def __init__(self, *results: _Result):
        self.results = deque(results)
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)
        return self.results.popleft()


def _active_user():
    return SimpleNamespace(
        user_id=USER_ID,
        account_status="active",
        deleted_at=None,
    )


def _watchlist_row(*, issuer_id: str = ISSUER_ID, issuer_code: str = "7203", sort_order: int = 0):
    return {
        "issuer_id": issuer_id,
        "issuer_code": issuer_code,
        "legal_name_ja": "トヨタ自動車株式会社",
        "sort_order": sort_order,
    }


def _event_row(
    *,
    event_id: str = "44444444-4444-4444-8444-444444444444",
    source_record_id: str = "55555555-5555-4555-8555-555555555555",
    revision_key: str = "v1",
    original_url: str = "https://example.jp/tdnet/TD-7203-001-v1.pdf",
):
    return {
        "event_id": event_id,
        "event_family_key": "tdnet:TD-7203-001",
        "event_version": 1,
        "issuer_id": ISSUER_ID,
        "title": "2026年3月期 決算短信",
        "event_type": "earnings_release",
        "occurred_at": datetime(2026, 7, 20, 6, 30),
        "event_lifecycle_status": "active",
        "source_record_id": source_record_id,
        "source_provider": "tdnet",
        "provider_document_id": "TD-7203-001",
        "provider_revision_key": revision_key,
        "original_url": original_url,
        "source_published_at": datetime(2026, 7, 20, 6, 30),
        "brief_status": None,
        "brief_review_status": None,
        "brief_payload": None,
        "brief_payload_sha256": None,
    }


def _published_brief_payload(*, source_ids=None, fact_source_ids=None):
    sources = source_ids or ["55555555-5555-4555-8555-555555555555"]
    fact_sources = fact_source_ids or sources
    return {
        "schema_version": 1,
        "status": "published",
        "event_id": "44444444-4444-4444-8444-444444444444",
        "event_version": 1,
        "title": "2026年3月期 決算短信",
        "occurred_at": "2026-07-20T06:30:00+00:00",
        "confirmed_facts": [
            {
                "text": "通期売上高予想を1,200億円へ修正した。",
                "source_ids": list(fact_sources),
            }
        ],
        "open_questions": ["利益率への影響は追加資料の確認が必要。"],
        "source_ids": list(sources),
        "sources": [
            {
                "source_id": source_id,
                "provider": "tdnet",
                "original_url": "https://example.jp/tdnet/source.pdf",
            }
            for source_id in sources
        ],
        "model_version": "fixture-model-v1",
        "review_status": "auto_validated",
        "error_codes": [],
    }


def _open_day():
    return build_edition_day_plan(
        EditionDayInput(
            edition_date=date(2026, 7, 21),
            jp_market_open=True,
            us_previous_session_available=True,
        )
    )


def test_rejected_event_briefs_are_excluded_from_shared_reader_subquery() -> None:
    from src.market_morning.edition_generation import (
        EditionGenerationWindow,
        build_generation_events_statement,
    )
    from src.market_morning.issuer_research import build_issuer_history_statement

    generation_sql = _mysql_sql(
        build_generation_events_statement(
            (ISSUER_ID,),
            window=EditionGenerationWindow.ending_at(GENERATED_AT),
        )
    )
    research_sql = _mysql_sql(build_issuer_history_statement(issuer_id=ISSUER_ID))

    assert "review_status != 'rejected'" in generation_sql
    assert "review_status != 'rejected'" in research_sql


def test_generation_window_is_aware_bounded_and_defaults_to_24_hours() -> None:
    from src.market_morning.edition_generation import EditionGenerationWindow

    window = EditionGenerationWindow.ending_at(GENERATED_AT)

    assert window.starts_at == GENERATED_AT - timedelta(hours=24)
    assert window.ends_at == GENERATED_AT
    assert window.mysql_bounds == (
        datetime(2026, 7, 19, 22, 0),
        datetime(2026, 7, 20, 22, 0),
    )

    with pytest.raises(ValueError, match="timezone-aware"):
        EditionGenerationWindow(
            starts_at=datetime(2026, 7, 20, 0, 0),
            ends_at=GENERATED_AT,
        )
    with pytest.raises(ValueError, match="72 hours"):
        EditionGenerationWindow(
            starts_at=GENERATED_AT - timedelta(hours=73),
            ends_at=GENERATED_AT,
        )


def test_watchlist_query_is_active_user_scoped_ordered_and_bounded() -> None:
    from src.market_morning.edition_generation import (
        build_generation_watchlist_statement,
    )

    sql = _mysql_sql(build_generation_watchlist_statement(USER_ID, limit=10))

    assert "FROM mm_watchlist_items" in sql
    assert "JOIN mm_users" in sql
    assert "JOIN mm_issuers" in sql
    assert f"mm_watchlist_items.user_id = '{USER_ID}'" in sql
    assert "mm_users.account_status = 'active'" in sql
    assert "mm_users.deleted_at IS NULL" in sql
    assert "mm_watchlist_items.removed_at IS NULL" in sql
    assert "mm_issuers.active_status = 'active'" in sql
    assert "mm_issuers.effective_to IS NULL" in sql
    assert "sort_order" in sql and "LIMIT 10" in sql


def test_event_query_is_issuer_and_time_window_scoped_with_source_citations() -> None:
    from src.market_morning.edition_generation import (
        EditionGenerationWindow,
        build_generation_events_statement,
    )

    window = EditionGenerationWindow.ending_at(GENERATED_AT)
    sql = _mysql_sql(
        build_generation_events_statement(
            (ISSUER_ID, OTHER_ISSUER_ID),
            window=window,
            max_source_rows=300,
        )
    )

    assert "FROM mm_normalized_events" in sql
    assert "JOIN mm_event_sources" in sql
    assert "JOIN mm_source_records" in sql
    assert "mm_event_briefs" in sql
    assert "row_number() OVER" in sql
    assert ISSUER_ID in sql and OTHER_ISSUER_ID in sql
    assert "occurred_at >= '2026-07-19 22:00:00'" in sql
    assert "occurred_at <= '2026-07-20 22:00:00'" in sql
    assert "LIMIT 301" in sql
    assert "normalized_payload" not in sql


def test_loader_uses_validated_event_brief_facts_with_fact_level_citations() -> None:
    from src.market_morning.edition_generation import (
        EditionGenerationWindow,
        load_generation_inputs,
    )
    from src.market_morning.repositories.event_briefs import (
        event_brief_payload_sha256,
    )

    first = _event_row()
    second = _event_row(
        source_record_id="66666666-6666-4666-8666-666666666666",
        revision_key="mirror-v1",
        original_url="https://example.jp/ir/TD-7203-001-v1.pdf",
    )
    second["source_provider"] = "company_ir"
    payload = _published_brief_payload(
        source_ids=[
            "55555555-5555-4555-8555-555555555555",
            "66666666-6666-4666-8666-666666666666",
        ],
        fact_source_ids=["55555555-5555-4555-8555-555555555555"],
    )
    for row in (first, second):
        row.update(
            brief_status="published",
            brief_review_status="auto_validated",
            brief_payload=payload,
            brief_payload_sha256=event_brief_payload_sha256(payload),
        )
    session = _Session(
        _Result(scalar=_active_user()),
        _Result(rows=(_watchlist_row(),)),
        _Result(rows=(first, second)),
    )

    inputs = asyncio.run(
        load_generation_inputs(
            session,
            user_id=USER_ID,
            window=EditionGenerationWindow.ending_at(GENERATED_AT),
            collection_status_by_issuer={ISSUER_ID: "complete"},
        )
    )

    event = inputs[0].events[0]
    assert event.title == "2026年3月期 決算短信"
    assert [fact.text for fact in event.facts] == [
        "通期売上高予想を1,200億円へ修正した。"
    ]
    assert [citation.provider for citation in event.facts[0].citations] == [
        "tdnet"
    ]
    assert event.warnings == ()


def test_loader_rejects_tampered_event_brief_and_falls_back_to_official_title() -> None:
    from src.market_morning.edition_generation import (
        EditionGenerationWindow,
        load_generation_inputs,
    )

    row = _event_row()
    row.update(
        brief_status="published",
        brief_review_status="auto_validated",
        brief_payload=_published_brief_payload(),
        brief_payload_sha256="0" * 64,
    )
    session = _Session(
        _Result(scalar=_active_user()),
        _Result(rows=(_watchlist_row(),)),
        _Result(rows=(row,)),
    )

    inputs = asyncio.run(
        load_generation_inputs(
            session,
            user_id=USER_ID,
            window=EditionGenerationWindow.ending_at(GENERATED_AT),
            collection_status_by_issuer={ISSUER_ID: "complete"},
        )
    )

    event = inputs[0].events[0]
    assert event.facts == ()
    assert event.warnings == ("event_brief_invalid",)


def test_generated_edition_marks_event_brief_fallback_as_partial() -> None:
    from src.market_morning.edition_generation import (
        EditionGenerationCommand,
        generate_and_publish_morning_edition,
    )
    from src.market_morning.pipeline.morning_edition import EditionStatus

    row = _event_row()
    session = _Session(
        _Result(scalar=_active_user()),
        _Result(rows=(_watchlist_row(),)),
        _Result(rows=(row,)),
    )

    async def publisher(_session, **kwargs):
        return SimpleNamespace(edition=kwargs["edition"])

    result = asyncio.run(
        generate_and_publish_morning_edition(
            session,
            command=EditionGenerationCommand(
                user_id=USER_ID,
                generation_key="2026-07-21:event-brief-fallback",
                day_plan=_open_day(),
                generated_at=GENERATED_AT,
                published_at=PUBLISHED_AT,
                collection_status_by_issuer={ISSUER_ID: "complete"},
            ),
            publisher=publisher,
        )
    )

    issuer = result.edition.issuers[0]
    assert result.edition.status is EditionStatus.PARTIAL
    assert issuer.status is IssuerBriefStatus.PARTIAL
    assert issuer.facts[0].text == "2026年3月期 決算短信"
    assert "event_brief_unavailable" in issuer.warnings


def test_generated_edition_emits_each_validated_fact_with_its_sources() -> None:
    from src.market_morning.pipeline.morning_edition import (
        EditionEventFactInput,
        EditionEventInput,
        EditionIssuerInput,
        EditionSourceCitation,
        generate_morning_edition,
    )

    citation = EditionSourceCitation(
        provider="tdnet",
        document_id="TD-7203-001",
        revision_key="v1",
        original_url="https://example.jp/tdnet/source.pdf",
        published_at=GENERATED_AT,
    )
    event = EditionEventInput(
        event_id="44444444-4444-4444-8444-444444444444",
        event_family_key="tdnet:TD-7203-001",
        event_version=1,
        title="公式タイトル",
        event_type="earnings_release",
        occurred_at=GENERATED_AT,
        lifecycle_status="active",
        citations=(citation,),
        facts=(
            EditionEventFactInput(text="確認済み事実A", citations=(citation,)),
            EditionEventFactInput(text="確認済み事実B", citations=(citation,)),
        ),
    )

    edition = generate_morning_edition(
        day_plan=_open_day(),
        issuers=(
            EditionIssuerInput(
                issuer_id=ISSUER_ID,
                issuer_code="7203",
                legal_name_ja="トヨタ自動車株式会社",
                collection_status="complete",
                events=(event,),
            ),
        ),
        generated_at=GENERATED_AT,
    )

    assert [fact.text for fact in edition.issuers[0].facts] == [
        "確認済み事実A",
        "確認済み事実B",
    ]
    assert all(fact.citations == (citation,) for fact in edition.issuers[0].facts)


def test_loader_groups_multiple_citations_and_defaults_to_unverified_coverage() -> None:
    from src.market_morning.edition_generation import (
        EditionGenerationWindow,
        load_generation_inputs,
    )

    second_citation = _event_row(
        source_record_id="66666666-6666-4666-8666-666666666666",
        revision_key="mirror-v1",
        original_url="https://example.jp/ir/TD-7203-001-v1.pdf",
    )
    second_citation["source_provider"] = "company_ir"
    session = _Session(
        _Result(scalar=_active_user()),
        _Result(rows=(_watchlist_row(),)),
        _Result(rows=(_event_row(), second_citation)),
    )

    inputs = asyncio.run(
        load_generation_inputs(
            session,
            user_id=USER_ID,
            window=EditionGenerationWindow.ending_at(GENERATED_AT),
        )
    )

    assert len(inputs) == 1
    assert inputs[0].collection_status == "partial"
    assert inputs[0].error_code == "source_coverage_unverified"
    assert len(inputs[0].events) == 1
    assert [citation.provider for citation in inputs[0].events[0].citations] == [
        "company_ir",
        "tdnet",
    ]
    assert inputs[0].events[0].occurred_at.tzinfo == timezone.utc
    assert inputs[0].events[0].citations[0].published_at.tzinfo == timezone.utc
    assert "FOR UPDATE" in _mysql_sql(session.statements[0])


def test_explicit_complete_coverage_allows_confirmed_no_events() -> None:
    from src.market_morning.edition_generation import (
        EditionGenerationCommand,
        EditionGenerationWindow,
        generate_and_publish_morning_edition,
    )

    session = _Session(
        _Result(scalar=_active_user()),
        _Result(rows=(_watchlist_row(),)),
        _Result(rows=()),
    )
    published = []

    async def publisher(_session, **kwargs):
        published.append(kwargs)
        return SimpleNamespace(edition=kwargs["edition"])

    result = asyncio.run(
        generate_and_publish_morning_edition(
            session,
            command=EditionGenerationCommand(
                user_id=USER_ID,
                generation_key="2026-07-21:scheduled-01",
                day_plan=_open_day(),
                generated_at=GENERATED_AT,
                published_at=PUBLISHED_AT,
                window=EditionGenerationWindow.ending_at(GENERATED_AT),
                collection_status_by_issuer={ISSUER_ID: "complete"},
            ),
            publisher=publisher,
        )
    )

    assert result.edition.issuers[0].status is IssuerBriefStatus.NO_CONFIRMED_EVENTS
    assert result.edition.issuers[0].error_code is None
    assert published[0]["generation_key"] == "2026-07-21:scheduled-01"
    assert published[0]["published_at"] == PUBLISHED_AT


def test_unverified_empty_corpus_cannot_claim_no_confirmed_events() -> None:
    from src.market_morning.edition_generation import (
        EditionGenerationCommand,
        generate_and_publish_morning_edition,
    )

    session = _Session(
        _Result(scalar=_active_user()),
        _Result(rows=(_watchlist_row(),)),
        _Result(rows=()),
    )

    async def publisher(_session, **kwargs):
        return SimpleNamespace(edition=kwargs["edition"])

    result = asyncio.run(
        generate_and_publish_morning_edition(
            session,
            command=EditionGenerationCommand(
                user_id=USER_ID,
                generation_key="2026-07-21:scheduled-01",
                day_plan=_open_day(),
                generated_at=GENERATED_AT,
                published_at=PUBLISHED_AT,
            ),
            publisher=publisher,
        )
    )

    brief = result.edition.issuers[0]
    assert brief.status is IssuerBriefStatus.INSUFFICIENT_DATA
    assert brief.error_code == "source_coverage_unverified"
    assert "collection_incomplete" in brief.warnings


def test_market_holiday_skips_database_input_loading_but_publishes_state() -> None:
    from src.market_morning.edition_generation import (
        EditionGenerationCommand,
        generate_and_publish_morning_edition,
    )

    holiday = build_edition_day_plan(
        EditionDayInput(
            edition_date=date(2026, 7, 20),
            jp_market_open=False,
            us_previous_session_available=True,
            jp_closure_reason="marine_day",
        )
    )
    published = []

    async def publisher(_session, **kwargs):
        published.append(kwargs)
        return SimpleNamespace(edition=kwargs["edition"])

    result = asyncio.run(
        generate_and_publish_morning_edition(
            _Session(),
            command=EditionGenerationCommand(
                user_id=USER_ID,
                generation_key="2026-07-20:market-holiday",
                day_plan=holiday,
                generated_at=GENERATED_AT,
                published_at=PUBLISHED_AT,
            ),
            publisher=publisher,
        )
    )

    assert result.edition.status == "market_holiday"
    assert result.edition.issuers == ()
    assert len(published) == 1


def test_loader_fails_closed_when_source_row_budget_is_exceeded() -> None:
    from src.market_morning.edition_generation import (
        EditionGenerationSourceRowsExceeded,
        EditionGenerationWindow,
        load_generation_inputs,
    )

    session = _Session(
        _Result(scalar=_active_user()),
        _Result(rows=(_watchlist_row(),)),
        _Result(rows=(_event_row(), _event_row())),
    )

    with pytest.raises(EditionGenerationSourceRowsExceeded):
        asyncio.run(
            load_generation_inputs(
                session,
                user_id=USER_ID,
                window=EditionGenerationWindow.ending_at(GENERATED_AT),
                max_source_rows=1,
            )
        )


def test_public_generation_name_reuses_the_deterministic_core() -> None:
    from src.market_morning.pipeline.morning_edition import (
        generate_morning_edition,
        generate_synthetic_edition,
    )

    assert generate_morning_edition is generate_synthetic_edition


def test_production_entry_point_wraps_generation_in_one_transaction(monkeypatch) -> None:
    import src.market_morning.edition_generation as generation
    from src.market_morning.edition_generation import EditionGenerationCommand

    session = object()
    transaction_entries = []

    class _Transaction:
        async def __aenter__(self):
            transaction_entries.append("entered")
            return session

        async def __aexit__(self, exc_type, exc, traceback):
            transaction_entries.append("exited")

    class _Factory:
        def begin(self):
            return _Transaction()

    expected = SimpleNamespace(edition_id="edition-1")

    async def orchestrator(actual_session, *, command):
        assert actual_session is session
        assert command.generation_key == "2026-07-21:scheduled-01"
        return expected

    monkeypatch.setattr(generation, "get_session_factory", lambda: _Factory())
    monkeypatch.setattr(
        generation,
        "generate_and_publish_morning_edition",
        orchestrator,
    )
    command = EditionGenerationCommand(
        user_id=USER_ID,
        generation_key="2026-07-21:scheduled-01",
        day_plan=_open_day(),
        generated_at=GENERATED_AT,
        published_at=PUBLISHED_AT,
    )

    result = asyncio.run(generation.run_morning_edition_generation(command))

    assert result is expected
    assert transaction_entries == ["entered", "exited"]


def test_fresh_source_coverage_policy_allows_confirmed_no_events() -> None:
    from src.market_morning.edition_generation import (
        EditionGenerationCommand,
        generate_and_publish_morning_edition,
    )
    from src.market_morning.source_coverage import SourceCoveragePolicy

    session = _Session(
        _Result(scalar=_active_user()),
        _Result(rows=(_watchlist_row(),)),
        _Result(
            rows=(
                {
                    "provider": "fixture_tdnet",
                    "last_successful_discovery_at": datetime(2026, 7, 20, 21, 55),
                    "last_error_at": None,
                    "last_error_code": None,
                },
                {
                    "provider": "fixture_edinet",
                    "last_successful_discovery_at": datetime(2026, 7, 20, 21, 50),
                    "last_error_at": None,
                    "last_error_code": None,
                },
            )
        ),
        _Result(rows=()),
    )

    async def publisher(_session, **kwargs):
        return SimpleNamespace(edition=kwargs["edition"])

    result = asyncio.run(
        generate_and_publish_morning_edition(
            session,
            command=EditionGenerationCommand(
                user_id=USER_ID,
                generation_key="2026-07-21:coverage-complete",
                day_plan=_open_day(),
                generated_at=GENERATED_AT,
                published_at=PUBLISHED_AT,
                source_coverage_policy=SourceCoveragePolicy(
                    global_required_providers=(
                        "fixture_tdnet",
                        "fixture_edinet",
                    ),
                    max_staleness=timedelta(minutes=30),
                ),
            ),
            publisher=publisher,
        )
    )

    brief = result.edition.issuers[0]
    assert brief.status is IssuerBriefStatus.NO_CONFIRMED_EVENTS
    assert brief.error_code is None
