from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

import pytest

from src.market_morning.db import EXPECTED_MARKET_MORNING_SCHEMA_REVISION
from src.market_morning.release_candidate import (
    REQUIRED_SOURCE_ARTIFACTS,
    REQUIRED_VERIFICATION_EVIDENCE,
    ReleaseCandidateError,
    RepositorySnapshot,
    VerificationSnapshot,
    collect_verification_snapshot,
    evaluate_release_candidate,
    parse_release_candidate_manifest,
)


RELEASE = "a" * 40
SHA = "1" * 64
GENERATED_AT = datetime(2026, 7, 22, 9, 0, tzinfo=timezone.utc)


def _repository(**overrides) -> RepositorySnapshot:
    values = {
        "release_revision": RELEASE,
        "repository_readable": True,
        "worktree_clean": True,
        "source_artifact_sha256": {
            name: SHA for name in REQUIRED_SOURCE_ARTIFACTS
        },
        "tracked_source_artifacts": frozenset(REQUIRED_SOURCE_ARTIFACTS),
    }
    values.update(overrides)
    return RepositorySnapshot(**values)


def _verification(**overrides) -> VerificationSnapshot:
    values = {
        "evidence_sha256": {
            name: SHA for name in REQUIRED_VERIFICATION_EVIDENCE
        },
        "valid_evidence": frozenset(REQUIRED_VERIFICATION_EVIDENCE),
    }
    values.update(overrides)
    return VerificationSnapshot(**values)


def _evaluate(**overrides):
    values = {
        "environment_tier": "ci",
        "expected_release_revision": RELEASE,
        "repository": _repository(),
        "verification": _verification(),
        "generated_at": GENERATED_AT,
    }
    values.update(overrides)
    return evaluate_release_candidate(**values)


