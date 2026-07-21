"""Security and API contract tests for private email delivery links."""

from __future__ import annotations

import asyncio
from collections import deque
from datetime import date, datetime, timedelta, timezone
import hashlib
from types import SimpleNamespace
import sys

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.dialects import mysql
from sqlalchemy.exc import SQLAlchemyError

USER_ID = "11111111-1111-4111-8111-111111111111"
OTHER_USER_ID = "22222222-2222-4222-8222-222222222222"
RUN_ID = "33333333-3333-4333-8333-333333333333"
ATTEMPT_ID = "44444444-4444-4444-8444-444444444444"
NOW = datetime(2026, 7, 21, 0, 30, tzinfo=timezone.utc)
EDITION_DATE = date(2026, 7, 21)
PRIMARY_SECRET = b"p" * 32
PREVIOUS_SECRET = b"o" * 32


class _Transaction:
    def __init__(self, session):
        self.session = session

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, exc_type, exc, traceback):
        return None


class _Factory:
    def __init__(self, *sessions):
        self.sessions = deque(sessions)

    def begin(self):
        return _Transaction(self.sessions.popleft())


class _Result:
    def __init__(self, rows):
        self.rows = rows

    def all(self):
        return self.rows


class _Session:
    def __init__(self, rows):
        self.rows = rows
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)
        return _Result(self.rows)


def _context(token_digest: str):
    from src.market_morning.email_delivery_service import EmailDeliveryContext

    return EmailDeliveryContext(
        delivery_attempt_id=ATTEMPT_ID,
        user_id=USER_ID,
        external_subject="oidc-subject",
        edition_date=EDITION_DATE,
        global_run_id=RUN_ID,
        idempotency_key=f"email:{EDITION_DATE}:{USER_ID}:global-run:{RUN_ID}",
        deep_link_token_sha256=token_digest,
        eligible=True,
        suppression_reason=None,
    )


def _digest(signer) -> str:
    return asyncio.run(
        signer.token_digest_builder(USER_ID, RUN_ID, EDITION_DATE)
    )


def test_signer_creates_opaque_fragment_link_without_identity() -> None:
    from src.market_morning.delivery_links import DeliveryLinkSigner

    signer = DeliveryLinkSigner(
        base_url="https://morning.example.jp/market-morning",
        signing_secret=PRIMARY_SECRET,
    )
    digest = _digest(signer)
    link = asyncio.run(signer.link_builder(_context(digest)))

    assert link.url == f"https://morning.example.jp/market-morning#token={link.token}"
    assert "?" not in link.url
    assert len(link.token) == 43
    assert USER_ID not in link.url and RUN_ID not in link.url
    assert hashlib.sha256(link.token.encode("ascii")).hexdigest() == digest
    assert repr(signer) == "<DeliveryLinkSigner configured>"
    assert PRIMARY_SECRET.decode() not in repr(signer)


def test_rotated_signer_can_finish_a_delivery_created_with_previous_key() -> None:
    from src.market_morning.delivery_links import DeliveryLinkSigner

    old_signer = DeliveryLinkSigner(
        base_url="https://morning.example.jp/market-morning",
        signing_secret=PREVIOUS_SECRET,
    )
    rotated_signer = DeliveryLinkSigner(
        base_url="https://morning.example.jp/market-morning",
        signing_secret=PRIMARY_SECRET,
        previous_signing_secret=PREVIOUS_SECRET,
    )

    link = asyncio.run(rotated_signer.link_builder(_context(_digest(old_signer))))

    assert hashlib.sha256(link.token.encode("ascii")).hexdigest() == _digest(old_signer)


def test_signer_fails_closed_for_mismatched_digest() -> None:
    import pytest

    from src.market_morning.delivery_links import DeliveryLinkSigner
    from src.market_morning.email_delivery_service import EmailDeliveryPortUnavailable

    signer = DeliveryLinkSigner(
        base_url="https://morning.example.jp/market-morning",
        signing_secret=PRIMARY_SECRET,
    )

    with pytest.raises(EmailDeliveryPortUnavailable, match="delivery_link_unavailable"):
        asyncio.run(signer.link_builder(_context("f" * 64)))


