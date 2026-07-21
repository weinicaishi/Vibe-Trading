"""User-scoped morning-edition service boundary."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID
from zoneinfo import ZoneInfo

from src.config.accessor import get_env_config
from src.market_morning.db import get_session_factory
from src.market_morning.pipeline.morning_edition import (
    EditionDayInput,
    EditionEventInput,
    EditionIssuerInput,
    EditionSourceCitation,
    MorningEdition,
    build_edition_day_plan,
    generate_synthetic_edition,
)
from src.market_morning.repositories.morning_edition import (
    get_latest_morning_edition,
)
from src.market_morning.user_edition_service import (
    LazyUserEditionUnavailable,
    materialize_lazy_user_edition,
)

_TOKYO = ZoneInfo("Asia/Tokyo")
_FIXTURE_VERSION = "synthetic-v1"


@dataclass(frozen=True, slots=True)
class TodayEditionResult:
    status: str
    data_mode: str
    edition: MorningEdition | None
    edition_id: str | None = None
    reason_code: str | None = None
    fixture_version: str | None = None


def _canonical_user_id(user_id: str) -> str:
    try:
        return str(UUID(user_id))
    except (ValueError, AttributeError) as error:
        raise ValueError("user_id must be a UUID") from error


def _fixture_citation(
    *,
    provider: str,
    document_id: str,
    revision_key: str,
    published_at: datetime,
) -> EditionSourceCitation:
    return EditionSourceCitation(
        provider=provider,
        document_id=document_id,
        revision_key=revision_key,
        original_url=(
            f"https://example.invalid/market-morning/{provider}/"
            f"{document_id}/{revision_key}"
        ),
        published_at=published_at,
    )


def _fixture_issuers() -> tuple[EditionIssuerInput, ...]:
    first_published = datetime(2026, 7, 20, 6, 0, tzinfo=timezone.utc)
    correction_published = datetime(2026, 7, 20, 6, 30, tzinfo=timezone.utc)
    presentation_published = datetime(2026, 7, 20, 7, 0, tzinfo=timezone.utc)
    return (
        EditionIssuerInput(
            issuer_id="22222222-2222-4222-8222-222222222222",
            issuer_code="7203",
            legal_name_ja="トヨタ自動車株式会社",
            collection_status="complete",
            events=(
                EditionEventInput(
                    event_id="fixture-event-7203-earnings-v1",
                    event_family_key="fixture-family-7203-earnings",
                    event_version=1,
                    title="2026年3月期 決算短信",
                    event_type="earnings_release",
                    occurred_at=first_published,
                    lifecycle_status="active",
                    citations=(
                        _fixture_citation(
                            provider="fixture_tdnet",
                            document_id="TD-7203-001",
                            revision_key="v1",
                            published_at=first_published,
                        ),
                    ),
                ),
                EditionEventInput(
                    event_id="fixture-event-7203-earnings-v2",
                    event_family_key="fixture-family-7203-earnings",
                    event_version=2,
                    title="（訂正）2026年3月期 決算短信",
                    event_type="earnings_release",
                    occurred_at=correction_published,
                    lifecycle_status="corrected",
                    citations=(
                        _fixture_citation(
                            provider="fixture_tdnet",
                            document_id="TD-7203-001",
                            revision_key="v2",
                            published_at=correction_published,
                        ),
                    ),
                ),
                EditionEventInput(
                    event_id="fixture-event-7203-presentation-v1",
                    event_family_key="fixture-family-7203-presentation",
                    event_version=1,
                    title="決算説明会資料を公開",
                    event_type="investor_presentation",
                    occurred_at=presentation_published,
                    lifecycle_status="active",
                    citations=(
                        _fixture_citation(
                            provider="fixture_company_ir_toyota",
                            document_id="IR-7203-001",
                            revision_key="v1",
                            published_at=presentation_published,
                        ),
                    ),
                ),
            ),
        ),
        EditionIssuerInput(
            issuer_id="33333333-3333-4333-8333-333333333333",
            issuer_code="6758",
            legal_name_ja="ソニーグループ株式会社",
            collection_status="unavailable",
            events=(),
            error_code="synthetic_source_unavailable",
        ),
        EditionIssuerInput(
            issuer_id="44444444-4444-4444-8444-444444444444",
            issuer_code="9984",
            legal_name_ja="ソフトバンクグループ株式会社",
            collection_status="complete",
            events=(),
        ),
    )


def build_today_edition(
    *,
    user_id: str,
    now: datetime,
    synthetic_enabled: bool,
) -> TodayEditionResult:
    """Return fixture content only under an explicit, default-off gate."""
    _canonical_user_id(user_id)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    if not synthetic_enabled:
        return TodayEditionResult(
            status="not_published",
            data_mode="unavailable",
            edition=None,
            reason_code="edition_repository_not_configured",
        )

    edition_date = now.astimezone(_TOKYO).date()
    edition = generate_synthetic_edition(
        day_plan=build_edition_day_plan(
            EditionDayInput(
                edition_date=edition_date,
                jp_market_open=True,
                us_previous_session_available=True,
            )
        ),
        issuers=_fixture_issuers(),
        generated_at=now,
    )
    return TodayEditionResult(
        status=edition.status.value,
        data_mode="synthetic_fixture",
        edition=edition,
        fixture_version=_FIXTURE_VERSION,
    )


async def get_today_edition(
    *,
    user_id: str,
    now: datetime | None = None,
) -> TodayEditionResult:
    """Resolve demo or persisted content without silently crossing modes."""
    canonical_user_id = _canonical_user_id(user_id)
    resolved_now = now or datetime.now(timezone.utc)
    if resolved_now.tzinfo is None or resolved_now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    cfg = get_env_config().market_morning
    if cfg.synthetic_edition_enabled:
        return build_today_edition(
            user_id=canonical_user_id,
            now=resolved_now,
            synthetic_enabled=True,
        )
    if not cfg.database_url.strip():
        return build_today_edition(
            user_id=canonical_user_id,
            now=resolved_now,
            synthetic_enabled=False,
        )

    edition_date = resolved_now.astimezone(_TOKYO).date()
    factory = get_session_factory()
    async with factory() as session:
        stored = await get_latest_morning_edition(
            session,
            user_id=canonical_user_id,
            edition_date=edition_date,
        )
    if stored is None:
        try:
            stored = await materialize_lazy_user_edition(
                user_id=canonical_user_id,
                edition_date=edition_date,
                requested_at=resolved_now,
            )
        except LazyUserEditionUnavailable as error:
            return TodayEditionResult(
                status="not_published",
                data_mode="persisted",
                edition=None,
                reason_code=error.error_code,
            )
    return TodayEditionResult(
        status=stored.edition.status.value,
        data_mode="persisted",
        edition=stored.edition,
        edition_id=stored.edition_id,
    )


__all__ = ["TodayEditionResult", "build_today_edition", "get_today_edition"]
