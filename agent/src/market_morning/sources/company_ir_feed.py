"""Production-safe, per-issuer company IR feed adapter.

Company IR sites do not share one stable API.  The core therefore owns the
network and persistence boundary while deployment code supplies one
side-effect-free index parser per approved issuer.  Every adapter instance has
one provider name, one issuer, one exact HTTPS/path allowlist and one health
state.  A broken company site cannot silently fall back to another issuer or
ask the model to invent missing content.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import re
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from html.parser import HTMLParser
from itertools import islice
from typing import Any
from urllib.parse import unquote, urlsplit, urlunsplit

import httpx

from src.market_morning.http_reachability import (
    AddressResolver,
    DEFAULT_USER_AGENT,
    MAX_SOURCE_URL_LENGTH,
    SourceReachabilityUnavailable,
    all_source_addresses_are_public,
    normalize_source_hostname,
    resolve_host_addresses,
)
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

_PROVIDER_KEY = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
_DOCUMENT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,190}$")
_DOCUMENT_TYPE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_MEDIA_TYPE = re.compile(r"^[a-z0-9!#$&^_.+-]+/[a-z0-9!#$&^_.+-]+$")
_HASH = re.compile(r"^[0-9a-f]{64}$")
_CHARSET = re.compile(r"charset\s*=\s*[\"']?([^;\"'\s]+)", re.IGNORECASE)
_PDF_MEDIA_TYPES = frozenset({"application/pdf"})
_HTML_MEDIA_TYPES = frozenset({"text/html", "application/xhtml+xml"})
_IGNORED_HTML_ELEMENTS = frozenset(
    {"script", "style", "noscript", "template", "svg", "canvas"}
)
_HTML_BREAK_ELEMENTS = frozenset(
    {
        "address",
        "article",
        "aside",
        "br",
        "div",
        "footer",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "li",
        "main",
        "nav",
        "p",
        "section",
        "table",
        "td",
        "th",
        "tr",
    }
)

IssuerCodeResolver = Callable[[], Awaitable[Iterable[str]]]
Clock = Callable[[], datetime]
PdfTextExtractor = Callable[[bytes], str]
HtmlTextExtractor = Callable[[bytes, str | None], str]


class CompanyIrContentKind(StrEnum):
    PDF = "pdf"
    HTML = "html"
    METADATA = "metadata"


@dataclass(frozen=True, slots=True)
class _ApprovedBase:
    host: str
    path: str
    url: str


def _canonical_text(
    value: object,
    *,
    error_code: str,
    maximum: int,
) -> str:
    if not isinstance(value, str):
        raise SourceValidationError(error_code)
    canonical = " ".join(value.split())
    if (
        not canonical
        or len(canonical) > maximum
        or any(ord(character) < 32 or ord(character) == 127 for character in canonical)
    ):
        raise SourceValidationError(error_code)
    return canonical


def _path_is_safe(path: str) -> bool:
    decoded = unquote(path)
    if any(ord(character) < 32 or ord(character) == 127 for character in decoded):
        return False
    return all(segment not in {".", ".."} for segment in decoded.split("/"))


def _approved_base(value: object) -> _ApprovedBase:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > MAX_SOURCE_URL_LENGTH
        or any(ord(character) < 32 for character in value)
    ):
        raise SourceValidationError("company_ir_allowed_base_url_invalid")
    try:
        parsed = urlsplit(value)
        host = normalize_source_hostname(parsed.hostname or "")
        port = parsed.port
    except (TypeError, UnicodeError, ValueError):
        raise SourceValidationError("company_ir_allowed_base_url_invalid") from None
    path = parsed.path or "/"
    if (
        parsed.scheme.lower() != "https"
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or parsed.query
        or parsed.fragment
        or not path.startswith("/")
        or not path.endswith("/")
        or not _path_is_safe(path)
    ):
        raise SourceValidationError("company_ir_allowed_base_url_invalid")
    return _ApprovedBase(
        host=host,
        path=path,
        url=urlunsplit(("https", host, path, "", "")),
    )


def _canonical_approved_url(
    value: object,
    *,
    approved_bases: Sequence[_ApprovedBase],
) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > MAX_SOURCE_URL_LENGTH
        or any(ord(character) < 32 for character in value)
    ):
        raise SourceValidationError("company_ir_url_not_approved")
    try:
        parsed = urlsplit(value)
        host = normalize_source_hostname(parsed.hostname or "")
        port = parsed.port
    except (TypeError, UnicodeError, ValueError):
        raise SourceValidationError("company_ir_url_not_approved") from None
    path = parsed.path or "/"
    if (
        parsed.scheme.lower() != "https"
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or parsed.query
        or parsed.fragment
        or not path.startswith("/")
        or not _path_is_safe(path)
        or not any(base.host == host and path.startswith(base.path) for base in approved_bases)
    ):
        raise SourceValidationError("company_ir_url_not_approved")
    return urlunsplit(("https", host, path, "", ""))


@dataclass(frozen=True, slots=True)
class CompanyIrApprovedFeedConfig:
    """One issuer's deployment-approved public IR feed boundary."""

    issuer_code: str
    provider_key: str
    index_url: str
    allowed_base_urls: tuple[str, ...]
    index_content_types: tuple[str, ...] = ("application/json",)
    _approved_bases: tuple[_ApprovedBase, ...] = field(
        init=False,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        try:
            issuer_code = normalize_issuer_code(self.issuer_code)
        except (IssuerMasterValidationError, TypeError):
            raise SourceValidationError("company_ir_issuer_code_invalid") from None
        provider_key = _canonical_text(
            self.provider_key,
            error_code="company_ir_provider_key_invalid",
            maximum=40,
        ).lower()
        if _PROVIDER_KEY.fullmatch(provider_key) is None:
            raise SourceValidationError("company_ir_provider_key_invalid")
        if (
            not isinstance(self.allowed_base_urls, tuple)
            or not 1 <= len(self.allowed_base_urls) <= 8
        ):
            raise SourceValidationError("company_ir_allowed_base_urls_invalid")
        bases = tuple(_approved_base(value) for value in self.allowed_base_urls)
        if len({base.url for base in bases}) != len(bases):
            raise SourceValidationError("company_ir_allowed_base_urls_invalid")
        index_url = _canonical_approved_url(
            self.index_url,
            approved_bases=bases,
        )
        if (
            not isinstance(self.index_content_types, tuple)
            or not 1 <= len(self.index_content_types) <= 8
        ):
            raise SourceValidationError("company_ir_index_content_types_invalid")
        content_types: list[str] = []
        for value in self.index_content_types:
            if not isinstance(value, str):
                raise SourceValidationError("company_ir_index_content_types_invalid")
            canonical = value.strip().lower()
            if _MEDIA_TYPE.fullmatch(canonical) is None:
                raise SourceValidationError("company_ir_index_content_types_invalid")
            content_types.append(canonical)
        if len(set(content_types)) != len(content_types):
            raise SourceValidationError("company_ir_index_content_types_invalid")
        object.__setattr__(self, "issuer_code", issuer_code)
        object.__setattr__(self, "provider_key", provider_key)
        object.__setattr__(self, "index_url", index_url)
        object.__setattr__(self, "allowed_base_urls", tuple(base.url for base in bases))
        object.__setattr__(self, "index_content_types", tuple(content_types))
        object.__setattr__(self, "_approved_bases", bases)

    @property
    def provider(self) -> str:
        return f"company_ir_{self.provider_key}"

    def approved_url(self, value: object) -> str:
        return _canonical_approved_url(value, approved_bases=self._approved_bases)


@dataclass(frozen=True, slots=True)
class CompanyIrIndexEntry:
    """Typed output from one deployment-owned company index parser."""

    document_id: str
    provider_revision: str
    original_url: str
    title: str
    document_type: str
    published_at: datetime
    lifecycle_status: SourceLifecycleStatus
    content_kind: CompanyIrContentKind

    def __post_init__(self) -> None:
        document_id = _canonical_text(
            self.document_id,
            error_code="company_ir_index_entry_invalid",
            maximum=191,
        )
        if _DOCUMENT_ID.fullmatch(document_id) is None:
            raise SourceValidationError("company_ir_index_entry_invalid")
        revision = _canonical_text(
            self.provider_revision,
            error_code="company_ir_index_entry_invalid",
            maximum=255,
        )
        original_url = _canonical_text(
            self.original_url,
            error_code="company_ir_index_entry_invalid",
            maximum=MAX_SOURCE_URL_LENGTH,
        )
        title = _canonical_text(
            self.title,
            error_code="company_ir_index_entry_invalid",
            maximum=2_000,
        )
        document_type = _canonical_text(
            self.document_type,
            error_code="company_ir_index_entry_invalid",
            maximum=64,
        ).lower()
        if _DOCUMENT_TYPE.fullmatch(document_type) is None:
            raise SourceValidationError("company_ir_index_entry_invalid")
        if (
            not isinstance(self.published_at, datetime)
            or self.published_at.tzinfo is None
            or self.published_at.utcoffset() is None
            or not isinstance(self.lifecycle_status, SourceLifecycleStatus)
            or not isinstance(self.content_kind, CompanyIrContentKind)
        ):
            raise SourceValidationError("company_ir_index_entry_invalid")
        object.__setattr__(self, "document_id", document_id)
        object.__setattr__(self, "provider_revision", revision)
        object.__setattr__(self, "original_url", original_url)
        object.__setattr__(self, "title", title)
        object.__setattr__(self, "document_type", document_type)


IndexParser = Callable[
    [bytes, str, CompanyIrApprovedFeedConfig],
    Iterable[CompanyIrIndexEntry],
]


def parse_company_ir_json_index(
    content: bytes,
    media_type: str,
    config: CompanyIrApprovedFeedConfig,
) -> tuple[CompanyIrIndexEntry, ...]:
    """Parse the strict normalized JSON contract offered to deployments."""

    if media_type != "application/json":
        raise SourceValidationError("company_ir_index_content_type_invalid")
    try:
        payload = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise SourceValidationError("company_ir_index_json_invalid") from None
    if not isinstance(payload, dict) or set(payload) != {
        "schema_version",
        "issuer_code",
        "items",
    }:
        raise SourceValidationError("company_ir_index_contract_invalid")
    if payload["schema_version"] != 1 or isinstance(payload["schema_version"], bool):
        raise SourceValidationError("company_ir_index_contract_invalid")
    try:
        issuer_code = normalize_issuer_code(payload["issuer_code"])
    except (IssuerMasterValidationError, TypeError):
        raise SourceValidationError("company_ir_index_contract_invalid") from None
    if issuer_code != config.issuer_code or not isinstance(payload["items"], list):
        raise SourceValidationError("company_ir_index_contract_invalid")
    item_keys = {
        "document_id",
        "revision",
        "url",
        "title",
        "document_type",
        "published_at",
        "lifecycle_status",
        "content_kind",
    }
    entries: list[CompanyIrIndexEntry] = []
    for row in payload["items"]:
        if not isinstance(row, dict) or set(row) != item_keys:
            raise SourceValidationError("company_ir_index_contract_invalid")
        try:
            published_at = datetime.fromisoformat(
                str(row["published_at"]).replace("Z", "+00:00")
            )
            lifecycle = SourceLifecycleStatus(row["lifecycle_status"])
            content_kind = CompanyIrContentKind(row["content_kind"])
        except (TypeError, ValueError):
            raise SourceValidationError("company_ir_index_contract_invalid") from None
        entries.append(
            CompanyIrIndexEntry(
                document_id=row["document_id"],
                provider_revision=row["revision"],
                original_url=row["url"],
                title=row["title"],
                document_type=row["document_type"],
                published_at=published_at,
                lifecycle_status=lifecycle,
                content_kind=content_kind,
            )
        )
    return tuple(entries)


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _revision_key(entry: CompanyIrIndexEntry, *, original_url: str) -> str:
    return hashlib.sha256(
        _canonical_json(
            {
                "content_kind": entry.content_kind.value,
                "document_id": entry.document_id,
                "document_type": entry.document_type,
                "lifecycle_status": entry.lifecycle_status.value,
                "original_url": original_url,
                "provider_revision": entry.provider_revision,
                "published_at": entry.published_at.isoformat(),
                "title": entry.title,
            }
        )
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class _CompanyIrDocument:
    reference: SourceDocumentRef
    original_url: str
    title: str
    document_type: str
    published_at: datetime
    lifecycle_status: SourceLifecycleStatus
    content_kind: CompanyIrContentKind
    metadata_bytes: bytes


@dataclass(frozen=True, slots=True)
class _HttpBody:
    content: bytes
    media_type: str
    charset: str | None


class _VisibleTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._ignored_depth = 0

    def handle_starttag(
        self,
        tag: str,
        _attrs: list[tuple[str, str | None]],
    ) -> None:
        normalized = tag.lower()
        if normalized in _IGNORED_HTML_ELEMENTS:
            self._ignored_depth += 1
        elif self._ignored_depth == 0 and normalized in _HTML_BREAK_ELEMENTS:
            self.parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        normalized = tag.lower()
        if normalized in _IGNORED_HTML_ELEMENTS and self._ignored_depth > 0:
            self._ignored_depth -= 1
        elif self._ignored_depth == 0 and normalized in _HTML_BREAK_ELEMENTS:
            self.parts.append(" ")

    def handle_data(self, data: str) -> None:
        if self._ignored_depth == 0:
            self.parts.append(data)


def extract_company_ir_html_text(
    content: bytes,
    charset: str | None,
    *,
    max_characters: int = 49_000,
) -> str:
    """Extract bounded visible text from an approved public IR page."""

    if not content:
        raise SourceValidationError("company_ir_html_empty")
    normalized_charset = (charset or "utf-8").strip().lower().replace("_", "-")
    charset_aliases = {
        "utf-8": "utf-8-sig",
        "utf8": "utf-8-sig",
        "shift-jis": "cp932",
        "shift_jis": "cp932",
        "sjis": "cp932",
        "windows-31j": "cp932",
        "cp932": "cp932",
        "euc-jp": "euc_jp",
    }
    codec = charset_aliases.get(normalized_charset)
    if codec is None:
        raise SourceValidationError("company_ir_html_charset_unsupported")
    try:
        decoded = content.decode(codec)
    except UnicodeDecodeError:
        raise SourceValidationError("company_ir_html_encoding_invalid") from None
    parser = _VisibleTextParser()
    try:
        parser.feed(decoded)
        parser.close()
    except Exception:
        raise SourceValidationError("company_ir_html_invalid") from None
    return " ".join("".join(parser.parts).split())[:max_characters]


def extract_company_ir_pdf_text(
    content: bytes,
    *,
    max_pages: int = 40,
    max_characters: int = 49_000,
) -> str:
    """Extract a bounded PDF text layer without OCR or temporary files."""

    if not content:
        raise SourceValidationError("company_ir_pdf_empty")
    try:
        import pypdfium2 as pdfium  # type: ignore
    except ImportError:
        raise SourceValidationError("company_ir_pdf_extractor_unavailable") from None
    try:
        document = pdfium.PdfDocument(io.BytesIO(content))
    except Exception:
        raise SourceValidationError("company_ir_pdf_invalid") from None
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
                raise SourceValidationError(
                    "company_ir_pdf_text_extraction_failed"
                ) from None
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


def _metadata_evidence(document: _CompanyIrDocument) -> str:
    state = {
        SourceLifecycleStatus.ACTIVE: "公開中",
        SourceLifecycleStatus.CORRECTED: "訂正",
        SourceLifecycleStatus.WITHDRAWN: "撤回",
    }[document.lifecycle_status]
    return " ".join(
        (
            "会社IRホワイトリスト情報。",
            f"状態: {state}。",
            f"資料: {document.title}。",
            f"公表日時: {document.published_at.isoformat()}。",
        )
    )


def _bounded_evidence(metadata: str, extracted: str) -> str:
    canonical = " ".join(extracted.split())
    if not canonical:
        return metadata
    prefix = f"{metadata} 本文抜粋: "
    return f"{prefix}{canonical[: max(0, 50_000 - len(prefix))]}"


class CompanyIrApprovedFeedAdapter:
    """One isolated production feed for one explicitly approved issuer."""

    def __init__(
        self,
        *,
        config: CompanyIrApprovedFeedConfig,
        issuer_code_resolver: IssuerCodeResolver,
        index_parser: IndexParser = parse_company_ir_json_index,
        timeout_seconds: float = 15.0,
        max_index_bytes: int = 5_000_000,
        max_document_bytes: int = 50_000_000,
        max_documents_per_batch: int = 500,
        pdf_text_extractor: PdfTextExtractor = extract_company_ir_pdf_text,
        html_text_extractor: HtmlTextExtractor = extract_company_ir_html_text,
        resolver: AddressResolver = resolve_host_addresses,
        clock: Clock = lambda: datetime.now(timezone.utc),
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not isinstance(config, CompanyIrApprovedFeedConfig):
            raise SourceValidationError("company_ir_config_invalid")
        if not all(
            callable(value)
            for value in (
                issuer_code_resolver,
                index_parser,
                pdf_text_extractor,
                html_text_extractor,
                resolver,
                clock,
            )
        ):
            raise SourceValidationError("company_ir_adapter_dependency_invalid")
        if not 0.05 <= float(timeout_seconds) <= 60.0:
            raise SourceValidationError("company_ir_timeout_invalid")
        if not 1_000 <= max_index_bytes <= 25_000_000:
            raise SourceValidationError("company_ir_index_limit_invalid")
        if not 1_000 <= max_document_bytes <= 100_000_000:
            raise SourceValidationError("company_ir_document_limit_invalid")
        if not 1 <= max_documents_per_batch <= 2_000:
            raise SourceValidationError("company_ir_batch_limit_invalid")
        self.config = config
        self.provider = config.provider
        self._issuer_code_resolver = issuer_code_resolver
        self._index_parser = index_parser
        self._timeout = httpx.Timeout(float(timeout_seconds))
        self._max_index_bytes = max_index_bytes
        self._max_document_bytes = max_document_bytes
        self._max_documents_per_batch = max_documents_per_batch
        self._pdf_text_extractor = pdf_text_extractor
        self._html_text_extractor = html_text_extractor
        self._resolver = resolver
        self._clock = clock
        self._transport = transport
        self._documents: dict[tuple[str, str], _CompanyIrDocument] = {}

    def __repr__(self) -> str:
        return f"CompanyIrApprovedFeedAdapter(provider={self.provider!r})"

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise SourceValidationError("company_ir_clock_invalid")
        return value

    async def _target_is_public(self, url: str) -> str:
        canonical = self.config.approved_url(url)
        host = normalize_source_hostname(urlsplit(canonical).hostname or "")
        try:
            addresses = await self._resolver(host, 443)
        except SourceReachabilityUnavailable:
            raise ConnectionError("company_ir_dns_unavailable") from None
        except Exception:
            raise ConnectionError("company_ir_dns_unavailable") from None
        try:
            is_public = all_source_addresses_are_public(addresses)
        except SourceReachabilityUnavailable:
            raise ConnectionError("company_ir_dns_unavailable") from None
        if not is_public:
            raise SourceValidationError("company_ir_url_dns_not_public")
        return canonical

    @staticmethod
    def _response_content_type(value: str) -> tuple[str, str | None]:
        media_type = value.split(";", 1)[0].strip().lower()
        if _MEDIA_TYPE.fullmatch(media_type) is None:
            raise SourceValidationError("company_ir_response_content_type_invalid")
        match = _CHARSET.search(value)
        return media_type, match.group(1).lower() if match else None

    async def _get_body(
        self,
        url: str,
        *,
        expected_content_types: frozenset[str],
        maximum_bytes: int,
    ) -> _HttpBody:
        canonical = await self._target_is_public(url)
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout,
                follow_redirects=False,
                trust_env=False,
                transport=self._transport,
            ) as client:
                async with client.stream(
                    "GET",
                    canonical,
                    headers={
                        "Accept": ", ".join(sorted(expected_content_types)),
                        "User-Agent": DEFAULT_USER_AGENT,
                    },
                ) as response:
                    if response.is_redirect:
                        raise ConnectionError("company_ir_redirect_rejected")
                    if response.status_code != 200:
                        raise ConnectionError("company_ir_upstream_unavailable")
                    media_type, charset = self._response_content_type(
                        response.headers.get("content-type", "")
                    )
                    if media_type not in expected_content_types:
                        raise SourceValidationError(
                            "company_ir_response_content_type_invalid"
                        )
                    declared = response.headers.get("content-length")
                    if declared is not None:
                        try:
                            declared_length = int(declared)
                        except ValueError:
                            raise SourceValidationError(
                                "company_ir_response_length_invalid"
                            ) from None
                        if declared_length < 0:
                            raise SourceValidationError(
                                "company_ir_response_length_invalid"
                            )
                        if declared_length > maximum_bytes:
                            raise SourceValidationError("company_ir_response_too_large")
                    chunks: list[bytes] = []
                    size = 0
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > maximum_bytes:
                            raise SourceValidationError("company_ir_response_too_large")
                        chunks.append(chunk)
                    if size == 0:
                        raise SourceValidationError("company_ir_response_empty")
                    return _HttpBody(b"".join(chunks), media_type, charset)
        except httpx.TimeoutException:
            raise TimeoutError("company_ir_timeout") from None
        except httpx.TransportError:
            raise ConnectionError("company_ir_upstream_unavailable") from None

    async def _tracked(self) -> bool:
        try:
            values = await self._issuer_code_resolver()
        except Exception:
            raise ConnectionError("company_ir_issuer_resolver_unavailable") from None
        if isinstance(values, (str, bytes)):
            raise SourceValidationError("company_ir_issuer_resolver_invalid")
        normalized: set[str] = set()
        try:
            for value in values:
                normalized.add(normalize_issuer_code(value))
        except (IssuerMasterValidationError, TypeError):
            raise SourceValidationError("company_ir_issuer_resolver_invalid") from None
        if len(normalized) > 2_000:
            raise SourceValidationError("company_ir_issuer_resolver_limit_exceeded")
        return self.config.issuer_code in normalized

    @staticmethod
    def _cursor(cursor: dict[str, Any] | None) -> dict[str, str]:
        if cursor is None:
            return {}
        if not isinstance(cursor, dict) or set(cursor) != {"schema", "documents"}:
            raise SourceValidationError("company_ir_cursor_invalid")
        documents = cursor.get("documents")
        if cursor.get("schema") != 1 or not isinstance(documents, dict):
            raise SourceValidationError("company_ir_cursor_invalid")
        decoded: dict[str, str] = {}
        for document_id, revision in documents.items():
            if (
                not isinstance(document_id, str)
                or _DOCUMENT_ID.fullmatch(document_id) is None
                or not isinstance(revision, str)
                or _HASH.fullmatch(revision) is None
            ):
                raise SourceValidationError("company_ir_cursor_invalid")
            decoded[document_id] = revision
        return decoded

    async def _load_index(self) -> tuple[_CompanyIrDocument, ...]:
        body = await self._get_body(
            self.config.index_url,
            expected_content_types=frozenset(self.config.index_content_types),
            maximum_bytes=self._max_index_bytes,
        )
        try:
            parsed_entries = self._index_parser(
                body.content,
                body.media_type,
                self.config,
            )
            if isinstance(parsed_entries, (str, bytes, Mapping)):
                raise TypeError
            entries = tuple(islice(iter(parsed_entries), self._max_documents_per_batch + 1))
        except SourceValidationError:
            raise
        except Exception:
            raise SourceValidationError("company_ir_index_parser_failed") from None
        if len(entries) > self._max_documents_per_batch:
            raise SourceValidationError("company_ir_batch_limit_exceeded")
        documents: list[_CompanyIrDocument] = []
        seen: set[str] = set()
        for entry in entries:
            if not isinstance(entry, CompanyIrIndexEntry):
                raise SourceValidationError("company_ir_index_parser_contract_invalid")
            if entry.document_id in seen:
                raise SourceValidationError("company_ir_index_duplicate_document_id")
            seen.add(entry.document_id)
            original_url = self.config.approved_url(entry.original_url)
            revision_key = _revision_key(entry, original_url=original_url)
            metadata_bytes = _canonical_json(
                {
                    "content_kind": entry.content_kind.value,
                    "document_id": entry.document_id,
                    "document_type": entry.document_type,
                    "lifecycle_status": entry.lifecycle_status.value,
                    "original_url": original_url,
                    "provider_revision_sha256": hashlib.sha256(
                        entry.provider_revision.encode("utf-8")
                    ).hexdigest(),
                    "published_at": entry.published_at.isoformat(),
                    "title": entry.title,
                }
            )
            documents.append(
                _CompanyIrDocument(
                    reference=SourceDocumentRef(entry.document_id, revision_key),
                    original_url=original_url,
                    title=entry.title,
                    document_type=entry.document_type,
                    published_at=entry.published_at,
                    lifecycle_status=entry.lifecycle_status,
                    content_kind=entry.content_kind,
                    metadata_bytes=metadata_bytes,
                )
            )
        documents.sort(
            key=lambda document: (
                document.published_at,
                document.reference.document_id,
            )
        )
        return tuple(documents)

    async def discover(self, cursor: dict[str, Any] | None) -> DiscoveryBatch:
        previous = self._cursor(cursor)
        if not await self._tracked():
            self._documents = {}
            return DiscoveryBatch(
                documents=(),
                next_cursor={"schema": 1, "documents": {}},
            )
        documents = await self._load_index()
        current = {
            document.reference.document_id: document.reference.revision_key
            for document in documents
        }
        discovered = tuple(
            document.reference
            for document in documents
            if previous.get(document.reference.document_id)
            != document.reference.revision_key
        )
        self._documents = {
            (document.reference.document_id, document.reference.revision_key): document
            for document in documents
        }
        return DiscoveryBatch(
            documents=discovered,
            next_cursor={"schema": 1, "documents": current},
        )

    async def fetch(self, document: SourceDocumentRef) -> SourcePayload:
        decoded = self._documents.get((document.document_id, document.revision_key))
        if decoded is None:
            raise SourceValidationError("company_ir_document_not_discovered")
        metadata = _metadata_evidence(decoded)
        evidence_text = metadata
        evidence_scope = "metadata_only"
        raw_content = decoded.metadata_bytes
        if decoded.lifecycle_status is not SourceLifecycleStatus.WITHDRAWN:
            if decoded.content_kind is CompanyIrContentKind.PDF:
                body = await self._get_body(
                    decoded.original_url,
                    expected_content_types=_PDF_MEDIA_TYPES,
                    maximum_bytes=self._max_document_bytes,
                )
                if not body.content.startswith(b"%PDF-"):
                    raise SourceValidationError("company_ir_pdf_invalid")
                try:
                    extracted = await asyncio.to_thread(
                        self._pdf_text_extractor,
                        body.content,
                    )
                except SourceValidationError:
                    raise
                except Exception:
                    raise SourceValidationError(
                        "company_ir_pdf_text_extraction_failed"
                    ) from None
                if not isinstance(extracted, str):
                    raise SourceValidationError("company_ir_pdf_text_extraction_failed")
                raw_content = body.content
                evidence_text = _bounded_evidence(metadata, extracted)
                evidence_scope = (
                    "metadata_and_pdf_text_first_40_pages"
                    if " ".join(extracted.split())
                    else "metadata_only_pdf_text_unavailable"
                )
            elif decoded.content_kind is CompanyIrContentKind.HTML:
                body = await self._get_body(
                    decoded.original_url,
                    expected_content_types=_HTML_MEDIA_TYPES,
                    maximum_bytes=self._max_document_bytes,
                )
                try:
                    extracted = await asyncio.to_thread(
                        self._html_text_extractor,
                        body.content,
                        body.charset,
                    )
                except SourceValidationError:
                    raise
                except Exception:
                    raise SourceValidationError(
                        "company_ir_html_text_extraction_failed"
                    ) from None
                if not isinstance(extracted, str):
                    raise SourceValidationError("company_ir_html_text_extraction_failed")
                raw_content = body.content
                evidence_text = _bounded_evidence(metadata, extracted)
                evidence_scope = (
                    "metadata_and_html_visible_text"
                    if " ".join(extracted.split())
                    else "metadata_only_html_text_unavailable"
                )
        return SourcePayload(
            document=document,
            original_url=decoded.original_url,
            fetched_at=self._now(),
            raw_content=raw_content,
            metadata={
                "company_ir_document": decoded,
                "evidence_text": evidence_text,
                "evidence_scope": evidence_scope,
            },
        )

    def normalize(self, payload: SourcePayload) -> NormalizedSourceRecord:
        document = payload.metadata.get("company_ir_document")
        evidence_text = payload.metadata.get("evidence_text")
        evidence_scope = payload.metadata.get("evidence_scope")
        if (
            not isinstance(document, _CompanyIrDocument)
            or document.reference != payload.document
            or not isinstance(evidence_text, str)
            or not isinstance(evidence_scope, str)
        ):
            raise SourceValidationError("company_ir_payload_contract_invalid")
        return NormalizedSourceRecord.from_payload(
            provider=self.provider,
            payload=payload,
            title=document.title,
            document_type=document.document_type,
            published_at=document.published_at,
            lifecycle_status=document.lifecycle_status,
            issuer_codes=(self.config.issuer_code,),
            evidence_text=evidence_text,
            metadata={
                "company_ir_provider_key": self.config.provider_key,
                "content_kind": document.content_kind.value,
                "evidence_scope": evidence_scope,
                "feed_contract": "per_issuer_approved_index_v1",
            },
        )

    async def health(self) -> SourceHealth:
        checked_at = self._now()
        try:
            await self._load_index()
        except Exception:
            return SourceHealth(
                ok=False,
                checked_at=checked_at,
                detail_code="company_ir_feed_unavailable",
            )
        return SourceHealth(
            ok=True,
            checked_at=checked_at,
            detail_code="company_ir_feed_ready",
        )


__all__ = [
    "CompanyIrApprovedFeedAdapter",
    "CompanyIrApprovedFeedConfig",
    "CompanyIrContentKind",
    "CompanyIrIndexEntry",
    "HtmlTextExtractor",
    "IndexParser",
    "IssuerCodeResolver",
    "PdfTextExtractor",
    "extract_company_ir_html_text",
    "extract_company_ir_pdf_text",
    "parse_company_ir_json_index",
]
