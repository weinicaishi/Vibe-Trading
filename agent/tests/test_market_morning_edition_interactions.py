"""Private per-user edition interaction state and source-open audit trail."""

from __future__ import annotations

import asyncio
from collections import deque
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import mysql
from sqlalchemy.schema import CreateTable

from src.market_morning.models import MorningEditionRecord
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
from src.market_morning.repositories.morning_edition import (
    encode_morning_edition,
    morning_edition_payload_sha256,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 7, 21, 0, 30, tzinfo=timezone.utc)
USER_ID = "11111111-1111-4111-8111-111111111111"
EDITION_ID = "22222222-2222-4222-8222-222222222222"
EVENT_ID = "33333333-3333-4333-8333-333333333333"
SOURCE_ID = "44444444-4444-4444-8444-444444444444"
STATE_ID = "55555555-5555-4555-8555-555555555555"
OPEN_ID = "66666666-6666-4666-8666-666666666666"
REQUEST_ID = "77777777-7777-4777-8777-777777777777"


class _Result:
    def __init__(self, scalar=None, rows=()):
        self.scalar = scalar
        self.rows = rows

    def scalar_one_or_none(self):
        return self.scalar

    def scalars(self):
        return SimpleNamespace(all=lambda: self.rows)


class _Session:
    def __init__(self, *results):
        self.results = deque(results)
        self.added = []
        self.statements = []
        self.flush_count = 0

    async def execute(self, statement):
        self.statements.append(statement)
        return self.results.popleft()

    def add(self, value):
        self.added.append(value)

    async def flush(self):
        self.flush_count += 1


