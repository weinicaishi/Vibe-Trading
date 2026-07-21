"""SSRF-resistant HTTP reachability adapter for EventBrief sources."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable, Collection, Sequence
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

DEFAULT_USER_AGENT = "Vibe-Trading-Market-Morning/1.0"
MAX_SOURCE_URL_LENGTH = 4096
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_HEAD_FALLBACK_STATUSES = frozenset({403, 405, 501})
_TRANSIENT_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})

AddressResolver = Callable[[str, int], Awaitable[Sequence[str]]]


class SourceReachabilityUnavailable(RuntimeError):
    def __init__(self, error_code: str) -> None:
        self.error_code = error_code
        super().__init__(error_code)


def normalize_source_hostname(value: str) -> str:
    """Return one canonical non-IP hostname for source allowlists."""

    normalized = value.strip().rstrip(".").lower()
    if not normalized:
        raise ValueError("hostname is empty")
    try:
        normalized = normalized.encode("idna").decode("ascii")
    except UnicodeError as error:
        raise ValueError("hostname is invalid") from error
    if len(normalized) > 253 or any(
        not label or len(label) > 63 for label in normalized.split(".")
    ):
        raise ValueError("hostname is invalid")
    try:
        ipaddress.ip_address(normalized)
    except ValueError:
        return normalized
    raise ValueError("IP literals are not permitted")


async def resolve_host_addresses(host: str, port: int) -> tuple[str, ...]:
    """Resolve a hostname without blocking the event loop."""

    try:
        answers = await asyncio.to_thread(
            socket.getaddrinfo,
            host,
            port,
            type=socket.SOCK_STREAM,
        )
    except OSError as error:
        raise SourceReachabilityUnavailable(
            "source_reachability_dns_unavailable"
        ) from error
    addresses = tuple(
        sorted({str(answer[4][0]).split("%", 1)[0] for answer in answers})
    )
    if not addresses:
        raise SourceReachabilityUnavailable(
            "source_reachability_dns_unavailable"
        )
    return addresses


def all_source_addresses_are_public(addresses: Sequence[str]) -> bool:
    """Fail closed unless every DNS answer is globally routable."""

    if not addresses:
        raise SourceReachabilityUnavailable(
            "source_reachability_dns_unavailable"
        )
    try:
        parsed = tuple(ipaddress.ip_address(value) for value in addresses)
    except ValueError as error:
        raise SourceReachabilityUnavailable(
            "source_reachability_dns_unavailable"
        ) from error
    return all(address.is_global for address in parsed)


class HttpSourceReachabilityChecker:
    """Check allowlisted public HTTPS sources without downloading their body."""

    def __init__(
        self,
        *,
        allowed_hosts: Collection[str],
        resolver: AddressResolver = resolve_host_addresses,
        timeout_seconds: float = 5.0,
        max_redirects: int = 3,
        user_agent: str = DEFAULT_USER_AGENT,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not allowed_hosts:
            raise ValueError("allowed_hosts must not be empty")
        try:
            normalized_hosts = frozenset(
                normalize_source_hostname(host) for host in allowed_hosts
            )
        except (TypeError, ValueError) as error:
            raise ValueError("allowed_hosts contains an invalid hostname") from error
        if not callable(resolver):
            raise TypeError("resolver must be callable")
        if not isinstance(timeout_seconds, (int, float)) or not (
            0.05 <= float(timeout_seconds) <= 30.0
        ):
            raise ValueError("timeout_seconds must be between 0.05 and 30")
        if type(max_redirects) is not int or not 0 <= max_redirects <= 5:
            raise ValueError("max_redirects must be between 0 and 5")
        normalized_agent = user_agent.strip()
        if not normalized_agent or len(normalized_agent) > 191:
            raise ValueError("user_agent must contain 1 to 191 characters")

        self._allowed_hosts = normalized_hosts
        self._resolver = resolver
        self._timeout = httpx.Timeout(float(timeout_seconds))
        self._max_redirects = max_redirects
        self._user_agent = normalized_agent
        self._transport = transport

    def _parse_target(self, raw_url: str) -> tuple[str, str] | None:
        if (
            not isinstance(raw_url, str)
            or not raw_url
            or len(raw_url) > MAX_SOURCE_URL_LENGTH
            or any(ord(character) < 32 for character in raw_url)
        ):
            return None
        try:
            parsed = urlsplit(raw_url)
            host = normalize_source_hostname(parsed.hostname or "")
            port = parsed.port
        except (TypeError, UnicodeError, ValueError):
            return None
        if (
            parsed.scheme.lower() != "https"
            or parsed.username is not None
            or parsed.password is not None
            or port not in (None, 443)
            or host not in self._allowed_hosts
        ):
            return None
        canonical = urlunsplit(
            (
                "https",
                host,
                parsed.path or "/",
                parsed.query,
                "",
            )
        )
        return canonical, host

    async def _target_is_public(self, host: str) -> bool:
        try:
            addresses = await self._resolver(host, 443)
        except SourceReachabilityUnavailable:
            raise
        except Exception as error:
            raise SourceReachabilityUnavailable(
                "source_reachability_dns_unavailable"
            ) from error
        return all_source_addresses_are_public(addresses)

    async def _request(
        self,
        client: httpx.AsyncClient,
        *,
        method: str,
        url: str,
    ) -> tuple[int, str | None]:
        headers: dict[str, str] = {"user-agent": self._user_agent}
        if method == "GET":
            headers["range"] = "bytes=0-0"
        try:
            async with client.stream(method, url, headers=headers) as response:
                return response.status_code, response.headers.get("location")
        except (httpx.TimeoutException, httpx.NetworkError) as error:
            raise SourceReachabilityUnavailable(
                "source_reachability_transport_unavailable"
            ) from error
        except httpx.HTTPError as error:
            raise SourceReachabilityUnavailable(
                "source_reachability_transport_unavailable"
            ) from error

    @staticmethod
    def _transient_status(status_code: int) -> None:
        if status_code in _TRANSIENT_STATUSES or status_code >= 500:
            raise SourceReachabilityUnavailable(
                "source_reachability_upstream_unavailable"
            )

    async def __call__(self, raw_url: str) -> bool:
        current_url = raw_url
        visited: set[str] = set()
        redirects = 0

        async with httpx.AsyncClient(
            timeout=self._timeout,
            follow_redirects=False,
            trust_env=False,
            transport=self._transport,
        ) as client:
            while True:
                target = self._parse_target(current_url)
                if target is None:
                    return False
                canonical_url, host = target
                if canonical_url in visited:
                    return False
                visited.add(canonical_url)
                if not await self._target_is_public(host):
                    return False

                status_code, location = await self._request(
                    client,
                    method="HEAD",
                    url=canonical_url,
                )
                self._transient_status(status_code)
                if status_code in _HEAD_FALLBACK_STATUSES:
                    status_code, location = await self._request(
                        client,
                        method="GET",
                        url=canonical_url,
                    )
                    self._transient_status(status_code)
                if 200 <= status_code < 300:
                    return True
                if status_code not in _REDIRECT_STATUSES or not location:
                    return False
                if redirects >= self._max_redirects:
                    return False
                redirects += 1
                current_url = urljoin(canonical_url, location)


__all__ = [
    "AddressResolver",
    "DEFAULT_USER_AGENT",
    "HttpSourceReachabilityChecker",
    "MAX_SOURCE_URL_LENGTH",
    "SourceReachabilityUnavailable",
    "all_source_addresses_are_public",
    "normalize_source_hostname",
    "resolve_host_addresses",
]
