"""JPX-anchored Market Morning calendar and publication-window rules."""

from __future__ import annotations

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pytest

JST = ZoneInfo("Asia/Tokyo")


def _calendars(*, jp_closed=None, us_closed=None):
    from src.market_morning.calendar.service import FixtureTradingCalendar, MarketCode

    return (
        FixtureTradingCalendar(
            provider="fixture_jpx_calendar",
            market=MarketCode.JPX_CASH,
            closed_dates=jp_closed or {},
        ),
        FixtureTradingCalendar(
            provider="fixture_us_calendar",
            market=MarketCode.US_CASH,
            closed_dates=us_closed or {},
        ),
    )


def test_fixture_calendar_is_explicitly_non_production_and_searches_open_sessions() -> None:
    from src.market_morning.calendar.service import FixtureTradingCalendar, MarketCode

    with pytest.raises(ValueError, match="fixture_"):
        FixtureTradingCalendar(
            provider="jpx_calendar",
            market=MarketCode.JPX_CASH,
        )

    calendar = FixtureTradingCalendar(
        provider="fixture_jpx_calendar",
        market=MarketCode.JPX_CASH,
        closed_dates={date(2026, 7, 20): "marine_day"},
    )

    assert calendar.session_on(date(2026, 7, 20)).is_open is False
    assert calendar.session_on(date(2026, 7, 20)).reason_code == "marine_day"
    assert calendar.previous_open_session(date(2026, 7, 20)) == date(2026, 7, 17)
    assert calendar.next_open_session(date(2026, 7, 20)) == date(2026, 7, 21)


def test_scenario_a_japan_and_previous_us_session_open() -> None:
    from src.market_morning.calendar.service import CalendarScenario, evaluate_market_day

    jp, us = _calendars()
    result = evaluate_market_day(date(2026, 7, 21), jp_calendar=jp, us_calendar=us)

    assert result.scenario is CalendarScenario.A_STANDARD
    assert result.generate_edition is True
    assert result.collect_sources is True
    assert result.email_permitted is True
    assert result.day_plan.generate is True
    assert result.us_reference_date == date(2026, 7, 20)
    assert result.us_last_valid_session_date == date(2026, 7, 20)
    assert result.jp_last_valid_session_date == date(2026, 7, 20)
    assert result.reason_code is None


def test_scenario_b_japan_open_and_us_holiday_uses_last_valid_close() -> None:
    from src.market_morning.calendar.service import CalendarScenario, evaluate_market_day
    from src.market_morning.pipeline.morning_edition import OvernightMarketContext

    jp, us = _calendars(us_closed={date(2026, 7, 20): "us_market_holiday"})
    result = evaluate_market_day(date(2026, 7, 21), jp_calendar=jp, us_calendar=us)

    assert result.scenario is CalendarScenario.B_US_CLOSED
    assert result.generate_edition is True
    assert result.email_permitted is True
    assert result.day_plan.overnight_context is OvernightMarketContext.US_MARKET_CLOSED
    assert result.us_last_valid_session_date == date(2026, 7, 17)
    assert result.reason_code == "us_market_holiday"


def test_normal_monday_uses_friday_us_close_without_calling_it_a_us_holiday() -> None:
    from src.market_morning.calendar.service import CalendarScenario, evaluate_market_day

    jp, us = _calendars()
    result = evaluate_market_day(date(2026, 7, 27), jp_calendar=jp, us_calendar=us)

    assert result.scenario is CalendarScenario.A_STANDARD
    assert result.us_reference_date == date(2026, 7, 26)
    assert result.us_last_valid_session_date == date(2026, 7, 24)
    assert result.reason_code is None


def test_scenario_c_japan_holiday_does_not_publish_but_keeps_collection() -> None:
    from src.market_morning.calendar.service import CalendarScenario, evaluate_market_day

    jp, us = _calendars(jp_closed={date(2026, 7, 21): "jpx_market_holiday"})
    result = evaluate_market_day(date(2026, 7, 21), jp_calendar=jp, us_calendar=us)

    assert result.scenario is CalendarScenario.C_JP_CLOSED
    assert result.generate_edition is False
    assert result.email_permitted is False
    assert result.collect_sources is True
    assert result.next_jp_session_date == date(2026, 7, 22)
    assert result.jp_last_valid_session_date == date(2026, 7, 20)
    assert result.reason_code == "jpx_market_holiday"


