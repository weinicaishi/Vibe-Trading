"""Sprint 3 contracts for licensed-source adapters and event ingestion."""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy.dialects import mysql

from src.market_morning.sources import base

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_source_adapter_module_is_isolated_in_market_morning_domain() -> None:
    try:
        spec = importlib.util.find_spec("src.market_morning.sources.base")
    except ModuleNotFoundError:
        spec = None
    assert spec is not None


def test_fixture_adapter_and_event_normalization_modules_are_isolated() -> None:
    module_names = (
        "src.market_morning.sources.fixture",
        "src.market_morning.pipeline.event_normalization",
    )
    specs = []
    for module_name in module_names:
        try:
            specs.append(importlib.util.find_spec(module_name))
        except ModuleNotFoundError:
            specs.append(None)

    assert all(spec is not None for spec in specs)


def test_japan_source_fixture_adapters_are_isolated_by_provider() -> None:
    module_names = (
        "src.market_morning.sources.tdnet",
        "src.market_morning.sources.edinet",
        "src.market_morning.sources.company_ir",
    )

    specs = []
    for module_name in module_names:
        try:
            specs.append(importlib.util.find_spec(module_name))
        except ModuleNotFoundError:
            specs.append(None)

    assert all(spec is not None for spec in specs)


def test_source_repository_is_isolated_from_existing_vibe_storage() -> None:
    try:
        spec = importlib.util.find_spec(
            "src.market_morning.repositories.source_ingestion"
        )
    except ModuleNotFoundError:
        spec = None

    assert spec is not None


def test_sqlalchemy_repository_implements_batch_transaction_boundary() -> None:
    from src.market_morning.repositories import source_ingestion

    repository_type = getattr(source_ingestion, "SqlAlchemySourceBatchRepository", None)
    result_type = getattr(source_ingestion, "SourceApplyResult", None)

    assert repository_type is not None
    assert result_type is not None
    assert {
        "load_cursor",
        "load_revision_identities",
        "apply",
    } <= set(dir(repository_type))


def test_source_adapter_contract_exposes_auditable_value_objects() -> None:
    required = {
        "DiscoveryBatch",
        "NormalizedSourceRecord",
        "SourceAdapter",
        "SourceDocumentRef",
        "SourceHealth",
        "SourceLifecycleStatus",
        "SourcePayload",
        "SourceValidationError",
    }

    assert required <= set(dir(base))


def test_normalized_record_requires_aware_times_and_original_url() -> None:
    record_type = getattr(base, "NormalizedSourceRecord", None)
    assert record_type is not None

    with pytest.raises(ValueError, match="timezone-aware"):
        record_type(
            provider="fixture_tdnet",
            document_id="TD-001",
            revision_key="v1",
            original_url="https://example.invalid/TD-001.pdf",
            title="決算短信",
            document_type="earnings_release",
            published_at=datetime(2026, 7, 20, 23, 0),
            fetched_at=datetime(2026, 7, 20, 23, 5, tzinfo=timezone.utc),
            lifecycle_status=base.SourceLifecycleStatus.ACTIVE,
            issuer_codes=("7203",),
            content_hash_sha256="a" * 64,
            metadata={},
        )

    with pytest.raises(ValueError, match="original URL"):
        record_type(
            provider="fixture_tdnet",
            document_id="TD-001",
            revision_key="v1",
            original_url="",
            title="決算短信",
            document_type="earnings_release",
            published_at=datetime(2026, 7, 20, 23, 0, tzinfo=timezone.utc),
            fetched_at=datetime(2026, 7, 20, 23, 5, tzinfo=timezone.utc),
            lifecycle_status=base.SourceLifecycleStatus.ACTIVE,
            issuer_codes=("7203",),
            content_hash_sha256="a" * 64,
            metadata={},
        )


