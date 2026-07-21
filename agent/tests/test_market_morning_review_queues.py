"""Audited operator queues for issuer aliases and merge candidates."""

from __future__ import annotations

import asyncio
from collections import deque
from datetime import date, datetime, timezone
import sys
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.dialects import mysql

NOW = datetime(2026, 7, 21, 0, 30, tzinfo=timezone.utc)
ALIAS_ID = "11111111-1111-4111-8111-111111111111"
ISSUER_ID = "22222222-2222-4222-8222-222222222222"
CANDIDATE_ID = "33333333-3333-4333-8333-333333333333"
LEFT_EVENT_ID = "44444444-4444-4444-8444-444444444444"
RIGHT_EVENT_ID = "55555555-5555-4555-8555-555555555555"


class _Result:
    def __init__(self, *, scalar=None, mappings=(), scalars=()):
        self.scalar = scalar
        self.mapping_rows = mappings
        self.scalar_rows = scalars

    def scalar_one_or_none(self):
        return self.scalar

    def mappings(self):
        return SimpleNamespace(all=lambda: self.mapping_rows)

    def scalars(self):
        return SimpleNamespace(all=lambda: self.scalar_rows)


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


def test_issuer_alias_review_queue_is_bounded_and_excludes_source_reference() -> None:
    import src.market_morning.review_queues as review

    sql = _mysql_sql(
        review.build_issuer_alias_review_queue_statement(
            review_status="pending",
            limit=20,
        )
    )

    assert "LIMIT 20" in sql
    assert "mm_issuer_aliases" in sql
    assert "mm_issuers" in sql
    assert "review_status = 'pending'" in sql
    assert "source_reference" not in sql
    assert "original_url" not in sql
    assert "normalized_payload" not in sql


def test_load_issuer_alias_queue_returns_only_safe_catalog_fields() -> None:
    import src.market_morning.review_queues as review

    session = _Session(
        _Result(
            mappings=(
                {
                    "alias_id": ALIAS_ID,
                    "issuer_id": ISSUER_ID,
                    "issuer_code": "7203",
                    "legal_name_ja": "トヨタ自動車株式会社",
                    "display_alias": "トヨタ",
                    "normalized_alias": "トヨタ",
                    "source_type": "operator",
                    "review_status": "pending",
                    "effective_from": date(2026, 7, 1),
                    "effective_to": None,
                    "reviewed_by": None,
                    "reviewed_at": None,
                    "created_at": NOW.replace(tzinfo=None),
                },
            )
        )
    )

    items = asyncio.run(
        review.load_issuer_alias_review_queue(
            session,
            review_status="pending",
            limit=20,
        )
    )

    assert len(items) == 1
    assert items[0].display_alias == "トヨタ"
    assert items[0].created_at.tzinfo is timezone.utc
    assert not hasattr(items[0], "source_reference")


def test_issuer_alias_approval_is_audited_idempotent_and_terminal() -> None:
    import src.market_morning.review_queues as review
    from src.market_morning.models import AuditLog, IssuerAlias

    record = IssuerAlias(
        alias_id=ALIAS_ID,
        issuer_id=ISSUER_ID,
        display_alias="トヨタ",
        normalized_alias="トヨタ",
        source_type="operator",
        source_reference="private-review-ticket-17",
        review_status="pending",
        effective_from=date(2026, 7, 1),
        effective_to=None,
        reviewed_by=None,
        reviewed_at=None,
        created_at=NOW.replace(tzinfo=None),
    )
    session = _Session(_Result(scalar=record), _Result(scalar=None))
    result = asyncio.run(
        review.review_issuer_alias(
            session,
            alias_id=ALIAS_ID,
            decision="approve",
            reason_code="verified_company_name",
            actor_reference="operator-17",
            reviewed_at=NOW,
        )
    )

    assert result.status == "reviewed"
    assert result.review_status == "approved"
    assert record.review_status == "approved"
    assert record.reviewed_by == "operator-17"
    assert record.source_reference == "private-review-ticket-17"
    audit = next(item for item in session.added if isinstance(item, AuditLog))
    assert audit.action == "issuer_alias.approved"
    assert audit.entity_type == "issuer_alias"
    assert audit.actor_user_id is None
    assert audit.details == {
        "actor_reference": "operator-17",
        "from_review_status": "pending",
        "reason_code": "verified_company_name",
        "to_review_status": "approved",
    }
    assert session.flush_count == 1

    repeated = asyncio.run(
        review.review_issuer_alias(
            _Session(_Result(scalar=record)),
            alias_id=ALIAS_ID,
            decision="approve",
            reason_code="verified_company_name",
            actor_reference="operator-17",
            reviewed_at=NOW,
        )
    )
    assert repeated.status == "already_reviewed"

    with pytest.raises(review.ReviewQueueConflict):
        asyncio.run(
            review.review_issuer_alias(
                _Session(_Result(scalar=record)),
                alias_id=ALIAS_ID,
                decision="reject",
                reason_code="wrong_issuer",
                actor_reference="operator-17",
                reviewed_at=NOW,
            )
        )

    active = IssuerAlias(
        alias_id=ALIAS_ID,
        issuer_id=ISSUER_ID,
        display_alias="トヨタ",
        normalized_alias="トヨタ",
        source_type="operator",
        source_reference=None,
        review_status="pending",
        effective_from=date(2026, 7, 1),
        effective_to=None,
        reviewed_by=None,
        reviewed_at=None,
        created_at=NOW.replace(tzinfo=None),
    )
    with pytest.raises(review.ReviewQueueConflict):
        asyncio.run(
            review.review_issuer_alias(
                _Session(
                    _Result(scalar=active),
                    _Result(scalar="33333333-3333-4333-8333-333333333333"),
                ),
                alias_id=ALIAS_ID,
                decision="approve",
                reason_code="verified_company_name",
                actor_reference="operator-17",
                reviewed_at=NOW,
            )
        )


