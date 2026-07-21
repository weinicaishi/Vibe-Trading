"""Fail-closed scheduler and worker process runtime for Market Morning."""

from __future__ import annotations

import asyncio
import importlib
import inspect
import logging
import signal
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.market_morning.calendar import MarketCode, TradingCalendar
from src.market_morning.calendar.scheduler import (
    SchedulerTickStatus,
    canonical_source_providers,
    run_scheduler_tick,
)
from src.market_morning.db import (
    EXPECTED_MARKET_MORNING_SCHEMA_REVISION,
    get_session_factory,
)
from src.market_morning.jobs.worker import JobHandler, WorkerLoopSummary, run_worker
from src.market_morning.repositories.jobs import MarketMorningJobType

logger = logging.getLogger(__name__)

_REQUIRED_WORKER_HANDLERS = frozenset(
    {
        MarketMorningJobType.ACCOUNT_DELETION,
        MarketMorningJobType.SOURCE_INGESTION,
        MarketMorningJobType.MARKET_SNAPSHOT,
        MarketMorningJobType.GLOBAL_EDITION_RUN,
        MarketMorningJobType.EVENT_BRIEF_GENERATION,
        MarketMorningJobType.EDITION_GENERATION,
        MarketMorningJobType.EMAIL_DELIVERY,
    }
)

REQUIRED_WORKER_PREFLIGHT_CHECKS = (
    "licensed_sources",
    "market_snapshots",
    "event_brief_model",
    "source_reachability",
    "email_delivery",
)

RuntimePreflightCheck = Callable[[], Awaitable[None]]


class MarketMorningRuntimeRole(StrEnum):
    WORKER = "worker"
    SCHEDULER = "scheduler"
    ALL = "all"


class MarketMorningRuntimeConfigurationError(RuntimeError):
    """Safe startup failure that can be logged without provider details."""

    def __init__(
        self,
        error_code: str,
        *,
        dependency_name: str | None = None,
    ) -> None:
        self.error_code = error_code
        self.dependency_name = dependency_name
        super().__init__(error_code)


@dataclass(frozen=True, slots=True)
class MarketMorningRuntimeDependencies:
    """Deployment-owned licensed adapters reduced to executable ports.

    The core repository deliberately does not choose or embed commercial data
    providers. A deployment factory creates these dependencies after its data
    licensing and secret-management policy has been approved.
    """

    handlers: Mapping[MarketMorningJobType, JobHandler]
    jp_calendar: TradingCalendar | None
    us_calendar: TradingCalendar | None
    source_providers: tuple[str, ...] = ()
    session_factory: async_sessionmaker[AsyncSession] | Any | None = None
    preflight_checks: Mapping[str, RuntimePreflightCheck] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SchedulerLoopSummary:
    iterations: int
    enqueued_jobs: int
    already_enqueued_jobs: int
    busy_ticks: int
    no_action_ticks: int
    infrastructure_failures: int


@dataclass(frozen=True, slots=True)
class MarketMorningRuntimeSummary:
    role: MarketMorningRuntimeRole
    worker: WorkerLoopSummary | None
    scheduler: SchedulerLoopSummary | None


def _calendar_contract(
    calendar: TradingCalendar | None,
    *,
    expected_market: MarketCode,
    allow_fixture_dependencies: bool,
) -> None:
    if calendar is None or getattr(calendar, "market", None) is not expected_market:
        raise MarketMorningRuntimeConfigurationError("runtime_calendar_contract_invalid")
    provider = getattr(calendar, "provider", None)
    if not isinstance(provider, str) or not provider.strip():
        raise MarketMorningRuntimeConfigurationError("runtime_calendar_contract_invalid")
    if provider.startswith("fixture_") and not allow_fixture_dependencies:
        raise MarketMorningRuntimeConfigurationError("runtime_fixture_dependency_forbidden")


