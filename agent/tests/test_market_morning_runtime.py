"""Fail-closed process runtime contracts for Market Morning."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest


def _handler_map():
    from src.market_morning.repositories.jobs import MarketMorningJobType

    async def handler(payload):
        return payload

    return {
        MarketMorningJobType.ACCOUNT_DELETION: handler,
        MarketMorningJobType.SOURCE_INGESTION: handler,
        MarketMorningJobType.MARKET_SNAPSHOT: handler,
        MarketMorningJobType.GLOBAL_EDITION_RUN: handler,
        MarketMorningJobType.EDITION_GENERATION: handler,
        MarketMorningJobType.EVENT_BRIEF_GENERATION: handler,
        MarketMorningJobType.EMAIL_DELIVERY: handler,
    }


def _preflight_checks(*, callback=None):
    async def ready() -> None:
        if callback is not None:
            callback()

    return {
        "licensed_sources": ready,
        "market_snapshots": ready,
        "event_brief_model": ready,
        "source_reachability": ready,
        "email_delivery": ready,
    }


class _Calendar:
    def __init__(self, *, market, provider: str) -> None:
        self.market = market
        self.provider = provider


def _dependencies(*, fixture: bool = False, handlers=None, preflight_checks=None):
    from src.market_morning.calendar import MarketCode
    from src.market_morning.runtime import MarketMorningRuntimeDependencies

    prefix = "fixture_" if fixture else "licensed_"
    return MarketMorningRuntimeDependencies(
        handlers=_handler_map() if handlers is None else handlers,
        jp_calendar=_Calendar(
            market=MarketCode.JPX_CASH,
            provider=f"{prefix}jpx_calendar",
        ),
        us_calendar=_Calendar(
            market=MarketCode.US_CASH,
            provider=f"{prefix}us_calendar",
        ),
        source_providers=(
            ("fixture_edinet", "fixture_tdnet")
            if fixture
            else ("edinet", "tdnet")
        ),
        session_factory=object(),
        preflight_checks=(
            _preflight_checks()
            if preflight_checks is None
            else preflight_checks
        ),
    )


def test_runtime_validation_requires_complete_worker_handlers() -> None:
    from src.market_morning.repositories.jobs import MarketMorningJobType
    from src.market_morning.runtime import (
        MarketMorningRuntimeConfigurationError,
        MarketMorningRuntimeRole,
        validate_runtime_dependencies,
    )

    handlers = _handler_map()
    handlers.pop(MarketMorningJobType.GLOBAL_EDITION_RUN)

    with pytest.raises(MarketMorningRuntimeConfigurationError) as error:
        validate_runtime_dependencies(
            _dependencies(handlers=handlers),
            role=MarketMorningRuntimeRole.WORKER,
        )

    assert error.value.error_code == "runtime_worker_handlers_incomplete"


def test_runtime_validation_rejects_fixture_calendars_unless_explicitly_allowed() -> None:
    from src.market_morning.runtime import (
        MarketMorningRuntimeConfigurationError,
        MarketMorningRuntimeRole,
        validate_runtime_dependencies,
    )

    with pytest.raises(MarketMorningRuntimeConfigurationError) as error:
        validate_runtime_dependencies(
            _dependencies(fixture=True),
            role=MarketMorningRuntimeRole.ALL,
        )
    assert error.value.error_code == "runtime_fixture_dependency_forbidden"

    validate_runtime_dependencies(
        _dependencies(fixture=True),
        role=MarketMorningRuntimeRole.ALL,
        allow_fixture_dependencies=True,
    )


def test_runtime_validation_checks_calendar_market_contract() -> None:
    from src.market_morning.calendar import MarketCode
    from src.market_morning.runtime import (
        MarketMorningRuntimeConfigurationError,
        MarketMorningRuntimeRole,
        validate_runtime_dependencies,
    )

    dependencies = _dependencies()
    dependencies.jp_calendar.market = MarketCode.US_CASH

    with pytest.raises(MarketMorningRuntimeConfigurationError) as error:
        validate_runtime_dependencies(
            dependencies,
            role=MarketMorningRuntimeRole.SCHEDULER,
        )

    assert error.value.error_code == "runtime_calendar_contract_invalid"


def test_runtime_validation_requires_non_fixture_scheduler_source_providers() -> None:
    from dataclasses import replace

    from src.market_morning.runtime import (
        MarketMorningRuntimeConfigurationError,
        MarketMorningRuntimeRole,
        validate_runtime_dependencies,
    )

    with pytest.raises(MarketMorningRuntimeConfigurationError) as missing:
        validate_runtime_dependencies(
            replace(_dependencies(), source_providers=()),
            role=MarketMorningRuntimeRole.SCHEDULER,
        )
    assert missing.value.error_code == "runtime_source_providers_missing"

    with pytest.raises(MarketMorningRuntimeConfigurationError) as fixture:
        validate_runtime_dependencies(
            replace(_dependencies(), source_providers=("fixture_edinet",)),
            role=MarketMorningRuntimeRole.SCHEDULER,
        )
    assert fixture.value.error_code == "runtime_fixture_dependency_forbidden"


def test_runtime_validation_requires_all_production_worker_preflight_checks() -> None:
    from src.market_morning.runtime import (
        MarketMorningRuntimeConfigurationError,
        MarketMorningRuntimeRole,
        validate_runtime_dependencies,
    )

    checks = _preflight_checks()
    checks.pop("email_delivery")

    with pytest.raises(MarketMorningRuntimeConfigurationError) as error:
        validate_runtime_dependencies(
            _dependencies(preflight_checks=checks),
            role=MarketMorningRuntimeRole.WORKER,
        )

    assert error.value.error_code == "runtime_preflight_checks_incomplete"


def test_runtime_preflight_checks_execute_in_stable_dependency_order() -> None:
    from src.market_morning.runtime import (
        MarketMorningRuntimeRole,
        run_runtime_preflight_checks,
    )

    called: list[str] = []

    def check(name: str):
        async def ready() -> None:
            called.append(name)

        return ready

    checks = {
        name: check(name)
        for name in reversed(
            (
                "licensed_sources",
                "market_snapshots",
                "event_brief_model",
                "source_reachability",
                "email_delivery",
            )
        )
    }

    asyncio.run(
        run_runtime_preflight_checks(
            _dependencies(preflight_checks=checks),
            role=MarketMorningRuntimeRole.WORKER,
        )
    )

    assert called == [
        "licensed_sources",
        "market_snapshots",
        "event_brief_model",
        "source_reachability",
        "email_delivery",
    ]


def test_runtime_preflight_failure_is_sanitized_and_names_fixed_check() -> None:
    from src.market_morning.runtime import (
        MarketMorningRuntimeConfigurationError,
        MarketMorningRuntimeRole,
        run_runtime_preflight_checks,
    )

    checks = _preflight_checks()

    async def fail_with_secret() -> None:
        raise RuntimeError("provider-key-and-host-must-not-escape")

    checks["event_brief_model"] = fail_with_secret

    with pytest.raises(MarketMorningRuntimeConfigurationError) as error:
        asyncio.run(
            run_runtime_preflight_checks(
                _dependencies(preflight_checks=checks),
                role=MarketMorningRuntimeRole.WORKER,
            )
        )

    assert error.value.error_code == "runtime_preflight_failed"
    assert error.value.dependency_name == "event_brief_model"
    assert "provider-key" not in str(error.value)


def test_runtime_preflight_timeout_uses_stable_sanitized_error() -> None:
    from src.market_morning.runtime import (
        MarketMorningRuntimeConfigurationError,
        MarketMorningRuntimeRole,
        run_runtime_preflight_checks,
    )

    checks = _preflight_checks()

    async def never_ready() -> None:
        await asyncio.sleep(1)

    checks["licensed_sources"] = never_ready

    with pytest.raises(MarketMorningRuntimeConfigurationError) as error:
        asyncio.run(
            run_runtime_preflight_checks(
                _dependencies(preflight_checks=checks),
                role=MarketMorningRuntimeRole.WORKER,
                per_check_timeout=timedelta(milliseconds=50),
            )
        )

    assert error.value.error_code == "runtime_preflight_timeout"
    assert error.value.dependency_name == "licensed_sources"


def test_runtime_completes_database_and_dependency_preflight_before_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.market_morning.runtime as runtime

    called: list[str] = []
    checks = {
        name: _preflight_checks(callback=lambda name=name: called.append(name))[name]
        for name in (
            "licensed_sources",
            "market_snapshots",
            "event_brief_model",
            "source_reachability",
            "email_delivery",
        )
    }

    async def database_preflight(_factory) -> None:
        called.append("database")

    async def worker(**_kwargs):
        called.append("worker")
        return "worker-summary"

    monkeypatch.setattr(runtime, "preflight_runtime_database", database_preflight)
    monkeypatch.setattr(runtime, "run_worker", worker)

    result = asyncio.run(
        runtime.run_market_morning_runtime(
            role=runtime.MarketMorningRuntimeRole.WORKER,
            dependencies=_dependencies(preflight_checks=checks),
            stop_event=asyncio.Event(),
            worker_id="worker-a",
            scheduler_owner_id="scheduler-a",
        )
    )

    assert called == [
        "database",
        "licensed_sources",
        "market_snapshots",
        "event_brief_model",
        "source_reachability",
        "email_delivery",
        "worker",
    ]
    assert result.worker == "worker-summary"


def test_database_preflight_requires_current_market_morning_revision() -> None:
    from src.market_morning.runtime import (
        EXPECTED_MARKET_MORNING_SCHEMA_REVISION,
        MarketMorningRuntimeConfigurationError,
        preflight_runtime_database,
    )

    class _Result:
        def __init__(self, revision: str | None) -> None:
            self._revision = revision

        def scalar_one_or_none(self):
            return self._revision

    class _Session:
        def __init__(self, revision: str | None) -> None:
            self.revision = revision
            self.statements: list[str] = []

        async def execute(self, statement):
            self.statements.append(str(statement))
            return _Result(self.revision)

    class _Transaction:
        def __init__(self, session) -> None:
            self.session = session

        async def __aenter__(self):
            return self.session

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    class _Factory:
        def __init__(self, revision: str | None) -> None:
            self.session = _Session(revision)

        def begin(self):
            return _Transaction(self.session)

    ready = _Factory(EXPECTED_MARKET_MORNING_SCHEMA_REVISION)
    asyncio.run(preflight_runtime_database(ready))
    assert "alembic_version" in ready.session.statements[0]

    stale = _Factory("0008_market_morning_market_snapshots")
    with pytest.raises(MarketMorningRuntimeConfigurationError) as error:
        asyncio.run(preflight_runtime_database(stale))
    assert error.value.error_code == "runtime_database_schema_outdated"


def test_scheduler_loop_uses_short_transactions_and_stops_cleanly(monkeypatch) -> None:
    import src.market_morning.runtime as runtime

    lifecycle: list[str] = []
    stop_event = asyncio.Event()

    class _Transaction:
        async def __aenter__(self):
            lifecycle.append("enter")
            return object()

        async def __aexit__(self, exc_type, exc, traceback):
            lifecycle.append("commit" if exc_type is None else "rollback")

    class _Factory:
        def begin(self):
            return _Transaction()

    async def tick(session, **kwargs):
        from src.market_morning.calendar.scheduler import SchedulerTickStatus

        lifecycle.append("tick")
        stop_event.set()
        return type("TickResult", (), {"status": SchedulerTickStatus.NO_ACTION})()

    monkeypatch.setattr(runtime, "run_scheduler_tick", tick)

    summary = asyncio.run(
        runtime.run_scheduler_loop(
            owner_id="scheduler-a",
            jp_calendar=object(),
            us_calendar=object(),
            source_providers=("edinet", "tdnet"),
            stop_event=stop_event,
            session_factory=_Factory(),
            clock=lambda: datetime(2026, 7, 20, 22, 0, tzinfo=timezone.utc),
        )
    )

    assert lifecycle == ["enter", "tick", "commit"]
    assert summary.iterations == 1
    assert summary.infrastructure_failures == 0


def test_install_shutdown_signal_handlers_targets_shared_stop_event() -> None:
    from src.market_morning.runtime import install_shutdown_signal_handlers

    callbacks = {}

    class _Loop:
        def add_signal_handler(self, sig, callback):
            callbacks[sig] = callback

    stop_event = asyncio.Event()
    registered = install_shutdown_signal_handlers(stop_event, loop=_Loop())

    assert len(registered) >= 1
    callbacks[registered[0]]()
    assert stop_event.is_set()


def test_runtime_env_defaults_are_closed() -> None:
    from src.config.env_schema import EnvConfig

    config = EnvConfig().market_morning

    assert config.runtime_enabled is False
    assert config.runtime_factory == ""
    assert config.email_webhook_factory == ""
    assert config.email_identity_factory == ""
    assert config.runtime_role == "all"
    assert config.allow_fixture_runtime is False
    assert config.worker_poll_seconds == 1.0
    assert config.scheduler_poll_seconds == 30.0
