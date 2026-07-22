from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

import pytest

from src.market_morning.release_candidate import (
    REQUIRED_SOURCE_ARTIFACTS,
    REQUIRED_VERIFICATION_EVIDENCE,
    RepositorySnapshot,
    VerificationSnapshot,
    evaluate_release_candidate,
)
from src.market_morning.staging_evidence import (
    REQUIRED_STAGING_GATES,
    StagingEvidenceError,
    parse_staging_day_evidence,
)


RELEASE = "a" * 40
SHA = "1" * 64
PROVIDERS = (
    "licensed-tdnet",
    "licensed-edinet",
    "licensed-market-data",
    "production-email",
    "production-model",
)


def _release_candidate():
    return evaluate_release_candidate(
        environment_tier="ci",
        expected_release_revision=RELEASE,
        repository=RepositorySnapshot(
            release_revision=RELEASE,
            repository_readable=True,
            worktree_clean=True,
            source_artifact_sha256={name: SHA for name in REQUIRED_SOURCE_ARTIFACTS},
            tracked_source_artifacts=frozenset(REQUIRED_SOURCE_ARTIFACTS),
        ),
        verification=VerificationSnapshot(
            evidence_sha256={name: SHA for name in REQUIRED_VERIFICATION_EVIDENCE},
            valid_evidence=frozenset(REQUIRED_VERIFICATION_EVIDENCE),
        ),
        generated_at=datetime(2026, 7, 22, 0, 0, tzinfo=timezone.utc),
    )


def _artifact_payload(
    gate: str,
    *,
    status: str = "passed",
    candidate_sha256: str | None = None,
    run_id: str = "staging-2026-07-22",
) -> dict[str, object]:
    ordered = sorted(REQUIRED_STAGING_GATES)
    provider_ids = [PROVIDERS[ordered.index(gate)]] if ordered.index(gate) < 5 else []
    return {
        "schema_version": 1,
        "scope": "market_morning_staging_gate_artifact",
        "environment_tier": "staging",
        "gate": gate,
        "run_id": run_id,
        "release_candidate_sha256": candidate_sha256 or _release_candidate().manifest_sha256,
        "release_revision": RELEASE,
        "runtime_schema_revision": "0018_market_morning_auth_sessions",
        "edition_date": "2026-07-22",
        "previous_jpx_open_date": "2026-07-21",
        "next_jpx_open_date": "2026-07-23",
        "calendar_provider": "licensed-jpx-calendar",
        "provider_ids": provider_ids,
        "started_at": "2026-07-22T06:20:00+09:00",
        "finished_at": "2026-07-22T07:05:00+09:00",
        "published_at": ("2026-07-22T06:58:00+09:00" if gate == "publication" and status == "passed" else None),
        "status": status,
        "contains_fixture_data": False,
        "contains_synthetic_data": False,
        "model_invocation_count": (3 if gate == "model_usage_cost" and status == "passed" else 0),
        "failure_code": None if status == "passed" else "probe_failed",
        "evidence_sha256": "3" * 64,
    }


def _artifacts(**overrides):
    from src.market_morning.staging_day_evidence import parse_staging_gate_artifact

    payloads = {gate: _artifact_payload(gate) for gate in REQUIRED_STAGING_GATES}
    payloads.update(overrides)
    return {gate: parse_staging_gate_artifact(payload) for gate, payload in payloads.items()}


def test_builder_emits_one_strict_passed_staging_day() -> None:
    from src.market_morning.staging_day_evidence import build_staging_day_evidence

    payload = build_staging_day_evidence(
        release_candidate=_release_candidate(),
        artifacts=_artifacts(),
    )
    parsed = parse_staging_day_evidence(payload)

    assert parsed.status == "passed"
    assert parsed.counts_as_staging_day is True
    assert parsed.release_revision == RELEASE
    assert parsed.model_invocation_count == 3
    assert payload["provider_ids"] == sorted(PROVIDERS)
    assert set(payload["artifact_sha256"]) == REQUIRED_STAGING_GATES
    assert len(set(payload["artifact_sha256"].values())) == len(REQUIRED_STAGING_GATES)


def test_builder_emits_auditable_failed_day_that_never_counts() -> None:
    from src.market_morning.staging_day_evidence import build_staging_day_evidence

    failed = _artifact_payload("publication", status="failed")
    payload = build_staging_day_evidence(
        release_candidate=_release_candidate(),
        artifacts=_artifacts(publication=failed),
    )
    parsed = parse_staging_day_evidence(payload)

    assert parsed.status == "failed"
    assert parsed.counts_as_staging_day is False
    assert parsed.failure_codes == ("publication_probe_failed",)
    assert payload["gates"]["publication"] == "failed"


