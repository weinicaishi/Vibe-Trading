"""Deployment-owned operator identity and least-privilege boundary."""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

TOKEN = "signed-admin-token-with-sensitive-content"


def _install_factory(monkeypatch, adapter) -> None:
    from src.api import market_morning_admin_auth as admin_auth

    monkeypatch.setenv("VIBE_MARKET_MORNING_ENABLED", "true")
    monkeypatch.setenv(
        "VIBE_MARKET_MORNING_ADMIN_AUTH_FACTORY",
        "test_market_morning_admin_deployment:build_adapter",
    )
    monkeypatch.setitem(
        sys.modules,
        "test_market_morning_admin_deployment",
        SimpleNamespace(build_adapter=lambda: adapter),
    )
    admin_auth.clear_admin_auth_adapter_cache()


def _client() -> TestClient:
    from src.api.market_morning_admin_auth import (
        VerifiedMarketMorningOperator,
        require_access_manager,
        require_content_reviewer,
        require_operations_reader,
        require_publication_controller,
        revoke_current_operator_session,
    )

    app = FastAPI()

    def payload(operator: VerifiedMarketMorningOperator):
        return {
            "actor_reference": operator.actor_reference,
            "permissions": sorted(operator.permissions),
        }

    @app.get("/read")
    async def read(
        operator: VerifiedMarketMorningOperator = Depends(require_operations_reader),
    ):
        return payload(operator)

    @app.post("/review")
    async def review(
        operator: VerifiedMarketMorningOperator = Depends(require_content_reviewer),
    ):
        return payload(operator)

    @app.post("/publication")
    async def publication(
        operator: VerifiedMarketMorningOperator = Depends(require_publication_controller),
    ):
        return payload(operator)

    @app.post("/access")
    async def access(
        operator: VerifiedMarketMorningOperator = Depends(require_access_manager),
    ):
        return payload(operator)

    @app.delete("/logout", status_code=204)
    async def logout(_revoked: None = Depends(revoke_current_operator_session)):
        return None

    return TestClient(app, client=("127.0.0.1", 50000))


@pytest.fixture(autouse=True)
def _clear_cache():
    from src.api import market_morning_admin_auth as admin_auth

    clear = getattr(admin_auth, "clear_admin_auth_adapter_cache", None)
    if clear is not None:
        clear()
    yield
    if clear is not None:
        clear()


def test_admin_auth_factory_loads_a_bounded_operator_contract(monkeypatch) -> None:
    from src.api import market_morning_admin_auth as admin_auth

    async def verify(_token: str):
        return admin_auth.VerifiedMarketMorningOperator(
            actor_reference="oidc|operator-17",
            permissions=frozenset({"operations.read", "content.review"}),
        )

    adapter = admin_auth.MarketMorningAdminAuthAdapter(
        provider="test-oidc",
        verify_bearer=verify,
    )
    calls = []
    monkeypatch.setitem(
        sys.modules,
        "test_market_morning_admin_deployment",
        SimpleNamespace(build=lambda: calls.append("load") or adapter),
    )

    assert admin_auth.load_admin_auth_adapter("test_market_morning_admin_deployment:build") is adapter
    assert calls == ["load"]


