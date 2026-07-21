"""Synthetic morning-edition generation contracts."""

from __future__ import annotations

import importlib.util
from datetime import date, datetime, timedelta, timezone

import pytest


def test_morning_edition_pipeline_is_isolated_in_market_morning_domain() -> None:
    try:
        spec = importlib.util.find_spec(
            "src.market_morning.pipeline.morning_edition"
        )
    except ModuleNotFoundError:
        spec = None

    assert spec is not None


@pytest.mark.parametrize(
    (
        "jp_market_open",
        "us_previous_session_available",
        "expected_generate",
        "expected_status",
        "expected_overnight_context",
    ),
    (
        (True, True, True, "scheduled", "us_session_available"),
        (True, False, True, "scheduled", "us_market_closed"),
        (False, True, False, "market_holiday", "us_session_available"),
        (False, False, False, "market_holiday", "us_market_closed"),
    ),
)
def test_day_plan_makes_japan_and_us_calendar_asymmetry_explicit(
    jp_market_open: bool,
    us_previous_session_available: bool,
    expected_generate: bool,
    expected_status: str,
    expected_overnight_context: str,
) -> None:
    from src.market_morning.pipeline.morning_edition import (
        EditionDayInput,
        build_edition_day_plan,
    )

    plan = build_edition_day_plan(
        EditionDayInput(
            edition_date=date(2026, 7, 20),
            jp_market_open=jp_market_open,
            us_previous_session_available=us_previous_session_available,
            jp_closure_reason=None if jp_market_open else "jp_exchange_holiday",
            us_closure_reason=(
                None if us_previous_session_available else "us_exchange_holiday"
            ),
        )
    )

    assert plan.generate is expected_generate
    assert plan.status == expected_status
    assert plan.overnight_context == expected_overnight_context


def test_market_holiday_plan_keeps_the_confirmed_closure_reason() -> None:
    from src.market_morning.pipeline.morning_edition import (
        EditionDayInput,
        build_edition_day_plan,
    )

    plan = build_edition_day_plan(
        EditionDayInput(
            edition_date=date(2026, 7, 20),
            jp_market_open=False,
            us_previous_session_available=True,
            jp_closure_reason="marine_day",
        )
    )

    assert plan.reason_code == "marine_day"


def _citation(*, revision: str = "v1"):
    from src.market_morning.pipeline.morning_edition import EditionSourceCitation

    return EditionSourceCitation(
        provider="fixture_tdnet",
        document_id="TD-7203-001",
        revision_key=revision,
        original_url=f"https://example.invalid/tdnet/TD-7203-001-{revision}.pdf",
        published_at=datetime(2026, 7, 20, 6, 0, tzinfo=timezone.utc),
    )


def _event(
    *,
    event_id: str,
    family_key: str,
    version: int = 1,
    title: str = "2026年3月期 決算短信",
    lifecycle_status: str = "active",
    citations=None,
    minutes: int = 0,
):
    from src.market_morning.pipeline.morning_edition import EditionEventInput

    return EditionEventInput(
        event_id=event_id,
        event_family_key=family_key,
        event_version=version,
        title=title,
        event_type="earnings_release",
        occurred_at=datetime(2026, 7, 20, 6, 0, tzinfo=timezone.utc)
        + timedelta(minutes=minutes),
        lifecycle_status=lifecycle_status,
        citations=tuple(citations if citations is not None else (_citation(),)),
    )


def _issuer(
    *,
    issuer_code: str,
    events=(),
    collection_status: str = "complete",
    error_code: str | None = None,
):
    from src.market_morning.pipeline.morning_edition import EditionIssuerInput

    return EditionIssuerInput(
        issuer_id=f"issuer-{issuer_code}",
        issuer_code=issuer_code,
        legal_name_ja=f"発行体 {issuer_code}",
        collection_status=collection_status,
        events=tuple(events),
        error_code=error_code,
    )


def _open_day():
    from src.market_morning.pipeline.morning_edition import (
        EditionDayInput,
        build_edition_day_plan,
    )

    return build_edition_day_plan(
        EditionDayInput(
            edition_date=date(2026, 7, 21),
            jp_market_open=True,
            us_previous_session_available=True,
        )
    )


def test_generation_keeps_only_latest_revision_and_cites_every_fact() -> None:
    from src.market_morning.pipeline.morning_edition import generate_synthetic_edition

    edition = generate_synthetic_edition(
        day_plan=_open_day(),
        issuers=(
            _issuer(
                issuer_code="7203",
                events=(
                    _event(event_id="event-v1", family_key="family-a"),
                    _event(
                        event_id="event-v2",
                        family_key="family-a",
                        version=2,
                        title="（訂正）2026年3月期 決算短信",
                        lifecycle_status="corrected",
                        citations=(_citation(revision="v2"),),
                        minutes=30,
                    ),
                ),
            ),
        ),
        generated_at=datetime(2026, 7, 20, 22, 0, tzinfo=timezone.utc),
    )

    assert edition.status == "ready"
    assert edition.consumed_event_units == 1
    assert len(edition.issuers[0].facts) == 1
    fact = edition.issuers[0].facts[0]
    assert fact.kind == "fact"
    assert fact.event_id == "event-v2"
    assert fact.lifecycle_status == "corrected"
    assert fact.citations[0].revision_key == "v2"
    assert edition.issuers[0].assessment.kind == "inference"
    assert edition.issuers[0].assessment.text == "不足以判断"


