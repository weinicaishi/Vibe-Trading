from __future__ import annotations

import json
from pathlib import Path
import ast

import pytest


def test_migration_target_requires_a_distinct_disposable_mysql_database() -> None:
    from src.market_morning.mysql_migration_rehearsal import (
        MigrationTargetError,
        validate_migration_database_url,
    )

    target = validate_migration_database_url(
        "mysql+asyncmy://migration:secret@db.example/market_morning_migration_acceptance_ci",
        production_url="mysql+asyncmy://app:other@db.example/market_morning",
    )

    assert target.database == "market_morning_migration_acceptance_ci"
    assert len(target.target_fingerprint) == 64
    assert "secret" not in repr(target)
    assert "db.example" not in repr(target)

    with pytest.raises(MigrationTargetError, match="migration acceptance database"):
        validate_migration_database_url("mysql+asyncmy://app:secret@db.example/market_morning_acceptance_ci")
    with pytest.raises(MigrationTargetError, match="production database"):
        validate_migration_database_url(
            "mysql+asyncmy://migration:secret@db.example/market_morning_migration_acceptance_ci",
            production_url=("mysql+asyncmy://prod:other@db.example/market_morning_migration_acceptance_ci"),
        )
    with pytest.raises(MigrationTargetError, match=r"mysql\+asyncmy"):
        validate_migration_database_url("mysql://migration:secret@db.example/market_morning_migration_acceptance_ci")


def test_migration_manifest_runs_catalog_and_marks_destructive_evidence() -> None:
    from src.market_morning.mysql_migration_rehearsal import (
        MYSQL_MIGRATION_SCENARIOS,
        run_mysql_migration_rehearsal,
    )
    from src.market_morning.rehearsal import ProbeOutcome

    assert {scenario.scenario_id for scenario in MYSQL_MIGRATION_SCENARIOS} == {
        "mysql_migration_fresh_head",
        "mysql_migration_0016_to_head",
        "mysql_migration_downgrade_roundtrip",
    }

    called: list[str] = []

    def execute(scenario):
        called.append(scenario.scenario_id)
        return ProbeOutcome(exit_code=0, duration_ms=9, output_sha256="a" * 64)

    manifest = run_mysql_migration_rehearsal(
        execute,
        environment="mysql-migration-ci",
        run_id="mysql-migration-001",
        target_fingerprint="b" * 64,
        started_at="2026-07-21T05:00:00Z",
        finished_at="2026-07-21T05:01:00Z",
    )
    payload = manifest.to_dict()

    assert called == [scenario.scenario_id for scenario in MYSQL_MIGRATION_SCENARIOS]
    assert payload["status"] == "passed"
    assert payload["scope"] == "real_mysql_migration_rehearsal"
    assert payload["counts_as_staging_day"] is False
    assert payload["contains_destructive_migration_evidence"] is True
    assert payload["target_fingerprint"] == "b" * 64
    assert payload["summary"] == {
        "passed": 3,
        "failed": 0,
        "errors": 0,
        "total": 3,
    }
    serialized = json.dumps(payload)
    assert "stdout" not in serialized.lower()
    assert "stderr" not in serialized.lower()
    assert "database_url" not in serialized.lower()


def test_migration_catalog_node_ids_resolve_to_real_tests() -> None:
    from src.market_morning.mysql_migration_rehearsal import (
        MYSQL_MIGRATION_SCENARIOS,
    )

    for scenario in MYSQL_MIGRATION_SCENARIOS:
        for node_id in scenario.pytest_node_ids:
            path_text, test_name = node_id.split("::", maxsplit=1)
            path = Path(path_text)
            assert path.exists(), node_id
            module = ast.parse(path.read_text(encoding="utf-8"))
            functions = {node.name for node in module.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
            assert test_name in functions, node_id


def test_migration_cli_refuses_unconfirmed_or_unsafe_targets(tmp_path: Path) -> None:
    from src.market_morning.mysql_migration_rehearsal_cli import run_cli

    destination = tmp_path / "must-not-exist.json"
    called: list[str] = []

    def execute(scenario, _child_environment):
        called.append(scenario.scenario_id)
        raise AssertionError("unsafe migration target must not execute probes")

    with pytest.raises(SystemExit):
        run_cli(
            ["--output", str(destination), "--environment", "ci"],
            executor=execute,
            environ={
                "VIBE_MARKET_MORNING_MIGRATION_DATABASE_URL": (
                    "mysql+asyncmy://migration:secret@db/market_morning_migration_acceptance_ci"
                )
            },
        )
    with pytest.raises(SystemExit):
        run_cli(
            [
                "--output",
                str(destination),
                "--environment",
                "ci",
                "--confirm-destructive-reset-of-dedicated-database",
            ],
            executor=execute,
            environ={"VIBE_MARKET_MORNING_MIGRATION_DATABASE_URL": ("mysql+asyncmy://app:secret@db/market_morning")},
        )

    assert called == []
    assert not destination.exists()


def test_migration_cli_writes_hash_only_evidence_and_scopes_child_env(
    tmp_path: Path,
) -> None:
    from src.market_morning.mysql_migration_rehearsal_cli import run_cli
    from src.market_morning.rehearsal import ProbeOutcome

    destination = tmp_path / "evidence" / "mysql-migration.json"
    secret_url = "mysql+asyncmy://migration:super-secret@db/market_morning_migration_acceptance_ci"
    child_environments: list[dict[str, str]] = []

    def execute(_scenario, child_environment):
        child_environments.append(dict(child_environment))
        return ProbeOutcome(exit_code=0, duration_ms=4, output_sha256="c" * 64)

    exit_code = run_cli(
        [
            "--output",
            str(destination),
            "--environment",
            "mysql-migration-ci",
            "--confirm-destructive-reset-of-dedicated-database",
        ],
        executor=execute,
        environ={"VIBE_MARKET_MORNING_MIGRATION_DATABASE_URL": secret_url},
    )

    assert exit_code == 0
    assert child_environments
    assert all(
        child["VIBE_MARKET_MORNING_DATABASE_URL"] == secret_url
        and child["VIBE_MARKET_MORNING_ENABLED"] == "true"
        and child["VIBE_MARKET_MORNING_MYSQL_MIGRATION_CONFIRMED"] == "true"
        for child in child_environments
    )
    raw_manifest = destination.read_text(encoding="utf-8")
    payload = json.loads(raw_manifest)
    assert payload["status"] == "passed"
    assert payload["environment"] == "mysql-migration-ci"
    assert payload["contains_destructive_migration_evidence"] is True
    assert "super-secret" not in raw_manifest
    assert "db/market_morning" not in raw_manifest


def test_pyproject_exposes_mysql_migration_rehearsal_entrypoint() -> None:
    pyproject = Path("pyproject.toml").read_text(encoding="utf-8")
    env_example = Path("agent/.env.example").read_text(encoding="utf-8")

    assert (
        "vibe-trading-market-morning-mysql-migration-rehearsal = "
        '"src.market_morning.mysql_migration_rehearsal_cli:main"' in pyproject
    )
    assert "VIBE_MARKET_MORNING_MIGRATION_DATABASE_URL=" in env_example
    assert "destructive" in env_example.lower()