def validate_runtime_dependencies(
    dependencies: MarketMorningRuntimeDependencies,
    *,
    role: MarketMorningRuntimeRole | str,
    allow_fixture_dependencies: bool = False,
) -> None:
    """Reject partial process wiring before any queue item can be claimed."""

    canonical_role = MarketMorningRuntimeRole(role)
    if dependencies.session_factory is None:
        raise MarketMorningRuntimeConfigurationError("runtime_database_factory_missing")
    if canonical_role in {
        MarketMorningRuntimeRole.WORKER,
        MarketMorningRuntimeRole.ALL,
    }:
        registered = frozenset(dependencies.handlers)
        if not _REQUIRED_WORKER_HANDLERS <= registered or any(
            not callable(dependencies.handlers[job_type]) for job_type in _REQUIRED_WORKER_HANDLERS & registered
        ):
            raise MarketMorningRuntimeConfigurationError("runtime_worker_handlers_incomplete")
        if not allow_fixture_dependencies and (
            any(name not in dependencies.preflight_checks for name in REQUIRED_WORKER_PREFLIGHT_CHECKS)
            or any(not callable(dependencies.preflight_checks.get(name)) for name in REQUIRED_WORKER_PREFLIGHT_CHECKS)
        ):
            raise MarketMorningRuntimeConfigurationError("runtime_preflight_checks_incomplete")
    if canonical_role in {
        MarketMorningRuntimeRole.SCHEDULER,
        MarketMorningRuntimeRole.ALL,
    }:
        try:
            source_providers = canonical_source_providers(dependencies.source_providers)
        except ValueError:
            raise MarketMorningRuntimeConfigurationError("runtime_source_providers_invalid") from None
        if not source_providers:
            raise MarketMorningRuntimeConfigurationError("runtime_source_providers_missing")
        if not allow_fixture_dependencies and any(provider.startswith("fixture_") for provider in source_providers):
            raise MarketMorningRuntimeConfigurationError("runtime_fixture_dependency_forbidden")
        _calendar_contract(
            dependencies.jp_calendar,
            expected_market=MarketCode.JPX_CASH,
            allow_fixture_dependencies=allow_fixture_dependencies,
        )
        _calendar_contract(
            dependencies.us_calendar,
            expected_market=MarketCode.US_CASH,
            allow_fixture_dependencies=allow_fixture_dependencies,
        )


async def preflight_runtime_database(
    session_factory: async_sessionmaker[AsyncSession] | Any,
) -> None:
    """Prove connectivity and the exact isolated Alembic revision."""

    try:
        async with session_factory.begin() as session:
            result = await session.execute(text("SELECT version_num FROM alembic_version"))
            revision = result.scalar_one_or_none()
    except MarketMorningRuntimeConfigurationError:
        raise
    except Exception as error:
        logger.error(
            "Market Morning database preflight failed: exception_type=%s",
            type(error).__name__,
        )
        raise MarketMorningRuntimeConfigurationError("runtime_database_unavailable") from None
    if revision != EXPECTED_MARKET_MORNING_SCHEMA_REVISION:
        raise MarketMorningRuntimeConfigurationError("runtime_database_schema_outdated")


async def run_runtime_preflight_checks(
    dependencies: MarketMorningRuntimeDependencies,
    *,
    role: MarketMorningRuntimeRole | str,
    allow_fixture_dependencies: bool = False,
    per_check_timeout: timedelta = timedelta(seconds=10),
) -> tuple[str, ...]:
    """Run deployment-owned, no-side-effect probes before worker creation."""

    canonical_role = MarketMorningRuntimeRole(role)
    if canonical_role is MarketMorningRuntimeRole.SCHEDULER:
        return ()
    if not timedelta(milliseconds=50) <= per_check_timeout <= timedelta(minutes=1):
        raise ValueError("per_check_timeout must be between 50 milliseconds and 1 minute")

    completed: list[str] = []
    for dependency_name in REQUIRED_WORKER_PREFLIGHT_CHECKS:
        check = dependencies.preflight_checks.get(dependency_name)
        if check is None and allow_fixture_dependencies:
            continue
        if not callable(check):
            raise MarketMorningRuntimeConfigurationError("runtime_preflight_checks_incomplete")
        try:
            await asyncio.wait_for(
                check(),
                timeout=per_check_timeout.total_seconds(),
            )
        except TimeoutError:
            logger.error(
                "Market Morning dependency preflight timed out: dependency_name=%s",
                dependency_name,
            )
            raise MarketMorningRuntimeConfigurationError(
                "runtime_preflight_timeout",
                dependency_name=dependency_name,
            ) from None
        except Exception as error:
            logger.error(
                "Market Morning dependency preflight failed: dependency_name=%s exception_type=%s",
                dependency_name,
                type(error).__name__,
            )
            raise MarketMorningRuntimeConfigurationError(
                "runtime_preflight_failed",
                dependency_name=dependency_name,
            ) from None
        completed.append(dependency_name)
    return tuple(completed)


