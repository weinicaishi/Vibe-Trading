"""Production runtime dependency assembly tests."""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

NOW = datetime(2026, 7, 20, 22, 0, tzinfo=timezone.utc)


class _SourceAdapter:
    def __init__(self, provider: str, calls: list[str]) -> None:
        self.provider = provider
        self.calls = calls

    async def discover(self, cursor):
        raise AssertionError

    async def fetch(self, document):
        raise AssertionError

    def normalize(self, payload):
        raise AssertionError

    async def health(self):
        from src.market_morning.sources.base import SourceHealth

        self.calls.append(f"source:{self.provider}")
        return SourceHealth(ok=True, checked_at=NOW, detail_code="ready")


class _MarketAdapter:
    def __init__(self, instrument, calls: list[str], *, provider="licensed_quotes"):
        from src.market_morning.market_snapshots import MarketInstrument

        self.instrument = MarketInstrument(instrument)
        self.provider = provider
        self.calls = calls

    async def fetch(self, *, at):
        from src.market_morning.market_snapshots import MarketSnapshot

        self.calls.append(f"market:{self.instrument.value}")
        return MarketSnapshot(
            instrument=self.instrument,
            provider=self.provider,
            session_date=date(2026, 7, 20),
            as_of=NOW - timedelta(minutes=5),
            value=Decimal("100"),
            previous_close=Decimal("99"),
            currency=("JPY" if self.instrument.value in {"nikkei_225", "usd_jpy"} else "USD"),
            delay_status="eod",
            fetched_at=NOW,
        )


class _Calendar:
    def __init__(self, market, provider: str) -> None:
        self.market = market
        self.provider = provider

    def session_on(self, session_date):
        raise AssertionError

    def previous_open_session(self, session_date):
        raise AssertionError

    def next_open_session(self, session_date):
        raise AssertionError


def _assembly(*, market_adapters=None, source_provider="licensed_tdnet"):
    from src.market_morning.calendar import MarketCode
    from src.market_morning.jobs.handlers import EventBriefDispatchConfig
    from src.market_morning.market_snapshots import MarketInstrument

    calls: list[str] = []
    sources = {
        source_provider: _SourceAdapter(source_provider, calls),
        "licensed_edinet": _SourceAdapter("licensed_edinet", calls),
    }
    markets = market_adapters or {instrument: _MarketAdapter(instrument, calls) for instrument in MarketInstrument}

    async def event_brief_runner(command):
        raise AssertionError

    async def email_delivery_runner(command):
        raise AssertionError

    async def email_dispatch_runner(command):
        raise AssertionError

    def check(name):
        async def ready():
            calls.append(f"check:{name}")

        return ready

    kwargs = {
        "source_adapters": sources,
        "market_data_adapters": markets,
        "jp_calendar": _Calendar(MarketCode.JPX_CASH, "licensed_jpx_calendar"),
        "us_calendar": _Calendar(MarketCode.US_CASH, "licensed_us_calendar"),
        "session_factory": object(),
        "event_brief_runner": event_brief_runner,
        "event_brief_dispatch": EventBriefDispatchConfig(
            model_version="model-v1",
            prompt_version="prompt-v1",
        ),
        "email_delivery_runner": email_delivery_runner,
        "email_dispatch_runner": email_dispatch_runner,
        "event_brief_model_check": check("event_brief_model"),
        "source_reachability_check": check("source_reachability"),
        "email_delivery_check": check("email_delivery"),
        "clock": lambda: NOW,
    }
    return kwargs, calls