def _edition() -> MorningEdition:
    citation = EditionSourceCitation(
        provider="tdnet",
        document_id="TD-7203-001",
        revision_key="v1",
        original_url="https://example.jp/tdnet/TD-7203-001-v1.pdf",
        published_at=NOW,
    )
    return MorningEdition(
        edition_date=date(2026, 7, 21),
        generated_at=NOW,
        status=EditionStatus.READY,
        day_plan=EditionDayPlan(
            edition_date=date(2026, 7, 21),
            generate=True,
            status=EditionDayStatus.SCHEDULED,
            overnight_context=OvernightMarketContext.US_SESSION_AVAILABLE,
            reason_code=None,
            us_reason_code=None,
        ),
        issuers=(
            IssuerBrief(
                issuer_id="88888888-8888-4888-8888-888888888888",
                issuer_code="7203",
                legal_name_ja="トヨタ自動車株式会社",
                status=IssuerBriefStatus.READY,
                facts=(
                    FactStatement(
                        kind=StatementKind.FACT,
                        text="通期売上高予想を修正した。",
                        event_id=EVENT_ID,
                        event_family_key="tdnet:TD-7203-001",
                        event_version=1,
                        event_type="guidance_revision",
                        occurred_at=NOW,
                        lifecycle_status="active",
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


def _edition_record():
    payload = encode_morning_edition(_edition())
    return MorningEditionRecord(
        edition_id=EDITION_ID,
        user_id=USER_ID,
        edition_date=date(2026, 7, 21),
        edition_version=1,
        generation_key=f"user-edition:2026-07-21:{USER_ID}",
        schema_version=1,
        status="ready",
        payload=payload,
        payload_sha256=morning_edition_payload_sha256(payload),
        generated_at=NOW.replace(tzinfo=None),
        published_at=NOW.replace(tzinfo=None),
        supersedes_edition_id=None,
        created_at=NOW.replace(tzinfo=None),
    )


def test_edition_interaction_schema_is_private_idempotent_and_url_free() -> None:
    from src.market_morning.models import (
        EditionEventStateRecord,
        EditionSourceOpenRecord,
    )

    state_ddl = str(
        CreateTable(EditionEventStateRecord.__table__).compile(
            dialect=mysql.dialect()
        )
    )
    open_ddl = str(
        CreateTable(EditionSourceOpenRecord.__table__).compile(
            dialect=mysql.dialect()
        )
    )

    assert "uq_mm_edition_event_state" in state_ddl
    assert "ck_mm_edition_event_state" in state_ddl
    assert "FOREIGN KEY(user_id) REFERENCES mm_users" in state_ddl
    assert "FOREIGN KEY(edition_id) REFERENCES mm_morning_editions" in state_ddl
    assert "uq_mm_edition_source_open_request" in open_ddl
    assert "FOREIGN KEY(source_record_id) REFERENCES mm_source_records" in open_ddl
    assert "original_url" not in open_ddl
    assert "note" not in state_ddl + open_ddl


def test_edition_interaction_migration_follows_delivery_webhooks() -> None:
    migration = (
        REPO_ROOT
        / "agent/migrations/market_morning/versions/0013_market_morning_edition_interactions.py"
    )

    assert migration.exists()
    text = migration.read_text(encoding="utf-8")
    assert 'down_revision: str | None = "0012_market_morning_delivery_webhooks"' in text
    assert '"mm_edition_event_states"' in text
    assert '"mm_edition_source_opens"' in text
    assert "original_url" not in text


def test_setting_event_state_verifies_owned_snapshot_and_event(monkeypatch) -> None:
    import src.market_morning.edition_interactions as service

    session = _Session(_Result(_edition_record()), _Result(None))
    monkeypatch.setattr(service, "new_id", lambda: STATE_ID)
    result = asyncio.run(
        service.set_edition_event_state(
            session,
            user_id=USER_ID,
            edition_id=EDITION_ID,
            event_id=EVENT_ID,
            state="later",
            changed_at=NOW,
        )
    )

    assert result.state.value == "later"
    assert result.event_id == EVENT_ID
    assert session.added[0].user_id == USER_ID
    assert session.added[0].edition_id == EDITION_ID
    assert session.added[0].first_read_at is None

    with pytest.raises(service.EditionInteractionUnavailable):
        asyncio.run(
            service.set_edition_event_state(
                _Session(_Result(None)),
                user_id=USER_ID,
                edition_id=EDITION_ID,
                event_id=EVENT_ID,
                state="read",
                changed_at=NOW,
            )
        )


def test_listing_event_states_returns_only_owned_edition_events() -> None:
    from src.market_morning.models import EditionEventStateRecord
    import src.market_morning.edition_interactions as service

    state = EditionEventStateRecord(
        event_state_id=STATE_ID,
        user_id=USER_ID,
        edition_id=EDITION_ID,
        event_id=EVENT_ID,
        state="read",
        first_read_at=NOW.replace(tzinfo=None),
        created_at=NOW.replace(tzinfo=None),
        updated_at=NOW.replace(tzinfo=None),
    )
    session = _Session(_Result(_edition_record()), _Result(rows=(state,)))

    result = asyncio.run(
        service.list_edition_event_states(
            session,
            user_id=USER_ID,
            edition_id=EDITION_ID,
        )
    )

    assert len(result) == 1
    assert result[0].event_id == EVENT_ID
    assert result[0].state.value == "read"
    assert result[0].first_read_at == NOW


def test_source_open_resolves_exact_citation_and_appends_safe_analytics(
    monkeypatch,
) -> None:
    import src.market_morning.edition_interactions as service

    source = SimpleNamespace(
        source_record_id=SOURCE_ID,
        source_provider="tdnet",
        provider_document_id="TD-7203-001",
        provider_revision_key="v1",
        original_url="https://example.jp/tdnet/TD-7203-001-v1.pdf",
    )
    created = SimpleNamespace(
        source_open_id=OPEN_ID,
        user_id=USER_ID,
        edition_id=EDITION_ID,
        event_id=EVENT_ID,
        source_record_id=SOURCE_ID,
        request_id=REQUEST_ID,
        opened_at=NOW.replace(tzinfo=None),
    )
    session = _Session(
        _Result(_edition_record()),
        _Result(source),
        _Result(),
        _Result(created),
    )
    monkeypatch.setattr(service, "new_id", lambda: OPEN_ID)

    result = asyncio.run(
        service.record_edition_source_open(
            session,
            user_id=USER_ID,
            edition_id=EDITION_ID,
            event_id=EVENT_ID,
            provider="tdnet",
            document_id="TD-7203-001",
            revision_key="v1",
            request_id=REQUEST_ID,
            opened_at=NOW,
        )
    )

    assert result.status.value == "recorded"
    assert result.original_url == source.original_url
    analytics = [item for item in session.added if hasattr(item, "event_name")]
    assert len(analytics) == 1
    assert analytics[0].event_name == "source_opened"
    assert analytics[0].properties == {"source_kind": "tdnet"}
    assert source.original_url not in repr(analytics[0].properties)
