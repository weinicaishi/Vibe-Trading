"""Production-safe HTTP source reachability checks for EventBrief."""

from __future__ import annotations

import asyncio

import httpx
import pytest

PUBLIC_IPS = ("8.8.8.8",)


async def _public_resolver(_host: str, _port: int):
    return PUBLIC_IPS


def _checker(handler, **overrides):
    from src.market_morning.http_reachability import (
        HttpSourceReachabilityChecker,
    )

    options = {
        "allowed_hosts": {"example.com", "cdn.example.com"},
        "resolver": _public_resolver,
        "transport": httpx.MockTransport(handler),
        "timeout_seconds": 2.0,
        "max_redirects": 2,
    }
    options.update(overrides)
    return HttpSourceReachabilityChecker(**options)


def test_https_source_is_reachable_after_public_dns_validation() -> None:
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, request=request)

    result = asyncio.run(_checker(handler)("https://example.com/disclosure.pdf"))

    assert result is True
    assert [request.method for request in requests] == ["HEAD"]
    assert requests[0].headers["user-agent"] == "Vibe-Trading-Market-Morning/1.0"


def test_head_not_supported_falls_back_to_streamed_range_get() -> None:
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "HEAD":
            return httpx.Response(405, request=request)
        return httpx.Response(206, content=b"first-byte", request=request)

    result = asyncio.run(_checker(handler)("https://example.com/disclosure.pdf"))

    assert result is True
    assert [request.method for request in requests] == ["HEAD", "GET"]
    assert requests[1].headers["range"] == "bytes=0-0"


def test_each_redirect_target_is_revalidated_before_following() -> None:
    resolved = []
    requests = []

    async def resolver(host: str, port: int):
        resolved.append((host, port))
        return PUBLIC_IPS

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        if request.url.host == "example.com":
            return httpx.Response(
                302,
                headers={"location": "https://cdn.example.com/final.pdf"},
                request=request,
            )
        return httpx.Response(200, request=request)

    result = asyncio.run(
        _checker(handler, resolver=resolver)("https://example.com/start")
    )

    assert result is True
    assert resolved == [("example.com", 443), ("cdn.example.com", 443)]
    assert requests == [
        "https://example.com/start",
        "https://cdn.example.com/final.pdf",
    ]


def test_redirect_to_unapproved_host_is_rejected_without_second_request() -> None:
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(
            302,
            headers={"location": "https://internal.example.invalid/secret"},
            request=request,
        )

    result = asyncio.run(_checker(handler)("https://example.com/start"))

    assert result is False
    assert requests == ["https://example.com/start"]


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/disclosure.pdf",
        "https://unapproved.example/disclosure.pdf",
        "https://user:password@example.com/disclosure.pdf",
        "https://example.com:8443/disclosure.pdf",
        "https://127.0.0.1/disclosure.pdf",
        "not-a-url",
    ],
)
def test_invalid_or_unapproved_url_is_rejected_without_http(url: str) -> None:
    def must_not_run(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("rejected URLs must not reach the HTTP transport")

    result = asyncio.run(_checker(must_not_run)(url))

    assert result is False


@pytest.mark.parametrize(
    "resolved_addresses",
    [
        ("127.0.0.1",),
        ("169.254.169.254",),
        ("10.0.0.1",),
        ("8.8.8.8", "192.168.1.10"),
        ("::1",),
    ],
)
def test_any_non_public_dns_answer_blocks_the_request(
    resolved_addresses: tuple[str, ...],
) -> None:
    async def resolver(_host: str, _port: int):
        return resolved_addresses

    def must_not_run(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("private DNS answers must not reach HTTP")

    result = asyncio.run(
        _checker(must_not_run, resolver=resolver)(
            "https://example.com/disclosure.pdf"
        )
    )

    assert result is False


@pytest.mark.parametrize("status_code", [400, 401, 403, 404, 410])
def test_terminal_client_errors_mean_source_is_unreachable(
    status_code: int,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, request=request)

    result = asyncio.run(_checker(handler)("https://example.com/missing"))

    assert result is False


@pytest.mark.parametrize("status_code", [408, 429, 500, 502, 503, 504])
def test_transient_http_failures_degrade_instead_of_marking_source_missing(
    status_code: int,
) -> None:
    from src.market_morning.http_reachability import (
        SourceReachabilityUnavailable,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, request=request)

    with pytest.raises(SourceReachabilityUnavailable) as error:
        asyncio.run(_checker(handler)("https://example.com/temporary"))

    assert error.value.error_code == "source_reachability_upstream_unavailable"
    assert str(status_code) not in str(error.value)


def test_transport_failure_is_sanitized_and_degrades() -> None:
    from src.market_morning.http_reachability import (
        SourceReachabilityUnavailable,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout(
            "private proxy and credential details",
            request=request,
        )

    with pytest.raises(SourceReachabilityUnavailable) as error:
        asyncio.run(_checker(handler)("https://example.com/temporary"))

    assert error.value.error_code == "source_reachability_transport_unavailable"
    assert "credential" not in str(error.value)


def test_dns_failure_is_sanitized_and_degrades() -> None:
    from src.market_morning.http_reachability import (
        SourceReachabilityUnavailable,
    )

    async def resolver(_host: str, _port: int):
        raise OSError("private resolver details")

    def must_not_run(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("DNS failure must not reach HTTP")

    with pytest.raises(SourceReachabilityUnavailable) as error:
        asyncio.run(
            _checker(must_not_run, resolver=resolver)(
                "https://example.com/temporary"
            )
        )

    assert error.value.error_code == "source_reachability_dns_unavailable"
    assert "resolver details" not in str(error.value)


def test_redirect_loop_or_limit_is_unreachable() -> None:
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(
            302,
            headers={"location": "/loop"},
            request=request,
        )

    result = asyncio.run(_checker(handler)("https://example.com/loop"))

    assert result is False
    assert requests == ["https://example.com/loop"]


def test_empty_dns_answer_degrades_as_resolver_unavailable() -> None:
    from src.market_morning.http_reachability import (
        SourceReachabilityUnavailable,
    )

    async def resolver(_host: str, _port: int):
        return ()

    def must_not_run(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("empty DNS answer must not reach HTTP")

    with pytest.raises(SourceReachabilityUnavailable) as error:
        asyncio.run(
            _checker(must_not_run, resolver=resolver)(
                "https://example.com/temporary"
            )
        )

    assert error.value.error_code == "source_reachability_dns_unavailable"