def test_assembly_builds_complete_handlers_exact_registries_and_real_preflights() -> None:
    from src.market_morning.deployment_runtime import assemble_runtime_dependencies
    from src.market_morning.repositories.jobs import MarketMorningJobType
    from src.market_morning.runtime import (
        MarketMorningRuntimeRole,
        run_runtime_preflight_checks,
        validate_runtime_dependencies,
    )

    kwargs, calls = _assembly()
    dependencies = assemble_runtime_dependencies(**kwargs)
    validate_runtime_dependencies(
        dependencies,
        role=MarketMorningRuntimeRole.ALL,
    )
    completed = asyncio.run(
        run_runtime_preflight_checks(
            dependencies,
            role=MarketMorningRuntimeRole.WORKER,
        )
    )

    assert set(dependencies.handlers) == set(MarketMorningJobType)
    assert dependencies.source_providers == (
        "licensed_edinet",
        "licensed_tdnet",
    )
    assert completed == (
        "licensed_sources",
        "market_snapshots",
        "event_brief_model",
        "source_reachability",
        "email_delivery",
    )
    assert calls[:2] == ["source:licensed_edinet", "source:licensed_tdnet"]
    assert calls[2:7] == [
        "market:nikkei_225",
        "market:sp_500",
        "market:nasdaq_composite",
        "market:djia",
        "market:usd_jpy",
    ]
    assert calls[7:] == [
        "check:event_brief_model",
        "check:source_reachability",
        "check:email_delivery",
    ]


def test_assembly_rejects_missing_drifted_and_fixture_registries() -> None:
    from src.market_morning.deployment_runtime import assemble_runtime_dependencies
    from src.market_morning.market_snapshots import MarketInstrument
    from src.market_morning.runtime import MarketMorningRuntimeConfigurationError

    kwargs, calls = _assembly()
    markets = dict(kwargs["market_data_adapters"])
    markets.pop(MarketInstrument.DJIA)
    kwargs["market_data_adapters"] = markets
    with pytest.raises(MarketMorningRuntimeConfigurationError) as missing:
        assemble_runtime_dependencies(**kwargs)
    assert missing.value.error_code == "runtime_market_adapters_incomplete"

    kwargs, calls = _assembly(source_provider="fixture_tdnet")
    with pytest.raises(MarketMorningRuntimeConfigurationError) as fixture:
        assemble_runtime_dependencies(**kwargs)
    assert fixture.value.error_code == "runtime_source_adapters_invalid"

    kwargs, calls = _assembly()
    markets = dict(kwargs["market_data_adapters"])
    markets[MarketInstrument.SP_500] = _MarketAdapter(
        MarketInstrument.NIKKEI_225,
        calls,
    )
    kwargs["market_data_adapters"] = markets
    with pytest.raises(MarketMorningRuntimeConfigurationError) as drift:
        assemble_runtime_dependencies(**kwargs)
    assert drift.value.error_code == "runtime_market_adapters_invalid"


def test_runtime_loader_supports_sync_and_async_deployment_factories(monkeypatch) -> None:
    import src.market_morning.runtime as runtime

    dependencies = SimpleNamespace()

    def sync_factory():
        return dependencies

    async def async_factory():
        return dependencies

    module = SimpleNamespace(sync_factory=sync_factory, async_factory=async_factory)
    monkeypatch.setattr(runtime.importlib, "import_module", lambda name: module)
    monkeypatch.setattr(runtime, "MarketMorningRuntimeDependencies", SimpleNamespace)

    assert asyncio.run(runtime.load_runtime_dependencies("deployment:sync_factory")) is dependencies
    assert asyncio.run(runtime.load_runtime_dependencies("deployment:async_factory")) is dependencies


def test_runtime_loader_sanitizes_factory_failures(monkeypatch) -> None:
    import src.market_morning.runtime as runtime

    def factory():
        raise RuntimeError("secret provider configuration")

    monkeypatch.setattr(
        runtime.importlib,
        "import_module",
        lambda name: SimpleNamespace(factory=factory),
    )
    with pytest.raises(runtime.MarketMorningRuntimeConfigurationError) as error:
        asyncio.run(runtime.load_runtime_dependencies("deployment:factory"))
    assert error.value.error_code == "runtime_factory_failed"
    assert "secret" not in str(error.value)


def test_runtime_loader_preserves_safe_configuration_error(monkeypatch) -> None:
    import src.market_morning.runtime as runtime

    def factory():
        raise runtime.MarketMorningRuntimeConfigurationError("production_provider_bundle_factory_missing")

    monkeypatch.setattr(
        runtime.importlib,
        "import_module",
        lambda name: SimpleNamespace(factory=factory),
    )
    with pytest.raises(runtime.MarketMorningRuntimeConfigurationError) as error:
        asyncio.run(runtime.load_runtime_dependencies("deployment:factory"))
    assert error.value.error_code == "production_provider_bundle_factory_missing"
