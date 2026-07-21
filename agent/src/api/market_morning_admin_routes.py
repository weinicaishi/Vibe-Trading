"""API-key-protected Market Morning operational and content-review routes."""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Literal, Never

from fastapi import Depends, FastAPI, HTTPException, Path, Query, Response, status
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from src.config.accessor import get_env_config
from src.market_morning.content_reports import (
    ContentReportConflict,
    ContentReportNotFound,
    apply_content_report_review,
    get_content_report_queue,
)
from src.market_morning.beta_access import (
    BetaAccessConflict,
    BetaAccessValidationError,
    create_invite,
    mutate_user_access,
    revoke_invite,
)
from src.market_morning.db import MarketMorningDatabaseNotConfigured
from src.market_morning.event_brief_review import (
    EventBriefReviewConflict,
    EventBriefReviewNotFound,
    apply_event_brief_review,
    get_event_brief_review_queue,
)
from src.market_morning.operations import (
    get_operations_summary,
    render_operations_openmetrics,
)
from src.market_morning.operation_controls import (
    create_publication_halt_control,
    revoke_publication_halt_control,
)
from src.market_morning.repositories.manual_overrides import (
    PublicationOverrideConflict,
)
from src.api.market_morning_admin_auth import (
    VerifiedMarketMorningOperator,
    require_access_manager,
    require_content_reviewer,
    require_operations_reader,
    require_publication_controller,
)
from src.market_morning.review_queues import (
    ReviewQueueConflict,
    ReviewQueueNotFound,
    apply_event_merge_candidate_review,
    apply_issuer_alias_review,
    get_event_merge_review_queue,
    get_issuer_alias_review_queue,
)

logger = logging.getLogger(__name__)


class _AdminResponseModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class OperationsSourceHealthResponse(_AdminResponseModel):
    provider: str
    status: str
    last_successful_discovery_at: datetime | None
    last_error_at: datetime | None
    last_error_code: str | None


class OperationsGlobalRunResponse(_AdminResponseModel):
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


class OperationsEventBriefCountResponse(_AdminResponseModel):
    status: str
    review_status: str
    count: int


class OperationsHaltResponse(_AdminResponseModel):
    edition_date: date
    reason_code: str
    created_at: datetime


class OperationsAlertResponse(_AdminResponseModel):
    code: str
    severity: Literal["warning", "critical"]
    count: int


class OperationsModelUsageResponse(_AdminResponseModel):
    invocation_count: int
    usage_missing_count: int
    unpriced_count: int
    input_tokens: int
    output_tokens: int
    costs_by_currency: dict[str, int]


class OperationsSummaryResponse(_AdminResponseModel):
    generated_at: datetime
    window_started_at: datetime
    cost_observability: Literal[
        "no_invocations",
        "usage_incomplete",
        "cost_incomplete",
        "complete",
    ]
    event_brief_generation_attempt_count: int
    model_usage: OperationsModelUsageResponse
    sources: list[OperationsSourceHealthResponse]
    job_counts: dict[str, int]
    job_failure_counts: dict[str, int]
    global_runs: list[OperationsGlobalRunResponse]
    event_brief_counts: list[OperationsEventBriefCountResponse]
    delivery_counts: dict[str, int]
    engagement_counts: dict[str, int]
    active_halts: list[OperationsHaltResponse]
    alerts: list[OperationsAlertResponse]


class EventBriefReviewQueueItemResponse(_AdminResponseModel):
    brief_id: str
    event_id: str
    event_version: int
    issuer_id: str
    issuer_code: str
    legal_name_ja: str
    event_title: str
    status: str
    review_status: str
    model_version: str
    prompt_version: str
    attempt_count: int
    source_count: int
    validation_errors: list[str]
    last_failure_code: str | None
    completed_at: datetime | None
    published_at: datetime | None
    updated_at: datetime


