"""Production product-auth HTTP boundary for Market Morning."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.dialects import mysql

USER_ID = "11111111-1111-4111-8111-111111111111"
SUBJECT = "oidc|verified-subject"
TOKEN = "signed-access-token-with-sensitive-content"


class _Result:
    def __init__(self, row=None) -> None:
        self.row = row

    def mappings(self):
        return SimpleNamespace(one_or_none=lambda: self.row)


class _Session:
    def __init__(self, row=None) -> None:
        self.row = row
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)
        return _Result(self.row)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class _SessionFactory:
    def __init__(self, row=None) -> None:
        self.session = _Session(row)

    def __call__(self):
        return self.session


def _install_factory(monkeypatch, adapter) -> None:
    from src.api import market_morning_auth

    monkeypatch.setenv("VIBE_MARKET_MORNING_ENABLED", "true")
    monkeypatch.setenv(
        "VIBE_MARKET_MORNING_AUTH_FACTORY",
        "test_market_morning_auth_deployment:build_adapter",
    )
    monkeypatch.setitem(
        sys.modules,
        "test_market_morning_auth_deployment",
        SimpleNamespace(build_adapter=lambda: adapter),
    )
    market_morning_auth.clear_product_auth_adapter_cache()


def _auth_client() -> TestClient:
    from src.api.market_morning_auth import (
        MarketMorningOnboardingPrincipal,
        MarketMorningPrincipal,
        require_market_morning_onboarding_principal,
        require_market_morning_principal,
        require_recent_market_morning_principal,
        revoke_current_product_session,
    )

    app = FastAPI()

    @app.get("/onboarding")
    async def onboarding(
        principal: MarketMorningOnboardingPrincipal = Depends(require_market_morning_onboarding_principal),
    ):
        return {"external_subject": principal.external_subject}

    @app.get("/product")
    async def product(
        principal: MarketMorningPrincipal = Depends(require_market_morning_principal),
    ):
        return {
            "user_id": principal.user_id,
            "external_subject": principal.external_subject,
        }

    @app.get("/recent")
    async def recent(
        principal: MarketMorningPrincipal = Depends(require_recent_market_morning_principal),
    ):
        return {"user_id": principal.user_id}

    @app.delete("/logout", status_code=204)
    async def logout(_revoked: None = Depends(revoke_current_product_session)):
        return None

    return TestClient(app, client=("127.0.0.1", 50000))


@pytest.fixture(autouse=True)
def _clear_auth_cache():
    from src.api import market_morning_auth

    clear = getattr(market_morning_auth, "clear_product_auth_adapter_cache", None)
    if clear is not None:
        clear()
    yield
    if clear is not None:
        clear()


def test_product_auth_factory_contract_is_loaded_once(monkeypatch) -> None:
    from src.api import market_morning_auth

    assert hasattr(market_morning_auth, "MarketMorningProductAuthAdapter")
    MarketMorningProductAuthAdapter = market_morning_auth.MarketMorningProductAuthAdapter
    VerifiedMarketMorningIdentity = market_morning_auth.VerifiedMarketMorningIdentity
    load_product_auth_adapter = market_morning_auth.load_product_auth_adapter

    calls = []

    async def verify(_token: str) -> VerifiedMarketMorningIdentity:
        return VerifiedMarketMorningIdentity(external_subject=SUBJECT)

    adapter = MarketMorningProductAuthAdapter(
        provider="test-oidc",
        verify_bearer=verify,
        session_factory=_SessionFactory(),
    )
    module = SimpleNamespace(build=lambda: calls.append("load") or adapter)
    monkeypatch.setitem(sys.modules, "test_auth_factory", module)

    assert load_product_auth_adapter("test_auth_factory:build") is adapter
    assert calls == ["load"]


def test_onboarding_requires_bearer_without_calling_verifier(monkeypatch) -> None:
    from src.api.market_morning_auth import (
        MarketMorningProductAuthAdapter,
        VerifiedMarketMorningIdentity,
    )

    calls = []

    async def verify(token: str) -> VerifiedMarketMorningIdentity:
        calls.append(token)
        return VerifiedMarketMorningIdentity(external_subject=SUBJECT)

    _install_factory(
        monkeypatch,
        MarketMorningProductAuthAdapter("test-oidc", verify),
    )

    response = _auth_client().get("/onboarding")

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert calls == []


def test_onboarding_uses_verified_subject_without_database_access(monkeypatch) -> None:
    from src.api.market_morning_auth import (
        MarketMorningProductAuthAdapter,
        VerifiedMarketMorningIdentity,
    )

    calls = []

    async def verify(token: str) -> VerifiedMarketMorningIdentity:
        calls.append(token)
        return VerifiedMarketMorningIdentity(external_subject=SUBJECT)

    _install_factory(
        monkeypatch,
        MarketMorningProductAuthAdapter("test-oidc", verify),
    )

    response = _auth_client().get(
        "/onboarding",
        headers={"Authorization": f"Bearer {TOKEN}"},
    )

    assert response.status_code == 200
    assert response.json() == {"external_subject": SUBJECT}
    assert calls == [TOKEN]


def test_product_logout_revokes_the_exact_verified_bearer(monkeypatch) -> None:
    from src.api.market_morning_auth import (
        MarketMorningProductAuthAdapter,
        VerifiedMarketMorningIdentity,
    )

    verified = []
    revoked = []

    async def verify(token: str) -> VerifiedMarketMorningIdentity:
        verified.append(token)
        return VerifiedMarketMorningIdentity(external_subject=SUBJECT)

    async def revoke(token: str) -> None:
        revoked.append(token)

    _install_factory(
        monkeypatch,
        MarketMorningProductAuthAdapter(
            provider="test-oidc",
            verify_bearer=verify,
            revoke_bearer=revoke,
        ),
    )
    response = _auth_client().delete("/logout", headers={"Authorization": f"Bearer {TOKEN}"})
    assert response.status_code == 204
    assert verified == [TOKEN]
    assert revoked == [TOKEN]
    assert TOKEN not in response.text


def test_product_principal_maps_verified_subject_to_active_entitled_user(
    monkeypatch,
) -> None:
    from src.api.market_morning_auth import (
        MarketMorningProductAuthAdapter,
        VerifiedMarketMorningIdentity,
    )

    session_factory = _SessionFactory(
        {
            "user_id": USER_ID,
            "external_subject": SUBJECT,
        }
    )

    async def verify(token: str) -> VerifiedMarketMorningIdentity:
        assert token == TOKEN
        return VerifiedMarketMorningIdentity(external_subject=SUBJECT)

    _install_factory(
        monkeypatch,
        MarketMorningProductAuthAdapter(
            "test-oidc",
            verify,
            session_factory=session_factory,
        ),
    )

    response = _auth_client().get(
        "/product",
        headers={"Authorization": f"Bearer {TOKEN}"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "user_id": USER_ID,
        "external_subject": SUBJECT,
    }
    sql = str(
        session_factory.session.statements[0].compile(
            dialect=mysql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    assert "mm_users.account_status = 'active'" in sql
    assert "mm_users.deleted_at IS NULL" in sql
    assert "private_beta" in sql
    assert TOKEN not in sql


def test_valid_identity_without_active_product_access_is_forbidden(monkeypatch) -> None:
    from src.api.market_morning_auth import (
        MarketMorningProductAuthAdapter,
        VerifiedMarketMorningIdentity,
    )

    async def verify(_token: str) -> VerifiedMarketMorningIdentity:
        return VerifiedMarketMorningIdentity(external_subject=SUBJECT)

    _install_factory(
        monkeypatch,
        MarketMorningProductAuthAdapter(
            "test-oidc",
            verify,
            session_factory=_SessionFactory(None),
        ),
    )

    response = _auth_client().get(
        "/product",
        headers={"Authorization": f"Bearer {TOKEN}"},
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Market Morning access is unavailable"
    assert SUBJECT not in response.text


@pytest.mark.parametrize(
    ("authenticated_at", "expected_status"),
    (
        (None, 401),
        (datetime.now(timezone.utc) - timedelta(minutes=10), 401),
        (datetime.now(timezone.utc), 200),
    ),
)
def test_sensitive_action_requires_recent_provider_authentication(
    monkeypatch,
    authenticated_at: datetime | None,
    expected_status: int,
) -> None:
    from src.api.market_morning_auth import (
        MarketMorningProductAuthAdapter,
        VerifiedMarketMorningIdentity,
    )

    async def verify(_token: str) -> VerifiedMarketMorningIdentity:
        return VerifiedMarketMorningIdentity(
            external_subject=SUBJECT,
            authenticated_at=authenticated_at,
        )

    _install_factory(
        monkeypatch,
        MarketMorningProductAuthAdapter(
            "test-oidc",
            verify,
            session_factory=_SessionFactory({"user_id": USER_ID, "external_subject": SUBJECT}),
        ),
    )

    response = _auth_client().get(
        "/recent",
        headers={"Authorization": f"Bearer {TOKEN}"},
    )

    assert response.status_code == expected_status
    if expected_status == 401:
        assert response.json()["detail"] == "Recent authentication is required"
        assert response.headers["www-authenticate"] == "Bearer"


def test_rejected_token_and_provider_outage_have_distinct_sanitized_responses(
    monkeypatch,
) -> None:
    from src.api.market_morning_auth import (
        MarketMorningAuthenticationRejected,
        MarketMorningProductAuthAdapter,
    )

    async def reject(_token: str):
        raise MarketMorningAuthenticationRejected("oidc_signature_invalid")

    _install_factory(
        monkeypatch,
        MarketMorningProductAuthAdapter("test-oidc", reject),
    )
    client = _auth_client()
    rejected = client.get(
        "/onboarding",
        headers={"Authorization": f"Bearer {TOKEN}"},
    )

    assert rejected.status_code == 401
    assert rejected.headers["www-authenticate"] == "Bearer"
    assert "oidc_signature_invalid" not in rejected.text
    assert TOKEN not in rejected.text

    async def unavailable(_token: str):
        raise RuntimeError("provider-host-and-secret-must-not-escape")

    from src.api import market_morning_auth

    market_morning_auth.clear_product_auth_adapter_cache()
    _install_factory(
        monkeypatch,
        MarketMorningProductAuthAdapter("test-oidc", unavailable),
    )
    outage = client.get(
        "/onboarding",
        headers={"Authorization": f"Bearer {TOKEN}"},
    )

    assert outage.status_code == 503
    assert outage.json()["detail"] == ("Market Morning product authentication is temporarily unavailable")
    assert "provider-host" not in outage.text
    assert TOKEN not in outage.text


def test_verified_identity_rejects_controls_and_oversized_subject() -> None:
    from src.api.market_morning_auth import (
        ProductAuthConfigurationError,
        VerifiedMarketMorningIdentity,
    )

    with pytest.raises(ProductAuthConfigurationError):
        VerifiedMarketMorningIdentity(external_subject="oidc|subject\nforged")
    with pytest.raises(ProductAuthConfigurationError):
        VerifiedMarketMorningIdentity(external_subject="x" * 256)


def test_account_deletion_principal_query_allows_only_active_or_pending() -> None:
    from src.api import market_morning_auth

    assert hasattr(
        market_morning_auth,
        "build_account_deletion_principal_statement",
    )
    statement = market_morning_auth.build_account_deletion_principal_statement(external_subject=SUBJECT)
    sql = str(
        statement.compile(
            dialect=mysql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )

    assert "mm_users.account_status IN ('active', 'deletion_pending')" in sql
    assert "mm_users.deleted_at IS NULL" in sql
    assert "suspended" not in sql
    assert "deleted'" not in sql


def test_auth_configuration_defaults_closed() -> None:
    from src.config.env_schema import EnvConfig

    assert EnvConfig().market_morning.auth_factory == ""
