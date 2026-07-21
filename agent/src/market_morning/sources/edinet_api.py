"""Production-safe EDINET API v2 adapter.

The adapter only talks to the fixed Financial Services Agency endpoint.  It
keeps the subscription key out of persisted URLs and errors, filters the
all-market document list to deployment-resolved watchlist issuer codes, and
tracks immutable provider-row revisions in the durable source cursor.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import re
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

import httpx

from src.market_morning.issuer_master import (
    IssuerMasterValidationError,
    normalize_issuer_code,
)
from src.market_morning.sources.base import (
    DiscoveryBatch,
    NormalizedSourceRecord,
    SourceDocumentRef,
    SourceHealth,
    SourceLifecycleStatus,
    SourcePayload,
    SourceValidationError,
)

EDINET_API_BASE_URL = "https://api.edinet-fsa.go.jp/api/v2"
EDINET_PUBLIC_VIEWER_BASE_URL = "https://disclosure2.edinet-fsa.go.jp/WZEK0040.aspx?"

_JST = timezone(timedelta(hours=9))
_REVISION_FIELDS = (
    "seqNumber",
    "docID",
    "edinetCode",
    "secCode",
    "JCN",
    "filerName",
    "fundCode",
    "ordinanceCode",
    "formCode",
    "docTypeCode",
    "periodStart",
    "periodEnd",
    "submitDateTime",
    "docDescription",
    "issuerEdinetCode",
    "subjectEdinetCode",
    "subsidiaryEdinetCode",
    "currentReportReason",
    "parentDocID",
    "opeDateTime",
    "withdrawalStatus",
    "docInfoEditStatus",
    "disclosureStatus",
    "xbrlFlag",
    "pdfFlag",
    "attachDocFlag",
    "englishDocFlag",
    "csvFlag",
    "legalStatus",
)
_DOCUMENT_ID_PATTERN = re.compile(r"^[A-Z0-9]{8,20}$")

IssuerCodeResolver = Callable[[], Awaitable[Iterable[str]]]
PdfTextExtractor = Callable[[bytes], str]
Clock = Callable[[], datetime]


@dataclass(frozen=True, slots=True)
class _EdinetDocument:
    reference: SourceDocumentRef
    file_date: date
    sequence: int
    issuer_code: str
    published_at: datetime
    lifecycle_status: SourceLifecycleStatus
    title: str
    document_type: str
    pdf_available: bool
    row: dict[str, Any]


def _required_secret(value: str) -> str:
    if not isinstance(value, str):
        raise SourceValidationError("edinet_subscription_key_invalid")
    canonical = value.strip()
    if (
        not canonical
        or len(canonical) > 512
        or any(ord(character) < 32 or ord(character) == 127 for character in canonical)
    ):
        raise SourceValidationError("edinet_subscription_key_invalid")
    return canonical


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _canonical_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    canonical = " ".join(value.split())
    return canonical or None


def _flag(value: Any) -> bool:
    return value == 1 or value == "1"


def _sequence(value: Any) -> int:
    if isinstance(value, bool):
        raise SourceValidationError("edinet_document_row_invalid")
    if isinstance(value, int):
        sequence = value
    elif isinstance(value, str) and value.isdigit():
        sequence = int(value)
    else:
        raise SourceValidationError("edinet_document_row_invalid")
    if sequence < 1:
        raise SourceValidationError("edinet_document_row_invalid")
    return sequence


def _document_id(value: Any) -> str:
    canonical = _canonical_text(value)
    if canonical is None:
        raise SourceValidationError("edinet_document_row_invalid")
    canonical = canonical.upper()
    if _DOCUMENT_ID_PATTERN.fullmatch(canonical) is None:
        raise SourceValidationError("edinet_document_row_invalid")
    return canonical


def _published_at(value: Any) -> datetime:
    canonical = _canonical_text(value)
    if canonical is None:
        raise SourceValidationError("edinet_document_row_invalid")
    try:
        parsed = datetime.fromisoformat(canonical.replace(" ", "T"))
    except ValueError as error:
        raise SourceValidationError("edinet_document_row_invalid") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=_JST)
    return parsed.astimezone(_JST)


def _issuer_code(value: Any) -> str | None:
    canonical = _canonical_text(value)
    if canonical is None or len(canonical) != 5:
        return None
    try:
        return normalize_issuer_code(canonical[:4])
    except IssuerMasterValidationError:
        return None


def _revision_key(row: Mapping[str, Any]) -> str:
    canonical = {field: row.get(field) for field in _REVISION_FIELDS}
    return hashlib.sha256(_canonical_json(canonical)).hexdigest()


def _lifecycle_status(row: Mapping[str, Any]) -> SourceLifecycleStatus:
    if _flag(row.get("withdrawalStatus")):
        return SourceLifecycleStatus.WITHDRAWN
    if _canonical_text(row.get("parentDocID")) is not None:
        return SourceLifecycleStatus.CORRECTED
    return SourceLifecycleStatus.ACTIVE


def _metadata_evidence(document: _EdinetDocument) -> str:
    state = {
        SourceLifecycleStatus.ACTIVE: "開示中",
        SourceLifecycleStatus.CORRECTED: "訂正書類",
        SourceLifecycleStatus.WITHDRAWN: "取下げ",
    }[document.lifecycle_status]
    parts = [
        "EDINET APIメタデータのみ。",
        f"状態: {state}。",
        f"書類: {document.title}。",
        f"提出日時: {document.published_at.isoformat()}。",
    ]
    reason = _canonical_text(document.row.get("currentReportReason"))
    if reason is not None:
        parts.append(f"臨時報告書の提出理由: {reason}。")
    return " ".join(parts)


def extract_edinet_pdf_text(
    content: bytes,
    *,
    max_pages: int = 40,
    max_characters: int = 50_000,
) -> str:
    """Extract a bounded text layer without OCR or temporary files."""

    if not content:
        raise SourceValidationError("edinet_pdf_empty")
    try:
        import pypdfium2 as pdfium  # type: ignore
    except ImportError:
        raise SourceValidationError("edinet_pdf_extractor_unavailable") from None
    try:
        document = pdfium.PdfDocument(io.BytesIO(content))
    except Exception:
        raise SourceValidationError("edinet_pdf_invalid") from None
    chunks: list[str] = []
    character_count = 0
    try:
        for page_index in range(min(len(document), max_pages)):
            page = document[page_index]
            text_page = None
            try:
                text_page = page.get_textpage()
                text = " ".join(text_page.get_text_range().split())
            except Exception:
                raise SourceValidationError("edinet_pdf_text_extraction_failed") from None
            finally:
                if text_page is not None:
                    text_page.close()
                page.close()
            if not text:
                continue
            remaining = max_characters - character_count
            if remaining <= 0:
                break
            chunks.append(text[:remaining])
            character_count += min(len(text), remaining)
            if character_count >= max_characters:
                break
    finally:
        document.close()
    return " ".join(chunks)


class EdinetApiV2Adapter:
    """Incremental EDINET v2 adapter restricted to tracked JPX issuers."""

    provider = "edinet"

    def __init__(
        self,
        *,
        subscription_key: str,
        issuer_code_resolver: IssuerCodeResolver,
        lookback_days: int = 2,
        timeout_seconds: float = 15.0,
        max_list_bytes: int = 10_000_000,
        max_pdf_bytes: int = 25_000_000,
        max_documents_per_batch: int = 250,
        max_cursor_revisions: int = 10_000,
        pdf_text_extractor: PdfTextExtractor = extract_edinet_pdf_text,
        clock: Clock = lambda: datetime.now(timezone.utc),
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not callable(issuer_code_resolver):
            raise SourceValidationError("edinet_issuer_resolver_invalid")
        if not 0 <= lookback_days <= 7:
            raise SourceValidationError("edinet_lookback_days_invalid")
        if not 1.0 <= timeout_seconds <= 60.0:
            raise SourceValidationError("edinet_timeout_invalid")
        if not 1_000 <= max_list_bytes <= 50_000_000:
            raise SourceValidationError("edinet_list_limit_invalid")
        if not 1_000 <= max_pdf_bytes <= 100_000_000:
            raise SourceValidationError("edinet_pdf_limit_invalid")
        if not 1 <= max_documents_per_batch <= 2_000:
            raise SourceValidationError("edinet_batch_limit_invalid")
        if not 1 <= max_cursor_revisions <= 50_000:
            raise SourceValidationError("edinet_cursor_limit_invalid")
        if not callable(pdf_text_extractor) or not callable(clock):
            raise SourceValidationError("edinet_adapter_dependency_invalid")
        self._subscription_key = _required_secret(subscription_key)
        self._issuer_code_resolver = issuer_code_resolver
        self._lookback_days = lookback_days
        self._timeout_seconds = timeout_seconds
        self._max_list_bytes = max_list_bytes
        self._max_pdf_bytes = max_pdf_bytes
        self._max_documents_per_batch = max_documents_per_batch
        self._max_cursor_revisions = max_cursor_revisions
        self._pdf_text_extractor = pdf_text_extractor
        self._clock = clock
        self._transport = transport
        self._documents: dict[tuple[str, str], _EdinetDocument] = {}

    def __repr__(self) -> str:
        return (
            "EdinetApiV2Adapter(provider='edinet', "
            f"lookback_days={self._lookback_days})"
        )

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise SourceValidationError("edinet_clock_invalid")
        return value

    async def _get_bytes(
        self,
        path: str,
        *,
        params: Mapping[str, str],
        expected_content_type: str,
        maximum_bytes: int,
    ) -> bytes:
        request_params = dict(params)
        request_params["Subscription-Key"] = self._subscription_key
        try:
            async with httpx.AsyncClient(
                base_url=EDINET_API_BASE_URL,
                timeout=self._timeout_seconds,
                follow_redirects=False,
                trust_env=False,
                transport=self._transport,
            ) as client:
                async with client.stream(
                    "GET",
                    path,
                    params=request_params,
                    headers={"Accept": expected_content_type},
                ) as response:
                    if response.is_redirect:
                        raise ConnectionError("edinet_redirect_rejected")
                    if response.status_code != 200:
                        raise ConnectionError("edinet_api_unavailable")
                    content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
                    if content_type != expected_content_type:
                        raise SourceValidationError("edinet_response_content_type_invalid")
                    content_length = response.headers.get("content-length")
                    if content_length is not None:
                        try:
                            declared_length = int(content_length)
                        except ValueError:
                            raise SourceValidationError("edinet_response_length_invalid") from None
                        if declared_length < 0:
                            raise SourceValidationError("edinet_response_length_invalid")
                        if declared_length > maximum_bytes:
                            raise SourceValidationError("edinet_response_too_large")
                    chunks: list[bytes] = []
                    size = 0
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > maximum_bytes:
                            raise SourceValidationError("edinet_response_too_large")
                        chunks.append(chunk)
                    if size == 0:
                        raise SourceValidationError("edinet_response_empty")
                    return b"".join(chunks)
        except httpx.TimeoutException:
            raise TimeoutError("edinet_api_timeout") from None
        except httpx.TransportError:
            raise ConnectionError("edinet_api_unavailable") from None

    async def _list_rows(self, file_date: date) -> tuple[Mapping[str, Any], ...]:
        content = await self._get_bytes(
            "/documents.json",
            params={"date": file_date.isoformat(), "type": "2"},
            expected_content_type="application/json",
            maximum_bytes=self._max_list_bytes,
        )
        try:
            payload = json.loads(content)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise SourceValidationError("edinet_list_json_invalid") from None
        if not isinstance(payload, dict):
            raise SourceValidationError("edinet_list_contract_invalid")
        metadata = payload.get("metadata")
        results = payload.get("results")
        if (
            not isinstance(metadata, dict)
            or str(metadata.get("status")) != "200"
            or not isinstance(results, list)
            or any(not isinstance(row, dict) for row in results)
        ):
            raise SourceValidationError("edinet_list_contract_invalid")
        return tuple(results)

    async def _tracked_issuer_codes(self) -> frozenset[str]:
        try:
            values = await self._issuer_code_resolver()
        except Exception:
            raise ConnectionError("edinet_issuer_resolver_unavailable") from None
        if isinstance(values, (str, bytes)):
            raise SourceValidationError("edinet_issuer_resolver_invalid")
        normalized: set[str] = set()
        try:
            for value in values:
                normalized.add(normalize_issuer_code(value))
        except (IssuerMasterValidationError, TypeError):
            raise SourceValidationError("edinet_issuer_resolver_invalid") from None
        if len(normalized) > 2_000:
            raise SourceValidationError("edinet_issuer_resolver_limit_exceeded")
        return frozenset(normalized)

    @staticmethod
    def _cursor(cursor: dict[str, Any] | None) -> dict[str, dict[str, str]]:
        if cursor is None:
            return {}
        if not isinstance(cursor, dict) or set(cursor) != {"schema", "days"}:
            raise SourceValidationError("edinet_cursor_invalid")
        if cursor.get("schema") != 1 or not isinstance(cursor.get("days"), dict):
            raise SourceValidationError("edinet_cursor_invalid")
        decoded: dict[str, dict[str, str]] = {}
        for day, revisions in cursor["days"].items():
            try:
                date.fromisoformat(day)
            except (TypeError, ValueError):
                raise SourceValidationError("edinet_cursor_invalid") from None
            if not isinstance(revisions, dict):
                raise SourceValidationError("edinet_cursor_invalid")
            decoded_revisions: dict[str, str] = {}
            for document_id, revision in revisions.items():
                if (
                    not isinstance(document_id, str)
                    or _DOCUMENT_ID_PATTERN.fullmatch(document_id) is None
                    or not isinstance(revision, str)
                    or len(revision) != 64
                    or any(character not in "0123456789abcdef" for character in revision)
                ):
                    raise SourceValidationError("edinet_cursor_invalid")
                decoded_revisions[document_id] = revision
            decoded[day] = decoded_revisions
        return decoded

    @staticmethod
    def _decode_document(row: Mapping[str, Any], *, file_date: date) -> _EdinetDocument | None:
        issuer_code = _issuer_code(row.get("secCode"))
        if issuer_code is None:
            return None
        document_id = _document_id(row.get("docID"))
        revision_key = _revision_key(row)
        title = _canonical_text(row.get("docDescription"))
        if title is None:
            title = _canonical_text(row.get("filerName")) or "EDINET提出書類"
        doc_type_code = _canonical_text(row.get("docTypeCode")) or "unknown"
        lifecycle = _lifecycle_status(row)
        return _EdinetDocument(
            reference=SourceDocumentRef(document_id, revision_key),
            file_date=file_date,
            sequence=_sequence(row.get("seqNumber")),
            issuer_code=issuer_code,
            published_at=_published_at(row.get("submitDateTime")),
            lifecycle_status=lifecycle,
            title=title[:2_000],
            document_type=f"edinet_{doc_type_code}"[:64],
            pdf_available=_flag(row.get("pdfFlag")) and lifecycle is not SourceLifecycleStatus.WITHDRAWN,
            row={field: row.get(field) for field in _REVISION_FIELDS},
        )

    async def discover(self, cursor: dict[str, Any] | None) -> DiscoveryBatch:
        previous_days = self._cursor(cursor)
        tracked = await self._tracked_issuer_codes()
        today = self._now().astimezone(_JST).date()
        window = tuple(today - timedelta(days=offset) for offset in range(self._lookback_days, -1, -1))
        if not tracked:
            return DiscoveryBatch(
                documents=(),
                next_cursor={"schema": 1, "days": {day.isoformat(): {} for day in window}},
            )

        discovered: list[_EdinetDocument] = []
        next_days: dict[str, dict[str, str]] = {}
        for file_date in window:
            rows = await self._list_rows(file_date)
            current_revisions: dict[str, str] = {}
            previous_revisions = previous_days.get(file_date.isoformat(), {})
            for row in rows:
                document = self._decode_document(row, file_date=file_date)
                if document is None or document.issuer_code not in tracked:
                    continue
                document_id = document.reference.document_id
                revision_key = document.reference.revision_key
                existing = current_revisions.get(document_id)
                if existing is not None and existing != revision_key:
                    raise SourceValidationError("edinet_list_duplicate_document_id")
                current_revisions[document_id] = revision_key
                self._documents[(document_id, revision_key)] = document
                if previous_revisions.get(document_id) != revision_key:
                    discovered.append(document)
            next_days[file_date.isoformat()] = current_revisions

        revision_count = sum(len(revisions) for revisions in next_days.values())
        if revision_count > self._max_cursor_revisions:
            raise SourceValidationError("edinet_cursor_limit_exceeded")
        if len(discovered) > self._max_documents_per_batch:
            raise SourceValidationError("edinet_batch_limit_exceeded")
        discovered.sort(
            key=lambda document: (
                document.published_at,
                document.sequence,
                document.reference.document_id,
            )
        )
        return DiscoveryBatch(
            documents=tuple(document.reference for document in discovered),
            next_cursor={"schema": 1, "days": next_days},
        )

    async def fetch(self, document: SourceDocumentRef) -> SourcePayload:
        decoded = self._documents.get((document.document_id, document.revision_key))
        if decoded is None:
            raise SourceValidationError("edinet_document_not_discovered")
        evidence_text = _metadata_evidence(decoded)
        evidence_scope = "metadata_only"
        if decoded.pdf_available:
            raw_content = await self._get_bytes(
                f"/documents/{decoded.reference.document_id}",
                params={"type": "2"},
                expected_content_type="application/pdf",
                maximum_bytes=self._max_pdf_bytes,
            )
            try:
                extracted = await asyncio.to_thread(self._pdf_text_extractor, raw_content)
            except SourceValidationError:
                raise
            except Exception:
                raise SourceValidationError("edinet_pdf_text_extraction_failed") from None
            extracted = " ".join(extracted.split()) if isinstance(extracted, str) else ""
            if extracted:
                evidence_text = extracted[:50_000]
                evidence_scope = "pdf_text_first_40_pages"
            else:
                evidence_scope = "metadata_only_pdf_text_unavailable"
        else:
            raw_content = _canonical_json(decoded.row)
        fetched_at = self._now()
        return SourcePayload(
            document=document,
            original_url=f"{EDINET_PUBLIC_VIEWER_BASE_URL}{decoded.reference.document_id}",
            fetched_at=fetched_at,
            raw_content=raw_content,
            metadata={
                "edinet_document": decoded,
                "evidence_text": evidence_text,
                "evidence_scope": evidence_scope,
            },
        )

    def normalize(self, payload: SourcePayload) -> NormalizedSourceRecord:
        document = payload.metadata.get("edinet_document")
        if not isinstance(document, _EdinetDocument) or document.reference != payload.document:
            raise SourceValidationError("edinet_payload_contract_invalid")
        evidence_text = payload.metadata.get("evidence_text")
        evidence_scope = payload.metadata.get("evidence_scope")
        if not isinstance(evidence_text, str) or not isinstance(evidence_scope, str):
            raise SourceValidationError("edinet_payload_contract_invalid")
        row = document.row
        metadata = {
            "file_date": document.file_date.isoformat(),
            "sequence": document.sequence,
            "edinet_code": _canonical_text(row.get("edinetCode")),
            "security_code": _canonical_text(row.get("secCode")),
            "filer_name": _canonical_text(row.get("filerName")),
            "ordinance_code": _canonical_text(row.get("ordinanceCode")),
            "form_code": _canonical_text(row.get("formCode")),
            "document_type_code": _canonical_text(row.get("docTypeCode")),
            "parent_document_id": _canonical_text(row.get("parentDocID")),
            "operation_datetime": _canonical_text(row.get("opeDateTime")),
            "withdrawal_status": row.get("withdrawalStatus"),
            "document_info_edit_status": row.get("docInfoEditStatus"),
            "disclosure_status": row.get("disclosureStatus"),
            "pdf_available": document.pdf_available,
            "evidence_scope": evidence_scope,
        }
        return NormalizedSourceRecord.from_payload(
            provider=self.provider,
            payload=payload,
            title=document.title,
            document_type=document.document_type,
            published_at=document.published_at,
            lifecycle_status=document.lifecycle_status,
            issuer_codes=(document.issuer_code,),
            evidence_text=evidence_text,
            metadata=metadata,
        )

    async def health(self) -> SourceHealth:
        checked_at = self._now()
        try:
            await self._list_rows(checked_at.astimezone(_JST).date())
        except Exception:
            return SourceHealth(
                ok=False,
                checked_at=checked_at,
                detail_code="edinet_api_unavailable",
            )
        return SourceHealth(
            ok=True,
            checked_at=checked_at,
            detail_code="edinet_api_ready",
        )


def build_edinet_api_v2_adapter_from_env(
    *,
    issuer_code_resolver: IssuerCodeResolver,
    environ: Mapping[str, str] | None = None,
    clock: Clock = lambda: datetime.now(timezone.utc),
    transport: httpx.AsyncBaseTransport | None = None,
) -> EdinetApiV2Adapter:
    values = os.environ if environ is None else environ
    raw_lookback = values.get("VIBE_MARKET_MORNING_EDINET_LOOKBACK_DAYS", "2")
    try:
        lookback_days = int(raw_lookback)
    except ValueError:
        raise SourceValidationError("edinet_lookback_days_invalid") from None
    return EdinetApiV2Adapter(
        subscription_key=values.get("VIBE_MARKET_MORNING_EDINET_API_KEY", ""),
        issuer_code_resolver=issuer_code_resolver,
        lookback_days=lookback_days,
        clock=clock,
        transport=transport,
    )


__all__ = [
    "EDINET_API_BASE_URL",
    "EDINET_PUBLIC_VIEWER_BASE_URL",
    "EdinetApiV2Adapter",
    "IssuerCodeResolver",
    "build_edinet_api_v2_adapter_from_env",
    "extract_edinet_pdf_text",
]
