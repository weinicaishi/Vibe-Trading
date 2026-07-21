"""First-party Market Morning production runtime factory tests."""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

NOW = datetime(2026, 7, 22, 0, 0, tzinfo=timezone.utc)


class _SourceAdapter:
    def __init__(self, provider: str) -> None:
        self.provider = provider

    async def discover(self, cursor):
        raise AssertionError

    async def fetch(self, document):
        raise AssertionError

    def normalize(self, payload):
        raise AssertionError

    async def health(self):
        from src.market_morning.sources.base import SourceHealth

        return SourceHealth(ok=True, checked_at=NOW, detail_code="ready")


class _MarketAdapter:
    def __init__(self, instrument) -> None:
        from src.market_morning.market_snapshots import MarketInstrument

        self.instrument = MarketInstrument(instrument)
        self.provider = f"licensed_{self.instrument.value}"

    async def fetch(self, *, at):
        from src.market_morning.market_snapshots import MarketSnapshot

        return MarketSnapshot(
            instrument=self.instrument,
            provider=self.provider,
            session_date=date(2026, 7, 21),
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


class _SessionFactory:
    def begin(self):
        raise AssertionError


class _FalseySessionFactory(_SessionFactory):
    def __bool__(self) -> bool:
        return False


def _bundle():
    from src.market_morning.calendar import MarketCode
    from src.market_morning.market_snapshots import MarketInstrument
    from src.market_morning.production_runtime_factory import (
        ApprovedMarketMorningProviderBundle,
    )

    async def reachability_checker(url: str) -> bool:
        return True

    async def ready() -> None:
        return None

    return ApprovedMarketMorningProviderBundle(
        source_adapters={
            "licensed_edinet": _SourceAdapter("licensed_edinet"),
            "licensed_tdnet": _SourceAdapter("licensed_tdnet"),
        },
        market_data_adapters={instrument: _MarketAdapter(instrument) for instrument in MarketInstrument},
        jp_calendar=_Calendar(
            MarketCode.JPX_CASH,
            "licensed_jpx_calendar",
        ),
        us_calendar=_Calendar(
            MarketCode.US_CASH,
            "licensed_us_calendar",
        ),
        source_reachability_checker=reachability_checker,
        event_brief_model_check=ready,
        source_reachability_check=ready,
        email_delivery_check=ready,
    )


def _model_builder(environ):
    async def generate(request):
        raise AssertionError

    return generate


def _email_builder(session_factory, environ):
    from src.market_morning.email_runtime import ResendEmailRuntimeRunners

    async def delivery(command):
        raise AssertionError

    async def dispatch(command):
        raise AssertionError

    return ResendEmailRuntimeRunners(
        email_delivery_runner=delivery,
        email_dispatch_runner=dispatch,
    )


def test_production_factory_assembles_complete_strict_runtime() -> None:
    from src.market_morning.production_runtime_factory import (
        OPENROUTER_MODEL_ENV,
        assemble_production_runtime_dependencies,
    )
    from src.market_morning.repositories.jobs import MarketMorningJobType
    from src.market_morning.runtime import (
        REQUIRED_WORKER_PREFLIGHT_CHECKS,
        MarketMorningRuntimeRole,
        validate_runtime_dependencies,
    )

    dependencies = assemble_production_runtime_dependencies(
        provider_bundle=_bundle(),
        session_factory=_SessionFactory(),
        environ={OPENROUTER_MODEL_ENV: "approved/model-v1"},
        model_builder=_model_builder,
        email_builder=_email_builder,
    )

    validate_runtime_dependencies(
        dependencies,
        role=MarketMorningRuntimeRole.ALL,
    )
    assert set(dependencies.handlers) == set(MarketMorningJobType)
    assert dependencies.source_providers == (
        "licensed_edinet",
        "licensed_tdnet",
    )
    assert tuple(dependencies.preflight_checks) == (REQUIRED_WORKER_PREFLIGHT_CHECKS)
    assert repr(_bundle()) == "<ApprovedMarketMorningProviderBundle configured>"


def test_provider_bundle_loader_supports_sync_and_async_factories(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.market_morning.production_runtime_factory as factory

    bundle = _bundle()

    async def async_factory():
        return bundle

    module = SimpleNamespace(
        sync_factory=lambda: bundle,
        async_factory=async_factory,
    )
    monkeypatch.setattr(factory.importlib, "import_module", lambda name: module)

    assert asyncio.run(factory.load_approved_provider_bundle("ports:sync_factory")) is bundle
    assert asyncio.run(factory.load_approved_provider_bundle("ports:async_factory")) is bundle


@pytest.mark.parametrize(
    ("factory_path", "expected_code"),
    [
        ("", "production_provider_bundle_factory_invalid"),
        ("module", "production_provider_bundle_factory_invalid"),
        ("module:factory:extra", "production_provider_bundle_factory_invalid"),
    ],
)
def test_provider_bundle_loader_rejects_invalid_paths(
    factory_path: str,
    expected_code: str,
) -> None:
    from src.market_morning.production_runtime_factory import (
        load_approved_provider_bundle,
    )
    from src.market_morning.runtime import MarketMorningRuntimeConfigurationError

    with pytest.raises(MarketMorningRuntimeConfigurationError) as error:
        asyncio.run(load_approved_provider_bundle(factory_path))
    assert error.value.error_code == expected_code


def test_provider_bundle_loader_sanitizes_factory_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.market_morning.production_runtime_factory as factory

    def failing_factory():
        raise RuntimeError("provider secret must not escape")

    monkeypatch.setattr(
        factory.importlib,
        "import_module",
        lambda name: SimpleNamespace(build=failing_factory),
    )
    with pytest.raises(factory.MarketMorningRuntimeConfigurationError) as error:
        asyncio.run(factory.load_approved_provider_bundle("ports:build"))
    assert error.value.error_code == "production_provider_bundle_factory_failed"
    assert "secret" not in str(error.value)


def test_environment_factory_keeps_explicit_falsey_session_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.market_morning.production_runtime_factory as factory

    session_factory = _FalseySessionFactory()
    bundle = _bundle()

    async def load_bundle(factory_path: str):
        assert factory_path == "deployment.providers:build"
        return bundle

    monkeypatch.setattr(factory, "load_approved_provider_bundle", load_bundle)
    monkeypatch.setattr(
        factory,
        "get_session_factory",
        lambda: (_ for _ in ()).throw(AssertionError("must not be called")),
    )

    dependencies = asyncio.run(
        factory.build_runtime_dependencies_from_environment(
            {
                factory.PROVIDER_BUNDLE_FACTORY_ENV: ("deployment.providers:build"),
                factory.OPENROUTER_MODEL_ENV: "approved/model-v1",
            },
            session_factory=session_factory,
            model_builder=_model_builder,
            email_builder=_email_builder,
        )
    )
    assert dependencies.session_factory is session_factory


def test_environment_factory_requires_provider_bundle_before_other_ports() -> None:
    from src.market_morning.production_runtime_factory import (
        build_runtime_dependencies_from_environment,
    )
    from src.market_morning.runtime import MarketMorningRuntimeConfigurationError

    with pytest.raises(MarketMorningRuntimeConfigurationError) as error:
        asyncio.run(build_runtime_dependencies_from_environment({}))
    assert error.value.error_code == "production_provider_bundle_factory_missing"


@pytest.mark.parametrize(
    ("builder_name", "expected_code"),
    [
        ("model", "production_model_configuration_invalid"),
        ("email", "production_email_configuration_invalid"),
    ],
)
def test_production_factory_sanitizes_port_configuration_failures(
    builder_name: str,
    expected_code: str,
) -> None:
    from src.market_morning.production_runtime_factory import (
        OPENROUTER_MODEL_ENV,
        assemble_production_runtime_dependencies,
    )
    from src.market_morning.runtime import MarketMorningRuntimeConfigurationError

    def failing_builder(*args):
        raise RuntimeError("secret configuration detail")

    kwargs = {
        "provider_bundle": _bundle(),
        "session_factory": _SessionFactory(),
        "environ": {OPENROUTER_MODEL_ENV: "approved/model-v1"},
        "model_builder": _model_builder,
        "email_builder": _email_builder,
    }
    kwargs[f"{builder_name}_builder"] = failing_builder

    with pytest.raises(MarketMorningRuntimeConfigurationError) as error:
        assemble_production_runtime_dependencies(**kwargs)
    assert error.value.error_code == expected_code
    assert "secret" not in str(error.value)


def test_no_argument_factory_passes_process_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.market_morning.production_runtime_factory as factory

    captured = None
    dependencies = SimpleNamespace()

    async def build_from_environment(environ):
        nonlocal captured
        captured = environ
        return dependencies

    monkeypatch.setattr(
        factory,
        "build_runtime_dependencies_from_environment",
        build_from_environment,
    )
    assert asyncio.run(factory.build_runtime_dependencies()) is dependencies
    assert captured == factory.os.environ
    assert captured is not factory.os.environ
