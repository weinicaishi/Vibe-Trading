from __future__ import annotations

import base64
from datetime import date, datetime, timezone
import json
from pathlib import Path

import httpx
import pytest


RELEASE = "a" * 40
API_AUDIENCE = "https://market-morning-staging.invalid/api"
ISSUER_ID = "22222222-2222-4222-8222-222222222222"
PRODUCT_SUBJECT = "auth0|product-probe-user"
OPERATOR_SUBJECT = "auth0|operator-probe-user"
PRODUCT_CLIENT = "product-client-public-reference"
OPERATOR_CLIENT = "operator-client-public-reference"


def _segment(payload: dict[str, object]) -> str:
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _token(
    *,
    subject: str,
    client_id: str,
    issued_at: int,
    nonce: str,
) -> str:
    return ".".join(
        (
            _segment({"alg": "RS256", "typ": "JWT"}),
            _segment(
                {
                    "iss": "https://tenant.example.auth0.com/",
                    "sub": subject,
                    "aud": [API_AUDIENCE, "https://tenant.example.auth0.com/userinfo"],
                    "azp": client_id,
                    "iat": issued_at,
                    "exp": issued_at + 900,
                    "nonce": nonce,
                }
            ),
            _segment({"signature": nonce}),
        )
    )


def _release_candidate_payload() -> dict[str, object]:
    from src.market_morning.release_candidate import (
        REQUIRED_SOURCE_ARTIFACTS,
        REQUIRED_VERIFICATION_EVIDENCE,
        RepositorySnapshot,
        VerificationSnapshot,
        evaluate_release_candidate,
    )

    source_hashes = {name: "1" * 64 for name in REQUIRED_SOURCE_ARTIFACTS}
    evidence_hashes = {name: "2" * 64 for name in REQUIRED_VERIFICATION_EVIDENCE}
    return evaluate_release_candidate(
        environment_tier="ci",
        expected_release_revision=RELEASE,
        repository=RepositorySnapshot(
            release_revision=RELEASE,
            repository_readable=True,
            worktree_clean=True,
            source_artifact_sha256=source_hashes,
            tracked_source_artifacts=frozenset(REQUIRED_SOURCE_ARTIFACTS),
        ),
        verification=VerificationSnapshot(
            evidence_sha256=evidence_hashes,
            valid_evidence=frozenset(REQUIRED_VERIFICATION_EVIDENCE),
        ),
        generated_at=datetime(2026, 7, 23, tzinfo=timezone.utc),
    ).to_dict()


def _write_owner_only(path: Path, value: str) -> None:
    path.write_text(value + "\n", encoding="utf-8")
    path.chmod(0o600)


