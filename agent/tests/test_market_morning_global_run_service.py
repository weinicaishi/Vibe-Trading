"""End-to-end application service for one global edition run."""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from src.market_morning.market_snapshots import MarketInstrument, MarketSnapshot

NOW = datetime(2026, 7, 20, 22, 0, tzinfo=timezone.utc)
RUN_ID = "11111111-1111-4111-8111-111111111111"


class _Transaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return None


class _Factory:
    def begin(self):
        return _Transaction()


def _command():
    from src.market_morning.global_run_service import GlobalEditionRunCommand
    from src.market_morning.global_runs import GlobalEditionRunSpec

    expected = {
        instrument: (
            date(2026, 7, 21)
            if instrument is MarketInstrument.USD_JPY
            else date(2026, 7, 20)
        )
        for instrument in MarketInstrument
    }
    return GlobalEditionRunCommand(
        spec=GlobalEditionRunSpec(
            edition_date=date(2026, 7, 21),
            generation_key="global-edition-run:2026-07-21:0700",
            attempt_key="0700",
            scenario="a_standard",
            email_permitted=True,
            late=False,
            reason_code=None,
            started_at=NOW,
        ),
        expected_market_sessions=expected,
        day_plan={"status": "scheduled"},
    )


def _persisted_snapshots(*, omit: MarketInstrument | None = None):
    from src.market_morning.repositories.market_snapshots import PersistedMarketSnapshot

    rows = []
    for index, instrument in enumerate(MarketInstrument, start=1):
        if instrument is omit:
            continue
        session_date = (
            date(2026, 7, 21)
            if instrument is MarketInstrument.USD_JPY
            else date(2026, 7, 20)
        )
        rows.append(
            PersistedMarketSnapshot(
                snapshot_id=f"00000000-0000-4000-8000-{index:012d}",
                snapshot=MarketSnapshot(
                    instrument=instrument,
                    provider=f"fixture_{instrument.value}",
                    session_date=session_date,
                    as_of=NOW - timedelta(minutes=5),
                    value=Decimal("100.00"),
                    previous_close=Decimal("99.00"),
                    currency=(
                        "JPY"
                        if instrument in {MarketInstrument.NIKKEI_225, MarketInstrument.USD_JPY}
                        else "USD"
                    ),
                    delay_status=(
                        "delayed" if instrument is MarketInstrument.USD_JPY else "eod"
                    ),
                    fetched_at=NOW,
                ),
            )
        )
    return tuple(rows)


def _providers():
    return {
        instrument: f"fixture_{instrument.value}" for instrument in MarketInstrument
    }


def test_global_run_service_publishes_only_after_snapshot_gate(monkeypatch) -> None:
    import src.market_morning.global_run_service as service
    from src.market_morning.repositories.global_runs import (
        GlobalRunPublishResult,
        GlobalRunPublishStatus,
        GlobalRunStartResult,
        GlobalRunStartStatus,
        GlobalRunTerminalStatus,
    )

    lifecycle = []

    async def start(session, *, spec):
        lifecycle.append("start")
        return GlobalRunStartResult(GlobalRunStartStatus.STARTED, RUN_ID, 1)

    async def load(session, **kwargs):
        lifecycle.append("load")
        return _persisted_snapshots()

    async def publish(session, **kwargs):
        lifecycle.append(("publish", kwargs["snapshot_items"]))
        return GlobalRunPublishResult(
            GlobalRunPublishStatus.PUBLISHED,
            RUN_ID,
            1,
            GlobalRunTerminalStatus.COMPLETE,
        )

    monkeypatch.setattr(service, "start_global_edition_run", start)
    monkeypatch.setattr(service, "load_expected_market_snapshots", load)
    monkeypatch.setattr(service, "publish_global_edition_run", publish)

    result = asyncio.run(
        service.run_global_edition_generation(
            _command(),
            provider_by_instrument=_providers(),
            session_factory=_Factory(),
            clock=lambda: NOW,
        )
    )

    assert lifecycle[:2] == ["start", "load"]
    assert lifecycle[2][0] == "publish" and len(lifecycle[2][1]) == 5
    assert result.publication_status == "complete"
    assert result.email_permitted is True


def test_global_run_service_records_failed_gate_without_publishing(monkeypatch) -> None:
    import src.market_morning.global_run_service as service
    from src.market_morning.repositories.global_runs import (
        GlobalRunStartResult,
        GlobalRunStartStatus,
    )

    failures = []

    async def start(session, *, spec):
        return GlobalRunStartResult(GlobalRunStartStatus.STARTED, RUN_ID, 1)

    async def load(session, **kwargs):
        return _persisted_snapshots(omit=MarketInstrument.DJIA)

    async def fail(session, **kwargs):
        failures.append(kwargs)

    async def must_not_publish(*args, **kwargs):
        raise AssertionError("blocked snapshot gate must not publish")

    monkeypatch.setattr(service, "start_global_edition_run", start)
    monkeypatch.setattr(service, "load_expected_market_snapshots", load)
    monkeypatch.setattr(service, "fail_global_edition_run", fail)
    monkeypatch.setattr(service, "publish_global_edition_run", must_not_publish)

    result = asyncio.run(
        service.run_global_edition_generation(
            _command(),
            provider_by_instrument=_providers(),
            session_factory=_Factory(),
            clock=lambda: NOW,
        )
    )

    assert result.publication_status == "failed"
    assert result.email_permitted is False
    assert failures[0]["reason_code"] == "market_snapshot_gate_failed"
