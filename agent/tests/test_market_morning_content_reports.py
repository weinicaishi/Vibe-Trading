"""Privacy-safe content reports tied to immutable owned editions."""

from __future__ import annotations

import asyncio
from collections import deque
from datetime import date, datetime, timezone
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
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
REPORT_ID = "44444444-4444-4444-8444-444444444444"


class _Result:
    def __init__(self, *, scalar=None, mappings=()):
        self.scalar = scalar
        self.mapping_rows = mappings

    def scalar_one_or_none(self):
        return self.scalar

    def mappings(self):
        return SimpleNamespace(all=lambda: self.mapping_rows)


class _Session:
    def __init__(self, *results):
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


def _edition(*, event_id: str = EVENT_ID) -> MorningEdition:
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
                issuer_id="55555555-5555-4555-8555-555555555555",
                issuer_code="7203",
                legal_name_ja="トヨタ自動車株式会社",
                status=IssuerBriefStatus.READY,
                facts=(
                    FactStatement(
                        kind=StatementKind.FACT,
                        text="通期売上高予想を修正した。",
                        event_id=event_id,
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


def _edition_record(*, event_id: str = EVENT_ID) -> MorningEditionRecord:
    payload = encode_morning_edition(_edition(event_id=event_id))
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


def test_content_report_schema_is_private_bounded_and_idempotent() -> None:
    from src.market_morning.models import ContentReportRecord

    ddl = str(CreateTable(ContentReportRecord.__table__).compile(dialect=mysql.dialect()))

    assert "uq_mm_content_report_user_edition_event" in ddl
    assert "ck_mm_content_report_reason" in ddl
    assert "ck_mm_content_report_status" in ddl
    assert "ck_mm_content_report_resolution_state" in ddl
    assert "FOREIGN KEY(user_id) REFERENCES mm_users" in ddl
    assert "FOREIGN KEY(edition_id) REFERENCES mm_morning_editions" in ddl
    assert "FOREIGN KEY(event_id) REFERENCES mm_normalized_events" in ddl
    assert "free_text" not in ddl
    assert "comment" not in ddl
    assert "payload" not in ddl
    assert "url" not in ddl


def test_content_report_migration_follows_model_usage() -> None:
    migration = REPO_ROOT / "agent/migrations/market_morning/versions/0017_market_morning_content_reports.py"

    assert migration.exists()
    text = migration.read_text(encoding="utf-8")
    assert 'down_revision: str | None = "0016_market_morning_model_usage"' in text
    assert '"alembic_version"' in text
    assert "type_=sa.String(64)" in text
    assert text.index("op.alter_column") < text.index("op.create_table")
    assert '"mm_content_reports"' in text
    assert "free_text" not in text
    assert "comment" not in text
    assert "payload" not in text
    assert "original_url" not in text


def test_0018_mysql_delivery_sql_matches_current_metadata_and_revision() -> None:
    from src.market_morning.db import EXPECTED_MARKET_MORNING_SCHEMA_REVISION
    from src.market_morning.models import Base

    directory = REPO_ROOT / "database/market-morning/mysql"
    bootstrap = (directory / "market_morning_schema_0018.sql").read_text(encoding="utf-8")
    incremental = (directory / "market_morning_upgrade_0017_to_0018.sql").read_text(encoding="utf-8")
    verifier = (directory / "market_morning_verify_0018.sql").read_text(encoding="utf-8")

    assert bootstrap.count("CREATE TABLE mm_") == len(Base.metadata.tables) == 34
    assert "CREATE TABLE mm_content_reports" in bootstrap
    assert bootstrap.index("ALTER TABLE alembic_version") < bootstrap.index(
        "version_num='0004_market_morning_sources_events'"
    )
    assert bootstrap.index("ALTER TABLE alembic_version") < bootstrap.index(
        "version_num='0018_market_morning_auth_sessions'"
    )
    assert incremental.index("CREATE TABLE mm_auth_sessions") < incremental.index(
        "version_num='0018_market_morning_auth_sessions'"
    )
    assert EXPECTED_MARKET_MORNING_SCHEMA_REVISION in bootstrap
    assert EXPECTED_MARKET_MORNING_SCHEMA_REVISION in verifier
    assert "market_morning_table_count" in verifier
    assert "alembic_version_column_type" in verifier


def test_submit_content_report_requires_owned_edition_event_and_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.market_morning.content_reports as reports

    session = _Session(_Result(scalar=_edition_record()), _Result(scalar=None))
    monkeypatch.setattr(reports, "new_id", lambda: REPORT_ID)
    result = asyncio.run(
        reports.submit_content_report(
            session,
            user_id=USER_ID,
            edition_id=EDITION_ID,
            event_id=EVENT_ID,
            reason_code="fact_inaccurate",
            reported_at=NOW,
        )
    )

    assert result.status == "reported"
    assert result.report_id == REPORT_ID
    assert result.report_status == "pending"
    assert result.reason_code == "fact_inaccurate"
    assert len(session.added) == 1
    assert session.added[0].user_id == USER_ID
    assert session.added[0].edition_id == EDITION_ID
    assert session.added[0].event_id == EVENT_ID
    assert not hasattr(session.added[0], "free_text")

    existing = session.added[0]
    duplicate = asyncio.run(
        reports.submit_content_report(
            _Session(
                _Result(scalar=_edition_record()),
                _Result(scalar=existing),
            ),
            user_id=USER_ID,
            edition_id=EDITION_ID,
            event_id=EVENT_ID,
            reason_code="source_mismatch",
            reported_at=NOW,
        )
    )
    assert duplicate.status == "already_reported"
    assert duplicate.report_id == REPORT_ID
    assert duplicate.reason_code == "fact_inaccurate"


def test_submit_content_report_rejects_unowned_or_absent_snapshot_event() -> None:
    import src.market_morning.content_reports as reports

    with pytest.raises(reports.ContentReportUnavailable):
        asyncio.run(
            reports.submit_content_report(
                _Session(_Result(scalar=None)),
                user_id=USER_ID,
                edition_id=EDITION_ID,
                event_id=EVENT_ID,
                reason_code="fact_inaccurate",
                reported_at=NOW,
            )
        )

    with pytest.raises(reports.ContentReportUnavailable):
        asyncio.run(
            reports.submit_content_report(
                _Session(_Result(scalar=_edition_record(event_id=REPORT_ID))),
                user_id=USER_ID,
                edition_id=EDITION_ID,
                event_id=EVENT_ID,
                reason_code="fact_inaccurate",
                reported_at=NOW,
            )
        )


def test_content_report_queue_is_bounded_and_does_not_select_user_or_payload() -> None:
    import src.market_morning.content_reports as reports

    statement = reports.build_content_report_queue_statement(
        report_status="pending",
        limit=20,
    )
    sql = str(
        statement.compile(
            dialect=mysql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )

    assert "LIMIT 20" in sql
    assert "mm_content_reports" in sql
    assert "mm_normalized_events" in sql
    assert "mm_issuers" in sql
    selected = sql.split("FROM", 1)[0]
    assert "user_id" not in selected
    assert "payload" not in sql
    assert "original_url" not in sql
    assert "note_text" not in sql


def test_review_content_report_is_audited_idempotent_and_terminal() -> None:
    import src.market_morning.content_reports as reports
    from src.market_morning.models import AuditLog, ContentReportRecord

    record = ContentReportRecord(
        report_id=REPORT_ID,
        user_id=USER_ID,
        edition_id=EDITION_ID,
        event_id=EVENT_ID,
        reason_code="fact_inaccurate",
        status="pending",
        resolution_code=None,
        reviewed_by=None,
        reviewed_at=None,
        created_at=NOW.replace(tzinfo=None),
        updated_at=NOW.replace(tzinfo=None),
    )
    session = _Session(_Result(scalar=record))
    result = asyncio.run(
        reports.review_content_report(
            session,
            report_id=REPORT_ID,
            decision="resolve",
            resolution_code="brief_rejected",
            actor_reference="operator-17",
            reviewed_at=NOW,
        )
    )

    assert result.status == "reviewed"
    assert result.report_status == "resolved"
    assert record.status == "resolved"
    assert record.resolution_code == "brief_rejected"
    assert record.reviewed_by == "operator-17"
    audits = [item for item in session.added if isinstance(item, AuditLog)]
    assert len(audits) == 1
    assert audits[0].action == "content_report.resolved"
    assert audits[0].actor_user_id is None
    assert "user_id" not in audits[0].details

    repeated = asyncio.run(
        reports.review_content_report(
            _Session(_Result(scalar=record)),
            report_id=REPORT_ID,
            decision="resolve",
            resolution_code="brief_rejected",
            actor_reference="operator-17",
            reviewed_at=NOW,
        )
    )
    assert repeated.status == "already_reviewed"

    with pytest.raises(reports.ContentReportConflict):
        asyncio.run(
            reports.review_content_report(
                _Session(_Result(scalar=record)),
                report_id=REPORT_ID,
                decision="dismiss",
                resolution_code="no_issue_found",
                actor_reference="operator-17",
                reviewed_at=NOW,
            )
        )


def test_authenticated_product_route_reports_only_for_principal_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.api import market_morning_routes
    from src.api.market_morning_auth import (
        MarketMorningPrincipal,
        require_market_morning_principal,
    )
    from src.market_morning.content_reports import ContentReportResult

    async def _allow_internal_request() -> None:
        return None

    monkeypatch.setitem(
        sys.modules,
        "api_server",
        SimpleNamespace(require_auth=_allow_internal_request),
    )
    app = FastAPI()
    market_morning_routes.register_market_morning_routes(app)

    async def _principal() -> MarketMorningPrincipal:
        return MarketMorningPrincipal(
            user_id=USER_ID,
            external_subject="verified-subject",
        )

    calls = []

    async def _create(**kwargs):
        calls.append(kwargs)
        return ContentReportResult(
            status="reported",
            report_id=REPORT_ID,
            reason_code="fact_inaccurate",
            report_status="pending",
            created_at=NOW,
        )

    app.dependency_overrides[require_market_morning_principal] = _principal
    monkeypatch.setattr(market_morning_routes, "create_content_report", _create)
    monkeypatch.setenv("VIBE_MARKET_MORNING_ENABLED", "true")
    client = TestClient(app, client=("127.0.0.1", 50000))
    response = client.post(
        f"/market-morning/editions/{EDITION_ID}/events/{EVENT_ID}/report",
        json={"reason_code": "fact_inaccurate"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "status": "reported",
        "report_id": REPORT_ID,
        "reason_code": "fact_inaccurate",
        "report_status": "pending",
        "created_at": "2026-07-21T00:30:00Z",
    }
    assert calls == [
        {
            "user_id": USER_ID,
            "edition_id": EDITION_ID,
            "event_id": EVENT_ID,
            "reason_code": "fact_inaccurate",
        }
    ]


def test_admin_content_report_routes_expose_no_user_or_edition_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.api import market_morning_admin_routes
    from src.market_morning.content_reports import (
        ContentReportQueueItem,
        ContentReportReviewResult,
    )

    async def _allow_internal_request() -> None:
        return None

    monkeypatch.setitem(
        sys.modules,
        "api_server",
        SimpleNamespace(require_auth=_allow_internal_request),
    )
    app = FastAPI()
    market_morning_admin_routes.register_market_morning_admin_routes(app)
    item = ContentReportQueueItem(
        report_id=REPORT_ID,
        edition_date=date(2026, 7, 21),
        event_id=EVENT_ID,
        issuer_id="55555555-5555-4555-8555-555555555555",
        issuer_code="7203",
        legal_name_ja="トヨタ自動車株式会社",
        event_title="決算短信",
        event_type="earnings",
        reason_code="fact_inaccurate",
        report_status="pending",
        resolution_code=None,
        reviewed_by=None,
        reviewed_at=None,
        created_at=NOW,
        updated_at=NOW,
    )
    review_calls = []

    async def _queue(*, report_status: str | None, limit: int):
        assert report_status == "pending"
        assert limit == 20
        return (item,)

    async def _review(**kwargs):
        review_calls.append(kwargs)
        return ContentReportReviewResult(
            status="reviewed",
            report_id=REPORT_ID,
            report_status="resolved",
            resolution_code="brief_rejected",
            reviewed_at=NOW,
        )

    monkeypatch.setattr(
        market_morning_admin_routes,
        "get_content_report_queue",
        _queue,
    )
    monkeypatch.setattr(
        market_morning_admin_routes,
        "apply_content_report_review",
        _review,
    )
    monkeypatch.setenv("VIBE_MARKET_MORNING_ENABLED", "true")
    client = TestClient(app, client=("127.0.0.1", 50000))
    queue_response = client.get(
        "/market-morning/_internal/content-reports",
        params={"report_status": "pending", "limit": 20},
    )
    review_response = client.post(
        f"/market-morning/_internal/content-reports/{REPORT_ID}/review",
        json={
            "decision": "resolve",
            "resolution_code": "brief_rejected",
        },
    )

    assert queue_response.status_code == 200
    assert queue_response.json()["count"] == 1
    assert queue_response.json()["items"][0]["issuer_code"] == "7203"
    assert "user_id" not in queue_response.text
    assert "payload" not in queue_response.text
    assert review_response.status_code == 200
    assert review_calls == [
        {
            "report_id": REPORT_ID,
            "decision": "resolve",
            "resolution_code": "brief_rejected",
            "actor_reference": "vibe-api-key-operator",
        }
    ]
