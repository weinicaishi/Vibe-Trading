"""Versioned EventBrief and deterministic content-quality contracts."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

OCCURRED_AT = datetime(2026, 7, 20, 6, 0, tzinfo=timezone.utc)


def _source(
    *,
    source_id: str = "source-1",
    reachable: bool = True,
    evidence_text: str = "2026年7月20日、通期売上高予想を1,200億円へ修正した。",
):
    from src.market_morning.event_briefs import EventBriefSource

    return EventBriefSource(
        source_id=source_id,
        provider="tdnet",
        original_url=f"https://example.com/{source_id}",
        reachable=reachable,
        evidence_text=evidence_text,
    )


def _payload(**overrides):
    payload = {
        "schema_version": 1,
        "event_id": "event-1",
        "event_version": 1,
        "title": "通期業績予想の修正",
        "occurred_at": OCCURRED_AT.isoformat(),
        "confirmed_facts": [
            {
                "text": "2026年7月20日、通期売上高予想を1,200億円へ修正した。",
                "source_ids": ["source-1"],
            }
        ],
        "open_questions": ["利益率への影響は追加資料の確認が必要。"],
        "source_ids": ["source-1"],
        "model_version": "fixture-model-v1",
        "review_status": "pending",
    }
    payload.update(overrides)
    return payload


def test_event_brief_v1_parses_and_passes_quality_gate() -> None:
    from src.market_morning.event_briefs import (
        EventBriefPublishStatus,
        parse_event_brief_v1,
        validate_event_brief,
    )

    brief = parse_event_brief_v1(_payload())
    result = validate_event_brief(brief, sources=(_source(),))

    assert result.status is EventBriefPublishStatus.PUBLISHED
    assert result.error_codes == ()
    assert result.brief.review_status.value == "auto_validated"
    assert result.brief.confirmed_facts[0].source_ids == ("source-1",)


def test_event_brief_rejects_unknown_or_unreachable_sources() -> None:
    from src.market_morning.event_briefs import (
        EventBriefPublishStatus,
        parse_event_brief_v1,
        validate_event_brief,
    )

    brief = parse_event_brief_v1(_payload())
    missing = validate_event_brief(brief, sources=())
    unreachable = validate_event_brief(
        brief,
        sources=(_source(reachable=False),),
    )

    assert missing.status is EventBriefPublishStatus.BLOCKED
    assert "source_missing" in missing.error_codes
    assert unreachable.status is EventBriefPublishStatus.BLOCKED
    assert "source_unreachable" in unreachable.error_codes


@pytest.mark.parametrize(
    "text",
    [
        "この銘柄は今すぐ買うべきだ。",
        "強烈推荐买入该股票。",
        "Guaranteed profit: buy now.",
    ],
)
def test_event_brief_blocks_recommendation_language(text: str) -> None:
    from src.market_morning.event_briefs import (
        EventBriefPublishStatus,
        parse_event_brief_v1,
        validate_event_brief,
    )

    payload = _payload(
        confirmed_facts=[{"text": text, "source_ids": ["source-1"]}],
    )
    result = validate_event_brief(
        parse_event_brief_v1(payload),
        sources=(_source(evidence_text=text),),
    )

    assert result.status is EventBriefPublishStatus.BLOCKED
    assert "prohibited_recommendation_language" in result.error_codes


def test_event_brief_blocks_numeric_or_date_claims_absent_from_evidence() -> None:
    from src.market_morning.event_briefs import (
        EventBriefPublishStatus,
        parse_event_brief_v1,
        validate_event_brief,
    )

    result = validate_event_brief(
        parse_event_brief_v1(_payload()),
        sources=(
            _source(evidence_text="通期売上高予想を修正した。"),
        ),
    )

    assert result.status is EventBriefPublishStatus.BLOCKED
    assert "unsupported_numeric_or_date_claim" in result.error_codes


def test_insufficient_information_is_a_valid_sourced_result() -> None:
    from src.market_morning.event_briefs import (
        EventBriefPublishStatus,
        parse_event_brief_v1,
        validate_event_brief,
    )

    brief = parse_event_brief_v1(
        _payload(
            confirmed_facts=[],
            open_questions=["現時点の資料だけでは影響を判断できない。"],
        )
    )
    result = validate_event_brief(brief, sources=(_source(),))

    assert result.status is EventBriefPublishStatus.PUBLISHED
    assert result.error_codes == ()


def test_fact_requires_its_own_source_ids_even_when_brief_has_sources() -> None:
    from src.market_morning.event_briefs import (
        EventBriefPublishStatus,
        parse_event_brief_v1,
        validate_event_brief,
    )

    result = validate_event_brief(
        parse_event_brief_v1(
            _payload(
                confirmed_facts=[
                    {"text": "業績予想を修正した。", "source_ids": []}
                ]
            )
        ),
        sources=(_source(evidence_text="業績予想を修正した。"),),
    )

    assert result.status is EventBriefPublishStatus.BLOCKED
    assert "fact_source_ids_empty" in result.error_codes


def test_degraded_brief_contains_only_event_identity_and_source_links() -> None:
    from src.market_morning.event_briefs import (
        EventBriefPublishStatus,
        build_degraded_event_brief,
    )

    brief = build_degraded_event_brief(
        event_id="event-1",
        event_version=1,
        title="通期業績予想の修正",
        occurred_at=OCCURRED_AT,
        sources=(_source(),),
        error_code="model_unavailable",
    )

    assert brief.status is EventBriefPublishStatus.DEGRADED
    assert brief.confirmed_facts == ()
    assert brief.open_questions == ()
    assert brief.model_version is None
    assert brief.error_codes == ("model_unavailable",)
    assert brief.sources[0].original_url == "https://example.com/source-1"


def test_persisted_event_brief_payload_omits_source_evidence_text() -> None:
    from src.market_morning.event_briefs import (
        encode_event_brief,
        parse_event_brief_v1,
        validate_event_brief,
    )

    result = validate_event_brief(
        parse_event_brief_v1(_payload()),
        sources=(_source(evidence_text="private normalized evidence"),),
    )
    payload = encode_event_brief(result.brief)

    assert payload["sources"] == [
        {
            "source_id": "source-1",
            "provider": "tdnet",
            "original_url": "https://example.com/source-1",
        }
    ]
    assert "private normalized evidence" not in repr(payload)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda payload: payload.update(schema_version=2),
        lambda payload: payload.update(extra_field=True),
        lambda payload: payload.update(event_version=0),
        lambda payload: payload.update(occurred_at="2026-07-20T06:00:00"),
    ],
)
def test_event_brief_v1_schema_is_strict(mutation) -> None:
    from src.market_morning.event_briefs import parse_event_brief_v1

    payload = _payload()
    mutation(payload)

    with pytest.raises((TypeError, ValueError)):
        parse_event_brief_v1(payload)