def test_collect_source_batch_advances_cursor_only_after_every_document_normalizes() -> None:
    from src.market_morning.pipeline.source_ingestion import collect_source_batch

    class FixtureAdapter:
        provider = "fixture_tdnet"

        def __init__(self) -> None:
            self.fail_document: str | None = None

        async def discover(self, cursor):
            assert cursor == {"sequence": 10}
            return base.DiscoveryBatch(
                documents=(
                    base.SourceDocumentRef("TD-011", "v1"),
                    base.SourceDocumentRef("TD-012", "v1"),
                ),
                next_cursor={"sequence": 12},
            )

        async def fetch(self, document):
            if document.document_id == self.fail_document:
                raise RuntimeError("fixture fetch unavailable")
            return base.SourcePayload(
                document=document,
                original_url=f"https://example.invalid/{document.document_id}.pdf",
                fetched_at=datetime(2026, 7, 20, 23, 5, tzinfo=timezone.utc),
                raw_content=document.document_id.encode(),
                metadata={},
            )

        def normalize(self, payload):
            return base.NormalizedSourceRecord.from_payload(
                provider=self.provider,
                payload=payload,
                title=f"開示 {payload.document.document_id}",
                document_type="timely_disclosure",
                published_at=datetime(2026, 7, 20, 23, 0, tzinfo=timezone.utc),
                lifecycle_status=base.SourceLifecycleStatus.ACTIVE,
                issuer_codes=("7203",),
            )

        async def health(self):
            return base.SourceHealth(
                ok=True,
                checked_at=datetime(2026, 7, 20, 23, 0, tzinfo=timezone.utc),
                detail_code="fixture_ready",
            )

    adapter = FixtureAdapter()
    collected = asyncio.run(collect_source_batch(adapter, {"sequence": 10}))

    assert collected.previous_cursor == {"sequence": 10}
    assert collected.next_cursor == {"sequence": 12}
    assert [item.document_id for item in collected.records] == ["TD-011", "TD-012"]

    adapter.fail_document = "TD-012"
    with pytest.raises(RuntimeError, match="fixture fetch unavailable"):
        asyncio.run(collect_source_batch(adapter, {"sequence": 10}))


def test_source_event_schema_has_a_versioned_migration_after_settings() -> None:
    migration = (
        REPO_ROOT
        / "agent"
        / "migrations"
        / "market_morning"
        / "versions"
        / "0004_market_morning_sources_events.py"
    )

    assert migration.exists()
    text = migration.read_text(encoding="utf-8")
    assert 'down_revision: str | None = "0003_market_morning_settings"' in text
    for table_name in (
        "mm_source_cursors",
        "mm_source_records",
        "mm_normalized_events",
        "mm_event_sources",
        "mm_event_merge_candidates",
    ):
        assert f'"{table_name}"' in text


def test_fixture_adapter_exercises_contract_without_network_access() -> None:
    from src.market_morning.sources.fixture import FixtureSourceAdapter, FixtureSourceDocument

    document = FixtureSourceDocument(
        document_id="TD-FIXTURE-001",
        revision_key="v1",
        original_url="https://example.invalid/tdnet/TD-FIXTURE-001.pdf",
        raw_content=b"synthetic tdnet disclosure",
        title="2026年3月期 決算短信",
        document_type="earnings_release",
        published_at=datetime(2026, 7, 20, 23, 0, tzinfo=timezone.utc),
        lifecycle_status=base.SourceLifecycleStatus.ACTIVE,
        issuer_codes=("7203",),
        evidence_text="  売上高は  前期比10%増加した。\n",
        metadata={"fixture": True},
    )
    adapter = FixtureSourceAdapter(
        provider="fixture_tdnet",
        documents=(document,),
        fetched_at=datetime(2026, 7, 20, 23, 5, tzinfo=timezone.utc),
    )

    health = asyncio.run(adapter.health())
    collected = asyncio.run(
        __import__(
            "src.market_morning.pipeline.source_ingestion",
            fromlist=["collect_source_batch"],
        ).collect_source_batch(adapter, None)
    )

    assert health.ok is True
    assert health.detail_code == "synthetic_fixture_ready"
    assert collected.next_cursor == {"offset": 1}
    assert collected.records[0].metadata == {"fixture": True}
    assert collected.records[0].evidence_text == "売上高は 前期比10%増加した。"
    assert collected.records[0].content_hash_sha256 == hashlib.sha256(
        document.raw_content
    ).hexdigest()


