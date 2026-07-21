"""Lazy, once-per-day materialization of a user's private morning edition."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.market_morning.calendar.service import CalendarScenario
from src.market_morning.db import get_session_factory
from src.market_morning.edition_generation import (
    EditionGenerationCommand,
    EditionGenerationWindow,
    generate_and_publish_morning_edition,
)
from src.market_morning.global_runs import global_run_manifest_sha256
from src.market_morning.models import GlobalEditionRunRecord, User
from src.market_morning.pipeline.morning_edition import (
    EditionDayInput,
    EditionDayPlan,
    build_edition_day_plan,
)
from src.market_morning.repositories.morning_edition import (
    EditionUserUnavailable,
    PublishedMorningEdition,
    get_latest_morning_edition,
)
from src.market_morning.source_coverage import SourceCoveragePolicy


class LazyUserEditionUnavailable(RuntimeError):
    def __init__(self, error_code: str) -> None:
        self.error_code = error_code
        super().__init__(error_code)


def _uuid(value: str, *, field_name: str) -> str:
    try:
        return str(UUID(value))
    except (ValueError, AttributeError) as error:
        raise ValueError(f"{field_name} must be a UUID") from error


def _aware_utc(value: datetime, *, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _required_aware_utc(value: datetime, *, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class LazyUserEditionContext:
    user_id: str
    global_run_id: str
    edition_date: date
    day_plan: EditionDayPlan
    global_run_completed_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "user_id",
            _uuid(self.user_id, field_name="user_id"),
        )
        object.__setattr__(
            self,
            "global_run_id",
            _uuid(self.global_run_id, field_name="global_run_id"),
        )
        if type(self.edition_date) is not date:
            raise ValueError("edition_date must be a date")
        if self.day_plan.edition_date != self.edition_date or not self.day_plan.generate:
            raise ValueError("day_plan must be publishable for edition_date")
        object.__setattr__(
            self,
            "global_run_completed_at",
            _aware_utc(
                self.global_run_completed_at,
                field_name="global_run_completed_at",
            ),
        )


def _day_plan_from_run(run: Any) -> EditionDayPlan:
    try:
        scenario = CalendarScenario(run.scenario)
    except (AttributeError, ValueError) as error:
        raise LazyUserEditionUnavailable("global_edition_invalid") from error
    if scenario not in {
        CalendarScenario.A_STANDARD,
        CalendarScenario.B_US_CLOSED,
    }:
        raise LazyUserEditionUnavailable("global_edition_invalid")
    us_available = scenario is CalendarScenario.A_STANDARD
    return build_edition_day_plan(
        EditionDayInput(
            edition_date=run.edition_date,
            jp_market_open=True,
            us_previous_session_available=us_available,
            us_closure_reason=(
                None
                if us_available
                else (run.reason_code or "us_market_closed")
            ),
        )
    )


async def load_lazy_user_edition_context(
    session: AsyncSession,
    *,
    user_id: str,
    edition_date: date,
) -> LazyUserEditionContext:
    """Lock the current global publication and active user for one snapshot."""

    canonical_user = _uuid(user_id, field_name="user_id")
    if type(edition_date) is not date:
        raise ValueError("edition_date must be a date")
    run = (
        await session.execute(
            select(GlobalEditionRunRecord)
            .where(
                GlobalEditionRunRecord.edition_date == edition_date,
                GlobalEditionRunRecord.is_current.is_(True),
                GlobalEditionRunRecord.status.in_(("complete", "partial", "late")),
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if run is None:
        raise LazyUserEditionUnavailable("global_edition_not_published")
    if (
        run.completed_at is None
        or not isinstance(run.manifest, dict)
        or not run.manifest_sha256
        or global_run_manifest_sha256(run.manifest) != run.manifest_sha256
    ):
        raise LazyUserEditionUnavailable("global_edition_invalid")

    user = (
        await session.execute(
            select(User)
            .where(User.user_id == canonical_user)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if user is None or user.deleted_at is not None or user.account_status != "active":
        raise EditionUserUnavailable("active Market Morning user is required")

    return LazyUserEditionContext(
        user_id=canonical_user,
        global_run_id=run.run_id,
        edition_date=edition_date,
        day_plan=_day_plan_from_run(run),
        global_run_completed_at=_aware_utc(
            run.completed_at,
            field_name="run.completed_at",
        ),
    )


async def materialize_lazy_user_edition(
    *,
    user_id: str,
    edition_date: date,
    requested_at: datetime,
    source_coverage_policy: SourceCoveragePolicy | None = None,
    session_factory: async_sessionmaker[AsyncSession] | Any | None = None,
) -> PublishedMorningEdition:
    """Return the existing daily snapshot or atomically create it once."""

    canonical_user = _uuid(user_id, field_name="user_id")
    requested = _required_aware_utc(requested_at, field_name="requested_at")
    factory = session_factory or get_session_factory()
    async with factory.begin() as session:
        context = await load_lazy_user_edition_context(
            session,
            user_id=canonical_user,
            edition_date=edition_date,
        )
        existing = await get_latest_morning_edition(
            session,
            user_id=canonical_user,
            edition_date=edition_date,
        )
        if existing is not None:
            return existing
        return await generate_and_publish_morning_edition(
            session,
            command=EditionGenerationCommand(
                user_id=canonical_user,
                generation_key=(
                    f"user-edition:{edition_date.isoformat()}:{canonical_user}"
                ),
                day_plan=context.day_plan,
                generated_at=context.global_run_completed_at,
                published_at=requested,
                window=EditionGenerationWindow.ending_at(
                    context.global_run_completed_at
                ),
                source_coverage_policy=source_coverage_policy,
            ),
        )


__all__ = [
    "LazyUserEditionContext",
    "LazyUserEditionUnavailable",
    "load_lazy_user_edition_context",
    "materialize_lazy_user_edition",
]