def test_configured_operator_requires_bearer_and_enforces_exact_permission(
    monkeypatch,
) -> None:
    from src.api import market_morning_admin_auth as admin_auth

    calls = []

    async def verify(token: str):
        calls.append(token)
        return admin_auth.VerifiedMarketMorningOperator(
            actor_reference="oidc|operator-17",
            permissions=frozenset({"operations.read"}),
        )

    _install_factory(
        monkeypatch,
        admin_auth.MarketMorningAdminAuthAdapter("test-oidc", verify),
    )
    client = _client()

    missing = client.get("/read")
    allowed = client.get(
        "/read",
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    forbidden = client.post(
        "/review",
        headers={"Authorization": f"Bearer {TOKEN}"},
    )

    assert missing.status_code == 401
    assert calls == [TOKEN, TOKEN]
    assert allowed.status_code == 200
    assert allowed.json()["actor_reference"] == "oidc|operator-17"
    assert forbidden.status_code == 403
    assert forbidden.json()["detail"] == "Market Morning operator permission is unavailable"
    assert TOKEN not in forbidden.text


def test_operator_logout_revokes_the_exact_verified_bearer(monkeypatch) -> None:
    from src.api import market_morning_admin_auth as admin_auth

    verified = []
    revoked = []

    async def verify(token: str):
        verified.append(token)
        return admin_auth.VerifiedMarketMorningOperator(
            actor_reference="oidc|operator-17",
            permissions=frozenset({"operations.read"}),
        )

    async def revoke(token: str) -> None:
        revoked.append(token)

    _install_factory(
        monkeypatch,
        admin_auth.MarketMorningAdminAuthAdapter(
            provider="test-oidc",
            verify_bearer=verify,
            revoke_bearer=revoke,
        ),
    )
    response = _client().delete("/logout", headers={"Authorization": f"Bearer {TOKEN}"})
    assert response.status_code == 204
    assert verified == [TOKEN]
    assert revoked == [TOKEN]
    assert TOKEN not in response.text


def test_operator_rejection_and_provider_outage_are_sanitized(monkeypatch, caplog) -> None:
    from src.api import market_morning_admin_auth as admin_auth

    caplog.set_level("INFO", logger=admin_auth.__name__)

    async def reject(_token: str):
        raise admin_auth.MarketMorningAdminAuthenticationRejected("jwt_signature_invalid")

    _install_factory(
        monkeypatch,
        admin_auth.MarketMorningAdminAuthAdapter("test-oidc", reject),
    )
    client = _client()
    rejected = client.get(
        "/read",
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    assert rejected.status_code == 401
    assert "jwt_signature_invalid" not in rejected.text
    assert "rejection_code=jwt_signature_invalid" in caplog.text
    assert TOKEN not in caplog.text

    caplog.clear()

    async def reject_with_unsafe_code(_token: str):
        raise admin_auth.MarketMorningAdminAuthenticationRejected(
            "provider-secret-value\nforged-log-line"
        )

    admin_auth.clear_admin_auth_adapter_cache()
    _install_factory(
        monkeypatch,
        admin_auth.MarketMorningAdminAuthAdapter("test-oidc", reject_with_unsafe_code),
    )
    unsafe_rejection = client.get(
        "/read",
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    assert unsafe_rejection.status_code == 401
    assert "rejection_code=admin_auth_rejected" in caplog.text
    assert "provider-secret-value" not in caplog.text
    assert "forged-log-line" not in caplog.text

    async def unavailable(_token: str):
        raise RuntimeError("provider-host-and-secret-must-not-escape")

    admin_auth.clear_admin_auth_adapter_cache()
    _install_factory(
        monkeypatch,
        admin_auth.MarketMorningAdminAuthAdapter("test-oidc", unavailable),
    )
    outage = client.get(
        "/read",
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    assert outage.status_code == 503
    assert outage.json()["detail"] == ("Market Morning operator authentication is temporarily unavailable")
    assert "provider-host" not in outage.text
    assert TOKEN not in outage.text


def test_legacy_local_control_plane_remains_available_for_development(
    monkeypatch,
) -> None:
    monkeypatch.setenv("VIBE_MARKET_MORNING_ENABLED", "true")
    monkeypatch.delenv("VIBE_MARKET_MORNING_ADMIN_AUTH_FACTORY", raising=False)

    response = _client().post("/access")

    assert response.status_code == 200
    assert response.json()["actor_reference"] == "vibe-api-key-operator"
    assert response.json()["permissions"] == [
        "access.manage",
        "content.review",
        "operations.read",
        "publication.control",
    ]


def test_operator_contract_rejects_controls_unknown_permissions_and_empty_roles() -> None:
    from src.api import market_morning_admin_auth as admin_auth

    with pytest.raises(admin_auth.AdminAuthConfigurationError):
        admin_auth.VerifiedMarketMorningOperator(
            actor_reference="oidc|operator\nforged",
            permissions=frozenset({"operations.read"}),
        )
    with pytest.raises(admin_auth.AdminAuthConfigurationError):
        admin_auth.VerifiedMarketMorningOperator(
            actor_reference="oidc|operator",
            permissions=frozenset(),
        )
    with pytest.raises(admin_auth.AdminAuthConfigurationError):
        admin_auth.VerifiedMarketMorningOperator(
            actor_reference="oidc|operator",
            permissions=frozenset({"root.everything"}),
        )


def test_admin_auth_configuration_defaults_closed() -> None:
    from src.config.env_schema import EnvConfig

    assert EnvConfig().market_morning.admin_auth_factory == ""


def test_admin_routes_use_personal_actor_and_enforce_route_permissions(
    monkeypatch,
) -> None:
    from src.api import market_morning_admin_auth as admin_auth
    from src.api import market_morning_admin_routes
    from src.market_morning.event_brief_review import EventBriefReviewResult

    async def verify(token: str):
        assert token == TOKEN
        return admin_auth.VerifiedMarketMorningOperator(
            actor_reference="oidc|operator-17",
            permissions=frozenset({"content.review"}),
        )

    _install_factory(
        monkeypatch,
        admin_auth.MarketMorningAdminAuthAdapter("test-oidc", verify),
    )

    async def _legacy_allow() -> None:
        return None

    monkeypatch.setitem(
        sys.modules,
        "api_server",
        SimpleNamespace(require_auth=_legacy_allow),
    )
    app = FastAPI()
    market_morning_admin_routes.register_market_morning_admin_routes(app)
    calls = []

    async def _review(**kwargs):
        calls.append(kwargs)
        return EventBriefReviewResult(
            status="reviewed",
            brief_id=kwargs["brief_id"],
            review_status="approved",
            reviewed_at=datetime(2026, 7, 21, 1, 0, tzinfo=timezone.utc),
        )

    async def _must_not_read(**_kwargs):
        raise AssertionError("read service must not run without operations.read")

    monkeypatch.setattr(
        market_morning_admin_routes,
        "apply_event_brief_review",
        _review,
    )
    monkeypatch.setattr(
        market_morning_admin_routes,
        "get_operations_summary",
        _must_not_read,
    )
    client = TestClient(app, client=("127.0.0.1", 50000))
    headers = {"Authorization": f"Bearer {TOKEN}"}
    brief_id = "11111111-1111-4111-8111-111111111111"

    reviewed = client.post(
        f"/market-morning/_internal/event-briefs/{brief_id}/review",
        headers=headers,
        json={"decision": "approve", "reason_code": "manual_quality_review"},
    )
    forbidden_read = client.get(
        "/market-morning/_internal/operations/summary",
        headers=headers,
    )
    forbidden_halt = client.post(
        "/market-morning/_internal/operations/halts",
        headers=headers,
        json={
            "edition_date": "2026-07-22",
            "reason_code": "operator_review",
        },
    )

    assert reviewed.status_code == 200
    assert calls == [
        {
            "brief_id": brief_id,
            "decision": "approve",
            "reason_code": "manual_quality_review",
            "actor_reference": "oidc|operator-17",
        }
    ]
    assert forbidden_read.status_code == 403
    assert forbidden_halt.status_code == 403


def test_deployment_preflight_requires_operations_read_permission(
    monkeypatch,
) -> None:
    from src.api import market_morning_admin_auth as admin_auth
    from src.api import market_morning_routes

    async def verify(_token: str):
        return admin_auth.VerifiedMarketMorningOperator(
            actor_reference="oidc|content-reviewer",
            permissions=frozenset({"content.review"}),
        )

    _install_factory(
        monkeypatch,
        admin_auth.MarketMorningAdminAuthAdapter("test-oidc", verify),
    )

    async def _legacy_allow() -> None:
        return None

    monkeypatch.setitem(
        sys.modules,
        "api_server",
        SimpleNamespace(require_auth=_legacy_allow),
    )

    async def _must_not_probe():
        raise AssertionError("database probe must not run before authorization")

    monkeypatch.setattr(market_morning_routes, "probe_database", _must_not_probe)
    app = FastAPI()
    market_morning_routes.register_market_morning_routes(app)
    response = TestClient(app, client=("127.0.0.1", 50000)).get(
        "/market-morning/_internal/deployment-preflight",
        headers={"Authorization": f"Bearer {TOKEN}"},
    )

    assert response.status_code == 403