def test_real_oidc_probe_emits_complete_hash_only_evidence() -> None:
    from src.market_morning.oidc_staging_evidence import (
        parse_oidc_staging_evidence,
    )
    from src.market_morning.oidc_staging_probe import run_oidc_staging_probe

    issued_at = 1_785_283_200
    product_a = _token(
        subject=PRODUCT_SUBJECT,
        client_id=PRODUCT_CLIENT,
        issued_at=issued_at,
        nonce="product-a",
    )
    product_b = _token(
        subject=PRODUCT_SUBJECT,
        client_id=PRODUCT_CLIENT,
        issued_at=issued_at + 1,
        nonce="product-b",
    )
    operator = _token(
        subject=OPERATOR_SUBJECT,
        client_id=OPERATOR_CLIENT,
        issued_at=issued_at,
        nonce="operator-a",
    )
    revoked: set[str] = set()

    def handler(request: httpx.Request) -> httpx.Response:
        bearer = request.headers["Authorization"].removeprefix("Bearer ")
        if bearer in revoked:
            return httpx.Response(401, json={"detail": "authentication failed"})
        if request.method == "GET" and request.url.path.endswith("/_internal/operations/summary"):
            return httpx.Response(200, json={"status": "ready"})
        if request.method == "POST" and request.url.path.endswith("/_internal/private-beta/invites"):
            return httpx.Response(
                200,
                json={
                    "status": "created",
                    "invite_id": "33333333-3333-4333-8333-333333333333",
                    "raw_token": "invite-token-that-must-never-leave-memory-1234567890",
                    "expires_at": "2026-07-24T06:00:00Z",
                },
            )
        if request.method == "POST" and request.url.path.endswith("/private-beta/invitations/accept"):
            assert json.loads(request.content)["token"].startswith("invite-token-")
            return httpx.Response(
                200,
                json={
                    "status": "accepted",
                    "invite_id": "33333333-3333-4333-8333-333333333333",
                    "user_id": "11111111-1111-4111-8111-111111111111",
                    "expires_at": "2026-07-24T06:00:00Z",
                },
            )
        if request.method == "POST" and request.url.path.endswith("/watchlist"):
            assert json.loads(request.content)["issuer_id"] == ISSUER_ID
            return httpx.Response(
                200,
                json={"status": "added", "active_count": 1, "limit": 10},
            )
        if request.method == "GET" and request.url.path.endswith("/watchlist"):
            return httpx.Response(
                200,
                json={
                    "items": [{"issuer_id": ISSUER_ID}],
                    "active_count": 1,
                    "limit": 10,
                },
            )
        if request.method == "DELETE" and request.url.path.endswith("/auth/session"):
            revoked.add(bearer)
            return httpx.Response(204)
        raise AssertionError(f"unexpected request: {request.method} {request.url.path}")

    with httpx.Client(
        base_url="https://staging.market-morning.invalid",
        transport=httpx.MockTransport(handler),
    ) as client:
        payload = run_oidc_staging_probe(
            client=client,
            release_revision=RELEASE,
            run_id="staging-2026-07-23",
            edition_date=date(2026, 7, 23),
            api_audience=API_AUDIENCE,
            issuer_id=ISSUER_ID,
            product_access_token=product_a,
            product_secondary_access_token=product_b,
            operator_access_token=operator,
            clock=lambda: datetime(2026, 7, 23, 6, 30, tzinfo=timezone.utc),
        )

    evidence = parse_oidc_staging_evidence(payload)
    serialized = json.dumps(payload, sort_keys=True)
    assert evidence.status == "passed"
    assert evidence.counts_as_staging_oidc_evidence is True
    assert set(evidence.checks.values()) == {"passed"}
    assert PRODUCT_SUBJECT not in serialized
    assert OPERATOR_SUBJECT not in serialized
    assert product_a not in serialized
    assert product_b not in serialized
    assert operator not in serialized
    assert "invite-token-" not in serialized


def test_oidc_probe_emits_auditable_failure_without_copying_response_body() -> None:
    from src.market_morning.oidc_staging_evidence import (
        parse_oidc_staging_evidence,
    )
    from src.market_morning.oidc_staging_probe import run_oidc_staging_probe

    issued_at = 1_785_283_200
    product_a = _token(
        subject=PRODUCT_SUBJECT,
        client_id=PRODUCT_CLIENT,
        issued_at=issued_at,
        nonce="product-a",
    )
    product_b = _token(
        subject=PRODUCT_SUBJECT,
        client_id=PRODUCT_CLIENT,
        issued_at=issued_at + 1,
        nonce="product-b",
    )
    operator = _token(
        subject=OPERATOR_SUBJECT,
        client_id=OPERATOR_CLIENT,
        issued_at=issued_at,
        nonce="operator-a",
    )
    private_response_value = "must-not-be-copied-into-oidc-evidence"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/_internal/operations/summary"):
            return httpx.Response(200, json={"status": "ready"})
        if request.url.path.endswith("/_internal/private-beta/invites"):
            return httpx.Response(403, json={"detail": private_response_value})
        raise AssertionError(f"unexpected request: {request.method} {request.url.path}")

    with httpx.Client(
        base_url="https://staging.market-morning.invalid",
        transport=httpx.MockTransport(handler),
    ) as client:
        payload = run_oidc_staging_probe(
            client=client,
            release_revision=RELEASE,
            run_id="staging-2026-07-23",
            edition_date=date(2026, 7, 23),
            api_audience=API_AUDIENCE,
            issuer_id=ISSUER_ID,
            product_access_token=product_a,
            product_secondary_access_token=product_b,
            operator_access_token=operator,
            clock=lambda: datetime(2026, 7, 23, 6, 30, tzinfo=timezone.utc),
        )

    evidence = parse_oidc_staging_evidence(payload)
    serialized = json.dumps(payload, sort_keys=True)
    assert evidence.status == "failed"
    assert evidence.counts_as_staging_oidc_evidence is False
    assert evidence.failure_codes == ("operator_role_authorization_failed",)
    assert evidence.checks["operator_login"] == "passed"
    assert evidence.checks["operator_role_authorized"] == "failed"
    assert private_response_value not in serialized
    assert product_a not in serialized
    assert operator not in serialized


