from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from datetime import datetime, timezone

import httpx
import pytest

from src.market_morning.pipeline.source_ingestion import (
    build_source_persistence_plan,
    collect_source_batch,
)
from src.market_morning.sources.base import SourceLifecycleStatus, SourceValidationError
from src.market_morning.sources.tdnet_api import (
    TDNET_API_BASE_URL,
    TDNET_DOCUMENT_S3_HOST,
    TDNET_PUBLIC_VIEWER_BASE_URL,
    TdnetApiAdapter,
    _OneRequestPerSecondPacer,
    build_tdnet_api_adapter_from_env,
)

NOW = datetime(2026, 7, 21, 0, 15, tzinfo=timezone.utc)
SECRET = "tdnet-secret-never-persist"
DISCLOSURE_NUMBER = "20260721596691"
PDF = b"%PDF-synthetic-tdnet"


def _row(**overrides):
    row = {
        "code": "72030",
        "name": "トヨタ自動車",
        "disclosedDate": "2026-07-21",
        "disclosedTime": "08:05:00",
        "handlingType": None,
        "disclosureNumber": DISCLOSURE_NUMBER,
        "modifiedHistory": "1",
        "title": "自己株式の取得に関するお知らせ",
        "disclosureItems": ["11105"],
        "pdfGeneralFlag": "1",
        "pdfSummaryFlag": "0",
        "xbrlFlag": "0",
    }
    row.update(overrides)
    return row


def _index_payload(*rows, status="200", count=None):
    return {
        "statusCode": status,
        "message": None,
        "count": str(len(rows) if count is None else count),
        "publiclyList": list(rows),
    }


def _document_payload(content: bytes = PDF):
    return {
        "fileUrl": None,
        "responseType": "1",
        "statusCode": "200",
        "message": None,
        "fileData": base64.b64encode(content).decode("ascii"),
    }


async def _tracked(*codes):
    return codes


async def _no_wait() -> None:
    return None


def _adapter(handler, **kwargs):
    return TdnetApiAdapter(
        access_key=SECRET,
        issuer_code_resolver=lambda: _tracked("7203"),
        lookback_days=0,
        clock=lambda: NOW,
        request_pacer=_no_wait,
        transport=httpx.MockTransport(handler),
        pdf_text_extractor=lambda content: "売上高は前期比10%増加した。",
        **kwargs,
    )


def test_tdnet_discovers_tracked_issuers_and_persists_public_pdf_evidence() -> None:
    requests: list[httpx.Request] = []
    pace_calls = 0

    async def pace() -> None:
        nonlocal pace_calls
        pace_calls += 1

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        body = json.loads(request.content)
        if request.url.path == "/tdlist":
            rows = (
                (
                    _row(),
                    _row(
                        code="67580",
                        disclosureNumber="20260721596692",
                        pdfGeneralFlag=None,
                    ),
                )
                if body["editDelFlag"] is None
                else ()
            )
            return httpx.Response(200, json=_index_payload(*rows))
        assert request.url.path == "/tdfile"
        return httpx.Response(200, json=_document_payload())

    adapter = TdnetApiAdapter(
        access_key=SECRET,
        issuer_code_resolver=lambda: _tracked("7203"),
        lookback_days=0,
        clock=lambda: NOW,
        request_pacer=pace,
        transport=httpx.MockTransport(handler),
        pdf_text_extractor=lambda content: "売上高は前期比10%増加した。",
    )
    batch = asyncio.run(collect_source_batch(adapter, None))

    assert len(batch.records) == 1
    record = batch.records[0]
    assert record.provider == "tdnet"
    assert record.issuer_codes == ("7203",)
    assert "状態: 開示中" in record.evidence_text
    assert "売上高は前期比10%増加した" in record.evidence_text
    assert record.original_url == (
        f"{TDNET_PUBLIC_VIEWER_BASE_URL}1401{DISCLOSURE_NUMBER}.pdf"
    )
    assert SECRET not in record.original_url
    assert record.metadata["evidence_scope"] == "metadata_and_pdf_text_first_40_pages"
    assert record.metadata["document_delivery"] == "inline_base64"
    assert batch.next_cursor["days"]["2026-07-21"] == {
        f"{DISCLOSURE_NUMBER}:1": record.revision_key
    }
    assert pace_calls == 3
    assert [request.url.path for request in requests] == ["/tdlist", "/tdlist", "/tdfile"]
    for request in requests:
        assert request.url.scheme == "https"
        assert request.url.host == "api.arrowfront.jp"
        assert request.headers["x-api-key"] == SECRET
        assert json.loads(request.content)["accessKey"] == SECRET
    current_body = json.loads(requests[0].content)
    history_body = json.loads(requests[1].content)
    assert current_body == {
        "code": None,
        "dateFrom": "2026-07-21",
        "dateTo": "2026-07-21",
        "editDelFlag": None,
        "accessKey": SECRET,
    }
    assert history_body["editDelFlag"] == "1"


