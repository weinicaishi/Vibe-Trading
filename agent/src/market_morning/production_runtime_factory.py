"""First-party no-argument production runtime factory for Market Morning.

Deployment code still owns licensed calendar, market, source and connectivity
adapters.  This module owns the final composition: it loads one reviewed
provider bundle, builds the policy-locked OpenRouter and Resend ports from the
secret store, binds MySQL, and invokes the strict runtime assembler.
"""

from __future__ import annotations

import importlib
import inspect
import logging
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.market_morning.calendar import TradingCalendar
from src.market_morning.db import get_session_factory
from src.market_morning.deployment_runtime import assemble_runtime_dependencies
from src.market_morning.email_runtime import (
    ResendEmailRuntimeRunners,
    build_resend_email_runtime_runners,
)
from src.market_morning.event_brief_model_adapter import (
    OpenRouterEventBriefGenerator,
    build_openrouter_event_brief_generator_from_env,
)
from src.market_morning.event_brief_service import (
    EventBriefGenerationCommand,
    EventBriefGenerationResult,
    SourceReachabilityChecker,
    run_event_brief_generation,
)
from src.market_morning.jobs.handlers import EventBriefDispatchConfig
from src.market_morning.market_snapshots import MarketDataAdapter, MarketInstrument
from src.market_morning.runtime import (
    MarketMorningRuntimeConfigurationError,
    MarketMorningRuntimeDependencies,
    RuntimePreflightCheck,
)
from src.market_morning.sources.base import SourceAdapter


logger = logging.getLogger(__name__)

PRODUCTION_RUNTIME_FACTORY_PATH = "src.market_morning.production_runtime_factory:build_runtime_dependencies"
PROVIDER_BUNDLE_FACTORY_ENV = "VIBE_MARKET_MORNING_PROVIDER_BUNDLE_FACTORY"
OPENROUTER_MODEL_ENV = "VIBE_MARKET_MORNING_OPENROUTER_MODEL"
EVENT_BRIEF_PROMPT_VERSION = "market-morning-event-brief-v1"

ProviderBundleFactory = Callable[[], "ApprovedMarketMorningProviderBundle"]
ModelGeneratorBuilder = Callable[[Mapping[str, str]], OpenRouterEventBriefGenerator | Any]
EmailRuntimeBuilder = Callable[
    [async_sessionmaker[AsyncSession] | Any, Mapping[str, str]],
    ResendEmailRuntimeRunners,
]


def _configuration_error(code: str) -> MarketMorningRuntimeConfigurationError:
    return MarketMorningRuntimeConfigurationError(code)


@dataclass(frozen=True, slots=True, repr=False)
class ApprovedMarketMorningProviderBundle:
    """Reviewed non-secret deployment ports required by the first-party factory."""

    source_adapters: Mapping[str, SourceAdapter] = field(repr=False)
    market_data_adapters: Mapping[MarketInstrument | str, MarketDataAdapter] = field(repr=False)
    jp_calendar: TradingCalendar = field(repr=False)
    us_calendar: TradingCalendar = field(repr=False)
    source_reachability_checker: SourceReachabilityChecker = field(repr=False)
    event_brief_model_check: RuntimePreflightCheck = field(repr=False)
    source_reachability_check: RuntimePreflightCheck = field(repr=False)
    email_delivery_check: RuntimePreflightCheck = field(repr=False)
    event_brief_provider: str = "openrouter"
    email_provider: str = "resend"

    def __post_init__(self) -> None:
        if (
            self.event_brief_provider != "openrouter"
            or self.email_provider != "resend"
            or not callable(self.source_reachability_checker)
            or not callable(self.event_brief_model_check)
            or not callable(self.source_reachability_check)
            or not callable(self.email_delivery_check)
        ):
            raise _configuration_error("production_provider_bundle_contract_invalid")

    def __repr__(self) -> str:
        return "<ApprovedMarketMorningProviderBundle configured>"


async def load_approved_provider_bundle(
    factory_path: str,
) -> ApprovedMarketMorningProviderBundle:
    """Load one sync or async zero-argument provider bundle factory safely."""

    normalized = factory_path.strip()
    if not normalized or normalized.count(":") != 1:
        raise _configuration_error("production_provider_bundle_factory_invalid")
    module_name, attribute_name = normalized.split(":", 1)
    if not module_name or not attribute_name:
        raise _configuration_error("production_provider_bundle_factory_invalid")
    try:
        factory = getattr(importlib.import_module(module_name), attribute_name)
        if not callable(factory):
            raise TypeError("provider bundle factory is not callable")
        bundle = factory()
        if inspect.isawaitable(bundle):
            bundle = await bundle
    except MarketMorningRuntimeConfigurationError:
        raise
    except Exception as error:
        logger.error(
            "Market Morning provider bundle factory failed: exception_type=%s",
            type(error).__name__,
        )
        raise _configuration_error("production_provider_bundle_factory_failed") from None
    if not isinstance(bundle, ApprovedMarketMorningProviderBundle):
        raise _configuration_error("production_provider_bundle_contract_invalid")
    return bundle


