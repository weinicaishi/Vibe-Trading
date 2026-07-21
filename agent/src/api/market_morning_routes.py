"""Internal foundation routes for the opt-in Market Morning product."""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Any, Literal, Never

from fastapi import Depends, FastAPI, HTTPException, Path, Query, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from src.api.market_morning_auth import (
    MarketMorningOnboardingPrincipal,
    MarketMorningPrincipal,
    require_account_deletion_principal,
    require_market_morning_onboarding_principal,
    require_market_morning_principal,
)
from src.api.market_morning_admin_auth import (
    VerifiedMarketMorningOperator,
    require_operations_reader,
)
from src.market_morning.beta_access import (
    BetaAccessConflict,
    BetaAccessValidationError,
    accept_invite,
)
from src.market_morning.account_privacy import (
    AccountPrivacyUnavailable,
    AccountPrivacyValidationError,
    get_account_data_export,
)
from src.config.accessor import get_env_config
from src.market_morning.content_reports import (
    ContentReportUnavailable,
    create_content_report,
)
from src.market_morning.delivery_links import (
    DELIVERY_LINK_DESTINATION_PATH,
    DeliveryLinkUnavailable,
    delivery_link_static_preflight_checks,
    redeem_delivery_link,
)
from src.market_morning.db import (
    MarketMorningDatabaseNotConfigured,
    probe_database,
    reset_database_state,
)
from src.market_morning.edition_service import get_today_edition
from src.market_morning.edition_interactions import (
    EditionInteractionUnavailable,
    get_edition_event_states,
    open_edition_source,
    update_edition_event_state,
)
from src.market_morning.email_identity import email_identity_static_preflight_checks
from src.market_morning.issuer_search import search_issuer_catalog
from src.market_morning.issuer_research import (
    IssuerResearchUnavailable,
    IssuerResearchValidationError,
    delete_issuer_research_note,
    get_issuer_research,
    update_issuer_research_note,
)
from src.market_morning.normalization import normalize_issuer_search_key
from src.market_morning.oidc_auth import builtin_oidc_preflight_checks
from src.market_morning.production_runtime_factory import (
    PRODUCTION_RUNTIME_FACTORY_PATH,
)
from src.market_morning.settings import (
    ConsentMutationResult,
    ConsentStateView,
    ConsentType,
    DeletionRequestResult,
    SettingsUserUnavailable,
    SettingsValidationError,
    UserSettingsView,
    get_settings,
    record_consent,
    request_account_deletion,
    update_settings,
)
from src.market_morning.source_queries import (
    EventAuditView,
    SourceHealthView,
    get_affected_events,
    get_event_history,
    list_source_health,
)
from src.market_morning.repositories.morning_edition import EditionPayloadInvalid
from src.market_morning.watchlist import (
    MAX_ACTIVE_WATCHLIST_ITEMS,
    WatchlistIssuerUnavailable,
    WatchlistItemNotFound,
    WatchlistItemView,
    WatchlistLimitReached,
    WatchlistMutationResult,
    WatchlistUserUnavailable,
    WatchlistValidationError,
    add_to_watchlist,
    get_watchlist,
    remove_from_watchlist,
    update_in_watchlist,
)

logger = logging.getLogger(__name__)


class MarketMorningReadiness(BaseModel):
    status: Literal["disabled", "misconfigured", "ready", "unavailable"]
    enabled: bool
    database_configured: bool
    timestamp: str
    reason: str | None = None


class MarketMorningDeploymentPreflight(BaseModel):
    status: Literal["blocked", "configuration_ready"]
    scope: Literal["static_configuration_and_database"]
    enabled: bool
    database_ready: bool
    product_auth_configured: bool
    admin_auth_configured: bool
    oidc_configuration_ready: bool
    runtime_enabled: bool
    runtime_factory_configured: bool
    provider_bundle_factory_configured: bool
    email_webhook_configured: bool
    email_identity_factory_configured: bool
    delivery_link_configuration_ready: bool
    synthetic_data_disabled: bool
    fixture_runtime_disabled: bool
    counts_as_t1_evidence: Literal[False] = False
    blocking_checks: list[str]
    timestamp: str


class IssuerSearchItem(BaseModel):
    issuer_id: str
    issuer_code: str
    legal_name_ja: str
    market_segment: str
    match_kind: Literal[
        "code_exact",
        "official_exact",
        "alias_exact",
        "official_prefix",
        "alias_prefix",
    ]
    matched_alias: str | None = None


class IssuerSearchResponse(BaseModel):
    query: str
    normalized_query: str
    items: list[IssuerSearchItem]
    no_match_guidance: str | None = None


class WatchlistItemResponse(BaseModel):
    watchlist_item_id: str
    issuer_id: str
    issuer_code: str
    legal_name_ja: str
    market_segment: str
    issuer_status: str
    user_label: str | None
    sort_order: int
    created_at: datetime


class WatchlistResponse(BaseModel):
    items: list[WatchlistItemResponse]
    active_count: int
    limit: int = MAX_ACTIVE_WATCHLIST_ITEMS


class WatchlistMutationRequest(BaseModel):
    issuer_id: str = Field(min_length=36, max_length=36)
    user_label: str | None = Field(default=None, max_length=40)


class WatchlistUpdateRequest(BaseModel):
    user_label: str | None = Field(default=None, max_length=40)
    sort_order: int = Field(ge=0, lt=MAX_ACTIVE_WATCHLIST_ITEMS)