def test_builder_rejects_missing_gate_and_mismatched_run_identity() -> None:
    from src.market_morning.staging_day_evidence import build_staging_day_evidence

    missing = dict(_artifacts())
    missing.pop("observability")
    with pytest.raises(StagingEvidenceError, match="every required"):
        build_staging_day_evidence(
            release_candidate=_release_candidate(),
            artifacts=missing,
        )

    changed = _artifact_payload("observability", run_id="other-run")
    with pytest.raises(StagingEvidenceError, match="one run identity"):
        build_staging_day_evidence(
            release_candidate=_release_candidate(),
            artifacts=_artifacts(observability=changed),
        )


def test_builder_rejects_release_candidate_drift() -> None:
    from src.market_morning.staging_day_evidence import build_staging_day_evidence

    changed = _artifact_payload("database_readiness", candidate_sha256="2" * 64)

    with pytest.raises(StagingEvidenceError, match="one run identity|supplied release"):
        build_staging_day_evidence(
            release_candidate=_release_candidate(),
            artifacts=_artifacts(database_readiness=changed),
        )


@pytest.mark.parametrize(
    ("gate", "field", "value", "message"),
    (
        ("licensed_sources", "contains_fixture_data", True, "fixture"),
        ("licensed_sources", "contains_synthetic_data", True, "synthetic"),
        ("licensed_sources", "provider_ids", ["fixture_tdnet"], "production"),
        ("publication", "published_at", None, "published_at"),
        ("database_readiness", "published_at", "2026-07-22T07:00:00+09:00", "only publication"),
        ("model_usage_cost", "model_invocation_count", 0, "invocation"),
        ("database_readiness", "model_invocation_count", 1, "only model_usage_cost"),
    ),
)
def test_gate_artifact_contract_rejects_unsafe_or_drifted_values(
    gate: str,
    field: str,
    value: object,
    message: str,
) -> None:
    from src.market_morning.staging_day_evidence import parse_staging_gate_artifact

    payload = _artifact_payload(gate)
    payload[field] = value

    with pytest.raises(StagingEvidenceError, match=message):
        parse_staging_gate_artifact(payload)


def test_gate_artifact_contract_rejects_extra_fields() -> None:
    from src.market_morning.staging_day_evidence import parse_staging_gate_artifact

    payload = _artifact_payload("database_readiness")
    payload["database_url"] = "mysql+asyncmy://secret"

    with pytest.raises(StagingEvidenceError, match="unexpected"):
        parse_staging_gate_artifact(payload)


def test_gate_artifact_builder_derives_release_identity_and_validates_payload() -> None:
    from src.market_morning.staging_day_evidence import (
        build_staging_gate_artifact_payload,
        parse_staging_gate_artifact,
    )

    candidate = _release_candidate()
    payload = build_staging_gate_artifact_payload(
        release_candidate=candidate,
        gate="database_readiness",
        run_id="staging-2026-07-22",
        edition_date="2026-07-22",
        previous_jpx_open_date="2026-07-21",
        next_jpx_open_date="2026-07-23",
        calendar_provider="licensed-jpx-calendar",
        provider_ids=("production-mysql",),
        started_at="2026-07-22T06:20:00+09:00",
        finished_at="2026-07-22T06:21:00+09:00",
        published_at=None,
        status="passed",
        model_invocation_count=0,
        failure_code=None,
        evidence_sha256="4" * 64,
    )
    artifact = parse_staging_gate_artifact(payload)

    assert artifact.release_revision == RELEASE
    assert artifact.release_candidate_sha256 == candidate.manifest_sha256
    assert artifact.evidence_sha256 == "4" * 64


def _write_cli_inputs(tmp_path: Path) -> tuple[Path, list[str]]:
    candidate_path = tmp_path / "release-candidate.json"
    candidate_path.write_text(
        json.dumps(_release_candidate().to_dict()),
        encoding="utf-8",
    )
    arguments: list[str] = []
    for gate in sorted(REQUIRED_STAGING_GATES):
        path = tmp_path / f"{gate}.json"
        path.write_text(json.dumps(_artifact_payload(gate)), encoding="utf-8")
        arguments.extend(("--gate", f"{gate}={path}"))
    return candidate_path, arguments


