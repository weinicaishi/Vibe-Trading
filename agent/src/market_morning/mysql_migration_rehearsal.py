"""Destructive, dedicated-database MySQL migration rehearsal contracts."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import hashlib
import re
from typing import Any

from sqlalchemy.engine import URL, make_url

from src.market_morning.rehearsal import (
    ProbeOutcome,
    RehearsalScenario,
    RehearsalScenarioResult,
)


_MIGRATION_DATABASE_PATTERN = re.compile(r"^market_morning_migration_acceptance(?:_[a-z0-9]+)*$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class MigrationTargetError(ValueError):
    """Raised before a destructive probe can touch an unsafe target."""


@dataclass(frozen=True, slots=True)
class MigrationDatabaseTarget:
    database: str
    target_fingerprint: str


def _parse_mysql_url(raw_url: str, *, label: str) -> URL:
    if not raw_url.strip():
        raise MigrationTargetError(f"{label} database URL is required")
    try:
        parsed = make_url(raw_url)
    except Exception as error:
        raise MigrationTargetError(f"{label} database URL is invalid") from error
    if parsed.drivername != "mysql+asyncmy":
        raise MigrationTargetError(f"{label} database URL must use mysql+asyncmy")
    if not parsed.host or not parsed.database:
        raise MigrationTargetError(f"{label} database URL must name a host and database")
    return parsed


def _target_identity(parsed: URL) -> tuple[str, int, str]:
    assert parsed.host is not None
    assert parsed.database is not None
    return parsed.host.lower(), parsed.port or 3306, parsed.database.lower()


def validate_migration_database_url(
    raw_url: str,
    *,
    production_url: str = "",
) -> MigrationDatabaseTarget:
    """Require a dedicated migration-only database before destructive work."""

    parsed = _parse_mysql_url(raw_url, label="migration acceptance")
    assert parsed.database is not None
    database = parsed.database.lower()
    if not _MIGRATION_DATABASE_PATTERN.fullmatch(database):
        raise MigrationTargetError("migration URL must name a dedicated migration acceptance database")
    identity = _target_identity(parsed)
    if production_url.strip():
        production = _parse_mysql_url(production_url, label="production")
        if identity == _target_identity(production):
            raise MigrationTargetError("migration target must not be the production database")
    fingerprint_input = f"{identity[0]}:{identity[1]}/{identity[2]}"
    return MigrationDatabaseTarget(
        database=database,
        target_fingerprint=hashlib.sha256(fingerprint_input.encode("utf-8")).hexdigest(),
    )


MYSQL_MIGRATION_SCENARIOS = (
    RehearsalScenario(
        scenario_id="mysql_migration_fresh_head",
        name="MySQL 空库迁移到 head",
        expected=("专用库从 base 升级到 head 后 revision 精确为 0018，且 mm_* 表集合与 ORM metadata 一致。"),
        pytest_node_ids=(
            "agent/tests/test_market_morning_mysql_migration_live.py::test_mysql_migration_fresh_database_reaches_exact_head",
        ),
    ),
    RehearsalScenario(
        scenario_id="mysql_migration_0016_to_head",
        name="MySQL 0016 升级到 head",
        expected=("0016 账户与私测邀请数据在升级 0018 后保持不变，并创建 content reports 与 auth sessions 表。"),
        pytest_node_ids=(
            "agent/tests/test_market_morning_mysql_migration_live.py::test_mysql_migration_0016_data_survives_upgrade_to_head",
        ),
    ),
    RehearsalScenario(
        scenario_id="mysql_migration_downgrade_roundtrip",
        name="MySQL 0018 downgrade roundtrip",
        expected=(
            "head 降到 0016 时只移除 content reports 与 auth sessions 表，既有数据保留；重新升级后恢复精确 head。"
        ),
        pytest_node_ids=(
            "agent/tests/test_market_morning_mysql_migration_live.py::test_mysql_migration_downgrade_roundtrip_preserves_prior_data",
        ),
    ),
)


@dataclass(frozen=True, slots=True)
class MysqlMigrationManifest:
    run_id: str
    environment: str
    target_fingerprint: str
    started_at: str
    finished_at: str
    scenarios: tuple[RehearsalScenarioResult, ...]

    @property
    def status(self) -> str:
        return "passed" if all(result.status == "passed" for result in self.scenarios) else "failed"

    def to_dict(self) -> dict[str, Any]:
        summary = {"passed": 0, "failed": 0, "errors": 0, "total": 0}
        for result in self.scenarios:
            summary["total"] += 1
            if result.status == "passed":
                summary["passed"] += 1
            elif result.status == "failed":
                summary["failed"] += 1
            else:
                summary["errors"] += 1
        return {
            "schema_version": 1,
            "run_id": self.run_id,
            "scope": "real_mysql_migration_rehearsal",
            "environment": self.environment,
            "target_fingerprint": self.target_fingerprint,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "status": self.status,
            "counts_as_staging_day": False,
            "contains_destructive_migration_evidence": True,
            "summary": summary,
            "scenarios": [result.to_dict() for result in self.scenarios],
        }


MigrationScenarioExecutor = Callable[[RehearsalScenario], ProbeOutcome]


def run_mysql_migration_rehearsal(
    executor: MigrationScenarioExecutor,
    *,
    environment: str,
    run_id: str,
    target_fingerprint: str,
    started_at: str,
    finished_at: str,
) -> MysqlMigrationManifest:
    """Run every destructive migration probe while retaining hash-only output."""

    canonical_environment = environment.strip()
    canonical_run_id = run_id.strip()
    if not canonical_environment or len(canonical_environment) > 128:
        raise ValueError("environment must contain 1 to 128 characters")
    if not canonical_run_id or len(canonical_run_id) > 128:
        raise ValueError("run_id must contain 1 to 128 characters")
    if not _SHA256_PATTERN.fullmatch(target_fingerprint):
        raise ValueError("target_fingerprint must be a lowercase SHA-256 digest")

    results: list[RehearsalScenarioResult] = []
    for scenario in MYSQL_MIGRATION_SCENARIOS:
        try:
            outcome = executor(scenario)
        except Exception:
            results.append(
                RehearsalScenarioResult(
                    scenario_id=scenario.scenario_id,
                    name=scenario.name,
                    expected=scenario.expected,
                    status="error",
                    pytest_node_ids=scenario.pytest_node_ids,
                    exit_code=None,
                    duration_ms=None,
                    output_sha256=None,
                    failure_code="probe_execution_error",
                )
            )
            continue
        passed = outcome.exit_code == 0
        results.append(
            RehearsalScenarioResult(
                scenario_id=scenario.scenario_id,
                name=scenario.name,
                expected=scenario.expected,
                status="passed" if passed else "failed",
                pytest_node_ids=scenario.pytest_node_ids,
                exit_code=outcome.exit_code,
                duration_ms=outcome.duration_ms,
                output_sha256=outcome.output_sha256,
                failure_code=None if passed else "probe_nonzero_exit",
            )
        )
    return MysqlMigrationManifest(
        run_id=canonical_run_id,
        environment=canonical_environment,
        target_fingerprint=target_fingerprint,
        started_at=started_at,
        finished_at=finished_at,
        scenarios=tuple(results),
    )


__all__ = [
    "MYSQL_MIGRATION_SCENARIOS",
    "MigrationDatabaseTarget",
    "MigrationScenarioExecutor",
    "MigrationTargetError",
    "MysqlMigrationManifest",
    "run_mysql_migration_rehearsal",
    "validate_migration_database_url",
]
