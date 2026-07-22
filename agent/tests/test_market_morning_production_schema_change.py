from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.market_morning.production_schema_change import (
    ProductionSchemaChangeTargetError,
    SchemaChangeExecution,
    SchemaSnapshot,
    validate_production_database_url,
)
from src.market_morning.production_schema_change_cli import (
    EXPECTED_HEAD_TABLE_COUNT,
    run_cli,
)


PRODUCT_URL = "mysql+asyncmy://product:super-secret@db.example:3306/market_morning_prod"
ACCEPTANCE_URL = "mysql+asyncmy://acceptance:secret@db.example:3306/market_morning_acceptance_ci"
MIGRATION_URL = "mysql+asyncmy://migration:secret@db.example:3306/market_morning_migration_acceptance_ci"
SOURCE_REVISION = "0004_market_morning_sources_events"
TARGET_REVISION = "0018_market_morning_auth_sessions"


def _environment(**overrides: str) -> dict[str, str]:
    values = {
        "VIBE_MARKET_MORNING_DATABASE_URL": PRODUCT_URL,
        "VIBE_MARKET_MORNING_ACCEPTANCE_DATABASE_URL": ACCEPTANCE_URL,
        "VIBE_MARKET_MORNING_MIGRATION_DATABASE_URL": MIGRATION_URL,
        "VIBE_MARKET_MORNING_RUNTIME_ENABLED": "false",
    }
    values.update(overrides)
    return values


def _apply_args(output: Path) -> list[str]:
    return [
        "--output",
        str(output),
        "--environment",
        "production",
        "--expected-current-revision",
        SOURCE_REVISION,
        "--backup-evidence-sha256",
        "a" * 64,
        "--change-reference",
        "CHG-2026-071",
        "--confirm-maintenance-window",
        "--confirm-runtime-stopped",
        "--confirm-no-automatic-downgrade",
        "--confirm-production-schema-change",
    ]


def _execution(exit_code: int = 0) -> SchemaChangeExecution:
    return SchemaChangeExecution(
        exit_code=exit_code,
        duration_ms=25,
        output_sha256="b" * 64,
    )


def test_production_target_rejects_acceptance_names_and_aliases() -> None:
    with pytest.raises(ProductionSchemaChangeTargetError):
        validate_production_database_url(ACCEPTANCE_URL)
    with pytest.raises(ProductionSchemaChangeTargetError):
        validate_production_database_url(
            PRODUCT_URL,
            acceptance_url=PRODUCT_URL,
        )
    target = validate_production_database_url(
        PRODUCT_URL,
        acceptance_url=ACCEPTANCE_URL,
        migration_acceptance_url=MIGRATION_URL,
    )
    assert target.database == "market_morning_prod"
    assert len(target.target_fingerprint) == 64


def test_check_only_never_runs_migration_and_emits_safe_manifest(
    tmp_path: Path,
) -> None:
    output = tmp_path / "check.json"

    def unexpected_executor(_environment: dict[str, str]) -> SchemaChangeExecution:
        raise AssertionError("check-only must not execute Alembic")

    exit_code = run_cli(
        [
            "--output",
            str(output),
            "--environment",
            "production",
            "--expected-current-revision",
            SOURCE_REVISION,
            "--check-only",
        ],
        inspector=lambda url: (
            SchemaSnapshot(SOURCE_REVISION, 14)
            if url == PRODUCT_URL
            else (_ for _ in ()).throw(AssertionError("unexpected URL"))
        ),
        executor=unexpected_executor,
        environ=_environment(),
    )

    assert exit_code == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["scope"] == "production_schema_change"
    assert payload["status"] == "passed"
    assert payload["check_only"] is True
    assert payload["before"]["revision"] == SOURCE_REVISION
    assert payload["execution"] is None
    assert payload["contains_credentials"] is False
    assert "super-secret" not in output.read_text(encoding="utf-8")


def test_apply_requires_every_confirmation_before_inspection(tmp_path: Path) -> None:
    inspected = False

    def inspector(_url: str) -> SchemaSnapshot:
        nonlocal inspected
        inspected = True
        return SchemaSnapshot(SOURCE_REVISION, 14)

    with pytest.raises(SystemExit) as error:
        run_cli(
            _apply_args(tmp_path / "missing-confirmation.json")[:-1],
            inspector=inspector,
            environ=_environment(),
        )
    assert error.value.code == 2
    assert inspected is False


