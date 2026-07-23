"""Sprint 0-1 contracts for the isolated Market Morning foundation."""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.dialects import mysql
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.schema import CreateTable

from src.api import market_morning_routes
from src.api.market_morning_auth import (
    MarketMorningPrincipal,
    require_account_deletion_principal,
    require_market_morning_principal,
)
from src.market_morning.db import MarketMorningDatabaseNotConfigured, _validated_database_url
from src.market_morning.issuer_search import IssuerSearchMatch
from src.market_morning.settings import (
    ConsentMutationResult,
    ConsentMutationStatus,
    ConsentStateView,
    ConsentType,
    DeletionRequestResult,
    DeletionRequestStatus,
    SettingsValidationError,
    UserSettingsView,
)
from src.market_morning.source_queries import EventAuditView, SourceHealthView
from src.market_morning.edition_service import build_today_edition
from src.market_morning.watchlist import (
    WatchlistItemView,
    WatchlistLimitReached,
)
from src.market_morning.models import Base
from src.market_morning.normalization import normalize_issuer_search_key


def test_japanese_issuer_normalization_is_deterministic() -> None:
    variants = (
        "トヨタ自動車株式会社",
        "株式会社 トヨタ自動車",
        "（株）トヨタ 自動車",
        "トヨタ・自動車（株）",
    )

    assert {normalize_issuer_search_key(value) for value in variants} == {"トヨタ自動車"}


def test_normalization_handles_width_and_latin_case() -> None:
    assert normalize_issuer_search_key(" ＡＢＣ　ﾎｰﾙﾃﾞｨﾝｸﾞｽ株式会社 ") == "abcホールディングス"


def test_database_url_requires_async_mysql_driver() -> None:
    with pytest.raises(MarketMorningDatabaseNotConfigured, match=r"mysql\+asyncmy"):
        _validated_database_url("mysql://user:secret@db/market_morning")


def test_database_url_requires_database_name() -> None:
    with pytest.raises(MarketMorningDatabaseNotConfigured, match="name a database"):
        _validated_database_url("mysql+asyncmy://user:secret@db")


def test_foundation_metadata_contains_only_prefixed_tables() -> None:
    expected = {
        "mm_users",
        "mm_private_beta_invites",
        "mm_user_consents",
        "mm_issuer_snapshots",
        "mm_issuers",
        "mm_issuer_aliases",
        "mm_audit_logs",
        "mm_watchlist_items",
        "mm_account_deletion_requests",
        "mm_analytics_events",
        "mm_source_cursors",
        "mm_source_records",
        "mm_normalized_events",
        "mm_event_sources",
        "mm_event_merge_candidates",
        "mm_morning_editions",
        "mm_jobs",
        "mm_job_attempts",
        "mm_scheduler_leases",
        "mm_manual_overrides",
        "mm_market_snapshots",
        "mm_global_edition_days",
        "mm_global_edition_runs",
        "mm_global_edition_items",
        "mm_event_briefs",
        "mm_event_brief_sources",
        "mm_model_usage_events",
        "mm_content_reports",
        "mm_auth_sessions",
        "mm_delivery_attempts",
        "mm_delivery_provider_events",
        "mm_edition_event_states",
        "mm_edition_source_opens",
        "mm_issuer_research_notes",
    }
    assert set(Base.metadata.tables) == expected
    assert all(name.startswith("mm_") for name in Base.metadata.tables)


def test_mysql_ddl_uses_innodb_utf8mb4_and_binary_search_collation() -> None:
    issuer_table = Base.metadata.tables["mm_issuers"]
    ddl = str(CreateTable(issuer_table).compile(dialect=mysql.dialect()))

    assert "ENGINE=InnoDB" in ddl
    assert "CHARSET=utf8mb4" in ddl
    assert "COLLATE utf8mb4_bin" in ddl


def test_only_one_active_approved_alias_can_own_a_search_key() -> None:
    alias_table = Base.metadata.tables["mm_issuer_aliases"]
    ddl = str(CreateTable(alias_table).compile(dialect=mysql.dialect()))

    assert "approved_search_key" in ddl
    assert "GENERATED ALWAYS AS" in ddl
    assert "review_status = 'approved' AND effective_to IS NULL" in ddl
    assert "uq_mm_issuer_alias_approved_search_key" in ddl


def test_watchlist_uses_generated_active_key_for_idempotency() -> None:
    table = Base.metadata.tables["mm_watchlist_items"]
    ddl = str(CreateTable(table).compile(dialect=mysql.dialect()))

    assert "active_issuer_id" in ddl
    assert "CASE WHEN removed_at IS NULL THEN issuer_id ELSE NULL END" in ddl
    assert "uq_mm_watchlist_active_user_issuer" in ddl


def test_deletion_request_uses_generated_active_user_key() -> None:
    table = Base.metadata.tables["mm_account_deletion_requests"]
    ddl = str(CreateTable(table).compile(dialect=mysql.dialect()))

    assert "active_user_id" in ddl
    assert "status IN ('pending', 'processing')" in ddl
    assert "uq_mm_account_deletion_active_user" in ddl


def test_analytics_event_contract_is_json_and_idempotency_ready() -> None:
    table = Base.metadata.tables["mm_analytics_events"]
    ddl = str(CreateTable(table).compile(dialect=mysql.dialect()))

    assert "properties JSON NOT NULL" in ddl
    assert "uq_mm_analytics_event_request" in ddl


def test_source_record_revision_is_idempotent_and_auditable() -> None:
    table = Base.metadata.tables["mm_source_records"]
    ddl = str(CreateTable(table).compile(dialect=mysql.dialect()))

    assert "uq_mm_source_record_provider_document_revision" in ddl
    assert "original_url TEXT NOT NULL" in ddl
    assert "fetched_at DATETIME(6) NOT NULL" in ddl
    assert "supersedes_record_id" in ddl


def test_event_versions_and_merge_candidates_never_overwrite_history() -> None:
    event_ddl = str(CreateTable(Base.metadata.tables["mm_normalized_events"]).compile(dialect=mysql.dialect()))
    candidate_ddl = str(CreateTable(Base.metadata.tables["mm_event_merge_candidates"]).compile(dialect=mysql.dialect()))

    assert "uq_mm_event_family_version" in event_ddl
    assert "supersedes_event_id" in event_ddl
    assert "uq_mm_event_merge_candidate_pair" in candidate_ddl
    assert "review_status" in candidate_ddl


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    async def _allow_test_request() -> None:
        return None

    host = SimpleNamespace(require_auth=_allow_test_request)
    monkeypatch.setitem(sys.modules, "api_server", host)
    app = FastAPI()
    market_morning_routes.register_market_morning_routes(app)
    return TestClient(app, client=("127.0.0.1", 50000))


