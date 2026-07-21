"""Safe, hash-only evidence contracts for real MySQL acceptance probes."""

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


_ACCEPTANCE_DATABASE_PATTERN = re.compile(r"^market_morning_acceptance(?:_[a-z0-9]+)*$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class AcceptanceTargetError(ValueError):
    """Raised before any probe can touch an unsafe database target."""


@dataclass(frozen=True, slots=True)
class AcceptanceDatabaseTarget:
    database: str
    target_fingerprint: str


def _parse_mysql_url(raw_url: str, *, label: str) -> URL:
    if not raw_url.strip():
        raise AcceptanceTargetError(f"{label} database URL is required")
    try:
        parsed = make_url(raw_url)
    except Exception as error:
        raise AcceptanceTargetError(f"{label} database URL is invalid") from error
    if parsed.drivername != "mysql+asyncmy":
        raise AcceptanceTargetError(f"{label} database URL must use mysql+asyncmy")
    if not parsed.host or not parsed.database:
        raise AcceptanceTargetError(f"{label} database URL must name a host and database")
    return parsed


def _target_identity(parsed: URL) -> tuple[str, int, str]:
    assert parsed.host is not None
    assert parsed.database is not None
    return parsed.host.lower(), parsed.port or 3306, parsed.database.lower()


def validate_acceptance_database_url(
    raw_url: str,
    *,
    production_url: str = "",
) -> AcceptanceDatabaseTarget:
    """Require an unmistakably dedicated target before write probes run."""

    parsed = _parse_mysql_url(raw_url, label="acceptance")
    assert parsed.database is not None
    database = parsed.database.lower()
    if not _ACCEPTANCE_DATABASE_PATTERN.fullmatch(database):
        raise AcceptanceTargetError("acceptance URL must name a dedicated acceptance database")
    identity = _target_identity(parsed)
    if production_url.strip():
        production = _parse_mysql_url(production_url, label="production")
        if identity == _target_identity(production):
            raise AcceptanceTargetError("acceptance target must not be the production database")
    fingerprint_input = f"{identity[0]}:{identity[1]}/{identity[2]}"
    return AcceptanceDatabaseTarget(
        database=database,
        target_fingerprint=hashlib.sha256(fingerprint_input.encode("utf-8")).hexdigest(),
    )


MYSQL_ACCEPTANCE_SCENARIOS = (
    RehearsalScenario(
        scenario_id="mysql_schema_and_session",
        name="MySQL schema 与 session contract",
        expected=("真实 MySQL 为 8.x、连接 session 使用 UTC/utf8mb4，且 Alembic revision 精确为当前版本。"),
        pytest_node_ids=("agent/tests/test_market_morning_mysql_live.py::test_mysql_schema_and_session_contract",),
    ),
    RehearsalScenario(
        scenario_id="mysql_job_concurrency",
        name="MySQL durable job 并发幂等",
        expected=("两个真实连接并发写入同一幂等键只保留一个 job，且两个调用返回同一 job ID。"),
        pytest_node_ids=(
            "agent/tests/test_market_morning_mysql_live.py::test_mysql_job_enqueue_is_concurrent_and_idempotent",
        ),
    ),
    RehearsalScenario(
        scenario_id="mysql_delivery_concurrency",
        name="MySQL 邮件尝试并发幂等",
        expected=("两个真实连接并发创建同一用户同日邮件尝试时只保留一条不可变 attempt。"),
        pytest_node_ids=(
            "agent/tests/test_market_morning_mysql_live.py::test_mysql_delivery_attempt_is_concurrent_and_idempotent",
        ),
    ),
    RehearsalScenario(
        scenario_id="mysql_account_deletion",
        name="MySQL 账户删除事务",
        expected=("真实 MySQL 执行删除 SQL 后私有子数据被清理、身份被伪名化且审计完整；probe 整体回滚。"),
        pytest_node_ids=(
            "agent/tests/test_market_morning_mysql_live.py::test_mysql_account_deletion_executes_atomically_and_probe_rolls_back",
        ),
    ),
    RehearsalScenario(
        scenario_id="mysql_source_revision_cursor",
        name="MySQL 来源 revision 与 cursor",
        expected=("相同来源 revision 并发写入只保留一条，订正/撤回链稳定，失败状态不推进 durable cursor。"),
        pytest_node_ids=(
            "agent/tests/test_market_morning_mysql_live.py::test_mysql_source_revision_chain_and_cursor_are_durable",
        ),
    ),
    RehearsalScenario(
        scenario_id="mysql_morning_edition_versions",
        name="MySQL 用户朝刊版本链",
        expected=("相同 generation key 并发发布保持幂等，新 key 创建递增版本并指向前一版本。"),
        pytest_node_ids=(
            "agent/tests/test_market_morning_mysql_live.py::test_mysql_morning_edition_generation_and_revision_chain",
        ),
    ),
    RehearsalScenario(
        scenario_id="mysql_worker_skip_locked",
        name="MySQL worker SKIP LOCKED",
        expected=("两个并发 worker 在首个事务仍持锁时领取不同 job，并各自创建唯一 attempt。"),
        pytest_node_ids=(
            "agent/tests/test_market_morning_mysql_live.py::test_mysql_workers_claim_distinct_jobs_with_skip_locked",
        ),
    ),
    RehearsalScenario(
        scenario_id="mysql_scheduler_lease",
        name="MySQL scheduler 单活租约",
        expected=("同一租约只允许一个 owner，当前 owner 可续租，过期后其他 owner 才能接管。"),
        pytest_node_ids=(
            "agent/tests/test_market_morning_mysql_live.py::test_mysql_scheduler_lease_is_single_owner_and_recoverable",
        ),
    ),
    RehearsalScenario(
        scenario_id="mysql_publication_halt_concurrency",
        name="MySQL publication halt 并发单例",
        expected=("两个真实连接并发创建同日同原因 halt 时只保留一个 active override，并只写一条 created 审计。"),
        pytest_node_ids=(
            "agent/tests/test_market_morning_mysql_live.py::test_mysql_publication_halt_is_concurrent_and_audited_once",
        ),
    ),
    RehearsalScenario(
        scenario_id="mysql_global_run_current_success",
        name="MySQL global run 当前成功版本",
        expected=("同日两个成功 run 并发发布后均为不可变终态，但生成列唯一约束保证仅一个 current success。"),
        pytest_node_ids=(
            "agent/tests/test_market_morning_mysql_live.py::test_mysql_global_runs_publish_one_current_success",
        ),
    ),
    RehearsalScenario(
        scenario_id="mysql_event_brief_model_usage",
        name="MySQL EventBrief 与 model usage 幂等",
        expected=(
            "同一生成 key 与同一 brief attempt 的并发写入分别只留下一个 EventBrief 和一条 hash-only model usage。"
        ),
        pytest_node_ids=(
            "agent/tests/test_market_morning_mysql_live.py::test_mysql_event_brief_and_model_usage_are_idempotent",
        ),
    ),
    RehearsalScenario(
        scenario_id="mysql_email_webhook_ordering",
        name="MySQL 邮件 webhook 重复与乱序",
        expected=("clicked 先到后生命周期不倒退，随后 delivered 被标记 stale，重复 provider event 幂等返回。"),
        pytest_node_ids=(
            "agent/tests/test_market_morning_mysql_live.py::test_mysql_email_webhook_replay_and_out_of_order_events_are_monotonic",
        ),
    ),
)


@dataclass(frozen=True, slots=True)
class MysqlAcceptanceManifest:
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
        summary = {
            "passed": 0,
            "failed": 0,
            "errors": 0,
            "total": len(self.scenarios),
        }
        for result in self.scenarios:
            if result.status == "passed":
                summary["passed"] += 1
            elif result.status == "failed":
                summary["failed"] += 1
            else:
                summary["errors"] += 1
        return {
            "schema_version": 1,
            "run_id": self.run_id,
            "scope": "real_mysql_acceptance",
            "environment": self.environment,
            "target_fingerprint": self.target_fingerprint,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "status": self.status,
            "counts_as_staging_day": False,
            "contains_destructive_migration_evidence": False,
            "summary": summary,
            "scenarios": [result.to_dict() for result in self.scenarios],
        }


AcceptanceScenarioExecutor = Callable[[RehearsalScenario], ProbeOutcome]


def run_mysql_acceptance(
    executor: AcceptanceScenarioExecutor,
    *,
    environment: str,
    run_id: str,
    target_fingerprint: str,
    started_at: str,
    finished_at: str,
) -> MysqlAcceptanceManifest:
    """Run every catalog probe and contain failures without storing output."""

    canonical_environment = environment.strip()
    canonical_run_id = run_id.strip()
    if not canonical_environment or len(canonical_environment) > 128:
        raise ValueError("environment must contain 1 to 128 characters")
    if not canonical_run_id or len(canonical_run_id) > 128:
        raise ValueError("run_id must contain 1 to 128 characters")
    if not _SHA256_PATTERN.fullmatch(target_fingerprint):
        raise ValueError("target_fingerprint must be a lowercase SHA-256 digest")

    results: list[RehearsalScenarioResult] = []
    for scenario in MYSQL_ACCEPTANCE_SCENARIOS:
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
    return MysqlAcceptanceManifest(
        run_id=canonical_run_id,
        environment=canonical_environment,
        target_fingerprint=target_fingerprint,
        started_at=started_at,
        finished_at=finished_at,
        scenarios=tuple(results),
    )


__all__ = [
    "AcceptanceDatabaseTarget",
    "AcceptanceScenarioExecutor",
    "AcceptanceTargetError",
    "MYSQL_ACCEPTANCE_SCENARIOS",
    "MysqlAcceptanceManifest",
    "run_mysql_acceptance",
    "validate_acceptance_database_url",
]
