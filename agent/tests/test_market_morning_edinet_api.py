from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime, timezone

import httpx
import pytest

from src.market_morning.pipeline.source_ingestion import collect_source_batch
from src.market_morning.sources.base import SourceLifecycleStatus, SourceValidationError
from src.market_morning.sources.edinet_api import (
    EDINET_API_BASE_URL,
    EDINET_PUBLIC_VIEWER_BASE_URL,
    EdinetApiV2Adapter,
    build_edinet_api_v2_adapter_from_env,
)

NOW = datetime(2026, 7, 21, 0, 15, tzinfo=timezone.utc)
SECRET = "edinet-secret-never-persist"


def _row(**overrides):
    row = {
        "seqNumber": 12,
        "docID": "S100ABCD",
        "edinetCode": "E01234",
        "secCode": "72030",
        "JCN": "1234567890123",
        "filerName": "トヨタ自動車株式会社",
        "fundCode": None,
        "ordinanceCode": "010",
        "formCode": "030000",
        "docTypeCode": "120",
        "periodStart": "2025-04-01",
        "periodEnd": "2026-03-31",
        "submitDateTime": "2026-07-21 08:05",
        "docDescription": "有価証券報告書",
        "issuerEdinetCode": None,
        "subjectEdinetCode": None,
        "subsidiaryEdinetCode": None,
        "currentReportReason": None,
        "parentDocID": None,
        "opeDateTime": "2026-07-21 08:06",
        "withdrawalStatus": "0",
        "docInfoEditStatus": "0",
        "disclosureStatus": "0",
        "xbrlFlag": "1",
        "pdfFlag": "1",
        "attachDocFlag": "0",
        "englishDocFlag": "0",
        "csvFlag": "1",
        "legalStatus": "1",
    }
    row.update(overrides)
    return row


def _list_payload(*rows, status="200"):
    return {
        "metadata": {
            "title": "提出された書類を把握するためのAPI",
            "parameter": {"date": "2026-07-21", "type": "2"},
            "resultset": {"count": len(rows)},
            "processDateTime": "2026-07-21 08:15",
            "status": status,
            "message": "OK" if status == "200" else "error",
        },
        "results": list(rows),
    }


async def _tracked(*codes):
    return codes


def _adapter(handler, **kwargs):
    return EdinetApiV2Adapter(
        subscription_key=SECRET,
        issuer_code_resolver=lambda: _tracked("7203"),
        lookback_days=0,
        clock=lambda: NOW,
        transport=httpx.MockTransport(handler),
        pdf_text_extractor=lambda content: "売上高は前期比10%増加した。",
        **kwargs,
    )


def test_edinet_discovers_only_tracked_issuers_and_persists_pdf_evidence() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/documents.json"):
            return httpx.Response(
                200,
                json=_list_payload(_row(), _row(docID="S100ZZZZ", secCode="67580")),
            )
        assert request.url.path.endswith("/documents/S100ABCD")
        return httpx.Response(200, content=b"%PDF-synthetic", headers={"content-type": "application/pdf"})

    batch = asyncio.run(collect_source_batch(_adapter(handler), None))

    assert len(batch.records) == 1
    record = batch.records[0]
    assert record.provider == "edinet"
    assert record.issuer_codes == ("7203",)
    assert record.evidence_text == "売上高は前期比10%増加した。"
    assert record.original_url == f"{EDINET_PUBLIC_VIEWER_BASE_URL}S100ABCD"
    assert "Subscription-Key" not in record.original_url
    assert record.metadata["evidence_scope"] == "pdf_text_first_40_pages"
    assert batch.next_cursor["schema"] == 1
    assert set(batch.next_cursor["days"]["2026-07-21"]) == {"S100ABCD"}
    assert requests[0].url.scheme == "https"
    assert requests[0].url.host == "api.edinet-fsa.go.jp"
    assert requests[0].url.params["date"] == "2026-07-21"
    assert requests[0].url.params["type"] == "2"
    assert requests[0].url.params["Subscription-Key"] == SECRET
    assert requests[1].url.params["type"] == "2"


