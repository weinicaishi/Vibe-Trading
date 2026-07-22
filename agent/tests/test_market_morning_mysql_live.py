"""Write-capable acceptance probes for an explicitly dedicated MySQL database."""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import os
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select, text, update

from src.market_morning.db import (
    EXPECTED_MARKET_MORNING_SCHEMA_REVISION,
    get_engine,
    get_session_factory,
    reset_database_state,
)
from src.market_morning.account_privacy import (
    AccountDeletionProcessStatus,
    process_account_deletion,
)
from src.market_morning.models import (
    AccountDeletionRequest,
    ApplicationSessionRecord,
    AuditLog,
    Base,
    DeliveryAttemptRecord,
    DeliveryProviderEventRecord,
    EventBriefRecord,
    GlobalEditionDayRecord,
    GlobalEditionRunRecord,
    Issuer,
    IssuerSnapshot,
    JobAttemptRecord,
    JobRecord,
    ManualOverrideRecord,
    MarketSnapshotRecord,
    ModelUsageEventRecord,
    MorningEditionRecord,
    NormalizedEvent,
    SchedulerLeaseRecord,
    SourceCursor,
    SourceRecord,
    User,
    UserConsent,
)
from src.market_morning.session_ledger import (
    ACCESS_TOKEN_SESSION_REFERENCE,
    ApplicationSessionValidator,
    pseudonymous_oidc_subject_reference,
)
from src.market_morning.event_brief_generation import EventBriefGenerationSpec
from src.market_morning.global_runs import (
    GlobalEditionRunSpec,
    GlobalEditionSnapshotItem,
)
from src.market_morning.market_snapshots import MarketInstrument
from src.market_morning.model_usage import (
    ModelUsageReport,
    build_model_usage_event,
)
from src.market_morning.mysql_acceptance import validate_acceptance_database_url
from src.market_morning.repositories.delivery_webhooks import (
    DeliveryProviderEventOutcome,
    DeliveryProviderEventType,
    record_delivery_provider_event,
)
from src.market_morning.repositories.event_briefs import (
    EventBriefStartStatus,
    start_event_brief_generation,
)
from src.market_morning.repositories.global_runs import (
    GlobalRunPublishStatus,
    GlobalRunTerminalStatus,
    publish_global_edition_run,
    start_global_edition_run,
)
from src.market_morning.repositories.jobs import (
    JobEnqueueStatus,
    MarketMorningJobType,
    SchedulerLeaseStatus,
    acquire_scheduler_lease,
    claim_next_job,
    enqueue_job,
)
from src.market_morning.repositories.email_delivery import (
    DeliveryCreateStatus,
    create_delivery_attempt,
)
from src.market_morning.repositories.morning_edition import (
    EditionPublishStatus,
    publish_morning_edition,
)
from src.market_morning.repositories.manual_overrides import (
    OverrideMutationStatus,
    create_publication_halt,
)
from src.market_morning.repositories.model_usage import record_model_usage
from src.market_morning.repositories.source_ingestion import (
    build_cursor_upsert_statement,
    build_source_record_upsert_statement,
    record_source_failure,
)
from src.market_morning.pipeline.morning_edition import (
    EditionDayPlan,
    EditionDayStatus,
    EditionStatus,
    GenerationBudget,
    MorningEdition,
    OvernightMarketContext,
)


pytestmark = pytest.mark.integration


def _require_confirmed_acceptance_target() -> None:
    if os.environ.get("VIBE_MARKET_MORNING_MYSQL_ACCEPTANCE_CONFIRMED") != "true":
        pytest.skip("real MySQL acceptance write confirmation is not enabled")
    acceptance_url = os.environ.get(
        "VIBE_MARKET_MORNING_ACCEPTANCE_DATABASE_URL",
        "",
    )
    product_url = os.environ.get("VIBE_MARKET_MORNING_DATABASE_URL", "")
    acceptance_target = validate_acceptance_database_url(acceptance_url)
    product_target = validate_acceptance_database_url(product_url)
    if acceptance_target.target_fingerprint != product_target.target_fingerprint:
        pytest.fail("product database URL does not match the confirmed acceptance target")