class EventBriefReviewQueueResponse(BaseModel):
    items: list[EventBriefReviewQueueItemResponse]
    count: int


class EventBriefReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["approve", "reject"]
    reason_code: str = Field(min_length=1, max_length=64)


class EventBriefReviewMutationResponse(_AdminResponseModel):
    status: Literal["reviewed", "already_reviewed"]
    brief_id: str
    review_status: Literal["approved", "rejected"]
    reviewed_at: datetime


class IssuerAliasReviewQueueItemResponse(_AdminResponseModel):
    alias_id: str
    issuer_id: str
    issuer_code: str
    legal_name_ja: str
    display_alias: str
    normalized_alias: str
    source_type: str
    review_status: Literal["pending", "approved", "rejected"]
    effective_from: date
    effective_to: date | None
    reviewed_by: str | None
    reviewed_at: datetime | None
    created_at: datetime


class IssuerAliasReviewQueueResponse(BaseModel):
    items: list[IssuerAliasReviewQueueItemResponse]
    count: int


class IssuerAliasReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["approve", "reject"]
    reason_code: Literal[
        "verified_company_name",
        "ambiguous_alias",
        "wrong_issuer",
        "unsupported_source",
    ]

    @model_validator(mode="after")
    def validate_decision_reason_pair(self) -> "IssuerAliasReviewRequest":
        valid = (
            self.decision == "approve"
            and self.reason_code == "verified_company_name"
        ) or (
            self.decision == "reject"
            and self.reason_code
            in {"ambiguous_alias", "wrong_issuer", "unsupported_source"}
        )
        if not valid:
            raise ValueError("issuer alias decision and reason do not match")
        return self


class IssuerAliasReviewMutationResponse(_AdminResponseModel):
    status: Literal["reviewed", "already_reviewed"]
    alias_id: str
    review_status: Literal["approved", "rejected"]
    reviewed_at: datetime


class EventMergeReviewQueueItemResponse(_AdminResponseModel):
    candidate_id: str
    issuer_id: str
    issuer_code: str
    legal_name_ja: str
    left_event_id: str
    left_event_title: str
    left_event_type: str
    left_occurred_at: datetime
    right_event_id: str
    right_event_title: str
    right_event_type: str
    right_occurred_at: datetime
    reason: str
    title_similarity: float
    time_distance_seconds: int
    review_status: Literal["pending", "approved", "rejected"]
    reviewed_by: str | None
    reviewed_at: datetime | None
    created_at: datetime


class EventMergeReviewQueueResponse(BaseModel):
    items: list[EventMergeReviewQueueItemResponse]
    count: int


class EventMergeReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["approve", "reject"]
    reason_code: Literal[
        "same_disclosure_event",
        "distinct_events",
        "insufficient_evidence",
    ]

    @model_validator(mode="after")
    def validate_decision_reason_pair(self) -> "EventMergeReviewRequest":
        valid = (
            self.decision == "approve"
            and self.reason_code == "same_disclosure_event"
        ) or (
            self.decision == "reject"
            and self.reason_code in {"distinct_events", "insufficient_evidence"}
        )
        if not valid:
            raise ValueError("event merge decision and reason do not match")
        return self


class EventMergeReviewMutationResponse(_AdminResponseModel):
    status: Literal["reviewed", "already_reviewed"]
    candidate_id: str
    review_status: Literal["approved", "rejected"]
    reviewed_at: datetime


class ContentReportQueueItemResponse(_AdminResponseModel):
    report_id: str
    edition_date: date
    event_id: str
    issuer_id: str
    issuer_code: str
    legal_name_ja: str
    event_title: str
    event_type: str
    reason_code: str
    report_status: Literal["pending", "resolved", "dismissed"]
    resolution_code: str | None
    reviewed_by: str | None
    reviewed_at: datetime | None
    created_at: datetime
    updated_at: datetime


class ContentReportQueueResponse(BaseModel):
    items: list[ContentReportQueueItemResponse]
    count: int


class ContentReportReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["resolve", "dismiss"]
    resolution_code: Literal[
        "brief_rejected",
        "source_corrected",
        "content_revised",
        "duplicate_report",
        "no_issue_found",
        "insufficient_evidence",
    ]


class ContentReportReviewMutationResponse(_AdminResponseModel):
    status: Literal["reviewed", "already_reviewed"]
    report_id: str
    report_status: Literal["resolved", "dismissed"]
    resolution_code: str
    reviewed_at: datetime


class PublicationHaltRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    edition_date: date
    reason_code: Literal[
        "source_incident",
        "market_data_incident",
        "content_quality_incident",
        "delivery_incident",
        "operator_review",
    ]


class PublicationHaltResponse(_AdminResponseModel):
    override_id: str
    edition_date: date
    reason_code: str


class PublicationHaltMutationResponse(BaseModel):
    status: Literal["created", "already_active", "revoked", "not_found"]
    override: PublicationHaltResponse | None


class PrivateBetaInviteCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expires_in_days: int = Field(ge=1, le=30)


class PrivateBetaInviteCreateResponse(BaseModel):
    status: Literal["created"]
    invite_id: str
    raw_token: str
    expires_at: datetime


class PrivateBetaInviteMutationResponse(BaseModel):
    status: Literal["revoked", "already_revoked"]
    invite_id: str
    user_id: str | None
    expires_at: datetime


class UserAccessMutationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["suspend", "reactivate"]
    reason_code: Literal[
        "beta_access_revoked",
        "security_incident",
        "user_request",
        "support_resolution",
    ]


class UserAccessMutationResponse(BaseModel):
    status: Literal[
        "suspended",
        "already_suspended",
        "reactivated",
        "already_active",
    ]
    user_id: str
    account_status: Literal["active", "suspended"]


def _require_enabled() -> None:
    if not get_env_config().market_morning.enabled:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Market Morning is disabled",
        )


def _raise_admin_http_error(exc: Exception) -> Never:
    if isinstance(exc, BetaAccessConflict):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Private beta access transition is unavailable",
        ) from exc
    if isinstance(exc, BetaAccessValidationError):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Private beta access request is invalid",
        ) from exc
    if isinstance(exc, EventBriefReviewNotFound):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="EventBrief is unavailable",
        ) from exc
    if isinstance(exc, EventBriefReviewConflict):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="EventBrief review transition is not allowed",
        ) from exc
    if isinstance(exc, ReviewQueueNotFound):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Review record is unavailable",
        ) from exc
    if isinstance(exc, ReviewQueueConflict):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Review transition is not allowed",
        ) from exc
    if isinstance(exc, ContentReportNotFound):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Content report is unavailable",
        ) from exc
    if isinstance(exc, ContentReportConflict):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Content report transition is not allowed",
        ) from exc
    if isinstance(exc, PublicationOverrideConflict):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A different publication halt is already active",
        ) from exc
    if isinstance(exc, ValueError):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Market Morning operations request is invalid",
        ) from exc
    if isinstance(
        exc,
        (
            IntegrityError,
            MarketMorningDatabaseNotConfigured,
            SQLAlchemyError,
        ),
    ):
        logger.warning(
            "Market Morning operations request failed: %s",
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Market Morning operations are temporarily unavailable",
        ) from exc
    raise exc


