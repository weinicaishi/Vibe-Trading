"""Strict assembly helpers for a deployment-owned Market Morning factory.

This module does not choose vendors or read secrets.  It turns already
approved adapters and ports into the complete runtime dependency contract,
derives two read-only registry probes from the actual adapters, and rejects
partial or fixture wiring before the worker can claim a job.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.market_morning.calendar import MarketCode, TradingCalendar
from src.market_morning.jobs.handlers import (
    EditionRunner,
    EmailDeliveryRunner,
    EmailDispatchRunner,
    EventBriefDispatchConfig,
    EventBriefRunner,
    build_job_handlers,
    run_morning_edition_generation,
)
from src.market_morning.market_snapshots import (
    MarketDataAdapter,
    MarketInstrument,
    MarketSnapshot,
)
from src.market_morning.runtime import (
    MarketMorningRuntimeConfigurationError,
    MarketMorningRuntimeDependencies,
    MarketMorningRuntimeRole,
    REQUIRED_WORKER_PREFLIGHT_CHECKS,
    RuntimePreflightCheck,
    validate_runtime_dependencies,
)
from src.market_morning.sources.base import SourceAdapter, SourceHealth


def _configuration_error(code: str) -> MarketMorningRuntimeConfigurationError:
    return MarketMorningRuntimeConfigurationError(code)


def _source_registry(
    adapters: Mapping[str, SourceAdapter],
) -> Mapping[str, SourceAdapter]:
    if not isinstance(adapters, Mapping) or not adapters:
        raise _configuration_error("runtime_source_adapters_missing")
    canonical: dict[str, SourceAdapter] = {}
    for key, adapter in adapters.items():
        if (
            not isinstance(key, str)
            or not key
            or len(key) > 64
            or key != key.strip()
            or key.startswith("fixture_")
            or getattr(adapter, "provider", None) != key
            or not all(
                callable(getattr(adapter, method, None))
                for method in ("discover", "fetch", "normalize", "health")
            )
        ):
            raise _configuration_error("runtime_source_adapters_invalid")
        canonical[key] = adapter
    return MappingProxyType(dict(sorted(canonical.items())))


def _market_registry(
    adapters: Mapping[MarketInstrument | str, MarketDataAdapter],
) -> Mapping[MarketInstrument, MarketDataAdapter]:
    if not isinstance(adapters, Mapping):
        raise _configuration_error("runtime_market_adapters_incomplete")
    try:
        canonical = {
            MarketInstrument(instrument): adapter
            for instrument, adapter in adapters.items()
        }
    except (TypeError, ValueError):
        raise _configuration_error("runtime_market_adapters_invalid") from None
    if set(canonical) != set(MarketInstrument):
        raise _configuration_error("runtime_market_adapters_incomplete")
    for instrument, adapter in canonical.items():
        provider = getattr(adapter, "provider", None)
        if (
            getattr(adapter, "instrument", None) is not instrument
            or not isinstance(provider, str)
            or not provider
            or len(provider) > 64
            or provider != provider.strip()
            or provider.startswith("fixture_")
            or not callable(getattr(adapter, "fetch", None))
        ):
            raise _configuration_error("runtime_market_adapters_invalid")
    return MappingProxyType(
        {instrument: canonical[instrument] for instrument in MarketInstrument}
    )


def _calendar(
    calendar: TradingCalendar,
    *,
    expected_market: MarketCode,
) -> TradingCalendar:
    provider = getattr(calendar, "provider", None)
    if (
        getattr(calendar, "market", None) is not expected_market
        or not isinstance(provider, str)
        or not provider
        or provider.startswith("fixture_")
        or not all(
            callable(getattr(calendar, method, None))
            for method in ("session_on", "previous_open_session", "next_open_session")
        )
    ):
        raise _configuration_error("runtime_calendar_contract_invalid")
    return calendar


def build_adapter_preflight_checks(
    *,
    source_adapters: Mapping[str, SourceAdapter],
    market_data_adapters: Mapping[MarketInstrument, MarketDataAdapter],
    event_brief_model_check: RuntimePreflightCheck,
    source_reachability_check: RuntimePreflightCheck,
    email_delivery_check: RuntimePreflightCheck,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> Mapping[str, RuntimePreflightCheck]:
    """Build all five no-write probes from the exact runtime registries."""

    external_checks = {
        "event_brief_model": event_brief_model_check,
        "source_reachability": source_reachability_check,
        "email_delivery": email_delivery_check,
    }
    if not callable(clock) or any(
        not callable(check) for check in external_checks.values()
    ):
        raise _configuration_error("runtime_preflight_checks_incomplete")

    async def licensed_sources() -> None:
        results = await asyncio.gather(
            *(adapter.health() for adapter in source_adapters.values())
        )
        if len(results) != len(source_adapters) or any(
            not isinstance(result, SourceHealth) or not result.ok
            for result in results
        ):
            raise RuntimeError("licensed_sources_not_ready")

    async def market_snapshots() -> None:
        requested_at = clock()
        if requested_at.tzinfo is None or requested_at.utcoffset() is None:
            raise RuntimeError("market_snapshot_clock_invalid")
        results = await asyncio.gather(
            *(
                adapter.fetch(at=requested_at)
                for adapter in market_data_adapters.values()
            )
        )
        if len(results) != len(MarketInstrument):
            raise RuntimeError("market_snapshot_registry_incomplete")
        for instrument, snapshot in zip(MarketInstrument, results, strict=True):
            adapter = market_data_adapters[instrument]
            if (
                not isinstance(snapshot, MarketSnapshot)
                or snapshot.instrument is not instrument
                or snapshot.provider != adapter.provider
            ):
                raise RuntimeError("market_snapshot_contract_invalid")

    checks: dict[str, RuntimePreflightCheck] = {
        "licensed_sources": licensed_sources,
        "market_snapshots": market_snapshots,
        **external_checks,
    }
    if tuple(checks) != REQUIRED_WORKER_PREFLIGHT_CHECKS:
        raise _configuration_error("runtime_preflight_checks_incomplete")
    return MappingProxyType(checks)


def assemble_runtime_dependencies(
    *,
    source_adapters: Mapping[str, SourceAdapter],
    market_data_adapters: Mapping[MarketInstrument | str, MarketDataAdapter],
    jp_calendar: TradingCalendar,
    us_calendar: TradingCalendar,
    session_factory: async_sessionmaker[AsyncSession] | Any,
    event_brief_runner: EventBriefRunner,
    event_brief_dispatch: EventBriefDispatchConfig,
    email_delivery_runner: EmailDeliveryRunner,
    email_dispatch_runner: EmailDispatchRunner,
    event_brief_model_check: RuntimePreflightCheck,
    source_reachability_check: RuntimePreflightCheck,
    email_delivery_check: RuntimePreflightCheck,
    edition_runner: EditionRunner = run_morning_edition_generation,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> MarketMorningRuntimeDependencies:
    """Assemble a complete production dependency graph or fail closed."""

    sources = _source_registry(source_adapters)
    markets = _market_registry(market_data_adapters)
    jp = _calendar(jp_calendar, expected_market=MarketCode.JPX_CASH)
    us = _calendar(us_calendar, expected_market=MarketCode.US_CASH)
    if session_factory is None:
        raise _configuration_error("runtime_database_factory_missing")
    if not all(
        callable(value)
        for value in (
            event_brief_runner,
            email_delivery_runner,
            email_dispatch_runner,
            edition_runner,
            clock,
        )
    ) or not isinstance(event_brief_dispatch, EventBriefDispatchConfig):
        raise _configuration_error("runtime_worker_ports_incomplete")
    checks = build_adapter_preflight_checks(
        source_adapters=sources,
        market_data_adapters=markets,
        event_brief_model_check=event_brief_model_check,
        source_reachability_check=source_reachability_check,
        email_delivery_check=email_delivery_check,
        clock=clock,
    )
    providers = {
        instrument: adapter.provider for instrument, adapter in markets.items()
    }
    try:
        handlers = build_job_handlers(
            adapters=sources,
            market_data_adapters=markets,
            session_factory=session_factory,
            edition_runner=edition_runner,
            event_brief_runner=event_brief_runner,
            event_brief_dispatch=event_brief_dispatch,
            email_delivery_runner=email_delivery_runner,
            email_dispatch_runner=email_dispatch_runner,
            global_market_data_providers=providers,
            clock=clock,
        )
    except Exception:
        raise _configuration_error("runtime_worker_handlers_incomplete") from None
    dependencies = MarketMorningRuntimeDependencies(
        handlers=MappingProxyType(dict(handlers)),
        jp_calendar=jp,
        us_calendar=us,
        source_providers=tuple(sources),
        session_factory=session_factory,
        preflight_checks=checks,
    )
    validate_runtime_dependencies(
        dependencies,
        role=MarketMorningRuntimeRole.ALL,
        allow_fixture_dependencies=False,
    )
    return dependencies


__all__ = [
    "assemble_runtime_dependencies",
    "build_adapter_preflight_checks",
]
