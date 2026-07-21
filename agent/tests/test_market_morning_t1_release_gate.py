from __future__ import annotations

from datetime import date, timedelta
import json
from pathlib import Path

import pytest

from src.market_morning.db import EXPECTED_MARKET_MORNING_SCHEMA_REVISION
from src.market_morning.mysql_acceptance import MYSQL_ACCEPTANCE_SCENARIOS
from src.market_morning.mysql_migration_rehearsal import MYSQL_MIGRATION_SCENARIOS


RELEASE = "a" * 40
SHA_A = "1" * 64
SHA_B = "2" * 64


def _scenario_result(scenario_id: str) -> dict[str, object]:
    return {
        "scenario_id": scenario_id,
        "status": "passed",
        "exit_code": 0,
        "output_sha256": "3" * 64,
    }


def _mysql_acceptance() -> dict[str, object]:
    scenarios = [_scenario_result(item.scenario_id) for item in MYSQL_ACCEPTANCE_SCENARIOS]
    return {
        "schema_version": 1,
        "scope": "real_mysql_acceptance",
        "environment": "staging",
        "target_fingerprint": SHA_A,
        "status": "passed",
        "counts_as_staging_day": False,
        "contains_destructive_migration_evidence": False,
        "summary": {"passed": len(scenarios), "failed": 0, "errors": 0, "total": len(scenarios)},
        "scenarios": scenarios,
    }


def _mysql_migration() -> dict[str, object]:
    scenarios = [_scenario_result(item.scenario_id) for item in MYSQL_MIGRATION_SCENARIOS]
    return {
        "schema_version": 1,
        "scope": "real_mysql_migration_rehearsal",
        "environment": "staging",
        "target_fingerprint": SHA_B,
        "status": "passed",
        "counts_as_staging_day": False,
        "contains_destructive_migration_evidence": True,
        "summary": {"passed": len(scenarios), "failed": 0, "errors": 0, "total": len(scenarios)},
        "scenarios": scenarios,
    }


def _staging_gate() -> dict[str, object]:
    start = date(2026, 7, 13)
    days = []
    for offset in range(5):
        current = start + timedelta(days=offset)
        days.append(
            {
                "run_id": f"staging-{current.isoformat()}",
                "release_revision": RELEASE,
                "edition_date": current.isoformat(),
                "status": "passed",
                "counts_as_staging_day": True,
                "failure_codes": [],
                "evidence_sha256": "4" * 64,
            }
        )
    return {
        "schema_version": 1,
        "scope": "market_morning_t1_staging_gate",
        "status": "passed",
        "counts_as_t1_evidence": True,
        "required_consecutive_days": 5,
        "trailing_consecutive_days": 5,
        "longest_consecutive_days": 5,
        "eligible_dates": [item["edition_date"] for item in days],
        "blocking_codes": [],
        "days": days,
    }


def _external_signoffs() -> dict[str, object]:
    names = (
        "tdnet_rights",
        "edinet_rights",
        "company_ir_rights",
        "jpx_market_data_rights",
        "japan_legal_review",
    )
    return {
        "scope": "market_morning_t1_external_signoffs",
        "schema_version": 2,
        "environment_tier": "staging",
        "release_revision": RELEASE,
        "runtime_schema_revision": EXPECTED_MARKET_MORNING_SCHEMA_REVISION,
        "generated_at": "2026-07-20T09:00:00+09:00",
        "signoffs": {
            name: {
                "status": "approved",
                "approval_reference": f"ticket:{name}",
                "approved_at": "2026-07-19T09:00:00+09:00",
                "review_due_on": "2027-07-19",
                "evidence_sha256": "5" * 64,
            }
            for name in names
        },
    }


def _fail_probe_manifest(value: dict[str, object]) -> None:
    value["status"] = "failed"
    summary = value["summary"]
    summary["passed"] -= 1
    summary["failed"] = 1
    first = value["scenarios"][0]
    first["status"] = "failed"
    first["exit_code"] = 1


def _fail_staging_gate(value: dict[str, object]) -> None:
    value.update(
        status="not_ready",
        counts_as_t1_evidence=False,
        trailing_consecutive_days=0,
        eligible_dates=[],
        blocking_codes=["staging_day_failed", "insufficient_consecutive_days"],
    )


