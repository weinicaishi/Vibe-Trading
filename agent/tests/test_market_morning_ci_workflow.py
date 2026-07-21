from __future__ import annotations

from pathlib import Path

import yaml


WORKFLOW = Path(".github/workflows/test.yml")


def _workflow_text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def test_ci_workflow_is_valid_yaml() -> None:
    assert yaml.compose(_workflow_text()) is not None


def test_ci_runs_isolated_real_mysql_acceptance_and_migration() -> None:
    workflow = _workflow_text()

    assert "image: mysql:8.0" in workflow
    assert "market_morning_acceptance_ci" in workflow
    assert "market_morning_migration_acceptance_ci" in workflow
    assert workflow.count("COLLATE utf8mb4_ja_0900_as_cs") == 2
    assert "src.market_morning.mysql_acceptance_cli" in workflow
    assert "--confirm-write-to-dedicated-database" in workflow
    assert "src.market_morning.mysql_migration_rehearsal_cli" in workflow
    assert "--confirm-destructive-reset-of-dedicated-database" in workflow


def test_ci_mysql_evidence_cannot_claim_staging_scope() -> None:
    workflow = _workflow_text()

    assert workflow.count("--environment ci") == 3
    assert "--environment staging" not in workflow


def test_ci_uploads_hash_only_manifests_and_enforces_every_mysql_step() -> None:
    workflow = _workflow_text()

    assert (
        "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02"
        in workflow
    )
    assert "market-morning-mysql-acceptance.json" in workflow
    assert "market-morning-mysql-migration.json" in workflow
    for step_id in (
        "mysql_databases",
        "mysql_acceptance_schema",
        "mysql_acceptance",
        "mysql_migration",
    ):
        assert f'${{{{ steps.{step_id}.outcome }}}}' in workflow


def test_ci_binds_all_verification_outputs_to_one_immutable_release() -> None:
    workflow = _workflow_text()

    assert "src.market_morning.release_candidate_cli" in workflow
    assert '--expected-release-revision "${GITHUB_SHA}"' in workflow
    assert '--repository-root "${GITHUB_WORKSPACE}"' in workflow
    for evidence_name in (
        "backend_tests",
        "frontend_build",
        "frontend_tests",
        "mysql_acceptance",
        "mysql_migration",
    ):
        assert f'--evidence "{evidence_name}=' in workflow
    assert "market-morning-release-candidate-${{ github.sha }}" in workflow