def test_apply_refuses_when_runtime_environment_is_enabled(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as error:
        run_cli(
            _apply_args(tmp_path / "runtime-enabled.json"),
            inspector=lambda _url: SchemaSnapshot(SOURCE_REVISION, 14),
            environ=_environment(VIBE_MARKET_MORNING_RUNTIME_ENABLED="true"),
        )
    assert error.value.code == 2


def test_source_revision_mismatch_fails_before_alembic(tmp_path: Path) -> None:
    output = tmp_path / "source-mismatch.json"

    def unexpected_executor(_environment: dict[str, str]) -> SchemaChangeExecution:
        raise AssertionError("revision drift must stop before Alembic")

    exit_code = run_cli(
        _apply_args(output),
        inspector=lambda _url: SchemaSnapshot("0003_market_morning_users", 7),
        executor=unexpected_executor,
        environ=_environment(),
    )
    assert exit_code == 1
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["failure_code"] == "source_revision_mismatch"
    assert payload["automatic_downgrade_attempted"] is False


def test_successful_apply_hashes_output_and_change_reference(tmp_path: Path) -> None:
    output = tmp_path / "passed.json"
    snapshots = iter(
        (
            SchemaSnapshot(SOURCE_REVISION, 14),
            SchemaSnapshot(TARGET_REVISION, EXPECTED_HEAD_TABLE_COUNT),
        )
    )

    def execute(environment: dict[str, str]) -> SchemaChangeExecution:
        assert environment["VIBE_MARKET_MORNING_DATABASE_URL"] == PRODUCT_URL
        assert environment["VIBE_MARKET_MORNING_RUNTIME_ENABLED"] == "false"
        assert environment["VIBE_MARKET_MORNING_PRODUCTION_SCHEMA_CHANGE_CONFIRMED"] == "true"
        return _execution()

    exit_code = run_cli(
        _apply_args(output),
        inspector=lambda _url: next(snapshots),
        executor=execute,
        environ=_environment(),
    )
    assert exit_code == 0
    raw = output.read_text(encoding="utf-8")
    payload = json.loads(raw)
    assert payload["status"] == "passed"
    assert payload["before"]["revision"] == SOURCE_REVISION
    assert payload["after"]["revision"] == TARGET_REVISION
    assert payload["execution"] == {
        "duration_ms": 25,
        "exit_code": 0,
        "output_sha256": "b" * 64,
    }
    assert payload["backup_evidence_sha256"] == "a" * 64
    assert len(payload["change_reference_sha256"]) == 64
    assert "CHG-2026-071" not in raw
    assert "super-secret" not in raw
    assert payload["automatic_downgrade_attempted"] is False


def test_nonzero_migration_never_attempts_automatic_downgrade(
    tmp_path: Path,
) -> None:
    output = tmp_path / "migration-failed.json"
    inspections = 0

    def inspect(_url: str) -> SchemaSnapshot:
        nonlocal inspections
        inspections += 1
        return SchemaSnapshot(SOURCE_REVISION, 14)

    exit_code = run_cli(
        _apply_args(output),
        inspector=inspect,
        executor=lambda _environment: _execution(exit_code=1),
        environ=_environment(),
    )
    assert exit_code == 1
    assert inspections == 1
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["failure_code"] == "migration_nonzero_exit"
    assert payload["automatic_downgrade_attempted"] is False
    assert payload["after"] is None


def test_postflight_requires_exact_head_and_table_count(tmp_path: Path) -> None:
    output = tmp_path / "postflight-failed.json"
    snapshots = iter(
        (
            SchemaSnapshot(SOURCE_REVISION, 14),
            SchemaSnapshot(TARGET_REVISION, EXPECTED_HEAD_TABLE_COUNT - 1),
        )
    )
    exit_code = run_cli(
        _apply_args(output),
        inspector=lambda _url: next(snapshots),
        executor=lambda _environment: _execution(),
        environ=_environment(),
    )
    assert exit_code == 1
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["failure_code"] == "postflight_schema_mismatch"
    assert payload["after"]["market_morning_table_count"] == (EXPECTED_HEAD_TABLE_COUNT - 1)


def test_already_current_is_an_idempotent_noop(tmp_path: Path) -> None:
    output = tmp_path / "current.json"

    def unexpected_executor(_environment: dict[str, str]) -> SchemaChangeExecution:
        raise AssertionError("current schema must not execute Alembic")

    args = _apply_args(output)
    args[args.index(SOURCE_REVISION)] = TARGET_REVISION
    exit_code = run_cli(
        args,
        inspector=lambda _url: SchemaSnapshot(
            TARGET_REVISION,
            EXPECTED_HEAD_TABLE_COUNT,
        ),
        executor=unexpected_executor,
        environ=_environment(),
    )
    assert exit_code == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["status"] == "passed"
    assert payload["execution"] is None


def test_already_current_revision_still_requires_complete_table_set(
    tmp_path: Path,
) -> None:
    output = tmp_path / "current-incomplete.json"
    args = _apply_args(output)
    args[args.index(SOURCE_REVISION)] = TARGET_REVISION
    exit_code = run_cli(
        args,
        inspector=lambda _url: SchemaSnapshot(
            TARGET_REVISION,
            EXPECTED_HEAD_TABLE_COUNT - 1,
        ),
        executor=lambda _environment: (_ for _ in ()).throw(
            AssertionError("incomplete current schema must not execute Alembic")
        ),
        environ=_environment(),
    )
    assert exit_code == 1
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["failure_code"] == "source_schema_incomplete"


def test_pyproject_exposes_production_schema_change_entrypoint() -> None:
    pyproject = Path("pyproject.toml").read_text(encoding="utf-8")
    assert (
        'vibe-trading-market-morning-production-schema-change = "src.market_morning.production_schema_change_cli:main"'
    ) in pyproject
