"""Auditable local T0 rehearsal contracts for the Market Morning runbook.

This module deliberately distinguishes automated synthetic evidence from a
real staging trading day.  A passing manifest reduces the manual rehearsal
surface, but can never satisfy the five-day staging gate by itself.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import re
from typing import Any


_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class RehearsalScenario:
    scenario_id: str
    name: str
    expected: str
    pytest_node_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        for field_name in ("scenario_id", "name", "expected"):
            value = getattr(self, field_name).strip()
            if not value:
                raise ValueError(f"{field_name} must not be empty")
            object.__setattr__(self, field_name, value)
        if not self.pytest_node_ids:
            raise ValueError("pytest_node_ids must not be empty")
        if len(set(self.pytest_node_ids)) != len(self.pytest_node_ids):
            raise ValueError("pytest_node_ids must be unique per scenario")


LOCAL_SYNTHETIC_SCENARIOS = (
    RehearsalScenario(
        scenario_id="standard_trading_day",
        name="标准交易日",
        expected="开放日可通过快照 Gate 发布，且应用层邮件尝试保持同日幂等。",
        pytest_node_ids=(
            "agent/tests/test_market_morning_calendar.py::test_scenario_a_japan_and_previous_us_session_open",
            "agent/tests/test_market_morning_global_run_service.py::test_global_run_service_publishes_only_after_snapshot_gate",
            "agent/tests/test_market_morning_email_delivery.py::test_delivery_attempt_insert_is_concurrency_safe_and_exactly_keyed",
        ),
    ),
    RehearsalScenario(
        scenario_id="us_holiday",
        name="美国休市",
        expected="日本开市时使用最后有效美国交易日，并明确标记美国休市场景。",
        pytest_node_ids=(
            "agent/tests/test_market_morning_calendar.py::test_scenario_b_japan_open_and_us_holiday_uses_last_valid_close",
        ),
    ),
    RehearsalScenario(
        scenario_id="japan_holiday",
        name="日本休市",
        expected="日本休市日不发布朝刊或邮件，但来源采集计划仍可继续。",
        pytest_node_ids=(
            "agent/tests/test_market_morning_calendar.py::test_scenario_c_japan_holiday_does_not_publish_but_keeps_collection",
            "agent/tests/test_market_morning_scheduler.py::test_snapshot_actions_are_suppressed_for_closed_or_complete_editions",
        ),
    ),
    RehearsalScenario(
        scenario_id="single_source_failure",
        name="单源失败",
        expected="失败被隔离，cursor 不前进，覆盖不足不能显示为已确认无新事件。",
        pytest_node_ids=(
            "agent/tests/test_market_morning_sources.py::test_collect_source_batch_advances_cursor_only_after_every_document_normalizes",
            "agent/tests/test_market_morning_job_handlers.py::test_source_handler_records_sanitized_failure_in_an_independent_transaction",
        ),
    ),
    RehearsalScenario(
        scenario_id="market_data_failure",
        name="市场数据失败",
        expected="任一必需市场快照失败都会阻止发布，且不会创建空邮件。",
        pytest_node_ids=(
            "agent/tests/test_market_morning_global_run_service.py::test_global_run_service_records_failed_gate_without_publishing",
        ),
    ),
    RehearsalScenario(
        scenario_id="model_failure",
        name="模型失败",
        expected="瞬态错误可重试，最终失败只生成安全降级卡，非法内容被阻断。",
        pytest_node_ids=(
            "agent/tests/test_market_morning_event_brief_service.py::test_transient_model_failure_is_recorded_and_retried",
            "agent/tests/test_market_morning_event_brief_service.py::test_final_model_failure_publishes_source_only_degraded_card",
            "agent/tests/test_market_morning_event_brief_service.py::test_invalid_model_content_is_blocked_without_persisting_its_claims",
        ),
    ),
    RehearsalScenario(
        scenario_id="worker_restart",
        name="worker 重启",
        expected="过期 lease 在重启后恢复，原 job 与 attempt 记录不被重复创建。",
        pytest_node_ids=(
            "agent/tests/test_market_morning_worker.py::test_worker_loop_recovers_an_expired_job_before_claiming_work",
            "agent/tests/test_market_morning_jobs.py::test_expired_running_job_is_recovered_for_retry",
        ),
    ),
    RehearsalScenario(
        scenario_id="email_timeout",
        name="邮件超时",
        expected="provider 超时记录脱敏失败并重试原 attempt，不生成第二封应用层提醒。",
        pytest_node_ids=(
            "agent/tests/test_market_morning_email_delivery_service.py::test_email_service_records_sanitized_provider_failure_and_retries",
            "agent/tests/test_market_morning_email_delivery.py::test_delivery_attempt_state_machine_retries_and_preserves_provider_identity",
        ),
    ),
    RehearsalScenario(
        scenario_id="event_brief_rejection",
        name="EventBrief 下架",
        expected="rejected 内容从当前研究读取路径排除，同时保留不可变内容和审核轨迹。",
        pytest_node_ids=(
            "agent/tests/test_market_morning_operations.py::test_rejection_is_audited_idempotent_and_approval_fails_closed",
            "agent/tests/test_market_morning_edition_generation.py::test_rejected_event_briefs_are_excluded_from_shared_reader_subquery",
        ),
    ),
    RehearsalScenario(
        scenario_id="manual_publication_halt",
        name="手动停发",
        expected="scheduler 尊重 active halt，撤销不能绕过日历、质量 Gate 或发布窗口。",
        pytest_node_ids=(
            "agent/tests/test_market_morning_scheduler.py::test_manual_halt_is_loaded_before_calendar_decision_and_blocks_enqueue",
            "agent/tests/test_market_morning_manual_overrides.py::test_revoke_publication_halt_is_locked_and_audited",
        ),
    ),
    RehearsalScenario(
        scenario_id="invite_and_suspension",
        name="邀请与停用",
        expected="邀请 secret 只返回一次且只存 hash，用户停用后访问和邮件资格立即失效。",
        pytest_node_ids=(
            "agent/tests/test_market_morning_beta_access.py::test_create_invite_returns_secret_once_but_persists_and_audits_hash_only",
            "agent/tests/test_market_morning_beta_access.py::test_revoke_invite_and_suspend_user_are_idempotent_and_audited",
        ),
    ),
    RehearsalScenario(
        scenario_id="export_and_deletion",
        name="导出与删除",
        expected="导出排除内部 secret，删除幂等清除私有数据并不可逆伪名化身份。",
        pytest_node_ids=(
            "agent/tests/test_market_morning_account_privacy.py::test_export_queries_cover_user_owned_data_without_operational_secrets",
            "agent/tests/test_market_morning_account_privacy.py::test_deletion_anonymizes_identity_and_removes_all_private_child_data",
            "agent/tests/test_market_morning_account_privacy.py::test_completed_deletion_is_idempotent_and_does_not_repeat_destructive_sql",
        ),
    ),
)


@dataclass(frozen=True, slots=True)
class ProbeOutcome:
    exit_code: int
    duration_ms: int
    output_sha256: str

    def __post_init__(self) -> None:
        if self.exit_code < 0:
            raise ValueError("exit_code must not be negative")
        if self.duration_ms < 0:
            raise ValueError("duration_ms must not be negative")
        if not _SHA256_PATTERN.fullmatch(self.output_sha256):
            raise ValueError("output_sha256 must be a lowercase SHA-256 digest")


@dataclass(frozen=True, slots=True)
class RehearsalScenarioResult:
    scenario_id: str
    name: str
    expected: str
    status: str
    pytest_node_ids: tuple[str, ...]
    exit_code: int | None
    duration_ms: int | None
    output_sha256: str | None
    failure_code: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "scenario_id": self.scenario_id,
            "name": self.name,
            "expected": self.expected,
            "status": self.status,
            "pytest_node_ids": list(self.pytest_node_ids),
            "exit_code": self.exit_code,
            "duration_ms": self.duration_ms,
            "output_sha256": self.output_sha256,
            "failure_code": self.failure_code,
        }


@dataclass(frozen=True, slots=True)
class RehearsalManifest:
    run_id: str
    environment: str
    started_at: str
    finished_at: str
    scenarios: tuple[RehearsalScenarioResult, ...]

    @property
    def status(self) -> str:
        return "passed" if all(result.status == "passed" for result in self.scenarios) else "failed"

    def to_dict(self) -> dict[str, Any]:
        summary = {"passed": 0, "failed": 0, "errors": 0, "total": len(self.scenarios)}
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
            "scope": "automated_local_synthetic",
            "environment": self.environment,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "status": self.status,
            "counts_as_staging_day": False,
            "summary": summary,
            "scenarios": [result.to_dict() for result in self.scenarios],
        }


ScenarioExecutor = Callable[[RehearsalScenario], ProbeOutcome]


def run_local_synthetic_rehearsal(
    executor: ScenarioExecutor,
    *,
    environment: str,
    run_id: str,
    started_at: str,
    finished_at: str,
) -> RehearsalManifest:
    """Run every required local probe, containing failures per scenario."""

    canonical_environment = environment.strip()
    canonical_run_id = run_id.strip()
    if not canonical_environment or len(canonical_environment) > 128:
        raise ValueError("environment must contain 1 to 128 characters")
    if not canonical_run_id or len(canonical_run_id) > 128:
        raise ValueError("run_id must contain 1 to 128 characters")

    results: list[RehearsalScenarioResult] = []
    for scenario in LOCAL_SYNTHETIC_SCENARIOS:
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
    return RehearsalManifest(
        run_id=canonical_run_id,
        environment=canonical_environment,
        started_at=started_at,
        finished_at=finished_at,
        scenarios=tuple(results),
    )


__all__ = [
    "LOCAL_SYNTHETIC_SCENARIOS",
    "ProbeOutcome",
    "RehearsalManifest",
    "RehearsalScenario",
    "RehearsalScenarioResult",
    "ScenarioExecutor",
    "run_local_synthetic_rehearsal",
]
