"""Run the real Product and Operator OIDC staging exercise without persisting secrets."""

from __future__ import annotations

import base64
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
from typing import Any
import unicodedata
from zoneinfo import ZoneInfo

import httpx

from src.market_morning.oidc_staging_evidence import (
    REQUIRED_OIDC_STAGING_CHECKS,
    parse_oidc_staging_evidence,
)


_MAX_BEARER_LENGTH = 8192
_MAX_TOKEN_LIFETIME_SECONDS = 900
_JST = ZoneInfo("Asia/Tokyo")


class OidcStagingProbeError(RuntimeError):
    """Raised when the live OIDC exercise cannot prove one required check."""

    def __init__(self, error_code: str) -> None:
        self.error_code = error_code
        super().__init__(error_code)


@dataclass(frozen=True, slots=True)
class _TokenClaims:
    issuer: str
    subject: str
    audiences: frozenset[str]
    authorized_party: str
    lifetime_seconds: int


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _artifact_hash(check: str, **facts: object) -> str:
    payload = {"check": check, **facts}
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _decode_segment(value: str) -> Mapping[str, Any]:
    try:
        padding = "=" * (-len(value) % 4)
        decoded = base64.urlsafe_b64decode((value + padding).encode("ascii"))
        payload = json.loads(decoded)
    except Exception as error:
        raise OidcStagingProbeError("oidc_probe_token_invalid") from error
    if not isinstance(payload, Mapping):
        raise OidcStagingProbeError("oidc_probe_token_invalid")
    return payload


def _token_claims(token: str, *, expected_audience: str) -> _TokenClaims:
    if (
        not isinstance(token, str)
        or not token
        or len(token) > _MAX_BEARER_LENGTH
        or token != token.strip()
        or any(char.isspace() for char in token)
        or any(unicodedata.category(char).startswith("C") for char in token)
    ):
        raise OidcStagingProbeError("oidc_probe_token_invalid")
    segments = token.split(".")
    if len(segments) != 3 or any(not segment for segment in segments):
        raise OidcStagingProbeError("oidc_probe_token_invalid")
    payload = _decode_segment(segments[1])
    issuer = payload.get("iss")
    subject = payload.get("sub")
    authorized_party = payload.get("azp")
    issued_at = payload.get("iat")
    expires_at = payload.get("exp")
    raw_audience = payload.get("aud")
    if isinstance(raw_audience, str):
        audiences = frozenset({raw_audience})
    elif isinstance(raw_audience, list) and all(isinstance(value, str) and value for value in raw_audience):
        audiences = frozenset(raw_audience)
    else:
        raise OidcStagingProbeError("oidc_probe_token_claims_invalid")
    if (
        not isinstance(issuer, str)
        or not issuer.startswith("https://")
        or not issuer.endswith("/")
        or not isinstance(subject, str)
        or not subject
        or not isinstance(authorized_party, str)
        or not authorized_party
        or type(issued_at) is not int
        or type(expires_at) is not int
        or expected_audience not in audiences
    ):
        raise OidcStagingProbeError("oidc_probe_token_claims_invalid")
    lifetime = expires_at - issued_at
    if not 1 <= lifetime <= _MAX_TOKEN_LIFETIME_SECONDS:
        raise OidcStagingProbeError("oidc_probe_token_lifetime_invalid")
    return _TokenClaims(
        issuer=issuer,
        subject=subject,
        audiences=audiences,
        authorized_party=authorized_party,
        lifetime_seconds=lifetime,
    )


def _authorization(token: str) -> dict[str, str]:
    return {
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
    }


def _json_object(response: httpx.Response, *, error_code: str) -> dict[str, Any]:
    try:
        payload = response.json()
    except Exception as error:
        raise OidcStagingProbeError(error_code) from error
    if not isinstance(payload, dict):
        raise OidcStagingProbeError(error_code)
    return payload


def _require_status(
    response: httpx.Response,
    expected: int,
    *,
    error_code: str,
) -> None:
    if response.status_code != expected:
        raise OidcStagingProbeError(error_code)


