from __future__ import annotations

import json
from pathlib import Path

import pytest


CHECKS = {
    "operator_exact_token_logout",
    "operator_login",
    "operator_protected_api",
    "operator_revoked_token_rejected",
    "operator_role_authorized",
    "product_exact_token_logout",
    "product_login",
    "product_private_beta_onboarding",
    "product_revoked_token_rejected",
    "product_watchlist_cross_session",
}
TEMPLATE_PATH = Path(
    "docs/evidence/market-morning/oidc-staging.template.json"
)


def _payload(*, status: str = "passed") -> dict[str, object]:
    passed = status == "passed"
    checks = {name: "passed" for name in CHECKS}
    if not passed:
        checks["operator_login"] = "failed"
    return {
        "schema_version": 1,
        "scope": "market_morning_oidc_staging_probe",
        "environment_tier": "staging",
        "run_id": "staging-2026-07-23",
        "release_revision": "a" * 40,
        "edition_date": "2026-07-23",
        "started_at": "2026-07-23T06:20:00+09:00",
        "finished_at": "2026-07-23T06:35:00+09:00",
        "identity_provider": "auth0",
        "tenant_reference_sha256": "b" * 64,
        "product_client_reference_sha256": "c" * 64,
        "operator_client_reference_sha256": "d" * 64,
        "api_audience_reference_sha256": "e" * 64,
        "session_mode": "access_token_sha256",
        "configured_max_access_token_lifetime_seconds": 900,
        "observed_product_access_token_lifetime_seconds": 900 if passed else 0,
        "observed_operator_access_token_lifetime_seconds": 900 if passed else 0,
        "status": status,
        "counts_as_staging_oidc_evidence": passed,
        "contains_access_token": False,
        "contains_authorization_header": False,
        "contains_id_token": False,
        "contains_invite_token": False,
        "contains_oauth_code_or_verifier": False,
        "contains_personal_identity": False,
        "contains_refresh_token": False,
        "checks": checks,
        "failure_codes": [] if passed else ["oidc_e2e_not_run"],
        "artifact_sha256": {name: "f" * 64 for name in CHECKS},
    }


def test_oidc_staging_evidence_accepts_complete_real_flow() -> None:
    from src.market_morning.oidc_staging_evidence import (
        parse_oidc_staging_evidence,
    )

    evidence = parse_oidc_staging_evidence(_payload())

    assert evidence.status == "passed"
    assert evidence.counts_as_staging_oidc_evidence is True
    assert evidence.run_id == "staging-2026-07-23"
    assert evidence.edition_date.isoformat() == "2026-07-23"
    assert evidence.configured_max_access_token_lifetime_seconds == 900
    assert evidence.session_mode == "access_token_sha256"
    assert len(evidence.evidence_sha256) == 64


@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        (lambda value: value.update(environment_tier="local"), "environment_tier"),
        (
            lambda value: value.update(
                configured_max_access_token_lifetime_seconds=36000
            ),
            "900",
        ),
        (
            lambda value: value.update(
                configured_max_access_token_lifetime_seconds=900.0
            ),
            "900",
        ),
        (
            lambda value: value.update(
                observed_operator_access_token_lifetime_seconds=901
            ),
            "operator access token lifetime",
        ),
        (
            lambda value: value.update(
                observed_product_access_token_lifetime_seconds=0
            ),
            "product access token lifetime",
        ),
        (lambda value: value.update(session_mode="auth0_session_id"), "session_mode"),
        (
            lambda value: value["checks"].pop("operator_revoked_token_rejected"),
            "every required",
        ),
        (
            lambda value: value.update(contains_authorization_header=True),
            "sensitive authentication material",
        ),
        (
            lambda value: value.update(contains_refresh_token=True),
            "sensitive authentication material",
        ),
        (lambda value: value.update(user_email="private@example.com"), "unexpected"),
    ),
)
def test_oidc_staging_evidence_rejects_partial_unsafe_or_long_lived_flow(
    mutation,
    message: str,
) -> None:
    from src.market_morning.oidc_staging_evidence import (
        OidcStagingEvidenceError,
        parse_oidc_staging_evidence,
    )

    payload = _payload()
    mutation(payload)

    with pytest.raises(OidcStagingEvidenceError, match=message):
        parse_oidc_staging_evidence(payload)


def test_failed_oidc_staging_evidence_is_auditable_but_never_counts() -> None:
    from src.market_morning.oidc_staging_evidence import (
        parse_oidc_staging_evidence,
    )

    evidence = parse_oidc_staging_evidence(_payload(status="failed"))

    assert evidence.status == "failed"
    assert evidence.counts_as_staging_oidc_evidence is False
    assert evidence.failure_codes == ("oidc_e2e_not_run",)


def test_repository_oidc_template_is_valid_but_permanently_blocked() -> None:
    from src.market_morning.oidc_staging_evidence import (
        parse_oidc_staging_evidence,
    )

    evidence = parse_oidc_staging_evidence(
        json.loads(TEMPLATE_PATH.read_text(encoding="utf-8"))
    )

    assert evidence.status == "failed"
    assert evidence.counts_as_staging_oidc_evidence is False
    assert evidence.failure_codes == ("oidc_e2e_not_run",)
