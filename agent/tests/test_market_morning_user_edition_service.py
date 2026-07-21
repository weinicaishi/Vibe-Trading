"""Lazy per-user edition materialization from the current global run."""

from __future__ import annotations

import asyncio
from collections import deque
from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest

NOW = datetime(2026, 7, 21, 0, 15, tzinfo=timezone.utc)
RUN_COMPLETED_AT = datetime(2026, 7, 20, 22, 0, tzinfo=timezone.utc)
USER_ID = "11111111-1111-4111-8111-111111111111"
RUN_ID = "22222222-2222-4222-8222-222222222222"


class _Transaction:
    def __init__(self, session, entries):
        self.session = session
        self.entries = entries

    async def __aenter__(self):
        self.entries.append(("enter", self.session))
        return self.session

    async def __aexit__(self, exc_type, exc, traceback):
        self.entries.append(("rollback" if exc_type else "commit", self.session))


class _Factory:
    def __init__(self, *sessions):
        self.sessions = deque(sessions)
        self.entries = []

    def begin(self):
        return _Transaction(self.sessions.popleft(), self.entries)


def _context():
    from src.market_morning.pipeline.morning_edition import (
        EditionDayInput,
        build_edition_day_plan,
    )
    from src.market_morning.user_edition_service import LazyUserEditionContext

    return LazyUserEditionContext(
        user_id=USER_ID,
        global_run_id=RUN_ID,
        edition_date=date(2026, 7, 21),
        day_plan=build_edition_day_plan(
            EditionDayInput(
                edition_date=date(2026, 7, 21),
                jp_market_open=True,
                us_previous_session_available=True,
            )
        ),
        global_run_completed_at=RUN_COMPLETED_AT,
    )


def test_lazy_user_edition_uses_global_cutoff_and_current_watchlist(monkeypatch) -> None:
    import src.market_morning.user_edition_service as service

    session = object()
    factory = _Factory(session)
    calls = []
    published = SimpleNamespace(edition_id="edition-1")

    async def load(active_session, **kwargs):
        calls.append(("load", active_session, kwargs))
        return _context()

    async def latest(active_session, **kwargs):
        calls.append(("latest", active_session, kwargs))
        return None

    async def generate(active_session, *, command):
        calls.append(("generate", active_session, command))
        return published

    monkeypatch.setattr(service, "load_lazy_user_edition_context", load)
    monkeypatch.setattr(service, "get_latest_morning_edition", latest)
    monkeypatch.setattr(service, "generate_and_publish_morning_edition", generate)

    result = asyncio.run(
        service.materialize_lazy_user_edition(
            user_id=USER_ID,
            edition_date=date(2026, 7, 21),
            requested_at=NOW,
            session_factory=factory,
        )
    )

    assert result is published
    command = calls[2][2]
    assert command.generation_key == f"user-edition:2026-07-21:{USER_ID}"
    assert command.generated_at == RUN_COMPLETED_AT
    assert command.published_at == NOW
    assert command.window.ends_at == RUN_COMPLETED_AT
    assert command.user_id == USER_ID
    assert factory.entries == [("enter", session), ("commit", session)]


def test_lazy_user_edition_returns_existing_daily_snapshot_without_regeneration(
    monkeypatch,
) -> None:
    import src.market_morning.user_edition_service as service

    existing = SimpleNamespace(edition_id="edition-existing")
    factory = _Factory(object())
    monkeypatch.setattr(
        service,
        "load_lazy_user_edition_context",
        lambda *args, **kwargs: asyncio.sleep(0, result=_context()),
    )
    monkeypatch.setattr(
        service,
        "get_latest_morning_edition",
        lambda *args, **kwargs: asyncio.sleep(0, result=existing),
    )
    monkeypatch.setattr(
        service,
        "generate_and_publish_morning_edition",
        lambda *args, **kwargs: pytest.fail("existing edition must be reused"),
    )

    result = asyncio.run(
        service.materialize_lazy_user_edition(
            user_id=USER_ID,
            edition_date=date(2026, 7, 21),
            requested_at=NOW,
            session_factory=factory,
        )
    )

    assert result is existing


def test_lazy_context_requires_a_valid_current_global_publication() -> None:
    import src.market_morning.user_edition_service as service

    class _Result:
        def scalar_one_or_none(self):
            return None

    class _Session:
        async def execute(self, statement):
            return _Result()

    with pytest.raises(service.LazyUserEditionUnavailable) as error:
        asyncio.run(
            service.load_lazy_user_edition_context(
                _Session(),
                user_id=USER_ID,
                edition_date=date(2026, 7, 21),
            )
        )

    assert error.value.error_code == "global_edition_not_published"