def test_tdnet_fixture_preserves_correction_and_withdrawal_revisions() -> None:
    from src.market_morning.pipeline.source_ingestion import collect_source_batch
    from src.market_morning.sources.tdnet import TdnetFixtureAdapter

    adapter = TdnetFixtureAdapter(
        rows=(
            {
                "disclosure_id": "TD-7203-001",
                "revision": "v1",
                "title": "2026年3月期 決算短信",
                "published_at": "2026-07-20T15:00:00+09:00",
                "issuer_codes": ["7203"],
                "original_url": "https://example.invalid/tdnet/TD-7203-001-v1.pdf",
                "status": "active",
                "document_type": "earnings_release",
                "raw_content": "synthetic tdnet v1",
            },
            {
                "disclosure_id": "TD-7203-001",
                "revision": "v2",
                "title": "（訂正）2026年3月期 決算短信",
                "published_at": "2026-07-20T16:00:00+09:00",
                "issuer_codes": ["7203"],
                "original_url": "https://example.invalid/tdnet/TD-7203-001-v2.pdf",
                "status": "corrected",
                "document_type": "earnings_release",
                "raw_content": "synthetic tdnet v2",
            },
            {
                "disclosure_id": "TD-7203-001",
                "revision": "v3",
                "title": "（取消）2026年3月期 決算短信",
                "published_at": "2026-07-20T17:00:00+09:00",
                "issuer_codes": ["7203"],
                "original_url": "https://example.invalid/tdnet/TD-7203-001-v3.pdf",
                "status": "withdrawn",
                "document_type": "earnings_release",
                "raw_content": "synthetic tdnet v3",
            },
        ),
        fetched_at=datetime(2026, 7, 20, 23, 5, tzinfo=timezone.utc),
    )

    collected = asyncio.run(collect_source_batch(adapter, None))

    assert {record.document_id for record in collected.records} == {"TD-7203-001"}
    assert [record.revision_key for record in collected.records] == ["v1", "v2", "v3"]
    assert [record.lifecycle_status for record in collected.records] == [
        base.SourceLifecycleStatus.ACTIVE,
        base.SourceLifecycleStatus.CORRECTED,
        base.SourceLifecycleStatus.WITHDRAWN,
    ]


def test_tdnet_fixture_fails_closed_when_source_traceability_is_incomplete() -> None:
    from src.market_morning.sources.tdnet import TdnetFixtureAdapter

    with pytest.raises(base.SourceValidationError, match="original_url"):
        TdnetFixtureAdapter(
            rows=(
                {
                    "disclosure_id": "TD-INVALID",
                    "revision": "v1",
                    "title": "missing source link",
                    "published_at": "2026-07-20T15:00:00+09:00",
                    "issuer_codes": ["7203"],
                    "status": "active",
                    "document_type": "timely_disclosure",
                    "raw_content": "synthetic",
                },
            ),
            fetched_at=datetime(2026, 7, 20, 23, 5, tzinfo=timezone.utc),
        )


def test_edinet_fixture_uses_metadata_update_as_revision_identity() -> None:
    from src.market_morning.pipeline.source_ingestion import collect_source_batch
    from src.market_morning.sources.edinet import EdinetFixtureAdapter

    adapter = EdinetFixtureAdapter(
        rows=(
            {
                "document_id": "ED-7203-001",
                "metadata_updated_at": "2026-07-20T15:01:00+09:00",
                "submitted_at": "2026-07-20T15:00:00+09:00",
                "title": "有価証券報告書",
                "issuer_codes": ["7203"],
                "original_url": "https://example.invalid/edinet/ED-7203-001",
                "document_type": "annual_securities_report",
                "raw_content": "synthetic edinet metadata v1",
            },
            {
                "document_id": "ED-7203-001",
                "metadata_updated_at": "2026-07-20T18:30:00+09:00",
                "submitted_at": "2026-07-20T15:00:00+09:00",
                "title": "有価証券報告書（更新）",
                "issuer_codes": ["7203"],
                "original_url": "https://example.invalid/edinet/ED-7203-001",
                "document_type": "annual_securities_report",
                "raw_content": "synthetic edinet metadata v2",
            },
        ),
        fetched_at=datetime(2026, 7, 20, 23, 5, tzinfo=timezone.utc),
    )

    collected = asyncio.run(collect_source_batch(adapter, None))

    assert [record.revision_key for record in collected.records] == [
        "2026-07-20T15:01:00+09:00",
        "2026-07-20T18:30:00+09:00",
    ]
    assert all(
        record.lifecycle_status is base.SourceLifecycleStatus.ACTIVE
        for record in collected.records
    )