def test_edinet_cursor_detects_changed_existing_sequence_without_redownloading_duplicates() -> None:
    current_row = _row()

    def first_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/documents.json"):
            return httpx.Response(200, json=_list_payload(current_row))
        return httpx.Response(200, content=b"%PDF-v1", headers={"content-type": "application/pdf"})

    first = asyncio.run(collect_source_batch(_adapter(first_handler), None))
    download_count = 0

    def duplicate_handler(request: httpx.Request) -> httpx.Response:
        nonlocal download_count
        if request.url.path.endswith("/documents.json"):
            return httpx.Response(200, json=_list_payload(current_row))
        download_count += 1
        return httpx.Response(200, content=b"%PDF-v1", headers={"content-type": "application/pdf"})

    duplicate = asyncio.run(collect_source_batch(_adapter(duplicate_handler), first.next_cursor))
    assert duplicate.records == ()
    assert download_count == 0

    corrected_row = _row(
        parentDocID="S100AAAA",
        docDescription="訂正有価証券報告書",
        opeDateTime="2026-07-21 08:20",
    )

    def corrected_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/documents.json"):
            return httpx.Response(200, json=_list_payload(corrected_row))
        return httpx.Response(200, content=b"%PDF-v2", headers={"content-type": "application/pdf"})

    corrected = asyncio.run(collect_source_batch(_adapter(corrected_handler), first.next_cursor))
    assert len(corrected.records) == 1
    assert corrected.records[0].lifecycle_status is SourceLifecycleStatus.CORRECTED
    assert corrected.records[0].revision_key != first.records[0].revision_key


def test_edinet_withdrawal_uses_metadata_evidence_and_does_not_download_pdf() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert request.url.path.endswith("/documents.json")
        return httpx.Response(
            200,
            json=_list_payload(_row(withdrawalStatus="1", pdfFlag="1")),
        )

    batch = asyncio.run(collect_source_batch(_adapter(handler), None))
    record = batch.records[0]
    assert calls == 1
    assert record.lifecycle_status is SourceLifecycleStatus.WITHDRAWN
    assert "状態: 取下げ" in record.evidence_text
    assert record.metadata["evidence_scope"] == "metadata_only"


def test_edinet_empty_pdf_text_falls_back_to_explicit_metadata_only_scope() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/documents.json"):
            return httpx.Response(200, json=_list_payload(_row()))
        return httpx.Response(200, content=b"%PDF-image-only", headers={"content-type": "application/pdf"})

    adapter = EdinetApiV2Adapter(
        subscription_key=SECRET,
        issuer_code_resolver=lambda: _tracked("7203"),
        lookback_days=0,
        clock=lambda: NOW,
        transport=httpx.MockTransport(handler),
        pdf_text_extractor=lambda content: "",
    )
    record = asyncio.run(collect_source_batch(adapter, None)).records[0]
    assert record.metadata["evidence_scope"] == "metadata_only_pdf_text_unavailable"
    assert record.evidence_text.startswith("EDINET APIメタデータのみ。")


@pytest.mark.parametrize(
    "payload",
    [
        _list_payload(_row(), status="401"),
        {"metadata": {"status": "200"}, "results": "not-a-list"},
        ["not-an-object"],
    ],
)
def test_edinet_rejects_http_200_logical_api_errors(payload) -> None:
    adapter = _adapter(lambda request: httpx.Response(200, json=payload))
    with pytest.raises(SourceValidationError, match="edinet_list_contract_invalid"):
        asyncio.run(adapter.discover(None))