def test_issuer_alias_approval_rejects_expired_or_globally_conflicting_alias() -> None:
    import src.market_morning.review_queues as review
    from src.market_morning.models import IssuerAlias

    expired = IssuerAlias(
        alias_id=ALIAS_ID,
        issuer_id=ISSUER_ID,
        display_alias="トヨタ",
        normalized_alias="トヨタ",
        source_type="operator",
        source_reference=None,
        review_status="pending",
        effective_from=date(2026, 1, 1),
        effective_to=date(2026, 6, 30),
        reviewed_by=None,
        reviewed_at=None,
        created_at=NOW.replace(tzinfo=None),
    )
    with pytest.raises(review.ReviewQueueConflict):
        asyncio.run(
            review.review_issuer_alias(
                _Session(_Result(scalar=expired)),
                alias_id=ALIAS_ID,
                decision="approve",
                reason_code="verified_company_name",
                actor_reference="operator-17",
                reviewed_at=NOW,
            )
        )


def test_event_merge_review_queue_is_bounded_and_excludes_source_content() -> None:
    import src.market_morning.review_queues as review

    sql = _mysql_sql(
        review.build_event_merge_review_queue_statement(
            review_status="pending",
            limit=30,
        )
    )

    assert "LIMIT 30" in sql
    assert "mm_event_merge_candidates" in sql
    assert "mm_normalized_events AS left_event" in sql
    assert "mm_normalized_events AS right_event" in sql
    assert "mm_issuers" in sql
    assert "review_status = 'pending'" in sql
    assert "mm_source_records" not in sql
    assert "original_url" not in sql
    assert "normalized_payload" not in sql


def test_load_event_merge_queue_returns_evidence_without_source_payload() -> None:
    import src.market_morning.review_queues as review

    session = _Session(
        _Result(
            mappings=(
                {
                    "candidate_id": CANDIDATE_ID,
                    "issuer_id": ISSUER_ID,
                    "issuer_code": "7203",
                    "legal_name_ja": "トヨタ自動車株式会社",
                    "left_event_id": LEFT_EVENT_ID,
                    "left_event_title": "通期業績予想の修正",
                    "left_event_type": "guidance_revision",
                    "left_occurred_at": NOW.replace(tzinfo=None),
                    "right_event_id": RIGHT_EVENT_ID,
                    "right_event_title": "業績予想修正のお知らせ",
                    "right_event_type": "guidance_revision",
                    "right_occurred_at": NOW.replace(tzinfo=None),
                    "reason": "same_issuer_similar_title_24h",
                    "title_similarity": 0.91,
                    "time_distance_seconds": 120,
                    "review_status": "pending",
                    "reviewed_by": None,
                    "reviewed_at": None,
                    "created_at": NOW.replace(tzinfo=None),
                },
            )
        )
    )

    items = asyncio.run(
        review.load_event_merge_review_queue(
            session,
            review_status="pending",
            limit=30,
        )
    )

    assert len(items) == 1
    assert items[0].left_event_title == "通期業績予想の修正"
    assert items[0].right_event_title == "業績予想修正のお知らせ"
    assert items[0].created_at.tzinfo is timezone.utc
    assert not hasattr(items[0], "source_url")
    assert not hasattr(items[0], "payload")