def test_oidc_probe_token_file_must_not_be_group_or_world_readable(
    tmp_path: Path,
) -> None:
    from src.market_morning.oidc_staging_probe_cli import (
        OidcStagingProbeCliError,
        read_oidc_token_file,
    )

    token_path = tmp_path / "product.token"
    token_path.write_text("header.payload.signature\n", encoding="utf-8")
    token_path.chmod(0o644)

    with pytest.raises(
        OidcStagingProbeCliError,
        match="oidc_probe_token_file_permissions_invalid",
    ):
        read_oidc_token_file(token_path)


def test_oidc_probe_token_file_must_not_have_hard_links(tmp_path: Path) -> None:
    from src.market_morning.oidc_staging_probe_cli import (
        OidcStagingProbeCliError,
        read_oidc_token_file,
    )

    token_path = tmp_path / "product.token"
    _write_owner_only(token_path, "header.payload.signature")
    linked_path = tmp_path / "linked-product.token"
    linked_path.hardlink_to(token_path)

    with pytest.raises(
        OidcStagingProbeCliError,
        match="oidc_probe_token_file_links_invalid",
    ):
        read_oidc_token_file(token_path)


def test_oidc_probe_cli_does_not_resolve_token_symlink_before_safety_check(
    tmp_path: Path,
) -> None:
    from src.market_morning.oidc_staging_probe_cli import run_cli

    issued_at = 1_785_283_200
    candidate = tmp_path / "candidate.json"
    candidate.write_text(
        json.dumps(_release_candidate_payload()),
        encoding="utf-8",
    )
    product_target = tmp_path / "product-target.token"
    product_link = tmp_path / "product-link.token"
    product_secondary = tmp_path / "product-secondary.token"
    operator = tmp_path / "operator.token"
    _write_owner_only(
        product_target,
        _token(
            subject=PRODUCT_SUBJECT,
            client_id=PRODUCT_CLIENT,
            issued_at=issued_at,
            nonce="product-a",
        ),
    )
    product_link.symlink_to(product_target)
    _write_owner_only(
        product_secondary,
        _token(
            subject=PRODUCT_SUBJECT,
            client_id=PRODUCT_CLIENT,
            issued_at=issued_at + 1,
            nonce="product-b",
        ),
    )
    _write_owner_only(
        operator,
        _token(
            subject=OPERATOR_SUBJECT,
            client_id=OPERATOR_CLIENT,
            issued_at=issued_at,
            nonce="operator-a",
        ),
    )

    with pytest.raises(SystemExit) as error:
        run_cli(
            [
                "--release-candidate",
                str(candidate),
                "--api-base-url",
                "https://staging.market-morning.invalid",
                "--api-audience",
                API_AUDIENCE,
                "--run-id",
                "staging-2026-07-23",
                "--edition-date",
                "2026-07-23",
                "--issuer-id",
                ISSUER_ID,
                "--product-token-file",
                str(product_link),
                "--product-secondary-token-file",
                str(product_secondary),
                "--operator-token-file",
                str(operator),
                "--output",
                str(tmp_path / "oidc-evidence.json"),
                "--confirm-staging-side-effects",
            ],
            client_factory=lambda _base_url: pytest.fail("unsafe token path reached the network"),
        )

    assert error.value.code == 2


def test_oidc_probe_cli_requires_explicit_staging_side_effect_confirmation(
    tmp_path: Path,
) -> None:
    from src.market_morning.oidc_staging_probe_cli import run_cli

    output = tmp_path / "oidc-evidence.json"
    with pytest.raises(SystemExit) as error:
        run_cli(
            [
                "--release-candidate",
                str(tmp_path / "candidate.json"),
                "--api-base-url",
                "https://staging.market-morning.invalid",
                "--api-audience",
                API_AUDIENCE,
                "--run-id",
                "staging-2026-07-23",
                "--edition-date",
                "2026-07-23",
                "--issuer-id",
                ISSUER_ID,
                "--product-token-file",
                str(tmp_path / "product-a.token"),
                "--product-secondary-token-file",
                str(tmp_path / "product-b.token"),
                "--operator-token-file",
                str(tmp_path / "operator.token"),
                "--output",
                str(output),
            ]
        )

    assert error.value.code == 2
    assert not output.exists()


