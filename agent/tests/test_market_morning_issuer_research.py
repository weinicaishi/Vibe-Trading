"""Private issuer research history and lightweight-note contracts."""

from __future__ import annotations

import asyncio
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import mysql
from sqlalchemy.schema import CreateTable

from src.market_morning.repositories.event_briefs import event_brief_payload_sha256

REPO_ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 7, 21, 0, 30, tzinfo=timezone.utc)
USER_ID = "11111111-1111-4111-8111-111111111111"
ISSUER_ID = "22222222-2222-4222-8222-222222222222"
EVENT_ID = "33333333-3333-4333-8333-333333333333"
SOURCE_ID = "44444444-4444-4444-8444-444444444444"
NOTE_ID = "55555555-5555-4555-8555-555555555555"


class _Result:
    def __init__(self, *, scalar=None, mappings=()):
        self.scalar = scalar
        self.mapping_rows = mappings

    def scalar_one_or_none(self):
        return self.scalar

    def mappings(self):
        return SimpleNamespace(
            one_or_none=lambda: self.mapping_rows[0] if self.mapping_rows else None,
            all=lambda: self.mapping_rows,
        )


class _Session:
    def __init__(self, *results):
        self.results = deque(results)
        self.added = []
        self.deleted = []
        self.statements = []
        self.flush_count = 0

    async def execute(self, statement):
        self.statements.append(statement)
        return self.results.popleft()

    def add(self, value):
        self.added.append(value)

    async def delete(self, value):
        self.deleted.append(value)

    async def flush(self):
        self.flush_count += 1


def _brief_payload() -> dict:
    return {
        "schema_version": 1,
        "status": "published",
        "event_id": EVENT_ID,
        "event_version": 2,
        "title": "業績予想の修正",
        "occurred_at": NOW.isoformat(),
        "confirmed_facts": [
            {"text": "通期売上高予想を修正した。", "source_ids": [SOURCE_ID]},
        ],
        "open_questions": [],
        "source_ids": [SOURCE_ID],
        "sources": [
            {
                "source_id": SOURCE_ID,
                "provider": "tdnet",
                "document_id": "TD-7203-001",
                "revision_key": "v2",
                "original_url": "https://example.jp/tdnet/TD-7203-001-v2.pdf",
                "published_at": NOW.isoformat(),
            }
        ],
        "model_version": "model-v1",
        "review_status": "approved",
        "error_codes": [],
    }


def _event_row() -> dict:
    payload = _brief_payload()
    return {
        "event_id": EVENT_ID,
        "event_family_key": "tdnet:TD-7203-001",
        "event_version": 2,
        "issuer_id": ISSUER_ID,
        "title": "（訂正）業績予想の修正",
        "event_type": "guidance_revision",
        "occurred_at": NOW.replace(tzinfo=None),
        "event_lifecycle_status": "corrected",
        "supersedes_event_id": "33333333-3333-4333-8333-222222222222",
        "source_record_id": SOURCE_ID,
        "source_provider": "tdnet",
        "provider_document_id": "TD-7203-001",
        "provider_revision_key": "v2",
        "original_url": "https://example.jp/tdnet/TD-7203-001-v2.pdf",
        "source_published_at": NOW.replace(tzinfo=None),
        "brief_status": "published",
        "brief_review_status": "approved",
        "brief_payload": payload,
        "brief_payload_sha256": event_brief_payload_sha256(payload),
    }


def test_issuer_note_schema_is_private_bounded_and_migrated_after_interactions() -> None:
    from src.market_morning.models import IssuerResearchNoteRecord

    ddl = str(
        CreateTable(IssuerResearchNoteRecord.__table__).compile(
            dialect=mysql.dialect()
        )
    )
    assert "uq_mm_issuer_research_note_user_issuer" in ddl
    assert "FOREIGN KEY(user_id) REFERENCES mm_users" in ddl
    assert "FOREIGN KEY(issuer_id) REFERENCES mm_issuers" in ddl
    assert "telemetry" not in ddl.lower()

    migration = (
        REPO_ROOT
        / "agent/migrations/market_morning/versions/0014_market_morning_issuer_research.py"
    )
    assert migration.exists()
    text = migration.read_text(encoding="utf-8")
    assert (
        'down_revision: str | None = "0013_market_morning_edition_interactions"'
        in text
    )
    assert '"mm_issuer_research_notes"' in text


