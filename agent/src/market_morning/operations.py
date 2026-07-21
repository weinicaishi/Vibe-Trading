"""Privacy-safe operational read model for the Market Morning control plane."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.market_morning.db import get_session_factory
from src.market_morning.models import (
    AnalyticsEvent,
    DeliveryAttemptRecord,
    EventBriefRecord,
    GlobalEditionRunRecord,
    JobRecord,
    ManualOverrideRecord,
    ModelUsageEventRecord,
)
from src.market_morning.source_queries import query_source_health

CostObservabilityStatus = Literal[
    "no_invocations",
    "usage_incomplete",
    "cost_incomplete",
    "complete",
]
AlertSeverity = Literal["warning", "critical"]


@dataclass(frozen=True, slots=True)
class OperationsSourceHealthView:
    provider: str
    status: str
    last_successful_discovery_at: datetime | None
    last_error_at: datetime | None
    last_error_code: str | None


@dataclass(frozen=True, slots=True)
class OperationsGlobalRunView:
    run_id: str
    edition_date: date
    run_version: int
    attempt_key: str
    scenario: str
    status: str
    is_current: bool
    email_permitted: bool
    late: bool
    reason_code: str | None
    started_at: datetime
    completed_at: datetime | None


@dataclass(frozen=True, slots=True)
class OperationsEventBriefCountView:
    status: str
    review_status: str
    count: int


@dataclass(frozen=True, slots=True)
class OperationsHaltView:
    edition_date: date
    reason_code: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class OperationsModelUsageView:
    invocation_count: int
    usage_missing_count: int
    unpriced_count: int
    input_tokens: int
    output_tokens: int
    costs_by_currency: dict[str, int]


@dataclass(frozen=True, slots=True)
class OperationsAlertView:
    code: str
    severity: AlertSeverity
    count: int


@dataclass(frozen=True, slots=True)
class OperationsSummaryView:
    generated_at: datetime
    window_started_at: datetime
    cost_observability: CostObservabilityStatus
    event_brief_generation_attempt_count: int
    model_usage: OperationsModelUsageView
    sources: tuple[OperationsSourceHealthView, ...]
    job_counts: dict[str, int]
    job_failure_counts: dict[str, int]
    global_runs: tuple[OperationsGlobalRunView, ...]
    event_brief_counts: tuple[OperationsEventBriefCountView, ...]
    delivery_counts: dict[str, int]
    engagement_counts: dict[str, int]
    active_halts: tuple[OperationsHaltView, ...]
    alerts: tuple[OperationsAlertView, ...]


def _mysql_utc(value: datetime, *, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _utc_aware(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=timezone.utc)


def _bounded(value: int, *, field_name: str, maximum: int) -> int:
    if not 1 <= value <= maximum:
        raise ValueError(f"{field_name} must be between 1 and {maximum}")
    return value


def build_job_status_counts_statement(*, since: datetime):
    start = _mysql_utc(since, field_name="since")
    return (
        select(JobRecord.status.label("key"), func.count().label("count"))
        .where(JobRecord.created_at >= start)
        .group_by(JobRecord.status)
        .order_by(JobRecord.status)
    )


def build_job_failure_counts_statement(*, since: datetime):
    start = _mysql_utc(since, field_name="since")
    return (
        select(JobRecord.last_error_code.label("key"), func.count().label("count"))
        .where(
            JobRecord.created_at >= start,
            JobRecord.status == "failed",
            JobRecord.last_error_code.is_not(None),
        )
        .group_by(JobRecord.last_error_code)
        .order_by(func.count().desc(), JobRecord.last_error_code)
    )


def build_recent_global_runs_statement(*, since: datetime, limit: int = 10):
    start = _mysql_utc(since, field_name="since")
    bounded = _bounded(limit, field_name="limit", maximum=30)
    return (
        select(
            GlobalEditionRunRecord.run_id,
            GlobalEditionRunRecord.edition_date,
            GlobalEditionRunRecord.run_version,
            GlobalEditionRunRecord.attempt_key,
            GlobalEditionRunRecord.scenario,
            GlobalEditionRunRecord.status,
            GlobalEditionRunRecord.is_current,
            GlobalEditionRunRecord.email_permitted,
            GlobalEditionRunRecord.late,
            GlobalEditionRunRecord.reason_code,
            GlobalEditionRunRecord.started_at,
            GlobalEditionRunRecord.completed_at,
        )
        .where(GlobalEditionRunRecord.started_at >= start)
        .order_by(
            GlobalEditionRunRecord.edition_date.desc(),
            GlobalEditionRunRecord.run_version.desc(),
        )
        .limit(bounded)
    )


def build_event_brief_counts_statement(*, since: datetime):
    start = _mysql_utc(since, field_name="since")
    return (
        select(
            EventBriefRecord.status,
            EventBriefRecord.review_status,
            func.count().label("count"),
        )
        .where(EventBriefRecord.created_at >= start)
        .group_by(EventBriefRecord.status, EventBriefRecord.review_status)
        .order_by(EventBriefRecord.status, EventBriefRecord.review_status)
    )


def build_event_brief_attempt_count_statement(*, since: datetime):
    start = _mysql_utc(since, field_name="since")
    return select(func.coalesce(func.sum(EventBriefRecord.attempt_count), 0)).where(
        EventBriefRecord.created_at >= start
    )


def build_model_usage_summary_statement(*, since: datetime):
    start = _mysql_utc(since, field_name="since")
    return select(
        func.count().label("invocation_count"),
        func.coalesce(
            func.sum(
                func.if_(ModelUsageEventRecord.usage_status == "missing", 1, 0)
            ),
            0,
        ).label("usage_missing_count"),
        func.coalesce(
            func.sum(
                func.if_(ModelUsageEventRecord.cost_status == "unpriced", 1, 0)
            ),
            0,
        ).label("unpriced_count"),
        func.coalesce(func.sum(ModelUsageEventRecord.input_tokens), 0).label(
            "input_tokens"
        ),
        func.coalesce(func.sum(ModelUsageEventRecord.output_tokens), 0).label(
            "output_tokens"
        ),
    ).where(ModelUsageEventRecord.occurred_at >= start)


def build_model_usage_costs_statement(*, since: datetime):
    start = _mysql_utc(since, field_name="since")
    return (
        select(
            ModelUsageEventRecord.currency,
            func.sum(ModelUsageEventRecord.billable_cost_micros).label(
                "cost_micros"
            ),
        )
        .where(
            ModelUsageEventRecord.occurred_at >= start,
            ModelUsageEventRecord.cost_status == "reported",
            ModelUsageEventRecord.currency.is_not(None),
        )
        .group_by(ModelUsageEventRecord.currency)
        .order_by(ModelUsageEventRecord.currency)
    )


def determine_cost_observability(
    *,
    invocation_count: int,
    usage_missing_count: int,
    unpriced_count: int,
) -> CostObservabilityStatus:
    if min(invocation_count, usage_missing_count, unpriced_count) < 0:
        raise ValueError("model usage counts must be non-negative")
    if usage_missing_count > invocation_count or unpriced_count > invocation_count:
        raise ValueError("incomplete counts cannot exceed invocation_count")
    if invocation_count == 0:
        return "no_invocations"
    if usage_missing_count:
        return "usage_incomplete"
    if unpriced_count:
        return "cost_incomplete"
    return "complete"


def build_delivery_counts_statement(*, since: datetime):
    start = _mysql_utc(since, field_name="since")
    return (
        select(
            DeliveryAttemptRecord.status.label("key"),
            func.count().label("count"),
        )
        .where(DeliveryAttemptRecord.requested_at >= start)
        .group_by(DeliveryAttemptRecord.status)
        .order_by(DeliveryAttemptRecord.status)
    )


def build_engagement_counts_statement(*, since: datetime):
    start = _mysql_utc(since, field_name="since")
    return (
        select(
            AnalyticsEvent.event_name.label("key"),
            func.count().label("count"),
        )
        .where(
            AnalyticsEvent.occurred_at >= start,
            AnalyticsEvent.event_name.in_(
                ("edition_opened", "source_opened", "delivery_clicked")
            ),
        )
        .group_by(AnalyticsEvent.event_name)
        .order_by(AnalyticsEvent.event_name)
    )


def build_active_halts_statement(*, limit: int = 10):
    bounded = _bounded(limit, field_name="limit", maximum=30)
    return (
        select(
            ManualOverrideRecord.edition_date,
            ManualOverrideRecord.reason_code,
            ManualOverrideRecord.created_at,
        )
        .where(
            ManualOverrideRecord.override_type == "publication_halt",
            ManualOverrideRecord.status == "active",
        )
        .order_by(ManualOverrideRecord.edition_date.desc())
        .limit(bounded)
    )


def _counts(rows) -> dict[str, int]:
    return {str(row["key"]): int(row["count"]) for row in rows}


def _alerts(
    *,
    sources: tuple[OperationsSourceHealthView, ...],
    jobs: dict[str, int],
    briefs: tuple[OperationsEventBriefCountView, ...],
    deliveries: dict[str, int],
    model_usage: OperationsModelUsageView,
) -> tuple[OperationsAlertView, ...]:
    alerts: list[OperationsAlertView] = []
    source_errors = sum(source.status == "error" for source in sources)
    if source_errors:
        alerts.append(OperationsAlertView("source_error", "critical", source_errors))
    never_run = sum(source.status == "never_run" for source in sources)
    if never_run:
        alerts.append(OperationsAlertView("source_never_run", "warning", never_run))
    if jobs.get("failed", 0):
        alerts.append(
            OperationsAlertView("job_failures", "critical", jobs["failed"])
        )
    blocked = sum(item.count for item in briefs if item.status == "blocked")
    if blocked:
        alerts.append(OperationsAlertView("event_brief_blocked", "warning", blocked))
    if deliveries.get("failed", 0):
        alerts.append(
            OperationsAlertView(
                "delivery_failures", "warning", deliveries["failed"]
            )
        )
    if model_usage.invocation_count == 0:
        alerts.append(
            OperationsAlertView("cost_observability_no_invocations", "warning", 1)
        )
    if model_usage.usage_missing_count:
        alerts.append(
            OperationsAlertView(
                "model_usage_missing",
                "warning",
                model_usage.usage_missing_count,
            )
        )
    if model_usage.unpriced_count:
        alerts.append(
            OperationsAlertView(
                "model_cost_missing",
                "warning",
                model_usage.unpriced_count,
            )
        )
    return tuple(alerts)


def _openmetrics_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _openmetrics_count(value: int) -> int:
    count = int(value)
    if count < 0:
        raise ValueError("count must be non-negative")
    return count


def _openmetrics_family(
    lines: list[str],
    *,
    name: str,
    help_text: str,
    samples: list[tuple[tuple[tuple[str, str], ...], int]],
) -> None:
    lines.extend((f"# HELP {name} {help_text}", f"# TYPE {name} gauge"))
    for labels, raw_value in samples:
        value = _openmetrics_count(raw_value)
        rendered_labels = ""
        if labels:
            rendered_labels = "{" + ",".join(
                f'{key}="{_openmetrics_label(label_value)}"'
                for key, label_value in labels
            ) + "}"
        lines.append(f"{name}{rendered_labels} {value}")


def render_operations_openmetrics(summary: OperationsSummaryView) -> str:
    """Render a bounded, aggregate-only OpenMetrics 1.0 snapshot.

    Deliberately excludes run IDs, edition dates, user identifiers, URLs,
    provider request IDs, payloads and halt reasons. Windowed values are gauges,
    not counters, because they can decrease as observations age out.
    """

    for field_name, value in (
        ("generated_at", summary.generated_at),
        ("window_started_at", summary.window_started_at),
    ):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"{field_name} must be timezone-aware")
    window_seconds = int(
        (summary.generated_at - summary.window_started_at).total_seconds()
    )
    if window_seconds < 0:
        raise ValueError("window_started_at must not be after generated_at")

    health = 0
    if any(alert.severity == "critical" for alert in summary.alerts):
        health = 2
    elif summary.alerts:
        health = 1

    lines: list[str] = []
    _openmetrics_family(
        lines,
        name="market_morning_operations_health",
        help_text="Aggregate health where 0 is healthy, 1 warning and 2 critical.",
        samples=[((), health)],
    )
    _openmetrics_family(
        lines,
        name="market_morning_operations_snapshot_timestamp_seconds",
        help_text="UTC generation time of this operational snapshot.",
        samples=[((), int(summary.generated_at.timestamp()))],
    )
    _openmetrics_family(
        lines,
        name="market_morning_operations_window_seconds",
        help_text="Observation window represented by windowed aggregate gauges.",
        samples=[((), window_seconds)],
    )
    _openmetrics_family(
        lines,
        name="market_morning_operations_alert_count",
        help_text="Active aggregate alerts by stable code and severity.",
        samples=[
            (
                (("code", alert.code), ("severity", alert.severity)),
                alert.count,
            )
            for alert in sorted(
                summary.alerts,
                key=lambda item: (item.code, item.severity),
            )
        ],
    )
    _openmetrics_family(
        lines,
        name="market_morning_source_health",
        help_text="Configured source health as one labelled gauge per provider.",
        samples=[
            (
                (("provider", source.provider), ("status", source.status)),
                1,
            )
            for source in sorted(
                summary.sources,
                key=lambda item: (item.provider, item.status),
            )
        ],
    )

    labelled_counts = (
        (
            "market_morning_job_count",
            "Windowed durable jobs by status.",
            "status",
            summary.job_counts,
        ),
        (
            "market_morning_job_failure_count",
            "Windowed failed durable jobs by sanitized failure code.",
            "code",
            summary.job_failure_counts,
        ),
        (
            "market_morning_delivery_count",
            "Windowed email delivery attempts by status.",
            "status",
            summary.delivery_counts,
        ),
        (
            "market_morning_engagement_count",
            "Windowed privacy-safe engagement events by event type.",
            "event",
            summary.engagement_counts,
        ),
    )
    for name, help_text, label_name, values in labelled_counts:
        _openmetrics_family(
            lines,
            name=name,
            help_text=help_text,
            samples=[
                (((label_name, key),), value)
                for key, value in sorted(values.items())
            ],
        )

    recent_runs: dict[str, int] = {}
    for run in summary.global_runs:
        recent_runs[run.status] = recent_runs.get(run.status, 0) + 1
    _openmetrics_family(
        lines,
        name="market_morning_recent_global_run_count",
        help_text="Bounded recent global edition runs grouped by status.",
        samples=[
            ((("status", key),), value)
            for key, value in sorted(recent_runs.items())
        ],
    )
    _openmetrics_family(
        lines,
        name="market_morning_event_brief_count",
        help_text="Windowed EventBriefs by generation and review status.",
        samples=[
            (
                (
                    ("status", brief.status),
                    ("review_status", brief.review_status),
                ),
                brief.count,
            )
            for brief in sorted(
                summary.event_brief_counts,
                key=lambda item: (item.status, item.review_status),
            )
        ],
    )
    _openmetrics_family(
        lines,
        name="market_morning_event_brief_generation_attempt_count",
        help_text="Windowed EventBrief generation attempts.",
        samples=[((), summary.event_brief_generation_attempt_count)],
    )
    _openmetrics_family(
        lines,
        name="market_morning_model_invocation_count",
        help_text="Windowed provider-reported model invocations.",
        samples=[((), summary.model_usage.invocation_count)],
    )
    _openmetrics_family(
        lines,
        name="market_morning_model_usage_missing_count",
        help_text="Windowed model invocations missing provider token usage.",
        samples=[((), summary.model_usage.usage_missing_count)],
    )
    _openmetrics_family(
        lines,
        name="market_morning_model_unpriced_count",
        help_text="Windowed model invocations missing provider cost.",
        samples=[((), summary.model_usage.unpriced_count)],
    )
    _openmetrics_family(
        lines,
        name="market_morning_model_input_tokens",
        help_text="Windowed provider-reported model input tokens.",
        samples=[((), summary.model_usage.input_tokens)],
    )
    _openmetrics_family(
        lines,
        name="market_morning_model_output_tokens",
        help_text="Windowed provider-reported model output tokens.",
        samples=[((), summary.model_usage.output_tokens)],
    )
    _openmetrics_family(
        lines,
        name="market_morning_model_cost_micros",
        help_text="Windowed provider-reported model cost in currency millionths.",
        samples=[
            ((("currency", currency),), value)
            for currency, value in sorted(
                summary.model_usage.costs_by_currency.items()
            )
        ],
    )
    _openmetrics_family(
        lines,
        name="market_morning_model_cost_observability",
        help_text="Current model cost observability state as a one-hot gauge.",
        samples=[((("status", summary.cost_observability),), 1)],
    )
    _openmetrics_family(
        lines,
        name="market_morning_active_publication_halt_count",
        help_text="Current active publication halt count.",
        samples=[((), len(summary.active_halts))],
    )
    lines.append("# EOF")
    return "\n".join(lines) + "\n"


async def load_operations_summary(
    session: AsyncSession,
    *,
    now: datetime,
    window: timedelta = timedelta(hours=24),
    recent_run_limit: int = 10,
) -> OperationsSummaryView:
    if window <= timedelta(0) or window > timedelta(days=7):
        raise ValueError("window must be between 1 second and 7 days")
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    generated_at = now.astimezone(timezone.utc)
    since = generated_at - window

    source_values = await query_source_health(session, limit=100)
    sources = tuple(
        OperationsSourceHealthView(
            provider=value.provider,
            status=value.status,
            last_successful_discovery_at=value.last_successful_discovery_at,
            last_error_at=value.last_error_at,
            last_error_code=value.last_error_code,
        )
        for value in source_values
    )
    jobs = _counts(
        (
            await session.execute(build_job_status_counts_statement(since=since))
        ).mappings().all()
    )
    job_failures = _counts(
        (
            await session.execute(build_job_failure_counts_statement(since=since))
        ).mappings().all()
    )
    global_run_rows = (
        await session.execute(
            build_recent_global_runs_statement(
                since=since,
                limit=recent_run_limit,
            )
        )
    ).mappings().all()
    global_runs = tuple(
        OperationsGlobalRunView(
            run_id=row["run_id"],
            edition_date=row["edition_date"],
            run_version=row["run_version"],
            attempt_key=row["attempt_key"],
            scenario=row["scenario"],
            status=row["status"],
            is_current=bool(row["is_current"]),
            email_permitted=bool(row["email_permitted"]),
            late=bool(row["late"]),
            reason_code=row["reason_code"],
            started_at=_utc_aware(row["started_at"]),
            completed_at=_utc_aware(row["completed_at"]),
        )
        for row in global_run_rows
    )
    brief_rows = (
        await session.execute(build_event_brief_counts_statement(since=since))
    ).mappings().all()
    briefs = tuple(
        OperationsEventBriefCountView(
            status=row["status"],
            review_status=row["review_status"],
            count=int(row["count"]),
        )
        for row in brief_rows
    )
    attempts = int(
        (
            await session.execute(
                build_event_brief_attempt_count_statement(since=since)
            )
        ).scalar_one()
    )
    usage_row = (
        await session.execute(build_model_usage_summary_statement(since=since))
    ).mappings().one()
    cost_rows = (
        await session.execute(build_model_usage_costs_statement(since=since))
    ).mappings().all()
    model_usage = OperationsModelUsageView(
        invocation_count=int(usage_row["invocation_count"] or 0),
        usage_missing_count=int(usage_row["usage_missing_count"] or 0),
        unpriced_count=int(usage_row["unpriced_count"] or 0),
        input_tokens=int(usage_row["input_tokens"] or 0),
        output_tokens=int(usage_row["output_tokens"] or 0),
        costs_by_currency={
            str(row["currency"]): int(row["cost_micros"])
            for row in cost_rows
        },
    )
    deliveries = _counts(
        (
            await session.execute(build_delivery_counts_statement(since=since))
        ).mappings().all()
    )
    engagement = _counts(
        (
            await session.execute(build_engagement_counts_statement(since=since))
        ).mappings().all()
    )
    halt_rows = (
        await session.execute(build_active_halts_statement(limit=10))
    ).mappings().all()
    halts = tuple(
        OperationsHaltView(
            edition_date=row["edition_date"],
            reason_code=row["reason_code"],
            created_at=_utc_aware(row["created_at"]),
        )
        for row in halt_rows
    )
    return OperationsSummaryView(
        generated_at=generated_at,
        window_started_at=since,
        cost_observability=determine_cost_observability(
            invocation_count=model_usage.invocation_count,
            usage_missing_count=model_usage.usage_missing_count,
            unpriced_count=model_usage.unpriced_count,
        ),
        event_brief_generation_attempt_count=attempts,
        model_usage=model_usage,
        sources=sources,
        job_counts=jobs,
        job_failure_counts=job_failures,
        global_runs=global_runs,
        event_brief_counts=briefs,
        delivery_counts=deliveries,
        engagement_counts=engagement,
        active_halts=halts,
        alerts=_alerts(
            sources=sources,
            jobs=jobs,
            briefs=briefs,
            deliveries=deliveries,
            model_usage=model_usage,
        ),
    )


async def get_operations_summary(
    *, hours: int = 24, recent_run_limit: int = 10
) -> OperationsSummaryView:
    bounded_hours = _bounded(hours, field_name="hours", maximum=168)
    factory = get_session_factory()
    async with factory() as session:
        return await load_operations_summary(
            session,
            now=datetime.now(timezone.utc),
            window=timedelta(hours=bounded_hours),
            recent_run_limit=recent_run_limit,
        )


__all__ = [
    "OperationsAlertView",
    "OperationsEventBriefCountView",
    "OperationsGlobalRunView",
    "OperationsHaltView",
    "OperationsModelUsageView",
    "OperationsSourceHealthView",
    "OperationsSummaryView",
    "build_active_halts_statement",
    "build_delivery_counts_statement",
    "build_engagement_counts_statement",
    "build_event_brief_attempt_count_statement",
    "build_event_brief_counts_statement",
    "build_job_failure_counts_statement",
    "build_job_status_counts_statement",
    "build_model_usage_costs_statement",
    "build_model_usage_summary_statement",
    "build_recent_global_runs_statement",
    "determine_cost_observability",
    "get_operations_summary",
    "load_operations_summary",
    "render_operations_openmetrics",
]
