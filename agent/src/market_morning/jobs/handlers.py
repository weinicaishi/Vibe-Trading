"""Production-safe handler factories for durable Market Morning jobs."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.market_morning.account_privacy import (
    AccountDeletionProcessResult,
    AccountPrivacyRetryable,
    AccountPrivacyUnavailable,
    process_account_deletion_request,
)
from src.market_morning.db import get_session_factory
from src.market_morning.edition_generation import (
    EditionGenerationCommand,
    EditionGenerationSourceRowsExceeded,
    EditionGenerationWindow,
    run_morning_edition_generation,
)
from src.market_morning.email_delivery_service import (
    EmailDeliveryCommand,
    EmailDeliveryResult,
    EmailDeliveryRetryable,
    EmailDeliveryUnavailable,
)
from src.market_morning.email_dispatch_service import (
    EmailDispatchCommand,
    EmailDispatchResult,
    EmailDispatchRetryable,
)
from src.market_morning.event_brief_generation import (
    EventBriefGenerationSpec,
    encode_event_brief_generation_spec,
)
from src.market_morning.event_brief_service import (
    EventBriefGenerationCommand,
    EventBriefGenerationResult,
    EventBriefGenerationRetryable,
    EventBriefGenerationUnavailable,
)
from src.market_morning.global_run_service import (
    GlobalEditionRunCommand,
    GlobalRunExecutionResult,
    run_global_edition_generation,
)
from src.market_morning.global_runs import GlobalEditionRunSpec
from src.market_morning.jobs.worker import JobHandler, PermanentJobError, RetryableJobError
from src.market_morning.market_snapshots import (
    MarketDataAdapter,
    MarketDataUnavailable,
    MarketInstrument,
    MarketSnapshot,
)
from src.market_morning.pipeline.morning_edition import (
    EditionDayPlan,
    EditionDayStatus,
    GenerationBudget,
    OvernightMarketContext,
)
from src.market_morning.pipeline.source_ingestion import (
    build_source_persistence_plan,
    collect_source_batch,
)
from src.market_morning.repositories.morning_edition import (
    EditionGenerationConflict,
    EditionPayloadInvalid,
    EditionUserUnavailable,
    PublishedMorningEdition,
)
from src.market_morning.repositories.market_snapshots import (
    MarketSnapshotConflict,
    persist_market_snapshot,
)
from src.market_morning.repositories.global_runs import GlobalRunConflict
from src.market_morning.repositories.event_briefs import EventBriefPersistenceConflict
from src.market_morning.repositories.email_delivery import DeliveryPersistenceConflict
from src.market_morning.repositories.jobs import MarketMorningJobType, enqueue_job
from src.market_morning.repositories.source_ingestion import (
    SourceApplyResult,
    SourceCursorConflict,
    SqlAlchemySourceBatchRepository,
    record_source_failure,
)
from src.market_morning.source_coverage import SourceCoveragePolicy
from src.market_morning.sources.base import SourceAdapter, SourceValidationError

logger = logging.getLogger(__name__)

RepositoryFactory = Callable[[AsyncSession], SqlAlchemySourceBatchRepository]
EditionRunner = Callable[[EditionGenerationCommand], Awaitable[PublishedMorningEdition]]
GlobalRunRunner = Callable[
    [GlobalEditionRunCommand], Awaitable[GlobalRunExecutionResult]
]
EventBriefRunner = Callable[
    [EventBriefGenerationCommand], Awaitable[EventBriefGenerationResult]
]
EmailDeliveryRunner = Callable[
    [EmailDeliveryCommand], Awaitable[EmailDeliveryResult]
]
EmailDispatchRunner = Callable[
    [EmailDispatchCommand], Awaitable[EmailDispatchResult]
]
AccountDeletionRunner = Callable[..., Awaitable[AccountDeletionProcessResult]]


@dataclass(frozen=True, slots=True)
class EventBriefDispatchConfig:
    model_version: str
    prompt_version: str
    max_attempts: int = 3
    priority: int = 20

    def __post_init__(self) -> None:
        for field_name in ("model_version", "prompt_version"):
            value = getattr(self, field_name).strip()
            if not value or len(value) > 64:
                raise ValueError(
                    f"{field_name} must contain 1 to 64 characters"
                )
            object.__setattr__(self, field_name, value)
        if not 1 <= self.max_attempts <= 10:
            raise ValueError("max_attempts must be between 1 and 10")
        if not -100 <= self.priority <= 100:
            raise ValueError("priority must be between -100 and 100")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _object(
    value: Any,
    *,
    field_name: str,
    required: frozenset[str],
    optional: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{field_name} must be an object")
    keys = frozenset(value)
    if not required <= keys or keys - required - optional:
        raise ValueError(f"{field_name} has invalid fields")
    return value


def _string(value: Any, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value.strip()


def _optional_string(value: Any, *, field_name: str) -> str | None:
    if value is None:
        return None
    return _string(value, field_name=field_name)


def _datetime(value: Any, *, field_name: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be an ISO-8601 string")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return parsed


def _positive_int(value: Any, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field_name} must be a positive integer")
    return value


def _decode_day_plan(value: Any) -> EditionDayPlan:
    payload = _object(
        value,
        field_name="day_plan",
        required=frozenset(
            {
                "edition_date",
                "generate",
                "status",
                "overnight_context",
                "reason_code",
                "us_reason_code",
            }
        ),
    )
    if type(payload["generate"]) is not bool:
        raise ValueError("day_plan.generate must be a boolean")
    plan = EditionDayPlan(
        edition_date=date.fromisoformat(_string(payload["edition_date"], field_name="edition_date")),
        generate=payload["generate"],
        status=EditionDayStatus(payload["status"]),
        overnight_context=OvernightMarketContext(payload["overnight_context"]),
        reason_code=_optional_string(payload["reason_code"], field_name="reason_code"),
        us_reason_code=_optional_string(payload["us_reason_code"], field_name="us_reason_code"),
    )
    if (plan.status is EditionDayStatus.SCHEDULED) != plan.generate:
        raise ValueError("day_plan status and generate disagree")
    return plan


def _decode_window(value: Any) -> EditionGenerationWindow:
    payload = _object(
        value,
        field_name="window",
        required=frozenset({"starts_at", "ends_at"}),
    )
    return EditionGenerationWindow(
        starts_at=_datetime(payload["starts_at"], field_name="window.starts_at"),
        ends_at=_datetime(payload["ends_at"], field_name="window.ends_at"),
    )


def _decode_budget(value: Any) -> GenerationBudget:
    payload = _object(
        value,
        field_name="budget",
        required=frozenset(
            {"max_issuers", "max_events_per_issuer", "max_total_events"}
        ),
    )
    return GenerationBudget(
        max_issuers=_positive_int(payload["max_issuers"], field_name="max_issuers"),
        max_events_per_issuer=_positive_int(
            payload["max_events_per_issuer"],
            field_name="max_events_per_issuer",
        ),
        max_total_events=_positive_int(
            payload["max_total_events"],
            field_name="max_total_events",
        ),
    )


def _provider_list(value: Any, *, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError(f"{field_name} must be an array")
    return tuple(_string(item, field_name=field_name) for item in value)


def _decode_coverage_policy(value: Any) -> SourceCoveragePolicy:
    payload = _object(
        value,
        field_name="source_coverage_policy",
        required=frozenset(
            {
                "global_required_providers",
                "issuer_required_providers",
                "max_staleness_seconds",
            }
        ),
    )
    issuer_payload = payload["issuer_required_providers"]
    if not isinstance(issuer_payload, dict):
        raise ValueError("issuer_required_providers must be an object")
    seconds = _positive_int(
        payload["max_staleness_seconds"],
        field_name="max_staleness_seconds",
    )
    return SourceCoveragePolicy(
        global_required_providers=_provider_list(
            payload["global_required_providers"],
            field_name="global_required_providers",
        ),
        issuer_required_providers={
            _string(code, field_name="issuer_code"): _provider_list(
                providers,
                field_name=f"issuer_required_providers.{code}",
            )
            for code, providers in issuer_payload.items()
        },
        max_staleness=timedelta(seconds=seconds),
    )


def decode_edition_generation_command(payload: dict[str, Any]) -> EditionGenerationCommand:
    root = _object(
        payload,
        field_name="payload",
        required=frozenset(
            {"user_id", "generation_key", "day_plan", "generated_at", "published_at"}
        ),
        optional=frozenset({"window", "budget", "source_coverage_policy"}),
    )
    return EditionGenerationCommand(
        user_id=_string(root["user_id"], field_name="user_id"),
        generation_key=_string(root["generation_key"], field_name="generation_key"),
        day_plan=_decode_day_plan(root["day_plan"]),
        generated_at=_datetime(root["generated_at"], field_name="generated_at"),
        published_at=_datetime(root["published_at"], field_name="published_at"),
        window=_decode_window(root["window"]) if "window" in root else None,
        budget=_decode_budget(root["budget"]) if "budget" in root else None,
        source_coverage_policy=(
            _decode_coverage_policy(root["source_coverage_policy"])
            if "source_coverage_policy" in root
            else None
        ),
    )


def decode_event_brief_generation_command(
    payload: dict[str, Any],
) -> EventBriefGenerationCommand:
    root = _object(
        payload,
        field_name="payload",
        required=frozenset(
            {
                "schema_version",
                "event_id",
                "event_version",
                "generation_key",
                "model_version",
                "prompt_version",
                "started_at",
                "max_attempts",
            }
        ),
    )
    schema_version = _positive_int(
        root["schema_version"], field_name="schema_version"
    )
    spec = EventBriefGenerationSpec(
        event_id=_string(root["event_id"], field_name="event_id"),
        event_version=_positive_int(
            root["event_version"], field_name="event_version"
        ),
        generation_key=_string(
            root["generation_key"], field_name="generation_key"
        ),
        model_version=_string(root["model_version"], field_name="model_version"),
        prompt_version=_string(
            root["prompt_version"], field_name="prompt_version"
        ),
        started_at=_datetime(root["started_at"], field_name="started_at"),
        max_attempts=_positive_int(
            root["max_attempts"], field_name="max_attempts"
        ),
        schema_version=schema_version,
    )
    return EventBriefGenerationCommand(spec=spec)


def make_event_brief_generation_handler(
    *,
    runner: EventBriefRunner,
    retry_delay: timedelta = timedelta(minutes=5),
) -> JobHandler:
    async def handle(payload: dict[str, Any]) -> dict[str, Any]:
        try:
            command = decode_event_brief_generation_command(payload)
        except (KeyError, TypeError, ValueError) as error:
            raise PermanentJobError("invalid_job_payload") from error
        try:
            result = await runner(command)
        except EventBriefGenerationRetryable as error:
            raise RetryableJobError(
                error.error_code,
                retry_delay=retry_delay,
            ) from None
        except EventBriefGenerationUnavailable as error:
            raise PermanentJobError("event_brief_unavailable") from error
        except EventBriefPersistenceConflict as error:
            raise PermanentJobError("event_brief_conflict") from error
        except SQLAlchemyError:
            raise RetryableJobError(
                "event_brief_database_unavailable",
                retry_delay=retry_delay,
            ) from None
        return {
            "brief_id": result.brief_id,
            "publish_status": result.status.value,
            "attempt_number": result.attempt_number,
            "payload_sha256": result.payload_sha256,
        }

    return handle


def decode_email_delivery_command(
    payload: dict[str, Any],
) -> EmailDeliveryCommand:
    root = _object(
        payload,
        field_name="payload",
        required=frozenset({"schema_version", "delivery_attempt_id"}),
    )
    if root["schema_version"] != 1:
        raise ValueError("schema_version is unsupported")
    return EmailDeliveryCommand(
        delivery_attempt_id=_string(
            root["delivery_attempt_id"],
            field_name="delivery_attempt_id",
        )
    )


def make_email_delivery_handler(
    *,
    runner: EmailDeliveryRunner,
    retry_delay: timedelta = timedelta(minutes=5),
) -> JobHandler:
    async def handle(payload: dict[str, Any]) -> dict[str, Any]:
        try:
            command = decode_email_delivery_command(payload)
        except (KeyError, TypeError, ValueError) as error:
            raise PermanentJobError("invalid_job_payload") from error
        try:
            result = await runner(command)
        except EmailDeliveryRetryable as error:
            raise RetryableJobError(
                error.error_code,
                retry_delay=retry_delay,
            ) from None
        except EmailDeliveryUnavailable as error:
            raise PermanentJobError(error.error_code) from None
        except DeliveryPersistenceConflict as error:
            raise PermanentJobError("email_delivery_conflict") from error
        except SQLAlchemyError:
            raise RetryableJobError(
                "email_delivery_database_unavailable",
                retry_delay=retry_delay,
            ) from None
        return {
            "delivery_attempt_id": result.delivery_attempt_id,
            "delivery_status": result.status.value,
            "attempt_number": result.attempt_number,
            "provider_message_id": result.provider_message_id,
            "reason_code": result.reason_code,
        }

    return handle


def _source_error_code(error: Exception) -> str:
    if isinstance(error, SourceCursorConflict):
        return "source_cursor_conflict"
    if isinstance(error, SourceValidationError):
        return "source_payload_invalid"
    if isinstance(error, TimeoutError):
        return "source_timeout"
    if isinstance(error, ConnectionError):
        return "source_unavailable"
    if isinstance(error, SQLAlchemyError):
        return "source_database_unavailable"
    return "source_ingestion_failed"


def make_source_ingestion_handler(
    *,
    adapters: Mapping[str, SourceAdapter],
    session_factory: async_sessionmaker[AsyncSession] | Any | None = None,
    repository_factory: RepositoryFactory = SqlAlchemySourceBatchRepository,
    event_brief_dispatch: EventBriefDispatchConfig | None = None,
    clock: Callable[[], datetime] = _utc_now,
    retry_delay: timedelta = timedelta(minutes=5),
) -> JobHandler:
    async def handle(payload: dict[str, Any]) -> dict[str, Any]:
        try:
            root = _object(
                payload,
                field_name="payload",
                required=frozenset({"provider"}),
            )
            provider = _string(root["provider"], field_name="provider")
        except (TypeError, ValueError) as error:
            raise PermanentJobError("invalid_job_payload") from error
        adapter = adapters.get(provider)
        if adapter is None:
            raise PermanentJobError("source_adapter_not_registered")
        if adapter.provider != provider:
            raise PermanentJobError("source_adapter_provider_mismatch")

        factory = session_factory or get_session_factory()
        try:
            async with factory.begin() as session:
                cursor = await repository_factory(session).load_cursor(provider)
            batch = await collect_source_batch(adapter, cursor)
            document_ids = tuple(
                dict.fromkeys(record.document_id for record in batch.records)
            )
            async with factory.begin() as session:
                repository = repository_factory(session)
                existing = await repository.load_revision_identities(
                    provider,
                    document_ids,
                )
                plan = build_source_persistence_plan(
                    batch,
                    existing_revisions=existing,
                )
                result: SourceApplyResult = await repository.apply(plan)
                dispatched_event_briefs = 0
                if event_brief_dispatch is not None:
                    dispatched_at = clock()
                    for event in result.created_event_revisions:
                        spec = EventBriefGenerationSpec(
                            event_id=event.event_id,
                            event_version=event.event_version,
                            generation_key=(
                                f"event-brief:{event.event_id}:v{event.event_version}:"
                                f"{event_brief_dispatch.model_version}:"
                                f"{event_brief_dispatch.prompt_version}:s1"
                            ),
                            model_version=event_brief_dispatch.model_version,
                            prompt_version=event_brief_dispatch.prompt_version,
                            started_at=dispatched_at,
                            max_attempts=event_brief_dispatch.max_attempts,
                        )
                        await enqueue_job(
                            session,
                            job_type=MarketMorningJobType.EVENT_BRIEF_GENERATION,
                            idempotency_key=spec.generation_key,
                            payload=encode_event_brief_generation_spec(spec),
                            priority=event_brief_dispatch.priority,
                            available_at=dispatched_at,
                            max_attempts=event_brief_dispatch.max_attempts,
                            created_at=dispatched_at,
                        )
                        dispatched_event_briefs += 1
        except Exception as error:
            error_code = _source_error_code(error)
            failed_at = clock()
            try:
                async with factory.begin() as session:
                    await record_source_failure(
                        session,
                        provider=provider,
                        failed_at=failed_at,
                        error_code=error_code,
                    )
            except Exception as record_error:
                logger.error(
                    "Market Morning source failure record failed: provider=%s exception_type=%s",
                    provider,
                    type(record_error).__name__,
                )
            raise RetryableJobError(error_code, retry_delay=retry_delay) from None

        return {
            "provider": provider,
            "inserted_records": result.inserted_records,
            "duplicate_records": result.duplicate_records,
            "created_events": result.created_events,
            "dispatched_event_briefs": dispatched_event_briefs,
            "unresolved_issuer_count": len(result.unresolved_issuer_codes),
        }

    return handle


def build_job_handlers(
    *,
    adapters: Mapping[str, SourceAdapter],
    market_data_adapters: Mapping[
        MarketInstrument | str, MarketDataAdapter
    ] | None = None,
    session_factory: async_sessionmaker[AsyncSession] | Any | None = None,
    edition_runner: EditionRunner = run_morning_edition_generation,
    global_run_runner: GlobalRunRunner | None = None,
    event_brief_runner: EventBriefRunner | None = None,
    event_brief_dispatch: EventBriefDispatchConfig | None = None,
    email_delivery_runner: EmailDeliveryRunner | None = None,
    email_dispatch_runner: EmailDispatchRunner | None = None,
    global_market_data_providers: Mapping[
        MarketInstrument | str, str
    ] | None = None,
    clock: Callable[[], datetime] = _utc_now,
) -> dict[MarketMorningJobType, JobHandler]:
    """Build only handlers with complete MVP execution paths."""
    if (event_brief_runner is None) != (event_brief_dispatch is None):
        raise ValueError(
            "event_brief_runner and event_brief_dispatch must be configured together"
        )
    if (email_delivery_runner is None) != (email_dispatch_runner is None):
        raise ValueError(
            "email_delivery_runner and email_dispatch_runner must be configured together"
        )
    handlers = {
        MarketMorningJobType.ACCOUNT_DELETION: make_account_deletion_handler(),
        MarketMorningJobType.SOURCE_INGESTION: make_source_ingestion_handler(
            adapters=adapters,
            session_factory=session_factory,
            event_brief_dispatch=event_brief_dispatch,
            clock=clock,
        ),
        MarketMorningJobType.EDITION_GENERATION: make_edition_generation_handler(
            runner=edition_runner,
        ),
    }
    if market_data_adapters:
        handlers[MarketMorningJobType.MARKET_SNAPSHOT] = (
            make_market_snapshot_handler(
                adapters=market_data_adapters,
                session_factory=session_factory,
                clock=clock,
            )
        )
    effective_global_runner = global_run_runner
    if effective_global_runner is None and global_market_data_providers:
        async def configured_global_runner(
            command: GlobalEditionRunCommand,
        ) -> GlobalRunExecutionResult:
            return await run_global_edition_generation(
                command,
                provider_by_instrument=global_market_data_providers,
                session_factory=session_factory,
                clock=clock,
            )

        effective_global_runner = configured_global_runner
    if effective_global_runner is not None:
        handlers[MarketMorningJobType.GLOBAL_EDITION_RUN] = (
            make_global_edition_run_handler(
                runner=effective_global_runner,
                email_dispatcher=email_dispatch_runner,
                clock=clock,
            )
        )
    if event_brief_runner is not None:
        handlers[MarketMorningJobType.EVENT_BRIEF_GENERATION] = (
            make_event_brief_generation_handler(runner=event_brief_runner)
        )
    if email_delivery_runner is not None:
        handlers[MarketMorningJobType.EMAIL_DELIVERY] = (
            make_email_delivery_handler(runner=email_delivery_runner)
        )
    return handlers


def make_account_deletion_handler(
    *,
    runner: AccountDeletionRunner = process_account_deletion_request,
    retry_delay: timedelta = timedelta(minutes=5),
) -> JobHandler:
    """Build the fail-closed worker for one idempotent deletion request."""

    async def handle(payload: dict[str, Any]) -> dict[str, Any]:
        try:
            root = _object(
                payload,
                field_name="payload",
                required=frozenset({"schema_version", "request_id"}),
            )
            if root["schema_version"] != 1:
                raise ValueError("schema_version is unsupported")
            from uuid import UUID

            request_id = str(
                UUID(_string(root["request_id"], field_name="request_id"))
            )
        except (KeyError, TypeError, ValueError) as error:
            raise PermanentJobError("invalid_job_payload") from error
        try:
            result = await runner(
                request_id=request_id,
                actor_reference="market-morning-deletion-worker",
            )
        except AccountPrivacyRetryable:
            raise RetryableJobError(
                "account_deletion_user_job_running",
                retry_delay=retry_delay,
            ) from None
        except AccountPrivacyUnavailable as error:
            raise PermanentJobError("account_deletion_unavailable") from error
        except SQLAlchemyError:
            raise RetryableJobError(
                "account_deletion_database_unavailable",
                retry_delay=retry_delay,
            ) from None
        return {
            "request_id": result.request_id,
            "user_id": result.user_id,
            "deletion_status": result.status.value,
            "completed_at": result.completed_at.isoformat(),
        }

    return handle


def _decode_expected_market_sessions(value: Any) -> dict[MarketInstrument, date]:
    if not isinstance(value, dict) or set(value) != {
        instrument.value for instrument in MarketInstrument
    }:
        raise ValueError("expected_market_sessions has invalid fields")
    result: dict[MarketInstrument, date] = {}
    for raw_instrument, raw_date in value.items():
        if not isinstance(raw_date, str):
            raise ValueError("expected market session must be a date string")
        result[MarketInstrument(raw_instrument)] = date.fromisoformat(raw_date)
    return result


def decode_global_edition_run_command(
    payload: dict[str, Any],
) -> GlobalEditionRunCommand:
    root = _object(
        payload,
        field_name="payload",
        required=frozenset(
            {
                "schema_version",
                "edition_date_jst",
                "attempt_key",
                "scheduled_at",
                "scenario",
                "email_permitted",
                "late",
                "us_reference_date",
                "us_last_valid_session_date",
                "next_jp_session_date",
                "reason_code",
                "expected_market_sessions",
                "day_plan",
            }
        ),
    )
    if root["schema_version"] != 1:
        raise ValueError("schema_version is unsupported")
    if type(root["email_permitted"]) is not bool or type(root["late"]) is not bool:
        raise ValueError("publication flags must be booleans")
    edition_date = date.fromisoformat(
        _string(root["edition_date_jst"], field_name="edition_date_jst")
    )
    attempt_key = _string(root["attempt_key"], field_name="attempt_key")
    scheduled_at = _datetime(root["scheduled_at"], field_name="scheduled_at")
    # Parse informational dates too so malformed scheduler payloads fail before work.
    date.fromisoformat(
        _string(root["us_reference_date"], field_name="us_reference_date")
    )
    if root["us_last_valid_session_date"] is not None:
        date.fromisoformat(
            _string(
                root["us_last_valid_session_date"],
                field_name="us_last_valid_session_date",
            )
        )
    if root["next_jp_session_date"] is not None:
        date.fromisoformat(
            _string(
                root["next_jp_session_date"],
                field_name="next_jp_session_date",
            )
        )
    day_plan = _object(
        root["day_plan"],
        field_name="day_plan",
        required=frozenset(
            {
                "edition_date",
                "generate",
                "status",
                "overnight_context",
                "reason_code",
                "us_reason_code",
            }
        ),
    )
    decoded_plan = _decode_day_plan(day_plan)
    if decoded_plan.edition_date != edition_date or not decoded_plan.generate:
        raise ValueError("day_plan is not publishable for edition date")
    spec = GlobalEditionRunSpec(
        edition_date=edition_date,
        generation_key=f"global-edition-run:{edition_date.isoformat()}:{attempt_key}",
        attempt_key=attempt_key,
        scenario=_string(root["scenario"], field_name="scenario"),
        email_permitted=root["email_permitted"],
        late=root["late"],
        reason_code=_optional_string(root["reason_code"], field_name="reason_code"),
        started_at=scheduled_at,
    )
    return GlobalEditionRunCommand(
        spec=spec,
        expected_market_sessions=_decode_expected_market_sessions(
            root["expected_market_sessions"]
        ),
        day_plan=dict(day_plan),
    )


def make_global_edition_run_handler(
    *,
    runner: GlobalRunRunner,
    email_dispatcher: EmailDispatchRunner | None = None,
    clock: Callable[[], datetime] = _utc_now,
    retry_delay: timedelta = timedelta(minutes=5),
) -> JobHandler:
    async def handle(payload: dict[str, Any]) -> dict[str, Any]:
        try:
            command = decode_global_edition_run_command(
                payload,
            )
        except (KeyError, TypeError, ValueError) as error:
            raise PermanentJobError("invalid_job_payload") from error
        try:
            result = await runner(command)
        except GlobalRunConflict as error:
            raise PermanentJobError("global_run_conflict") from error
        except SQLAlchemyError:
            raise RetryableJobError(
                "global_run_database_unavailable",
                retry_delay=retry_delay,
            ) from None
        dispatch_status = (
            "not_permitted"
            if not result.email_permitted
            else "not_configured"
        )
        enqueued_jobs = 0
        if result.email_permitted and email_dispatcher is not None:
            try:
                dispatched = await email_dispatcher(
                    EmailDispatchCommand(
                        global_run_id=result.run_id,
                        edition_date=command.spec.edition_date,
                        dispatched_at=clock(),
                    )
                )
            except EmailDispatchRetryable as error:
                raise RetryableJobError(
                    error.error_code,
                    retry_delay=retry_delay,
                ) from None
            except SQLAlchemyError:
                raise RetryableJobError(
                    "email_dispatch_database_unavailable",
                    retry_delay=retry_delay,
                ) from None
            dispatch_status = dispatched.dispatch_status
            enqueued_jobs = dispatched.enqueued_jobs
        return {
            "run_id": result.run_id,
            "run_version": result.run_version,
            "publication_status": result.publication_status,
            "publish_status": result.publish_status,
            "email_permitted": result.email_permitted,
            "email_dispatch_status": dispatch_status,
            "email_enqueued_jobs": enqueued_jobs,
        }

    return handle


def make_market_snapshot_handler(
    *,
    adapters: Mapping[MarketInstrument | str, MarketDataAdapter],
    session_factory: async_sessionmaker[AsyncSession] | Any | None = None,
    clock: Callable[[], datetime] = _utc_now,
    retry_delay: timedelta = timedelta(minutes=5),
) -> JobHandler:
    canonical_adapters = {
        MarketInstrument(instrument): adapter
        for instrument, adapter in adapters.items()
    }

    async def handle(payload: dict[str, Any]) -> dict[str, Any]:
        try:
            root = _object(
                payload,
                field_name="payload",
                required=frozenset(
                    {"schema_version", "instrument", "scheduled_at"}
                ),
            )
            if root["schema_version"] != 1:
                raise ValueError("schema_version is unsupported")
            instrument = MarketInstrument(
                _string(root["instrument"], field_name="instrument")
            )
            scheduled_at = _datetime(
                root["scheduled_at"],
                field_name="scheduled_at",
            )
        except (KeyError, TypeError, ValueError) as error:
            raise PermanentJobError("invalid_job_payload") from error

        adapter = canonical_adapters.get(instrument)
        if adapter is None:
            raise PermanentJobError("market_snapshot_adapter_not_registered")
        if adapter.instrument is not instrument:
            raise PermanentJobError("market_snapshot_adapter_contract")
        try:
            snapshot = await adapter.fetch(at=scheduled_at)
        except (MarketDataUnavailable, TimeoutError, ConnectionError):
            raise RetryableJobError(
                "market_snapshot_unavailable",
                retry_delay=retry_delay,
            ) from None
        except Exception:
            raise RetryableJobError(
                "market_snapshot_failed",
                retry_delay=retry_delay,
            ) from None

        if (
            not isinstance(snapshot, MarketSnapshot)
            or snapshot.instrument is not instrument
            or snapshot.provider != adapter.provider
        ):
            raise PermanentJobError("market_snapshot_adapter_contract")

        factory = session_factory or get_session_factory()
        try:
            async with factory.begin() as session:
                result = await persist_market_snapshot(
                    session,
                    snapshot=snapshot,
                    created_at=clock(),
                )
        except MarketSnapshotConflict as error:
            raise PermanentJobError("market_snapshot_conflict") from error
        except SQLAlchemyError:
            raise RetryableJobError(
                "market_snapshot_database_unavailable",
                retry_delay=retry_delay,
            ) from None

        return {
            "snapshot_id": result.snapshot_id,
            "instrument": snapshot.instrument.value,
            "provider": snapshot.provider,
            "write_status": result.status.value,
            "session_date": snapshot.session_date.isoformat(),
        }

    return handle


def make_edition_generation_handler(
    *,
    runner: EditionRunner = run_morning_edition_generation,
    retry_delay: timedelta = timedelta(minutes=5),
) -> JobHandler:
    async def handle(payload: dict[str, Any]) -> dict[str, Any]:
        try:
            command = decode_edition_generation_command(payload)
        except (KeyError, TypeError, ValueError) as error:
            raise PermanentJobError("invalid_job_payload") from error
        try:
            result = await runner(command)
        except EditionUserUnavailable as error:
            raise PermanentJobError("edition_user_unavailable") from error
        except EditionGenerationSourceRowsExceeded as error:
            raise PermanentJobError("edition_source_budget_exceeded") from error
        except EditionGenerationConflict as error:
            raise PermanentJobError("edition_generation_conflict") from error
        except EditionPayloadInvalid as error:
            raise PermanentJobError("edition_payload_invalid") from error
        except SQLAlchemyError:
            raise RetryableJobError(
                "edition_database_unavailable",
                retry_delay=retry_delay,
            ) from None
        return {
            "edition_id": result.edition_id,
            "edition_version": result.edition_version,
            "publish_status": result.status.value,
        }

    return handle


__all__ = [
    "EventBriefDispatchConfig",
    "build_job_handlers",
    "decode_edition_generation_command",
    "decode_event_brief_generation_command",
    "decode_email_delivery_command",
    "decode_global_edition_run_command",
    "make_edition_generation_handler",
    "make_account_deletion_handler",
    "make_event_brief_generation_handler",
    "make_email_delivery_handler",
    "make_global_edition_run_handler",
    "make_market_snapshot_handler",
    "make_source_ingestion_handler",
]
