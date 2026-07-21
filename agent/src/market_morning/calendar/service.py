"""Fail-closed trading-calendar decisions for Market Morning publication."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from enum import StrEnum
from typing import Protocol
from zoneinfo import ZoneInfo

from src.market_morning.pipeline.morning_edition import (
    EditionDayInput,
    EditionDayPlan,
    build_edition_day_plan,
)

JST = ZoneInfo("Asia/Tokyo")
MAX_SESSION_SEARCH_DAYS = 370


class MarketCode(StrEnum):
    JPX_CASH = "jpx_cash"
    US_CASH = "us_cash"


class CalendarScenario(StrEnum):
    A_STANDARD = "a_standard"
    B_US_CLOSED = "b_us_closed"
    C_JP_CLOSED = "c_jp_closed"
    D_BOTH_CLOSED_OR_WEEKEND = "d_both_closed_or_weekend"
    E_MANUAL_HALT_OR_UNAVAILABLE = "e_manual_halt_or_unavailable"


class PublicationPhase(StrEnum):
    COLLECTING = "collecting"
    PREPARING = "preparing"
    PUBLISH = "publish"
    LATE = "late"


class CalendarUnavailable(RuntimeError):
    """Raised by a calendar port when no authoritative answer is available."""


def _required(value: str, *, field_name: str, maximum: int) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > maximum:
        raise ValueError(f"{field_name} must contain 1 to {maximum} characters")
    return normalized


@dataclass(frozen=True, slots=True)
class MarketSession:
    market: MarketCode
    session_date: date
    is_open: bool
    reason_code: str | None
    provider: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "provider",
            _required(self.provider, field_name="provider", maximum=64),
        )
        if self.is_open and self.reason_code is not None:
            raise ValueError("open sessions cannot have a closure reason")
        if not self.is_open:
            object.__setattr__(
                self,
                "reason_code",
                _required(
                    self.reason_code or "market_closed",
                    field_name="reason_code",
                    maximum=64,
                ),
            )


class TradingCalendar(Protocol):
    provider: str
    market: MarketCode

    def session_on(self, session_date: date) -> MarketSession: ...

    def previous_open_session(self, session_date: date) -> date | None: ...

    def next_open_session(self, session_date: date) -> date | None: ...


class FixtureTradingCalendar:
    """Deterministic calendar that cannot be mistaken for a licensed feed."""

    def __init__(
        self,
        *,
        provider: str,
        market: MarketCode,
        closed_dates: Mapping[date, str] | None = None,
        special_open_dates: tuple[date, ...] = (),
    ) -> None:
        canonical_provider = _required(provider, field_name="provider", maximum=64)
        if not canonical_provider.startswith("fixture_"):
            raise ValueError("fixture calendar providers must start with fixture_")
        self.provider = canonical_provider
        self.market = MarketCode(market)
        self._closed_dates = {
            session_date: _required(reason, field_name="reason_code", maximum=64)
            for session_date, reason in (closed_dates or {}).items()
        }
        self._special_open_dates = frozenset(special_open_dates)
        if self._special_open_dates & self._closed_dates.keys():
            raise ValueError("a session date cannot be both closed and specially open")

    def session_on(self, session_date: date) -> MarketSession:
        if not isinstance(session_date, date):
            raise TypeError("session_date must be a date")
        reason = self._closed_dates.get(session_date)
        if reason is not None:
            is_open = False
        elif session_date in self._special_open_dates:
            is_open = True
        elif session_date.weekday() >= 5:
            is_open = False
            reason = "weekend"
        else:
            is_open = True
        return MarketSession(
            market=self.market,
            session_date=session_date,
            is_open=is_open,
            reason_code=None if is_open else reason,
            provider=self.provider,
        )

    def previous_open_session(self, session_date: date) -> date | None:
        candidate = session_date - timedelta(days=1)
        for _ in range(MAX_SESSION_SEARCH_DAYS):
            if self.session_on(candidate).is_open:
                return candidate
            candidate -= timedelta(days=1)
        return None

    def next_open_session(self, session_date: date) -> date | None:
        candidate = session_date + timedelta(days=1)
        for _ in range(MAX_SESSION_SEARCH_DAYS):
            if self.session_on(candidate).is_open:
                return candidate
            candidate += timedelta(days=1)
        return None


@dataclass(frozen=True, slots=True)
class PublicationHaltOverride:
    override_id: str
    edition_date: date
    reason_code: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "override_id",
            _required(self.override_id, field_name="override_id", maximum=36),
        )
        object.__setattr__(
            self,
            "reason_code",
            _required(self.reason_code, field_name="reason_code", maximum=64),
        )


@dataclass(frozen=True, slots=True)
class MarketDayEvaluation:
    edition_date: date
    scenario: CalendarScenario
    day_plan: EditionDayPlan
    collect_sources: bool
    generate_edition: bool
    email_permitted: bool
    us_reference_date: date
    us_last_valid_session_date: date | None
    jp_last_valid_session_date: date | None
    next_jp_session_date: date | None
    reason_code: str | None
    override_id: str | None = None


def _closed_evaluation(
    *,
    edition_date: date,
    scenario: CalendarScenario,
    us_reference_date: date,
    us_last_valid_session_date: date | None,
    jp_last_valid_session_date: date | None,
    next_jp_session_date: date | None,
    reason_code: str,
    override_id: str | None = None,
) -> MarketDayEvaluation:
    day_plan = build_edition_day_plan(
        EditionDayInput(
            edition_date=edition_date,
            jp_market_open=False,
            us_previous_session_available=False,
            jp_closure_reason=reason_code,
            us_closure_reason=None,
        )
    )
    return MarketDayEvaluation(
        edition_date=edition_date,
        scenario=scenario,
        day_plan=day_plan,
        collect_sources=True,
        generate_edition=False,
        email_permitted=False,
        us_reference_date=us_reference_date,
        us_last_valid_session_date=us_last_valid_session_date,
        jp_last_valid_session_date=jp_last_valid_session_date,
        next_jp_session_date=next_jp_session_date,
        reason_code=reason_code,
        override_id=override_id,
    )


def evaluate_market_day(
    edition_date: date,
    *,
    jp_calendar: TradingCalendar,
    us_calendar: TradingCalendar,
    publication_override: PublicationHaltOverride | None = None,
) -> MarketDayEvaluation:
    if jp_calendar.market is not MarketCode.JPX_CASH:
        raise ValueError("jp_calendar must provide JPX cash sessions")
    if us_calendar.market is not MarketCode.US_CASH:
        raise ValueError("us_calendar must provide US cash sessions")
    if publication_override is not None and publication_override.edition_date != edition_date:
        raise ValueError("publication override date does not match edition_date")
    us_reference_date = edition_date - timedelta(days=1)
    try:
        jp_session = jp_calendar.session_on(edition_date)
        us_reference_session = us_calendar.session_on(us_reference_date)
        us_last_valid = (
            us_reference_date
            if us_reference_session.is_open
            else us_calendar.previous_open_session(us_reference_date)
        )
        next_jp_session = (
            None if jp_session.is_open else jp_calendar.next_open_session(edition_date)
        )
        jp_last_valid = jp_calendar.previous_open_session(edition_date)
    except CalendarUnavailable:
        return _closed_evaluation(
            edition_date=edition_date,
            scenario=CalendarScenario.E_MANUAL_HALT_OR_UNAVAILABLE,
            us_reference_date=us_reference_date,
            us_last_valid_session_date=None,
            jp_last_valid_session_date=None,
            next_jp_session_date=None,
            reason_code="calendar_unavailable",
        )

    if publication_override is not None:
        return _closed_evaluation(
            edition_date=edition_date,
            scenario=CalendarScenario.E_MANUAL_HALT_OR_UNAVAILABLE,
            us_reference_date=us_reference_date,
            us_last_valid_session_date=us_last_valid,
            jp_last_valid_session_date=jp_last_valid,
            next_jp_session_date=next_jp_session,
            reason_code=publication_override.reason_code,
            override_id=publication_override.override_id,
        )

    if not jp_session.is_open:
        scenario = (
            CalendarScenario.D_BOTH_CLOSED_OR_WEEKEND
            if jp_session.reason_code == "weekend" or not us_reference_session.is_open
            else CalendarScenario.C_JP_CLOSED
        )
        return _closed_evaluation(
            edition_date=edition_date,
            scenario=scenario,
            us_reference_date=us_reference_date,
            us_last_valid_session_date=us_last_valid,
            jp_last_valid_session_date=jp_last_valid,
            next_jp_session_date=next_jp_session,
            reason_code=jp_session.reason_code or "jpx_market_closed",
        )

    normal_weekend_gap = us_reference_session.reason_code == "weekend"
    us_available = us_reference_session.is_open or normal_weekend_gap
    scenario = (
        CalendarScenario.A_STANDARD
        if us_available
        else CalendarScenario.B_US_CLOSED
    )
    reason_code = None if us_available else us_reference_session.reason_code
    day_plan = build_edition_day_plan(
        EditionDayInput(
            edition_date=edition_date,
            jp_market_open=True,
            us_previous_session_available=us_available,
            us_closure_reason=reason_code,
        )
    )
    return MarketDayEvaluation(
        edition_date=edition_date,
        scenario=scenario,
        day_plan=day_plan,
        collect_sources=True,
        generate_edition=True,
        email_permitted=True,
        us_reference_date=us_reference_date,
        us_last_valid_session_date=us_last_valid,
        jp_last_valid_session_date=jp_last_valid,
        next_jp_session_date=None,
        reason_code=reason_code,
    )


@dataclass(frozen=True, slots=True)
class PublicationWindow:
    edition_date: date
    local_time: datetime
    phase: PublicationPhase
    attempt_key: str | None
    attempt_at: datetime | None
    email_permitted: bool
    late: bool


_PUBLICATION_ATTEMPTS = (
    (time(7, 0), "0700"),
    (time(7, 15), "0715"),
    (time(7, 30), "0730"),
    (time(8, 0), "0800"),
    (time(8, 30), "0830"),
)


def publication_window(at: datetime) -> PublicationWindow:
    if at.tzinfo is None or at.utcoffset() is None:
        raise ValueError("at must be timezone-aware")
    local = at.astimezone(JST)
    local_clock = local.timetz().replace(tzinfo=None)
    if local_clock < time(6, 30):
        phase = PublicationPhase.COLLECTING
        attempt_key = None
        attempt_at = None
        email_permitted = False
        late = False
    elif local_clock < time(7, 0):
        phase = PublicationPhase.PREPARING
        attempt_key = None
        attempt_at = None
        email_permitted = False
        late = False
    elif local_clock <= time(8, 30):
        phase = PublicationPhase.PUBLISH
        scheduled_time, attempt_key = next(
            (scheduled_at, key)
            for scheduled_at, key in reversed(_PUBLICATION_ATTEMPTS)
            if local_clock >= scheduled_at
        )
        attempt_at = datetime.combine(local.date(), scheduled_time, tzinfo=JST)
        email_permitted = True
        late = False
    else:
        phase = PublicationPhase.LATE
        attempt_key = "late"
        attempt_at = datetime.combine(
            local.date(),
            time(8, 30, 0, 1),
            tzinfo=JST,
        )
        email_permitted = False
        late = True
    return PublicationWindow(
        edition_date=local.date(),
        local_time=local,
        phase=phase,
        attempt_key=attempt_key,
        attempt_at=attempt_at,
        email_permitted=email_permitted,
        late=late,
    )


__all__ = [
    "CalendarScenario",
    "CalendarUnavailable",
    "FixtureTradingCalendar",
    "MarketCode",
    "MarketDayEvaluation",
    "MarketSession",
    "PublicationHaltOverride",
    "PublicationPhase",
    "PublicationWindow",
    "TradingCalendar",
    "evaluate_market_day",
    "publication_window",
]
