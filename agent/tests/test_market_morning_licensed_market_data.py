"""Licensed market observation adapter tests."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json

import httpx
import pytest

NOW = datetime(2026, 7, 20, 22, 0, tzinfo=timezone.utc)


async def _public_resolver(host: str, port: int) -> tuple[str, ...]:
    assert host == "quotes.vendor.example"
    assert port == 443
    return ("93.184.216.34",)


def _config(instrument="nikkei_225", *, header_factory=None):
    from src.market_morning.licensed_market_data import (
        LicensedMarketDataFeedConfig,
    )

    currency = "JPY" if instrument in {"nikkei_225", "usd_jpy"} else "USD"
    delay = ("delayed",) if instrument == "usd_jpy" else ("eod",)
    return LicensedMarketDataFeedConfig(
        provider="licensed_market_vendor",
        instrument=instrument,
        expected_currency=currency,
        endpoint_url=f"https://quotes.vendor.example/v2/observations/{instrument}?adjusted=false",
        allowed_base_urls=("https://quotes.vendor.example/v2/observations/",),
        allowed_delay_statuses=delay,
        header_factory=header_factory,
    )


def _payload(instrument="nikkei_225") -> dict:
    return {
        "schema_version": 1,
        "provider": "licensed_market_vendor",
        "instrument": instrument,
        "session_date": "2026-07-20",
        "as_of": (NOW - timedelta(minutes=5)).isoformat(),
        "value": "39819.11",
        "previous_close": "39780.00",
        "currency": "JPY" if instrument in {"nikkei_225", "usd_jpy"} else "USD",
        "delay_status": "delayed" if instrument == "usd_jpy" else "eod",
    }


@pytest.mark.parametrize(
    "instrument",
    ["nikkei_225", "sp_500", "nasdaq_composite", "djia", "usd_jpy"],
)
def test_each_required_instrument_uses_an_exact_adapter_contract(instrument: str) -> None:
    from src.market_morning.licensed_market_data import LicensedMarketDataAdapter
    from src.market_morning.market_snapshots import MarketInstrument

    requests = []

    def headers():
        return {"x-provider-key": "market-secret"}

    async def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            headers={"content-type": "application/json; charset=utf-8"},
            content=json.dumps(_payload(instrument)).encode(),
        )

    config = _config(instrument, header_factory=headers)
    assert "market-secret" not in repr(config)
    assert "adjusted=false" not in repr(config)
    adapter = LicensedMarketDataAdapter(
        config,
        clock=lambda: NOW,
        resolver=_public_resolver,
        transport=httpx.MockTransport(respond),
    )
    snapshot = asyncio.run(adapter.fetch(at=NOW - timedelta(minutes=30)))

    assert snapshot.instrument is MarketInstrument(instrument)
    assert snapshot.provider == "licensed_market_vendor"
    assert snapshot.value == Decimal("39819.11")
    assert snapshot.fetched_at == NOW
    assert requests[0].headers["x-provider-key"] == "market-secret"
    assert requests[0].url.params["adjusted"] == "false"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("provider", "other_vendor"),
        ("instrument", "sp_500"),
        ("currency", "USD"),
        ("delay_status", "realtime"),
        ("value", 39819.11),
        ("as_of", (NOW - timedelta(days=8)).isoformat()),
    ],
)
def test_market_adapter_rejects_contract_drift_without_leaking_payload(
    field: str,
    value,
) -> None:
    from src.market_morning.licensed_market_data import LicensedMarketDataAdapter
    from src.market_morning.market_snapshots import MarketDataUnavailable

    payload = deepcopy(_payload())
    payload[field] = value
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            headers={"content-type": "application/json"},
            content=json.dumps(payload).encode(),
        )
    )
    adapter = LicensedMarketDataAdapter(
        _config(),
        clock=lambda: NOW,
        resolver=_public_resolver,
        transport=transport,
    )

    with pytest.raises(MarketDataUnavailable) as error:
        asyncio.run(adapter.fetch(at=NOW))
    assert str(error.value) == "licensed_market_data_unavailable"
    assert "other_vendor" not in str(error.value)


def test_market_adapter_rejects_redirect_oversize_and_private_dns() -> None:
    from src.market_morning.licensed_market_data import (
        LicensedMarketDataAdapter,
        LicensedMarketDataFeedConfig,
    )
    from src.market_morning.market_snapshots import MarketDataUnavailable

    config = LicensedMarketDataFeedConfig(
        provider="licensed_market_vendor",
        instrument="nikkei_225",
        expected_currency="JPY",
        endpoint_url="https://quotes.vendor.example/v2/observations/nikkei_225",
        allowed_base_urls=("https://quotes.vendor.example/v2/observations/",),
        allowed_delay_statuses=("eod",),
        max_response_bytes=64,
    )
    redirect = LicensedMarketDataAdapter(
        config,
        clock=lambda: NOW,
        resolver=_public_resolver,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(302, headers={"location": "/private"})
        ),
    )
    oversize = LicensedMarketDataAdapter(
        config,
        clock=lambda: NOW,
        resolver=_public_resolver,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                headers={"content-type": "application/json"},
                content=b"x" * 65,
            )
        ),
    )

    async def private(host: str, port: int) -> tuple[str, ...]:
        return ("10.0.0.1",)

    private_target = LicensedMarketDataAdapter(
        config,
        clock=lambda: NOW,
        resolver=private,
        transport=httpx.MockTransport(lambda request: httpx.Response(200)),
    )
    for adapter in (redirect, oversize, private_target):
        with pytest.raises(MarketDataUnavailable, match="licensed_market_data_unavailable"):
            asyncio.run(adapter.fetch(at=NOW))


def test_market_adapter_requires_aware_request_time_and_approved_path() -> None:
    from src.market_morning.licensed_market_data import (
        LicensedMarketDataAdapter,
        LicensedMarketDataFeedConfig,
    )

    adapter = LicensedMarketDataAdapter(_config())
    with pytest.raises(ValueError, match="timezone-aware"):
        asyncio.run(adapter.fetch(at=NOW.replace(tzinfo=None)))
    with pytest.raises(ValueError, match="endpoint"):
        LicensedMarketDataFeedConfig(
            provider="licensed_market_vendor",
            instrument="nikkei_225",
            expected_currency="JPY",
            endpoint_url="https://quotes.vendor.example/v2/admin/secret",
            allowed_base_urls=("https://quotes.vendor.example/v2/observations/",),
            allowed_delay_statuses=("eod",),
        )
    with pytest.raises(ValueError, match="endpoint"):
        LicensedMarketDataFeedConfig(
            provider="licensed_market_vendor",
            instrument="nikkei_225",
            expected_currency="JPY",
            endpoint_url=(
                "https://quotes.vendor.example/v2/observations/nikkei_225"
                "?api_key=must-not-live-in-a-url"
            ),
            allowed_base_urls=("https://quotes.vendor.example/v2/observations/",),
            allowed_delay_statuses=("eod",),
        )