def test_signer_rejects_unsafe_configuration_without_echoing_secrets() -> None:
    import pytest

    from src.market_morning.delivery_links import (
        DeliveryLinkConfigurationError,
        DeliveryLinkSigner,
        build_delivery_link_signer_from_env,
    )

    unsafe_urls = (
        "http://morning.example.jp/market-morning",
        "https://127.0.0.1/market-morning",
        "https://morning.example.jp/../market-morning",
        "https://morning.example.jp/%2e%2e/market-morning",
        "https://morning.example.jp/%252e%252e/market-morning",
        "https://morning.example.jp/%250a/market-morning",
        "https://morning.example.jp/market-morning?token=x",
    )
    for url in unsafe_urls:
        with pytest.raises(DeliveryLinkConfigurationError):
            DeliveryLinkSigner(base_url=url, signing_secret=PRIMARY_SECRET)

    with pytest.raises(DeliveryLinkConfigurationError):
        DeliveryLinkSigner(
            base_url="https://morning.example.jp/market-morning",
            signing_secret=b"short",
        )
    with pytest.raises(DeliveryLinkConfigurationError):
        DeliveryLinkSigner(
            base_url="https://morning.example.jp/market-morning",
            signing_secret=PRIMARY_SECRET,
            previous_signing_secret=PRIMARY_SECRET,
        )
    with pytest.raises(DeliveryLinkConfigurationError) as caught:
        build_delivery_link_signer_from_env(
            {
                "VIBE_MARKET_MORNING_DELIVERY_LINK_BASE_URL": "invalid",
                "VIBE_MARKET_MORNING_DELIVERY_LINK_SIGNING_SECRET": "secret-value",
            }
        )
    assert "secret-value" not in str(caught.value)


def test_delivery_link_static_preflight_reports_only_stable_codes() -> None:
    from src.market_morning.delivery_links import (
        delivery_link_static_preflight_checks,
    )

    assert delivery_link_static_preflight_checks({}) == (
        "delivery_link_configuration_missing",
    )
    assert delivery_link_static_preflight_checks(
        {
            "VIBE_MARKET_MORNING_DELIVERY_LINK_BASE_URL": "unsafe",
            "VIBE_MARKET_MORNING_DELIVERY_LINK_SIGNING_SECRET": "s" * 32,
        }
    ) == ("delivery_link_configuration_invalid",)
    assert delivery_link_static_preflight_checks(
        {
            "VIBE_MARKET_MORNING_DELIVERY_LINK_BASE_URL": (
                "https://morning.example.jp/market-morning"
            ),
            "VIBE_MARKET_MORNING_DELIVERY_LINK_SIGNING_SECRET": "s" * 32,
        }
    ) == ()