def test_company_ir_fixture_enforces_url_whitelist_and_isolates_health() -> None:
    from src.market_morning.sources.company_ir import (
        CompanyIrFeedConfig,
        CompanyIrFixtureAdapter,
    )

    config = CompanyIrFeedConfig(
        issuer_code="7203",
        provider_key="toyota",
        allowed_base_url="https://global.toyota/en/ir/",
    )
    healthy = CompanyIrFixtureAdapter(
        config=config,
        rows=(
            {
                "document_id": "IR-7203-001",
                "revision": "v1",
                "published_at": "2026-07-20T15:00:00+09:00",
                "title": "決算説明会資料",
                "original_url": "https://global.toyota/en/ir/library/result/",
                "document_type": "investor_presentation",
                "raw_content": "synthetic company IR",
            },
        ),
        fetched_at=datetime(2026, 7, 20, 23, 5, tzinfo=timezone.utc),
    )
    unavailable = CompanyIrFixtureAdapter(
        config=CompanyIrFeedConfig(
            issuer_code="6758",
            provider_key="sony",
            allowed_base_url="https://www.sony.com/en/SonyInfo/IR/",
        ),
        rows=(),
        fetched_at=datetime(2026, 7, 20, 23, 5, tzinfo=timezone.utc),
        available=False,
    )

    assert asyncio.run(healthy.health()).ok is True
    assert asyncio.run(unavailable.health()).ok is False
    assert healthy.provider != unavailable.provider

    with pytest.raises(base.SourceValidationError, match="whitelist"):
        CompanyIrFixtureAdapter(
            config=config,
            rows=(
                {
                    "document_id": "IR-INVALID",
                    "revision": "v1",
                    "published_at": "2026-07-20T15:00:00+09:00",
                    "title": "host prefix attack",
                    "original_url": "https://global.toyota.evil.example/en/ir/",
                    "document_type": "investor_presentation",
                    "raw_content": "synthetic",
                },
            ),
            fetched_at=datetime(2026, 7, 20, 23, 5, tzinfo=timezone.utc),
        )


def test_event_family_is_stable_across_revisions_of_one_provider_document() -> None:
    from src.market_morning.pipeline.event_normalization import build_event_family_key

    first = build_event_family_key(
        provider="fixture_tdnet",
        provider_document_id="TD-001",
        issuer_id="11111111-1111-4111-8111-111111111111",
    )
    correction = build_event_family_key(
        provider="fixture_tdnet",
        provider_document_id="TD-001",
        issuer_id="11111111-1111-4111-8111-111111111111",
    )
    other_provider = build_event_family_key(
        provider="fixture_edinet",
        provider_document_id="TD-001",
        issuer_id="11111111-1111-4111-8111-111111111111",
    )

    assert first == correction
    assert first != other_provider
    assert len(first) == 64


def test_cross_source_similarity_creates_review_candidate_but_never_auto_merges() -> None:
    from src.market_morning.pipeline.event_normalization import (
        EventCandidateInput,
        propose_merge_candidate,
    )

    left = EventCandidateInput(
        event_id="event-a",
        issuer_id="issuer-7203",
        source_provider="fixture_tdnet",
        title="2026年3月期 決算短信",
        occurred_at=datetime(2026, 7, 20, 23, 0, tzinfo=timezone.utc),
    )
    right = EventCandidateInput(
        event_id="event-b",
        issuer_id="issuer-7203",
        source_provider="fixture_ir",
        title="2026年3月期 決算短信（訂正）",
        occurred_at=datetime(2026, 7, 21, 1, 0, tzinfo=timezone.utc),
    )

    candidate = propose_merge_candidate(left, right)

    assert candidate is not None
    assert candidate.left_event_id == "event-a"
    assert candidate.right_event_id == "event-b"
    assert candidate.review_status == "pending"
    assert candidate.auto_merge is False

    assert propose_merge_candidate(
        left,
        EventCandidateInput(
            event_id="event-c",
            issuer_id="issuer-6758",
            source_provider="fixture_ir",
            title=right.title,
            occurred_at=right.occurred_at,
        ),
    ) is None
    assert propose_merge_candidate(
        left,
        EventCandidateInput(
            event_id="event-d",
            issuer_id=left.issuer_id,
            source_provider="fixture_tdnet",
            title=right.title,
            occurred_at=right.occurred_at,
        ),
    ) is None


