"""Market Morning publication-grade market snapshot contracts."""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import mysql
from sqlalchemy.schema import CreateTable

REPO_ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 7, 20, 22, 0, tzinfo=timezone.utc)
SNAPSHOT_ID = "11111111-1111-4111-8111-111111111111"


def _snapshot(instrument="nikkei_225", **overrides):
    from src.market_morning.market_snapshots import MarketSnapshot

    values = {
        "instrument": instrument,
        "provider": "fixture_market_data",
        "session_date": date(2026, 7, 20),
        "as_of": NOW - timedelta(minutes=5),
        "value": Decimal("39819.11"),
        "previous_close": Decimal("39780.00"),
        "currency": "JPY",
        "delay_status": "eod",
        "fetched_at": NOW,
    }
    values.update(overrides)
    return MarketSnapshot(**values)


def _required_snapshots():
    from src.market_morning.market_snapshots import MarketInstrument

    return tuple(
        _snapshot(
            instrument,
            value=Decimal("155.42") if instrument is MarketInstrument.USD_JPY else Decimal("100.00"),
            currency=(
                "JPY"
                if instrument in {MarketInstrument.NIKKEI_225, MarketInstrument.USD_JPY}
                else "USD"
            ),
            session_date=(
                date(2026, 7, 21)
                if instrument is MarketInstrument.USD_JPY
                else date(2026, 7, 20)
            ),
            delay_status="delayed" if instrument is MarketInstrument.USD_JPY else "eod",
        )
        for instrument in MarketInstrument
    )


class _Result:
    def __init__(self, scalar=None):
        self.scalar = scalar

    def scalar_one_or_none(self):
        return self.scalar


class _Scalars:
    def __init__(self, rows):
        self.rows = rows

    def all(self):
        return self.rows


class _RowsResult(_Result):
    def __init__(self, rows):
        super().__init__()
        self.rows = rows

    def scalars(self):
        return _Scalars(self.rows)


class _Session:
    def __init__(self, *results):
        self.results = list(results)
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)
        return self.results.pop(0) if self.results else _Result()


def test_market_snapshot_schema_is_immutable_idempotent_and_mysql_safe() -> None:
    from src.market_morning.models import MarketSnapshotRecord

    ddl = str(CreateTable(MarketSnapshotRecord.__table__).compile(dialect=mysql.dialect()))

    assert "mm_market_snapshots" in ddl
    assert "NUMERIC(24, 8) NOT NULL" in ddl
    assert "snapshot_sha256 VARCHAR(64) NOT NULL" in ddl
    assert "uq_mm_market_snapshot_identity" in ddl
    assert "ENGINE=InnoDB" in ddl and "CHARSET=utf8mb4" in ddl


def test_market_snapshot_migration_follows_manual_overrides() -> None:
    migration = (
        REPO_ROOT
        / "agent"
        / "migrations"
        / "market_morning"
        / "versions"
        / "0008_market_morning_market_snapshots.py"
    )

    assert migration.exists()
    text = migration.read_text(encoding="utf-8")
    assert 'down_revision: str | None = "0007_market_morning_manual_overrides"' in text
    assert '"mm_market_snapshots"' in text


def test_snapshot_rejects_unknown_delay_nonpositive_value_and_naive_time() -> None:
    with pytest.raises(ValueError, match="delay_status"):
        _snapshot(delay_status="unknown")
    with pytest.raises(ValueError, match="positive"):
        _snapshot(value=Decimal("0"))
    with pytest.raises(ValueError, match="timezone-aware"):
        _snapshot(as_of=NOW.replace(tzinfo=None))


def test_fixture_adapter_cannot_be_mistaken_for_production() -> None:
    from src.market_morning.market_snapshots import FixtureMarketDataAdapter

    snapshot = _snapshot()
    with pytest.raises(ValueError, match="fixture_"):
        FixtureMarketDataAdapter(provider="licensed_market_data", snapshot=snapshot)
    with pytest.raises(ValueError, match="provider"):
        FixtureMarketDataAdapter(provider="fixture_other", snapshot=snapshot)

    adapter = FixtureMarketDataAdapter(
        provider="fixture_market_data",
        snapshot=snapshot,
    )
    assert asyncio.run(adapter.fetch(at=NOW)) is snapshot