async def _wait_for_stop(stop_event: asyncio.Event, seconds: float) -> None:
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=seconds)
    except TimeoutError:
        return


async def run_scheduler_loop(
    *,
    owner_id: str,
    jp_calendar: TradingCalendar,
    us_calendar: TradingCalendar,
    source_providers: tuple[str, ...],
    stop_event: asyncio.Event,
    session_factory: async_sessionmaker[AsyncSession] | Any | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    poll_interval: timedelta = timedelta(seconds=30),
) -> SchedulerLoopSummary:
    """Evaluate one DB-leased scheduler tick per short transaction."""

    if not timedelta(milliseconds=50) <= poll_interval <= timedelta(minutes=1):
        raise ValueError("poll_interval must be between 50 milliseconds and 1 minute")
    if not owner_id.strip() or len(owner_id) > 128:
        raise ValueError("owner_id must contain 1 to 128 characters")
    if stop_event.is_set():
        return SchedulerLoopSummary(0, 0, 0, 0, 0, 0)

    factory = session_factory or get_session_factory()
    iterations = 0
    enqueued_jobs = 0
    already_enqueued_jobs = 0
    busy_ticks = 0
    no_action_ticks = 0
    infrastructure_failures = 0

    while not stop_event.is_set():
        iterations += 1
        try:
            async with factory.begin() as session:
                result = await run_scheduler_tick(
                    session,
                    owner_id=owner_id,
                    now=clock(),
                    jp_calendar=jp_calendar,
                    us_calendar=us_calendar,
                    source_providers=source_providers,
                )
            if result.status is SchedulerTickStatus.ENQUEUED:
                enqueued_jobs += 1
            elif result.status is SchedulerTickStatus.ALREADY_ENQUEUED:
                already_enqueued_jobs += 1
            elif result.status is SchedulerTickStatus.BUSY:
                busy_ticks += 1
            else:
                no_action_ticks += 1
        except Exception as error:
            infrastructure_failures += 1
            logger.error(
                "Market Morning scheduler tick failed: owner_id=%s exception_type=%s",
                owner_id,
                type(error).__name__,
            )
        if not stop_event.is_set():
            await _wait_for_stop(stop_event, poll_interval.total_seconds())

    return SchedulerLoopSummary(
        iterations=iterations,
        enqueued_jobs=enqueued_jobs,
        already_enqueued_jobs=already_enqueued_jobs,
        busy_ticks=busy_ticks,
        no_action_ticks=no_action_ticks,
        infrastructure_failures=infrastructure_failures,
    )


def install_shutdown_signal_handlers(
    stop_event: asyncio.Event,
    *,
    loop: asyncio.AbstractEventLoop | Any | None = None,
) -> tuple[signal.Signals, ...]:
    """Route process termination signals into the shared cooperative stop event."""

    event_loop = loop or asyncio.get_running_loop()
    registered: list[signal.Signals] = []
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            event_loop.add_signal_handler(sig, stop_event.set)
        except (NotImplementedError, RuntimeError, ValueError):
            continue
        registered.append(sig)
    return tuple(registered)