def _normalized_record(
    *,
    revision_key: str,
    status: base.SourceLifecycleStatus,
) -> base.NormalizedSourceRecord:
    return base.NormalizedSourceRecord(
        provider="fixture_tdnet",
        document_id="TD-CHAIN-001",
        revision_key=revision_key,
        original_url=f"https://example.invalid/{revision_key}.pdf",
        title="決算短信",
        document_type="earnings_release",
        published_at=datetime(2026, 7, 20, 23, 0, tzinfo=timezone.utc),
        fetched_at=datetime(2026, 7, 20, 23, 5, tzinfo=timezone.utc),
        lifecycle_status=status,
        issuer_codes=("7203",),
        content_hash_sha256=(revision_key[-1] * 64),
        metadata={},
    )


def test_persistence_plan_deduplicates_exact_revision_and_chains_new_versions() -> None:
    from src.market_morning.pipeline.source_ingestion import (
        CollectedSourceBatch,
        SourceRevisionIdentity,
        build_source_persistence_plan,
    )

    existing = SourceRevisionIdentity("fixture_tdnet", "TD-CHAIN-001", "v1")
    batch = CollectedSourceBatch(
        provider="fixture_tdnet",
        previous_cursor={"sequence": 1},
        next_cursor={"sequence": 3},
        records=(
            _normalized_record(revision_key="v1", status=base.SourceLifecycleStatus.ACTIVE),
            _normalized_record(revision_key="v2", status=base.SourceLifecycleStatus.CORRECTED),
            _normalized_record(revision_key="v3", status=base.SourceLifecycleStatus.WITHDRAWN),
        ),
    )

    plan = build_source_persistence_plan(batch, existing_revisions=(existing,))

    assert [item.action for item in plan.items] == ["duplicate", "insert", "insert"]
    assert plan.items[0].identity == existing
    assert plan.items[1].supersedes_identity == existing
    assert plan.items[2].supersedes_identity == plan.items[1].identity
    assert plan.next_cursor == {"sequence": 3}
    assert plan.insert_count == 2
    assert plan.duplicate_count == 1


def test_ingestion_does_not_call_repository_when_collection_is_incomplete() -> None:
    from src.market_morning.pipeline.source_ingestion import run_source_ingestion

    class FailingAdapter:
        provider = "fixture_tdnet"

        async def discover(self, _cursor):
            return base.DiscoveryBatch(
                documents=(base.SourceDocumentRef("TD-FAIL", "v1"),),
                next_cursor={"sequence": 1},
            )

        async def fetch(self, _document):
            raise RuntimeError("source unavailable")

        def normalize(self, _payload):
            raise AssertionError("normalize must not run")

        async def health(self):
            raise AssertionError("health is not part of ingestion")

    class RecordingRepository:
        applied = False

        async def load_cursor(self, provider):
            assert provider == "fixture_tdnet"
            return {"sequence": 0}

        async def load_revision_identities(self, provider, document_ids):
            raise AssertionError("history lookup must wait for complete collection")

        async def apply(self, plan):
            self.applied = True

    repository = RecordingRepository()
    with pytest.raises(RuntimeError, match="source unavailable"):
        asyncio.run(run_source_ingestion(FailingAdapter(), repository))

    assert repository.applied is False


def test_mysql_source_values_preserve_provenance_without_raw_document_bytes() -> None:
    from src.market_morning.pipeline.source_ingestion import (
        PlannedSourceRecord,
        SourceRevisionIdentity,
    )
    from src.market_morning.repositories.source_ingestion import build_source_record_values

    jst = timezone(timedelta(hours=9))
    record = base.NormalizedSourceRecord(
        provider="fixture_tdnet",
        document_id="TD-PERSIST-001",
        revision_key="v1",
        original_url="https://example.invalid/TD-PERSIST-001.pdf",
        title="適時開示",
        document_type="timely_disclosure",
        published_at=datetime(2026, 7, 21, 8, 0, tzinfo=jst),
        fetched_at=datetime(2026, 7, 20, 23, 5, tzinfo=timezone.utc),
        lifecycle_status=base.SourceLifecycleStatus.ACTIVE,
        issuer_codes=("7203",),
        content_hash_sha256="a" * 64,
        evidence_text="売上高は前期比10%増加した。",
        metadata={"disclosure_number": "TD-PERSIST-001"},
    )
    item = PlannedSourceRecord(
        action="insert",
        identity=SourceRevisionIdentity(
            "fixture_tdnet", "TD-PERSIST-001", "v1"
        ),
        record=record,
        supersedes_identity=None,
    )

    values = build_source_record_values(item, source_record_id="record-001")

    assert values["published_at"] == datetime(2026, 7, 20, 23, 0)
    assert values["published_at"].tzinfo is None
    assert values["original_url"] == record.original_url
    assert values["normalized_payload"] == {
        "issuer_codes": ["7203"],
        "evidence_text": "売上高は前期比10%増加した。",
        "metadata": {"disclosure_number": "TD-PERSIST-001"},
    }
    assert "raw_content" not in values