def test_oidc_probe_cli_rejects_invalid_run_id_before_network_side_effects(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from src.market_morning.oidc_staging_probe_cli import run_cli

    candidate = tmp_path / "candidate.json"
    candidate.write_text(
        json.dumps(_release_candidate_payload()),
        encoding="utf-8",
    )

    with pytest.raises(SystemExit) as error:
        run_cli(
            [
                "--release-candidate",
                str(candidate),
                "--api-base-url",
                "https://staging.market-morning.invalid",
                "--api-audience",
                API_AUDIENCE,
                "--run-id",
                "invalid run id",
                "--edition-date",
                "2026-07-23",
                "--issuer-id",
                ISSUER_ID,
                "--product-token-file",
                str(tmp_path / "product-a.token"),
                "--product-secondary-token-file",
                str(tmp_path / "product-b.token"),
                "--operator-token-file",
                str(tmp_path / "operator.token"),
                "--output",
                str(tmp_path / "oidc-evidence.json"),
                "--confirm-staging-side-effects",
            ],
            client_factory=lambda _base_url: pytest.fail("invalid run ID reached the network"),
        )

    assert error.value.code == 2
    assert "oidc_probe_run_id_invalid" in capsys.readouterr().err


def test_oidc_probe_cli_runs_real_flow_from_owner_only_token_files(
    tmp_path: Path,
) -> None:
    from src.market_morning.oidc_staging_evidence import (
        parse_oidc_staging_evidence,
    )
    from src.market_morning.oidc_staging_probe_cli import run_cli

    issued_at = 1_785_283_200
    product_a = _token(
        subject=PRODUCT_SUBJECT,
        client_id=PRODUCT_CLIENT,
        issued_at=issued_at,
        nonce="product-a",
    )
    product_b = _token(
        subject=PRODUCT_SUBJECT,
        client_id=PRODUCT_CLIENT,
        issued_at=issued_at + 1,
        nonce="product-b",
    )
    operator = _token(
        subject=OPERATOR_SUBJECT,
        client_id=OPERATOR_CLIENT,
        issued_at=issued_at,
        nonce="operator-a",
    )
    candidate = tmp_path / "candidate.json"
    candidate.write_text(
        json.dumps(_release_candidate_payload()),
        encoding="utf-8",
    )
    product_a_path = tmp_path / "product-a.token"
    product_b_path = tmp_path / "product-b.token"
    operator_path = tmp_path / "operator.token"
    _write_owner_only(product_a_path, product_a)
    _write_owner_only(product_b_path, product_b)
    _write_owner_only(operator_path, operator)
    revoked: set[str] = set()

    def handler(request: httpx.Request) -> httpx.Response:
        bearer = request.headers["Authorization"].removeprefix("Bearer ")
        if bearer in revoked:
            return httpx.Response(401, json={"detail": "authentication failed"})
        path = request.url.path
        if request.method == "GET" and path.endswith("/_internal/operations/summary"):
            return httpx.Response(200, json={"status": "ready"})
        if request.method == "POST" and path.endswith("/_internal/private-beta/invites"):
            return httpx.Response(
                200,
                json={
                    "status": "created",
                    "invite_id": "33333333-3333-4333-8333-333333333333",
                    "raw_token": "invite-token-that-must-never-leave-memory-1234567890",
                    "expires_at": "2026-07-24T06:00:00Z",
                },
            )
        if request.method == "POST" and path.endswith("/private-beta/invitations/accept"):
            return httpx.Response(
                200,
                json={
                    "status": "accepted",
                    "invite_id": "33333333-3333-4333-8333-333333333333",
                    "user_id": "11111111-1111-4111-8111-111111111111",
                    "expires_at": "2026-07-24T06:00:00Z",
                },
            )
        if request.method == "POST" and path.endswith("/watchlist"):
            return httpx.Response(
                200,
                json={"status": "added", "active_count": 1, "limit": 10},
            )
        if request.method == "GET" and path.endswith("/watchlist"):
            return httpx.Response(
                200,
                json={
                    "items": [{"issuer_id": ISSUER_ID}],
                    "active_count": 1,
                    "limit": 10,
                },
            )
        if request.method == "DELETE" and path.endswith("/auth/session"):
            revoked.add(bearer)
            return httpx.Response(204)
        raise AssertionError(f"unexpected request: {request.method} {path}")

    def client_factory(base_url: str) -> httpx.Client:
        return httpx.Client(
            base_url=base_url,
            transport=httpx.MockTransport(handler),
        )

    output = tmp_path / "oidc-evidence.json"
    exit_code = run_cli(
        [
            "--release-candidate",
            str(candidate),
            "--api-base-url",
            "https://staging.market-morning.invalid",
            "--api-audience",
            API_AUDIENCE,
            "--run-id",
            "staging-2026-07-23",
            "--edition-date",
            "2026-07-23",
            "--issuer-id",
            ISSUER_ID,
            "--product-token-file",
            str(product_a_path),
            "--product-secondary-token-file",
            str(product_b_path),
            "--operator-token-file",
            str(operator_path),
            "--output",
            str(output),
            "--confirm-staging-side-effects",
        ],
        client_factory=client_factory,
        clock=lambda: datetime(2026, 7, 23, 6, 30, tzinfo=timezone.utc),
    )

    raw = output.read_text(encoding="utf-8")
    evidence = parse_oidc_staging_evidence(json.loads(raw))
    assert exit_code == 0
    assert evidence.status == "passed"
    assert evidence.release_revision == RELEASE
    assert product_a not in raw
    assert product_b not in raw
    assert operator not in raw
    assert "invite-token-" not in raw
    assert str(tmp_path) not in raw


def test_oidc_probe_cli_rejects_loopback_as_counting_staging_target(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from src.market_morning.oidc_staging_probe_cli import run_cli

    candidate = tmp_path / "candidate.json"
    candidate.write_text(
        json.dumps(_release_candidate_payload()),
        encoding="utf-8",
    )
    output = tmp_path / "oidc-evidence.json"

    with pytest.raises(SystemExit) as error:
        run_cli(
            [
                "--release-candidate",
                str(candidate),
                "--api-base-url",
                "https://127.0.0.1:8898",
                "--api-audience",
                API_AUDIENCE,
                "--run-id",
                "staging-2026-07-23",
                "--edition-date",
                "2026-07-23",
                "--issuer-id",
                ISSUER_ID,
                "--product-token-file",
                str(tmp_path / "product-a.token"),
                "--product-secondary-token-file",
                str(tmp_path / "product-b.token"),
                "--operator-token-file",
                str(tmp_path / "operator.token"),
                "--output",
                str(output),
                "--confirm-staging-side-effects",
            ]
        )

    assert error.value.code == 2
    assert "oidc_probe_api_base_url_not_staging" in capsys.readouterr().err
    assert not output.exists()


def test_oidc_probe_rejects_wrong_jst_edition_before_any_side_effect() -> None:
    from src.market_morning.oidc_staging_probe import (
        OidcStagingProbeError,
        run_oidc_staging_probe,
    )

    issued_at = 1_785_283_200
    product_a = _token(
        subject=PRODUCT_SUBJECT,
        client_id=PRODUCT_CLIENT,
        issued_at=issued_at,
        nonce="product-a",
    )
    product_b = _token(
        subject=PRODUCT_SUBJECT,
        client_id=PRODUCT_CLIENT,
        issued_at=issued_at + 1,
        nonce="product-b",
    )
    operator = _token(
        subject=OPERATOR_SUBJECT,
        client_id=OPERATOR_CLIENT,
        issued_at=issued_at,
        nonce="operator-a",
    )

    def must_not_call(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("wrong-edition probe must not call staging")

    with httpx.Client(
        base_url="https://staging.market-morning.invalid",
        transport=httpx.MockTransport(must_not_call),
    ) as client:
        with pytest.raises(
            OidcStagingProbeError,
            match="oidc_probe_edition_date_mismatch",
        ):
            run_oidc_staging_probe(
                client=client,
                release_revision=RELEASE,
                run_id="staging-2026-07-22",
                edition_date=date(2026, 7, 22),
                api_audience=API_AUDIENCE,
                issuer_id=ISSUER_ID,
                product_access_token=product_a,
                product_secondary_access_token=product_b,
                operator_access_token=operator,
                clock=lambda: datetime(
                    2026,
                    7,
                    23,
                    6,
                    30,
                    tzinfo=timezone.utc,
                ),
            )
