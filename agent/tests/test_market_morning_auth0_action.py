"""Deployment contract for the Auth0 Free/Development post-login Action."""

from pathlib import Path


ACTION = Path("deploy/auth0/market-morning-post-login.js")
README = Path("deploy/auth0/README.md")
AUTH_TIME_CLAIM = "https://market-morning.invalid/claims/auth-time"
ROLES_CLAIM = "https://market-morning.invalid/claims/roles"


def test_auth0_action_uses_free_tier_safe_namespaced_claims() -> None:
    assert ACTION.exists()
    source = ACTION.read_text(encoding="utf-8")

    assert AUTH_TIME_CLAIM in source
    assert ROLES_CLAIM in source
    assert "api.accessToken.setCustomClaim" in source
    assert "event.authentication" in source
    assert "event.user" in source and "app_metadata" in source
    assert "event.authorization" in source
    assert "MARKET_MORNING_OPERATOR_CLIENT_ID" in source


def test_auth0_action_does_not_depend_on_enterprise_session_metadata() -> None:
    assert ACTION.exists()
    source = ACTION.read_text(encoding="utf-8")

    assert "api.refreshToken.setMetadata" not in source
    assert "api.session.setMetadata" not in source
    assert "event.refresh_token.metadata" not in source
    assert "event.session.metadata" not in source


def test_auth0_staging_runbook_pins_short_lived_access_tokens() -> None:
    assert README.exists()
    source = README.read_text(encoding="utf-8")

    assert "Maximum Access Token Lifetime" in source
    assert "900" in source
    assert "重新登录" in source