def test_normalized_source_record_rejects_oversized_evidence() -> None:
    with pytest.raises(base.SourceValidationError, match="at most 50000"):
        base.NormalizedSourceRecord(
            provider="fixture_tdnet",
            document_id="TD-EVIDENCE-001",
            revision_key="v1",
            original_url="https://example.invalid/TD-EVIDENCE-001.pdf",
            title="適時開示",
            document_type="timely_disclosure",
            published_at=datetime(2026, 7, 21, 8, 0, tzinfo=timezone.utc),
            fetched_at=datetime(2026, 7, 21, 8, 1, tzinfo=timezone.utc),
            lifecycle_status=base.SourceLifecycleStatus.ACTIVE,
            issuer_codes=("7203",),
            content_hash_sha256="a" * 64,
            evidence_text="証" * 50_001,
        )


def test_mysql_upserts_use_unique_identity_as_concurrency_backstop() -> None:
    from src.market_morning.repositories.source_ingestion import (
        build_cursor_upsert_statement,
        build_source_record_upsert_statement,
    )

    source_sql = str(
        build_source_record_upsert_statement(
            {
                "source_record_id": "record-001",
                "source_provider": "fixture_tdnet",
                "provider_document_id": "TD-001",
                "provider_revision_key": "v1",
                "original_url": "https://example.invalid/TD-001.pdf",
                "title": "適時開示",
                "document_type": "timely_disclosure",
                "published_at": datetime(2026, 7, 20, 23, 0),
                "fetched_at": datetime(2026, 7, 20, 23, 5),
                "lifecycle_status": "active",
                "content_hash_sha256": "a" * 64,
                "normalized_payload": {"issuer_codes": ["7203"], "metadata": {}},
                "supersedes_record_id": None,
                "created_at": datetime(2026, 7, 20, 23, 5),
            }
        ).compile(dialect=mysql.dialect())
    )
    cursor_sql = str(
        build_cursor_upsert_statement(
            provider="fixture_tdnet",
            cursor={"sequence": 1},
            completed_at=datetime(2026, 7, 20, 23, 5),
        ).compile(dialect=mysql.dialect())
    )

    assert "ON DUPLICATE KEY UPDATE" in source_sql
    assert "source_record_id = mm_source_records.source_record_id" in source_sql
    assert "ON DUPLICATE KEY UPDATE" in cursor_sql
    assert "last_successful_discovery_at" in cursor_sql


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (base.SourceLifecycleStatus.ACTIVE, "primary"),
        (base.SourceLifecycleStatus.CORRECTED, "correction"),
        (base.SourceLifecycleStatus.WITHDRAWN, "withdrawal"),
    ],
)
def test_event_source_relation_type_follows_lifecycle(status, expected) -> None:
    from src.market_morning.repositories.source_ingestion import source_relation_type

    assert source_relation_type(status) == expected


def test_source_apply_rejects_a_cursor_advanced_during_network_collection() -> None:
    from src.market_morning.pipeline.source_ingestion import SourcePersistencePlan
    from src.market_morning.repositories.source_ingestion import (
        SourceCursorConflict,
        SqlAlchemySourceBatchRepository,
    )

    class Result:
        def scalar_one_or_none(self):
            return {"sequence": 2}

    class Session:
        def __init__(self):
            self.statements = []

        async def execute(self, statement):
            self.statements.append(statement)
            return Result()

    session = Session()
    plan = SourcePersistencePlan(
        provider="fixture_tdnet",
        previous_cursor={"sequence": 1},
        next_cursor={"sequence": 3},
        items=(),
    )

    with pytest.raises(SourceCursorConflict, match="cursor advanced"):
        asyncio.run(SqlAlchemySourceBatchRepository(session).apply(plan))

    sql = str(session.statements[0].compile(dialect=mysql.dialect()))
    assert "FROM mm_source_cursors" in sql
    assert "FOR UPDATE" in sql