def _exercise_oidc_routes(
    *,
    client: httpx.Client,
    issuer_id: str,
    product_access_token: str,
    product_secondary_access_token: str,
    operator_access_token: str,
    passed: Callable[..., None],
) -> None:
    operator_headers = _authorization(operator_access_token)
    product_headers = _authorization(product_access_token)
    secondary_headers = _authorization(product_secondary_access_token)

    response = client.get(
        "/market-morning/_internal/operations/summary",
        params={"hours": 24, "recent_run_limit": 1},
        headers=operator_headers,
    )
    _require_status(response, 200, error_code="operator_protected_api_failed")
    _json_object(response, error_code="operator_protected_api_failed")
    passed("operator_login", http_status=200)
    passed("operator_protected_api", http_status=200)

    response = client.post(
        "/market-morning/_internal/private-beta/invites",
        json={"expires_in_days": 1},
        headers=operator_headers,
    )
    _require_status(response, 200, error_code="operator_role_authorization_failed")
    invite_payload = _json_object(
        response,
        error_code="operator_role_authorization_failed",
    )
    invite_token = invite_payload.get("raw_token")
    if not isinstance(invite_token, str) or not 32 <= len(invite_token) <= 256:
        raise OidcStagingProbeError("operator_role_authorization_failed")
    passed("operator_role_authorized", http_status=200)

    response = client.post(
        "/market-morning/private-beta/invitations/accept",
        json={"token": invite_token},
        headers=product_headers,
    )
    _require_status(response, 200, error_code="product_private_beta_onboarding_failed")
    onboarding_payload = _json_object(
        response,
        error_code="product_private_beta_onboarding_failed",
    )
    if onboarding_payload.get("status") not in {"accepted", "already_accepted"}:
        raise OidcStagingProbeError("product_private_beta_onboarding_failed")
    passed("product_private_beta_onboarding", http_status=200)

    response = client.post(
        "/market-morning/watchlist",
        json={"issuer_id": issuer_id},
        headers=product_headers,
    )
    _require_status(response, 200, error_code="product_watchlist_write_failed")
    watchlist_mutation = _json_object(
        response,
        error_code="product_watchlist_write_failed",
    )
    if watchlist_mutation.get("status") not in {"added", "already_active"}:
        raise OidcStagingProbeError("product_watchlist_write_failed")

    response = client.get("/market-morning/watchlist", headers=secondary_headers)
    _require_status(response, 200, error_code="product_secondary_login_failed")
    _json_object(response, error_code="product_secondary_login_failed")
    passed("product_login", primary_http_status=200, secondary_http_status=200)

    response = client.delete(
        "/market-morning/_internal/auth/session",
        headers=operator_headers,
    )
    _require_status(response, 204, error_code="operator_exact_token_logout_failed")
    passed("operator_exact_token_logout", http_status=204)
    response = client.get(
        "/market-morning/_internal/operations/summary",
        params={"hours": 24, "recent_run_limit": 1},
        headers=operator_headers,
    )
    _require_status(
        response,
        401,
        error_code="operator_revoked_token_was_accepted",
    )
    passed("operator_revoked_token_rejected", http_status=401)

    response = client.delete(
        "/market-morning/auth/session",
        headers=product_headers,
    )
    _require_status(response, 204, error_code="product_exact_token_logout_failed")
    passed("product_exact_token_logout", http_status=204)
    response = client.get("/market-morning/watchlist", headers=product_headers)
    _require_status(
        response,
        401,
        error_code="product_revoked_token_was_accepted",
    )
    passed("product_revoked_token_rejected", http_status=401)

    response = client.get("/market-morning/watchlist", headers=secondary_headers)
    _require_status(response, 200, error_code="product_watchlist_cross_session_failed")
    watchlist = _json_object(
        response,
        error_code="product_watchlist_cross_session_failed",
    )
    items = watchlist.get("items")
    if not isinstance(items, list) or not any(
        isinstance(item, Mapping) and item.get("issuer_id") == issuer_id for item in items
    ):
        raise OidcStagingProbeError("product_watchlist_cross_session_failed")
    passed("product_watchlist_cross_session", http_status=200)


