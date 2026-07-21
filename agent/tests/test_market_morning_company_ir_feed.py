"""Production company IR feed boundaries and failure isolation."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

import httpx
import pytest

NOW = datetime(2026, 7, 21, 0, 0, tzinfo=timezone.utc)
PUBLIC_IPS = ("8.8.8.8",)


async def _public_resolver(_host: str, _port: int):
    return PUBLIC_IPS


async def _tracked_toyota():
    return ("7203",)


async def _not_tracked():
    return ("6758",)


def _config(**overrides):
    from src.market_morning.sources.company_ir_feed import (
        CompanyIrApprovedFeedConfig,
    )

    values = {
        "issuer_code": "7203",
        "provider_key": "toyota",
        "index_url": "https://global.toyota/en/ir/feed/index.json",
        "allowed_base_urls": (
            "https://global.toyota/en/ir/",
            "https://cdn.toyota.example/ir/",
        ),
    }
    values.update(overrides)
    return CompanyIrApprovedFeedConfig(**values)


def _item(**overrides):
    values = {
        "document_id": "IR-2026-001",
        "revision": "2026-07-21T08:00:00+09:00",
        "url": "https://global.toyota/en/ir/library/result/report.pdf",
        "title": "2027年3月期 第1四半期決算説明資料",
        "document_type": "investor_presentation",
        "published_at": "2026-07-21T08:00:00+09:00",
        "lifecycle_status": "active",
        "content_kind": "pdf",
    }
    values.update(overrides)
    return values


def _index_bytes(*items, issuer_code="7203") -> bytes:
    return json.dumps(
        {
            "schema_version": 1,
            "issuer_code": issuer_code,
            "items": list(items),
        },
        ensure_ascii=False,
    ).encode()


def _adapter(handler, **overrides):
    from src.market_morning.sources.company_ir_feed import (
        CompanyIrApprovedFeedAdapter,
    )

    values = {
        "config": _config(),
        "issuer_code_resolver": _tracked_toyota,
        "resolver": _public_resolver,
        "clock": lambda: NOW,
        "transport": httpx.MockTransport(handler),
        "pdf_text_extractor": lambda _content: "売上高は前年同期比で増加しました。",
    }
    values.update(overrides)
    return CompanyIrApprovedFeedAdapter(**values)


def test_pdf_feed_collects_one_auditable_record_and_revision_cursor() -> None:
    from src.market_morning.pipeline.source_ingestion import collect_source_batch
    from src.market_morning.sources.base import SourceLifecycleStatus

    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("index.json"):
            return httpx.Response(
                200,
                content=_index_bytes(_item()),
                headers={"content-type": "application/json"},
                request=request,
            )
        return httpx.Response(
            200,
            content=b"%PDF-1.7 synthetic",
            headers={"content-type": "application/pdf"},
            request=request,
        )

    adapter = _adapter(handler)
    batch = asyncio.run(collect_source_batch(adapter, None))

    assert adapter.provider == "company_ir_toyota"
    assert len(batch.records) == 1
    record = batch.records[0]
    assert record.issuer_codes == ("7203",)
    assert record.lifecycle_status is SourceLifecycleStatus.ACTIVE
    assert record.original_url.endswith("/ir/library/result/report.pdf")
    assert "売上高は前年同期比で増加しました" in (record.evidence_text or "")
    assert record.metadata == {
        "company_ir_provider_key": "toyota",
        "content_kind": "pdf",
        "evidence_scope": "metadata_and_pdf_text_first_40_pages",
        "feed_contract": "per_issuer_approved_index_v1",
    }
    assert batch.next_cursor == {
        "schema": 1,
        "documents": {"IR-2026-001": record.revision_key},
    }
    assert [request.url.path for request in requests] == [
        "/en/ir/feed/index.json",
        "/en/ir/library/result/report.pdf",
    ]
    assert all(request.headers["user-agent"] for request in requests)

    second = asyncio.run(adapter.discover(batch.next_cursor))
    assert second.documents == ()
    assert second.next_cursor == batch.next_cursor


def test_correction_and_withdrawal_are_new_revisions_without_stale_download() -> None:
    from src.market_morning.pipeline.source_ingestion import collect_source_batch
    from src.market_morning.sources.base import SourceLifecycleStatus

    payloads = iter(
        (
            _index_bytes(
                _item(
                    content_kind="metadata",
                    url="https://global.toyota/en/ir/news/notice.html",
                )
            ),
            _index_bytes(
                _item(
                    revision="2026-07-21T09:00:00+09:00",
                    title="2027年3月期 第1四半期決算説明資料（訂正）",
                    lifecycle_status="corrected",
                    content_kind="metadata",
                    url="https://global.toyota/en/ir/news/notice.html",
                )
            ),
            _index_bytes(
                _item(
                    revision="2026-07-21T10:00:00+09:00",
                    title="2027年3月期 第1四半期決算説明資料（撤回）",
                    lifecycle_status="withdrawn",
                    content_kind="pdf",
                )
            ),
        )
    )
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            content=next(payloads),
            headers={"content-type": "application/json"},
            request=request,
        )

    adapter = _adapter(handler)
    active = asyncio.run(collect_source_batch(adapter, None))
    corrected = asyncio.run(collect_source_batch(adapter, active.next_cursor))
    withdrawn = asyncio.run(collect_source_batch(adapter, corrected.next_cursor))

    records = (active.records[0], corrected.records[0], withdrawn.records[0])
    assert [record.document_id for record in records] == ["IR-2026-001"] * 3
    assert len({record.revision_key for record in records}) == 3
    assert [record.lifecycle_status for record in records] == [
        SourceLifecycleStatus.ACTIVE,
        SourceLifecycleStatus.CORRECTED,
        SourceLifecycleStatus.WITHDRAWN,
    ]
    assert "撤回" in (records[-1].evidence_text or "")
    assert [request.url.path for request in requests] == [
        "/en/ir/feed/index.json",
        "/en/ir/feed/index.json",
        "/en/ir/feed/index.json",
    ]


def test_untracked_issuer_skips_network_and_clears_only_its_feed_cursor() -> None:
    def must_not_run(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("untracked issuer must not fetch its company feed")

    adapter = _adapter(must_not_run, issuer_code_resolver=_not_tracked)
    result = asyncio.run(adapter.discover(None))

    assert result.documents == ()
    assert result.next_cursor == {"schema": 1, "documents": {}}


@pytest.mark.parametrize(
    "overrides",
    [
        {"index_url": "http://global.toyota/en/ir/feed/index.json"},
        {"index_url": "https://global.toyota.evil.example/en/ir/feed/index.json"},
        {"index_url": "https://global.toyota/en/ir/feed/index.json?token=secret"},
        {"index_url": "https://global.toyota/en/ir/../private/index.json"},
        {"allowed_base_urls": ("https://127.0.0.1/ir/",)},
        {"allowed_base_urls": ("https://user:password@global.toyota/en/ir/",)},
    ],
)
def test_config_rejects_non_public_or_non_exact_url_boundaries(overrides) -> None:
    from src.market_morning.sources.base import SourceValidationError

    with pytest.raises(SourceValidationError):
        _config(**overrides)


def test_index_item_outside_whitelist_fails_before_document_request() -> None:
    from src.market_morning.sources.base import SourceValidationError

    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            content=_index_bytes(
                _item(url="https://global.toyota.evil.example/en/ir/report.pdf")
            ),
            headers={"content-type": "application/json"},
            request=request,
        )

    with pytest.raises(SourceValidationError, match="not_approved"):
        asyncio.run(_adapter(handler).discover(None))

    assert [request.url.path for request in requests] == ["/en/ir/feed/index.json"]


def test_private_dns_answer_rejects_request_before_transport() -> None:
    from src.market_morning.sources.base import SourceValidationError

    async def private_resolver(_host: str, _port: int):
        return ("127.0.0.1",)

    def must_not_run(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("private DNS answer must not reach transport")

    adapter = _adapter(must_not_run, resolver=private_resolver)
    with pytest.raises(SourceValidationError, match="dns_not_public"):
        asyncio.run(adapter.discover(None))


@pytest.mark.parametrize(
    ("status", "headers", "content", "expected"),
    [
        (302, {"location": "https://cdn.toyota.example/ir/index.json"}, b"", ConnectionError),
        (200, {"content-type": "text/plain"}, b"not json", Exception),
        (
            200,
            {"content-type": "application/json", "content-length": "99999999"},
            b"{}",
            Exception,
        ),
    ],
)
def test_index_http_contract_fails_closed(status, headers, content, expected) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status,
            headers=headers,
            content=content,
            request=request,
        )

    with pytest.raises(expected):
        asyncio.run(_adapter(handler).discover(None))


def test_pdf_magic_is_checked_even_when_content_type_claims_pdf() -> None:
    from src.market_morning.pipeline.source_ingestion import collect_source_batch
    from src.market_morning.sources.base import SourceValidationError

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("index.json"):
            return httpx.Response(
                200,
                content=_index_bytes(_item()),
                headers={"content-type": "application/json"},
                request=request,
            )
        return httpx.Response(
            200,
            content=b"<html>not a pdf</html>",
            headers={"content-type": "application/pdf"},
            request=request,
        )

    with pytest.raises(SourceValidationError, match="pdf_invalid"):
        asyncio.run(collect_source_batch(_adapter(handler), None))


def test_custom_company_html_parser_and_visible_text_extractor_are_supported() -> None:
    from src.market_morning.pipeline.source_ingestion import collect_source_batch
    from src.market_morning.sources.base import SourceLifecycleStatus
    from src.market_morning.sources.company_ir_feed import (
        CompanyIrContentKind,
        CompanyIrIndexEntry,
    )

    config = _config(index_content_types=("text/html",))

    def parser(content: bytes, media_type: str, _config):
        assert content == b"<html>approved index</html>"
        assert media_type == "text/html"
        return (
            CompanyIrIndexEntry(
                document_id="IR-HTML-001",
                provider_revision="v1",
                original_url="https://global.toyota/en/ir/news/detail.html",
                title="経営方針説明会",
                document_type="management_briefing",
                published_at=NOW,
                lifecycle_status=SourceLifecycleStatus.ACTIVE,
                content_kind=CompanyIrContentKind.HTML,
            ),
        )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("index.json"):
            return httpx.Response(
                200,
                content=b"<html>approved index</html>",
                headers={"content-type": "text/html; charset=utf-8"},
                request=request,
            )
        return httpx.Response(
            200,
            content=(
                "<html><body><h1>経営方針</h1><script>secret()</script>"
                "<p>設備投資を拡大します。</p></body></html>"
            ).encode(),
            headers={"content-type": "text/html; charset=utf-8"},
            request=request,
        )

    batch = asyncio.run(
        collect_source_batch(
            _adapter(handler, config=config, index_parser=parser),
            None,
        )
    )

    evidence = batch.records[0].evidence_text or ""
    assert "経営方針" in evidence
    assert "設備投資を拡大します" in evidence
    assert "secret" not in evidence
    assert batch.records[0].metadata["evidence_scope"] == (
        "metadata_and_html_visible_text"
    )


@pytest.mark.parametrize(
    "payload",
    [
        {"schema_version": 1, "issuer_code": "7203", "items": [], "extra": True},
        {"schema_version": 1, "issuer_code": "6758", "items": []},
        {
            "schema_version": 1,
            "issuer_code": "7203",
            "items": [{**_item(), "unexpected": "field"}],
        },
        {
            "schema_version": 1,
            "issuer_code": "7203",
            "items": [_item(published_at="2026-07-21T08:00:00")],
        },
    ],
)
def test_default_json_index_contract_is_strict(payload) -> None:
    from src.market_morning.sources.base import SourceValidationError
    from src.market_morning.sources.company_ir_feed import (
        parse_company_ir_json_index,
    )

    with pytest.raises(SourceValidationError):
        parse_company_ir_json_index(
            json.dumps(payload, ensure_ascii=False).encode(),
            "application/json",
            _config(),
        )


def test_duplicate_document_ids_and_invalid_cursor_fail_closed() -> None:
    from src.market_morning.sources.base import SourceValidationError

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=_index_bytes(_item(), _item(revision="v2")),
            headers={"content-type": "application/json"},
            request=request,
        )

    adapter = _adapter(handler)
    with pytest.raises(SourceValidationError, match="duplicate_document_id"):
        asyncio.run(adapter.discover(None))
    with pytest.raises(SourceValidationError, match="cursor_invalid"):
        asyncio.run(adapter.discover({"schema": 1, "documents": {"IR": "bad"}}))


def test_each_company_has_an_independent_provider_and_health_state() -> None:
    from src.market_morning.sources.company_ir_feed import (
        CompanyIrApprovedFeedAdapter,
    )

    def healthy(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=_index_bytes(),
            headers={"content-type": "application/json"},
            request=request,
        )

    def unavailable(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, request=request)

    toyota = _adapter(healthy)
    sony_config = _config(
        issuer_code="6758",
        provider_key="sony",
        index_url="https://www.sony.com/en/SonyInfo/IR/feed/index.json",
        allowed_base_urls=("https://www.sony.com/en/SonyInfo/IR/",),
    )
    sony = CompanyIrApprovedFeedAdapter(
        config=sony_config,
        issuer_code_resolver=_not_tracked,
        resolver=_public_resolver,
        clock=lambda: NOW,
        transport=httpx.MockTransport(unavailable),
    )

    assert toyota.provider == "company_ir_toyota"
    assert sony.provider == "company_ir_sony"
    assert asyncio.run(toyota.health()).ok is True
    assert asyncio.run(sony.health()).ok is False
    assert asyncio.run(toyota.health()).detail_code == "company_ir_feed_ready"
    assert asyncio.run(sony.health()).detail_code == "company_ir_feed_unavailable"
    assert "global.toyota" not in repr(toyota)


def test_fetch_requires_reference_from_same_successful_discovery() -> None:
    from src.market_morning.sources.base import (
        SourceDocumentRef,
        SourceValidationError,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=_index_bytes(),
            headers={"content-type": "application/json"},
            request=request,
        )

    adapter = _adapter(handler)
    with pytest.raises(SourceValidationError, match="not_discovered"):
        asyncio.run(adapter.fetch(SourceDocumentRef("IR-UNKNOWN", "v1")))