def test_edinet_rejects_json_error_body_on_document_download_without_leaking_key() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/documents.json"):
            return httpx.Response(200, json=_list_payload(_row()))
        return httpx.Response(
            200,
            json={"metadata": {"status": "404"}},
        )

    adapter = _adapter(handler)
    discovered = asyncio.run(adapter.discover(None))
    with pytest.raises(SourceValidationError) as raised:
        asyncio.run(adapter.fetch(discovered.documents[0]))
    assert str(raised.value) == "edinet_response_content_type_invalid"
    assert SECRET not in str(raised.value)
    assert SECRET not in repr(adapter)
    assert EDINET_API_BASE_URL not in str(raised.value)


def test_edinet_enforces_streamed_response_size_limit() -> None:
    adapter = _adapter(
        lambda request: httpx.Response(
            200,
            content=b"x" * 1_001,
            headers={"content-type": "application/json"},
        ),
        max_list_bytes=1_000,
    )
    with pytest.raises(SourceValidationError, match="edinet_response_too_large"):
        asyncio.run(adapter.discover(None))


@pytest.mark.parametrize(
    "cursor",
    [
        {},
        {"schema": 2, "days": {}},
        {"schema": 1, "days": {"bad-date": {}}},
        {"schema": 1, "days": {"2026-07-21": {"S100ABCD": "bad"}}},
    ],
)
def test_edinet_rejects_malformed_cursor(cursor) -> None:
    adapter = _adapter(lambda request: pytest.fail("network must not be reached"))
    with pytest.raises(SourceValidationError, match="edinet_cursor_invalid"):
        asyncio.run(adapter.discover(cursor))


def test_edinet_empty_watchlist_does_not_call_all_market_endpoint() -> None:
    adapter = EdinetApiV2Adapter(
        subscription_key=SECRET,
        issuer_code_resolver=lambda: _tracked(),
        lookback_days=1,
        clock=lambda: NOW,
        transport=httpx.MockTransport(lambda request: pytest.fail("unexpected network call")),
    )
    batch = asyncio.run(adapter.discover(None))
    assert batch.documents == ()
    assert set(batch.next_cursor["days"]) == {"2026-07-20", "2026-07-21"}


def test_edinet_health_returns_stable_degraded_code() -> None:
    adapter = _adapter(lambda request: httpx.Response(503))
    health = asyncio.run(adapter.health())
    assert health.ok is False
    assert health.detail_code == "edinet_api_unavailable"
    assert health.checked_at == NOW


def test_edinet_environment_factory_is_fail_closed_and_never_repr_leaks_secret() -> None:
    adapter = build_edinet_api_v2_adapter_from_env(
        issuer_code_resolver=lambda: _tracked("7203"),
        environ={
            "VIBE_MARKET_MORNING_EDINET_API_KEY": SECRET,
            "VIBE_MARKET_MORNING_EDINET_LOOKBACK_DAYS": "3",
        },
        clock=lambda: NOW,
        transport=httpx.MockTransport(lambda request: httpx.Response(503)),
    )
    assert "lookback_days=3" in repr(adapter)
    assert SECRET not in repr(adapter)

    with pytest.raises(SourceValidationError, match="edinet_subscription_key_invalid"):
        build_edinet_api_v2_adapter_from_env(
            issuer_code_resolver=lambda: _tracked("7203"),
            environ={},
        )
    with pytest.raises(SourceValidationError, match="edinet_lookback_days_invalid"):
        build_edinet_api_v2_adapter_from_env(
            issuer_code_resolver=lambda: _tracked("7203"),
            environ={
                "VIBE_MARKET_MORNING_EDINET_API_KEY": SECRET,
                "VIBE_MARKET_MORNING_EDINET_LOOKBACK_DAYS": "two",
            },
        )


def test_edinet_content_hash_is_the_downloaded_pdf_hash() -> None:
    content = b"%PDF-content-hash"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/documents.json"):
            return httpx.Response(200, json=_list_payload(_row()))
        return httpx.Response(200, content=content, headers={"content-type": "application/pdf"})

    record = asyncio.run(collect_source_batch(_adapter(handler), None)).records[0]
    assert record.content_hash_sha256 == hashlib.sha256(content).hexdigest()