def test_redemption_statement_is_user_bound_and_never_contains_raw_token() -> None:
    from src.market_morning.delivery_links import (
        build_delivery_link_redemption_statement,
    )

    raw_token = "A" * 43
    statement = build_delivery_link_redemption_statement(
        user_id=USER_ID,
        token_sha256=hashlib.sha256(raw_token.encode()).hexdigest(),
        requested_after=NOW.replace(tzinfo=None) - timedelta(days=7),
    )
    compiled = str(
        statement.compile(
            dialect=mysql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )

    assert "delivery_attempts.user_id" in compiled
    assert "delivery_attempts.deep_link_token_sha256" in compiled
    assert "delivery_attempts.requested_at" in compiled
    assert raw_token not in compiled


def test_redeem_delivery_link_accepts_exactly_one_recent_user_bound_attempt() -> None:
    from src.market_morning.delivery_links import redeem_delivery_link

    token = "A" * 43
    session = _Session(
        [SimpleNamespace(delivery_attempt_id=ATTEMPT_ID, edition_date=EDITION_DATE)]
    )
    result = asyncio.run(
        redeem_delivery_link(
            user_id=USER_ID,
            token=token,
            redeemed_at=NOW,
            session_factory=_Factory(session),
        )
    )

    assert result.edition_date == EDITION_DATE
    assert result.destination_path == "/market-morning"
    compiled = str(
        session.statements[0].compile(
            dialect=mysql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    assert USER_ID in compiled
    assert OTHER_USER_ID not in compiled
    assert token not in compiled


def test_redeem_delivery_link_rejects_missing_duplicate_and_invalid_tokens() -> None:
    import pytest

    from src.market_morning.delivery_links import (
        DeliveryLinkUnavailable,
        redeem_delivery_link,
    )

    for rows in (
        [],
        [
            SimpleNamespace(delivery_attempt_id=ATTEMPT_ID, edition_date=EDITION_DATE),
            SimpleNamespace(delivery_attempt_id=RUN_ID, edition_date=EDITION_DATE),
        ],
    ):
        with pytest.raises(DeliveryLinkUnavailable, match="delivery_link_unavailable"):
            asyncio.run(
                redeem_delivery_link(
                    user_id=USER_ID,
                    token="A" * 43,
                    redeemed_at=NOW,
                    session_factory=_Factory(_Session(rows)),
                )
            )

    with pytest.raises(ValueError, match="delivery token is invalid"):
        asyncio.run(
            redeem_delivery_link(
                user_id=USER_ID,
                token="invalid",
                redeemed_at=NOW,
                session_factory=_Factory(_Session([])),
            )
        )


def _client(monkeypatch, redeem):
    from src.api import market_morning_routes
    from src.api.market_morning_auth import (
        MarketMorningPrincipal,
        require_market_morning_principal,
    )

    async def allow_internal_request() -> None:
        return None

    async def principal() -> MarketMorningPrincipal:
        return MarketMorningPrincipal(
            user_id=USER_ID,
            external_subject="verified-subject",
        )

    monkeypatch.setitem(
        sys.modules,
        "api_server",
        SimpleNamespace(require_auth=allow_internal_request),
    )
    monkeypatch.setattr(market_morning_routes, "redeem_delivery_link", redeem)
    app = FastAPI()
    market_morning_routes.register_market_morning_routes(app)
    app.dependency_overrides[require_market_morning_principal] = principal
    return TestClient(app, client=("127.0.0.1", 50000))


def test_delivery_link_api_uses_body_token_and_authenticated_user(monkeypatch) -> None:
    from src.market_morning.delivery_links import DeliveryLinkRedemption

    calls = []

    async def redeem(**kwargs):
        calls.append(kwargs)
        return DeliveryLinkRedemption(edition_date=EDITION_DATE)

    token = "A" * 43
    response = _client(monkeypatch, redeem).post(
        "/market-morning/delivery-links/redeem",
        json={"token": token},
    )

    assert response.status_code == 200
    assert response.json() == {
        "status": "accepted",
        "edition_date": "2026-07-21",
        "destination_path": "/market-morning",
    }
    assert calls[0]["user_id"] == USER_ID
    assert calls[0]["token"] == token
    assert calls[0]["redeemed_at"].tzinfo is timezone.utc
    assert token not in response.text


def test_delivery_link_api_has_stable_not_found_and_database_errors(monkeypatch) -> None:
    from src.market_morning.delivery_links import DeliveryLinkUnavailable

    async def unavailable(**kwargs):
        raise DeliveryLinkUnavailable("sensitive-token-context")

    response = _client(monkeypatch, unavailable).post(
        "/market-morning/delivery-links/redeem",
        json={"token": "A" * 43},
    )
    assert response.status_code == 404
    assert response.json() == {"detail": "Delivery link is unavailable"}
    assert "sensitive-token-context" not in response.text

    async def database_error(**kwargs):
        raise SQLAlchemyError("sensitive database context")

    response = _client(monkeypatch, database_error).post(
        "/market-morning/delivery-links/redeem",
        json={"token": "A" * 43},
    )
    assert response.status_code == 503
    assert response.json() == {
        "detail": "Delivery link is temporarily unavailable"
    }
    assert "sensitive database context" not in response.text


def test_delivery_link_api_rejects_bad_schema_before_redemption(monkeypatch) -> None:
    async def redeem(**kwargs):
        raise AssertionError("redeem must not run")

    client = _client(monkeypatch, redeem)
    response = client.post(
        "/market-morning/delivery-links/redeem",
        json={"token": "bad", "user_id": OTHER_USER_ID},
    )

    assert response.status_code == 422