def test_staging_day_cli_writes_strict_manifest_without_input_paths(tmp_path: Path) -> None:
    from src.market_morning.staging_day_evidence_cli import run_cli

    candidate_path, gate_arguments = _write_cli_inputs(tmp_path)
    destination = tmp_path / "staging-day.json"

    assert (
        run_cli(
            [
                "--release-candidate",
                str(candidate_path),
                *gate_arguments,
                "--output",
                str(destination),
            ]
        )
        == 0
    )
    raw = destination.read_text(encoding="utf-8")
    payload = json.loads(raw)
    assert parse_staging_day_evidence(payload).status == "passed"
    assert str(tmp_path) not in raw
    assert "source_artifact_sha256" not in raw
    assert "verification_evidence_sha256" not in raw


def test_staging_day_cli_rejects_duplicate_gate_paths(tmp_path: Path) -> None:
    from src.market_morning.staging_day_evidence_cli import run_cli

    candidate_path, gate_arguments = _write_cli_inputs(tmp_path)
    shared = tmp_path / "shared.json"
    shared.write_text(json.dumps(_artifact_payload("database_readiness")), encoding="utf-8")
    arguments = [
        "--release-candidate",
        str(candidate_path),
        "--gate",
        f"database_readiness={shared}",
        "--gate",
        f"deployment_preflight={shared}",
        "--output",
        str(tmp_path / "out.json"),
    ]
    with pytest.raises(SystemExit) as caught:
        run_cli(arguments)
    assert caught.value.code == 2


def test_gate_artifact_cli_hashes_raw_evidence_without_copying_it(tmp_path: Path) -> None:
    from src.market_morning.staging_day_evidence import parse_staging_gate_artifact
    from src.market_morning.staging_gate_artifact_cli import run_cli

    candidate = tmp_path / "release-candidate.json"
    candidate.write_text(json.dumps(_release_candidate().to_dict()), encoding="utf-8")
    raw_evidence = tmp_path / "publication-probe.log"
    raw_evidence.write_text("private probe output must remain outside envelope", encoding="utf-8")
    output = tmp_path / "publication-artifact.json"

    exit_code = run_cli(
        [
            "--release-candidate",
            str(candidate),
            "--gate",
            "publication",
            "--run-id",
            "staging-2026-07-22",
            "--edition-date",
            "2026-07-22",
            "--previous-jpx-open-date",
            "2026-07-21",
            "--next-jpx-open-date",
            "2026-07-23",
            "--calendar-provider",
            "licensed-jpx-calendar",
            "--provider-id",
            "market-morning-runtime",
            "--started-at",
            "2026-07-22T06:55:00+09:00",
            "--finished-at",
            "2026-07-22T07:01:00+09:00",
            "--published-at",
            "2026-07-22T06:58:00+09:00",
            "--status",
            "passed",
            "--evidence",
            str(raw_evidence),
            "--output",
            str(output),
        ]
    )

    raw = output.read_text(encoding="utf-8")
    artifact = parse_staging_gate_artifact(json.loads(raw))
    assert exit_code == 0
    assert artifact.status == "passed"
    assert len(artifact.evidence_sha256) == 64
    assert str(tmp_path) not in raw
    assert "private probe output" not in raw


def test_gate_artifact_cli_writes_failed_envelope_and_returns_one(tmp_path: Path) -> None:
    from src.market_morning.staging_gate_artifact_cli import run_cli

    candidate = tmp_path / "release-candidate.json"
    candidate.write_text(json.dumps(_release_candidate().to_dict()), encoding="utf-8")
    raw_evidence = tmp_path / "database-probe.log"
    raw_evidence.write_text("failed", encoding="utf-8")
    output = tmp_path / "database-artifact.json"

    exit_code = run_cli(
        [
            "--release-candidate",
            str(candidate),
            "--gate",
            "database_readiness",
            "--run-id",
            "staging-2026-07-22",
            "--edition-date",
            "2026-07-22",
            "--previous-jpx-open-date",
            "2026-07-21",
            "--next-jpx-open-date",
            "2026-07-23",
            "--calendar-provider",
            "licensed-jpx-calendar",
            "--started-at",
            "2026-07-22T06:20:00+09:00",
            "--finished-at",
            "2026-07-22T06:21:00+09:00",
            "--status",
            "failed",
            "--failure-code",
            "schema_outdated",
            "--evidence",
            str(raw_evidence),
            "--output",
            str(output),
        ]
    )

    assert exit_code == 1
    assert json.loads(output.read_text(encoding="utf-8"))["failure_code"] == "schema_outdated"


def test_pyproject_exposes_staging_day_builder_entrypoint() -> None:
    project = Path("pyproject.toml").read_text(encoding="utf-8")

    assert ('vibe-trading-market-morning-staging-day = "src.market_morning.staging_day_evidence_cli:main"') in project
    assert (
        'vibe-trading-market-morning-staging-gate-artifact = "src.market_morning.staging_gate_artifact_cli:main"'
    ) in project
