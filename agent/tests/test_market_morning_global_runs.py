"""Global edition run aggregate and MySQL publication invariants."""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from collections import deque

import pytest
from sqlalchemy.dialects import mysql
from sqlalchemy.schema import CreateTable

REPO_ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 7, 20, 22, 0, tzinfo=timezone.utc)
RUN_ID = "11111111-1111-4111-8111-111111111111"


class _Result:
    def __init__(self, scalar=None):
        self.scalar = scalar

    def scalar_one_or_none(self):
        return self.scalar


class _Session:
    def __init__(self, *results):
        self.results = deque(results)
        self.statements = []
        self.added = []
        self.flush_count = 0

    async def execute(self, statement):
        self.statements.append(statement)
        return self.results.popleft() if self.results else _Result()

    def add(self, value):
        self.added.append(value)

    async def flush(self):
        self.flush_count += 1


def _spec(**overrides):
    from src.market_morning.global_runs import GlobalEditionRunSpec

    values = {
        "edition_date": date(2026, 7, 21),
        "generation_key": "global-edition-run:2026-07-21:0700",
        "attempt_key": "0700",
        "scenario": "a_standard",
        "email_permitted": True,
        "late": False,
        "reason_code": None,
        "started_at": NOW,
    }
    values.update(overrides)
    return GlobalEditionRunSpec(**values)


def test_global_run_schema_serializes_days_and_enforces_one_current_success() -> None:
    from src.market_morning.models import (
        GlobalEditionDayRecord,
        GlobalEditionItemRecord,
        GlobalEditionRunRecord,
    )

    day_ddl = str(CreateTable(GlobalEditionDayRecord.__table__).compile(dialect=mysql.dialect()))
    run_ddl = str(CreateTable(GlobalEditionRunRecord.__table__).compile(dialect=mysql.dialect()))
    item_ddl = str(CreateTable(GlobalEditionItemRecord.__table__).compile(dialect=mysql.dialect()))

    assert "PRIMARY KEY (edition_date)" in day_ddl
    assert "uq_mm_global_run_date_version" in run_ddl
    assert "uq_mm_global_run_generation_key" in run_ddl
    assert "current_success_date" in run_ddl
    assert "GENERATED ALWAYS AS" in run_ddl
    assert "uq_mm_global_run_current_success" in run_ddl
    assert "uq_mm_global_item_run_position" in item_ddl
    assert "FOREIGN KEY(snapshot_id) REFERENCES mm_market_snapshots" in item_ddl
    assert "ENGINE=InnoDB" in run_ddl and "CHARSET=utf8mb4" in run_ddl


def test_global_run_migration_follows_market_snapshots() -> None:
    migration = (
        REPO_ROOT
        / "agent/migrations/market_morning/versions/0009_market_morning_global_runs.py"
    )

    assert migration.exists()
    text = migration.read_text(encoding="utf-8")
    assert 'down_revision: str | None = "0008_market_morning_market_snapshots"' in text
    assert '"mm_global_edition_days"' in text
    assert '"mm_global_edition_runs"' in text
    assert '"mm_global_edition_items"' in text


@pytest.mark.parametrize(
    ("attempt_key", "version"),
    [("0700", 1), ("0715", 2), ("0730", 3), ("0800", 4), ("0830", 5), ("late", 6)],
)
def test_scheduled_attempts_have_deterministic_run_versions(attempt_key, version) -> None:
    from src.market_morning.global_runs import scheduled_run_version

    assert scheduled_run_version(attempt_key) == version


def test_global_run_spec_rejects_inconsistent_late_and_generation_key() -> None:
    with pytest.raises(ValueError, match="late"):
        _spec(attempt_key="late", late=False)
    with pytest.raises(ValueError, match="generation_key"):
        _spec(generation_key="other")


def test_day_and_run_inserts_are_concurrency_safe_noops() -> None:
    from src.market_morning.repositories.global_runs import (
        build_global_day_insert_statement,
        build_global_run_insert_statement,
    )

    day_sql = str(
        build_global_day_insert_statement(
            edition_date=date(2026, 7, 21),
            now=NOW,
        ).compile(dialect=mysql.dialect())
    )
    run_sql = str(
        build_global_run_insert_statement(
            run_id=RUN_ID,
            spec=_spec(),
        ).compile(dialect=mysql.dialect())
    )
    run_params = build_global_run_insert_statement(
        run_id=RUN_ID,
        spec=_spec(),
    ).compile(dialect=mysql.dialect()).params

    assert "ON DUPLICATE KEY UPDATE" in day_sql
    assert "edition_date = mm_global_edition_days.edition_date" in day_sql
    assert "ON DUPLICATE KEY UPDATE" in run_sql
    assert "draft" in run_params.values()
    update = run_sql.split("ON DUPLICATE KEY UPDATE", maxsplit=1)[1]
    assert "run_id = mm_global_edition_runs.run_id" in update
    assert "status" not in update
    assert "spec_sha256" not in update


def test_global_run_manifest_is_ordered_and_hash_stable() -> None:
    from src.market_morning.global_runs import (
        GlobalEditionSnapshotItem,
        build_global_run_manifest,
        global_run_manifest_sha256,
    )

    items = (
        GlobalEditionSnapshotItem(
            "22222222-2222-4222-8222-222222222222",
            "sp_500",
        ),
        GlobalEditionSnapshotItem(
            "33333333-3333-4333-8333-333333333333",
            "nikkei_225",
        ),
    )
    first = build_global_run_manifest(_spec(), snapshot_items=items)
    second = build_global_run_manifest(_spec(), snapshot_items=tuple(reversed(items)))

    assert first == second
    assert [item["instrument"] for item in first["market_snapshots"]] == [
        "nikkei_225",
        "sp_500",
    ]
    assert global_run_manifest_sha256(first) == global_run_manifest_sha256(second)