def test_publication_gate_requires_all_five_expected_sessions() -> None:
    from src.market_morning.market_snapshots import (
        MarketInstrument,
        SnapshotGateStatus,
        evaluate_snapshot_gate,
    )

    expected = {
        instrument: (
            date(2026, 7, 21)
            if instrument is MarketInstrument.USD_JPY
            else date(2026, 7, 20)
        )
        for instrument in MarketInstrument
    }
    complete = evaluate_snapshot_gate(_required_snapshots(), expected_sessions=expected)
    missing = evaluate_snapshot_gate(
        _required_snapshots()[:-1],
        expected_sessions=expected,
    )
    wrong_session = evaluate_snapshot_gate(
        (
            *_required_snapshots()[:-1],
            _snapshot(
                "usd_jpy",
                session_date=date(2026, 7, 20),
                currency="JPY",
            ),
        ),
        expected_sessions=expected,
    )

    assert complete.status is SnapshotGateStatus.READY
    assert complete.snapshots_by_instrument[MarketInstrument.USD_JPY].session_date == date(2026, 7, 21)
    assert missing.status is SnapshotGateStatus.BLOCKED
    assert missing.missing_instruments == (MarketInstrument.USD_JPY,)
    assert wrong_session.status is SnapshotGateStatus.BLOCKED
    assert wrong_session.session_mismatches == (MarketInstrument.USD_JPY,)


def test_duplicate_instrument_is_ambiguous_and_fails_closed() -> None:
    from src.market_morning.market_snapshots import (
        MarketInstrument,
        SnapshotGateStatus,
        evaluate_snapshot_gate,
    )

    snapshots = _required_snapshots()
    expected = {
        item.instrument: item.session_date
        for item in snapshots
    }
    result = evaluate_snapshot_gate(
        (*snapshots, _snapshot(MarketInstrument.NIKKEI_225)),
        expected_sessions=expected,
    )

    assert result.status is SnapshotGateStatus.BLOCKED
    assert result.duplicate_instruments == (MarketInstrument.NIKKEI_225,)


def test_market_snapshot_insert_is_noop_on_duplicate_identity() -> None:
    from src.market_morning.repositories.market_snapshots import (
        build_market_snapshot_insert_statement,
    )

    sql = str(
        build_market_snapshot_insert_statement(
            snapshot_id=SNAPSHOT_ID,
            snapshot=_snapshot(),
            created_at=NOW,
        ).compile(dialect=mysql.dialect())
    )

    assert "INSERT INTO mm_market_snapshots" in sql
    assert "ON DUPLICATE KEY UPDATE" in sql
    update = sql.split("ON DUPLICATE KEY UPDATE", maxsplit=1)[1]
    assert "snapshot_id = mm_market_snapshots.snapshot_id" in update
    assert "value" not in update
    assert "snapshot_sha256" not in update


def test_persist_returns_existing_identity_and_rejects_payload_drift(monkeypatch) -> None:
    import src.market_morning.repositories.market_snapshots as repository

    snapshot = _snapshot()
    monkeypatch.setattr(repository, "new_id", lambda: SNAPSHOT_ID)
    record = SimpleNamespace(
        snapshot_id=SNAPSHOT_ID,
        snapshot_sha256=repository.market_snapshot_sha256(snapshot),
    )
    inserted = asyncio.run(
        repository.persist_market_snapshot(
            _Session(_Result(), _Result(record)),
            snapshot=snapshot,
            created_at=NOW,
        )
    )
    assert inserted.status is repository.MarketSnapshotWriteStatus.INSERTED

    existing = SimpleNamespace(snapshot_id="existing-id", snapshot_sha256="0" * 64)
    with pytest.raises(repository.MarketSnapshotConflict, match="different payload"):
        asyncio.run(
            repository.persist_market_snapshot(
                _Session(_Result(), _Result(existing)),
                snapshot=snapshot,
                created_at=NOW,
            )
        )


def test_load_expected_snapshots_selects_latest_configured_provider_per_instrument() -> None:
    import src.market_morning.repositories.market_snapshots as repository
    from src.market_morning.market_snapshots import MarketInstrument

    expected = {
        instrument: (
            date(2026, 7, 21)
            if instrument is MarketInstrument.USD_JPY
            else date(2026, 7, 20)
        )
        for instrument in MarketInstrument
    }
    providers = {
        instrument: f"fixture_{instrument.value}" for instrument in MarketInstrument
    }

    def record(instrument, suffix, *, minutes=0):
        return SimpleNamespace(
            snapshot_id=f"00000000-0000-4000-8000-{suffix:012d}",
            instrument=instrument.value,
            provider=providers[instrument],
            session_date=expected[instrument],
            as_of=(NOW - timedelta(minutes=minutes)).replace(tzinfo=None),
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
            fetched_at=NOW.replace(tzinfo=None),
        )

    rows = [
        record(MarketInstrument.NIKKEI_225, 1),
        record(MarketInstrument.NIKKEI_225, 2, minutes=30),
        *[
            record(instrument, index + 10)
            for index, instrument in enumerate(tuple(MarketInstrument)[1:])
        ],
    ]
    session = _Session(_RowsResult(rows))

    loaded = asyncio.run(
        repository.load_expected_market_snapshots(
            session,
            expected_sessions=expected,
            provider_by_instrument=providers,
        )
    )

    assert len(loaded) == 5
    assert loaded[0].snapshot_id.endswith("000000000001")
    assert loaded[-1].snapshot.instrument is MarketInstrument.USD_JPY