def test_release_candidate_passes_and_is_hash_self_verifiable() -> None:
    manifest = _evaluate().to_dict()

    assert manifest["status"] == "passed"
    assert manifest["blocking_codes"] == []
    assert manifest["release_revision"] == RELEASE
    assert manifest["runtime_schema_revision"] == EXPECTED_MARKET_MORNING_SCHEMA_REVISION
    assert manifest["counts_as_staging_day"] is False
    assert manifest["counts_as_t1_release_evidence"] is False
    assert set(manifest["source_artifact_sha256"]) == set(REQUIRED_SOURCE_ARTIFACTS)
    assert set(manifest["verification_evidence_sha256"]) == set(
        REQUIRED_VERIFICATION_EVIDENCE
    )
    expected_hash = manifest.pop("manifest_sha256")
    canonical = json.dumps(
        manifest,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    assert expected_hash == hashlib.sha256(canonical).hexdigest()


def test_release_candidate_parser_accepts_only_untampered_manifest() -> None:
    payload = _evaluate().to_dict()

    parsed = parse_release_candidate_manifest(payload)

    assert parsed.status == "passed"
    assert parsed.release_revision == RELEASE
    assert parsed.manifest_sha256 == payload["manifest_sha256"]


@pytest.mark.parametrize(
    "mutate",
    (
        lambda value: value.update(release_revision="b" * 40),
        lambda value: value["checks"].update(clean_worktree="failed"),
        lambda value: value["source_artifact_sha256"].pop("python_project_contract"),
        lambda value: value.update(counts_as_staging_day=True),
        lambda value: value.update(secret="not-allowed"),
    ),
)
def test_release_candidate_parser_rejects_tampering(mutate) -> None:
    payload = _evaluate().to_dict()
    mutate(payload)

    with pytest.raises(ReleaseCandidateError):
        parse_release_candidate_manifest(payload)


@pytest.mark.parametrize(
    ("repository", "blocking_code"),
    (
        (_repository(worktree_clean=False), "worktree_not_clean"),
        (_repository(release_revision="b" * 40), "release_revision_mismatch"),
        (_repository(release_revision=None), "release_revision_unavailable"),
        (_repository(repository_readable=False), "repository_unreadable"),
        (
            _repository(tracked_source_artifacts=frozenset()),
            "source_artifacts_not_tracked",
        ),
        (
            _repository(
                source_artifact_sha256={
                    name: None for name in REQUIRED_SOURCE_ARTIFACTS
                }
            ),
            "source_artifacts_unreadable",
        ),
    ),
)
def test_release_candidate_fails_closed_for_repository_state(
    repository: RepositorySnapshot,
    blocking_code: str,
) -> None:
    manifest = _evaluate(repository=repository).to_dict()

    assert manifest["status"] == "blocked"
    assert blocking_code in manifest["blocking_codes"]


def test_release_candidate_fails_closed_for_missing_or_invalid_evidence() -> None:
    verification = VerificationSnapshot(
        evidence_sha256={name: None for name in REQUIRED_VERIFICATION_EVIDENCE},
        valid_evidence=frozenset(),
    )

    manifest = _evaluate(verification=verification).to_dict()

    assert manifest["status"] == "blocked"
    assert "verification_evidence_missing" in manifest["blocking_codes"]
    assert "verification_evidence_invalid" in manifest["blocking_codes"]


def test_release_candidate_rejects_stale_runtime_schema() -> None:
    manifest = evaluate_release_candidate(
        environment_tier="staging",
        expected_release_revision=RELEASE,
        repository=_repository(),
        verification=_verification(),
        generated_at=GENERATED_AT,
        runtime_schema_revision="0016_market_morning_accounts",
    ).to_dict()

    assert manifest["status"] == "blocked"
    assert "runtime_schema_not_current" in manifest["blocking_codes"]


def test_manifest_does_not_expose_paths_status_text_or_credentials() -> None:
    encoded = json.dumps(_evaluate().to_dict(), ensure_ascii=False)

    for forbidden in (
        "/Users/example/private",
        "mysql+asyncmy://",
        "password",
        "secret",
        "database.example.jp",
        "agent/src/market_morning",
    ):
        assert forbidden not in encoded


def _write_evidence(path: Path, value: object) -> None:
    if isinstance(value, str):
        path.write_text(value, encoding="utf-8")
    else:
        path.write_text(json.dumps(value), encoding="utf-8")


def test_verification_snapshot_hashes_outputs_and_validates_mysql_scope(
    tmp_path: Path,
) -> None:
    paths = {name: tmp_path / f"{name}.txt" for name in REQUIRED_VERIFICATION_EVIDENCE}
    _write_evidence(paths["backend_tests"], "6241 passed")
    _write_evidence(paths["frontend_build"], "built")
    _write_evidence(paths["frontend_tests"], "341 passed")
    _write_evidence(
        paths["mysql_acceptance"],
        {
            "schema_version": 1,
            "scope": "real_mysql_acceptance",
            "environment": "ci",
            "status": "passed",
            "counts_as_staging_day": False,
        },
    )
    _write_evidence(
        paths["mysql_migration"],
        {
            "schema_version": 1,
            "scope": "real_mysql_migration_rehearsal",
            "environment": "ci",
            "status": "passed",
            "counts_as_staging_day": False,
        },
    )

    snapshot = collect_verification_snapshot(paths, environment_tier="ci")

    assert snapshot.valid_evidence == frozenset(REQUIRED_VERIFICATION_EVIDENCE)
    assert all(len(value or "") == 64 for value in snapshot.evidence_sha256.values())


def test_verification_snapshot_marks_failed_mysql_evidence_invalid(tmp_path: Path) -> None:
    paths = {name: tmp_path / f"{name}.txt" for name in REQUIRED_VERIFICATION_EVIDENCE}
    for name, path in paths.items():
        _write_evidence(path, f"evidence:{name}")
    _write_evidence(
        paths["mysql_acceptance"],
        {
            "schema_version": 1,
            "scope": "real_mysql_acceptance",
            "environment": "ci",
            "status": "failed",
            "counts_as_staging_day": False,
        },
    )

    snapshot = collect_verification_snapshot(paths, environment_tier="ci")

    assert "mysql_acceptance" not in snapshot.valid_evidence
    assert len(snapshot.evidence_sha256["mysql_acceptance"] or "") == 64


def test_verification_snapshot_requires_exact_distinct_inputs(tmp_path: Path) -> None:
    with pytest.raises(ReleaseCandidateError):
        collect_verification_snapshot({}, environment_tier="ci")

    shared = tmp_path / "shared.txt"
    shared.write_text("evidence", encoding="utf-8")
    with pytest.raises(ReleaseCandidateError):
        collect_verification_snapshot(
            {name: shared for name in REQUIRED_VERIFICATION_EVIDENCE},
            environment_tier="ci",
        )


def test_required_release_source_artifacts_exist() -> None:
    root = Path(__file__).resolve().parents[2]

    missing = [path for path in REQUIRED_SOURCE_ARTIFACTS.values() if not (root / path).is_file()]
    assert missing == []


def test_required_release_source_artifacts_pin_auth0_deployment_contract() -> None:
    assert REQUIRED_SOURCE_ARTIFACTS["market_morning_auth0_post_login_action"] == (
        "deploy/auth0/market-morning-post-login.js"
    )
    assert REQUIRED_SOURCE_ARTIFACTS["market_morning_auth0_runbook"] == "deploy/auth0/README.md"


def test_release_candidate_console_script_is_packaged() -> None:
    project = Path("pyproject.toml").read_text(encoding="utf-8")

    assert (
        'vibe-trading-market-morning-release-candidate = '
        '"src.market_morning.release_candidate_cli:main"'
    ) in project


def test_cli_writes_blocked_manifest_outside_repository(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.market_morning import release_candidate_cli

    repository_root = tmp_path / "repo"
    repository_root.mkdir()
    output = tmp_path / "release-candidate.json"
    evidence_paths = {}
    for name in REQUIRED_VERIFICATION_EVIDENCE:
        path = tmp_path / f"{name}.txt"
        path.write_text(name, encoding="utf-8")
        evidence_paths[name] = path

    monkeypatch.setattr(
        release_candidate_cli,
        "collect_repository_snapshot",
        lambda _root: _repository(worktree_clean=False),
    )
    monkeypatch.setattr(
        release_candidate_cli,
        "collect_verification_snapshot",
        lambda _paths, environment_tier: _verification(),
    )
    argv = [
        "--repository-root",
        str(repository_root),
        "--environment",
        "ci",
        "--expected-release-revision",
        RELEASE,
        "--output",
        str(output),
    ]
    for name, path in evidence_paths.items():
        argv.extend(("--evidence", f"{name}={path}"))

    assert release_candidate_cli.run_cli(argv) == 1
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["status"] == "blocked"
    assert payload["blocking_codes"] == ["worktree_not_clean"]


def test_cli_refuses_to_dirty_repository_with_its_own_output(tmp_path: Path) -> None:
    from src.market_morning.release_candidate_cli import run_cli

    repository_root = tmp_path / "repo"
    repository_root.mkdir()
    with pytest.raises(SystemExit) as caught:
        run_cli(
            [
                "--repository-root",
                str(repository_root),
                "--environment",
                "ci",
                "--expected-release-revision",
                RELEASE,
                "--evidence",
                f"backend_tests={tmp_path / 'backend'}",
                "--output",
                str(repository_root / "manifest.json"),
            ]
        )
    assert caught.value.code == 2