def _default_model_builder(
    environ: Mapping[str, str],
) -> OpenRouterEventBriefGenerator:
    return build_openrouter_event_brief_generator_from_env(environ)


def _default_email_builder(
    session_factory: async_sessionmaker[AsyncSession] | Any,
    environ: Mapping[str, str],
) -> ResendEmailRuntimeRunners:
    return build_resend_email_runtime_runners(
        session_factory=session_factory,
        environ=environ,
    )


def assemble_production_runtime_dependencies(
    *,
    provider_bundle: ApprovedMarketMorningProviderBundle,
    session_factory: async_sessionmaker[AsyncSession] | Any,
    environ: Mapping[str, str],
    model_builder: ModelGeneratorBuilder = _default_model_builder,
    email_builder: EmailRuntimeBuilder = _default_email_builder,
) -> MarketMorningRuntimeDependencies:
    """Compose built-in production ports with one approved provider bundle."""

    if not isinstance(provider_bundle, ApprovedMarketMorningProviderBundle):
        raise _configuration_error("production_provider_bundle_contract_invalid")
    if not isinstance(environ, Mapping):
        raise _configuration_error("production_runtime_environment_invalid")
    if not callable(getattr(session_factory, "begin", None)):
        raise _configuration_error("runtime_database_factory_missing")
    model_version = environ.get(OPENROUTER_MODEL_ENV, "")
    try:
        event_brief_dispatch = EventBriefDispatchConfig(
            model_version=model_version,
            prompt_version=EVENT_BRIEF_PROMPT_VERSION,
        )
        generator = model_builder(environ)
    except Exception:
        raise _configuration_error("production_model_configuration_invalid") from None
    if not callable(generator):
        raise _configuration_error("production_model_configuration_invalid")
    try:
        email_runners = email_builder(session_factory, environ)
    except Exception:
        raise _configuration_error("production_email_configuration_invalid") from None
    if not isinstance(email_runners, ResendEmailRuntimeRunners):
        raise _configuration_error("production_email_configuration_invalid")

    async def event_brief_runner(
        command: EventBriefGenerationCommand,
    ) -> EventBriefGenerationResult:
        return await run_event_brief_generation(
            command,
            generator=generator,
            reachability_checker=provider_bundle.source_reachability_checker,
            session_factory=session_factory,
        )

    return assemble_runtime_dependencies(
        source_adapters=provider_bundle.source_adapters,
        market_data_adapters=provider_bundle.market_data_adapters,
        jp_calendar=provider_bundle.jp_calendar,
        us_calendar=provider_bundle.us_calendar,
        session_factory=session_factory,
        event_brief_runner=event_brief_runner,
        event_brief_dispatch=event_brief_dispatch,
        email_delivery_runner=email_runners.email_delivery_runner,
        email_dispatch_runner=email_runners.email_dispatch_runner,
        event_brief_model_check=provider_bundle.event_brief_model_check,
        source_reachability_check=provider_bundle.source_reachability_check,
        email_delivery_check=provider_bundle.email_delivery_check,
    )


async def build_runtime_dependencies_from_environment(
    environ: Mapping[str, str],
    *,
    session_factory: async_sessionmaker[AsyncSession] | Any | None = None,
    model_builder: ModelGeneratorBuilder = _default_model_builder,
    email_builder: EmailRuntimeBuilder = _default_email_builder,
) -> MarketMorningRuntimeDependencies:
    """Load the reviewed bundle and assemble the complete production graph."""

    if not isinstance(environ, Mapping):
        raise _configuration_error("production_runtime_environment_invalid")
    provider_bundle_factory = environ.get(PROVIDER_BUNDLE_FACTORY_ENV, "")
    if not isinstance(provider_bundle_factory, str) or not provider_bundle_factory.strip():
        raise _configuration_error("production_provider_bundle_factory_missing")
    bundle = await load_approved_provider_bundle(provider_bundle_factory)
    return assemble_production_runtime_dependencies(
        provider_bundle=bundle,
        session_factory=(session_factory if session_factory is not None else get_session_factory()),
        environ=environ,
        model_builder=model_builder,
        email_builder=email_builder,
    )


async def build_runtime_dependencies() -> MarketMorningRuntimeDependencies:
    """No-argument factory configured directly by the runtime environment."""

    return await build_runtime_dependencies_from_environment(os.environ.copy())


__all__ = [
    "ApprovedMarketMorningProviderBundle",
    "EVENT_BRIEF_PROMPT_VERSION",
    "OPENROUTER_MODEL_ENV",
    "PRODUCTION_RUNTIME_FACTORY_PATH",
    "PROVIDER_BUNDLE_FACTORY_ENV",
    "assemble_production_runtime_dependencies",
    "build_runtime_dependencies",
    "build_runtime_dependencies_from_environment",
    "load_approved_provider_bundle",
]
