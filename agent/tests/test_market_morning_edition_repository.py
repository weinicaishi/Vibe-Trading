"""Persisted morning-edition read-model contracts without a live MySQL server."""

from __future__ import annotations

import asyncio
import importlib.util
from collections import deque
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from sqlalchemy.dialects import mysql
from sqlalchemy.schema import CreateTable

from src.market_morning.models import Base, MorningEditionRecord
from src.market_morning.pipeline.morning_edition import (
    EditionDayPlan,
    EditionDayStatus,
    EditionSourceCitation,
    EditionStatus,
    FactStatement,
    GenerationBudget,
    InferenceStatement,
    IssuerBrief,
    IssuerBriefStatus,
    MorningEdition,
    OvernightMarketContext,
    StatementKind,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
USER_ID = "11111111-1111-4111-8111-111111111111"
EDITION_DATE = date(2026, 7, 21)
GENERATED_AT = datetime(2026, 7, 20, 22, 0, tzinfo=timezone.utc)
PUBLISHED_AT = datetime(2026, 7, 20, 22, 5, tzinfo=timezone.utc)


def _edition() -> MorningEdition:
    citation = EditionSourceCitation(
        provider="tdnet",
        document_id="TD-7203-001",
        revision_key="v2",
        original_url="https://example.jp/tdnet/TD-7203-001-v2.pdf",
        published_at=datetime(2026, 7, 20, 6, 30, tzinfo=timezone.utc),
    )
    return MorningEdition(
        edition_date=EDITION_DATE,
        generated_at=GENERATED_AT,
        status=EditionStatus.READY,
        day_plan=EditionDayPlan(
            edition_date=EDITION_DATE,
            generate=True,
            status=EditionDayStatus.SCHEDULED,
            overnight_context=OvernightMarketContext.US_SESSION_AVAILABLE,
            reason_code=None,
            us_reason_code=None,
        ),
        issuers=(
            IssuerBrief(
                issuer_id="22222222-2222-4222-8222-222222222222",
                issuer_code="7203",
                legal_name_ja="トヨタ自動車株式会社",
                status=IssuerBriefStatus.READY,
                facts=(
                    FactStatement(
                        kind=StatementKind.FACT,
                        text="（訂正）2026年3月期 決算短信",
                        event_id="33333333-3333-4333-8333-333333333333",
                        event_family_key="tdnet:TD-7203-001",
                        event_version=2,
                        event_type="earnings_release",
                        occurred_at=datetime(
                            2026, 7, 20, 6, 30, tzinfo=timezone.utc
                        ),
                        lifecycle_status="corrected",
                        citations=(citation,),
                    ),
                ),
                assessment=InferenceStatement(),
                omitted_event_count=0,
                warnings=(),
            ),
        ),
        consumed_event_units=1,
        budget=GenerationBudget(),
        budget_exhausted=False,
        omitted_issuer_count=0,
    )


class _Result:
    def __init__(self, scalar=None):
        self.scalar = scalar

    def scalar_one_or_none(self):
        return self.scalar


class _Session:
    def __init__(self, *results: _Result):
        self.results = deque(results)
        self.statements = []
        self.added = []
        self.flush_count = 0

    async def execute(self, statement):
        self.statements.append(statement)
        return self.results.popleft()

    def add(self, value):
        self.added.append(value)

    async def flush(self):
        self.flush_count += 1


def test_edition_snapshot_schema_keeps_user_daily_revision_history() -> None:
    table = Base.metadata.tables["mm_morning_editions"]
    ddl = str(CreateTable(table).compile(dialect=mysql.dialect()))

    assert "payload JSON NOT NULL" in ddl
    assert "payload_sha256 VARCHAR(64) NOT NULL" in ddl
    assert "supersedes_edition_id" in ddl
    assert "uq_mm_edition_user_date_version" in ddl
    assert "uq_mm_edition_user_generation_key" in ddl
    assert "ix_mm_edition_user_date_version" in {index.name for index in table.indexes}
    assert "FOREIGN KEY(user_id) REFERENCES mm_users" in ddl


def test_edition_snapshot_has_a_versioned_migration_after_source_events() -> None:
    migration = (
        REPO_ROOT
        / "agent"
        / "migrations"
        / "market_morning"
        / "versions"
        / "0005_market_morning_editions.py"
    )

    assert migration.exists()
    text = migration.read_text(encoding="utf-8")
    assert 'down_revision: str | None = "0004_market_morning_sources_events"' in text
    assert '"mm_morning_editions"' in text
    assert '"uq_mm_edition_user_date_version"' in text
    assert '"uq_mm_edition_user_generation_key"' in text


def test_edition_repository_is_isolated_in_market_morning_domain() -> None:
    try:
        spec = importlib.util.find_spec(
            "src.market_morning.repositories.morning_edition"
        )
    except ModuleNotFoundError:
        spec = None

    assert spec is not None


def test_edition_payload_round_trip_preserves_domain_types_and_citations() -> None:
    from src.market_morning.repositories.morning_edition import (
        decode_morning_edition,
        encode_morning_edition,
    )

    edition = _edition()
    payload = encode_morning_edition(edition)
    restored = decode_morning_edition(payload)

    assert restored == edition
    assert payload["issuers"][0]["facts"][0]["citations"][0]["provider"] == "tdnet"
    assert payload["generated_at"] == "2026-07-20T22:00:00+00:00"


def test_latest_edition_query_is_user_and_trading_day_scoped() -> None:
    from src.market_morning.repositories.morning_edition import (
        build_latest_edition_statement,
    )

    statement = build_latest_edition_statement(USER_ID, EDITION_DATE)
    sql = str(
        statement.compile(
            dialect=mysql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )

    assert f"user_id = '{USER_ID}'" in sql
    assert "edition_date = '2026-07-21'" in sql
    assert "edition_version DESC" in sql
    assert "LIMIT 1" in sql


def test_latest_edition_loads_and_verifies_the_persisted_snapshot() -> None:
    from src.market_morning.repositories.morning_edition import (
        encode_morning_edition,
        get_latest_morning_edition,
        morning_edition_payload_sha256,
    )

    payload = encode_morning_edition(_edition())
    record = MorningEditionRecord(
        edition_id="44444444-4444-4444-8444-444444444444",
        user_id=USER_ID,
        edition_date=EDITION_DATE,
        edition_version=2,
        generation_key="2026-07-21:run-002",
        schema_version=1,
        status="ready",
        payload=payload,
        payload_sha256=morning_edition_payload_sha256(payload),
        generated_at=GENERATED_AT.replace(tzinfo=None),
        published_at=PUBLISHED_AT.replace(tzinfo=None),
        supersedes_edition_id="33333333-3333-4333-8333-333333333333",
        created_at=PUBLISHED_AT.replace(tzinfo=None),
    )
    session = _Session(_Result(record))

    result = asyncio.run(
        get_latest_morning_edition(
            session,
            user_id=USER_ID,
            edition_date=EDITION_DATE,
        )
    )

    assert result is not None
    assert result.edition_id == record.edition_id
    assert result.edition_version == 2
    assert result.edition == _edition()
    assert result.published_at == PUBLISHED_AT


def test_publish_serializes_on_user_and_creates_first_immutable_version() -> None:
    from src.market_morning.repositories.morning_edition import (
        EditionPublishStatus,
        publish_morning_edition,
    )

    active_user = SimpleNamespace(
        user_id=USER_ID,
        account_status="active",
        deleted_at=None,
    )
    session = _Session(
        _Result(active_user),
        _Result(None),
        _Result(None),
    )

    result = asyncio.run(
        publish_morning_edition(
            session,
            user_id=USER_ID,
            generation_key="2026-07-21:run-001",
            edition=_edition(),
            published_at=PUBLISHED_AT,
        )
    )

    assert result.status == EditionPublishStatus.PUBLISHED
    assert result.edition_version == 1
    assert result.edition == _edition()
    assert session.flush_count == 1
    assert len(session.added) == 1
    record = session.added[0]
    assert isinstance(record, MorningEditionRecord)
    assert record.supersedes_edition_id is None
    assert record.payload_sha256
    first_sql = str(session.statements[0].compile(dialect=mysql.dialect()))
    assert "FOR UPDATE" in first_sql


def test_publish_is_idempotent_for_the_same_generation_and_payload() -> None:
    from src.market_morning.repositories.morning_edition import (
        EditionPublishStatus,
        encode_morning_edition,
        morning_edition_payload_sha256,
        publish_morning_edition,
    )

    payload = encode_morning_edition(_edition())
    existing = MorningEditionRecord(
        edition_id="44444444-4444-4444-8444-444444444444",
        user_id=USER_ID,
        edition_date=EDITION_DATE,
        edition_version=1,
        generation_key="2026-07-21:run-001",
        schema_version=1,
        status="ready",
        payload=payload,
        payload_sha256=morning_edition_payload_sha256(payload),
        generated_at=GENERATED_AT.replace(tzinfo=None),
        published_at=PUBLISHED_AT.replace(tzinfo=None),
        supersedes_edition_id=None,
        created_at=PUBLISHED_AT.replace(tzinfo=None),
    )
    active_user = SimpleNamespace(
        user_id=USER_ID,
        account_status="active",
        deleted_at=None,
    )
    session = _Session(_Result(active_user), _Result(existing))

    result = asyncio.run(
        publish_morning_edition(
            session,
            user_id=USER_ID,
            generation_key="2026-07-21:run-001",
            edition=_edition(),
            published_at=PUBLISHED_AT,
        )
    )

    assert result.status == EditionPublishStatus.ALREADY_PUBLISHED
    assert result.edition_id == existing.edition_id
    assert session.added == []
    assert session.flush_count == 0


def test_publish_creates_a_new_version_that_supersedes_the_latest_snapshot() -> None:
    from src.market_morning.repositories.morning_edition import (
        encode_morning_edition,
        morning_edition_payload_sha256,
        publish_morning_edition,
    )

    payload = encode_morning_edition(_edition())
    latest = MorningEditionRecord(
        edition_id="44444444-4444-4444-8444-444444444444",
        user_id=USER_ID,
        edition_date=EDITION_DATE,
        edition_version=1,
        generation_key="2026-07-21:run-001",
        schema_version=1,
        status="ready",
        payload=payload,
        payload_sha256=morning_edition_payload_sha256(payload),
        generated_at=GENERATED_AT.replace(tzinfo=None),
        published_at=PUBLISHED_AT.replace(tzinfo=None),
        supersedes_edition_id=None,
        created_at=PUBLISHED_AT.replace(tzinfo=None),
    )
    active_user = SimpleNamespace(
        user_id=USER_ID,
        account_status="active",
        deleted_at=None,
    )
    session = _Session(
        _Result(active_user),
        _Result(None),
        _Result(latest),
    )

    result = asyncio.run(
        publish_morning_edition(
            session,
            user_id=USER_ID,
            generation_key="2026-07-21:run-002",
            edition=_edition(),
            published_at=PUBLISHED_AT,
        )
    )

    assert result.edition_version == 2
    assert session.added[0].supersedes_edition_id == latest.edition_id


def test_generation_key_reuse_with_different_payload_fails_closed() -> None:
    from src.market_morning.repositories.morning_edition import (
        EditionGenerationConflict,
        encode_morning_edition,
        publish_morning_edition,
    )

    existing = MorningEditionRecord(
        edition_id="44444444-4444-4444-8444-444444444444",
        user_id=USER_ID,
        edition_date=EDITION_DATE,
        edition_version=1,
        generation_key="2026-07-21:run-001",
        schema_version=1,
        status="ready",
        payload=encode_morning_edition(_edition()),
        payload_sha256="0" * 64,
        generated_at=GENERATED_AT.replace(tzinfo=None),
        published_at=PUBLISHED_AT.replace(tzinfo=None),
        supersedes_edition_id=None,
        created_at=PUBLISHED_AT.replace(tzinfo=None),
    )
    active_user = SimpleNamespace(
        user_id=USER_ID,
        account_status="active",
        deleted_at=None,
    )
    session = _Session(_Result(active_user), _Result(existing))

    with pytest.raises(EditionGenerationConflict, match="generation_key"):
        asyncio.run(
            publish_morning_edition(
                session,
                user_id=USER_ID,
                generation_key="2026-07-21:run-001",
                edition=_edition(),
                published_at=PUBLISHED_AT,
            )
        )
