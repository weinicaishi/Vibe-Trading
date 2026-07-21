"""User-scoped edition service contracts."""

from __future__ import annotations

import asyncio
import importlib.util
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest


def test_edition_service_is_isolated_in_market_morning_domain() -> None:
    try:
        spec = importlib.util.find_spec("src.market_morning.edition_service")
    except ModuleNotFoundError:
        spec = None

    assert spec is not None


def test_fixture_mode_is_fail_closed_when_not_explicitly_enabled() -> None:
    from src.market_morning.edition_service import build_today_edition

    result = build_today_edition(
        user_id="11111111-1111-4111-8111-111111111111",
        now=datetime(2026, 7, 20, 22, 0, tzinfo=timezone.utc),
        synthetic_enabled=False,
    )

    assert result.status == "not_published"
    assert result.data_mode == "unavailable"
    assert result.edition is None
    assert result.reason_code == "edition_repository_not_configured"
    assert result.edition_id is None


def test_fixture_mode_returns_a_clearly_labeled_partial_edition() -> None:
    from src.market_morning.edition_service import build_today_edition

    result = build_today_edition(
        user_id="11111111-1111-4111-8111-111111111111",
        now=datetime(2026, 7, 20, 22, 0, tzinfo=timezone.utc),
        synthetic_enabled=True,
    )

    assert result.status == "partial"
    assert result.data_mode == "synthetic_fixture"
    assert result.fixture_version == "synthetic-v1"
    assert result.edition is not None
    assert result.edition.edition_date.isoformat() == "2026-07-21"
    assert [brief.issuer_code for brief in result.edition.issuers] == [
        "7203",
        "6758",
        "9984",
    ]
    assert result.edition.issuers[0].facts[0].citations
    assert result.edition.issuers[1].status == "unavailable"
    assert not hasattr(result, "user_id")


def test_edition_service_rejects_noncanonical_user_identity() -> None:
    from src.market_morning.edition_service import build_today_edition

    with pytest.raises(ValueError, match="user_id"):
        build_today_edition(
            user_id="shared-demo-user",
            now=datetime(2026, 7, 20, 22, 0, tzinfo=timezone.utc),
            synthetic_enabled=True,
        )


class _SessionContext:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, *_args):
        return None


def test_today_service_reads_the_latest_persisted_edition_when_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.market_morning import edition_service

    now = datetime(2026, 7, 20, 22, 0, tzinfo=timezone.utc)
    persisted = edition_service.build_today_edition(
        user_id="11111111-1111-4111-8111-111111111111",
        now=now,
        synthetic_enabled=True,
    ).edition
    assert persisted is not None
    monkeypatch.setattr(
        edition_service,
        "get_env_config",
        lambda: SimpleNamespace(
            market_morning=SimpleNamespace(
                synthetic_edition_enabled=False,
                database_url="mysql+asyncmy://configured/db",
            )
        ),
    )
    monkeypatch.setattr(
        edition_service,
        "get_session_factory",
        lambda: lambda: _SessionContext(),
        raising=False,
    )

    async def _latest(session, *, user_id: str, edition_date):
        assert session is not None
        assert user_id == "11111111-1111-4111-8111-111111111111"
        assert edition_date.isoformat() == "2026-07-21"
        return SimpleNamespace(
            edition_id="22222222-2222-4222-8222-222222222222",
            edition=persisted,
        )

    monkeypatch.setattr(
        edition_service,
        "get_latest_morning_edition",
        _latest,
        raising=False,
    )

    result = asyncio.run(
        edition_service.get_today_edition(
            user_id="11111111-1111-4111-8111-111111111111",
            now=now,
        )
    )

    assert result.status == "partial"
    assert result.data_mode == "persisted"
    assert result.edition == persisted
    assert result.edition_id == "22222222-2222-4222-8222-222222222222"
    assert result.fixture_version is None


def test_today_service_does_not_fabricate_content_when_no_snapshot_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.market_morning import edition_service

    now = datetime(2026, 7, 20, 22, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(
        edition_service,
        "get_env_config",
        lambda: SimpleNamespace(
            market_morning=SimpleNamespace(
                synthetic_edition_enabled=False,
                database_url="mysql+asyncmy://configured/db",
            )
        ),
    )
    monkeypatch.setattr(
        edition_service,
        "get_session_factory",
        lambda: lambda: _SessionContext(),
        raising=False,
    )

    async def _missing(*_args, **_kwargs):
        return None

    monkeypatch.setattr(
        edition_service,
        "get_latest_morning_edition",
        _missing,
        raising=False,
    )

    async def _not_published(**_kwargs):
        raise edition_service.LazyUserEditionUnavailable(
            "global_edition_not_published"
        )

    monkeypatch.setattr(
        edition_service,
        "materialize_lazy_user_edition",
        _not_published,
        raising=False,
    )

    result = asyncio.run(
        edition_service.get_today_edition(
            user_id="11111111-1111-4111-8111-111111111111",
            now=now,
        )
    )

    assert result.status == "not_published"
    assert result.data_mode == "persisted"
    assert result.edition is None
    assert result.reason_code == "global_edition_not_published"


def test_today_service_materializes_missing_snapshot_from_current_global_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.market_morning import edition_service

    now = datetime(2026, 7, 20, 22, 0, tzinfo=timezone.utc)
    persisted = edition_service.build_today_edition(
        user_id="11111111-1111-4111-8111-111111111111",
        now=now,
        synthetic_enabled=True,
    ).edition
    monkeypatch.setattr(
        edition_service,
        "get_env_config",
        lambda: SimpleNamespace(
            market_morning=SimpleNamespace(
                synthetic_edition_enabled=False,
                database_url="mysql+asyncmy://configured/db",
            )
        ),
    )
    monkeypatch.setattr(
        edition_service,
        "get_session_factory",
        lambda: lambda: _SessionContext(),
        raising=False,
    )
    monkeypatch.setattr(
        edition_service,
        "get_latest_morning_edition",
        lambda *args, **kwargs: asyncio.sleep(0, result=None),
    )

    async def materialize(**kwargs):
        assert kwargs["user_id"] == "11111111-1111-4111-8111-111111111111"
        assert kwargs["edition_date"].isoformat() == "2026-07-21"
        assert kwargs["requested_at"] == now
        return SimpleNamespace(
            edition_id="22222222-2222-4222-8222-222222222222",
            edition=persisted,
        )

    monkeypatch.setattr(
        edition_service,
        "materialize_lazy_user_edition",
        materialize,
        raising=False,
    )

    result = asyncio.run(
        edition_service.get_today_edition(
            user_id="11111111-1111-4111-8111-111111111111",
            now=now,
        )
    )

    assert result.status == "partial"
    assert result.data_mode == "persisted"
    assert result.edition is persisted


def test_explicit_synthetic_mode_never_opens_the_persisted_repository(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.market_morning import edition_service

    monkeypatch.setattr(
        edition_service,
        "get_env_config",
        lambda: SimpleNamespace(
            market_morning=SimpleNamespace(
                synthetic_edition_enabled=True,
                database_url="mysql+asyncmy://configured/db",
            )
        ),
    )

    def _must_not_open():
        raise AssertionError("demo mode must not touch the edition repository")

    monkeypatch.setattr(
        edition_service,
        "get_session_factory",
        _must_not_open,
        raising=False,
    )

    result = asyncio.run(
        edition_service.get_today_edition(
            user_id="11111111-1111-4111-8111-111111111111",
            now=datetime(2026, 7, 20, 22, 0, tzinfo=timezone.utc),
        )
    )

    assert result.data_mode == "synthetic_fixture"
