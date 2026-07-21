"""Persistent fail-closed publication halt overrides."""

from __future__ import annotations

import asyncio
from collections import deque
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import mysql
from sqlalchemy.schema import CreateTable

REPO_ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 7, 20, 22, 0, tzinfo=timezone.utc)
EDITION_DATE = date(2026, 7, 21)
OVERRIDE_ID = "11111111-1111-4111-8111-111111111111"
AUDIT_ID = "22222222-2222-4222-8222-222222222222"


class _Result:
    def __init__(self, *, scalar=None):
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


def _record(
    *,
    override_id: str = OVERRIDE_ID,
    reason_code: str = "emergency_market_halt",
    status: str = "active",
):
    return SimpleNamespace(
        override_id=override_id,
        override_type="publication_halt",
        edition_date=EDITION_DATE,
        reason_code=reason_code,
        status=status,
        active_edition_date=EDITION_DATE if status == "active" else None,
        created_by="operator-1",
        created_at=NOW.replace(tzinfo=None),
        revoked_by=None,
        revoked_at=None,
        updated_at=NOW.replace(tzinfo=None),
    )


def test_manual_override_schema_and_0007_migration_enforce_one_active_halt_per_day() -> None:
    from src.market_morning.models import ManualOverrideRecord

    ddl = str(CreateTable(ManualOverrideRecord.__table__).compile(dialect=mysql.dialect()))
    migration = (
        REPO_ROOT
        / "agent"
        / "migrations"
        / "market_morning"
        / "versions"
        / "0007_market_morning_manual_overrides.py"
    )

    assert "active_edition_date DATE GENERATED ALWAYS AS" in ddl
    assert "uq_mm_manual_override_active_date" in ddl
    assert "ENGINE=InnoDB" in ddl and "CHARSET=utf8mb4" in ddl
    assert migration.exists()
    text = migration.read_text(encoding="utf-8")
    assert 'down_revision: str | None = "0006_market_morning_jobs"' in text
    assert '"mm_manual_overrides"' in text


def test_override_insert_uses_unique_generated_date_as_concurrency_backstop() -> None:
    from src.market_morning.repositories.manual_overrides import (
        build_publication_halt_insert_statement,
    )

    sql = str(
        build_publication_halt_insert_statement(
            override_id=OVERRIDE_ID,
            edition_date=EDITION_DATE,
            reason_code="emergency_market_halt",
            actor_reference="operator-1",
            created_at=NOW,
        ).compile(dialect=mysql.dialect())
    )

    assert "INSERT INTO mm_manual_overrides" in sql
    assert "ON DUPLICATE KEY UPDATE" in sql
    update_clause = sql.split("ON DUPLICATE KEY UPDATE", maxsplit=1)[1]
    assert "reason_code" not in update_clause
    assert "status" not in update_clause


def test_create_publication_halt_is_idempotent_and_appends_audit(monkeypatch) -> None:
    import src.market_morning.repositories.manual_overrides as overrides
    from src.market_morning.models import AuditLog

    ids = iter((OVERRIDE_ID, AUDIT_ID))
    monkeypatch.setattr(overrides, "new_id", lambda: next(ids))
    session = _Session(_Result(), _Result(scalar=_record()))

    result = asyncio.run(
        overrides.create_publication_halt(
            session,
            edition_date=EDITION_DATE,
            reason_code="emergency_market_halt",
            actor_reference="operator-1",
            now=NOW,
        )
    )

    assert result.status is overrides.OverrideMutationStatus.CREATED
    assert result.override.override_id == OVERRIDE_ID
    assert isinstance(session.added[0], AuditLog)
    assert session.added[0].action == "publication_halt.created"
    assert session.added[0].details == {
        "actor_reference": "operator-1",
        "edition_date": "2026-07-21",
        "reason_code": "emergency_market_halt",
    }


def test_repeated_same_halt_returns_existing_without_duplicate_audit(monkeypatch) -> None:
    import src.market_morning.repositories.manual_overrides as overrides

    monkeypatch.setattr(
        overrides,
        "new_id",
        lambda: "33333333-3333-4333-8333-333333333333",
    )
    session = _Session(_Result(), _Result(scalar=_record()))

    result = asyncio.run(
        overrides.create_publication_halt(
            session,
            edition_date=EDITION_DATE,
            reason_code="emergency_market_halt",
            actor_reference="operator-1",
            now=NOW,
        )
    )

    assert result.status is overrides.OverrideMutationStatus.ALREADY_ACTIVE
    assert session.added == []


def test_active_halt_reason_cannot_be_silently_overwritten(monkeypatch) -> None:
    import src.market_morning.repositories.manual_overrides as overrides

    monkeypatch.setattr(
        overrides,
        "new_id",
        lambda: "33333333-3333-4333-8333-333333333333",
    )
    session = _Session(_Result(), _Result(scalar=_record(reason_code="operator_halt")))

    with pytest.raises(overrides.PublicationOverrideConflict, match="different reason"):
        asyncio.run(
            overrides.create_publication_halt(
                session,
                edition_date=EDITION_DATE,
                reason_code="emergency_market_halt",
                actor_reference="operator-1",
                now=NOW,
            )
        )
    assert session.added == []


def test_load_active_halt_returns_calendar_domain_override() -> None:
    from src.market_morning.repositories.manual_overrides import (
        load_publication_halt,
    )

    result = asyncio.run(
        load_publication_halt(
            _Session(_Result(scalar=_record())),
            edition_date=EDITION_DATE,
        )
    )

    assert result is not None
    assert result.override_id == OVERRIDE_ID
    assert result.edition_date == EDITION_DATE
    assert result.reason_code == "emergency_market_halt"


def test_revoke_publication_halt_is_locked_and_audited(monkeypatch) -> None:
    import src.market_morning.repositories.manual_overrides as overrides
    from src.market_morning.models import AuditLog

    monkeypatch.setattr(overrides, "new_id", lambda: AUDIT_ID)
    record = _record()
    session = _Session(_Result(scalar=record))

    result = asyncio.run(
        overrides.revoke_publication_halt(
            session,
            edition_date=EDITION_DATE,
            actor_reference="operator-2",
            now=NOW,
        )
    )

    assert result.status is overrides.OverrideMutationStatus.REVOKED
    assert record.status == "revoked"
    assert record.revoked_by == "operator-2"
    assert record.revoked_at == NOW.replace(tzinfo=None)
    assert isinstance(session.added[0], AuditLog)
    assert session.added[0].action == "publication_halt.revoked"
    sql = str(session.statements[0].compile(dialect=mysql.dialect()))
    assert "FOR UPDATE" in sql
