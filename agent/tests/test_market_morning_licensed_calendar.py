"""Production calendar feed boundary tests."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import json

import httpx
import pytest

NOW = datetime(2026, 7, 20, 22, 0, tzinfo=timezone.utc)


async def _public_resolver(host: str, port: int) -> tuple[str, ...]:
    assert host == "calendar.vendor.example"
    assert port == 443
    return ("93.184.216.34",)


def _payload(*, provider: str = "licensed_jpx_calendar") -> dict:
    sessions = []
    for offset in range(7):
        session_date = date(2026, 7, 18) + timedelta(days=offset)
        is_open = session_date.weekday() < 5
        sessions.append(
            {
                "session_date": session_date.isoformat(),
                "is_open": is_open,
                "reason_code": None if is_open else "weekend",
            }
        )
    return {
        "schema_version": 1,
        "provider": provider,
        "market": "jpx_cash",
        "generated_at": NOW.isoformat(),
        "coverage_start": "2026-07-18",
        "coverage_end": "2026-07-24",
        "sessions": sessions,
    }


def _config(*, header_factory=None):
    from src.market_morning.calendar import LicensedCalendarFeedConfig

    return LicensedCalendarFeedConfig(
        provider="licensed_jpx_calendar",
        market="jpx_cash",
        endpoint_url="https://calendar.vendor.example/v1/jpx/sessions.json",
        allowed_base_urls=("https://calendar.vendor.example/v1/jpx/",),
        header_factory=header_factory,
    )


def test_licensed_calendar_preloads_complete_sync_contract_without_secret_repr() -> None:
    from src.market_morning.calendar import load_licensed_trading_calendar

    requested = []

    def headers():
        return {"authorization": "Bearer calendar-secret"}

    async def respond(request: httpx.Request) -> httpx.Response:
        requested.append(request)
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            content=json.dumps(_payload()).encode(),
        )

    config = _config(header_factory=headers)
    assert "calendar-secret" not in repr(config)
    assert "sessions.json" not in repr(config)
    calendar = asyncio.run(
        load_licensed_trading_calendar(
            config,
            required_start=date(2026, 7, 19),
            required_end=date(2026, 7, 23),
            clock=lambda: NOW,
            resolver=_public_resolver,
            transport=httpx.MockTransport(respond),
        )
    )

    assert requested[0].headers["authorization"] == "Bearer calendar-secret"
    assert calendar.session_on(date(2026, 7, 20)).is_open is True
    assert calendar.session_on(date(2026, 7, 19)).reason_code == "weekend"
    assert calendar.previous_open_session(date(2026, 7, 21)) == date(2026, 7, 20)
    assert calendar.next_open_session(date(2026, 7, 19)) == date(2026, 7, 20)


def test_licensed_calendar_fails_closed_outside_manifest_coverage() -> None:
    from src.market_morning.calendar import (
        CalendarUnavailable,
        load_licensed_trading_calendar,
    )

    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            headers={"content-type": "application/json"},
            content=json.dumps(_payload()).encode(),
        )
    )
    calendar = asyncio.run(
        load_licensed_trading_calendar(
            _config(),
            required_start=date(2026, 7, 18),
            required_end=date(2026, 7, 24),
            clock=lambda: NOW,
            resolver=_public_resolver,
            transport=transport,
        )
    )

    with pytest.raises(CalendarUnavailable, match="out_of_coverage"):
        calendar.session_on(date(2026, 7, 17))
    with pytest.raises(CalendarUnavailable, match="out_of_coverage"):
        calendar.previous_open_session(date(2026, 7, 18))


@pytest.mark.parametrize("mutation", ["provider", "gap", "stale", "extra"])
def test_licensed_calendar_rejects_drift_staleness_and_incomplete_days(
    mutation: str,
) -> None:
    from src.market_morning.calendar import (
        CalendarUnavailable,
        load_licensed_trading_calendar,
    )

    payload = deepcopy(_payload())
    if mutation == "provider":
        payload["provider"] = "other_calendar"
    elif mutation == "gap":
        payload["sessions"].pop(2)
    elif mutation == "stale":
        payload["generated_at"] = (NOW - timedelta(days=8)).isoformat()
    else:
        payload["unexpected"] = True
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            headers={"content-type": "application/json"},
            content=json.dumps(payload).encode(),
        )
    )

    with pytest.raises(CalendarUnavailable, match="licensed_calendar_unavailable"):
        asyncio.run(
            load_licensed_trading_calendar(
                _config(),
                required_start=date(2026, 7, 18),
                required_end=date(2026, 7, 24),
                clock=lambda: NOW,
                resolver=_public_resolver,
                transport=transport,
            )
        )


def test_licensed_calendar_rejects_private_dns_before_http() -> None:
    from src.market_morning.calendar import (
        CalendarUnavailable,
        load_licensed_trading_calendar,
    )

    called = False

    async def private(host: str, port: int) -> tuple[str, ...]:
        return ("127.0.0.1",)

    async def should_not_call(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200)

    with pytest.raises(CalendarUnavailable, match="licensed_calendar_unavailable"):
        asyncio.run(
            load_licensed_trading_calendar(
                _config(),
                required_start=date(2026, 7, 18),
                required_end=date(2026, 7, 24),
                clock=lambda: NOW,
                resolver=private,
                transport=httpx.MockTransport(should_not_call),
            )
        )
    assert called is False


def test_licensed_calendar_endpoint_requires_exact_https_path_boundary() -> None:
    from src.market_morning.calendar import LicensedCalendarFeedConfig

    with pytest.raises(ValueError, match="endpoint"):
        LicensedCalendarFeedConfig(
            provider="licensed_jpx_calendar",
            market="jpx_cash",
            endpoint_url="https://calendar.vendor.example/v1/us/sessions.json",
            allowed_base_urls=("https://calendar.vendor.example/v1/jpx/",),
        )
    with pytest.raises(ValueError, match="allowed_base"):
        LicensedCalendarFeedConfig(
            provider="licensed_jpx_calendar",
            market="jpx_cash",
            endpoint_url="https://127.0.0.1/v1/jpx/sessions.json",
            allowed_base_urls=("https://127.0.0.1/v1/jpx/",),
        )