class WatchlistMutationResponse(BaseModel):
    status: Literal[
        "added",
        "already_active",
        "updated",
        "removed",
        "already_removed",
    ]
    item: WatchlistItemResponse | None
    active_count: int
    limit: int = MAX_ACTIVE_WATCHLIST_ITEMS


class ConsentStateResponse(BaseModel):
    consent_type: Literal["risk_disclosure", "data_disclosure"]
    accepted: bool
    consent_version: str | None
    accepted_at: datetime | None
    revoked_at: datetime | None


class MarketMorningSettingsResponse(BaseModel):
    timezone: str
    email_opt_in: bool
    risk_disclosure: ConsentStateResponse
    data_disclosure: ConsentStateResponse


class MarketMorningSettingsUpdateRequest(BaseModel):
    timezone: str | None = Field(default=None, max_length=64)
    email_opt_in: bool | None = None


class ConsentMutationRequest(BaseModel):
    consent_type: Literal["risk_disclosure", "data_disclosure"]
    consent_version: str = Field(min_length=1, max_length=64)
    accepted: bool


class ConsentMutationResponse(BaseModel):
    status: Literal[
        "accepted",
        "already_accepted",
        "revoked",
        "already_revoked",
    ]
    consent: ConsentStateResponse


class AccountDeletionRequestResponse(BaseModel):
    status: Literal["requested", "already_requested"]
    request_id: str
    request_status: Literal["pending", "processing"]
    requested_at: datetime


class AccountDataExportResponse(BaseModel):
    schema_version: Literal[1]
    generated_at: datetime
    data: dict[str, list[dict[str, Any]]]


class PrivateBetaInviteAcceptRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: str = Field(min_length=32, max_length=256)


class PrivateBetaInviteAcceptResponse(BaseModel):
    status: Literal["accepted", "already_accepted"]
    invite_id: str
    user_id: str
    expires_at: datetime


class DeliveryLinkRedeemRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: str = Field(pattern=r"^[A-Za-z0-9_-]{43}$")


class DeliveryLinkRedeemResponse(BaseModel):
    status: Literal["accepted"]
    edition_date: date
    destination_path: Literal["/market-morning"] = DELIVERY_LINK_DESTINATION_PATH


class SourceHealthItemResponse(BaseModel):
    provider: str
    status: Literal["healthy", "error", "never_run"]
    cursor: dict | None
    last_successful_discovery_at: datetime | None
    last_error_at: datetime | None
    last_error_code: str | None


class SourceHealthResponse(BaseModel):
    items: list[SourceHealthItemResponse]
    count: int


class EventAuditItemResponse(BaseModel):
    event_id: str
    event_family_key: str
    event_version: int
    issuer_id: str
    issuer_code: str
    legal_name_ja: str
    title: str
    event_type: str
    occurred_at: datetime
    event_lifecycle_status: str
    supersedes_event_id: str | None
    source_record_id: str
    source_provider: str
    provider_document_id: str
    provider_revision_key: str
    original_url: str
    source_published_at: datetime
    source_fetched_at: datetime
    source_lifecycle_status: str
    relation_type: str


class EventAuditResponse(BaseModel):
    items: list[EventAuditItemResponse]
    count: int


class _EditionResponseModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class EditionSourceCitationResponse(_EditionResponseModel):
    provider: str
    document_id: str
    revision_key: str
    original_url: str
    published_at: datetime


class EditionFactResponse(_EditionResponseModel):
    kind: Literal["fact"]
    text: str
    event_id: str
    event_family_key: str
    event_version: int
    event_type: str
    occurred_at: datetime
    lifecycle_status: Literal["active", "corrected", "withdrawn"]
    citations: list[EditionSourceCitationResponse]


class EditionAssessmentResponse(_EditionResponseModel):
    kind: Literal["inference"]
    text: str
    evidence_quality: Literal["insufficient_evidence"]


class EditionIssuerResponse(_EditionResponseModel):
    issuer_id: str
    issuer_code: str
    legal_name_ja: str
    status: Literal[
        "ready",
        "partial",
        "no_confirmed_events",
        "insufficient_data",
        "unavailable",
    ]
    facts: list[EditionFactResponse]
    assessment: EditionAssessmentResponse
    omitted_event_count: int
    warnings: list[str]
    error_code: str | None = None


class EditionDayPlanResponse(_EditionResponseModel):
    edition_date: date
    generate: bool
    status: Literal["scheduled", "market_holiday"]
    overnight_context: Literal["us_session_available", "us_market_closed"]
    reason_code: str | None
    us_reason_code: str | None


class EditionBudgetResponse(_EditionResponseModel):
    max_issuers: int
    max_events_per_issuer: int
    max_total_events: int


class MorningEditionResponse(_EditionResponseModel):
    edition_date: date
    generated_at: datetime
    status: Literal["ready", "partial", "no_data", "market_holiday"]
    day_plan: EditionDayPlanResponse
    issuers: list[EditionIssuerResponse]
    consumed_event_units: int
    budget: EditionBudgetResponse
    budget_exhausted: bool
    omitted_issuer_count: int