def test_tdnet_cursor_avoids_duplicate_document_download() -> None:
    def first_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if request.url.path == "/tdlist":
            return httpx.Response(
                200,
                json=_index_payload(*((_row(),) if body["editDelFlag"] is None else ())),
            )
        return httpx.Response(200, json=_document_payload())

    first = asyncio.run(collect_source_batch(_adapter(first_handler), None))
    document_calls = 0

    def duplicate_handler(request: httpx.Request) -> httpx.Response:
        nonlocal document_calls
        body = json.loads(request.content)
        if request.url.path == "/tdlist":
            return httpx.Response(
                200,
                json=_index_payload(*((_row(),) if body["editDelFlag"] is None else ())),
            )
        document_calls += 1
        return httpx.Response(200, json=_document_payload())

    duplicate = asyncio.run(
        collect_source_batch(_adapter(duplicate_handler), first.next_cursor)
    )
    assert duplicate.records == ()
    assert document_calls == 0


def test_tdnet_cold_start_captures_correction_chain_and_downloads_only_latest() -> None:
    original = _row()
    corrected = _row(
        handlingType="revision",
        modifiedHistory="2",
        title="（訂正）自己株式の取得に関するお知らせ",
    )
    document_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal document_calls
        body = json.loads(request.content)
        if request.url.path == "/tdlist":
            rows = (corrected,) if body["editDelFlag"] is None else (original, corrected)
            return httpx.Response(200, json=_index_payload(*rows))
        document_calls += 1
        return httpx.Response(200, json=_document_payload(b"%PDF-corrected"))

    batch = asyncio.run(collect_source_batch(_adapter(handler), None))

    assert document_calls == 1
    assert [record.lifecycle_status for record in batch.records] == [
        SourceLifecycleStatus.ACTIVE,
        SourceLifecycleStatus.CORRECTED,
    ]
    assert batch.records[0].metadata["document_delivery"] == "not_requested"
    assert batch.records[1].metadata["document_delivery"] == "inline_base64"
    plan = build_source_persistence_plan(batch, existing_revisions=())
    assert plan.items[0].supersedes_identity is None
    assert plan.items[1].supersedes_identity == plan.items[0].identity


def test_tdnet_deleted_disclosure_is_metadata_only_and_never_fetches_document() -> None:
    original = _row(pdfGeneralFlag="0")
    deleted = _row(
        handlingType="delete",
        modifiedHistory="2",
        pdfGeneralFlag="0",
    )
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert request.url.path == "/tdlist"
        body = json.loads(request.content)
        rows = (deleted,) if body["editDelFlag"] is None else (original, deleted)
        return httpx.Response(200, json=_index_payload(*rows))

    batch = asyncio.run(collect_source_batch(_adapter(handler), None))

    assert calls == 2
    assert batch.records[-1].lifecycle_status is SourceLifecycleStatus.WITHDRAWN
    assert "状態: 削除" in batch.records[-1].evidence_text
    assert all(record.metadata["evidence_scope"] == "metadata_only" for record in batch.records)


def test_tdnet_uses_summary_pdf_when_full_text_is_unavailable() -> None:
    requested_file_type = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requested_file_type
        body = json.loads(request.content)
        if request.url.path == "/tdlist":
            row = _row(pdfGeneralFlag="0", pdfSummaryFlag="1")
            return httpx.Response(
                200,
                json=_index_payload(*((row,) if body["editDelFlag"] is None else ())),
            )
        requested_file_type = body["fileTypeFlag"]
        return httpx.Response(200, json=_document_payload())

    record = asyncio.run(collect_source_batch(_adapter(handler), None)).records[0]
    assert requested_file_type == "s"
    assert record.metadata["document_file_type"] == "s"