def test_merge_candidate_approval_is_audited_but_never_merges_events() -> None:
    import src.market_morning.review_queues as review
    from src.market_morning.models import AuditLog, EventMergeCandidate, EventSource, NormalizedEvent

    record = EventMergeCandidate(
        candidate_id=CANDIDATE_ID,
        left_event_id=LEFT_EVENT_ID,
        right_event_id=RIGHT_EVENT_ID,
        reason="same_issuer_similar_title_24h",
        title_similarity=0.91,
        time_distance_seconds=120,
        review_status="pending",
        reviewed_by=None,
        reviewed_at=None,
        created_at=NOW.replace(tzinfo=None),
    )
    session = _Session(
        _Result(scalar=record),
        _Result(scalars=(ISSUER_ID, ISSUER_ID)),
    )
    result = asyncio.run(
        review.review_event_merge_candidate(
            session,
            candidate_id=CANDIDATE_ID,
            decision="approve",
            reason_code="same_disclosure_event",
            actor_reference="operator-17",
            reviewed_at=NOW,
        )
    )

    assert result.status == "reviewed"
    assert result.review_status == "approved"
    assert record.review_status == "approved"
    assert record.reviewed_by == "operator-17"
    audit = next(item for item in session.added if isinstance(item, AuditLog))
    assert audit.action == "event_merge_candidate.approved"
    assert audit.entity_type == "event_merge_candidate"
    assert audit.details == {
        "actor_reference": "operator-17",
        "from_review_status": "pending",
        "reason_code": "same_disclosure_event",
        "to_review_status": "approved",
    }
    assert not any(isinstance(item, (NormalizedEvent, EventSource)) for item in session.added)
    assert session.flush_count == 1

    repeated = asyncio.run(
        review.review_event_merge_candidate(
            _Session(_Result(scalar=record)),
            candidate_id=CANDIDATE_ID,
            decision="approve",
            reason_code="same_disclosure_event",
            actor_reference="operator-17",
            reviewed_at=NOW,
        )
    )
    assert repeated.status == "already_reviewed"

    with pytest.raises(review.ReviewQueueConflict):
        asyncio.run(
            review.review_event_merge_candidate(
                _Session(_Result(scalar=record)),
                candidate_id=CANDIDATE_ID,
                decision="reject",
                reason_code="distinct_events",
                actor_reference="operator-17",
                reviewed_at=NOW,
            )
        )


def test_merge_candidate_approval_fails_closed_when_issuer_invariant_is_broken() -> None:
    import src.market_morning.review_queues as review
    from src.market_morning.models import EventMergeCandidate

    record = EventMergeCandidate(
        candidate_id=CANDIDATE_ID,
        left_event_id=LEFT_EVENT_ID,
        right_event_id=RIGHT_EVENT_ID,
        reason="same_issuer_similar_title_24h",
        title_similarity=0.91,
        time_distance_seconds=120,
        review_status="pending",
        reviewed_by=None,
        reviewed_at=None,
        created_at=NOW.replace(tzinfo=None),
    )
    with pytest.raises(review.ReviewQueueConflict):
        asyncio.run(
            review.review_event_merge_candidate(
                _Session(
                    _Result(scalar=record),
                    _Result(
                        scalars=(
                            ISSUER_ID,
                            "66666666-6666-4666-8666-666666666666",
                        )
                    ),
                ),
                candidate_id=CANDIDATE_ID,
                decision="approve",
                reason_code="same_disclosure_event",
                actor_reference="operator-17",
                reviewed_at=NOW,
            )
        )