class TodayEditionResponse(_EditionResponseModel):
    status: Literal[
        "ready",
        "partial",
        "no_data",
        "market_holiday",
        "not_published",
    ]
    data_mode: Literal["synthetic_fixture", "persisted", "unavailable"]
    edition_id: str | None
    reason_code: str | None
    fixture_version: str | None
    edition: MorningEditionResponse | None


class EditionEventStateUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state: Literal["read", "later", "irrelevant"]


class EditionEventStateResponse(_EditionResponseModel):
    edition_id: str
    event_id: str
    state: Literal["read", "later", "irrelevant"]
    first_read_at: datetime | None
    updated_at: datetime


class EditionEventStatesResponse(_EditionResponseModel):
    items: list[EditionEventStateResponse]


class EditionSourceOpenRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(min_length=1, max_length=64)
    provider: str = Field(min_length=1, max_length=64)
    document_id: str = Field(min_length=1, max_length=191)
    revision_key: str = Field(min_length=1, max_length=128)
    request_id: str = Field(min_length=36, max_length=36)


class EditionSourceOpenResponse(_EditionResponseModel):
    status: Literal["recorded", "already_recorded"]
    source_open_id: str
    original_url: str


class ContentReportCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason_code: Literal[
        "fact_inaccurate",
        "source_mismatch",
        "outdated_or_corrected",
        "other_content_issue",
    ]


class ContentReportCreateResponse(_EditionResponseModel):
    status: Literal["reported", "already_reported"]
    report_id: str
    reason_code: Literal[
        "fact_inaccurate",
        "source_mismatch",
        "outdated_or_corrected",
        "other_content_issue",
    ]
    report_status: Literal["pending", "resolved", "dismissed"]
    created_at: datetime


class IssuerResearchNoteResponse(_EditionResponseModel):
    note_id: str
    text: str
    created_at: datetime
    updated_at: datetime


class IssuerResearchFactResponse(_EditionResponseModel):
    text: str
    citations: list[EditionSourceCitationResponse]


class IssuerResearchEventResponse(_EditionResponseModel):
    event_id: str
    event_family_key: str
    event_version: int
    title: str
    event_type: str
    occurred_at: datetime
    lifecycle_status: Literal["active", "corrected", "withdrawn"]
    supersedes_event_id: str | None
    facts: list[IssuerResearchFactResponse]
    citations: list[EditionSourceCitationResponse]
    warnings: list[str]


class IssuerResearchResponse(_EditionResponseModel):
    issuer_id: str
    issuer_code: str
    legal_name_ja: str
    market_segment: str
    note: IssuerResearchNoteResponse | None
    events: list[IssuerResearchEventResponse]


class IssuerResearchNoteUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=1_000)


class IssuerResearchNoteMutationResponse(_EditionResponseModel):
    status: Literal["saved"]
    note: IssuerResearchNoteResponse


class IssuerResearchNoteDeleteResponse(BaseModel):
    status: Literal["cleared", "already_clear"]


def _watchlist_item_response(item: WatchlistItemView) -> WatchlistItemResponse:
    return WatchlistItemResponse(
        watchlist_item_id=item.watchlist_item_id,
        issuer_id=item.issuer_id,
        issuer_code=item.issuer_code,
        legal_name_ja=item.legal_name_ja,
        market_segment=item.market_segment,
        issuer_status=item.issuer_status,
        user_label=item.user_label,
        sort_order=item.sort_order,
        created_at=item.created_at,
    )


def _watchlist_mutation_response(
    result: WatchlistMutationResult,
) -> WatchlistMutationResponse:
    return WatchlistMutationResponse(
        status=result.status.value,
        item=_watchlist_item_response(result.item) if result.item else None,
        active_count=result.active_count,
        limit=result.limit,
    )


def _consent_state_response(value: ConsentStateView) -> ConsentStateResponse:
    return ConsentStateResponse(
        consent_type=value.consent_type.value,
        accepted=value.accepted,
        consent_version=value.consent_version,
        accepted_at=value.accepted_at,
        revoked_at=value.revoked_at,
    )


def _settings_response(value: UserSettingsView) -> MarketMorningSettingsResponse:
    return MarketMorningSettingsResponse(
        timezone=value.timezone,
        email_opt_in=value.email_opt_in,
        risk_disclosure=_consent_state_response(value.risk_disclosure),
        data_disclosure=_consent_state_response(value.data_disclosure),
    )


def _consent_mutation_response(
    value: ConsentMutationResult,
) -> ConsentMutationResponse:
    return ConsentMutationResponse(
        status=value.status.value,
        consent=_consent_state_response(value.consent),
    )


def _deletion_request_response(
    value: DeletionRequestResult,
) -> AccountDeletionRequestResponse:
    return AccountDeletionRequestResponse(
        status=value.status.value,
        request_id=value.request_id,
        request_status=value.request_status,
        requested_at=value.requested_at,
    )


def _source_health_item_response(value: SourceHealthView) -> SourceHealthItemResponse:
    return SourceHealthItemResponse(
        provider=value.provider,
        status=value.status,
        cursor=value.cursor,
        last_successful_discovery_at=value.last_successful_discovery_at,
        last_error_at=value.last_error_at,
        last_error_code=value.last_error_code,
    )