def test_scenario_d_weekend_does_not_publish_and_shows_next_jpx_session() -> None:
    from src.market_morning.calendar.service import CalendarScenario, evaluate_market_day

    jp, us = _calendars()
    result = evaluate_market_day(date(2026, 7, 25), jp_calendar=jp, us_calendar=us)

    assert result.scenario is CalendarScenario.D_BOTH_CLOSED_OR_WEEKEND
    assert result.generate_edition is False
    assert result.email_permitted is False
    assert result.next_jp_session_date == date(2026, 7, 27)
    assert result.reason_code == "weekend"


def test_scenario_e_manual_halt_overrides_an_open_day() -> None:
    from src.market_morning.calendar.service import (
        CalendarScenario,
        PublicationHaltOverride,
        evaluate_market_day,
    )

    jp, us = _calendars()
    result = evaluate_market_day(
        date(2026, 7, 21),
        jp_calendar=jp,
        us_calendar=us,
        publication_override=PublicationHaltOverride(
            override_id="override-1",
            edition_date=date(2026, 7, 21),
            reason_code="emergency_market_halt",
        ),
    )

    assert result.scenario is CalendarScenario.E_MANUAL_HALT_OR_UNAVAILABLE
    assert result.generate_edition is False
    assert result.email_permitted is False
    assert result.collect_sources is True
    assert result.reason_code == "emergency_market_halt"
    assert result.override_id == "override-1"


def test_calendar_failure_fails_closed_as_scenario_e() -> None:
    from src.market_morning.calendar.service import (
        CalendarScenario,
        CalendarUnavailable,
        MarketCode,
        evaluate_market_day,
    )

    class UnavailableCalendar:
        market = MarketCode.JPX_CASH
        provider = "licensed_jpx"

        def session_on(self, session_date):
            raise CalendarUnavailable("secret provider response")

        def previous_open_session(self, session_date):
            raise AssertionError

        def next_open_session(self, session_date):
            raise AssertionError

    _, us = _calendars()
    result = evaluate_market_day(
        date(2026, 7, 21),
        jp_calendar=UnavailableCalendar(),
        us_calendar=us,
    )

    assert result.scenario is CalendarScenario.E_MANUAL_HALT_OR_UNAVAILABLE
    assert result.generate_edition is False
    assert result.reason_code == "calendar_unavailable"
    assert "secret provider response" not in repr(result)


@pytest.mark.parametrize(
    ("at", "phase", "attempt_key", "email_permitted", "late"),
    [
        ("2026-07-21T06:29:59+09:00", "collecting", None, False, False),
        ("2026-07-21T06:30:00+09:00", "preparing", None, False, False),
        ("2026-07-21T06:59:59+09:00", "preparing", None, False, False),
        ("2026-07-21T07:00:00+09:00", "publish", "0700", True, False),
        ("2026-07-21T07:14:59+09:00", "publish", "0700", True, False),
        ("2026-07-21T07:15:00+09:00", "publish", "0715", True, False),
        ("2026-07-21T07:30:00+09:00", "publish", "0730", True, False),
        ("2026-07-21T08:00:00+09:00", "publish", "0800", True, False),
        ("2026-07-21T08:30:00+09:00", "publish", "0830", True, False),
        ("2026-07-21T08:30:00.000001+09:00", "late", "late", False, True),
    ],
)
def test_publication_window_boundaries(
    at,
    phase,
    attempt_key,
    email_permitted,
    late,
) -> None:
    from src.market_morning.calendar.service import publication_window

    result = publication_window(datetime.fromisoformat(at))

    assert result.edition_date == date(2026, 7, 21)
    assert result.phase == phase
    assert result.attempt_key == attempt_key
    assert result.email_permitted is email_permitted
    assert result.late is late


def test_publication_window_converts_utc_to_jst_and_rejects_naive_time() -> None:
    from src.market_morning.calendar.service import publication_window

    result = publication_window(datetime(2026, 7, 20, 22, 0, tzinfo=timezone.utc))
    assert result.local_time == datetime(2026, 7, 21, 7, 0, tzinfo=JST)
    assert result.attempt_key == "0700"

    with pytest.raises(ValueError, match="timezone-aware"):
        publication_window(datetime(2026, 7, 21, 7, 0))