async def run_market_morning_runtime(
    *,
    role: MarketMorningRuntimeRole | str,
    dependencies: MarketMorningRuntimeDependencies,
    stop_event: asyncio.Event,
    worker_id: str,
    scheduler_owner_id: str,
    allow_fixture_dependencies: bool = False,
    worker_poll_interval: timedelta = timedelta(seconds=1),
    scheduler_poll_interval: timedelta = timedelta(seconds=30),
) -> MarketMorningRuntimeSummary:
    """Preflight once, then run the selected cooperative process loops."""

    canonical_role = MarketMorningRuntimeRole(role)
    validate_runtime_dependencies(
        dependencies,
        role=canonical_role,
        allow_fixture_dependencies=allow_fixture_dependencies,
    )
    assert dependencies.session_factory is not None
    await preflight_runtime_database(dependencies.session_factory)
    await run_runtime_preflight_checks(
        dependencies,
        role=canonical_role,
        allow_fixture_dependencies=allow_fixture_dependencies,
    )

    worker_task: asyncio.Task[WorkerLoopSummary] | None = None
    scheduler_task: asyncio.Task[SchedulerLoopSummary] | None = None
    if canonical_role in {
        MarketMorningRuntimeRole.WORKER,
        MarketMorningRuntimeRole.ALL,
    }:
        worker_task = asyncio.create_task(
            run_worker(
                worker_id=worker_id,
                handlers=dependencies.handlers,
                stop_event=stop_event,
                session_factory=dependencies.session_factory,
                poll_interval=worker_poll_interval,
            )
        )
    if canonical_role in {
        MarketMorningRuntimeRole.SCHEDULER,
        MarketMorningRuntimeRole.ALL,
    }:
        assert dependencies.jp_calendar is not None
        assert dependencies.us_calendar is not None
        scheduler_task = asyncio.create_task(
            run_scheduler_loop(
                owner_id=scheduler_owner_id,
                jp_calendar=dependencies.jp_calendar,
                us_calendar=dependencies.us_calendar,
                source_providers=dependencies.source_providers,
                stop_event=stop_event,
                session_factory=dependencies.session_factory,
                poll_interval=scheduler_poll_interval,
            )
        )

    tasks = tuple(task for task in (worker_task, scheduler_task) if task is not None)
    try:
        await asyncio.gather(*tasks)
    except BaseException:
        stop_event.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise

    return MarketMorningRuntimeSummary(
        role=canonical_role,
        worker=worker_task.result() if worker_task is not None else None,
        scheduler=scheduler_task.result() if scheduler_task is not None else None,
    )


async def load_runtime_dependencies(
    factory_path: str,
) -> MarketMorningRuntimeDependencies:
    """Load a deployment-owned ``module:function`` dependency factory."""

    normalized = factory_path.strip()
    if not normalized or normalized.count(":") != 1:
        raise MarketMorningRuntimeConfigurationError("runtime_factory_invalid")
    module_name, attribute_name = normalized.split(":", 1)
    if not module_name or not attribute_name:
        raise MarketMorningRuntimeConfigurationError("runtime_factory_invalid")
    try:
        factory = getattr(importlib.import_module(module_name), attribute_name)
        dependencies = factory()
        if inspect.isawaitable(dependencies):
            dependencies = await dependencies
    except MarketMorningRuntimeConfigurationError:
        raise
    except Exception as error:
        logger.error(
            "Market Morning runtime factory failed: exception_type=%s",
            type(error).__name__,
        )
        raise MarketMorningRuntimeConfigurationError("runtime_factory_failed") from None
    if not isinstance(dependencies, MarketMorningRuntimeDependencies):
        raise MarketMorningRuntimeConfigurationError("runtime_factory_contract_invalid")
    return dependencies


__all__ = [
    "EXPECTED_MARKET_MORNING_SCHEMA_REVISION",
    "MarketMorningRuntimeConfigurationError",
    "MarketMorningRuntimeDependencies",
    "MarketMorningRuntimeRole",
    "MarketMorningRuntimeSummary",
    "REQUIRED_WORKER_PREFLIGHT_CHECKS",
    "RuntimePreflightCheck",
    "SchedulerLoopSummary",
    "install_shutdown_signal_handlers",
    "load_runtime_dependencies",
    "preflight_runtime_database",
    "run_market_morning_runtime",
    "run_runtime_preflight_checks",
    "run_scheduler_loop",
    "validate_runtime_dependencies",
]