def _event_audit_item_response(value: EventAuditView) -> EventAuditItemResponse:
    return EventAuditItemResponse(
        event_id=value.event_id,
        event_family_key=value.event_family_key,
        event_version=value.event_version,
        issuer_id=value.issuer_id,
        issuer_code=value.issuer_code,
        legal_name_ja=value.legal_name_ja,
        title=value.title,
        event_type=value.event_type,
        occurred_at=value.occurred_at,
        event_lifecycle_status=value.event_lifecycle_status,
        supersedes_event_id=value.supersedes_event_id,
        source_record_id=value.source_record_id,
        source_provider=value.source_provider,
        provider_document_id=value.provider_document_id,
        provider_revision_key=value.provider_revision_key,
        original_url=value.original_url,
        source_published_at=value.source_published_at,
        source_fetched_at=value.source_fetched_at,
        source_lifecycle_status=value.source_lifecycle_status,
        relation_type=value.relation_type,
    )


def _raise_watchlist_http_error(exc: Exception) -> Never:
    if isinstance(exc, WatchlistLimitReached):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    if isinstance(exc, (WatchlistIssuerUnavailable, WatchlistItemNotFound)):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    if isinstance(exc, WatchlistUserUnavailable):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    if isinstance(exc, WatchlistValidationError):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
    if isinstance(exc, IntegrityError):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Watchlist changed concurrently; retry the request",
        ) from exc
    if isinstance(exc, (MarketMorningDatabaseNotConfigured, SQLAlchemyError)):
        logger.warning("Market Morning watchlist request failed: %s", type(exc).__name__)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Watchlist is temporarily unavailable",
        ) from exc
    raise exc


def _raise_settings_http_error(exc: Exception) -> Never:
    if isinstance(exc, SettingsUserUnavailable):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    if isinstance(exc, SettingsValidationError):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
    if isinstance(exc, IntegrityError):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Account settings changed concurrently; retry the request",
        ) from exc
    if isinstance(exc, (MarketMorningDatabaseNotConfigured, SQLAlchemyError)):
        logger.warning("Market Morning settings request failed: %s", type(exc).__name__)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Account settings are temporarily unavailable",
        ) from exc
    raise exc


def _raise_account_privacy_http_error(exc: Exception) -> Never:
    if isinstance(exc, AccountPrivacyUnavailable):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account data export is unavailable",
        ) from exc
    if isinstance(exc, AccountPrivacyValidationError):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Account data export request is invalid",
        ) from exc
    if isinstance(exc, (MarketMorningDatabaseNotConfigured, SQLAlchemyError)):
        logger.warning(
            "Market Morning account export failed: %s",
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Account data export is temporarily unavailable",
        ) from exc
    raise exc


def _raise_delivery_link_http_error(exc: Exception) -> Never:
    if isinstance(exc, (DeliveryLinkUnavailable, ValueError)):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Delivery link is unavailable",
        ) from exc
    if isinstance(exc, (MarketMorningDatabaseNotConfigured, SQLAlchemyError)):
        logger.warning(
            "Market Morning delivery link request failed: %s",
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Delivery link is temporarily unavailable",
        ) from exc
    raise exc


def _raise_beta_acceptance_http_error(exc: Exception) -> Never:
    if isinstance(exc, BetaAccessConflict):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Private beta invitation is unavailable",
        ) from exc
    if isinstance(exc, BetaAccessValidationError):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Private beta invitation is invalid",
        ) from exc
    if isinstance(exc, (IntegrityError, MarketMorningDatabaseNotConfigured, SQLAlchemyError)):
        logger.warning(
            "Market Morning invite acceptance failed: %s",
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Private beta invitation is temporarily unavailable",
        ) from exc
    raise exc


def _require_source_audit_enabled() -> None:
    if not get_env_config().market_morning.enabled:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Market Morning is disabled",
        )


def _raise_source_audit_http_error(exc: Exception) -> Never:
    if isinstance(exc, ValueError):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc
    if isinstance(exc, (MarketMorningDatabaseNotConfigured, SQLAlchemyError)):
        logger.warning("Market Morning source audit request failed: %s", type(exc).__name__)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Source audit data is temporarily unavailable",
        ) from exc
    raise exc


def _raise_edition_interaction_http_error(exc: Exception) -> Never:
    if isinstance(exc, EditionInteractionUnavailable):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Edition item is unavailable",
        ) from exc
    if isinstance(exc, ValueError):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Edition interaction is invalid",
        ) from exc
    if isinstance(
        exc,
        (EditionPayloadInvalid, MarketMorningDatabaseNotConfigured, SQLAlchemyError),
    ):
        logger.warning(
            "Market Morning edition interaction failed: %s",
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Edition interactions are temporarily unavailable",
        ) from exc
    raise exc


def _raise_content_report_http_error(exc: Exception) -> Never:
    if isinstance(exc, ContentReportUnavailable):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Edition event is unavailable for reporting",
        ) from exc
    if isinstance(exc, ValueError):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Content report is invalid",
        ) from exc
    if isinstance(exc, IntegrityError):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Content report changed concurrently; retry the request",
        ) from exc
    if isinstance(exc, (MarketMorningDatabaseNotConfigured, SQLAlchemyError)):
        logger.warning(
            "Market Morning content report failed: %s",
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Content reporting is temporarily unavailable",
        ) from exc
    raise exc