def test_mysql_schema_and_session_contract() -> None:
    _require_confirmed_acceptance_target()
    issuer = "https://identity.acceptance.example/market-morning"
    subject = f"acceptance-subject-{uuid4()}"
    external_subject = pseudonymous_oidc_subject_reference(issuer, subject)
    first_token = f"mysql-acceptance-token-{uuid4()}"
    second_token = f"mysql-acceptance-token-{uuid4()}"

    async def verify():
        await reset_database_state()
        try:
            engine = get_engine()
            async with engine.connect() as connection:
                version, session_timezone, connection_charset = (
                    await connection.execute(text("SELECT @@version, @@session.time_zone, @@character_set_connection"))
                ).one()
                database_charset = (
                    await connection.execute(
                        text(
                            "SELECT DEFAULT_CHARACTER_SET_NAME "
                            "FROM information_schema.SCHEMATA "
                            "WHERE SCHEMA_NAME = DATABASE()"
                        )
                    )
                ).scalar_one()
                revision = (await connection.execute(text("SELECT version_num FROM alembic_version"))).scalar_one()
                table_names = (
                    (
                        await connection.execute(
                            text(
                                "SELECT TABLE_NAME FROM information_schema.TABLES "
                                "WHERE TABLE_SCHEMA = DATABASE() "
                                "AND TABLE_NAME LIKE 'mm\\_%' "
                                "ORDER BY TABLE_NAME"
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                database_contract = (
                    str(version),
                    str(session_timezone),
                    str(connection_charset),
                    str(database_charset),
                    str(revision),
                    frozenset(str(name) for name in table_names),
                )
            factory = get_session_factory()
            validator = ApplicationSessionValidator(
                claim_name=ACCESS_TOKEN_SESSION_REFERENCE,
                session_factory=factory,
            )
            now = int(datetime.now(timezone.utc).timestamp())
            claims = {
                "iss": issuer,
                "sub": subject,
                "iat": now - 5,
                "exp": now + 895,
            }
            first, second = await asyncio.gather(
                validator(first_token, claims),
                validator(first_token, claims),
            )
            independent_token = await validator(second_token, claims)
            async with factory() as session:
                rows = tuple(
                    (
                        await session.execute(
                            select(ApplicationSessionRecord).where(
                                ApplicationSessionRecord.external_subject == external_subject
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
            revoked = await validator.revoke_token(first_token, claims, reason="logout")
            rejected_after_revocation = not await validator(first_token, claims)
            independent_token_remains_active = await validator(second_token, claims)
            return (
                database_contract,
                first,
                second,
                independent_token,
                rows,
                revoked,
                rejected_after_revocation,
                independent_token_remains_active,
            )
        finally:
            factory = get_session_factory()
            async with factory.begin() as session:
                await session.execute(
                    delete(ApplicationSessionRecord).where(
                        ApplicationSessionRecord.external_subject == external_subject
                    )
                )
            await reset_database_state()

    (
        contract,
        first,
        second,
        independent_token,
        rows,
        revoked,
        rejected_after_revocation,
        independent_token_remains_active,
    ) = asyncio.run(verify())
    (
        version,
        session_timezone,
        connection_charset,
        database_charset,
        revision,
        table_names,
    ) = contract

    assert version.split(".", maxsplit=1)[0] == "8"
    assert "mariadb" not in version.lower()
    assert session_timezone == "+00:00"
    assert connection_charset == "utf8mb4"
    assert database_charset == "utf8mb4"
    assert revision == EXPECTED_MARKET_MORNING_SCHEMA_REVISION
    assert table_names == frozenset(Base.metadata.tables)
    assert first is True and second is True
    assert independent_token is True
    assert len(rows) == 2
    assert {row.issuer_sha256 for row in rows} == {
        hashlib.sha256(issuer.encode("utf-8")).hexdigest()
    }
    assert {row.session_reference_sha256 for row in rows} == {
        hashlib.sha256(first_token.encode("utf-8")).hexdigest(),
        hashlib.sha256(second_token.encode("utf-8")).hexdigest(),
    }
    serialized_rows = repr(tuple(row.__dict__ for row in rows))
    assert issuer not in serialized_rows
    assert subject not in serialized_rows
    assert first_token not in serialized_rows
    assert second_token not in serialized_rows
    assert revoked is True
    assert rejected_after_revocation is True
    assert independent_token_remains_active is True


def test_mysql_job_enqueue_is_concurrent_and_idempotent() -> None:
    _require_confirmed_acceptance_target()
    idempotency_key = f"mysql-acceptance:job:{uuid4()}"
    now = datetime.now(timezone.utc)

    async def verify():
        await reset_database_state()
        factory = get_session_factory()

        async def enqueue_once():
            async with factory.begin() as session:
                return await enqueue_job(
                    session,
                    job_type=MarketMorningJobType.SOURCE_INGESTION,
                    idempotency_key=idempotency_key,
                    payload={
                        "schema_version": 1,
                        "acceptance_probe": True,
                        "probe_id": idempotency_key,
                    },
                    priority=1,
                    available_at=now,
                    max_attempts=2,
                    created_at=now,
                )

        try:
            first, second = await asyncio.gather(enqueue_once(), enqueue_once())
            async with factory() as session:
                row_count = int(
                    (
                        await session.execute(
                            select(func.count())
                            .select_from(JobRecord)
                            .where(JobRecord.idempotency_key == idempotency_key)
                        )
                    ).scalar_one()
                )
            return first, second, row_count
        finally:
            async with factory.begin() as session:
                await session.execute(delete(JobRecord).where(JobRecord.idempotency_key == idempotency_key))
            await reset_database_state()

    first, second, row_count = asyncio.run(verify())

    assert {first.status, second.status} == {
        JobEnqueueStatus.ENQUEUED,
        JobEnqueueStatus.ALREADY_ENQUEUED,
    }
    assert first.job_id == second.job_id
    assert row_count == 1


def _acceptance_edition_date(probe_id: str) -> date:
    value = int(probe_id.replace("-", "")[:8], 16)
    return date(
        2100 + value % 100,
        1 + (value // 100) % 12,
        1 + (value // 1_200) % 28,
    )


def test_mysql_delivery_attempt_is_concurrent_and_idempotent() -> None:
    _require_confirmed_acceptance_target()
    probe_id = str(uuid4())
    user_id = str(uuid4())
    run_id = str(uuid4())
    edition_date = _acceptance_edition_date(probe_id)
    now = datetime.now(timezone.utc)
    now_db = now.replace(tzinfo=None)

    async def verify():
        await reset_database_state()
        factory = get_session_factory()
        async with factory.begin() as session:
            session.add_all(
                [
                    User(
                        user_id=user_id,
                        external_subject=f"mysql-acceptance:{probe_id}",
                        timezone="Asia/Tokyo",
                        email_opt_in=True,
                        account_status="active",
                        trial_or_subscription_status="private_beta",
                        last_product_activity_at=now_db,
                        created_at=now_db,
                        updated_at=now_db,
                        deleted_at=None,
                    ),
                    GlobalEditionDayRecord(
                        edition_date=edition_date,
                        created_at=now_db,
                        updated_at=now_db,
                    ),
                    GlobalEditionRunRecord(
                        run_id=run_id,
                        edition_date=edition_date,
                        run_version=1,
                        generation_key=f"mysql-acceptance:{probe_id}",
                        attempt_key="mysql-accept",
                        scenario="standard",
                        status="complete",
                        is_current=True,
                        email_permitted=True,
                        late=False,
                        reason_code=None,
                        spec={"acceptance_probe": True},
                        spec_sha256="a" * 64,
                        manifest={"acceptance_probe": True},
                        manifest_sha256="b" * 64,
                        started_at=now_db,
                        completed_at=now_db,
                        created_at=now_db,
                        updated_at=now_db,
                    ),
                ]
            )

        async def create_once():
            async with factory.begin() as session:
                return await create_delivery_attempt(
                    session,
                    user_id=user_id,
                    global_run_id=run_id,
                    edition_date=edition_date,
                    deep_link_token_sha256="c" * 64,
                    max_attempts=3,
                    requested_at=now,
                )

        try:
            first, second = await asyncio.gather(create_once(), create_once())
            async with factory() as session:
                row_count = int(
                    (
                        await session.execute(
                            select(func.count())
                            .select_from(DeliveryAttemptRecord)
                            .where(DeliveryAttemptRecord.user_id == user_id)
                        )
                    ).scalar_one()
                )
            return first, second, row_count
        finally:
            async with factory.begin() as session:
                await session.execute(delete(DeliveryAttemptRecord).where(DeliveryAttemptRecord.user_id == user_id))
                await session.execute(delete(GlobalEditionRunRecord).where(GlobalEditionRunRecord.run_id == run_id))
                await session.execute(
                    delete(GlobalEditionDayRecord).where(GlobalEditionDayRecord.edition_date == edition_date)
                )
                await session.execute(delete(User).where(User.user_id == user_id))
            await reset_database_state()

    first, second, row_count = asyncio.run(verify())

    assert {first.status, second.status} == {
        DeliveryCreateStatus.CREATED,
        DeliveryCreateStatus.ALREADY_CREATED,
    }
    assert first.delivery_attempt_id == second.delivery_attempt_id
    assert row_count == 1


def test_mysql_account_deletion_executes_atomically_and_probe_rolls_back() -> None:
    _require_confirmed_acceptance_target()
    probe_id = str(uuid4())
    user_id = str(uuid4())
    request_id = str(uuid4())
    consent_id = str(uuid4())
    original_subject = f"mysql-acceptance:{probe_id}"
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    async def verify():
        await reset_database_state()
        factory = get_session_factory()
        async with factory() as session:
            transaction = await session.begin()
            try:
                user = User(
                    user_id=user_id,
                    external_subject=original_subject,
                    timezone="Asia/Tokyo",
                    email_opt_in=True,
                    account_status="deletion_pending",
                    trial_or_subscription_status="private_beta",
                    last_product_activity_at=None,
                    created_at=now,
                    updated_at=now,
                    deleted_at=None,
                )
                request = AccountDeletionRequest(
                    request_id=request_id,
                    user_id=user_id,
                    status="pending",
                    requested_at=now,
                    processing_started_at=None,
                    completed_at=None,
                    updated_at=now,
                )
                session.add(user)
                await session.flush()
                session.add_all(
                    [
                        request,
                        UserConsent(
                            consent_id=consent_id,
                            user_id=user_id,
                            consent_type="risk_disclosure",
                            consent_version="mysql-acceptance-v1",
                            accepted_at=now,
                            revoked_at=None,
                        ),
                    ]
                )
                await session.flush()

                result = await process_account_deletion(
                    session,
                    request_id=request_id,
                    actor_reference="mysql-acceptance-probe",
                    now=now,
                )
                consent_count = int(
                    (
                        await session.execute(
                            select(func.count()).select_from(UserConsent).where(UserConsent.user_id == user_id)
                        )
                    ).scalar_one()
                )
                audit_count = int(
                    (
                        await session.execute(
                            select(func.count())
                            .select_from(AuditLog)
                            .where(
                                AuditLog.entity_type == "account_deletion_request",
                                AuditLog.entity_id == request_id,
                                AuditLog.action == "account_deletion.completed",
                            )
                        )
                    ).scalar_one()
                )
                observed = (
                    result.status,
                    request.status,
                    user.account_status,
                    user.external_subject,
                    consent_count,
                    audit_count,
                )
            finally:
                await transaction.rollback()

        async with factory() as session:
            remaining = int(
                (
                    await session.execute(select(func.count()).select_from(User).where(User.user_id == user_id))
                ).scalar_one()
            )
        await reset_database_state()
        return observed, remaining

    observed, remaining = asyncio.run(verify())
    (
        result_status,
        request_status,
        account_status,
        pseudonymized_subject,
        consent_count,
        audit_count,
    ) = observed

    assert result_status == AccountDeletionProcessStatus.COMPLETED
    assert request_status == "completed"
    assert account_status == "deleted"
    assert pseudonymized_subject.startswith("deleted:")
    assert original_subject not in pseudonymized_subject
    assert consent_count == 0
    assert audit_count == 1
    assert remaining == 0


def test_mysql_source_revision_chain_and_cursor_are_durable() -> None:
    _require_confirmed_acceptance_target()
    probe_id = uuid4().hex
    provider = f"mysql_acceptance_{probe_id[:12]}"
    document_id = f"DOC-{probe_id[:16]}"
    now = datetime.now(timezone.utc)
    now_db = now.replace(tzinfo=None)

    def source_values(
        *,
        source_record_id: str,
        revision: str,
        lifecycle_status: str,
        supersedes_record_id: str | None,
    ) -> dict:
        return {
            "source_record_id": source_record_id,
            "source_provider": provider,
            "provider_document_id": document_id,
            "provider_revision_key": revision,
            "original_url": (f"https://example.invalid/{document_id}/{revision}.pdf"),
            "title": f"acceptance disclosure {revision}",
            "document_type": "timely_disclosure",
            "published_at": now_db,
            "fetched_at": now_db,
            "lifecycle_status": lifecycle_status,
            "content_hash_sha256": revision[-1] * 64,
            "normalized_payload": {
                "issuer_codes": [],
                "metadata": {"acceptance_probe": True},
            },
            "supersedes_record_id": supersedes_record_id,
            "created_at": now_db,
        }

    async def verify():
        await reset_database_state()
        factory = get_session_factory()

        async def insert_revision(values: dict) -> None:
            async with factory.begin() as session:
                await session.execute(build_source_record_upsert_statement(values))

        try:
            first_id = str(uuid4())
            second_id = str(uuid4())
            await asyncio.gather(
                insert_revision(
                    source_values(
                        source_record_id=first_id,
                        revision="v1",
                        lifecycle_status="active",
                        supersedes_record_id=None,
                    )
                ),
                insert_revision(
                    source_values(
                        source_record_id=second_id,
                        revision="v1",
                        lifecycle_status="active",
                        supersedes_record_id=None,
                    )
                ),
            )
            async with factory() as session:
                durable_v1 = (
                    await session.execute(
                        select(SourceRecord).where(
                            SourceRecord.source_provider == provider,
                            SourceRecord.provider_document_id == document_id,
                            SourceRecord.provider_revision_key == "v1",
                        )
                    )
                ).scalar_one()

            v2_id = str(uuid4())
            v3_id = str(uuid4())
            async with factory.begin() as session:
                await session.execute(
                    build_source_record_upsert_statement(
                        source_values(
                            source_record_id=v2_id,
                            revision="v2",
                            lifecycle_status="corrected",
                            supersedes_record_id=durable_v1.source_record_id,
                        )
                    )
                )
                await session.execute(
                    build_source_record_upsert_statement(
                        source_values(
                            source_record_id=v3_id,
                            revision="v3",
                            lifecycle_status="withdrawn",
                            supersedes_record_id=v2_id,
                        )
                    )
                )
                await session.execute(
                    build_cursor_upsert_statement(
                        provider=provider,
                        cursor={"sequence": 3},
                        completed_at=now,
                    )
                )
            async with factory.begin() as session:
                await record_source_failure(
                    session,
                    provider=provider,
                    failed_at=now + timedelta(minutes=1),
                    error_code="acceptance_followup_failed",
                )

            async with factory() as session:
                revisions = (
                    (
                        await session.execute(
                            select(SourceRecord)
                            .where(SourceRecord.source_provider == provider)
                            .order_by(SourceRecord.provider_revision_key)
                        )
                    )
                    .scalars()
                    .all()
                )
                cursor = (
                    await session.execute(select(SourceCursor).where(SourceCursor.source_provider == provider))
                ).scalar_one()
                observed = (
                    tuple(
                        (
                            row.provider_revision_key,
                            row.lifecycle_status,
                            row.source_record_id,
                            row.supersedes_record_id,
                        )
                        for row in revisions
                    ),
                    dict(cursor.cursor_value),
                    cursor.last_error_code,
                )
            return observed
        finally:
            async with factory.begin() as session:
                await session.execute(
                    update(SourceRecord)
                    .where(SourceRecord.source_provider == provider)
                    .values(supersedes_record_id=None)
                )
                await session.execute(delete(SourceRecord).where(SourceRecord.source_provider == provider))
                await session.execute(delete(SourceCursor).where(SourceCursor.source_provider == provider))
            await reset_database_state()

    revisions, cursor_value, last_error_code = asyncio.run(verify())

    assert len(revisions) == 3
    assert [(row[0], row[1]) for row in revisions] == [
        ("v1", "active"),
        ("v2", "corrected"),
        ("v3", "withdrawn"),
    ]
    assert revisions[1][3] == revisions[0][2]
    assert revisions[2][3] == revisions[1][2]
    assert cursor_value == {"sequence": 3}
    assert last_error_code == "acceptance_followup_failed"


def _minimal_acceptance_edition(
    *,
    edition_date: date,
    generated_at: datetime,
) -> MorningEdition:
    return MorningEdition(
        edition_date=edition_date,
        generated_at=generated_at,
        status=EditionStatus.NO_DATA,
        day_plan=EditionDayPlan(
            edition_date=edition_date,
            generate=True,
            status=EditionDayStatus.SCHEDULED,
            overnight_context=OvernightMarketContext.US_SESSION_AVAILABLE,
            reason_code=None,
            us_reason_code=None,
        ),
        issuers=(),
        consumed_event_units=0,
        budget=GenerationBudget(),
        budget_exhausted=False,
        omitted_issuer_count=0,
    )


def test_mysql_morning_edition_generation_and_revision_chain() -> None:
    _require_confirmed_acceptance_target()
    probe_id = str(uuid4())
    user_id = str(uuid4())
    edition_date = _acceptance_edition_date(probe_id)
    now = datetime.now(timezone.utc)
    now_db = now.replace(tzinfo=None)
    generation_key = f"mysql-acceptance:{probe_id}:v1"
    edition = _minimal_acceptance_edition(
        edition_date=edition_date,
        generated_at=now,
    )

    async def verify():
        await reset_database_state()
        factory = get_session_factory()
        async with factory.begin() as session:
            session.add(
                User(
                    user_id=user_id,
                    external_subject=f"mysql-acceptance:{probe_id}",
                    timezone="Asia/Tokyo",
                    email_opt_in=False,
                    account_status="active",
                    trial_or_subscription_status="private_beta",
                    last_product_activity_at=now_db,
                    created_at=now_db,
                    updated_at=now_db,
                    deleted_at=None,
                )
            )

        async def publish_once(key: str, published_at: datetime):
            async with factory.begin() as session:
                return await publish_morning_edition(
                    session,
                    user_id=user_id,
                    generation_key=key,
                    edition=edition,
                    published_at=published_at,
                )

        try:
            first, duplicate = await asyncio.gather(
                publish_once(generation_key, now),
                publish_once(generation_key, now),
            )
            second_version = await publish_once(
                f"mysql-acceptance:{probe_id}:v2",
                now + timedelta(seconds=1),
            )
            async with factory() as session:
                rows = (
                    (
                        await session.execute(
                            select(MorningEditionRecord)
                            .where(MorningEditionRecord.user_id == user_id)
                            .order_by(MorningEditionRecord.edition_version)
                        )
                    )
                    .scalars()
                    .all()
                )
                chain = tuple(
                    (
                        row.edition_id,
                        row.edition_version,
                        row.supersedes_edition_id,
                    )
                    for row in rows
                )
            return first, duplicate, second_version, chain
        finally:
            async with factory.begin() as session:
                await session.execute(
                    update(MorningEditionRecord)
                    .where(MorningEditionRecord.user_id == user_id)
                    .values(supersedes_edition_id=None)
                )
                await session.execute(delete(MorningEditionRecord).where(MorningEditionRecord.user_id == user_id))
                await session.execute(delete(User).where(User.user_id == user_id))
            await reset_database_state()

    first, duplicate, second_version, chain = asyncio.run(verify())

    assert {first.status, duplicate.status} == {
        EditionPublishStatus.PUBLISHED,
        EditionPublishStatus.ALREADY_PUBLISHED,
    }
    assert first.edition_id == duplicate.edition_id
    assert first.edition_version == duplicate.edition_version == 1
    assert second_version.edition_version == 2
    assert len(chain) == 2
    assert chain[0][1:] == (1, None)
    assert chain[1][1:] == (2, chain[0][0])


def test_mysql_workers_claim_distinct_jobs_with_skip_locked() -> None:
    _require_confirmed_acceptance_target()
    probe_id = uuid4().hex
    keys = (
        f"mysql-acceptance:claim:{probe_id}:1",
        f"mysql-acceptance:claim:{probe_id}:2",
    )
    now = datetime.now(timezone.utc)

    async def verify():
        await reset_database_state()
        factory = get_session_factory()
        async with factory.begin() as session:
            for index, key in enumerate(keys):
                await enqueue_job(
                    session,
                    job_type=MarketMorningJobType.SOURCE_INGESTION,
                    idempotency_key=key,
                    payload={"schema_version": 1, "probe": probe_id, "index": index},
                    priority=10 - index,
                    available_at=now,
                    max_attempts=2,
                    created_at=now + timedelta(microseconds=index),
                )

        both_claimed = asyncio.Event()
        counter_lock = asyncio.Lock()
        claimed_count = 0

        async def claim(worker_id: str):
            nonlocal claimed_count
            async with factory.begin() as session:
                claimed = await claim_next_job(
                    session,
                    worker_id=worker_id,
                    now=now + timedelta(seconds=1),
                    lease_duration=timedelta(seconds=30),
                )
                async with counter_lock:
                    claimed_count += 1
                    if claimed_count == 2:
                        both_claimed.set()
                await asyncio.wait_for(both_claimed.wait(), timeout=5)
                return claimed

        try:
            first, second = await asyncio.gather(
                claim(f"mysql-acceptance-worker-{probe_id[:8]}-1"),
                claim(f"mysql-acceptance-worker-{probe_id[:8]}-2"),
            )
            async with factory() as session:
                attempt_count = int(
                    (
                        await session.execute(
                            select(func.count())
                            .select_from(JobAttemptRecord)
                            .join(JobRecord, JobRecord.job_id == JobAttemptRecord.job_id)
                            .where(JobRecord.idempotency_key.in_(keys))
                        )
                    ).scalar_one()
                )
            return first, second, attempt_count
        finally:
            async with factory.begin() as session:
                await session.execute(delete(JobRecord).where(JobRecord.idempotency_key.in_(keys)))
            await reset_database_state()

    first, second, attempt_count = asyncio.run(verify())

    assert first is not None and second is not None
    assert first.job_id != second.job_id
    assert first.attempt_id != second.attempt_id
    assert attempt_count == 2


def test_mysql_scheduler_lease_is_single_owner_and_recoverable() -> None:
    _require_confirmed_acceptance_target()
    probe_id = uuid4().hex
    lease_name = f"mysql-acceptance-scheduler-{probe_id}"
    owners = (
        f"mysql-acceptance-owner-{probe_id[:8]}-1",
        f"mysql-acceptance-owner-{probe_id[:8]}-2",
    )
    now = datetime.now(timezone.utc)

    async def verify():
        await reset_database_state()
        factory = get_session_factory()

        async def acquire(owner_id: str, at: datetime):
            async with factory.begin() as session:
                return await acquire_scheduler_lease(
                    session,
                    lease_name=lease_name,
                    owner_id=owner_id,
                    now=at,
                    lease_duration=timedelta(seconds=30),
                )

        try:
            first_status, second_status = await asyncio.gather(
                acquire(owners[0], now),
                acquire(owners[1], now),
            )
            winner = owners[0] if first_status is SchedulerLeaseStatus.ACQUIRED else owners[1]
            loser = owners[1] if winner == owners[0] else owners[0]
            renewed = await acquire(winner, now + timedelta(seconds=1))
            still_busy = await acquire(loser, now + timedelta(seconds=2))
            taken_over = await acquire(loser, now + timedelta(seconds=32))
            async with factory() as session:
                final_owner = (
                    await session.execute(
                        select(SchedulerLeaseRecord.owner_id).where(SchedulerLeaseRecord.lease_name == lease_name)
                    )
                ).scalar_one()
            return (
                first_status,
                second_status,
                renewed,
                still_busy,
                taken_over,
                final_owner,
                loser,
            )
        finally:
            async with factory.begin() as session:
                await session.execute(delete(SchedulerLeaseRecord).where(SchedulerLeaseRecord.lease_name == lease_name))
            await reset_database_state()

    (
        first_status,
        second_status,
        renewed,
        still_busy,
        taken_over,
        final_owner,
        loser,
    ) = asyncio.run(verify())

    assert {first_status, second_status} == {
        SchedulerLeaseStatus.ACQUIRED,
        SchedulerLeaseStatus.BUSY,
    }
    assert renewed is SchedulerLeaseStatus.RENEWED
    assert still_busy is SchedulerLeaseStatus.BUSY
    assert taken_over is SchedulerLeaseStatus.ACQUIRED
    assert final_owner == loser


def test_mysql_publication_halt_is_concurrent_and_audited_once() -> None:
    _require_confirmed_acceptance_target()
    probe_id = str(uuid4())
    edition_date = _acceptance_edition_date(probe_id)
    now = datetime.now(timezone.utc)

    async def verify():
        await reset_database_state()
        factory = get_session_factory()

        async def create_once(actor_reference: str):
            async with factory.begin() as session:
                return await create_publication_halt(
                    session,
                    edition_date=edition_date,
                    reason_code="mysql_acceptance_halt",
                    actor_reference=actor_reference,
                    now=now,
                )

        try:
            first, second = await asyncio.wait_for(
                asyncio.gather(
                    create_once(f"mysql-acceptance-{probe_id}-1"),
                    create_once(f"mysql-acceptance-{probe_id}-2"),
                ),
                timeout=15,
            )
            async with factory() as session:
                overrides = (
                    (
                        await session.execute(
                            select(ManualOverrideRecord).where(
                                ManualOverrideRecord.edition_date == edition_date,
                                ManualOverrideRecord.override_type == "publication_halt",
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                override_ids = tuple(row.override_id for row in overrides)
                created_audits = int(
                    (
                        await session.execute(
                            select(func.count())
                            .select_from(AuditLog)
                            .where(
                                AuditLog.entity_type == "manual_override",
                                AuditLog.entity_id.in_(override_ids),
                                AuditLog.action == "publication_halt.created",
                            )
                        )
                    ).scalar_one()
                )
            return first, second, overrides, created_audits
        finally:
            async with factory.begin() as session:
                override_ids = tuple(
                    (
                        await session.execute(
                            select(ManualOverrideRecord.override_id).where(
                                ManualOverrideRecord.edition_date == edition_date,
                                ManualOverrideRecord.override_type == "publication_halt",
                            )
                        )
                    ).scalars()
                )
                if override_ids:
                    await session.execute(
                        delete(AuditLog).where(
                            AuditLog.entity_type == "manual_override",
                            AuditLog.entity_id.in_(override_ids),
                        )
                    )
                await session.execute(
                    delete(ManualOverrideRecord).where(
                        ManualOverrideRecord.edition_date == edition_date,
                        ManualOverrideRecord.override_type == "publication_halt",
                    )
                )
            await reset_database_state()

    first, second, overrides, created_audits = asyncio.run(verify())

    assert {first.status, second.status} == {
        OverrideMutationStatus.CREATED,
        OverrideMutationStatus.ALREADY_ACTIVE,
    }
    assert first.override is not None and second.override is not None
    assert first.override.override_id == second.override.override_id
    assert len(overrides) == 1
    assert overrides[0].status == "active"
    assert overrides[0].active_edition_date == edition_date
    assert created_audits == 1


def test_mysql_global_runs_publish_one_current_success() -> None:
    _require_confirmed_acceptance_target()
    probe_id = str(uuid4())
    edition_date = _acceptance_edition_date(probe_id)
    now = datetime.now(timezone.utc)
    now_db = now.replace(tzinfo=None)
    provider = f"mysql_acceptance_{probe_id.replace('-', '')[:12]}"
    snapshot_ids = tuple(str(uuid4()) for _ in MarketInstrument)
    items = tuple(
        GlobalEditionSnapshotItem(snapshot_id, instrument)
        for snapshot_id, instrument in zip(
            snapshot_ids,
            MarketInstrument,
            strict=True,
        )
    )
    specs = tuple(
        GlobalEditionRunSpec(
            edition_date=edition_date,
            generation_key=(f"global-edition-run:{edition_date.isoformat()}:{attempt_key}"),
            attempt_key=attempt_key,
            scenario="a_standard",
            email_permitted=True,
            late=False,
            reason_code=None,
            started_at=now + timedelta(seconds=index),
        )
        for index, attempt_key in enumerate(("0700", "0715"))
    )

    async def verify():
        await reset_database_state()
        factory = get_session_factory()
        async with factory.begin() as session:
            session.add_all(
                [
                    MarketSnapshotRecord(
                        snapshot_id=snapshot_id,
                        instrument=instrument.value,
                        provider=provider,
                        session_date=edition_date,
                        as_of=now_db + timedelta(microseconds=index),
                        value=Decimal(100 + index),
                        previous_close=Decimal(99 + index),
                        currency=("JPY" if instrument is MarketInstrument.NIKKEI_225 else "USD"),
                        delay_status="eod",
                        fetched_at=now_db + timedelta(microseconds=index),
                        snapshot_sha256=f"{index + 1:x}" * 64,
                        created_at=now_db,
                    )
                    for index, (snapshot_id, instrument) in enumerate(zip(snapshot_ids, MarketInstrument, strict=True))
                ]
            )

        starts = []
        for spec in specs:
            async with factory.begin() as session:
                starts.append(await start_global_edition_run(session, spec=spec))

        async def publish_once(index: int):
            async with factory.begin() as session:
                return await publish_global_edition_run(
                    session,
                    run_id=starts[index].run_id,
                    spec=specs[index],
                    snapshot_items=items,
                    publication_status=GlobalRunTerminalStatus.COMPLETE,
                    completed_at=now + timedelta(minutes=1, seconds=index),
                )

        try:
            publications = await asyncio.wait_for(
                asyncio.gather(publish_once(0), publish_once(1)),
                timeout=15,
            )
            async with factory() as session:
                rows = (
                    (
                        await session.execute(
                            select(GlobalEditionRunRecord)
                            .where(GlobalEditionRunRecord.edition_date == edition_date)
                            .order_by(GlobalEditionRunRecord.run_version)
                        )
                    )
                    .scalars()
                    .all()
                )
            return tuple(starts), publications, rows
        finally:
            async with factory.begin() as session:
                await session.execute(
                    delete(GlobalEditionRunRecord).where(GlobalEditionRunRecord.edition_date == edition_date)
                )
                await session.execute(
                    delete(GlobalEditionDayRecord).where(GlobalEditionDayRecord.edition_date == edition_date)
                )
                await session.execute(
                    delete(MarketSnapshotRecord).where(MarketSnapshotRecord.snapshot_id.in_(snapshot_ids))
                )
            await reset_database_state()

    starts, publications, rows = asyncio.run(verify())

    assert len({start.run_id for start in starts}) == 2
    assert all(publication.status is GlobalRunPublishStatus.PUBLISHED for publication in publications)
    assert len(rows) == 2
    assert {row.status for row in rows} == {"complete"}
    current = [row for row in rows if row.is_current]
    assert len(current) == 1
    assert current[0].current_success_date == edition_date
    assert [row.current_success_date for row in rows].count(None) == 1


def test_mysql_event_brief_and_model_usage_are_idempotent() -> None:
    _require_confirmed_acceptance_target()
    probe_id = uuid4().hex
    snapshot_id = str(uuid4())
    issuer_id = str(uuid4())
    event_id = str(uuid4())
    edition_date = _acceptance_edition_date(probe_id)
    now = datetime.now(timezone.utc)
    now_db = now.replace(tzinfo=None)
    model_version = "mysql-acceptance-model-v1"
    prompt_version = "mysql-acceptance-prompt-v1"
    spec = EventBriefGenerationSpec(
        event_id=event_id,
        event_version=1,
        generation_key=(f"event-brief:{event_id}:v1:{model_version}:{prompt_version}:s1"),
        model_version=model_version,
        prompt_version=prompt_version,
        started_at=now,
    )

    async def verify():
        await reset_database_state()
        factory = get_session_factory()
        async with factory.begin() as session:
            session.add(
                IssuerSnapshot(
                    snapshot_id=snapshot_id,
                    source_provider=f"mysql_acceptance_{probe_id[:12]}",
                    source_version=probe_id,
                    source_url="https://example.invalid/issuer-master.csv",
                    snapshot_date=edition_date,
                    checksum_sha256="a" * 64,
                    import_status="complete",
                    fetched_at=now_db,
                    imported_at=now_db,
                )
            )
            await session.flush()
            session.add(
                Issuer(
                    issuer_id=issuer_id,
                    issuer_code=f"A{probe_id[:10]}",
                    legal_name_ja="MySQL 受入試験株式会社",
                    normalized_search_key=f"mysqlacceptance{probe_id[:8]}",
                    market_segment="prime",
                    source_snapshot_id=snapshot_id,
                    effective_from=edition_date,
                    effective_to=None,
                    active_status="active",
                    created_at=now_db,
                )
            )
            await session.flush()
            session.add(
                NormalizedEvent(
                    event_id=event_id,
                    event_family_key=f"mysql-acceptance:{probe_id}",
                    event_version=1,
                    issuer_id=issuer_id,
                    title="MySQL acceptance event",
                    normalized_title=f"mysqlacceptanceevent{probe_id[:8]}",
                    event_type="timely_disclosure",
                    occurred_at=now_db,
                    lifecycle_status="active",
                    supersedes_event_id=None,
                    created_at=now_db,
                )
            )
            await session.flush()

        async def start_once():
            async with factory.begin() as session:
                return await start_event_brief_generation(session, spec=spec)

        try:
            first, second = await asyncio.wait_for(
                asyncio.gather(start_once(), start_once()),
                timeout=15,
            )
            usage = build_model_usage_event(
                brief_id=first.brief_id,
                attempt_number=1,
                report=ModelUsageReport(
                    provider="mysql-acceptance",
                    model=model_version,
                    input_tokens=21,
                    output_tokens=8,
                    billable_cost_micros=37,
                    currency="JPY",
                    provider_request_id=f"private-request-{probe_id}",
                ),
                occurred_at=now + timedelta(seconds=1),
            )

            async def record_usage_once():
                async with factory.begin() as session:
                    return await record_model_usage(session, event=usage)

            usage_results = await asyncio.wait_for(
                asyncio.gather(record_usage_once(), record_usage_once()),
                timeout=15,
            )
            async with factory() as session:
                brief = (
                    await session.execute(select(EventBriefRecord).where(EventBriefRecord.event_id == event_id))
                ).scalar_one()
                usage_rows = (
                    (
                        await session.execute(
                            select(ModelUsageEventRecord).where(ModelUsageEventRecord.brief_id == brief.brief_id)
                        )
                    )
                    .scalars()
                    .all()
                )
            return first, second, usage_results, brief, usage_rows
        finally:
            async with factory.begin() as session:
                await session.execute(
                    delete(ModelUsageEventRecord).where(
                        ModelUsageEventRecord.brief_id.in_(
                            select(EventBriefRecord.brief_id).where(EventBriefRecord.event_id == event_id)
                        )
                    )
                )
                await session.execute(delete(EventBriefRecord).where(EventBriefRecord.event_id == event_id))
                await session.execute(delete(NormalizedEvent).where(NormalizedEvent.event_id == event_id))
                await session.execute(delete(Issuer).where(Issuer.issuer_id == issuer_id))
                await session.execute(delete(IssuerSnapshot).where(IssuerSnapshot.snapshot_id == snapshot_id))
            await reset_database_state()

    first, second, usage_results, brief, usage_rows = asyncio.run(verify())

    assert {first.status, second.status} == {
        EventBriefStartStatus.STARTED,
        EventBriefStartStatus.ALREADY_RUNNING,
    }
    assert first.brief_id == second.brief_id == brief.brief_id
    assert first.attempt_number == second.attempt_number == brief.attempt_count == 1
    assert len({result.usage_event_id for result in usage_results}) == 1
    assert len(usage_rows) == 1
    assert usage_rows[0].usage_event_id == usage_results[0].usage_event_id
    assert usage_rows[0].provider_request_id_sha256 is not None
    assert len(usage_rows[0].provider_request_id_sha256) == 64


def test_mysql_email_webhook_replay_and_out_of_order_events_are_monotonic() -> None:
    _require_confirmed_acceptance_target()
    probe_id = str(uuid4())
    user_id = str(uuid4())
    run_id = str(uuid4())
    attempt_id = str(uuid4())
    edition_date = _acceptance_edition_date(probe_id)
    provider = "mysql-acceptance"
    provider_message_id = f"mysql-acceptance-message-{probe_id}"
    clicked_event_id = f"mysql-acceptance-clicked-{probe_id}"
    delivered_event_id = f"mysql-acceptance-delivered-{probe_id}"
    now = datetime.now(timezone.utc)
    now_db = now.replace(tzinfo=None)

    async def verify():
        await reset_database_state()
        factory = get_session_factory()
        async with factory.begin() as session:
            session.add_all(
                [
                    User(
                        user_id=user_id,
                        external_subject=f"mysql-acceptance:{probe_id}",
                        timezone="Asia/Tokyo",
                        email_opt_in=True,
                        account_status="active",
                        trial_or_subscription_status="private_beta",
                        last_product_activity_at=now_db,
                        created_at=now_db,
                        updated_at=now_db,
                        deleted_at=None,
                    ),
                    GlobalEditionDayRecord(
                        edition_date=edition_date,
                        created_at=now_db,
                        updated_at=now_db,
                    ),
                ]
            )
            await session.flush()
            session.add(
                GlobalEditionRunRecord(
                    run_id=run_id,
                    edition_date=edition_date,
                    run_version=1,
                    generation_key=f"mysql-acceptance:webhook:{probe_id}",
                    attempt_key="mysql-accept",
                    scenario="standard",
                    status="complete",
                    is_current=True,
                    email_permitted=True,
                    late=False,
                    reason_code=None,
                    spec={"acceptance_probe": True},
                    spec_sha256="d" * 64,
                    manifest={"acceptance_probe": True},
                    manifest_sha256="e" * 64,
                    started_at=now_db,
                    completed_at=now_db,
                    created_at=now_db,
                    updated_at=now_db,
                )
            )
            await session.flush()
            session.add(
                DeliveryAttemptRecord(
                    delivery_attempt_id=attempt_id,
                    user_id=user_id,
                    global_run_id=run_id,
                    edition_date=edition_date,
                    channel="email",
                    idempotency_key=f"mysql-acceptance:webhook:{probe_id}",
                    status="sent",
                    provider_message_id=provider_message_id,
                    deep_link_token_sha256="f" * 64,
                    attempt_count=1,
                    max_attempts=3,
                    last_error_code=None,
                    requested_at=now_db,
                    sent_at=now_db,
                    delivered_at=None,
                    clicked_at=None,
                    failed_at=None,
                    updated_at=now_db,
                )
            )
            await session.flush()

        async def record_event(
            *,
            provider_event_id: str,
            event_type: DeliveryProviderEventType,
            payload_sha256: str,
            received_at: datetime,
        ):
            async with factory.begin() as session:
                return await record_delivery_provider_event(
                    session,
                    provider=provider,
                    provider_event_id=provider_event_id,
                    provider_message_id=provider_message_id,
                    event_type=event_type,
                    payload_sha256=payload_sha256,
                    received_at=received_at,
                )

        try:
            clicked = await record_event(
                provider_event_id=clicked_event_id,
                event_type=DeliveryProviderEventType.CLICKED,
                payload_sha256="1" * 64,
                received_at=now + timedelta(seconds=2),
            )
            delivered = await record_event(
                provider_event_id=delivered_event_id,
                event_type=DeliveryProviderEventType.DELIVERED,
                payload_sha256="2" * 64,
                received_at=now + timedelta(seconds=1),
            )
            duplicate = await record_event(
                provider_event_id=clicked_event_id,
                event_type=DeliveryProviderEventType.CLICKED,
                payload_sha256="1" * 64,
                received_at=now + timedelta(seconds=2),
            )
            async with factory() as session:
                delivery = (
                    await session.execute(
                        select(DeliveryAttemptRecord).where(DeliveryAttemptRecord.delivery_attempt_id == attempt_id)
                    )
                ).scalar_one()
                outcomes = dict(
                    (
                        await session.execute(
                            select(
                                DeliveryProviderEventRecord.provider_event_id,
                                DeliveryProviderEventRecord.outcome,
                            ).where(
                                DeliveryProviderEventRecord.provider == provider,
                                DeliveryProviderEventRecord.provider_message_id == provider_message_id,
                            )
                        )
                    ).all()
                )
            return clicked, delivered, duplicate, delivery, outcomes
        finally:
            async with factory.begin() as session:
                await session.execute(
                    delete(DeliveryProviderEventRecord).where(
                        DeliveryProviderEventRecord.provider == provider,
                        DeliveryProviderEventRecord.provider_message_id == provider_message_id,
                    )
                )
                await session.execute(
                    delete(DeliveryAttemptRecord).where(DeliveryAttemptRecord.delivery_attempt_id == attempt_id)
                )
                await session.execute(delete(GlobalEditionRunRecord).where(GlobalEditionRunRecord.run_id == run_id))
                await session.execute(
                    delete(GlobalEditionDayRecord).where(GlobalEditionDayRecord.edition_date == edition_date)
                )
                await session.execute(delete(User).where(User.user_id == user_id))
            await reset_database_state()

    clicked, delivered, duplicate, delivery, outcomes = asyncio.run(verify())

    assert clicked.outcome is DeliveryProviderEventOutcome.APPLIED
    assert clicked.delivery_status is not None
    assert clicked.delivery_status.value == "clicked"
    assert delivered.outcome is DeliveryProviderEventOutcome.STALE
    assert delivered.delivery_status is not None
    assert delivered.delivery_status.value == "clicked"
    assert duplicate.duplicate is True
    assert duplicate.outcome is DeliveryProviderEventOutcome.APPLIED
    assert delivery.status == "clicked"
    assert delivery.delivered_at is None
    assert outcomes == {
        clicked_event_id: "applied",
        delivered_event_id: "stale",
    }