def test_admin_review_queue_routes_use_fixed_privacy_safe_contracts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.api import market_morning_admin_routes
    from src.market_morning.review_queues import (
        EventMergeReviewQueueItem,
        EventMergeReviewResult,
        IssuerAliasReviewQueueItem,
        IssuerAliasReviewResult,
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
    alias_item = IssuerAliasReviewQueueItem(
        alias_id=ALIAS_ID,
        issuer_id=ISSUER_ID,
        issuer_code="7203",
        legal_name_ja="トヨタ自動車株式会社",
        display_alias="トヨタ",
        normalized_alias="トヨタ",
        source_type="operator",
        review_status="pending",
        effective_from=date(2026, 7, 1),
        effective_to=None,
        reviewed_by=None,
        reviewed_at=None,
        created_at=NOW,
    )
    merge_item = EventMergeReviewQueueItem(
        candidate_id=CANDIDATE_ID,
        issuer_id=ISSUER_ID,
        issuer_code="7203",
        legal_name_ja="トヨタ自動車株式会社",
        left_event_id=LEFT_EVENT_ID,
        left_event_title="通期業績予想の修正",
        left_event_type="guidance_revision",
        left_occurred_at=NOW,
        right_event_id=RIGHT_EVENT_ID,
        right_event_title="業績予想修正のお知らせ",
        right_event_type="guidance_revision",
        right_occurred_at=NOW,
        reason="same_issuer_similar_title_24h",
        title_similarity=0.91,
        time_distance_seconds=120,
        review_status="pending",
        reviewed_by=None,
        reviewed_at=None,
        created_at=NOW,
    )
    calls = []

    async def _aliases(*, review_status: str | None, limit: int):
        assert review_status == "pending"
        assert limit == 20
        return (alias_item,)

    async def _review_alias(**kwargs):
        calls.append(("alias", kwargs))
        return IssuerAliasReviewResult(
            status="reviewed",
            alias_id=ALIAS_ID,
            review_status="approved",
            reviewed_at=NOW,
        )

    async def _candidates(*, review_status: str | None, limit: int):
        assert review_status == "pending"
        assert limit == 30
        return (merge_item,)

    async def _review_candidate(**kwargs):
        calls.append(("candidate", kwargs))
        return EventMergeReviewResult(
            status="reviewed",
            candidate_id=CANDIDATE_ID,
            review_status="approved",
            reviewed_at=NOW,
        )

    monkeypatch.setattr(market_morning_admin_routes, "get_issuer_alias_review_queue", _aliases)
    monkeypatch.setattr(market_morning_admin_routes, "apply_issuer_alias_review", _review_alias)
    monkeypatch.setattr(market_morning_admin_routes, "get_event_merge_review_queue", _candidates)
    monkeypatch.setattr(
        market_morning_admin_routes,
        "apply_event_merge_candidate_review",
        _review_candidate,
    )
    monkeypatch.setenv("VIBE_MARKET_MORNING_ENABLED", "true")
    client = TestClient(app, client=("127.0.0.1", 50000))

    alias_queue = client.get(
        "/market-morning/_internal/issuer-aliases",
        params={"review_status": "pending", "limit": 20},
    )
    alias_review = client.post(
        f"/market-morning/_internal/issuer-aliases/{ALIAS_ID}/review",
        json={
            "decision": "approve",
            "reason_code": "verified_company_name",
        },
    )
    merge_queue = client.get(
        "/market-morning/_internal/event-merge-candidates",
        params={"review_status": "pending", "limit": 30},
    )
    merge_review = client.post(
        f"/market-morning/_internal/event-merge-candidates/{CANDIDATE_ID}/review",
        json={
            "decision": "approve",
            "reason_code": "same_disclosure_event",
        },
    )

    assert alias_queue.status_code == 200
    assert alias_queue.json()["items"][0]["display_alias"] == "トヨタ"
    assert "source_reference" not in alias_queue.text
    assert alias_review.status_code == 200
    assert merge_queue.status_code == 200
    assert merge_queue.json()["items"][0]["title_similarity"] == 0.91
    assert "source_url" not in merge_queue.text
    assert "payload" not in merge_queue.text
    assert merge_review.status_code == 200
    assert calls == [
        (
            "alias",
            {
                "alias_id": ALIAS_ID,
                "decision": "approve",
                "reason_code": "verified_company_name",
                "actor_reference": "vibe-api-key-operator",
            },
        ),
        (
            "candidate",
            {
                "candidate_id": CANDIDATE_ID,
                "decision": "approve",
                "reason_code": "same_disclosure_event",
                "actor_reference": "vibe-api-key-operator",
            },
        ),
    ]

    invalid_alias_reason = client.post(
        f"/market-morning/_internal/issuer-aliases/{ALIAS_ID}/review",
        json={"decision": "approve", "reason_code": "free_form_reason"},
    )
    invalid_merge_reason = client.post(
        f"/market-morning/_internal/event-merge-candidates/{CANDIDATE_ID}/review",
        json={"decision": "approve", "reason_code": "free_form_reason"},
    )
    mismatched_alias_reason = client.post(
        f"/market-morning/_internal/issuer-aliases/{ALIAS_ID}/review",
        json={"decision": "approve", "reason_code": "wrong_issuer"},
    )
    mismatched_merge_reason = client.post(
        f"/market-morning/_internal/event-merge-candidates/{CANDIDATE_ID}/review",
        json={"decision": "approve", "reason_code": "distinct_events"},
    )
    assert invalid_alias_reason.status_code == 422
    assert invalid_merge_reason.status_code == 422
    assert mismatched_alias_reason.status_code == 422
    assert mismatched_merge_reason.status_code == 422