def _configure_frontend_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VIBE_MARKET_MORNING_PUBLIC_AUTH_PROVIDER", "auth0")
    monkeypatch.setenv(
        "VIBE_MARKET_MORNING_PUBLIC_AUTH0_DOMAIN",
        "tenant.jp.auth0.com",
    )
    monkeypatch.setenv(
        "VIBE_MARKET_MORNING_PUBLIC_AUTH0_AUDIENCE",
        "https://api.market-morning.example",
    )
    monkeypatch.setenv(
        "VIBE_MARKET_MORNING_PUBLIC_AUTH0_PRODUCT_CLIENT_ID",
        "product_client_1234567890",
    )
    monkeypatch.setenv(
        "VIBE_MARKET_MORNING_PUBLIC_AUTH0_OPERATOR_CLIENT_ID",
        "operator_client_123456789",
    )


def test_frontend_runtime_config_defaults_closed_without_auth(
    client: TestClient,
) -> None:
    response = client.get("/market-morning/runtime-config")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.json() == {
        "schema_version": 1,
        "status": "disabled",
        "feature_enabled": False,
        "ui_enabled": False,
        "auth": None,
        "blocking_codes": [],
    }


def test_frontend_runtime_config_returns_only_public_ready_values(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VIBE_MARKET_MORNING_ENABLED", "true")
    monkeypatch.setenv(
        "VIBE_MARKET_MORNING_DATABASE_URL",
        "mysql+asyncmy://market:super-secret@mysql/market_morning",
    )
    monkeypatch.setenv(
        "VIBE_MARKET_MORNING_OIDC_JWKS_URL",
        "https://tenant.jp.auth0.com/.well-known/jwks.json",
    )
    _configure_frontend_runtime(monkeypatch)

    response = client.get("/market-morning/runtime-config")

    assert response.status_code == 200
    assert response.json() == {
        "schema_version": 1,
        "status": "ready",
        "feature_enabled": True,
        "ui_enabled": True,
        "auth": {
            "provider": "auth0",
            "domain": "tenant.jp.auth0.com",
            "audience": "https://api.market-morning.example",
            "product_client_id": "product_client_1234567890",
            "operator_client_id": "operator_client_123456789",
        },
        "blocking_codes": [],
    }
    assert "super-secret" not in response.text
    assert "jwks" not in response.text.lower()


def test_frontend_runtime_config_fails_closed_with_stable_codes(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VIBE_MARKET_MORNING_ENABLED", "true")
    monkeypatch.setenv("VIBE_MARKET_MORNING_PUBLIC_AUTH_PROVIDER", "unknown")
    monkeypatch.setenv(
        "VIBE_MARKET_MORNING_PUBLIC_AUTH0_DOMAIN",
        "http://localhost:8080",
    )
    monkeypatch.setenv(
        "VIBE_MARKET_MORNING_PUBLIC_AUTH0_AUDIENCE",
        "audience with spaces",
    )
    monkeypatch.setenv(
        "VIBE_MARKET_MORNING_PUBLIC_AUTH0_PRODUCT_CLIENT_ID",
        "short",
    )

    response = client.get("/market-morning/runtime-config")

    assert response.status_code == 200
    assert response.json() == {
        "schema_version": 1,
        "status": "misconfigured",
        "feature_enabled": True,
        "ui_enabled": False,
        "auth": None,
        "blocking_codes": [
            "frontend_auth0_audience_invalid",
            "frontend_auth0_domain_invalid",
            "frontend_auth0_operator_client_id_invalid",
            "frontend_auth0_product_client_id_invalid",
            "frontend_auth_provider_invalid",
        ],
    }


def test_frontend_runtime_config_oversized_value_does_not_break_other_routes(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VIBE_MARKET_MORNING_ENABLED", "true")
    _configure_frontend_runtime(monkeypatch)
    monkeypatch.setenv(
        "VIBE_MARKET_MORNING_PUBLIC_AUTH0_DOMAIN",
        f"{'a' * 254}.example",
    )

    response = client.get("/market-morning/runtime-config")

    assert response.status_code == 200
    assert response.json()["status"] == "misconfigured"
    assert response.json()["blocking_codes"] == [
        "frontend_auth0_domain_invalid",
    ]


def test_market_morning_readiness_defaults_closed(client: TestClient) -> None:
    response = client.get("/market-morning/_internal/ready")

    assert response.status_code == 503
    assert response.json()["status"] == "disabled"
    assert response.json()["enabled"] is False


def test_enabled_market_morning_requires_database_url(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VIBE_MARKET_MORNING_ENABLED", "true")
    monkeypatch.delenv("VIBE_MARKET_MORNING_DATABASE_URL", raising=False)

    response = client.get("/market-morning/_internal/ready")

    assert response.status_code == 503
    assert response.json()["status"] == "misconfigured"
    assert response.json()["database_configured"] is False


def test_invalid_database_url_is_reported_as_misconfigured(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VIBE_MARKET_MORNING_ENABLED", "true")
    monkeypatch.setenv(
        "VIBE_MARKET_MORNING_DATABASE_URL",
        "mysql+asyncmy://market:super-secret@mysql",
    )

    response = client.get("/market-morning/_internal/ready")

    assert response.status_code == 503
    assert response.json()["status"] == "misconfigured"
    assert response.json()["database_configured"] is True
    assert "super-secret" not in response.text


def test_enabled_market_morning_reports_ready_without_exposing_url(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VIBE_MARKET_MORNING_ENABLED", "true")
    monkeypatch.setenv(
        "VIBE_MARKET_MORNING_DATABASE_URL",
        "mysql+asyncmy://market:super-secret@mysql/market_morning?charset=utf8mb4",
    )

    async def _ready() -> tuple[bool, str]:
        return True, "ready"

    monkeypatch.setattr(market_morning_routes, "probe_database", _ready)
    response = client.get("/market-morning/_internal/ready")

    assert response.status_code == 200
    assert response.json()["status"] == "ready"
    assert "super-secret" not in response.text


def test_stale_database_schema_is_reported_as_misconfigured(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VIBE_MARKET_MORNING_ENABLED", "true")
    monkeypatch.setenv(
        "VIBE_MARKET_MORNING_DATABASE_URL",
        "mysql+asyncmy://market:super-secret@mysql/market_morning",
    )

    async def _stale() -> tuple[bool, str]:
        return False, "Market Morning database schema is not current"

    monkeypatch.setattr(market_morning_routes, "probe_database", _stale)

    response = client.get("/market-morning/_internal/ready")

    assert response.status_code == 503
    assert response.json()["status"] == "misconfigured"
    assert response.json()["reason"] == "Market Morning database schema is not current"
    assert "super-secret" not in response.text


@pytest.mark.parametrize(
    ("revision", "expected_ready", "expected_reason"),
    (
        ("0018_market_morning_auth_sessions", True, "ready"),
        (
            "0016_market_morning_model_usage",
            False,
            "Market Morning database schema is not current",
        ),
        (None, False, "Market Morning database schema is not current"),
    ),
)
def test_database_readiness_requires_exact_alembic_revision(
    monkeypatch: pytest.MonkeyPatch,
    revision: str | None,
    expected_ready: bool,
    expected_reason: str,
) -> None:
    from src.market_morning import db

    class _Result:
        def scalar_one_or_none(self):
            return revision

    class _Connection:
        def __init__(self) -> None:
            self.statements = []

        async def execute(self, statement):
            self.statements.append(str(statement))
            return _Result()

    class _ConnectionContext:
        def __init__(self, connection) -> None:
            self.connection = connection

        async def __aenter__(self):
            return self.connection

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    class _Engine:
        def __init__(self) -> None:
            self.connection = _Connection()

        def connect(self):
            return _ConnectionContext(self.connection)

    engine = _Engine()
    monkeypatch.setenv("VIBE_MARKET_MORNING_ENABLED", "true")
    monkeypatch.setattr(db, "get_engine", lambda: engine)

    ready, reason = asyncio.run(db.probe_database())

    assert ready is expected_ready
    assert reason == expected_reason
    assert engine.connection.statements == ["SELECT version_num FROM alembic_version"]


def test_deployment_preflight_lists_only_stable_blocking_checks(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VIBE_MARKET_MORNING_ENABLED", "true")
    monkeypatch.setenv(
        "VIBE_MARKET_MORNING_DATABASE_URL",
        "mysql+asyncmy://market:super-secret@mysql/market_morning",
    )

    async def _stale() -> tuple[bool, str]:
        return False, "Market Morning database schema is not current"

    monkeypatch.setattr(market_morning_routes, "probe_database", _stale)

    response = client.get("/market-morning/_internal/deployment-preflight")

    assert response.status_code == 503
    assert response.json() == {
        "status": "blocked",
        "scope": "static_configuration_and_database",
        "enabled": True,
        "database_ready": False,
        "product_auth_configured": False,
        "admin_auth_configured": False,
        "frontend_runtime_configured": False,
        "oidc_configuration_ready": True,
        "runtime_enabled": False,
        "runtime_factory_configured": False,
        "provider_bundle_factory_configured": False,
        "email_webhook_configured": False,
        "email_identity_factory_configured": False,
        "delivery_link_configuration_ready": False,
        "synthetic_data_disabled": True,
        "fixture_runtime_disabled": True,
        "counts_as_t1_evidence": False,
        "blocking_checks": [
            "admin_auth_factory_missing",
            "database_not_ready",
            "delivery_link_configuration_missing",
            "email_identity_factory_missing",
            "email_webhook_factory_missing",
            "frontend_runtime_config_missing",
            "product_auth_factory_missing",
            "runtime_disabled",
            "runtime_factory_missing",
        ],
        "timestamp": response.json()["timestamp"],
    }
    assert "super-secret" not in response.text
    assert "schema is not current" not in response.text


def test_deployment_preflight_reports_configuration_ready_without_values(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VIBE_MARKET_MORNING_ENABLED", "true")
    monkeypatch.setenv(
        "VIBE_MARKET_MORNING_DATABASE_URL",
        "mysql+asyncmy://market:super-secret@mysql/market_morning",
    )
    monkeypatch.setenv(
        "VIBE_MARKET_MORNING_AUTH_FACTORY",
        "deployment.identity:build",
    )
    monkeypatch.setenv(
        "VIBE_MARKET_MORNING_ADMIN_AUTH_FACTORY",
        "test_market_morning_admin_preflight:build",
    )
    from src.api.market_morning_admin_auth import (
        MarketMorningAdminAuthAdapter,
        VerifiedMarketMorningOperator,
        clear_admin_auth_adapter_cache,
    )

    async def _verify_admin(token: str) -> VerifiedMarketMorningOperator:
        assert token == "signed-admin-token"
        return VerifiedMarketMorningOperator(
            actor_reference="oidc|preflight-operator",
            permissions=frozenset({"operations.read"}),
        )

    monkeypatch.setitem(
        sys.modules,
        "test_market_morning_admin_preflight",
        SimpleNamespace(
            build=lambda: MarketMorningAdminAuthAdapter(
                provider="test-oidc",
                verify_bearer=_verify_admin,
            )
        ),
    )
    clear_admin_auth_adapter_cache()
    monkeypatch.setenv(
        "VIBE_MARKET_MORNING_RUNTIME_ENABLED",
        "true",
    )
    monkeypatch.setenv(
        "VIBE_MARKET_MORNING_RUNTIME_FACTORY",
        "deployment.runtime:build",
    )
    monkeypatch.setenv(
        "VIBE_MARKET_MORNING_EMAIL_WEBHOOK_FACTORY",
        "deployment.email:build_webhooks",
    )
    monkeypatch.setenv(
        "VIBE_MARKET_MORNING_EMAIL_IDENTITY_FACTORY",
        "deployment.identity:build_email_identity",
    )
    monkeypatch.setenv(
        "VIBE_MARKET_MORNING_DELIVERY_LINK_BASE_URL",
        "https://morning.example.jp/market-morning",
    )
    monkeypatch.setenv(
        "VIBE_MARKET_MORNING_DELIVERY_LINK_SIGNING_SECRET",
        "deployment-only-signing-secret-32",
    )
    _configure_frontend_runtime(monkeypatch)

    async def _ready() -> tuple[bool, str]:
        return True, "ready"

    monkeypatch.setattr(market_morning_routes, "probe_database", _ready)

    response = client.get(
        "/market-morning/_internal/deployment-preflight",
        headers={"Authorization": "Bearer signed-admin-token"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "configuration_ready"
    assert body["scope"] == "static_configuration_and_database"
    assert body["blocking_checks"] == []
    assert body["counts_as_t1_evidence"] is False
    assert all(
        body[field] is True
        for field in (
            "database_ready",
            "product_auth_configured",
            "admin_auth_configured",
            "frontend_runtime_configured",
            "oidc_configuration_ready",
            "runtime_enabled",
            "runtime_factory_configured",
            "email_webhook_configured",
            "email_identity_factory_configured",
            "delivery_link_configuration_ready",
            "synthetic_data_disabled",
            "fixture_runtime_disabled",
        )
    )
    assert "deployment." not in response.text
    assert "super-secret" not in response.text


def test_deployment_preflight_requires_provider_bundle_for_builtin_runtime_only(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.api.market_morning_admin_auth import VerifiedMarketMorningOperator
    from src.market_morning.production_runtime_factory import (
        PRODUCTION_RUNTIME_FACTORY_PATH,
    )

    async def _operator() -> VerifiedMarketMorningOperator:
        return VerifiedMarketMorningOperator(
            actor_reference="test-preflight-operator",
            permissions=frozenset({"operations.read"}),
        )

    async def _ready() -> tuple[bool, str]:
        return True, "ready"

    client.app.dependency_overrides[market_morning_routes.require_operations_reader] = _operator
    monkeypatch.setenv("VIBE_MARKET_MORNING_ENABLED", "true")
    monkeypatch.setenv("VIBE_MARKET_MORNING_RUNTIME_ENABLED", "true")
    monkeypatch.setenv(
        "VIBE_MARKET_MORNING_RUNTIME_FACTORY",
        PRODUCTION_RUNTIME_FACTORY_PATH,
    )
    monkeypatch.setattr(market_morning_routes, "probe_database", _ready)
    try:
        response = client.get("/market-morning/_internal/deployment-preflight")
    finally:
        client.app.dependency_overrides.clear()

    assert response.status_code == 503
    assert response.json()["provider_bundle_factory_configured"] is False
    assert "provider_bundle_factory_missing" in response.json()["blocking_checks"]


def test_deployment_preflight_blocks_incomplete_builtin_oidc_with_stable_codes(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.api.market_morning_admin_auth import VerifiedMarketMorningOperator
    from src.market_morning.oidc_auth import (
        BUILTIN_ADMIN_AUTH_FACTORY,
        BUILTIN_PRODUCT_AUTH_FACTORY,
    )

    async def _operator() -> VerifiedMarketMorningOperator:
        return VerifiedMarketMorningOperator(
            actor_reference="test-preflight-operator",
            permissions=frozenset({"operations.read"}),
        )

    client.app.dependency_overrides[market_morning_routes.require_operations_reader] = _operator
    monkeypatch.setenv("VIBE_MARKET_MORNING_ENABLED", "true")
    monkeypatch.setenv(
        "VIBE_MARKET_MORNING_DATABASE_URL",
        "mysql+asyncmy://market:super-secret@mysql/market_morning",
    )
    monkeypatch.setenv(
        "VIBE_MARKET_MORNING_AUTH_FACTORY",
        BUILTIN_PRODUCT_AUTH_FACTORY,
    )
    monkeypatch.setenv(
        "VIBE_MARKET_MORNING_ADMIN_AUTH_FACTORY",
        BUILTIN_ADMIN_AUTH_FACTORY,
    )
    monkeypatch.setenv("VIBE_MARKET_MORNING_RUNTIME_ENABLED", "true")
    monkeypatch.setenv(
        "VIBE_MARKET_MORNING_RUNTIME_FACTORY",
        "deployment.runtime:build",
    )
    monkeypatch.setenv(
        "VIBE_MARKET_MORNING_EMAIL_WEBHOOK_FACTORY",
        "deployment.email:build",
    )
    monkeypatch.setenv(
        "VIBE_MARKET_MORNING_EMAIL_IDENTITY_FACTORY",
        "deployment.identity:build_email_identity",
    )
    monkeypatch.setenv(
        "VIBE_MARKET_MORNING_DELIVERY_LINK_BASE_URL",
        "https://morning.example.jp/market-morning",
    )
    monkeypatch.setenv(
        "VIBE_MARKET_MORNING_DELIVERY_LINK_SIGNING_SECRET",
        "deployment-only-signing-secret-32",
    )
    _configure_frontend_runtime(monkeypatch)

    async def _ready() -> tuple[bool, str]:
        return True, "ready"

    monkeypatch.setattr(market_morning_routes, "probe_database", _ready)
    try:
        response = client.get("/market-morning/_internal/deployment-preflight")
    finally:
        client.app.dependency_overrides.clear()

    assert response.status_code == 503
    assert response.json()["oidc_configuration_ready"] is False
    assert response.json()["blocking_checks"] == [
        "oidc_admin_role_permissions_missing",
        "oidc_audience_missing",
        "oidc_issuer_missing",
        "oidc_jwks_url_missing",
        "oidc_session_validator_factory_missing",
    ]
    assert "super-secret" not in response.text


def test_issuer_search_does_not_touch_database_when_feature_is_closed(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _must_not_run(*_args, **_kwargs):
        raise AssertionError("database search must not run while feature is disabled")

    monkeypatch.setattr(market_morning_routes, "search_issuer_catalog", _must_not_run)

    response = client.get("/market-morning/issuers/search", params={"q": "7203"})

    assert response.status_code == 503
    assert response.json()["detail"] == "Market Morning is disabled"


def test_issuer_search_returns_deterministic_contract(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VIBE_MARKET_MORNING_ENABLED", "true")

    async def _principal() -> MarketMorningPrincipal:
        return MarketMorningPrincipal(
            user_id="user-search",
            external_subject="oidc|search-user",
        )

    client.app.dependency_overrides[require_market_morning_principal] = _principal

    async def _search(query: str, *, limit: int):
        assert query == "９８７ａ"
        assert limit == 3
        return (
            IssuerSearchMatch(
                issuer_id="issuer-09",
                issuer_code="987A",
                legal_name_ja="株式会社瀬戸内半導体",
                market_segment="グロース",
                match_kind="code_exact",
            ),
        )

    monkeypatch.setattr(market_morning_routes, "search_issuer_catalog", _search)
    response = client.get(
        "/market-morning/issuers/search",
        params={"q": "９８７ａ", "limit": 3},
    )

    assert response.status_code == 200
    assert response.json()["items"][0]["issuer_code"] == "987A"
    assert response.json()["normalized_query"] == "987a"
    assert response.json()["no_match_guidance"] is None


def test_issuer_search_no_match_returns_guidance_without_guessing(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VIBE_MARKET_MORNING_ENABLED", "true")

    async def _principal() -> MarketMorningPrincipal:
        return MarketMorningPrincipal(
            user_id="user-search",
            external_subject="oidc|search-user",
        )

    client.app.dependency_overrides[require_market_morning_principal] = _principal

    async def _empty(_query: str, *, limit: int):
        assert limit == 10
        return ()

    monkeypatch.setattr(market_morning_routes, "search_issuer_catalog", _empty)
    response = client.get(
        "/market-morning/issuers/search",
        params={"q": "存在しない会社"},
    )

    assert response.status_code == 200
    assert response.json()["items"] == []
    assert "正式な会社名" in response.json()["no_match_guidance"]


def test_issuer_search_authentication_fails_closed_before_database_access(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VIBE_MARKET_MORNING_ENABLED", "true")
    calls = 0

    async def _must_not_search(_query: str, *, limit: int):
        nonlocal calls
        calls += 1
        return ()

    monkeypatch.setattr(
        market_morning_routes,
        "search_issuer_catalog",
        _must_not_search,
    )

    response = client.get(
        "/market-morning/issuers/search",
        params={"q": "7203"},
    )

    assert response.status_code == 503
    assert response.json()["detail"] == ("Market Morning product authentication is not configured")
    assert calls == 0


def test_watchlist_authentication_fails_closed_before_database_access(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VIBE_MARKET_MORNING_ENABLED", "true")

    async def _must_not_run(**_kwargs):
        raise AssertionError("watchlist database must not run without product auth")

    monkeypatch.setattr(market_morning_routes, "get_watchlist", _must_not_run)
    response = client.get("/market-morning/watchlist")

    assert response.status_code == 503
    assert response.json()["detail"] == ("Market Morning product authentication is not configured")


def test_settings_authentication_fails_closed_before_database_access(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VIBE_MARKET_MORNING_ENABLED", "true")

    async def _must_not_run(**_kwargs):
        raise AssertionError("settings database must not run without product auth")

    monkeypatch.setattr(market_morning_routes, "get_settings", _must_not_run)
    response = client.get("/market-morning/settings")

    assert response.status_code == 503
    assert response.json()["detail"] == ("Market Morning product authentication is not configured")


def test_authenticated_watchlist_contract_is_user_scoped(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VIBE_MARKET_MORNING_ENABLED", "true")
    user_id = "11111111-1111-4111-8111-111111111111"

    async def _principal() -> MarketMorningPrincipal:
        return MarketMorningPrincipal(user_id=user_id, external_subject="test-subject")

    async def _get_watchlist(*, user_id: str):
        assert user_id == "11111111-1111-4111-8111-111111111111"
        return (
            WatchlistItemView(
                watchlist_item_id="33333333-3333-4333-8333-333333333333",
                issuer_id="22222222-2222-4222-8222-222222222222",
                issuer_code="987A",
                legal_name_ja="株式会社瀬戸内半導体",
                market_segment="グロース",
                issuer_status="active",
                user_label="決算フォロー",
                sort_order=0,
                created_at=datetime(2026, 7, 20, 23, 0, 0),
            ),
        )

    client.app.dependency_overrides[require_market_morning_principal] = _principal
    monkeypatch.setattr(market_morning_routes, "get_watchlist", _get_watchlist)
    response = client.get("/market-morning/watchlist")

    assert response.status_code == 200
    assert response.json()["active_count"] == 1
    assert response.json()["limit"] == 10
    assert response.json()["items"][0]["issuer_code"] == "987A"


def test_watchlist_limit_maps_to_conflict(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VIBE_MARKET_MORNING_ENABLED", "true")

    async def _principal() -> MarketMorningPrincipal:
        return MarketMorningPrincipal(
            user_id="11111111-1111-4111-8111-111111111111",
            external_subject="test-subject",
        )

    async def _limit(**_kwargs):
        raise WatchlistLimitReached()

    client.app.dependency_overrides[require_market_morning_principal] = _principal
    monkeypatch.setattr(market_morning_routes, "add_to_watchlist", _limit)
    response = client.post(
        "/market-morning/watchlist",
        json={"issuer_id": "22222222-2222-4222-8222-222222222222"},
    )

    assert response.status_code == 409
    assert "limited to 10" in response.json()["detail"]


def test_authenticated_settings_contract_is_user_scoped(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VIBE_MARKET_MORNING_ENABLED", "true")
    user_id = "11111111-1111-4111-8111-111111111111"

    async def _principal() -> MarketMorningPrincipal:
        return MarketMorningPrincipal(user_id=user_id, external_subject="test-subject")

    async def _settings(*, user_id: str):
        assert user_id == "11111111-1111-4111-8111-111111111111"
        return UserSettingsView(
            user_id=user_id,
            timezone="Asia/Tokyo",
            email_opt_in=True,
            risk_disclosure=ConsentStateView(
                consent_type=ConsentType.RISK_DISCLOSURE,
                accepted=True,
                consent_version="2026-07-01",
                accepted_at=datetime(2026, 7, 20, 23, 0, 0),
                revoked_at=None,
            ),
            data_disclosure=ConsentStateView(
                consent_type=ConsentType.DATA_DISCLOSURE,
                accepted=False,
                consent_version=None,
                accepted_at=None,
                revoked_at=None,
            ),
        )

    client.app.dependency_overrides[require_market_morning_principal] = _principal
    monkeypatch.setattr(market_morning_routes, "get_settings", _settings)
    response = client.get("/market-morning/settings")

    assert response.status_code == 200
    assert response.json()["timezone"] == "Asia/Tokyo"
    assert response.json()["email_opt_in"] is True
    assert response.json()["risk_disclosure"]["accepted"] is True
    assert response.json()["data_disclosure"]["accepted"] is False


def test_settings_patch_validation_maps_to_unprocessable_entity(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VIBE_MARKET_MORNING_ENABLED", "true")

    async def _principal() -> MarketMorningPrincipal:
        return MarketMorningPrincipal(
            user_id="11111111-1111-4111-8111-111111111111",
            external_subject="test-subject",
        )

    async def _invalid(**_kwargs):
        raise SettingsValidationError("at least one setting must be supplied")

    client.app.dependency_overrides[require_market_morning_principal] = _principal
    monkeypatch.setattr(market_morning_routes, "update_settings", _invalid)
    response = client.patch("/market-morning/settings", json={})

    assert response.status_code == 422
    assert "at least one" in response.json()["detail"]


def test_consent_and_deletion_mutation_contracts(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VIBE_MARKET_MORNING_ENABLED", "true")
    user_id = "11111111-1111-4111-8111-111111111111"

    async def _principal() -> MarketMorningPrincipal:
        return MarketMorningPrincipal(user_id=user_id, external_subject="test-subject")

    async def _consent(**kwargs):
        assert kwargs["user_id"] == user_id
        assert kwargs["consent_type"] == ConsentType.RISK_DISCLOSURE
        return ConsentMutationResult(
            status=ConsentMutationStatus.ACCEPTED,
            consent=ConsentStateView(
                consent_type=ConsentType.RISK_DISCLOSURE,
                accepted=True,
                consent_version="2026-07-20",
                accepted_at=datetime(2026, 7, 20, 23, 0, 0),
                revoked_at=None,
            ),
        )

    async def _delete(**kwargs):
        assert kwargs["user_id"] == user_id
        return DeletionRequestResult(
            status=DeletionRequestStatus.REQUESTED,
            request_id="44444444-4444-4444-8444-444444444444",
            request_status="pending",
            requested_at=datetime(2026, 7, 20, 23, 5, 0),
        )

    client.app.dependency_overrides[require_market_morning_principal] = _principal
    client.app.dependency_overrides[require_account_deletion_principal] = _principal
    monkeypatch.setattr(market_morning_routes, "record_consent", _consent)
    monkeypatch.setattr(market_morning_routes, "request_account_deletion", _delete)

    consent_response = client.post(
        "/market-morning/consents",
        json={
            "consent_type": "risk_disclosure",
            "consent_version": "2026-07-20",
            "accepted": True,
        },
    )
    deletion_response = client.post("/market-morning/account-deletion-requests")

    assert consent_response.status_code == 200
    assert consent_response.json()["status"] == "accepted"
    assert deletion_response.status_code == 200
    assert deletion_response.json()["request_status"] == "pending"


def test_internal_source_health_fails_closed_before_database_access(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _must_not_run(**_kwargs):
        raise AssertionError("source health database query must not run while disabled")

    monkeypatch.setattr(market_morning_routes, "list_source_health", _must_not_run)

    response = client.get("/market-morning/_internal/sources/health")

    assert response.status_code == 503
    assert response.json()["detail"] == "Market Morning is disabled"


def test_internal_source_health_returns_cursor_safe_contract(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VIBE_MARKET_MORNING_ENABLED", "true")

    async def _health(*, provider: str | None, limit: int):
        assert provider == "fixture_tdnet"
        assert limit == 20
        return (
            SourceHealthView(
                provider="fixture_tdnet",
                status="healthy",
                cursor={"offset": 12},
                last_successful_discovery_at=datetime(2026, 7, 20, 23, 5, tzinfo=timezone.utc),
                last_error_at=None,
                last_error_code=None,
            ),
        )

    monkeypatch.setattr(market_morning_routes, "list_source_health", _health)
    response = client.get(
        "/market-morning/_internal/sources/health",
        params={"provider": "fixture_tdnet", "limit": 20},
    )

    assert response.status_code == 200
    assert response.json()["count"] == 1
    assert response.json()["items"][0]["status"] == "healthy"
    assert response.json()["items"][0]["cursor"] == {"offset": 12}


def test_internal_event_audit_routes_return_source_revision_links(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VIBE_MARKET_MORNING_ENABLED", "true")
    view = EventAuditView(
        event_id="event-v2",
        event_family_key="family-7203",
        event_version=2,
        issuer_id="issuer-7203",
        issuer_code="7203",
        legal_name_ja="トヨタ自動車株式会社",
        title="（訂正）2026年3月期 決算短信",
        event_type="earnings_release",
        occurred_at=datetime(2026, 7, 20, 7, 0, tzinfo=timezone.utc),
        event_lifecycle_status="corrected",
        supersedes_event_id="event-v1",
        source_record_id="source-v2",
        source_provider="fixture_tdnet",
        provider_document_id="TD-7203-001",
        provider_revision_key="v2",
        original_url="https://example.invalid/tdnet/TD-7203-001-v2.pdf",
        source_published_at=datetime(2026, 7, 20, 7, 0, tzinfo=timezone.utc),
        source_fetched_at=datetime(2026, 7, 20, 7, 5, tzinfo=timezone.utc),
        source_lifecycle_status="corrected",
        relation_type="correction",
    )

    async def _history(*, event_family_key: str, limit: int):
        assert event_family_key == "family-7203"
        assert limit == 25
        return (view,)

    async def _affected(*, provider: str, document_id: str, limit: int):
        assert provider == "fixture_tdnet"
        assert document_id == "TD-7203-001"
        assert limit == 50
        return (view,)

    monkeypatch.setattr(market_morning_routes, "get_event_history", _history)
    monkeypatch.setattr(market_morning_routes, "get_affected_events", _affected)

    history_response = client.get(
        "/market-morning/_internal/events/family-7203/history",
        params={"limit": 25},
    )
    affected_response = client.get(
        "/market-morning/_internal/sources/fixture_tdnet/TD-7203-001/affected-events",
        params={"limit": 50},
    )

    assert history_response.status_code == 200
    assert history_response.json()["items"][0]["event_version"] == 2
    assert affected_response.status_code == 200
    item = affected_response.json()["items"][0]
    assert item["provider_revision_key"] == "v2"
    assert item["relation_type"] == "correction"
    assert "normalized_payload" not in item


def test_internal_source_queries_hide_database_failures(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sqlalchemy.exc import SQLAlchemyError

    monkeypatch.setenv("VIBE_MARKET_MORNING_ENABLED", "true")

    async def _failed(**_kwargs):
        raise SQLAlchemyError("mysql://market:secret@db/market_morning")

    monkeypatch.setattr(market_morning_routes, "list_source_health", _failed)
    response = client.get("/market-morning/_internal/sources/health")

    assert response.status_code == 503
    assert response.json()["detail"] == "Source audit data is temporarily unavailable"
    assert "secret" not in response.text


def test_today_edition_authentication_fails_before_service_access(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VIBE_MARKET_MORNING_ENABLED", "true")

    async def _must_not_run(**_kwargs):
        raise AssertionError("edition service must not run without product auth")

    monkeypatch.setattr(market_morning_routes, "get_today_edition", _must_not_run)
    response = client.get("/market-morning/edition/today")

    assert response.status_code == 503
    assert response.json()["detail"] == ("Market Morning product authentication is not configured")


def test_today_edition_contract_is_user_scoped_and_explicitly_synthetic(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VIBE_MARKET_MORNING_ENABLED", "true")
    user_id = "11111111-1111-4111-8111-111111111111"

    async def _principal() -> MarketMorningPrincipal:
        return MarketMorningPrincipal(user_id=user_id, external_subject="test-subject")

    async def _edition(*, user_id: str):
        assert user_id == "11111111-1111-4111-8111-111111111111"
        return build_today_edition(
            user_id=user_id,
            now=datetime(2026, 7, 20, 22, 0, tzinfo=timezone.utc),
            synthetic_enabled=True,
        )

    client.app.dependency_overrides[require_market_morning_principal] = _principal
    monkeypatch.setattr(market_morning_routes, "get_today_edition", _edition)
    response = client.get("/market-morning/edition/today")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "partial"
    assert body["data_mode"] == "synthetic_fixture"
    assert body["edition_id"] is None
    assert body["fixture_version"] == "synthetic-v1"
    assert body["edition"]["edition_date"] == "2026-07-21"
    assert body["edition"]["issuers"][0]["facts"][0]["citations"]
    assert body["edition"]["issuers"][1]["status"] == "unavailable"
    assert "user_id" not in response.text
    assert "user_label" not in response.text


def test_today_edition_returns_honest_not_published_contract_when_fixture_is_off(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VIBE_MARKET_MORNING_ENABLED", "true")
    user_id = "11111111-1111-4111-8111-111111111111"

    async def _principal() -> MarketMorningPrincipal:
        return MarketMorningPrincipal(user_id=user_id, external_subject="test-subject")

    async def _edition(*, user_id: str):
        return build_today_edition(
            user_id=user_id,
            now=datetime(2026, 7, 20, 22, 0, tzinfo=timezone.utc),
            synthetic_enabled=False,
        )

    client.app.dependency_overrides[require_market_morning_principal] = _principal
    monkeypatch.setattr(market_morning_routes, "get_today_edition", _edition)
    response = client.get("/market-morning/edition/today")

    assert response.status_code == 200
    assert response.json() == {
        "status": "not_published",
        "data_mode": "unavailable",
        "edition_id": None,
        "reason_code": "edition_repository_not_configured",
        "fixture_version": None,
        "edition": None,
    }


def test_today_edition_response_contract_accepts_persisted_read_models() -> None:
    result = build_today_edition(
        user_id="11111111-1111-4111-8111-111111111111",
        now=datetime(2026, 7, 20, 22, 0, tzinfo=timezone.utc),
        synthetic_enabled=True,
    )
    result = result.__class__(
        status=result.status,
        data_mode="persisted",
        edition=result.edition,
    )

    response = market_morning_routes.TodayEditionResponse.model_validate(result)

    assert response.data_mode == "persisted"
    assert response.edition is not None


def test_today_edition_database_failure_is_a_neutral_retryable_response(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VIBE_MARKET_MORNING_ENABLED", "true")

    async def _principal() -> MarketMorningPrincipal:
        return MarketMorningPrincipal(
            user_id="11111111-1111-4111-8111-111111111111",
            external_subject="test-subject",
        )

    async def _failed(**_kwargs):
        raise SQLAlchemyError("mysql+asyncmy://market:super-secret@db/market_morning")

    client.app.dependency_overrides[require_market_morning_principal] = _principal
    monkeypatch.setattr(market_morning_routes, "get_today_edition", _failed)

    response = client.get("/market-morning/edition/today")

    assert response.status_code == 503
    assert response.json()["detail"] == "Today's edition is temporarily unavailable"
    assert "super-secret" not in response.text


def test_edition_event_state_route_uses_authenticated_user(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VIBE_MARKET_MORNING_ENABLED", "true")
    user_id = "11111111-1111-4111-8111-111111111111"
    edition_id = "22222222-2222-4222-8222-222222222222"
    event_id = "33333333-3333-4333-8333-333333333333"

    async def _principal() -> MarketMorningPrincipal:
        return MarketMorningPrincipal(user_id=user_id, external_subject="subject")

    async def _update(**kwargs):
        assert kwargs == {
            "user_id": user_id,
            "edition_id": edition_id,
            "event_id": event_id,
            "state": "later",
        }
        return SimpleNamespace(
            edition_id=edition_id,
            event_id=event_id,
            state="later",
            first_read_at=None,
            updated_at=datetime(2026, 7, 21, 0, 30, tzinfo=timezone.utc),
        )

    client.app.dependency_overrides[require_market_morning_principal] = _principal
    monkeypatch.setattr(
        market_morning_routes,
        "update_edition_event_state",
        _update,
        raising=False,
    )
    response = client.patch(
        f"/market-morning/editions/{edition_id}/events/{event_id}/state",
        json={"state": "later"},
    )

    assert response.status_code == 200
    assert response.json()["state"] == "later"
    assert "user_id" not in response.text


def test_edition_event_states_route_uses_authenticated_user(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VIBE_MARKET_MORNING_ENABLED", "true")
    user_id = "11111111-1111-4111-8111-111111111111"
    edition_id = "22222222-2222-4222-8222-222222222222"
    event_id = "33333333-3333-4333-8333-333333333333"

    async def _principal() -> MarketMorningPrincipal:
        return MarketMorningPrincipal(user_id=user_id, external_subject="subject")

    async def _list(**kwargs):
        assert kwargs == {"user_id": user_id, "edition_id": edition_id}
        return (
            SimpleNamespace(
                edition_id=edition_id,
                event_id=event_id,
                state="read",
                first_read_at=datetime(2026, 7, 21, 0, 30, tzinfo=timezone.utc),
                updated_at=datetime(2026, 7, 21, 0, 30, tzinfo=timezone.utc),
            ),
        )

    client.app.dependency_overrides[require_market_morning_principal] = _principal
    monkeypatch.setattr(
        market_morning_routes,
        "get_edition_event_states",
        _list,
        raising=False,
    )
    response = client.get(f"/market-morning/editions/{edition_id}/event-states")

    assert response.status_code == 200
    assert response.json()["items"][0]["event_id"] == event_id
    assert response.json()["items"][0]["state"] == "read"
    assert "user_id" not in response.text


def test_edition_source_open_route_records_before_returning_verified_url(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VIBE_MARKET_MORNING_ENABLED", "true")
    user_id = "11111111-1111-4111-8111-111111111111"
    edition_id = "22222222-2222-4222-8222-222222222222"
    event_id = "33333333-3333-4333-8333-333333333333"
    request_id = "77777777-7777-4777-8777-777777777777"

    async def _principal() -> MarketMorningPrincipal:
        return MarketMorningPrincipal(user_id=user_id, external_subject="subject")

    async def _open(**kwargs):
        assert kwargs["user_id"] == user_id
        assert kwargs["edition_id"] == edition_id
        assert kwargs["event_id"] == event_id
        assert kwargs["request_id"] == request_id
        return SimpleNamespace(
            status="recorded",
            source_open_id="66666666-6666-4666-8666-666666666666",
            original_url="https://example.jp/tdnet/source.pdf",
        )

    client.app.dependency_overrides[require_market_morning_principal] = _principal
    monkeypatch.setattr(
        market_morning_routes,
        "open_edition_source",
        _open,
        raising=False,
    )
    response = client.post(
        f"/market-morning/editions/{edition_id}/sources/open",
        json={
            "event_id": event_id,
            "provider": "tdnet",
            "document_id": "TD-7203-001",
            "revision_key": "v1",
            "request_id": request_id,
        },
    )

    assert response.status_code == 200
    assert response.json()["status"] == "recorded"
    assert response.json()["original_url"].startswith("https://")


def test_issuer_research_route_uses_authenticated_user(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VIBE_MARKET_MORNING_ENABLED", "true")
    user_id = "11111111-1111-4111-8111-111111111111"
    issuer_id = "22222222-2222-4222-8222-222222222222"

    async def _principal() -> MarketMorningPrincipal:
        return MarketMorningPrincipal(user_id=user_id, external_subject="subject")

    async def _research(**kwargs):
        assert kwargs == {"user_id": user_id, "issuer_id": issuer_id, "limit": 50}
        citation = SimpleNamespace(
            provider="tdnet",
            document_id="TD-7203-001",
            revision_key="v2",
            original_url="https://example.jp/tdnet/source.pdf",
            published_at=datetime(2026, 7, 21, 0, 30, tzinfo=timezone.utc),
        )
        return SimpleNamespace(
            issuer_id=issuer_id,
            issuer_code="7203",
            legal_name_ja="トヨタ自動車株式会社",
            market_segment="プライム",
            note=None,
            events=(
                SimpleNamespace(
                    event_id="33333333-3333-4333-8333-333333333333",
                    event_family_key="tdnet:TD-7203-001",
                    event_version=2,
                    title="（訂正）業績予想の修正",
                    event_type="guidance_revision",
                    occurred_at=datetime(2026, 7, 21, 0, 30, tzinfo=timezone.utc),
                    lifecycle_status="corrected",
                    supersedes_event_id="33333333-3333-4333-8333-222222222222",
                    facts=(
                        SimpleNamespace(
                            text="通期売上高予想を修正した。",
                            citations=(citation,),
                        ),
                    ),
                    citations=(citation,),
                    warnings=(),
                ),
            ),
        )

    client.app.dependency_overrides[require_market_morning_principal] = _principal
    monkeypatch.setattr(
        market_morning_routes,
        "get_issuer_research",
        _research,
        raising=False,
    )
    response = client.get(f"/market-morning/issuers/{issuer_id}/research")

    assert response.status_code == 200
    body = response.json()
    assert body["issuer_code"] == "7203"
    assert body["events"][0]["lifecycle_status"] == "corrected"
    assert body["events"][0]["facts"][0]["citations"][0]["provider"] == "tdnet"
    assert "user_id" not in response.text


def test_issuer_note_routes_are_private_and_content_is_not_logged_in_response(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VIBE_MARKET_MORNING_ENABLED", "true")
    user_id = "11111111-1111-4111-8111-111111111111"
    issuer_id = "22222222-2222-4222-8222-222222222222"
    note_text = "次回の開示で海外販売台数を確認する。"

    async def _principal() -> MarketMorningPrincipal:
        return MarketMorningPrincipal(user_id=user_id, external_subject="subject")

    async def _update(**kwargs):
        assert kwargs == {
            "user_id": user_id,
            "issuer_id": issuer_id,
            "text": note_text,
        }
        return SimpleNamespace(
            note_id="55555555-5555-4555-8555-555555555555",
            text=note_text,
            created_at=datetime(2026, 7, 21, 0, 30, tzinfo=timezone.utc),
            updated_at=datetime(2026, 7, 21, 0, 30, tzinfo=timezone.utc),
        )

    async def _delete(**kwargs):
        assert kwargs == {"user_id": user_id, "issuer_id": issuer_id}
        return True

    client.app.dependency_overrides[require_market_morning_principal] = _principal
    monkeypatch.setattr(
        market_morning_routes,
        "update_issuer_research_note",
        _update,
        raising=False,
    )
    monkeypatch.setattr(
        market_morning_routes,
        "delete_issuer_research_note",
        _delete,
        raising=False,
    )

    update = client.put(
        f"/market-morning/issuers/{issuer_id}/note",
        json={"text": note_text},
    )
    deleted = client.delete(f"/market-morning/issuers/{issuer_id}/note")

    assert update.status_code == 200
    assert update.json()["note"]["text"] == note_text
    assert "user_id" not in update.text
    assert deleted.status_code == 200
    assert deleted.json() == {"status": "cleared"}