def test_event_without_citation_is_omitted_and_marks_edition_partial() -> None:
    from src.market_morning.pipeline.morning_edition import generate_synthetic_edition

    edition = generate_synthetic_edition(
        day_plan=_open_day(),
        issuers=(
            _issuer(
                issuer_code="7203",
                events=(
                    _event(event_id="event-valid", family_key="family-valid"),
                    _event(
                        event_id="event-no-source",
                        family_key="family-no-source",
                        citations=(),
                        minutes=10,
                    ),
                ),
            ),
        ),
        generated_at=datetime(2026, 7, 20, 22, 0, tzinfo=timezone.utc),
    )

    assert edition.status == "partial"
    assert [fact.event_id for fact in edition.issuers[0].facts] == ["event-valid"]
    assert edition.issuers[0].omitted_event_count == 1
    assert "missing_citation" in edition.issuers[0].warnings


def test_one_unavailable_issuer_does_not_block_other_issuer_facts() -> None:
    from src.market_morning.pipeline.morning_edition import generate_synthetic_edition

    edition = generate_synthetic_edition(
        day_plan=_open_day(),
        issuers=(
            _issuer(
                issuer_code="7203",
                events=(_event(event_id="event-a", family_key="family-a"),),
            ),
            _issuer(
                issuer_code="6758",
                collection_status="unavailable",
                error_code="source_timeout",
            ),
        ),
        generated_at=datetime(2026, 7, 20, 22, 0, tzinfo=timezone.utc),
    )

    assert edition.status == "partial"
    assert edition.issuers[0].facts[0].event_id == "event-a"
    assert edition.issuers[1].status == "unavailable"
    assert edition.issuers[1].error_code == "source_timeout"


def test_complete_empty_collection_is_not_conflated_with_unavailable_data() -> None:
    from src.market_morning.pipeline.morning_edition import generate_synthetic_edition

    confirmed_empty = generate_synthetic_edition(
        day_plan=_open_day(),
        issuers=(_issuer(issuer_code="7203"),),
        generated_at=datetime(2026, 7, 20, 22, 0, tzinfo=timezone.utc),
    )
    unavailable = generate_synthetic_edition(
        day_plan=_open_day(),
        issuers=(
            _issuer(
                issuer_code="7203",
                collection_status="unavailable",
                error_code="source_unavailable",
            ),
        ),
        generated_at=datetime(2026, 7, 20, 22, 0, tzinfo=timezone.utc),
    )

    assert confirmed_empty.status == "ready"
    assert confirmed_empty.issuers[0].status == "no_confirmed_events"
    assert unavailable.status == "no_data"
    assert unavailable.issuers[0].status == "unavailable"


def test_generation_budget_caps_issuers_and_event_units_without_hiding_truncation() -> None:
    from src.market_morning.pipeline.morning_edition import (
        GenerationBudget,
        generate_synthetic_edition,
    )

    edition = generate_synthetic_edition(
        day_plan=_open_day(),
        issuers=tuple(
            _issuer(
                issuer_code=code,
                events=(_event(event_id=f"event-{code}", family_key=f"family-{code}"),),
            )
            for code in ("7203", "6758", "9984")
        ),
        generated_at=datetime(2026, 7, 20, 22, 0, tzinfo=timezone.utc),
        budget=GenerationBudget(
            max_issuers=2,
            max_events_per_issuer=1,
            max_total_events=1,
        ),
    )

    assert edition.status == "partial"
    assert edition.budget_exhausted is True
    assert edition.consumed_event_units == 1
    assert edition.omitted_issuer_count == 1
    assert len(edition.issuers) == 2
    assert edition.issuers[1].omitted_event_count == 1


def test_market_holiday_never_generates_issuer_content() -> None:
    from src.market_morning.pipeline.morning_edition import (
        EditionDayInput,
        build_edition_day_plan,
        generate_synthetic_edition,
    )

    edition = generate_synthetic_edition(
        day_plan=build_edition_day_plan(
            EditionDayInput(
                edition_date=date(2026, 7, 20),
                jp_market_open=False,
                us_previous_session_available=True,
                jp_closure_reason="marine_day",
            )
        ),
        issuers=(
            _issuer(
                issuer_code="7203",
                events=(_event(event_id="event-a", family_key="family-a"),),
            ),
        ),
        generated_at=datetime(2026, 7, 19, 22, 0, tzinfo=timezone.utc),
    )

    assert edition.status == "market_holiday"
    assert edition.issuers == ()
    assert edition.consumed_event_units == 0
