"""Conservative provider-freshness assessment for issuer edition coverage."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.market_morning.models import SourceCursor
from src.market_morning.pipeline.morning_edition import IssuerCollectionStatus

DEFAULT_MAX_STALENESS = timedelta(minutes=30)
MAX_COVERAGE_PROVIDERS = 50
MAX_FUTURE_CLOCK_SKEW = timedelta(minutes=5)


class SourceProviderCoverageState(StrEnum):
    FRESH = "fresh"
    STALE = "stale"
    ERROR = "error"
    NEVER_RUN = "never_run"


def _aware(value: datetime, *, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value


def _utc_aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _provider(value: str) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > 64:
        raise ValueError("provider must contain 1 to 64 characters")
    return normalized


def _issuer_id(value: str) -> str:
    try:
        return str(UUID(value))
    except (ValueError, AttributeError) as error:
        raise ValueError("issuer_id must be a UUID") from error


def _issuer_code(value: str) -> str:
    normalized = value.strip().upper()
    if not normalized or len(normalized) > 12:
        raise ValueError("issuer_code must contain 1 to 12 characters")
    return normalized


def _provider_tuple(values: tuple[str, ...], *, field_name: str) -> tuple[str, ...]:
    normalized = tuple(dict.fromkeys(_provider(value) for value in values))
    if len(normalized) > MAX_COVERAGE_PROVIDERS:
        raise ValueError(f"{field_name} cannot exceed {MAX_COVERAGE_PROVIDERS} providers")
    return normalized


@dataclass(frozen=True, slots=True)
class CoverageIssuer:
    issuer_id: str
    issuer_code: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "issuer_id", _issuer_id(self.issuer_id))
        object.__setattr__(self, "issuer_code", _issuer_code(self.issuer_code))


@dataclass(frozen=True, slots=True)
class SourceCoveragePolicy:
    """Expected global sources plus optional issuer-specific IR feeds."""

    global_required_providers: tuple[str, ...]
    issuer_required_providers: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    max_staleness: timedelta = DEFAULT_MAX_STALENESS

    def __post_init__(self) -> None:
        global_providers = _provider_tuple(
            self.global_required_providers,
            field_name="global_required_providers",
        )
        if not global_providers:
            raise ValueError("global_required_providers must not be empty")
        if not timedelta(0) < self.max_staleness <= timedelta(hours=24):
            raise ValueError("max_staleness must be between 0 and 24 hours")
        issuer_providers = {
            _issuer_code(code): _provider_tuple(
                tuple(providers),
                field_name="issuer_required_providers",
            )
            for code, providers in self.issuer_required_providers.items()
        }
        object.__setattr__(self, "global_required_providers", global_providers)
        object.__setattr__(self, "issuer_required_providers", issuer_providers)

    def required_providers_for(self, issuer_code: str) -> tuple[str, ...]:
        specific = self.issuer_required_providers.get(_issuer_code(issuer_code), ())
        return tuple(dict.fromkeys((*self.global_required_providers, *specific)))


@dataclass(frozen=True, slots=True)
class SourceCursorSnapshot:
    provider: str
    last_successful_discovery_at: datetime | None
    last_error_at: datetime | None
    last_error_code: str | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "provider", _provider(self.provider))
        for field_name in ("last_successful_discovery_at", "last_error_at"):
            value = getattr(self, field_name)
            if value is not None:
                _aware(value, field_name=field_name)
        if self.last_error_code is not None:
            error_code = self.last_error_code.strip()
            if not error_code or len(error_code) > 64:
                raise ValueError("last_error_code must contain 1 to 64 characters")
            object.__setattr__(self, "last_error_code", error_code)


@dataclass(frozen=True, slots=True)
class SourceProviderCoverage:
    provider: str
    state: SourceProviderCoverageState
    last_successful_discovery_at: datetime | None
    last_error_at: datetime | None
    detail_code: str


@dataclass(frozen=True, slots=True)
class IssuerSourceCoverage:
    issuer_id: str
    issuer_code: str
    status: IssuerCollectionStatus
    error_code: str | None
    required_providers: tuple[str, ...]
    providers: tuple[SourceProviderCoverage, ...]


def evaluate_provider_coverage(
    snapshot: SourceCursorSnapshot | None,
    *,
    as_of: datetime,
    max_staleness: timedelta,
    provider: str | None = None,
) -> SourceProviderCoverage:
    """Classify the latest durable ingestion outcome for one provider."""
    as_of_utc = _aware(as_of, field_name="as_of").astimezone(timezone.utc)
    if not timedelta(0) < max_staleness <= timedelta(hours=24):
        raise ValueError("max_staleness must be between 0 and 24 hours")
    if snapshot is None:
        if provider is None:
            raise ValueError("provider is required when snapshot is missing")
        return SourceProviderCoverage(
            provider=_provider(provider),
            state=SourceProviderCoverageState.NEVER_RUN,
            last_successful_discovery_at=None,
            last_error_at=None,
            detail_code="source_never_run",
        )

    success = snapshot.last_successful_discovery_at
    error = snapshot.last_error_at
    if error is not None and (success is None or error >= success):
        return SourceProviderCoverage(
            provider=snapshot.provider,
            state=SourceProviderCoverageState.ERROR,
            last_successful_discovery_at=success,
            last_error_at=error,
            detail_code=snapshot.last_error_code or "source_error",
        )
    if success is None:
        return SourceProviderCoverage(
            provider=snapshot.provider,
            state=SourceProviderCoverageState.NEVER_RUN,
            last_successful_discovery_at=None,
            last_error_at=error,
            detail_code="source_never_run",
        )
    if success > as_of_utc + MAX_FUTURE_CLOCK_SKEW:
        return SourceProviderCoverage(
            provider=snapshot.provider,
            state=SourceProviderCoverageState.ERROR,
            last_successful_discovery_at=success,
            last_error_at=error,
            detail_code="source_clock_skew",
        )
    if success < as_of_utc - max_staleness:
        state = SourceProviderCoverageState.STALE
        detail_code = "source_stale"
    else:
        state = SourceProviderCoverageState.FRESH
        detail_code = "source_fresh"
    return SourceProviderCoverage(
        provider=snapshot.provider,
        state=state,
        last_successful_discovery_at=success,
        last_error_at=error,
        detail_code=detail_code,
    )


def assess_issuer_source_coverage(
    issuer: CoverageIssuer,
    *,
    snapshots: Mapping[str, SourceCursorSnapshot],
    policy: SourceCoveragePolicy,
    as_of: datetime,
) -> IssuerSourceCoverage:
    required = policy.required_providers_for(issuer.issuer_code)
    provider_results = tuple(
        evaluate_provider_coverage(
            snapshots.get(provider),
            provider=provider,
            as_of=as_of,
            max_staleness=policy.max_staleness,
        )
        for provider in required
    )
    fresh_count = sum(item.state is SourceProviderCoverageState.FRESH for item in provider_results)
    if fresh_count == len(provider_results):
        status = IssuerCollectionStatus.COMPLETE
        error_code = None
    elif fresh_count:
        status = IssuerCollectionStatus.PARTIAL
        error_code = "source_coverage_incomplete"
    else:
        status = IssuerCollectionStatus.UNAVAILABLE
        error_code = "source_coverage_unavailable"
    return IssuerSourceCoverage(
        issuer_id=issuer.issuer_id,
        issuer_code=issuer.issuer_code,
        status=status,
        error_code=error_code,
        required_providers=required,
        providers=provider_results,
    )


def _required_provider_names(
    issuers: tuple[CoverageIssuer, ...],
    policy: SourceCoveragePolicy,
) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(provider for issuer in issuers for provider in policy.required_providers_for(issuer.issuer_code))
    )


def build_source_coverage_statement(providers: tuple[str, ...]):
    canonical = _provider_tuple(providers, field_name="providers")
    if not canonical:
        raise ValueError("providers must not be empty")
    return select(
        SourceCursor.source_provider.label("provider"),
        SourceCursor.last_successful_discovery_at,
        SourceCursor.last_error_at,
        SourceCursor.last_error_code,
    ).where(SourceCursor.source_provider.in_(canonical))


async def load_issuer_source_coverages(
    session: AsyncSession,
    *,
    issuers: tuple[CoverageIssuer, ...],
    policy: SourceCoveragePolicy,
    as_of: datetime,
) -> dict[str, IssuerSourceCoverage]:
    if not issuers:
        return {}
    provider_names = _required_provider_names(issuers, policy)
    rows = (await session.execute(build_source_coverage_statement(provider_names))).mappings().all()
    snapshots = {
        row["provider"]: SourceCursorSnapshot(
            provider=row["provider"],
            last_successful_discovery_at=_utc_aware(row["last_successful_discovery_at"]),
            last_error_at=_utc_aware(row["last_error_at"]),
            last_error_code=row["last_error_code"],
        )
        for row in rows
    }
    return {
        issuer.issuer_id: assess_issuer_source_coverage(
            issuer,
            snapshots=snapshots,
            policy=policy,
            as_of=as_of,
        )
        for issuer in issuers
    }


__all__ = [
    "DEFAULT_MAX_STALENESS",
    "MAX_COVERAGE_PROVIDERS",
    "MAX_FUTURE_CLOCK_SKEW",
    "CoverageIssuer",
    "IssuerSourceCoverage",
    "SourceCoveragePolicy",
    "SourceCursorSnapshot",
    "SourceProviderCoverage",
    "SourceProviderCoverageState",
    "assess_issuer_source_coverage",
    "build_source_coverage_statement",
    "evaluate_provider_coverage",
    "load_issuer_source_coverages",
]