def run_oidc_staging_probe(
    *,
    client: httpx.Client,
    release_revision: str,
    run_id: str,
    edition_date: date,
    api_audience: str,
    issuer_id: str,
    product_access_token: str,
    product_secondary_access_token: str,
    operator_access_token: str,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> dict[str, Any]:
    """Exercise real staging routes and return only the strict hash-only evidence."""

    started_at = clock()
    if started_at.tzinfo is None or started_at.utcoffset() is None:
        raise OidcStagingProbeError("oidc_probe_clock_invalid")
    if started_at.astimezone(_JST).date() != edition_date:
        raise OidcStagingProbeError("oidc_probe_edition_date_mismatch")
    product_primary = _token_claims(
        product_access_token,
        expected_audience=api_audience,
    )
    product_secondary = _token_claims(
        product_secondary_access_token,
        expected_audience=api_audience,
    )
    operator = _token_claims(
        operator_access_token,
        expected_audience=api_audience,
    )
    if (
        product_access_token == product_secondary_access_token
        or product_primary.issuer != product_secondary.issuer
        or product_primary.subject != product_secondary.subject
        or product_primary.authorized_party != product_secondary.authorized_party
    ):
        raise OidcStagingProbeError("oidc_probe_product_sessions_invalid")
    if operator.issuer != product_primary.issuer:
        raise OidcStagingProbeError("oidc_probe_tenant_mismatch")
    if operator.authorized_party == product_primary.authorized_party:
        raise OidcStagingProbeError("oidc_probe_client_separation_invalid")

    checks = {name: "failed" for name in REQUIRED_OIDC_STAGING_CHECKS}
    artifacts = {name: _artifact_hash(name, status="failed", reason="not_run") for name in REQUIRED_OIDC_STAGING_CHECKS}

    def passed(name: str, **facts: object) -> None:
        checks[name] = "passed"
        artifacts[name] = _artifact_hash(name, status="passed", **facts)

    failure_codes: list[str] = []
    try:
        _exercise_oidc_routes(
            client=client,
            issuer_id=issuer_id,
            product_access_token=product_access_token,
            product_secondary_access_token=product_secondary_access_token,
            operator_access_token=operator_access_token,
            passed=passed,
        )
    except OidcStagingProbeError as error:
        failure_codes.append(error.error_code)
    except httpx.HTTPError:
        failure_codes.append("oidc_probe_request_failed")

    finished_at = clock()
    if finished_at <= started_at:
        finished_at = started_at + timedelta(microseconds=1)
    payload: dict[str, Any] = {
        "schema_version": 1,
        "scope": "market_morning_oidc_staging_probe",
        "environment_tier": "staging",
        "run_id": run_id,
        "release_revision": release_revision,
        "edition_date": edition_date.isoformat(),
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "identity_provider": "auth0",
        "tenant_reference_sha256": _sha256_text(product_primary.issuer),
        "product_client_reference_sha256": _sha256_text(product_primary.authorized_party),
        "operator_client_reference_sha256": _sha256_text(operator.authorized_party),
        "api_audience_reference_sha256": _sha256_text(api_audience),
        "session_mode": "access_token_sha256",
        "configured_max_access_token_lifetime_seconds": 900,
        "observed_product_access_token_lifetime_seconds": max(
            product_primary.lifetime_seconds,
            product_secondary.lifetime_seconds,
        ),
        "observed_operator_access_token_lifetime_seconds": operator.lifetime_seconds,
        "status": "failed" if failure_codes else "passed",
        "counts_as_staging_oidc_evidence": not failure_codes,
        "contains_access_token": False,
        "contains_authorization_header": False,
        "contains_id_token": False,
        "contains_invite_token": False,
        "contains_oauth_code_or_verifier": False,
        "contains_personal_identity": False,
        "contains_refresh_token": False,
        "checks": dict(sorted(checks.items())),
        "failure_codes": failure_codes,
        "artifact_sha256": dict(sorted(artifacts.items())),
    }
    parse_oidc_staging_evidence(payload)
    return payload


__all__ = [
    "OidcStagingProbeError",
    "run_oidc_staging_probe",
]