def _evaluate(**overrides):
    from src.market_morning.t1_release_gate import evaluate_t1_release_gate

    inputs = {
        "mysql_acceptance": _mysql_acceptance(),
        "mysql_migration": _mysql_migration(),
        "staging_gate": _staging_gate(),
        "external_signoffs": _external_signoffs(),
    }
    inputs.update(overrides)
    return evaluate_t1_release_gate(**inputs)


def test_release_gate_passes_only_with_all_four_evidence_classes() -> None:
    report = _evaluate()

    assert report.status == "approved"
    assert report.counts_as_t1_release_evidence is True
    assert report.release_revision == RELEASE
    assert report.runtime_schema_revision == EXPECTED_MARKET_MORNING_SCHEMA_REVISION
    assert report.blocking_codes == ()
    assert set(report.input_sha256) == {
        "mysql_acceptance",
        "mysql_migration",
        "staging_gate",
        "external_signoffs",
    }
    assert all(len(value) == 64 for value in report.input_sha256.values())


@pytest.mark.parametrize(
    ("input_name", "mutate", "blocking_code"),
    (
        (
            "mysql_acceptance",
            _fail_probe_manifest,
            "mysql_acceptance_failed",
        ),
        (
            "mysql_migration",
            _fail_probe_manifest,
            "mysql_migration_failed",
        ),
        (
            "staging_gate",
            _fail_staging_gate,
            "staging_gate_not_ready",
        ),
    ),
)
def test_release_gate_blocks_each_incomplete_automated_evidence(
    input_name: str,
    mutate,
    blocking_code: str,
) -> None:
    values = {
        "mysql_acceptance": _mysql_acceptance(),
        "mysql_migration": _mysql_migration(),
        "staging_gate": _staging_gate(),
    }
    mutate(values[input_name])

    report = _evaluate(**{input_name: values[input_name]})

    assert report.status == "not_ready"
    assert report.counts_as_t1_release_evidence is False
    assert blocking_code in report.blocking_codes


def test_release_gate_requires_distinct_acceptance_and_migration_databases() -> None:
    migration = _mysql_migration()
    migration["target_fingerprint"] = SHA_A

    report = _evaluate(mysql_migration=migration)

    assert report.status == "not_ready"
    assert "mysql_targets_not_isolated" in report.blocking_codes


@pytest.mark.parametrize(
    ("input_name", "payload_factory"),
    (
        ("mysql_acceptance", _mysql_acceptance),
        ("mysql_migration", _mysql_migration),
    ),
)
def test_release_gate_rejects_ci_mysql_evidence(
    input_name: str,
    payload_factory,
) -> None:
    from src.market_morning.t1_release_gate import T1ReleaseEvidenceError

    payload = payload_factory()
    payload["environment"] = "ci"

    with pytest.raises(T1ReleaseEvidenceError, match="staging environment"):
        _evaluate(**{input_name: payload})


def test_release_gate_blocks_missing_expired_or_unapproved_external_signoff() -> None:
    payload = _external_signoffs()
    payload["signoffs"]["tdnet_rights"]["status"] = "blocked"
    payload["signoffs"]["edinet_rights"]["review_due_on"] = "2026-07-19"

    report = _evaluate(external_signoffs=payload)

    assert report.status == "not_ready"
    assert "tdnet_rights_not_approved" in report.blocking_codes
    assert "edinet_rights_expired" in report.blocking_codes


def test_release_gate_requires_company_ir_registry_approval() -> None:
    payload = _external_signoffs()
    payload["signoffs"]["company_ir_rights"]["status"] = "blocked"

    report = _evaluate(external_signoffs=payload)

    assert report.status == "not_ready"
    assert "company_ir_rights_not_approved" in report.blocking_codes


def test_release_gate_blocks_release_or_schema_mismatch() -> None:
    payload = _external_signoffs()
    payload["release_revision"] = "b" * 40
    payload["runtime_schema_revision"] = "0016_market_morning_model_usage"

    report = _evaluate(external_signoffs=payload)

    assert "release_revision_mismatch" in report.blocking_codes
    assert "runtime_schema_revision_mismatch" in report.blocking_codes


@pytest.mark.parametrize(
    "replacement_scope",
    (
        "automated_local_synthetic",
        "real_mysql_acceptance",
        "market_morning_staging_day",
    ),
)
def test_release_gate_rejects_substituted_or_malformed_external_evidence(
    replacement_scope: str,
) -> None:
    from src.market_morning.t1_release_gate import T1ReleaseEvidenceError

    payload = _external_signoffs()
    payload["scope"] = replacement_scope

    with pytest.raises(T1ReleaseEvidenceError):
        _evaluate(external_signoffs=payload)