def _running_record(spec=None):
    from src.market_morning.global_runs import (
        encode_global_run_spec,
        global_run_spec_sha256,
    )

    spec = spec or _spec()
    return SimpleNamespace(
        run_id=RUN_ID,
        edition_date=spec.edition_date,
        run_version=1,
        generation_key=spec.generation_key,
        attempt_key=spec.attempt_key,
        scenario=spec.scenario.value,
        status="running",
        is_current=False,
        email_permitted=spec.email_permitted,
        late=spec.late,
        reason_code=spec.reason_code,
        spec=encode_global_run_spec(spec),
        spec_sha256=global_run_spec_sha256(spec),
        manifest=None,
        manifest_sha256=None,
        started_at=NOW.replace(tzinfo=None),
        completed_at=None,
        updated_at=NOW.replace(tzinfo=None),
    )


def test_start_global_run_is_idempotent_and_rejects_spec_drift(monkeypatch) -> None:
    import asyncio

    import src.market_morning.repositories.global_runs as repository

    spec = _spec()
    day = SimpleNamespace(edition_date=spec.edition_date, updated_at=NOW.replace(tzinfo=None))
    record = _running_record(spec)
    monkeypatch.setattr(repository, "new_id", lambda: RUN_ID)
    created = asyncio.run(
        repository.start_global_edition_run(
            _Session(_Result(), _Result(day), _Result(), _Result(record)),
            spec=spec,
        )
    )
    assert created.status is repository.GlobalRunStartStatus.STARTED

    record.run_id = "22222222-2222-4222-8222-222222222222"
    existing = asyncio.run(
        repository.start_global_edition_run(
            _Session(_Result(), _Result(day), _Result(), _Result(record)),
            spec=spec,
        )
    )
    assert existing.status is repository.GlobalRunStartStatus.ALREADY_STARTED

    record.spec_sha256 = "0" * 64
    with pytest.raises(repository.GlobalRunConflict, match="different spec"):
        asyncio.run(
            repository.start_global_edition_run(
                _Session(_Result(), _Result(day), _Result(), _Result(record)),
                spec=spec,
            )
        )


def test_publish_global_run_demotes_previous_current_and_writes_ordered_items(
    monkeypatch,
) -> None:
    import asyncio

    import src.market_morning.repositories.global_runs as repository
    from src.market_morning.models import GlobalEditionItemRecord

    ids = iter(
        [
            "44444444-4444-4444-8444-444444444444",
            "55555555-5555-4555-8555-555555555555",
            "66666666-6666-4666-8666-666666666666",
            "77777777-7777-4777-8777-777777777777",
            "88888888-8888-4888-8888-888888888888",
        ]
    )
    monkeypatch.setattr(repository, "new_id", lambda: next(ids))
    day = SimpleNamespace(edition_date=date(2026, 7, 21), updated_at=None)
    run = _running_record()
    previous = SimpleNamespace(run_id="previous", is_current=True, updated_at=None)
    items = (
        repository.GlobalEditionSnapshotItem(
            "22222222-2222-4222-8222-222222222222",
            "sp_500",
        ),
        repository.GlobalEditionSnapshotItem(
            "33333333-3333-4333-8333-333333333333",
            "nikkei_225",
        ),
        repository.GlobalEditionSnapshotItem(
            "99999999-9999-4999-8999-999999999999",
            "nasdaq_composite",
        ),
        repository.GlobalEditionSnapshotItem(
            "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            "djia",
        ),
        repository.GlobalEditionSnapshotItem(
            "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
            "usd_jpy",
        ),
    )
    session = _Session(_Result(day), _Result(run), _Result(previous))

    result = asyncio.run(
        repository.publish_global_edition_run(
            session,
            run_id=RUN_ID,
            spec=_spec(),
            snapshot_items=items,
            publication_status="complete",
            completed_at=NOW,
        )
    )

    assert result.status is repository.GlobalRunPublishStatus.PUBLISHED
    assert previous.is_current is False
    assert run.status == "complete" and run.is_current is True
    assert [item.instrument for item in session.added] == [
        "nikkei_225",
        "sp_500",
        "nasdaq_composite",
        "djia",
        "usd_jpy",
    ]
    assert all(isinstance(item, GlobalEditionItemRecord) for item in session.added)
    # Demotion and promotion require separate flushes to preserve the MySQL
    # generated-column uniqueness invariant throughout the transition.
    assert session.flush_count == 2


def test_late_run_cannot_be_published_as_email_eligible_complete() -> None:
    import asyncio

    import src.market_morning.repositories.global_runs as repository

    spec = _spec(
        generation_key="global-edition-run:2026-07-21:late",
        attempt_key="late",
        email_permitted=False,
        late=True,
    )
    with pytest.raises(ValueError, match="late"):
        asyncio.run(
            repository.publish_global_edition_run(
                _Session(),
                run_id=RUN_ID,
                spec=spec,
                snapshot_items=(),
                publication_status="complete",
                completed_at=NOW,
            )
        )