def test_tdnet_response_type_two_downloads_only_allowlisted_s3_base64() -> None:
    one_time_url = f"https://{TDNET_DOCUMENT_S3_HOST}/one-time/file?signature=secret-query"
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.host == TDNET_DOCUMENT_S3_HOST:
            return httpx.Response(
                200,
                content=base64.b64encode(b"%PDF-large-file"),
                headers={"content-type": "text/plain"},
            )
        body = json.loads(request.content)
        if request.url.path == "/tdlist":
            row = _row(pdfGeneralFlag="2")
            return httpx.Response(
                200,
                json=_index_payload(*((row,) if body["editDelFlag"] is None else ())),
            )
        return httpx.Response(
            200,
            json={
                "fileUrl": one_time_url,
                "responseType": "2",
                "statusCode": "200",
                "message": None,
                "fileData": None,
            },
        )

    record = asyncio.run(collect_source_batch(_adapter(handler), None)).records[0]

    s3_request = requests[-1]
    assert s3_request.url.host == TDNET_DOCUMENT_S3_HOST
    assert "x-api-key" not in s3_request.headers
    assert SECRET.encode() not in s3_request.content
    assert one_time_url not in json.dumps(record.metadata)
    assert one_time_url != record.original_url
    assert record.metadata["document_delivery"] == "s3_one_time_base64"
    assert record.content_hash_sha256 == hashlib.sha256(b"%PDF-large-file").hexdigest()


@pytest.mark.parametrize(
    "file_url",
    [
        "http://mi-bucket-tdfile.s3.ap-northeast-1.amazonaws.com/file",
        "https://evil.example/file",
        "https://mi-bucket-tdfile.s3.ap-northeast-1.amazonaws.com.evil.example/file",
        "https://user@mi-bucket-tdfile.s3.ap-northeast-1.amazonaws.com/file",
        "https://mi-bucket-tdfile.s3.ap-northeast-1.amazonaws.com:444/file",
        "https://mi-bucket-tdfile.s3.ap-northeast-1.amazonaws.com:bad/file",
    ],
)
def test_tdnet_rejects_untrusted_one_time_document_urls(file_url) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if request.url.path == "/tdlist":
            return httpx.Response(
                200,
                json=_index_payload(*((_row(),) if body["editDelFlag"] is None else ())),
            )
        return httpx.Response(
            200,
            json={
                "fileUrl": file_url,
                "responseType": "2",
                "statusCode": "200",
                "message": None,
                "fileData": None,
            },
        )

    adapter = _adapter(handler)
    discovered = asyncio.run(adapter.discover(None))
    with pytest.raises(SourceValidationError, match="tdnet_document_url_invalid") as raised:
        asyncio.run(adapter.fetch(discovered.documents[0]))
    assert SECRET not in str(raised.value)
    assert SECRET not in repr(adapter)
    assert TDNET_API_BASE_URL not in str(raised.value)


def test_tdnet_fails_closed_when_index_is_truncated() -> None:
    adapter = _adapter(
        lambda request: httpx.Response(
            200,
            json=_index_payload(status="206", count=10_000),
        )
    )
    with pytest.raises(SourceValidationError, match="tdnet_index_truncated"):
        asyncio.run(adapter.discover(None))


def test_tdnet_default_pacer_reserves_one_second_between_api_calls() -> None:
    now = 10.0
    delays: list[float] = []

    def monotonic() -> float:
        return now

    async def sleep(delay: float) -> None:
        nonlocal now
        delays.append(delay)
        now += delay

    pacer = _OneRequestPerSecondPacer(monotonic=monotonic, sleep=sleep)

    async def run() -> None:
        await pacer()
        await pacer()
        await pacer()

    asyncio.run(run())
    assert delays == [1.0, 1.0]


def test_tdnet_enforces_streamed_index_response_size_limit() -> None:
    adapter = _adapter(
        lambda request: httpx.Response(
            200,
            content=b"x" * 1_001,
            headers={"content-type": "application/json"},
        ),
        max_index_bytes=1_000,
    )
    with pytest.raises(SourceValidationError, match="tdnet_api_too_large"):
        asyncio.run(adapter.discover(None))


@pytest.mark.parametrize(
    "payload",
    [
        {"statusCode": "422", "message": "bad key", "count": "0", "publiclyList": []},
        {"statusCode": "200", "message": None, "count": "1", "publiclyList": []},
        {"statusCode": "200", "message": None, "count": "0", "publiclyList": "bad"},
        ["not-an-object"],
    ],
)
def test_tdnet_rejects_logical_index_contract_errors(payload) -> None:
    adapter = _adapter(lambda request: httpx.Response(200, json=payload))
    with pytest.raises(SourceValidationError, match="tdnet_index_contract_invalid"):
        asyncio.run(adapter.discover(None))


