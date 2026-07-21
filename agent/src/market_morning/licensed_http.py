"""Small, SSRF-resistant HTTPS client boundary for licensed provider adapters.

The deployment owns credentials and provider-specific parsing.  The product
core owns the network invariants: an exact approved HTTPS/path boundary,
public DNS only, no redirects or environment proxy, bounded responses and
sanitized failures.  Request headers are produced just-in-time so secrets do
not become dataclass fields or exception text.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qsl, unquote, urlsplit, urlunsplit

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

_MEDIA_TYPE = re.compile(r"^[a-z0-9!#$&^_.+-]+/[a-z0-9!#$&^_.+-]+$")
_HEADER_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]{1,128}$")
_FORBIDDEN_REQUEST_HEADERS = frozenset(
    {"connection", "content-length", "host", "transfer-encoding"}
)
_TRANSIENT_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})
_SENSITIVE_QUERY_FRAGMENTS = (
    "api_key",
    "apikey",
    "access_key",
    "access_token",
    "authorization",
    "credential",
    "password",
    "secret",
    "signature",
    "subscription_key",
    "token",
)

HeaderFactory = Callable[[], Mapping[str, str]]


class LicensedHttpError(RuntimeError):
    """Sanitized network/contract failure safe for a durable job boundary."""

    def __init__(self, error_code: str) -> None:
        self.error_code = error_code
        super().__init__(error_code)


@dataclass(frozen=True, slots=True)
class _ApprovedBase:
    host: str
    path: str
    url: str


def _safe_path(path: str) -> bool:
    decoded = unquote(path)
    if any(ord(character) < 32 or ord(character) == 127 for character in decoded):
        return False
    return all(segment not in {".", ".."} for segment in decoded.split("/"))


def _approved_base(value: Any) -> _ApprovedBase:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > MAX_SOURCE_URL_LENGTH
        or any(ord(character) < 32 for character in value)
    ):
        raise ValueError("licensed_http_allowed_base_invalid")
    try:
        parsed = urlsplit(value)
        host = normalize_source_hostname(parsed.hostname or "")
        port = parsed.port
    except (TypeError, UnicodeError, ValueError):
        raise ValueError("licensed_http_allowed_base_invalid") from None
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
        or not _safe_path(path)
    ):
        raise ValueError("licensed_http_allowed_base_invalid")
    return _ApprovedBase(
        host=host,
        path=path,
        url=urlunsplit(("https", host, path, "", "")),
    )


def _approved_endpoint(value: Any, *, bases: Sequence[_ApprovedBase]) -> tuple[str, str]:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > MAX_SOURCE_URL_LENGTH
        or any(ord(character) < 32 for character in value)
    ):
        raise ValueError("licensed_http_endpoint_invalid")
    try:
        parsed = urlsplit(value)
        host = normalize_source_hostname(parsed.hostname or "")
        port = parsed.port
    except (TypeError, UnicodeError, ValueError):
        raise ValueError("licensed_http_endpoint_invalid") from None
    path = parsed.path or "/"
    try:
        query_pairs = parse_qsl(
            parsed.query,
            keep_blank_values=True,
            strict_parsing=True,
            max_num_fields=32,
        )
    except ValueError:
        raise ValueError("licensed_http_endpoint_invalid") from None
    query_names = [name.strip().lower().replace("-", "_") for name, _ in query_pairs]
    if (
        parsed.scheme.lower() != "https"
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or parsed.fragment
        or not path.startswith("/")
        or not _safe_path(path)
        or not any(base.host == host and path.startswith(base.path) for base in bases)
        or any(not name for name in query_names)
        or len(set(query_names)) != len(query_names)
        or any(
            fragment in name
            for name in query_names
            for fragment in _SENSITIVE_QUERY_FRAGMENTS
        )
    ):
        raise ValueError("licensed_http_endpoint_invalid")
    return urlunsplit(("https", host, path, parsed.query, "")), host


@dataclass(frozen=True, slots=True)
class ApprovedHttpsEndpoint:
    """One exact provider URL inside one or more reviewed path prefixes."""

    url: str = field(repr=False)
    allowed_base_urls: tuple[str, ...]
    accepted_media_types: tuple[str, ...] = ("application/json",)
    max_response_bytes: int = 1_000_000
    _host: str = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if (
            not isinstance(self.allowed_base_urls, tuple)
            or not 1 <= len(self.allowed_base_urls) <= 8
        ):
            raise ValueError("licensed_http_allowed_bases_invalid")
        bases = tuple(_approved_base(value) for value in self.allowed_base_urls)
        if len({base.url for base in bases}) != len(bases):
            raise ValueError("licensed_http_allowed_bases_invalid")
        endpoint, host = _approved_endpoint(self.url, bases=bases)
        if (
            not isinstance(self.accepted_media_types, tuple)
            or not 1 <= len(self.accepted_media_types) <= 8
        ):
            raise ValueError("licensed_http_media_types_invalid")
        media_types: list[str] = []
        for value in self.accepted_media_types:
            if not isinstance(value, str):
                raise ValueError("licensed_http_media_types_invalid")
            canonical = value.strip().lower()
            if _MEDIA_TYPE.fullmatch(canonical) is None:
                raise ValueError("licensed_http_media_types_invalid")
            media_types.append(canonical)
        if len(set(media_types)) != len(media_types):
            raise ValueError("licensed_http_media_types_invalid")
        if type(self.max_response_bytes) is not int or not (
            1 <= self.max_response_bytes <= 10_000_000
        ):
            raise ValueError("licensed_http_response_limit_invalid")
        object.__setattr__(self, "url", endpoint)
        object.__setattr__(
            self,
            "allowed_base_urls",
            tuple(base.url for base in bases),
        )
        object.__setattr__(self, "accepted_media_types", tuple(media_types))
        object.__setattr__(self, "_host", host)

    @property
    def host(self) -> str:
        return self._host


def _request_headers(
    factory: HeaderFactory | None,
    *,
    user_agent: str,
) -> dict[str, str]:
    try:
        supplied = {} if factory is None else factory()
    except Exception:
        raise LicensedHttpError("licensed_http_credentials_unavailable") from None
    if not isinstance(supplied, Mapping):
        raise LicensedHttpError("licensed_http_headers_invalid")
    headers: dict[str, str] = {}
    for name, value in supplied.items():
        if not isinstance(name, str) or not isinstance(value, str):
            raise LicensedHttpError("licensed_http_headers_invalid")
        canonical_name = name.strip()
        lowered = canonical_name.lower()
        if (
            _HEADER_NAME.fullmatch(canonical_name) is None
            or lowered in _FORBIDDEN_REQUEST_HEADERS
            or not value
            or len(value) > 4096
            or any(ord(character) < 32 or ord(character) == 127 for character in value)
        ):
            raise LicensedHttpError("licensed_http_headers_invalid")
        if lowered in {existing.lower() for existing in headers}:
            raise LicensedHttpError("licensed_http_headers_invalid")
        headers[canonical_name] = value
    if "user-agent" not in {name.lower() for name in headers}:
        headers["user-agent"] = user_agent
    return headers


async def fetch_approved_https_bytes(
    endpoint: ApprovedHttpsEndpoint,
    *,
    header_factory: HeaderFactory | None = None,
    resolver: AddressResolver = resolve_host_addresses,
    timeout_seconds: float = 10.0,
    user_agent: str = DEFAULT_USER_AGENT,
    transport: httpx.AsyncBaseTransport | None = None,
) -> tuple[bytes, str]:
    """Fetch one bounded body without redirects, proxy inheritance or URL logs."""

    if not callable(resolver):
        raise TypeError("resolver must be callable")
    if not isinstance(timeout_seconds, (int, float)) or not (
        0.05 <= float(timeout_seconds) <= 30.0
    ):
        raise ValueError("timeout_seconds must be between 0.05 and 30")
    normalized_agent = user_agent.strip()
    if not normalized_agent or len(normalized_agent) > 191:
        raise ValueError("user_agent must contain 1 to 191 characters")
    headers = _request_headers(header_factory, user_agent=normalized_agent)
    try:
        addresses = await resolver(endpoint.host, 443)
        if not all_source_addresses_are_public(addresses):
            raise LicensedHttpError("licensed_http_non_public_target")
    except LicensedHttpError:
        raise
    except SourceReachabilityUnavailable:
        raise LicensedHttpError("licensed_http_dns_unavailable") from None
    except Exception:
        raise LicensedHttpError("licensed_http_dns_unavailable") from None

    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(float(timeout_seconds)),
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        ) as client:
            async with client.stream("GET", endpoint.url, headers=headers) as response:
                if 300 <= response.status_code < 400:
                    raise LicensedHttpError("licensed_http_redirect_forbidden")
                if (
                    response.status_code in _TRANSIENT_STATUSES
                    or response.status_code >= 500
                ):
                    raise LicensedHttpError("licensed_http_upstream_unavailable")
                if response.status_code != 200:
                    raise LicensedHttpError("licensed_http_request_rejected")
                raw_content_length = response.headers.get("content-length")
                if raw_content_length is not None:
                    try:
                        content_length = int(raw_content_length)
                    except ValueError:
                        raise LicensedHttpError(
                            "licensed_http_response_invalid"
                        ) from None
                    if content_length < 1 or content_length > endpoint.max_response_bytes:
                        raise LicensedHttpError("licensed_http_response_too_large")
                media_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                if media_type not in endpoint.accepted_media_types:
                    raise LicensedHttpError("licensed_http_content_type_invalid")
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    content.extend(chunk)
                    if len(content) > endpoint.max_response_bytes:
                        raise LicensedHttpError("licensed_http_response_too_large")
                if not content:
                    raise LicensedHttpError("licensed_http_response_invalid")
                return bytes(content), media_type
    except LicensedHttpError:
        raise
    except (httpx.TimeoutException, httpx.NetworkError, httpx.HTTPError):
        raise LicensedHttpError("licensed_http_transport_unavailable") from None


__all__ = [
    "ApprovedHttpsEndpoint",
    "HeaderFactory",
    "LicensedHttpError",
    "fetch_approved_https_bytes",
]
