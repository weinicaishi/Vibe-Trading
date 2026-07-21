from __future__ import annotations

import json
from pathlib import Path

import pytest


def test_acceptance_target_requires_a_dedicated_non_production_mysql_database() -> None:
    from src.market_morning.mysql_acceptance import (
        AcceptanceTargetError,
        validate_acceptance_database_url,
    )

    target = validate_acceptance_database_url(
        "mysql+asyncmy://acceptance:super-secret@db.example/market_morning_acceptance_ci",
        production_url="mysql+asyncmy://app:other-secret@db.example/market_morning",
    )

    assert target.database == "market_morning_acceptance_ci"
    assert len(target.target_fingerprint) == 64
    assert "super-secret" not in repr(target)
    assert "db.example" not in repr(target)

    with pytest.raises(AcceptanceTargetError, match="dedicated acceptance database"):
        validate_acceptance_database_url(
            "mysql+asyncmy://app:secret@db.example/market_morning",
        )
    with pytest.raises(AcceptanceTargetError, match="production database"):
        validate_acceptance_database_url(
            "mysql+asyncmy://acceptance:secret@db.example/market_morning_acceptance_ci",
            production_url=("mysql+asyncmy://prod:different-secret@db.example/market_morning_acceptance_ci"),
        )
    with pytest.raises(AcceptanceTargetError, match=r"mysql\+asyncmy"):
        validate_acceptance_database_url(
            "mysql://acceptance:secret@db.example/market_morning_acceptance_ci",
        )


def test_real_mysql_manifest_runs_all_catalog_probes_without_persisting_output() -> None:
    from src.market_morning.mysql_acceptance import (
        MYSQL_ACCEPTANCE_SCENARIOS,
        run_mysql_acceptance,
    )
    from src.market_morning.rehearsal import ProbeOutcome

    called: list[str] = []

    assert {scenario.scenario_id for scenario in MYSQL_ACCEPTANCE_SCENARIOS} == {
        "mysql_schema_and_session",
        "mysql_job_concurrency",
        "mysql_delivery_concurrency",
        "mysql_account_deletion",
        "mysql_source_revision_cursor",
        "mysql_morning_edition_versions",
        "mysql_worker_skip_locked",
        "mysql_scheduler_lease",
        "mysql_publication_halt_concurrency",
        "mysql_global_run_current_success",
        "mysql_event_brief_model_usage",
        "mysql_email_webhook_ordering",
    }

    def execute(scenario):
        called.append(scenario.scenario_id)
        return ProbeOutcome(
            exit_code=0,
            duration_ms=7,
            output_sha256="a" * 64,
        )

    manifest = run_mysql_acceptance(
        execute,
        environment="mysql-ci",
        run_id="mysql-acceptance-001",
        target_fingerprint="b" * 64,
        started_at="2026-07-21T04:00:00Z",
        finished_at="2026-07-21T04:01:00Z",
    )
    payload = manifest.to_dict()

    assert called == [scenario.scenario_id for scenario in MYSQL_ACCEPTANCE_SCENARIOS]
    assert payload["status"] == "passed"
    assert payload["scope"] == "real_mysql_acceptance"
    assert payload["counts_as_staging_day"] is False
    assert payload["contains_destructive_migration_evidence"] is False
    assert payload["target_fingerprint"] == "b" * 64
    assert payload["summary"] == {
        "passed": len(MYSQL_ACCEPTANCE_SCENARIOS),
        "failed": 0,
        "errors": 0,
        "total": len(MYSQL_ACCEPTANCE_SCENARIOS),
    }
    serialized = json.dumps(payload)
    assert "stdout" not in serialized.lower()
    assert "stderr" not in serialized.lower()
    assert "database_url" not in serialized.lower()


def test_mysql_acceptance_cli_refuses_unsafe_or_unconfirmed_targets(
    tmp_path: Path,
) -> None:
    from src.market_morning.mysql_acceptance_cli import run_cli

    destination = tmp_path / "must-not-exist.json"
    called: list[str] = []

    def execute(scenario, _child_environment):
        called.append(scenario.scenario_id)
        raise AssertionError("unsafe acceptance target must not execute probes")

    with pytest.raises(SystemExit):
        run_cli(
            ["--output", str(destination), "--environment", "ci"],
            executor=execute,
            environ={
                "VIBE_MARKET_MORNING_ACCEPTANCE_DATABASE_URL": (
                    "mysql+asyncmy://acceptance:secret@db/market_morning_acceptance_ci"
                ),
            },
        )
    with pytest.raises(SystemExit):
        run_cli(
            [
                "--output",
                str(destination),
                "--environment",
                "ci",
                "--confirm-write-to-dedicated-database",
            ],
            executor=execute,
            environ={
                "VIBE_MARKET_MORNING_ACCEPTANCE_DATABASE_URL": ("mysql+asyncmy://app:secret@db/market_morning"),
            },
        )

    assert called == []
    assert not destination.exists()


def test_mysql_acceptance_cli_writes_hash_only_evidence_and_scopes_child_env(
    tmp_path: Path,
) -> None:
    from src.market_morning.mysql_acceptance_cli import run_cli
    from src.market_morning.rehearsal import ProbeOutcome

    destination = tmp_path / "evidence" / "mysql.json"
    secret_url = "mysql+asyncmy://acceptance:super-secret@db/market_morning_acceptance_ci"
    child_environments: list[dict[str, str]] = []

    def execute(_scenario, child_environment):
        child_environments.append(dict(child_environment))
        return ProbeOutcome(exit_code=0, duration_ms=4, output_sha256="c" * 64)

    exit_code = run_cli(
        [
            "--output",
            str(destination),
            "--environment",
            "mysql-ci",
            "--confirm-write-to-dedicated-database",
        ],
        executor=execute,
        environ={"VIBE_MARKET_MORNING_ACCEPTANCE_DATABASE_URL": secret_url},
    )

    assert exit_code == 0
    assert child_environments
    assert all(
        child["VIBE_MARKET_MORNING_DATABASE_URL"] == secret_url
        and child["VIBE_MARKET_MORNING_ENABLED"] == "true"
        and child["VIBE_MARKET_MORNING_MYSQL_ACCEPTANCE_CONFIRMED"] == "true"
        for child in child_environments
    )
    raw_manifest = destination.read_text(encoding="utf-8")
    payload = json.loads(raw_manifest)
    assert payload["status"] == "passed"
    assert payload["environment"] == "mysql-ci"
    assert "super-secret" not in raw_manifest
    assert "db/market_morning" not in raw_manifest


def test_pyproject_exposes_mysql_acceptance_entrypoint() -> None:
    pyproject = Path("pyproject.toml").read_text(encoding="utf-8")

    assert 'vibe-trading-market-morning-mysql-acceptance = "src.market_morning.mysql_acceptance_cli:main"' in pyproject