def register_market_morning_admin_routes(app: FastAPI) -> None:
    """Register least-privilege operator routes without opening product auth."""

    @app.get(
        "/market-morning/_internal/operations/summary",
        response_model=OperationsSummaryResponse,
    )
    async def market_morning_operations_summary(
        hours: int = Query(default=24, ge=1, le=168),
        recent_run_limit: int = Query(default=10, ge=1, le=30),
        _operator: VerifiedMarketMorningOperator = Depends(
            require_operations_reader
        ),
    ) -> OperationsSummaryResponse:
        _require_enabled()
        try:
            result = await get_operations_summary(
                hours=hours,
                recent_run_limit=recent_run_limit,
            )
        except Exception as exc:
            _raise_admin_http_error(exc)
        return OperationsSummaryResponse.model_validate(result)

    @app.get(
        "/market-morning/_internal/operations/metrics",
        response_class=Response,
    )
    async def market_morning_operations_metrics(
        hours: int = Query(default=24, ge=1, le=168),
        _operator: VerifiedMarketMorningOperator = Depends(
            require_operations_reader
        ),
    ) -> Response:
        _require_enabled()
        try:
            result = await get_operations_summary(
                hours=hours,
                recent_run_limit=10,
            )
            content = render_operations_openmetrics(result)
        except Exception as exc:
            _raise_admin_http_error(exc)
        return Response(
            content=content,
            headers={
                "Content-Type": (
                    "application/openmetrics-text; version=1.0.0; charset=utf-8"
                )
            },
        )

    @app.get(
        "/market-morning/_internal/event-briefs",
        response_model=EventBriefReviewQueueResponse,
    )
    async def market_morning_event_brief_review_queue(
        review_status: Literal[
            "pending", "auto_validated", "approved", "rejected"
        ]
        | None = Query(default=None),
        limit: int = Query(default=50, ge=1, le=100),
        _operator: VerifiedMarketMorningOperator = Depends(
            require_operations_reader
        ),
    ) -> EventBriefReviewQueueResponse:
        _require_enabled()
        try:
            items = await get_event_brief_review_queue(
                review_status=review_status,
                limit=limit,
            )
        except Exception as exc:
            _raise_admin_http_error(exc)
        return EventBriefReviewQueueResponse(
            items=[
                EventBriefReviewQueueItemResponse.model_validate(item)
                for item in items
            ],
            count=len(items),
        )

    @app.post(
        "/market-morning/_internal/event-briefs/{brief_id}/review",
        response_model=EventBriefReviewMutationResponse,
    )
    async def market_morning_event_brief_review(
        payload: EventBriefReviewRequest,
        brief_id: str = Path(..., min_length=36, max_length=36),
        operator: VerifiedMarketMorningOperator = Depends(
            require_content_reviewer
        ),
    ) -> EventBriefReviewMutationResponse:
        _require_enabled()
        try:
            result = await apply_event_brief_review(
                brief_id=brief_id,
                decision=payload.decision,
                reason_code=payload.reason_code,
                actor_reference=operator.actor_reference,
            )
        except Exception as exc:
            _raise_admin_http_error(exc)
        return EventBriefReviewMutationResponse.model_validate(result)

    @app.get(
        "/market-morning/_internal/issuer-aliases",
        response_model=IssuerAliasReviewQueueResponse,
    )
    async def market_morning_issuer_alias_review_queue(
        review_status: Literal["pending", "approved", "rejected"]
        | None = Query(default="pending"),
        limit: int = Query(default=50, ge=1, le=100),
        _operator: VerifiedMarketMorningOperator = Depends(
            require_operations_reader
        ),
    ) -> IssuerAliasReviewQueueResponse:
        _require_enabled()
        try:
            items = await get_issuer_alias_review_queue(
                review_status=review_status,
                limit=limit,
            )
        except Exception as exc:
            _raise_admin_http_error(exc)
        return IssuerAliasReviewQueueResponse(
            items=[
                IssuerAliasReviewQueueItemResponse.model_validate(item)
                for item in items
            ],
            count=len(items),
        )

    @app.post(
        "/market-morning/_internal/issuer-aliases/{alias_id}/review",
        response_model=IssuerAliasReviewMutationResponse,
    )
    async def market_morning_issuer_alias_review(
        payload: IssuerAliasReviewRequest,
        alias_id: str = Path(..., min_length=36, max_length=36),
        operator: VerifiedMarketMorningOperator = Depends(
            require_content_reviewer
        ),
    ) -> IssuerAliasReviewMutationResponse:
        _require_enabled()
        try:
            result = await apply_issuer_alias_review(
                alias_id=alias_id,
                decision=payload.decision,
                reason_code=payload.reason_code,
                actor_reference=operator.actor_reference,
            )
        except Exception as exc:
            _raise_admin_http_error(exc)
        return IssuerAliasReviewMutationResponse.model_validate(result)

    @app.get(
        "/market-morning/_internal/event-merge-candidates",
        response_model=EventMergeReviewQueueResponse,
    )
    async def market_morning_event_merge_review_queue(
        review_status: Literal["pending", "approved", "rejected"]
        | None = Query(default="pending"),
        limit: int = Query(default=50, ge=1, le=100),
        _operator: VerifiedMarketMorningOperator = Depends(
            require_operations_reader
        ),
    ) -> EventMergeReviewQueueResponse:
        _require_enabled()
        try:
            items = await get_event_merge_review_queue(
                review_status=review_status,
                limit=limit,
            )
        except Exception as exc:
            _raise_admin_http_error(exc)
        return EventMergeReviewQueueResponse(
            items=[
                EventMergeReviewQueueItemResponse.model_validate(item)
                for item in items
            ],
            count=len(items),
        )

    @app.post(
        "/market-morning/_internal/event-merge-candidates/{candidate_id}/review",
        response_model=EventMergeReviewMutationResponse,
    )
    async def market_morning_event_merge_review(
        payload: EventMergeReviewRequest,
        candidate_id: str = Path(..., min_length=36, max_length=36),
        operator: VerifiedMarketMorningOperator = Depends(
            require_content_reviewer
        ),
    ) -> EventMergeReviewMutationResponse:
        _require_enabled()
        try:
            result = await apply_event_merge_candidate_review(
                candidate_id=candidate_id,
                decision=payload.decision,
                reason_code=payload.reason_code,
                actor_reference=operator.actor_reference,
            )
        except Exception as exc:
            _raise_admin_http_error(exc)
        return EventMergeReviewMutationResponse.model_validate(result)

    @app.get(
        "/market-morning/_internal/content-reports",
        response_model=ContentReportQueueResponse,
    )
    async def market_morning_content_report_queue(
        report_status: Literal["pending", "resolved", "dismissed"]
        | None = Query(default=None),
        limit: int = Query(default=50, ge=1, le=100),
        _operator: VerifiedMarketMorningOperator = Depends(
            require_operations_reader
        ),
    ) -> ContentReportQueueResponse:
        _require_enabled()
        try:
            items = await get_content_report_queue(
                report_status=report_status,
                limit=limit,
            )
        except Exception as exc:
            _raise_admin_http_error(exc)
        return ContentReportQueueResponse(
            items=[ContentReportQueueItemResponse.model_validate(item) for item in items],
            count=len(items),
        )

    @app.post(
        "/market-morning/_internal/content-reports/{report_id}/review",
        response_model=ContentReportReviewMutationResponse,
    )
    async def market_morning_content_report_review(
        payload: ContentReportReviewRequest,
        report_id: str = Path(..., min_length=36, max_length=36),
        operator: VerifiedMarketMorningOperator = Depends(
            require_content_reviewer
        ),
    ) -> ContentReportReviewMutationResponse:
        _require_enabled()
        try:
            result = await apply_content_report_review(
                report_id=report_id,
                decision=payload.decision,
                resolution_code=payload.resolution_code,
                actor_reference=operator.actor_reference,
            )
        except Exception as exc:
            _raise_admin_http_error(exc)
        return ContentReportReviewMutationResponse.model_validate(result)

    @app.post(
        "/market-morning/_internal/operations/halts",
        response_model=PublicationHaltMutationResponse,
    )
    async def market_morning_publication_halt_create(
        payload: PublicationHaltRequest,
        operator: VerifiedMarketMorningOperator = Depends(
            require_publication_controller
        ),
    ) -> PublicationHaltMutationResponse:
        _require_enabled()
        try:
            result = await create_publication_halt_control(
                edition_date=payload.edition_date,
                reason_code=payload.reason_code,
                actor_reference=operator.actor_reference,
            )
        except Exception as exc:
            _raise_admin_http_error(exc)
        return PublicationHaltMutationResponse(
            status=result.status.value,
            override=(
                PublicationHaltResponse.model_validate(result.override)
                if result.override
                else None
            ),
        )

    @app.delete(
        "/market-morning/_internal/operations/halts/{edition_date}",
        response_model=PublicationHaltMutationResponse,
    )
    async def market_morning_publication_halt_revoke(
        edition_date: date,
        operator: VerifiedMarketMorningOperator = Depends(
            require_publication_controller
        ),
    ) -> PublicationHaltMutationResponse:
        _require_enabled()
        try:
            result = await revoke_publication_halt_control(
                edition_date=edition_date,
                actor_reference=operator.actor_reference,
            )
        except Exception as exc:
            _raise_admin_http_error(exc)
        return PublicationHaltMutationResponse(
            status=result.status.value,
            override=(
                PublicationHaltResponse.model_validate(result.override)
                if result.override
                else None
            ),
        )

    @app.post(
        "/market-morning/_internal/private-beta/invites",
        response_model=PrivateBetaInviteCreateResponse,
    )
    async def market_morning_private_beta_invite_create(
        payload: PrivateBetaInviteCreateRequest,
        operator: VerifiedMarketMorningOperator = Depends(require_access_manager),
    ) -> PrivateBetaInviteCreateResponse:
        _require_enabled()
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        try:
            result = await create_invite(
                actor_reference=operator.actor_reference,
                expires_at=now + timedelta(days=payload.expires_in_days),
                now=now,
            )
        except Exception as exc:
            _raise_admin_http_error(exc)
        return PrivateBetaInviteCreateResponse(
            status=result.status.value,
            invite_id=result.invite_id,
            raw_token=result.raw_token,
            expires_at=result.expires_at,
        )

    @app.delete(
        "/market-morning/_internal/private-beta/invites/{invite_id}",
        response_model=PrivateBetaInviteMutationResponse,
    )
    async def market_morning_private_beta_invite_revoke(
        invite_id: str = Path(..., min_length=36, max_length=36),
        operator: VerifiedMarketMorningOperator = Depends(require_access_manager),
    ) -> PrivateBetaInviteMutationResponse:
        _require_enabled()
        try:
            result = await revoke_invite(
                invite_id=invite_id,
                actor_reference=operator.actor_reference,
            )
        except Exception as exc:
            _raise_admin_http_error(exc)
        return PrivateBetaInviteMutationResponse(
            status=result.status.value,
            invite_id=result.invite_id,
            user_id=result.user_id,
            expires_at=result.expires_at,
        )

    @app.post(
        "/market-morning/_internal/users/{user_id}/access",
        response_model=UserAccessMutationResponse,
    )
    async def market_morning_user_access_mutate(
        payload: UserAccessMutationRequest,
        user_id: str = Path(..., min_length=36, max_length=36),
        operator: VerifiedMarketMorningOperator = Depends(require_access_manager),
    ) -> UserAccessMutationResponse:
        _require_enabled()
        try:
            result = await mutate_user_access(
                user_id=user_id,
                action=payload.action,
                reason_code=payload.reason_code,
                actor_reference=operator.actor_reference,
            )
        except Exception as exc:
            _raise_admin_http_error(exc)
        return UserAccessMutationResponse(
            status=result.status.value,
            user_id=result.user_id,
            account_status=result.account_status,
        )


__all__ = ["register_market_morning_admin_routes"]