@pytest.mark.parametrize(
    ("input_name", "payload_factory", "replacement_scope"),
    (
        ("mysql_acceptance", _mysql_acceptance, "automated_local_synthetic"),
        ("mysql_migration", _mysql_migration, "real_mysql_acceptance"),
        ("staging_gate", _staging_gate, "market_morning_staging_day"),
    ),
)
def test_release_gate_rejects_scope_substitution_for_automated_evidence(
    input_name: str,
    payload_factory,
    replacement_scope: str,
) -> None:
    from src.market_morning.t1_release_gate import T1ReleaseEvidenceError

    payload = payload_factory()
    payload["scope"] = replacement_scope

    with pytest.raises(T1ReleaseEvidenceError):
        _evaluate(**{input_name: payload})


def test_release_gate_requires_exact_signoff_keys_and_safe_references() -> None:
    from src.market_morning.t1_release_gate import T1ReleaseEvidenceError

    missing = _external_signoffs()
    del missing["signoffs"]["tdnet_rights"]
    with pytest.raises(T1ReleaseEvidenceError):
        _evaluate(external_signoffs=missing)

    unsafe = _external_signoffs()
    unsafe["signoffs"]["tdnet_rights"]["approval_reference"] = "person@example.jp"
    with pytest.raises(T1ReleaseEvidenceError):
        _evaluate(external_signoffs=unsafe)


def test_release_gate_output_is_hash_only_and_omits_approval_references() -> None:
    rendered = json.dumps(_evaluate().to_dict(), sort_keys=True)

    assert "ticket:" not in rendered
    assert "approved_at" not in rendered
    assert "review_due_on" not in rendered
    assert "evidence_sha256" not in rendered
    assert "target_fingerprint" not in rendered


def test_repository_external_signoff_template_is_deliberately_blocked() -> None:
    path = Path(
        "docs/evidence/market-morning/t1-external-signoffs.template.json"
    )
    payload = json.loads(path.read_text(encoding="utf-8"))

    report = _evaluate(external_signoffs=payload)

    assert report.status == "not_ready"
    assert all(
        value["status"] == "blocked"
        for value in payload["signoffs"].values()
    )
    assert all(
        value["evidence_sha256"] == "0" * 64
        for value in payload["signoffs"].values()
    )


def test_release_gate_cli_writes_a_strict_report(tmp_path: Path) -> None:
    from src.market_morning.t1_release_gate_cli import run_cli

    inputs = {
        "mysql-acceptance.json": _mysql_acceptance(),
        "mysql-migration.json": _mysql_migration(),
        "staging-gate.json": _staging_gate(),
        "external-signoffs.json": _external_signoffs(),
    }
    for name, payload in inputs.items():
        (tmp_path / name).write_text(json.dumps(payload), encoding="utf-8")
    destination = tmp_path / "release-gate.json"

    exit_code = run_cli(
        [
            "--mysql-acceptance",
            str(tmp_path / "mysql-acceptance.json"),
            "--mysql-migration",
            str(tmp_path / "mysql-migration.json"),
            "--staging-gate",
            str(tmp_path / "staging-gate.json"),
            "--external-signoffs",
            str(tmp_path / "external-signoffs.json"),
            "--output",
            str(destination),
        ]
    )
    body = json.loads(destination.read_text(encoding="utf-8"))

    assert exit_code == 0
    assert body["scope"] == "market_morning_t1_release_gate"
    assert body["status"] == "approved"
    assert body["counts_as_t1_release_evidence"] is True


def test_release_gate_cli_rejects_output_overwriting_any_input(tmp_path: Path) -> None:
    from src.market_morning.t1_release_gate_cli import run_cli

    source = tmp_path / "same.json"
    source.write_text(json.dumps(_mysql_acceptance()), encoding="utf-8")

    with pytest.raises(SystemExit):
        run_cli(
            [
                "--mysql-acceptance",
                str(source),
                "--mysql-migration",
                str(source),
                "--staging-gate",
                str(source),
                "--external-signoffs",
                str(source),
                "--output",
                str(source),
            ]
        )


def test_pyproject_exposes_t1_release_gate_entrypoint() -> None:
    pyproject = Path("pyproject.toml").read_text(encoding="utf-8")

    assert (
        'vibe-trading-market-morning-t1-release-gate = '
        '"src.market_morning.t1_release_gate_cli:main"'
    ) in pyproject