@pytest.mark.parametrize(
    "cursor",
    [
        {},
        {"schema": 2, "days": {}},
        {"schema": 1, "days": {"bad-date": {}}},
        {"schema": 1, "days": {"2026-07-21": {f"{DISCLOSURE_NUMBER}:0": "a" * 64}}},
        {"schema": 1, "days": {"2026-07-21": {f"{DISCLOSURE_NUMBER}:1": "bad"}}},
    ],
)
def test_tdnet_rejects_malformed_cursor(cursor) -> None:
    adapter = _adapter(lambda request: pytest.fail("network must not be reached"))
    with pytest.raises(SourceValidationError, match="tdnet_cursor_invalid"):
        asyncio.run(adapter.discover(cursor))


def test_tdnet_empty_watchlist_does_not_call_all_market_endpoint() -> None:
    adapter = TdnetApiAdapter(
        access_key=SECRET,
        issuer_code_resolver=lambda: _tracked(),
        lookback_days=1,
        clock=lambda: NOW,
        request_pacer=_no_wait,
        transport=httpx.MockTransport(lambda request: pytest.fail("unexpected network call")),
    )
    batch = asyncio.run(adapter.discover(None))
    assert batch.documents == ()
    assert set(batch.next_cursor["days"]) == {"2026-07-20", "2026-07-21"}


@pytest.mark.parametrize(
    ("document_payload", "error_code"),
    [
        (
            {
                "fileUrl": None,
                "responseType": "1",
                "statusCode": "200",
                "message": None,
                "fileData": "not-base64!",
            },
            "tdnet_document_base64_invalid",
        ),
        (_document_payload(b"not-a-pdf"), "tdnet_pdf_invalid"),
        (
            {
                "fileUrl": None,
                "responseType": "1",
                "statusCode": "204",
                "message": "missing",
                "fileData": None,
            },
            "tdnet_document_contract_invalid",
        ),
    ],
)
def test_tdnet_rejects_invalid_document_contracts(document_payload, error_code) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if request.url.path == "/tdlist":
            return httpx.Response(
                200,
                json=_index_payload(*((_row(),) if body["editDelFlag"] is None else ())),
            )
        return httpx.Response(200, json=document_payload)

    adapter = _adapter(handler)
    discovered = asyncio.run(adapter.discover(None))
    with pytest.raises(SourceValidationError, match=error_code):
        asyncio.run(adapter.fetch(discovered.documents[0]))


def test_tdnet_duplicate_revision_conflict_fails_before_cursor_advances() -> None:
    changed = _row(title="same identity but changed title")

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        rows = (_row(),) if body["editDelFlag"] is None else (changed,)
        return httpx.Response(200, json=_index_payload(*rows))

    with pytest.raises(SourceValidationError, match="tdnet_index_duplicate_revision_conflict"):
        asyncio.run(_adapter(handler).discover(None))


def test_tdnet_health_returns_stable_degraded_code() -> None:
    adapter = _adapter(lambda request: httpx.Response(503))
    health = asyncio.run(adapter.health())
    assert health.ok is False
    assert health.detail_code == "tdnet_api_unavailable"
    assert health.checked_at == NOW


def test_tdnet_environment_factory_is_fail_closed_and_hides_secret() -> None:
    adapter = build_tdnet_api_adapter_from_env(
        issuer_code_resolver=lambda: _tracked("7203"),
        environ={
            "VIBE_MARKET_MORNING_TDNET_API_KEY": SECRET,
            "VIBE_MARKET_MORNING_TDNET_LOOKBACK_DAYS": "3",
        },
        clock=lambda: NOW,
        request_pacer=_no_wait,
        transport=httpx.MockTransport(lambda request: httpx.Response(503)),
    )
    assert "lookback_days=3" in repr(adapter)
    assert SECRET not in repr(adapter)

    with pytest.raises(SourceValidationError, match="tdnet_access_key_invalid"):
        build_tdnet_api_adapter_from_env(
            issuer_code_resolver=lambda: _tracked("7203"),
            environ={},
        )
    with pytest.raises(SourceValidationError, match="tdnet_lookback_days_invalid"):
        build_tdnet_api_adapter_from_env(
            issuer_code_resolver=lambda: _tracked("7203"),
            environ={
                "VIBE_MARKET_MORNING_TDNET_API_KEY": SECRET,
                "VIBE_MARKET_MORNING_TDNET_LOOKBACK_DAYS": "two",
            },
        )
