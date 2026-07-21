"""Production-safe adapter for the JPX TDnet API.

The paid API has two fixed JSON POST endpoints.  This adapter keeps the access
key out of persisted URLs and errors, filters the all-market index to active
watchlist issuers, captures correction/deletion histories, and only persists a
public TDnet viewer URL.  A real key must not be injected before the TDnet data
rights Gate is approved.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import io
import json
import os
import re
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlsplit

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

TDNET_API_BASE_URL = "https://api.arrowfront.jp"
TDNET_INDEX_PATH = "/tdlist"
TDNET_DOCUMENT_PATH = "/tdfile"
TDNET_PUBLIC_VIEWER_BASE_URL = "https://www.release.tdnet.info/inbs/"
TDNET_DOCUMENT_S3_HOST = "mi-bucket-tdfile.s3.ap-northeast-1.amazonaws.com"

_JST = timezone(timedelta(hours=9))
_DISCLOSURE_NUMBER_PATTERN = re.compile(r"^[0-9]{14}$")
_CURSOR_IDENTITY_PATTERN = re.compile(r"^[0-9]{14}:[1-9][0-9]?$")
_API_ISSUER_CODE_PATTERN = re.compile(r"^[0-9A-Z]{5}$")
_DISCLOSURE_ITEM_PATTERN = re.compile(r"^[0-9]{5}$")
_REVISION_FIELDS = (
    "code",
    "name",
    "disclosedDate",
    "disclosedTime",
    "handlingType",
    "disclosureNumber",
    "modifiedHistory",
    "title",
    "disclosureItems",
    "pdfGeneralFlag",
    "pdfSummaryFlag",
    "xbrlFlag",
)

IssuerCodeResolver = Callable[[], Awaitable[Iterable[str]]]
PdfTextExtractor = Callable[[bytes], str]
Clock = Callable[[], datetime]
RequestPacer = Callable[[], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class _TdnetDisclosure:
    reference: SourceDocumentRef
    issuer_code: str
    issuer_name: str
    disclosed_date: date
    published_at: datetime
    modified_history: int
    lifecycle_status: SourceLifecycleStatus
    title: str
    disclosure_items: tuple[str, ...]
    pdf_general_flag: int
    pdf_summary_flag: int
    xbrl_flag: int
    document_file_type: str | None
    download_document: bool
    row: dict[str, Any]


class _OneRequestPerSecondPacer:
    """Serialize paid API calls and reserve at least one second per request."""

    def __init__(
        self,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._monotonic = monotonic
        self._sleep = sleep
        self._lock = asyncio.Lock()
        self._next_allowed_at = 0.0

    async def __call__(self) -> None:
        async with self._lock:
            now = self._monotonic()
            delay = self._next_allowed_at - now
            if delay > 0:
                await self._sleep(delay)
            self._next_allowed_at = max(self._next_allowed_at, now) + 1.0


def _required_secret(value: str) -> str:
    if not isinstance(value, str):
        raise SourceValidationError("tdnet_access_key_invalid")
    canonical = value.strip()
    if (
        not canonical
        or len(canonical) > 512
        or any(ord(character) < 32 or ord(character) == 127 for character in canonical)
    ):
        raise SourceValidationError("tdnet_access_key_invalid")
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


def _decimal_int(
    value: Any,
    *,
    minimum: int,
    maximum: int,
    error_code: str,
) -> int:
    if isinstance(value, bool):
        raise SourceValidationError(error_code)
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str) and value.isdigit():
        parsed = int(value)
    else:
        raise SourceValidationError(error_code)
    if not minimum <= parsed <= maximum:
        raise SourceValidationError(error_code)
    return parsed


def _flag(value: Any, *, maximum: int = 1) -> int:
    return _decimal_int(
        value,
        minimum=0,
        maximum=maximum,
        error_code="tdnet_index_row_invalid",
    )


def _issuer_code(value: Any) -> str | None:
    canonical = _canonical_text(value)
    if canonical is None:
        return None
    canonical = canonical.upper()
    if _API_ISSUER_CODE_PATTERN.fullmatch(canonical) is None:
        return None
    try:
        return normalize_issuer_code(canonical[:4])
    except IssuerMasterValidationError:
        return None


def _published_at(disclosed_date: Any, disclosed_time: Any) -> tuple[date, datetime]:
    date_text = _canonical_text(disclosed_date)
    time_text = _canonical_text(disclosed_time)
    if date_text is None or time_text is None:
        raise SourceValidationError("tdnet_index_row_invalid")
    try:
        parsed_date = date.fromisoformat(date_text)
        parsed = datetime.fromisoformat(f"{date_text}T{time_text}")
    except ValueError as error:
        raise SourceValidationError("tdnet_index_row_invalid") from error
    return parsed_date, parsed.replace(tzinfo=_JST)


def _summary_flag(row: Mapping[str, Any]) -> int:
    canonical = row.get("pdfSummaryFlag")
    legacy_typo = row.get("pdfSumaryFlag")
    if canonical is not None and legacy_typo is not None and canonical != legacy_typo:
        raise SourceValidationError("tdnet_index_row_invalid")
    return _flag(canonical if canonical is not None else legacy_typo)


def _lifecycle(value: Any) -> SourceLifecycleStatus:
    if value is None:
        return SourceLifecycleStatus.ACTIVE
    if value == "revision":
        return SourceLifecycleStatus.CORRECTED
    if value == "delete":
        return SourceLifecycleStatus.WITHDRAWN
    raise SourceValidationError("tdnet_index_row_invalid")


def _revision_key(row: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(row)).hexdigest()


def _metadata_evidence(document: _TdnetDisclosure) -> str:
    state = {
        SourceLifecycleStatus.ACTIVE: "開示中",
        SourceLifecycleStatus.CORRECTED: "訂正",
        SourceLifecycleStatus.WITHDRAWN: "削除",
    }[document.lifecycle_status]
    return " ".join(
        (
            "TDnet APIメタデータ。",
            f"状態: {state}。",
            f"会社: {document.issuer_name}。",
            f"開示: {document.title}。",
            f"開示日時: {document.published_at.isoformat()}。",
            f"開示履歴番号: {document.modified_history}。",
        )
    )


def _bounded_evidence(metadata: str, extracted: str) -> str:
    canonical = " ".join(extracted.split())
    if not canonical:
        return metadata
    prefix = f"{metadata} 本文抜粋: "
    return f"{prefix}{canonical[: max(0, 50_000 - len(prefix))]}"


def extract_tdnet_pdf_text(
    content: bytes,
    *,
    max_pages: int = 40,
    max_characters: int = 49_000,
) -> str:
    """Extract a bounded PDF text layer without OCR or temporary files."""

    if not content:
        raise SourceValidationError("tdnet_pdf_empty")
    try:
        import pypdfium2 as pdfium  # type: ignore
    except ImportError:
        raise SourceValidationError("tdnet_pdf_extractor_unavailable") from None
    try:
        document = pdfium.PdfDocument(io.BytesIO(content))
    except Exception:
        raise SourceValidationError("tdnet_pdf_invalid") from None
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
                raise SourceValidationError("tdnet_pdf_text_extraction_failed") from None
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


class TdnetApiAdapter:
    """Incremental TDnet adapter restricted to tracked JPX issuers."""

    provider = "tdnet"

    def __init__(
        self,
        *,
        access_key: str,
        issuer_code_resolver: IssuerCodeResolver,
        lookback_days: int = 2,
        timeout_seconds: float = 20.0,
        max_index_bytes: int = 25_000_000,
        max_document_response_bytes: int = 12_000_000,
        max_pdf_bytes: int = 50_000_000,
        max_documents_per_batch: int = 250,
        max_cursor_revisions: int = 20_000,
        pdf_text_extractor: PdfTextExtractor = extract_tdnet_pdf_text,
        clock: Clock = lambda: datetime.now(timezone.utc),
        request_pacer: RequestPacer | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not callable(issuer_code_resolver):
            raise SourceValidationError("tdnet_issuer_resolver_invalid")
        if not 0 <= lookback_days <= 7:
            raise SourceValidationError("tdnet_lookback_days_invalid")
        if not 1.0 <= timeout_seconds <= 60.0:
            raise SourceValidationError("tdnet_timeout_invalid")
        if not 1_000 <= max_index_bytes <= 100_000_000:
            raise SourceValidationError("tdnet_index_limit_invalid")
        if not 1_000 <= max_document_response_bytes <= 25_000_000:
            raise SourceValidationError("tdnet_document_response_limit_invalid")
        if not 1_000 <= max_pdf_bytes <= 100_000_000:
            raise SourceValidationError("tdnet_pdf_limit_invalid")
        if not 1 <= max_documents_per_batch <= 2_000:
            raise SourceValidationError("tdnet_batch_limit_invalid")
        if not 1 <= max_cursor_revisions <= 50_000:
            raise SourceValidationError("tdnet_cursor_limit_invalid")
        if not callable(pdf_text_extractor) or not callable(clock):
            raise SourceValidationError("tdnet_adapter_dependency_invalid")
        if request_pacer is not None and not callable(request_pacer):
            raise SourceValidationError("tdnet_adapter_dependency_invalid")
        self._access_key = _required_secret(access_key)
        self._issuer_code_resolver = issuer_code_resolver
        self._lookback_days = lookback_days
        self._timeout_seconds = timeout_seconds
        self._max_index_bytes = max_index_bytes
        self._max_document_response_bytes = max_document_response_bytes
        self._max_pdf_bytes = max_pdf_bytes
        self._max_documents_per_batch = max_documents_per_batch
        self._max_cursor_revisions = max_cursor_revisions
        self._pdf_text_extractor = pdf_text_extractor
        self._clock = clock
        self._request_pacer = request_pacer or _OneRequestPerSecondPacer()
        self._transport = transport
        self._documents: dict[tuple[str, str], _TdnetDisclosure] = {}

    def __repr__(self) -> str:
        return f"TdnetApiAdapter(provider='tdnet', lookback_days={self._lookback_days})"

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise SourceValidationError("tdnet_clock_invalid")
        return value

    @staticmethod
    async def _read_response(
        response: httpx.Response,
        *,
        expected_content_types: frozenset[str],
        maximum_bytes: int,
        error_prefix: str,
    ) -> bytes:
        if response.is_redirect:
            raise ConnectionError(f"{error_prefix}_redirect_rejected")
        if response.status_code != 200:
            raise ConnectionError(f"{error_prefix}_unavailable")
        content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
        if content_type not in expected_content_types:
            raise SourceValidationError(f"{error_prefix}_content_type_invalid")
        content_length = response.headers.get("content-length")
        if content_length is not None:
            try:
                declared_length = int(content_length)
            except ValueError:
                raise SourceValidationError(f"{error_prefix}_length_invalid") from None
            if declared_length < 0:
                raise SourceValidationError(f"{error_prefix}_length_invalid")
            if declared_length > maximum_bytes:
                raise SourceValidationError(f"{error_prefix}_too_large")
        chunks: list[bytes] = []
        size = 0
        async for chunk in response.aiter_bytes():
            size += len(chunk)
            if size > maximum_bytes:
                raise SourceValidationError(f"{error_prefix}_too_large")
            chunks.append(chunk)
        if size == 0:
            raise SourceValidationError(f"{error_prefix}_empty")
        return b"".join(chunks)

    async def _post_json(self, path: str, body: Mapping[str, Any], *, maximum_bytes: int) -> Any:
        await self._request_pacer()
        payload = dict(body)
        payload["accessKey"] = self._access_key
        try:
            async with httpx.AsyncClient(
                base_url=TDNET_API_BASE_URL,
                timeout=self._timeout_seconds,
                follow_redirects=False,
                trust_env=False,
                transport=self._transport,
            ) as client:
                async with client.stream(
                    "POST",
                    path,
                    json=payload,
                    headers={
                        "Accept": "application/json",
                        "x-api-key": self._access_key,
                    },
                ) as response:
                    content = await self._read_response(
                        response,
                        expected_content_types=frozenset({"application/json"}),
                        maximum_bytes=maximum_bytes,
                        error_prefix="tdnet_api",
                    )
        except httpx.TimeoutException:
            raise TimeoutError("tdnet_api_timeout") from None
        except httpx.TransportError:
            raise ConnectionError("tdnet_api_unavailable") from None
        try:
            return json.loads(content)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise SourceValidationError("tdnet_api_json_invalid") from None

    async def _index_rows(
        self,
        *,
        date_from: date,
        date_to: date,
        include_history: bool,
    ) -> tuple[Mapping[str, Any], ...]:
        payload = await self._post_json(
            TDNET_INDEX_PATH,
            {
                "code": None,
                "dateFrom": date_from.isoformat(),
                "dateTo": date_to.isoformat(),
                "editDelFlag": "1" if include_history else None,
            },
            maximum_bytes=self._max_index_bytes,
        )
        if not isinstance(payload, dict):
            raise SourceValidationError("tdnet_index_contract_invalid")
        status_code = str(payload.get("statusCode"))
        if status_code == "206":
            raise SourceValidationError("tdnet_index_truncated")
        rows = payload.get("publiclyList")
        if status_code != "200" or not isinstance(rows, list) or any(
            not isinstance(row, dict) for row in rows
        ):
            raise SourceValidationError("tdnet_index_contract_invalid")
        count = _decimal_int(
            payload.get("count"),
            minimum=0,
            maximum=10_000,
            error_code="tdnet_index_contract_invalid",
        )
        if count != len(rows):
            raise SourceValidationError("tdnet_index_contract_invalid")
        return tuple(rows)

    async def _tracked_issuer_codes(self) -> frozenset[str]:
        try:
            values = await self._issuer_code_resolver()
        except Exception:
            raise ConnectionError("tdnet_issuer_resolver_unavailable") from None
        if isinstance(values, (str, bytes)):
            raise SourceValidationError("tdnet_issuer_resolver_invalid")
        normalized: set[str] = set()
        try:
            for value in values:
                normalized.add(normalize_issuer_code(value))
        except (IssuerMasterValidationError, TypeError):
            raise SourceValidationError("tdnet_issuer_resolver_invalid") from None
        if len(normalized) > 2_000:
            raise SourceValidationError("tdnet_issuer_resolver_limit_exceeded")
        return frozenset(normalized)

    @staticmethod
    def _cursor(cursor: dict[str, Any] | None) -> dict[str, dict[str, str]]:
        if cursor is None:
            return {}
        if not isinstance(cursor, dict) or set(cursor) != {"schema", "days"}:
            raise SourceValidationError("tdnet_cursor_invalid")
        if cursor.get("schema") != 1 or not isinstance(cursor.get("days"), dict):
            raise SourceValidationError("tdnet_cursor_invalid")
        decoded: dict[str, dict[str, str]] = {}
        for day, revisions in cursor["days"].items():
            try:
                date.fromisoformat(day)
            except (TypeError, ValueError):
                raise SourceValidationError("tdnet_cursor_invalid") from None
            if not isinstance(revisions, dict):
                raise SourceValidationError("tdnet_cursor_invalid")
            decoded_revisions: dict[str, str] = {}
            for identity, revision in revisions.items():
                if (
                    not isinstance(identity, str)
                    or _CURSOR_IDENTITY_PATTERN.fullmatch(identity) is None
                    or not isinstance(revision, str)
                    or len(revision) != 64
                    or any(character not in "0123456789abcdef" for character in revision)
                ):
                    raise SourceValidationError("tdnet_cursor_invalid")
                decoded_revisions[identity] = revision
            decoded[day] = decoded_revisions
        return decoded

    @staticmethod
    def _decode_document(row: Mapping[str, Any]) -> _TdnetDisclosure | None:
        issuer_code = _issuer_code(row.get("code"))
        if issuer_code is None:
            return None
        disclosure_number = _canonical_text(row.get("disclosureNumber"))
        if (
            disclosure_number is None
            or _DISCLOSURE_NUMBER_PATTERN.fullmatch(disclosure_number) is None
        ):
            raise SourceValidationError("tdnet_index_row_invalid")
        modified_history = _decimal_int(
            row.get("modifiedHistory"),
            minimum=1,
            maximum=99,
            error_code="tdnet_index_row_invalid",
        )
        disclosed_date, published_at = _published_at(
            row.get("disclosedDate"),
            row.get("disclosedTime"),
        )
        issuer_name = _canonical_text(row.get("name"))
        title = _canonical_text(row.get("title"))
        if issuer_name is None or title is None:
            raise SourceValidationError("tdnet_index_row_invalid")
        items = row.get("disclosureItems")
        if not isinstance(items, list) or len(items) > 100:
            raise SourceValidationError("tdnet_index_row_invalid")
        disclosure_items: list[str] = []
        for item in items:
            canonical = _canonical_text(item)
            if canonical is None or _DISCLOSURE_ITEM_PATTERN.fullmatch(canonical) is None:
                raise SourceValidationError("tdnet_index_row_invalid")
            disclosure_items.append(canonical)
        if len(set(disclosure_items)) != len(disclosure_items):
            raise SourceValidationError("tdnet_index_row_invalid")
        general_flag = _flag(row.get("pdfGeneralFlag"), maximum=2)
        summary_flag = _summary_flag(row)
        xbrl_flag = _flag(row.get("xbrlFlag"))
        lifecycle = _lifecycle(row.get("handlingType"))
        canonical_row = {
            "code": _canonical_text(row.get("code")),
            "name": issuer_name,
            "disclosedDate": disclosed_date.isoformat(),
            "disclosedTime": published_at.strftime("%H:%M:%S"),
            "handlingType": row.get("handlingType"),
            "disclosureNumber": disclosure_number,
            "modifiedHistory": str(modified_history),
            "title": title,
            "disclosureItems": disclosure_items,
            "pdfGeneralFlag": str(general_flag),
            "pdfSummaryFlag": str(summary_flag),
            "xbrlFlag": str(xbrl_flag),
        }
        document_file_type = "g" if general_flag else "s" if summary_flag else None
        return _TdnetDisclosure(
            reference=SourceDocumentRef(disclosure_number, _revision_key(canonical_row)),
            issuer_code=issuer_code,
            issuer_name=issuer_name[:240],
            disclosed_date=disclosed_date,
            published_at=published_at,
            modified_history=modified_history,
            lifecycle_status=lifecycle,
            title=title[:2_000],
            disclosure_items=tuple(disclosure_items),
            pdf_general_flag=general_flag,
            pdf_summary_flag=summary_flag,
            xbrl_flag=xbrl_flag,
            document_file_type=document_file_type,
            download_document=False,
            row=canonical_row,
        )

    async def discover(self, cursor: dict[str, Any] | None) -> DiscoveryBatch:
        previous_days = self._cursor(cursor)
        tracked = await self._tracked_issuer_codes()
        today = self._now().astimezone(_JST).date()
        date_from = today - timedelta(days=self._lookback_days)
        window = tuple(
            date_from + timedelta(days=offset)
            for offset in range((today - date_from).days + 1)
        )
        if not tracked:
            self._documents = {}
            return DiscoveryBatch(
                documents=(),
                next_cursor={"schema": 1, "days": {day.isoformat(): {} for day in window}},
            )

        current_rows = await self._index_rows(
            date_from=date_from,
            date_to=today,
            include_history=False,
        )
        history_rows = await self._index_rows(
            date_from=date_from,
            date_to=today,
            include_history=True,
        )
        decoded_by_identity: dict[tuple[str, int], _TdnetDisclosure] = {}
        for row in (*current_rows, *history_rows):
            row_issuer_code = _issuer_code(row.get("code"))
            if row_issuer_code is None or row_issuer_code not in tracked:
                continue
            document = self._decode_document(row)
            assert document is not None
            if not date_from <= document.disclosed_date <= today:
                raise SourceValidationError("tdnet_index_row_outside_window")
            identity = (document.reference.document_id, document.modified_history)
            existing = decoded_by_identity.get(identity)
            if existing is not None and existing.row != document.row:
                raise SourceValidationError("tdnet_index_duplicate_revision_conflict")
            decoded_by_identity[identity] = document

        latest_history: dict[str, int] = {}
        latest_lifecycle: dict[str, SourceLifecycleStatus] = {}
        for document in decoded_by_identity.values():
            document_id = document.reference.document_id
            if document.modified_history >= latest_history.get(document_id, 0):
                latest_history[document_id] = document.modified_history
                latest_lifecycle[document_id] = document.lifecycle_status

        documents: list[_TdnetDisclosure] = []
        next_days: dict[str, dict[str, str]] = {
            day.isoformat(): {} for day in window
        }
        discovered: list[_TdnetDisclosure] = []
        for document in decoded_by_identity.values():
            document_id = document.reference.document_id
            can_download = (
                document.modified_history == latest_history[document_id]
                and latest_lifecycle[document_id] is not SourceLifecycleStatus.WITHDRAWN
                and document.lifecycle_status is not SourceLifecycleStatus.WITHDRAWN
                and document.document_file_type is not None
            )
            prepared = replace(document, download_document=can_download)
            documents.append(prepared)
            day = prepared.disclosed_date.isoformat()
            identity = f"{document_id}:{prepared.modified_history}"
            next_days[day][identity] = prepared.reference.revision_key
            previous = previous_days.get(day, {}).get(identity)
            if previous != prepared.reference.revision_key:
                discovered.append(prepared)

        revision_count = sum(len(revisions) for revisions in next_days.values())
        if revision_count > self._max_cursor_revisions:
            raise SourceValidationError("tdnet_cursor_limit_exceeded")
        if len(discovered) > self._max_documents_per_batch:
            raise SourceValidationError("tdnet_batch_limit_exceeded")
        discovered.sort(
            key=lambda document: (
                document.published_at,
                document.reference.document_id,
                document.modified_history,
            )
        )
        self._documents = {
            (document.reference.document_id, document.reference.revision_key): document
            for document in documents
        }
        return DiscoveryBatch(
            documents=tuple(document.reference for document in discovered),
            next_cursor={"schema": 1, "days": next_days},
        )

    @staticmethod
    def _decode_base64(value: Any, *, maximum_bytes: int) -> bytes:
        if not isinstance(value, (str, bytes)):
            raise SourceValidationError("tdnet_document_base64_invalid")
        try:
            encoded = value.encode("ascii") if isinstance(value, str) else value
        except UnicodeEncodeError:
            raise SourceValidationError("tdnet_document_base64_invalid") from None
        encoded = b"".join(encoded.split())
        maximum_encoded = ((maximum_bytes + 2) // 3) * 4
        if not encoded or len(encoded) > maximum_encoded:
            raise SourceValidationError("tdnet_document_too_large")
        try:
            decoded = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError):
            raise SourceValidationError("tdnet_document_base64_invalid") from None
        if not decoded:
            raise SourceValidationError("tdnet_document_empty")
        if len(decoded) > maximum_bytes:
            raise SourceValidationError("tdnet_document_too_large")
        return decoded

    @staticmethod
    def _validated_s3_url(value: Any) -> str:
        if not isinstance(value, str) or not value or len(value) > 4_096:
            raise SourceValidationError("tdnet_document_url_invalid")
        try:
            parsed = urlsplit(value)
            port = parsed.port
        except ValueError:
            raise SourceValidationError("tdnet_document_url_invalid") from None
        if (
            parsed.scheme != "https"
            or parsed.hostname != TDNET_DOCUMENT_S3_HOST
            or parsed.username is not None
            or parsed.password is not None
            or port not in (None, 443)
            or not parsed.path.startswith("/")
            or parsed.path == "/"
            or parsed.fragment
        ):
            raise SourceValidationError("tdnet_document_url_invalid")
        return value

    async def _download_s3_base64(self, url: str) -> bytes:
        maximum_encoded = ((self._max_pdf_bytes + 2) // 3) * 4 + 4_096
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout_seconds,
                follow_redirects=False,
                trust_env=False,
                transport=self._transport,
            ) as client:
                async with client.stream(
                    "GET",
                    url,
                    headers={"Accept": "text/plain, application/octet-stream"},
                ) as response:
                    encoded = await self._read_response(
                        response,
                        expected_content_types=frozenset(
                            {"text/plain", "application/octet-stream", "binary/octet-stream"}
                        ),
                        maximum_bytes=maximum_encoded,
                        error_prefix="tdnet_document_file",
                    )
        except httpx.TimeoutException:
            raise TimeoutError("tdnet_document_file_timeout") from None
        except httpx.TransportError:
            raise ConnectionError("tdnet_document_file_unavailable") from None
        return self._decode_base64(encoded, maximum_bytes=self._max_pdf_bytes)

    async def _fetch_pdf(self, document: _TdnetDisclosure) -> tuple[bytes, str]:
        assert document.document_file_type is not None
        payload = await self._post_json(
            TDNET_DOCUMENT_PATH,
            {
                "disclosureNumber": document.reference.document_id,
                "fileTypeFlag": document.document_file_type,
            },
            maximum_bytes=self._max_document_response_bytes,
        )
        if not isinstance(payload, dict) or str(payload.get("statusCode")) != "200":
            raise SourceValidationError("tdnet_document_contract_invalid")
        response_type = payload.get("responseType")
        if response_type == "1":
            if payload.get("fileUrl") is not None:
                raise SourceValidationError("tdnet_document_contract_invalid")
            content = self._decode_base64(
                payload.get("fileData"),
                maximum_bytes=self._max_pdf_bytes,
            )
            delivery = "inline_base64"
        elif response_type == "2":
            if payload.get("fileData") is not None:
                raise SourceValidationError("tdnet_document_contract_invalid")
            url = self._validated_s3_url(payload.get("fileUrl"))
            content = await self._download_s3_base64(url)
            delivery = "s3_one_time_base64"
        else:
            raise SourceValidationError("tdnet_document_contract_invalid")
        if not content.startswith(b"%PDF-"):
            raise SourceValidationError("tdnet_pdf_invalid")
        return content, delivery

    async def fetch(self, document: SourceDocumentRef) -> SourcePayload:
        decoded = self._documents.get((document.document_id, document.revision_key))
        if decoded is None:
            raise SourceValidationError("tdnet_document_not_discovered")
        metadata_evidence = _metadata_evidence(decoded)
        evidence_text = metadata_evidence
        evidence_scope = "metadata_only"
        document_delivery = "not_requested"
        if decoded.download_document:
            raw_content, document_delivery = await self._fetch_pdf(decoded)
            try:
                extracted = await asyncio.to_thread(self._pdf_text_extractor, raw_content)
            except SourceValidationError:
                raise
            except Exception:
                raise SourceValidationError("tdnet_pdf_text_extraction_failed") from None
            if not isinstance(extracted, str):
                raise SourceValidationError("tdnet_pdf_text_extraction_failed")
            evidence_text = _bounded_evidence(metadata_evidence, extracted)
            evidence_scope = (
                "metadata_and_pdf_text_first_40_pages"
                if " ".join(extracted.split())
                else "metadata_only_pdf_text_unavailable"
            )
        else:
            raw_content = _canonical_json(decoded.row)
        return SourcePayload(
            document=document,
            original_url=(
                f"{TDNET_PUBLIC_VIEWER_BASE_URL}1401{decoded.reference.document_id}.pdf"
            ),
            fetched_at=self._now(),
            raw_content=raw_content,
            metadata={
                "tdnet_document": decoded,
                "evidence_text": evidence_text,
                "evidence_scope": evidence_scope,
                "document_delivery": document_delivery,
            },
        )

    def normalize(self, payload: SourcePayload) -> NormalizedSourceRecord:
        document = payload.metadata.get("tdnet_document")
        if not isinstance(document, _TdnetDisclosure) or document.reference != payload.document:
            raise SourceValidationError("tdnet_payload_contract_invalid")
        evidence_text = payload.metadata.get("evidence_text")
        evidence_scope = payload.metadata.get("evidence_scope")
        document_delivery = payload.metadata.get("document_delivery")
        if (
            not isinstance(evidence_text, str)
            or not isinstance(evidence_scope, str)
            or not isinstance(document_delivery, str)
        ):
            raise SourceValidationError("tdnet_payload_contract_invalid")
        return NormalizedSourceRecord.from_payload(
            provider=self.provider,
            payload=payload,
            title=document.title,
            document_type="tdnet_disclosure",
            published_at=document.published_at,
            lifecycle_status=document.lifecycle_status,
            issuer_codes=(document.issuer_code,),
            evidence_text=evidence_text,
            metadata={
                "issuer_name": document.issuer_name,
                "disclosed_date": document.disclosed_date.isoformat(),
                "modified_history": document.modified_history,
                "handling_type": document.row["handlingType"],
                "disclosure_items": list(document.disclosure_items),
                "pdf_general_flag": document.pdf_general_flag,
                "pdf_summary_flag": document.pdf_summary_flag,
                "xbrl_flag": document.xbrl_flag,
                "document_file_type": document.document_file_type,
                "document_delivery": document_delivery,
                "evidence_scope": evidence_scope,
            },
        )

    async def health(self) -> SourceHealth:
        checked_at = self._now()
        day = checked_at.astimezone(_JST).date()
        try:
            await self._index_rows(
                date_from=day,
                date_to=day,
                include_history=False,
            )
        except Exception:
            return SourceHealth(
                ok=False,
                checked_at=checked_at,
                detail_code="tdnet_api_unavailable",
            )
        return SourceHealth(
            ok=True,
            checked_at=checked_at,
            detail_code="tdnet_api_ready",
        )


def build_tdnet_api_adapter_from_env(
    *,
    issuer_code_resolver: IssuerCodeResolver,
    environ: Mapping[str, str] | None = None,
    clock: Clock = lambda: datetime.now(timezone.utc),
    request_pacer: RequestPacer | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> TdnetApiAdapter:
    values = os.environ if environ is None else environ
    raw_lookback = values.get("VIBE_MARKET_MORNING_TDNET_LOOKBACK_DAYS", "2")
    try:
        lookback_days = int(raw_lookback)
    except ValueError:
        raise SourceValidationError("tdnet_lookback_days_invalid") from None
    return TdnetApiAdapter(
        access_key=values.get("VIBE_MARKET_MORNING_TDNET_API_KEY", ""),
        issuer_code_resolver=issuer_code_resolver,
        lookback_days=lookback_days,
        clock=clock,
        request_pacer=request_pacer,
        transport=transport,
    )


__all__ = [
    "TDNET_API_BASE_URL",
    "TDNET_DOCUMENT_S3_HOST",
    "TDNET_PUBLIC_VIEWER_BASE_URL",
    "IssuerCodeResolver",
    "TdnetApiAdapter",
    "build_tdnet_api_adapter_from_env",
    "extract_tdnet_pdf_text",
]
