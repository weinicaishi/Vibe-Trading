"""Privacy-safe operational overview and EventBrief review contracts."""

from __future__ import annotations

import asyncio
import sys
from collections import deque
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.dialects import mysql

NOW = datetime(2026, 7, 21, 0, 30, tzinfo=timezone.utc)


class _Result:
    def __init__(self, *, mappings=(), scalar=None):
        self.mapping_rows = mappings
        self.scalar = scalar

    def mappings(self):
        return SimpleNamespace(
            all=lambda: self.mapping_rows,
            one=lambda: self.mapping_rows[0],
        )

    def scalar_one(self):
        return self.scalar

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
        return self.results.popleft()

    def add(self, value):
        self.added.append(value)

    async def flush(self):
        self.flush_count += 1


def _mysql_sql(statement) -> str:
    return str(
        statement.compile(
            dialect=mysql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )


def test_operations_statements_are_bounded_and_do_not_select_private_content() -> None:
    import src.market_morning.operations as operations

    since = NOW - timedelta(hours=24)
    statements = (
        operations.build_job_status_counts_statement(since=since),
        operations.build_job_failure_counts_statement(since=since),
        operations.build_recent_global_runs_statement(since=since, limit=10),
        operations.build_event_brief_counts_statement(since=since),
        operations.build_event_brief_attempt_count_statement(since=since),
        operations.build_model_usage_summary_statement(since=since),
        operations.build_model_usage_costs_statement(since=since),
        operations.build_delivery_counts_statement(since=since),
        operations.build_engagement_counts_statement(since=since),
        operations.build_active_halts_statement(limit=10),
    )
    sql = "\n".join(_mysql_sql(statement) for statement in statements)

    assert "LIMIT 10" in sql
    assert "mm_jobs" in sql
    assert "mm_global_edition_runs" in sql
    assert "mm_event_briefs" in sql
    assert "mm_model_usage_events" in sql
    assert "mm_delivery_attempts" in sql
    assert "mm_analytics_events" in sql
    assert "note_text" not in sql
    assert "deep_link_token" not in sql
    assert "original_url" not in sql
    assert "normalized_payload" not in sql
    assert "mm_event_briefs.payload" not in sql
    assert "mm_jobs.payload" not in sql


def test_load_operations_summary_aggregates_real_usage_cost_and_missing_alerts() -> None:
    import src.market_morning.operations as operations

    session = _Session(
        _Result(
            mappings=(
                {
                    "provider": "tdnet",
                    "cursor": {"page": 2},
                    "last_successful_discovery_at": NOW.replace(tzinfo=None),
                    "last_error_at": None,
                    "last_error_code": None,
                },
                {
                    "provider": "edinet",
                    "cursor": None,
                    "last_successful_discovery_at": None,
                    "last_error_at": NOW.replace(tzinfo=None),
                    "last_error_code": "source_timeout",
                },
            )
        ),
        _Result(mappings=({"key": "succeeded", "count": 12}, {"key": "failed", "count": 2})),
        _Result(mappings=({"key": "source_timeout", "count": 2},)),
        _Result(
            mappings=(
                {
                    "run_id": "11111111-1111-4111-8111-111111111111",
                    "edition_date": date(2026, 7, 21),
                    "run_version": 1,
                    "attempt_key": "0700",
                    "scenario": "standard",
                    "status": "complete",
                    "is_current": True,
                    "email_permitted": True,
                    "late": False,
                    "reason_code": None,
                    "started_at": NOW.replace(tzinfo=None),
                    "completed_at": NOW.replace(tzinfo=None),
                },
            )
        ),
        _Result(
            mappings=(
                {"status": "published", "review_status": "auto_validated", "count": 8},
                {"status": "blocked", "review_status": "pending", "count": 1},
            )
        ),
        _Result(scalar=11),
        _Result(
            mappings=(
                {
                    "invocation_count": 11,
                    "usage_missing_count": 1,
                    "unpriced_count": 2,
                    "input_tokens": 1000,
                    "output_tokens": 250,
                },
            )
        ),
        _Result(mappings=({"currency": "USD", "cost_micros": 1234},)),
        _Result(mappings=({"key": "sent", "count": 5}, {"key": "failed", "count": 1})),
        _Result(mappings=({"key": "edition_opened", "count": 4}, {"key": "source_opened", "count": 7})),
        _Result(
            mappings=(
                {
                    "edition_date": date(2026, 7, 22),
                    "reason_code": "operator_review",
                    "created_at": NOW.replace(tzinfo=None),
                },
            )
        ),
    )

    summary = asyncio.run(
        operations.load_operations_summary(
            session,
            now=NOW,
            window=timedelta(hours=24),
        )
    )

    assert summary.generated_at == NOW
    assert summary.window_started_at == NOW - timedelta(hours=24)
    assert summary.cost_observability == "usage_incomplete"
    assert summary.event_brief_generation_attempt_count == 11
    assert summary.model_usage.invocation_count == 11
    assert summary.model_usage.usage_missing_count == 1
    assert summary.model_usage.unpriced_count == 2
    assert summary.model_usage.input_tokens == 1000
    assert summary.model_usage.output_tokens == 250
    assert summary.model_usage.costs_by_currency == {"USD": 1234}
    assert summary.job_counts == {"failed": 2, "succeeded": 12}
    assert summary.delivery_counts["failed"] == 1
    assert summary.engagement_counts["source_opened"] == 7
    assert summary.sources[0].provider == "tdnet"
    assert summary.sources[0].status == "healthy"
    assert summary.sources[1].status == "error"
    assert summary.sources[1].last_error_code == "source_timeout"
    assert summary.global_runs[0].status == "complete"
    assert summary.active_halts[0].reason_code == "operator_review"
    assert {alert.code for alert in summary.alerts} >= {
        "source_error",
        "job_failures",
        "event_brief_blocked",
        "delivery_failures",
        "model_usage_missing",
        "model_cost_missing",
    }
    serialized = repr(summary)
    assert "page" not in serialized
    assert "note" not in serialized.lower()
    assert "deep_link_token" not in serialized.lower()
    assert "provider_request_id" not in serialized.lower()


@pytest.mark.parametrize(
    ("invocations", "missing", "unpriced", "expected"),
    (
        (0, 0, 0, "no_invocations"),
        (3, 1, 1, "usage_incomplete"),
        (3, 0, 1, "cost_incomplete"),
        (3, 0, 0, "complete"),
    ),
)
def test_cost_observability_state_is_derived_without_guessing(
    invocations: int,
    missing: int,
    unpriced: int,
    expected: str,
) -> None:
    from src.market_morning.operations import determine_cost_observability

    assert (
        determine_cost_observability(
            invocation_count=invocations,
            usage_missing_count=missing,
            unpriced_count=unpriced,
        )
        == expected
    )


def test_openmetrics_export_is_deterministic_aggregate_only_and_monitorable() -> None:
    from src.market_morning.operations import (
        OperationsAlertView,
        OperationsEventBriefCountView,
        OperationsGlobalRunView,
        OperationsHaltView,
        OperationsModelUsageView,
        OperationsSourceHealthView,
        OperationsSummaryView,
        render_operations_openmetrics,
    )

    summary = OperationsSummaryView(
        generated_at=NOW,
        window_started_at=NOW - timedelta(hours=24),
        cost_observability="cost_incomplete",
        event_brief_generation_attempt_count=7,
        model_usage=OperationsModelUsageView(
            invocation_count=7,
            usage_missing_count=0,
            unpriced_count=1,
            input_tokens=700,
            output_tokens=175,
            costs_by_currency={"JPY": 250, "USD": 525},
        ),
        sources=(
            OperationsSourceHealthView("tdnet", "healthy", NOW, None, None),
            OperationsSourceHealthView(
                "edinet", "error", None, NOW, "source_timeout"
            ),
        ),
        job_counts={"succeeded": 9, "failed": 2},
        job_failure_counts={"source_timeout": 2},
        global_runs=(
            OperationsGlobalRunView(
                "11111111-1111-4111-8111-111111111111",
                date(2026, 7, 21),
                1,
                "0700",
                "standard",
                "complete",
                True,
                True,
                False,
                None,
                NOW,
                NOW,
            ),
        ),
        event_brief_counts=(
            OperationsEventBriefCountView("published", "auto_validated", 6),
            OperationsEventBriefCountView("blocked", "pending", 1),
        ),
        delivery_counts={"sent": 4, "failed": 1},
        engagement_counts={"source_opened": 3, "edition_opened": 2},
        active_halts=(
            OperationsHaltView(date(2026, 7, 22), "operator_review", NOW),
        ),
        alerts=(
            OperationsAlertView("job_failures", "critical", 2),
            OperationsAlertView("model_cost_missing", "warning", 1),
        ),
    )

    rendered = render_operations_openmetrics(summary)

    assert rendered.endswith("# EOF\n")
    assert "market_morning_operations_health 2\n" in rendered
    assert (
        'market_morning_operations_alert_count{code="job_failures",severity="critical"} 2\n'
        in rendered
    )
    assert (
        'market_morning_source_health{provider="edinet",status="error"} 1\n'
        in rendered
    )
    assert (
        'market_morning_job_count{status="failed"} 2\n'
        in rendered
    )
    assert (
        'market_morning_model_cost_micros{currency="JPY"} 250\n'
        in rendered
    )
    assert "market_morning_active_publication_halt_count 1\n" in rendered
    assert rendered.index('currency="JPY"') < rendered.index('currency="USD"')
    assert "11111111-1111-4111-8111-111111111111" not in rendered
    assert "operator_review" not in rendered
    assert "source_timeout" in rendered
    assert "original_url" not in rendered
    assert "payload" not in rendered
    assert "deep_link_token" not in rendered
    assert "provider_request_id" not in rendered
    assert "api_key" not in rendered


def test_openmetrics_export_rejects_naive_time_and_negative_counts() -> None:
    from dataclasses import replace

    from src.market_morning.operations import (
        OperationsModelUsageView,
        OperationsSummaryView,
        render_operations_openmetrics,
    )

    base = OperationsSummaryView(
        generated_at=NOW,
        window_started_at=NOW - timedelta(hours=1),
        cost_observability="no_invocations",
        event_brief_generation_attempt_count=0,
        model_usage=OperationsModelUsageView(0, 0, 0, 0, 0, {}),
        sources=(),
        job_counts={},
        job_failure_counts={},
        global_runs=(),
        event_brief_counts=(),
        delivery_counts={},
        engagement_counts={},
        active_halts=(),
        alerts=(),
    )

    with pytest.raises(ValueError, match="generated_at must be timezone-aware"):
        render_operations_openmetrics(
            replace(base, generated_at=NOW.replace(tzinfo=None))
        )
    with pytest.raises(ValueError, match="count must be non-negative"):
        render_operations_openmetrics(replace(base, job_counts={"failed": -1}))


def test_event_brief_review_queue_is_bounded_and_never_selects_model_payload() -> None:
    import src.market_morning.event_brief_review as review

    sql = _mysql_sql(
        review.build_event_brief_review_queue_statement(
            review_status="auto_validated",
            limit=20,
        )
    )

    assert "LIMIT 20" in sql
    assert "mm_event_briefs" in sql
    assert "mm_normalized_events" in sql
    assert "mm_issuers" in sql
    assert "review_status = 'auto_validated'" in sql
    assert "mm_event_briefs.payload" not in sql
    assert "normalized_payload" not in sql
    assert "original_url" not in sql


def test_operator_can_approve_auto_validated_brief_without_mutating_payload() -> None:
    import src.market_morning.event_brief_review as review
    from src.market_morning.models import AuditLog

    payload = {"schema_version": 1, "confirmed_facts": [{"text": "fact"}]}
    record = SimpleNamespace(
        brief_id="11111111-1111-4111-8111-111111111111",
        event_id="22222222-2222-4222-8222-222222222222",
        status="published",
        review_status="auto_validated",
        payload=payload,
        payload_sha256="a" * 64,
        updated_at=NOW.replace(tzinfo=None),
    )
    session = _Session(_Result(scalar=record))

    result = asyncio.run(
        review.review_event_brief(
            session,
            brief_id=record.brief_id,
            decision="approve",
            reason_code="manual_quality_review",
            actor_reference="api-key-operator",
            reviewed_at=NOW,
        )
    )

    assert result.status == "reviewed"
    assert result.review_status == "approved"
    assert record.review_status == "approved"
    assert record.payload is payload
    assert record.payload_sha256 == "a" * 64
    audit = next(item for item in session.added if isinstance(item, AuditLog))
    assert audit.action == "event_brief.approved"
    assert audit.entity_id == record.brief_id
    assert audit.details == {
        "actor_reference": "api-key-operator",
        "from_review_status": "auto_validated",
        "reason_code": "manual_quality_review",
        "to_review_status": "approved",
    }
    assert session.flush_count == 1


def test_rejection_is_audited_idempotent_and_approval_fails_closed() -> None:
    import src.market_morning.event_brief_review as review

    record = SimpleNamespace(
        brief_id="11111111-1111-4111-8111-111111111111",
        event_id="22222222-2222-4222-8222-222222222222",
        status="published",
        review_status="approved",
        payload={"schema_version": 1},
        payload_sha256="b" * 64,
        updated_at=NOW.replace(tzinfo=None),
    )
    session = _Session(_Result(scalar=record))
    rejected = asyncio.run(
        review.review_event_brief(
            session,
            brief_id=record.brief_id,
            decision="reject",
            reason_code="unsupported_claim",
            actor_reference="api-key-operator",
            reviewed_at=NOW,
        )
    )
    assert rejected.review_status == "rejected"
    assert record.payload_sha256 == "b" * 64

    repeated = _Session(_Result(scalar=record))
    duplicate = asyncio.run(
        review.review_event_brief(
            repeated,
            brief_id=record.brief_id,
            decision="reject",
            reason_code="unsupported_claim",
            actor_reference="api-key-operator",
            reviewed_at=NOW,
        )
    )
    assert duplicate.status == "already_reviewed"
    assert repeated.added == []

    pending = SimpleNamespace(**{**record.__dict__, "review_status": "pending"})
    with pytest.raises(review.EventBriefReviewConflict):
        asyncio.run(
            review.review_event_brief(
                _Session(_Result(scalar=pending)),
                brief_id=pending.brief_id,
                decision="approve",
                reason_code="manual_quality_review",
                actor_reference="api-key-operator",
                reviewed_at=NOW,
            )
        )


@pytest.fixture
def admin_client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    from src.api import market_morning_admin_routes

    async def _allow_test_request() -> None:
        return None

    monkeypatch.setitem(
        sys.modules,
        "api_server",
        SimpleNamespace(require_auth=_allow_test_request),
    )
    app = FastAPI()
    market_morning_admin_routes.register_market_morning_admin_routes(app)
    return TestClient(app, client=("127.0.0.1", 50000))


def test_admin_summary_and_metrics_fail_closed_while_product_is_disabled(
    admin_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.api import market_morning_admin_routes

    async def _must_not_run(**_kwargs):
        raise AssertionError("operations query must not run while disabled")

    monkeypatch.setattr(
        market_morning_admin_routes,
        "get_operations_summary",
        _must_not_run,
    )
    for path in (
        "/market-morning/_internal/operations/summary",
        "/market-morning/_internal/operations/metrics",
    ):
        response = admin_client.get(path)
        assert response.status_code == 503
        assert response.json()["detail"] == "Market Morning is disabled"


def test_admin_metrics_route_returns_openmetrics_without_private_identifiers(
    admin_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.api import market_morning_admin_routes
    from src.market_morning.operations import (
        OperationsAlertView,
        OperationsModelUsageView,
        OperationsSourceHealthView,
        OperationsSummaryView,
    )

    monkeypatch.setenv("VIBE_MARKET_MORNING_ENABLED", "true")
    summary = OperationsSummaryView(
        generated_at=NOW,
        window_started_at=NOW - timedelta(hours=24),
        cost_observability="complete",
        event_brief_generation_attempt_count=1,
        model_usage=OperationsModelUsageView(1, 0, 0, 100, 25, {"USD": 100}),
        sources=(
            OperationsSourceHealthView("tdnet", "healthy", NOW, None, None),
        ),
        job_counts={"succeeded": 1},
        job_failure_counts={},
        global_runs=(),
        event_brief_counts=(),
        delivery_counts={"sent": 1},
        engagement_counts={"edition_opened": 1},
        active_halts=(),
        alerts=(OperationsAlertView("delivery_failures", "warning", 1),),
    )

    async def _summary(*, hours: int, recent_run_limit: int):
        assert hours == 48
        assert recent_run_limit == 10
        return summary

    monkeypatch.setattr(market_morning_admin_routes, "get_operations_summary", _summary)

    response = admin_client.get(
        "/market-morning/_internal/operations/metrics",
        params={"hours": 48},
    )

    assert response.status_code == 200
    assert response.headers["content-type"] == (
        "application/openmetrics-text; version=1.0.0; charset=utf-8"
    )
    assert "market_morning_operations_health 1" in response.text
    assert 'provider="tdnet"' in response.text
    assert "# EOF" in response.text
    assert "user_id" not in response.text
    assert "original_url" not in response.text
    assert "payload" not in response.text


def test_admin_summary_and_review_routes_use_safe_operator_contract(
    admin_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.api import market_morning_admin_routes
    from src.market_morning.event_brief_review import (
        EventBriefReviewQueueItem,
        EventBriefReviewResult,
    )
    from src.market_morning.operations import (
        OperationsEventBriefCountView,
        OperationsGlobalRunView,
        OperationsHaltView,
        OperationsModelUsageView,
        OperationsSourceHealthView,
        OperationsSummaryView,
    )

    monkeypatch.setenv("VIBE_MARKET_MORNING_ENABLED", "true")
    summary = OperationsSummaryView(
        generated_at=NOW,
        window_started_at=NOW - timedelta(hours=24),
        cost_observability="complete",
        event_brief_generation_attempt_count=3,
        model_usage=OperationsModelUsageView(
            invocation_count=3,
            usage_missing_count=0,
            unpriced_count=0,
            input_tokens=300,
            output_tokens=75,
            costs_by_currency={"USD": 525},
        ),
        sources=(
            OperationsSourceHealthView("tdnet", "healthy", NOW, None, None),
        ),
        job_counts={"succeeded": 3},
        job_failure_counts={},
        global_runs=(
            OperationsGlobalRunView(
                "11111111-1111-4111-8111-111111111111",
                date(2026, 7, 21),
                1,
                "0700",
                "standard",
                "complete",
                True,
                True,
                False,
                None,
                NOW,
                NOW,
            ),
        ),
        event_brief_counts=(
            OperationsEventBriefCountView("published", "auto_validated", 3),
        ),
        delivery_counts={"sent": 2},
        engagement_counts={"edition_opened": 1},
        active_halts=(
            OperationsHaltView(date(2026, 7, 22), "operator_review", NOW),
        ),
        alerts=(),
    )
    queue_item = EventBriefReviewQueueItem(
        brief_id="22222222-2222-4222-8222-222222222222",
        event_id="33333333-3333-4333-8333-333333333333",
        event_version=1,
        issuer_id="44444444-4444-4444-8444-444444444444",
        issuer_code="7203",
        legal_name_ja="トヨタ自動車株式会社",
        event_title="決算短信",
        status="published",
        review_status="auto_validated",
        model_version="model-v1",
        prompt_version="prompt-v1",
        attempt_count=1,
        source_count=1,
        validation_errors=(),
        last_failure_code=None,
        completed_at=NOW,
        published_at=NOW,
        updated_at=NOW,
    )
    review_calls = []

    async def _summary(*, hours: int, recent_run_limit: int):
        assert hours == 24
        assert recent_run_limit == 10
        return summary

    async def _queue(*, review_status: str | None, limit: int):
        assert review_status == "auto_validated"
        assert limit == 20
        return (queue_item,)

    async def _review(**kwargs):
        review_calls.append(kwargs)
        return EventBriefReviewResult(
            status="reviewed",
            brief_id=kwargs["brief_id"],
            review_status="approved",
            reviewed_at=NOW,
        )

    monkeypatch.setattr(market_morning_admin_routes, "get_operations_summary", _summary)
    monkeypatch.setattr(market_morning_admin_routes, "get_event_brief_review_queue", _queue)
    monkeypatch.setattr(market_morning_admin_routes, "apply_event_brief_review", _review)

    summary_response = admin_client.get(
        "/market-morning/_internal/operations/summary",
        params={"hours": 24, "recent_run_limit": 10},
    )
    queue_response = admin_client.get(
        "/market-morning/_internal/event-briefs",
        params={"review_status": "auto_validated", "limit": 20},
    )
    review_response = admin_client.post(
        f"/market-morning/_internal/event-briefs/{queue_item.brief_id}/review",
        json={"decision": "approve", "reason_code": "manual_quality_review"},
    )

    assert summary_response.status_code == 200
    summary_body = summary_response.json()
    assert summary_body["cost_observability"] == "complete"
    assert summary_body["model_usage"]["costs_by_currency"] == {"USD": 525}
    assert "cursor" not in summary_response.text
    assert "payload" not in summary_response.text
    assert queue_response.status_code == 200
    assert queue_response.json()["items"][0]["issuer_code"] == "7203"
    assert "payload" not in queue_response.text
    assert review_response.status_code == 200
    assert review_calls == [
        {
            "brief_id": queue_item.brief_id,
            "decision": "approve",
            "reason_code": "manual_quality_review",
            "actor_reference": "vibe-api-key-operator",
        }
    ]


def test_admin_publication_halt_routes_only_stop_and_revoke_with_fixed_actor(
    admin_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.api import market_morning_admin_routes
    from src.market_morning.calendar.service import PublicationHaltOverride
    from src.market_morning.repositories.manual_overrides import (
        OverrideMutationResult,
        OverrideMutationStatus,
    )

    monkeypatch.setenv("VIBE_MARKET_MORNING_ENABLED", "true")
    calls = []
    override = PublicationHaltOverride(
        override_id="55555555-5555-4555-8555-555555555555",
        edition_date=date(2026, 7, 22),
        reason_code="content_quality_incident",
    )

    async def _create(**kwargs):
        calls.append(("create", kwargs))
        return OverrideMutationResult(OverrideMutationStatus.CREATED, override)

    async def _revoke(**kwargs):
        calls.append(("revoke", kwargs))
        return OverrideMutationResult(OverrideMutationStatus.REVOKED, override)

    monkeypatch.setattr(
        market_morning_admin_routes,
        "create_publication_halt_control",
        _create,
    )
    monkeypatch.setattr(
        market_morning_admin_routes,
        "revoke_publication_halt_control",
        _revoke,
    )

    create_response = admin_client.post(
        "/market-morning/_internal/operations/halts",
        json={
            "edition_date": "2026-07-22",
            "reason_code": "content_quality_incident",
        },
    )
    revoke_response = admin_client.delete(
        "/market-morning/_internal/operations/halts/2026-07-22"
    )

    assert create_response.status_code == 200
    assert create_response.json()["status"] == "created"
    assert revoke_response.status_code == 200
    assert revoke_response.json()["status"] == "revoked"
    assert calls == [
        (
            "create",
            {
                "edition_date": date(2026, 7, 22),
                "reason_code": "content_quality_incident",
                "actor_reference": "vibe-api-key-operator",
            },
        ),
        (
            "revoke",
            {
                "edition_date": date(2026, 7, 22),
                "actor_reference": "vibe-api-key-operator",
            },
        ),
    ]