def test_research_statements_require_active_user_watchlist_scope() -> None:
    import src.market_morning.issuer_research as service

    access_sql = str(
        service.build_issuer_research_access_statement(
            user_id=USER_ID,
            issuer_id=ISSUER_ID,
        ).compile(
            dialect=mysql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    history_sql = str(
        service.build_issuer_history_statement(issuer_id=ISSUER_ID, limit=50).compile(
            dialect=mysql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )

    assert "mm_watchlist_items.user_id" in access_sql
    assert "mm_watchlist_items.removed_at IS NULL" in access_sql
    assert "mm_users.account_status = 'active'" in access_sql
    assert "mm_users.deleted_at IS NULL" in access_sql
    assert "LIMIT 50" in history_sql
    assert "mm_event_briefs" in history_sql
    assert "brief_rank" in history_sql


def test_loading_research_returns_verified_brief_history_and_private_note() -> None:
    import src.market_morning.issuer_research as service

    note = SimpleNamespace(
        note_id=NOTE_ID,
        user_id=USER_ID,
        issuer_id=ISSUER_ID,
        note_text="決算説明会の設備投資方針を確認する。",
        created_at=NOW.replace(tzinfo=None),
        updated_at=NOW.replace(tzinfo=None),
    )
    session = _Session(
        _Result(
            mappings=(
                {
                    "issuer_id": ISSUER_ID,
                    "issuer_code": "7203",
                    "legal_name_ja": "トヨタ自動車株式会社",
                    "market_segment": "プライム",
                },
            )
        ),
        _Result(mappings=(_event_row(),)),
        _Result(scalar=note),
    )

    result = asyncio.run(
        service.load_issuer_research(
            session,
            user_id=USER_ID,
            issuer_id=ISSUER_ID,
            limit=50,
        )
    )

    assert result.issuer_code == "7203"
    assert result.note is not None
    assert result.note.text == "決算説明会の設備投資方針を確認する。"
    assert len(result.events) == 1
    event = result.events[0]
    assert event.lifecycle_status == "corrected"
    assert event.supersedes_event_id is not None
    assert event.facts[0].text == "通期売上高予想を修正した。"
    assert event.facts[0].citations[0].document_id == "TD-7203-001"


def test_research_access_and_note_write_fail_closed(monkeypatch) -> None:
    import src.market_morning.issuer_research as service

    with pytest.raises(service.IssuerResearchUnavailable):
        asyncio.run(
            service.load_issuer_research(
                _Session(_Result(mappings=())),
                user_id=USER_ID,
                issuer_id=ISSUER_ID,
            )
        )

    session = _Session(
        _Result(
            mappings=(
                {
                    "issuer_id": ISSUER_ID,
                    "issuer_code": "7203",
                    "legal_name_ja": "トヨタ自動車株式会社",
                    "market_segment": "プライム",
                },
            )
        ),
        _Result(),
        _Result(
            scalar=SimpleNamespace(
                note_id=NOTE_ID,
                user_id=USER_ID,
                issuer_id=ISSUER_ID,
                note_text="次回の開示で海外販売台数を確認する。",
                created_at=NOW.replace(tzinfo=None),
                updated_at=NOW.replace(tzinfo=None),
            )
        ),
    )
    monkeypatch.setattr(service, "new_id", lambda: NOTE_ID)
    note = asyncio.run(
        service.set_issuer_research_note(
            session,
            user_id=USER_ID,
            issuer_id=ISSUER_ID,
            text="  次回の開示で海外販売台数を確認する。  ",
            changed_at=NOW,
        )
    )

    assert note.text == "次回の開示で海外販売台数を確認する。"
    assert session.added == []
    assert not any(hasattr(value, "event_name") for value in session.added)
    with pytest.raises(service.IssuerResearchValidationError):
        asyncio.run(
            service.set_issuer_research_note(
                _Session(),
                user_id=USER_ID,
                issuer_id=ISSUER_ID,
                text="x" * 1001,
                changed_at=NOW,
            )
        )


def test_note_upsert_uses_mysql_unique_key_for_concurrent_writers() -> None:
    import src.market_morning.issuer_research as service

    sql = str(
        service.build_issuer_note_upsert_statement(
            note_id=NOTE_ID,
            user_id=USER_ID,
            issuer_id=ISSUER_ID,
            text="次回の開示を確認する。",
            changed_at=NOW.replace(tzinfo=None),
        ).compile(
            dialect=mysql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )

    assert "ON DUPLICATE KEY UPDATE" in sql
    assert "note_text = %s" not in sql
    assert "updated_at" in sql


def test_clearing_private_note_requires_access_and_deletes_only_note() -> None:
    import src.market_morning.issuer_research as service

    note = SimpleNamespace(
        note_id=NOTE_ID,
        user_id=USER_ID,
        issuer_id=ISSUER_ID,
        note_text="次回の開示を確認する。",
        created_at=NOW.replace(tzinfo=None),
        updated_at=NOW.replace(tzinfo=None),
    )
    session = _Session(
        _Result(
            mappings=(
                {
                    "issuer_id": ISSUER_ID,
                    "issuer_code": "7203",
                    "legal_name_ja": "トヨタ自動車株式会社",
                    "market_segment": "プライム",
                },
            )
        ),
        _Result(scalar=note),
    )

    deleted = asyncio.run(
        service.clear_issuer_research_note(
            session,
            user_id=USER_ID,
            issuer_id=ISSUER_ID,
        )
    )

    assert deleted is True
    assert session.deleted == [note]
    assert session.added == []
    assert session.flush_count == 1