def _raise_issuer_research_http_error(exc: Exception) -> Never:
    if isinstance(exc, IssuerResearchUnavailable):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Issuer research is unavailable",
        ) from exc
    if isinstance(exc, IssuerResearchValidationError):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Issuer research request is invalid",
        ) from exc
    if isinstance(
        exc,
        (IntegrityError, MarketMorningDatabaseNotConfigured, SQLAlchemyError),
    ):
        logger.warning(
            "Market Morning issuer research request failed: %s",
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Issuer research is temporarily unavailable",
        ) from exc
    raise exc


def register_market_morning_routes(app: FastAPI) -> None:
    """Register internal readiness without initializing the product database."""

    @app.post(
        "/market-morning/private-beta/invitations/accept",
        response_model=PrivateBetaInviteAcceptResponse,
    )
    async def market_morning_private_beta_invite_accept(
        payload: PrivateBetaInviteAcceptRequest,
        principal: MarketMorningOnboardingPrincipal = Depends(require_market_morning_onboarding_principal),
    ) -> PrivateBetaInviteAcceptResponse:
        try:
            result = await accept_invite(
                raw_token=payload.token,
                external_subject=principal.external_subject,
            )
        except Exception as exc:
            _raise_beta_acceptance_http_error(exc)
        assert result.user_id is not None
        return PrivateBetaInviteAcceptResponse(
            status=result.status.value,
            invite_id=result.invite_id,
            user_id=result.user_id,
            expires_at=result.expires_at,
        )

    app.router.add_event_handler("shutdown", reset_database_state)

    @app.get(
        "/market-morning/_internal/ready",
        response_model=MarketMorningReadiness,
        responses={503: {"model": MarketMorningReadiness}},
    )
    async def market_morning_readiness(
        _operator: VerifiedMarketMorningOperator = Depends(require_operations_reader),
    ):
        cfg = get_env_config().market_morning
        now = datetime.now(timezone.utc).isoformat()
        configured = bool(cfg.database_url.strip())

        if not cfg.enabled:
            payload = MarketMorningReadiness(
                status="disabled",
                enabled=False,
                database_configured=configured,
                timestamp=now,
                reason="Market Morning is disabled",
            )
            return JSONResponse(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, content=payload.model_dump())

        ready, reason = await probe_database()
        if ready:
            return MarketMorningReadiness(
                status="ready",
                enabled=True,
                database_configured=True,
                timestamp=now,
            )

        readiness_status: Literal["misconfigured", "unavailable"]
        is_configuration_error = (
            not configured
            or reason.startswith("VIBE_MARKET_MORNING_DATABASE_URL")
            or reason.startswith("Market Morning database URL")
            or reason == "Market Morning database schema is not current"
        )
        readiness_status = "misconfigured" if is_configuration_error else "unavailable"
        payload = MarketMorningReadiness(
            status=readiness_status,
            enabled=True,
            database_configured=configured,
            timestamp=now,
            reason=reason,
        )
        return JSONResponse(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, content=payload.model_dump())

    @app.get(
        "/market-morning/_internal/deployment-preflight",
        response_model=MarketMorningDeploymentPreflight,
        responses={503: {"model": MarketMorningDeploymentPreflight}},
    )
    async def market_morning_deployment_preflight(
        _operator: VerifiedMarketMorningOperator = Depends(require_operations_reader),
    ):
        cfg = get_env_config().market_morning
        enabled = bool(cfg.enabled)
        database_ready = False
        if enabled:
            database_ready, _reason = await probe_database()

        product_auth_configured = bool(cfg.auth_factory.strip())
        admin_auth_configured = bool(cfg.admin_auth_factory.strip())
        oidc_blocking_checks = builtin_oidc_preflight_checks(cfg)
        oidc_configuration_ready = not oidc_blocking_checks
        runtime_enabled = bool(cfg.runtime_enabled)
        runtime_factory_configured = bool(cfg.runtime_factory.strip())
        provider_bundle_factory_configured = bool(cfg.provider_bundle_factory.strip())
        using_builtin_production_factory = cfg.runtime_factory.strip() == PRODUCTION_RUNTIME_FACTORY_PATH
        email_webhook_configured = bool(cfg.email_webhook_factory.strip())
        email_identity_blocking_checks = email_identity_static_preflight_checks(cfg.email_identity_factory)
        email_identity_factory_configured = not email_identity_blocking_checks
        delivery_link_blocking_checks = delivery_link_static_preflight_checks()
        delivery_link_configuration_ready = not delivery_link_blocking_checks
        synthetic_data_disabled = not cfg.synthetic_edition_enabled
        fixture_runtime_disabled = not cfg.allow_fixture_runtime

        checks = {
            "market_morning_disabled": not enabled,
            "database_not_ready": not database_ready,
            "product_auth_factory_missing": not product_auth_configured,
            "admin_auth_factory_missing": not admin_auth_configured,
            "runtime_disabled": not runtime_enabled,
            "runtime_factory_missing": not runtime_factory_configured,
            "provider_bundle_factory_missing": (
                using_builtin_production_factory and not provider_bundle_factory_configured
            ),
            "email_webhook_factory_missing": not email_webhook_configured,
            "synthetic_data_enabled": not synthetic_data_disabled,
            "fixture_runtime_enabled": not fixture_runtime_disabled,
        }
        checks.update({name: True for name in oidc_blocking_checks})
        checks.update({name: True for name in email_identity_blocking_checks})
        checks.update({name: True for name in delivery_link_blocking_checks})
        blocking_checks = sorted(name for name, blocked in checks.items() if blocked)
        payload = MarketMorningDeploymentPreflight(
            status="blocked" if blocking_checks else "configuration_ready",
            scope="static_configuration_and_database",
            enabled=enabled,
            database_ready=database_ready,
            product_auth_configured=product_auth_configured,
            admin_auth_configured=admin_auth_configured,
            oidc_configuration_ready=oidc_configuration_ready,
            runtime_enabled=runtime_enabled,
            runtime_factory_configured=runtime_factory_configured,
            provider_bundle_factory_configured=(provider_bundle_factory_configured),
            email_webhook_configured=email_webhook_configured,
            email_identity_factory_configured=email_identity_factory_configured,
            delivery_link_configuration_ready=delivery_link_configuration_ready,
            synthetic_data_disabled=synthetic_data_disabled,
            fixture_runtime_disabled=fixture_runtime_disabled,
            blocking_checks=blocking_checks,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
        if blocking_checks:
            return JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content=payload.model_dump(),
            )
        return payload

    @app.get(
        "/market-morning/_internal/sources/health",
        response_model=SourceHealthResponse,
    )
    async def market_morning_source_health(
        provider: str | None = Query(default=None, min_length=1, max_length=64),
        limit: int = Query(default=50, ge=1, le=100),
        _operator: VerifiedMarketMorningOperator = Depends(require_operations_reader),
    ) -> SourceHealthResponse:
        _require_source_audit_enabled()
        try:
            items = await list_source_health(provider=provider, limit=limit)
        except Exception as exc:
            _raise_source_audit_http_error(exc)
        return SourceHealthResponse(
            items=[_source_health_item_response(item) for item in items],
            count=len(items),
        )

    @app.get(
        "/market-morning/_internal/events/{event_family_key}/history",
        response_model=EventAuditResponse,
    )
    async def market_morning_event_history(
        event_family_key: str = Path(..., min_length=1, max_length=191),
        limit: int = Query(default=50, ge=1, le=100),
        _operator: VerifiedMarketMorningOperator = Depends(require_operations_reader),
    ) -> EventAuditResponse:
        _require_source_audit_enabled()
        try:
            items = await get_event_history(
                event_family_key=event_family_key,
                limit=limit,
            )
        except Exception as exc:
            _raise_source_audit_http_error(exc)
        return EventAuditResponse(
            items=[_event_audit_item_response(item) for item in items],
            count=len(items),
        )

    @app.get(
        "/market-morning/_internal/sources/{provider}/{document_id}/affected-events",
        response_model=EventAuditResponse,
    )
    async def market_morning_affected_events(
        provider: str = Path(..., min_length=1, max_length=64),
        document_id: str = Path(..., min_length=1, max_length=191),
        limit: int = Query(default=100, ge=1, le=100),
        _operator: VerifiedMarketMorningOperator = Depends(require_operations_reader),
    ) -> EventAuditResponse:
        _require_source_audit_enabled()
        try:
            items = await get_affected_events(
                provider=provider,
                document_id=document_id,
                limit=limit,
            )
        except Exception as exc:
            _raise_source_audit_http_error(exc)
        return EventAuditResponse(
            items=[_event_audit_item_response(item) for item in items],
            count=len(items),
        )

    @app.get(
        "/market-morning/issuers/search",
        response_model=IssuerSearchResponse,
    )
    async def market_morning_issuer_search(
        q: str = Query(..., min_length=1, max_length=100),
        limit: int = Query(10, ge=1, le=20),
        _principal: MarketMorningPrincipal = Depends(require_market_morning_principal),
    ) -> IssuerSearchResponse:
        cfg = get_env_config().market_morning
        if not cfg.enabled:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Market Morning is disabled",
            )
        try:
            matches = await search_issuer_catalog(q, limit=limit)
        except MarketMorningDatabaseNotConfigured as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Market Morning database is not configured",
            ) from exc
        except SQLAlchemyError as exc:
            logger.warning("Market Morning issuer search failed: %s", type(exc).__name__)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Issuer search is temporarily unavailable",
            ) from exc

        items = [
            IssuerSearchItem(
                issuer_id=match.issuer_id,
                issuer_code=match.issuer_code,
                legal_name_ja=match.legal_name_ja,
                market_segment=match.market_segment,
                match_kind=match.match_kind,
                matched_alias=match.matched_alias,
            )
            for match in matches
        ]
        return IssuerSearchResponse(
            query=q,
            normalized_query=normalize_issuer_search_key(q),
            items=items,
            no_match_guidance=(None if items else "証券コードまたは正式な会社名で検索してください。"),
        )

    @app.get(
        "/market-morning/issuers/{issuer_id}/research",
        response_model=IssuerResearchResponse,
    )
    async def market_morning_issuer_research(
        issuer_id: str = Path(..., min_length=36, max_length=36),
        limit: int = Query(default=50, ge=1, le=100),
        principal: MarketMorningPrincipal = Depends(require_market_morning_principal),
    ) -> IssuerResearchResponse:
        try:
            result = await get_issuer_research(
                user_id=principal.user_id,
                issuer_id=issuer_id,
                limit=limit,
            )
        except Exception as exc:
            _raise_issuer_research_http_error(exc)
        return IssuerResearchResponse.model_validate(result)

    @app.put(
        "/market-morning/issuers/{issuer_id}/note",
        response_model=IssuerResearchNoteMutationResponse,
    )
    async def market_morning_issuer_note_update(
        payload: IssuerResearchNoteUpdateRequest,
        issuer_id: str = Path(..., min_length=36, max_length=36),
        principal: MarketMorningPrincipal = Depends(require_market_morning_principal),
    ) -> IssuerResearchNoteMutationResponse:
        try:
            note = await update_issuer_research_note(
                user_id=principal.user_id,
                issuer_id=issuer_id,
                text=payload.text,
            )
        except Exception as exc:
            _raise_issuer_research_http_error(exc)
        return IssuerResearchNoteMutationResponse(
            status="saved",
            note=IssuerResearchNoteResponse.model_validate(note),
        )

    @app.delete(
        "/market-morning/issuers/{issuer_id}/note",
        response_model=IssuerResearchNoteDeleteResponse,
    )
    async def market_morning_issuer_note_delete(
        issuer_id: str = Path(..., min_length=36, max_length=36),
        principal: MarketMorningPrincipal = Depends(require_market_morning_principal),
    ) -> IssuerResearchNoteDeleteResponse:
        try:
            cleared = await delete_issuer_research_note(
                user_id=principal.user_id,
                issuer_id=issuer_id,
            )
        except Exception as exc:
            _raise_issuer_research_http_error(exc)
        return IssuerResearchNoteDeleteResponse(status="cleared" if cleared else "already_clear")

    @app.get(
        "/market-morning/edition/today",
        response_model=TodayEditionResponse,
    )
    async def market_morning_today_edition(
        principal: MarketMorningPrincipal = Depends(require_market_morning_principal),
    ) -> TodayEditionResponse:
        try:
            result = await get_today_edition(user_id=principal.user_id)
        except (
            EditionPayloadInvalid,
            MarketMorningDatabaseNotConfigured,
            SQLAlchemyError,
        ) as exc:
            logger.warning(
                "Market Morning edition request failed: %s",
                type(exc).__name__,
            )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Today's edition is temporarily unavailable",
            ) from exc
        return TodayEditionResponse.model_validate(result)

    @app.post(
        "/market-morning/delivery-links/redeem",
        response_model=DeliveryLinkRedeemResponse,
    )
    async def market_morning_delivery_link_redeem(
        payload: DeliveryLinkRedeemRequest,
        principal: MarketMorningPrincipal = Depends(require_market_morning_principal),
    ) -> DeliveryLinkRedeemResponse:
        try:
            result = await redeem_delivery_link(
                user_id=principal.user_id,
                token=payload.token,
                redeemed_at=datetime.now(timezone.utc),
            )
        except Exception as exc:
            _raise_delivery_link_http_error(exc)
        return DeliveryLinkRedeemResponse(
            status="accepted",
            edition_date=result.edition_date,
            destination_path=result.destination_path,
        )

    @app.patch(
        "/market-morning/editions/{edition_id}/events/{event_id}/state",
        response_model=EditionEventStateResponse,
    )
    async def market_morning_edition_event_state(
        payload: EditionEventStateUpdateRequest,
        edition_id: str = Path(..., min_length=36, max_length=36),
        event_id: str = Path(..., min_length=1, max_length=64),
        principal: MarketMorningPrincipal = Depends(require_market_morning_principal),
    ) -> EditionEventStateResponse:
        try:
            result = await update_edition_event_state(
                user_id=principal.user_id,
                edition_id=edition_id,
                event_id=event_id,
                state=payload.state,
            )
        except Exception as exc:
            _raise_edition_interaction_http_error(exc)
        return EditionEventStateResponse.model_validate(result)

    @app.get(
        "/market-morning/editions/{edition_id}/event-states",
        response_model=EditionEventStatesResponse,
    )
    async def market_morning_edition_event_states(
        edition_id: str = Path(..., min_length=36, max_length=36),
        principal: MarketMorningPrincipal = Depends(require_market_morning_principal),
    ) -> EditionEventStatesResponse:
        try:
            results = await get_edition_event_states(
                user_id=principal.user_id,
                edition_id=edition_id,
            )
        except Exception as exc:
            _raise_edition_interaction_http_error(exc)
        return EditionEventStatesResponse(items=[EditionEventStateResponse.model_validate(item) for item in results])

    @app.post(
        "/market-morning/editions/{edition_id}/sources/open",
        response_model=EditionSourceOpenResponse,
    )
    async def market_morning_edition_source_open(
        payload: EditionSourceOpenRequest,
        edition_id: str = Path(..., min_length=36, max_length=36),
        principal: MarketMorningPrincipal = Depends(require_market_morning_principal),
    ) -> EditionSourceOpenResponse:
        try:
            result = await open_edition_source(
                user_id=principal.user_id,
                edition_id=edition_id,
                event_id=payload.event_id,
                provider=payload.provider,
                document_id=payload.document_id,
                revision_key=payload.revision_key,
                request_id=payload.request_id,
            )
        except Exception as exc:
            _raise_edition_interaction_http_error(exc)
        return EditionSourceOpenResponse.model_validate(result)

    @app.post(
        "/market-morning/editions/{edition_id}/events/{event_id}/report",
        response_model=ContentReportCreateResponse,
    )
    async def market_morning_content_report_create(
        payload: ContentReportCreateRequest,
        edition_id: str = Path(..., min_length=36, max_length=36),
        event_id: str = Path(..., min_length=36, max_length=36),
        principal: MarketMorningPrincipal = Depends(require_market_morning_principal),
    ) -> ContentReportCreateResponse:
        try:
            result = await create_content_report(
                user_id=principal.user_id,
                edition_id=edition_id,
                event_id=event_id,
                reason_code=payload.reason_code,
            )
        except Exception as exc:
            _raise_content_report_http_error(exc)
        return ContentReportCreateResponse.model_validate(result)

    @app.get("/market-morning/watchlist", response_model=WatchlistResponse)
    async def market_morning_watchlist(
        principal: MarketMorningPrincipal = Depends(require_market_morning_principal),
    ) -> WatchlistResponse:
        try:
            items = await get_watchlist(user_id=principal.user_id)
        except Exception as exc:
            _raise_watchlist_http_error(exc)
        return WatchlistResponse(
            items=[_watchlist_item_response(item) for item in items],
            active_count=len(items),
        )

    @app.post("/market-morning/watchlist", response_model=WatchlistMutationResponse)
    async def market_morning_watchlist_add(
        payload: WatchlistMutationRequest,
        principal: MarketMorningPrincipal = Depends(require_market_morning_principal),
    ) -> WatchlistMutationResponse:
        try:
            result = await add_to_watchlist(
                user_id=principal.user_id,
                issuer_id=payload.issuer_id,
                user_label=payload.user_label,
            )
        except Exception as exc:
            _raise_watchlist_http_error(exc)
        return _watchlist_mutation_response(result)

    @app.patch(
        "/market-morning/watchlist/{issuer_id}",
        response_model=WatchlistMutationResponse,
    )
    async def market_morning_watchlist_update(
        issuer_id: str,
        payload: WatchlistUpdateRequest,
        principal: MarketMorningPrincipal = Depends(require_market_morning_principal),
    ) -> WatchlistMutationResponse:
        try:
            result = await update_in_watchlist(
                user_id=principal.user_id,
                issuer_id=issuer_id,
                user_label=payload.user_label,
                sort_order=payload.sort_order,
            )
        except Exception as exc:
            _raise_watchlist_http_error(exc)
        return _watchlist_mutation_response(result)

    @app.delete(
        "/market-morning/watchlist/{issuer_id}",
        response_model=WatchlistMutationResponse,
    )
    async def market_morning_watchlist_remove(
        issuer_id: str,
        principal: MarketMorningPrincipal = Depends(require_market_morning_principal),
    ) -> WatchlistMutationResponse:
        try:
            result = await remove_from_watchlist(
                user_id=principal.user_id,
                issuer_id=issuer_id,
            )
        except Exception as exc:
            _raise_watchlist_http_error(exc)
        return _watchlist_mutation_response(result)

    @app.get(
        "/market-morning/settings",
        response_model=MarketMorningSettingsResponse,
    )
    async def market_morning_settings(
        principal: MarketMorningPrincipal = Depends(require_market_morning_principal),
    ) -> MarketMorningSettingsResponse:
        try:
            result = await get_settings(user_id=principal.user_id)
        except Exception as exc:
            _raise_settings_http_error(exc)
        return _settings_response(result)

    @app.patch(
        "/market-morning/settings",
        response_model=MarketMorningSettingsResponse,
    )
    async def market_morning_settings_update(
        payload: MarketMorningSettingsUpdateRequest,
        principal: MarketMorningPrincipal = Depends(require_market_morning_principal),
    ) -> MarketMorningSettingsResponse:
        try:
            result = await update_settings(
                user_id=principal.user_id,
                timezone=payload.timezone,
                email_opt_in=payload.email_opt_in,
            )
        except Exception as exc:
            _raise_settings_http_error(exc)
        return _settings_response(result)

    @app.post(
        "/market-morning/consents",
        response_model=ConsentMutationResponse,
    )
    async def market_morning_consent(
        payload: ConsentMutationRequest,
        principal: MarketMorningPrincipal = Depends(require_market_morning_principal),
    ) -> ConsentMutationResponse:
        try:
            result = await record_consent(
                user_id=principal.user_id,
                consent_type=ConsentType(payload.consent_type),
                consent_version=payload.consent_version,
                accepted=payload.accepted,
            )
        except Exception as exc:
            _raise_settings_http_error(exc)
        return _consent_mutation_response(result)

    @app.post(
        "/market-morning/account-deletion-requests",
        response_model=AccountDeletionRequestResponse,
    )
    async def market_morning_account_deletion_request(
        principal: MarketMorningPrincipal = Depends(require_account_deletion_principal),
    ) -> AccountDeletionRequestResponse:
        try:
            result = await request_account_deletion(user_id=principal.user_id)
        except Exception as exc:
            _raise_settings_http_error(exc)
        return _deletion_request_response(result)

    @app.get(
        "/market-morning/account-data-export",
        response_model=AccountDataExportResponse,
    )
    async def market_morning_account_data_export(
        principal: MarketMorningPrincipal = Depends(require_market_morning_principal),
    ) -> AccountDataExportResponse:
        try:
            result = await get_account_data_export(user_id=principal.user_id)
        except Exception as exc:
            _raise_account_privacy_http_error(exc)
        return AccountDataExportResponse(
            schema_version=result.schema_version,
            generated_at=result.generated_at,
            data=result.data,
        )
