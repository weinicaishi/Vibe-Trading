"""Fail-closed short-transaction orchestration for reminder email delivery."""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Protocol
from urllib.parse import urlsplit
from uuid import UUID

from sqlalchemy import distinct, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.market_morning.db import get_session_factory
from src.market_morning.delivery_eligibility import (
    DELIVERY_ACTIVITY_WINDOW,
    DELIVERY_ELIGIBLE_SUBSCRIPTION_STATUSES,
    DELIVERY_MINIMUM_ACTIVE_ISSUERS,
)
from src.market_morning.models import (
    DeliveryAttemptRecord,
    GlobalEditionRunRecord,
    Issuer,
    User,
    WatchlistItem,
)
from src.market_morning.repositories.email_delivery import (
    DeliveryLifecycleStatus,
    complete_delivery_attempt,
    fail_delivery_attempt,
    start_delivery_attempt,
    suppress_delivery_attempt,
)

EMAIL_REMINDER_SUBJECT = "本日のMarket Morningが準備できました"
EMAIL_REMINDER_BODY = "安全な専用リンクから本日の朝刊をご確認ください。"


class EmailDeliveryError(RuntimeError):
    pass


class EmailDeliveryRetryable(EmailDeliveryError):
    def __init__(self, error_code: str) -> None:
        self.error_code = error_code
        super().__init__(error_code)


class EmailDeliveryUnavailable(EmailDeliveryError):
    def __init__(self, error_code: str) -> None:
        self.error_code = error_code
        super().__init__(error_code)


class EmailDeliveryPortUnavailable(RuntimeError):
    """Provider failure whose original message must never reach persistence."""


def _uuid(value: str, *, field_name: str) -> str:
    try:
        return str(UUID(value))
    except (ValueError, AttributeError) as error:
        raise ValueError(f"{field_name} must be a UUID") from error


def _required(value: str, *, field_name: str, maximum: int) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > maximum:
        raise ValueError(f"{field_name} must contain 1 to {maximum} characters")
    return normalized


def _aware(value: datetime, *, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value


def _utc_aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _email_destination(value: str) -> str:
    destination = _required(value, field_name="destination", maximum=320)
    if (
        "@" not in destination
        or destination.startswith("@")
        or destination.endswith("@")
    ):
        raise ValueError("destination must be an email address")
    return destination


@dataclass(frozen=True, slots=True)
class EmailDeliveryCommand:
    delivery_attempt_id: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "delivery_attempt_id",
            _uuid(self.delivery_attempt_id, field_name="delivery_attempt_id"),
        )


@dataclass(frozen=True, slots=True)
class EmailDeliveryContext:
    delivery_attempt_id: str
    user_id: str
    external_subject: str
    edition_date: date
    global_run_id: str
    idempotency_key: str
    deep_link_token_sha256: str
    eligible: bool
    suppression_reason: str | None
    delivery_status: DeliveryLifecycleStatus = DeliveryLifecycleStatus.PENDING
    attempt_count: int = 0
    provider_message_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "delivery_attempt_id",
            _uuid(self.delivery_attempt_id, field_name="delivery_attempt_id"),
        )
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
        object.__setattr__(
            self,
            "external_subject",
            _required(
                self.external_subject,
                field_name="external_subject",
                maximum=255,
            ),
        )
        if type(self.edition_date) is not date:
            raise ValueError("edition_date must be a date")
        object.__setattr__(
            self,
            "idempotency_key",
            _required(
                self.idempotency_key,
                field_name="idempotency_key",
                maximum=191,
            ),
        )
        digest = self.deep_link_token_sha256.strip().lower()
        if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
            raise ValueError("deep_link_token_sha256 must be a SHA-256 digest")
        object.__setattr__(self, "deep_link_token_sha256", digest)
        if self.eligible == (self.suppression_reason is not None):
            raise ValueError("eligibility and suppression_reason disagree")
        if self.suppression_reason is not None:
            object.__setattr__(
                self,
                "suppression_reason",
                _required(
                    self.suppression_reason,
                    field_name="suppression_reason",
                    maximum=64,
                ),
            )
        object.__setattr__(
            self,
            "delivery_status",
            DeliveryLifecycleStatus(self.delivery_status),
        )
        if self.attempt_count < 0:
            raise ValueError("attempt_count cannot be negative")


@dataclass(frozen=True, slots=True)
class DeliveryDeepLink:
    url: str
    token: str

    def __post_init__(self) -> None:
        token = _required(self.token, field_name="token", maximum=2_048)
        url = _required(self.url, field_name="url", maximum=4_096)
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname or token not in url:
            raise ValueError("delivery deep link must be HTTPS and contain its token")
        object.__setattr__(self, "token", token)
        object.__setattr__(self, "url", url)


@dataclass(frozen=True, slots=True)
class EmailReminderMessage:
    destination: str
    private_url: str
    idempotency_key: str
    subject: str = EMAIL_REMINDER_SUBJECT
    body: str = EMAIL_REMINDER_BODY

    def __post_init__(self) -> None:
        destination = _email_destination(self.destination)
        parsed = urlsplit(self.private_url)
        if parsed.scheme != "https" or not parsed.hostname:
            raise ValueError("private_url must be an absolute HTTPS URL")
        _required(
            self.idempotency_key,
            field_name="idempotency_key",
            maximum=191,
        )
        if self.subject != EMAIL_REMINDER_SUBJECT or self.body != EMAIL_REMINDER_BODY:
            raise ValueError("reminder content must use the approved fixed template")
        object.__setattr__(self, "destination", destination)


@dataclass(frozen=True, slots=True)
class EmailProviderReceipt:
    provider_message_id: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "provider_message_id",
            _required(
                self.provider_message_id,
                field_name="provider_message_id",
                maximum=191,
            ),
        )


@dataclass(frozen=True, slots=True)
class EmailDeliveryResult:
    delivery_attempt_id: str
    status: DeliveryLifecycleStatus
    attempt_number: int
    provider_message_id: str | None = None
    reason_code: str | None = None


class EmailReminderProvider(Protocol):
    async def __call__(self, message: EmailReminderMessage) -> EmailProviderReceipt: ...


EmailIdentityResolver = Callable[[str, str], Awaitable[str]]
DeliveryLinkBuilder = Callable[[EmailDeliveryContext], Awaitable[DeliveryDeepLink]]


def build_email_delivery_context_statement(delivery_attempt_id: str):
    canonical_id = _uuid(
        delivery_attempt_id, field_name="delivery_attempt_id"
    )
    active_issuer_count = (
        select(func.count(distinct(WatchlistItem.issuer_id)))
        .select_from(WatchlistItem)
        .join(Issuer, Issuer.issuer_id == WatchlistItem.issuer_id)
        .where(
            WatchlistItem.user_id == DeliveryAttemptRecord.user_id,
            WatchlistItem.removed_at.is_(None),
            Issuer.active_status == "active",
            Issuer.effective_to.is_(None),
        )
        .scalar_subquery()
    )
    return (
        select(
            DeliveryAttemptRecord.delivery_attempt_id,
            DeliveryAttemptRecord.user_id,
            DeliveryAttemptRecord.global_run_id,
            DeliveryAttemptRecord.edition_date,
            DeliveryAttemptRecord.idempotency_key,
            DeliveryAttemptRecord.deep_link_token_sha256,
            DeliveryAttemptRecord.status.label("delivery_status"),
            DeliveryAttemptRecord.attempt_count,
            DeliveryAttemptRecord.provider_message_id,
            User.external_subject,
            User.email_opt_in,
            User.account_status,
            User.trial_or_subscription_status,
            User.last_product_activity_at,
            User.deleted_at,
            GlobalEditionRunRecord.edition_date.label("run_edition_date"),
            GlobalEditionRunRecord.status.label("run_status"),
            GlobalEditionRunRecord.is_current,
            GlobalEditionRunRecord.email_permitted,
            GlobalEditionRunRecord.late,
            active_issuer_count.label("active_issuer_count"),
        )
        .select_from(DeliveryAttemptRecord)
        .join(User, User.user_id == DeliveryAttemptRecord.user_id)
        .join(
            GlobalEditionRunRecord,
            GlobalEditionRunRecord.run_id == DeliveryAttemptRecord.global_run_id,
        )
        .where(
            DeliveryAttemptRecord.delivery_attempt_id == canonical_id
        )
    )


def _suppression_reason(row: Any, *, as_of: datetime) -> str | None:
    current = _aware(as_of, field_name="as_of").astimezone(timezone.utc)
    run_ineligible = (
        row["run_edition_date"] != row["edition_date"]
        or row["run_status"] not in {"complete", "partial"}
        or not row["is_current"]
        or not row["email_permitted"]
        or row["late"]
    )
    if run_ineligible:
        return "delivery_run_ineligible"
    last_activity = row["last_product_activity_at"]
    user_ineligible = (
        row["deleted_at"] is not None
        or row["account_status"] != "active"
        or row["trial_or_subscription_status"]
        not in DELIVERY_ELIGIBLE_SUBSCRIPTION_STATUSES
        or not row["email_opt_in"]
        or last_activity is None
        or _utc_aware(last_activity) < current - DELIVERY_ACTIVITY_WINDOW
        or row["active_issuer_count"] < DELIVERY_MINIMUM_ACTIVE_ISSUERS
    )
    return "delivery_user_ineligible" if user_ineligible else None


async def load_email_delivery_context(
    session: AsyncSession,
    *,
    delivery_attempt_id: str,
    as_of: datetime,
) -> EmailDeliveryContext:
    row = (
        await session.execute(
            build_email_delivery_context_statement(delivery_attempt_id)
        )
    ).mappings().one_or_none()
    if row is None:
        raise EmailDeliveryUnavailable("delivery_attempt_unavailable")
    reason = _suppression_reason(row, as_of=as_of)
    return EmailDeliveryContext(
        delivery_attempt_id=row["delivery_attempt_id"],
        user_id=row["user_id"],
        external_subject=row["external_subject"],
        edition_date=row["edition_date"],
        global_run_id=row["global_run_id"],
        idempotency_key=row["idempotency_key"],
        deep_link_token_sha256=row["deep_link_token_sha256"],
        eligible=reason is None,
        suppression_reason=reason,
        delivery_status=DeliveryLifecycleStatus(row["delivery_status"]),
        attempt_count=row["attempt_count"],
        provider_message_id=row["provider_message_id"],
    )


async def _suppress(
    *,
    factory,
    command: EmailDeliveryCommand,
    reason_code: str,
    suppressed_at: datetime,
    attempt_number: int,
) -> EmailDeliveryResult:
    async with factory.begin() as session:
        result = await suppress_delivery_attempt(
            session,
            delivery_attempt_id=command.delivery_attempt_id,
            reason_code=reason_code,
            suppressed_at=suppressed_at,
        )
    return EmailDeliveryResult(
        delivery_attempt_id=result.delivery_attempt_id,
        status=result.status,
        attempt_number=attempt_number,
        reason_code=result.reason_code,
    )


async def _record_transient_failure(
    *,
    factory,
    command: EmailDeliveryCommand,
    failed_at: datetime,
    attempt_number: int,
    error_code: str,
) -> EmailDeliveryResult:
    async with factory.begin() as session:
        result = await fail_delivery_attempt(
            session,
            delivery_attempt_id=command.delivery_attempt_id,
            error_code=error_code,
            failed_at=failed_at,
        )
    if result.retry_permitted:
        raise EmailDeliveryRetryable(error_code)
    return EmailDeliveryResult(
        delivery_attempt_id=command.delivery_attempt_id,
        status=DeliveryLifecycleStatus.FAILED,
        attempt_number=attempt_number,
        reason_code=error_code,
    )


async def run_email_delivery(
    command: EmailDeliveryCommand,
    *,
    identity_resolver: EmailIdentityResolver,
    link_builder: DeliveryLinkBuilder,
    provider: EmailReminderProvider,
    session_factory: async_sessionmaker[AsyncSession] | Any | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> EmailDeliveryResult:
    """Revalidate, send outside transactions, and persist only safe metadata."""

    factory = session_factory or get_session_factory()
    checked_at = clock()
    async with factory.begin() as session:
        context = await load_email_delivery_context(
            session,
            delivery_attempt_id=command.delivery_attempt_id,
            as_of=checked_at,
        )
        if not context.eligible:
            assert context.suppression_reason is not None
            suppressed = await suppress_delivery_attempt(
                session,
                delivery_attempt_id=command.delivery_attempt_id,
                reason_code=context.suppression_reason,
                suppressed_at=checked_at,
            )
            return EmailDeliveryResult(
                delivery_attempt_id=suppressed.delivery_attempt_id,
                status=suppressed.status,
                attempt_number=context.attempt_count,
                reason_code=suppressed.reason_code,
            )
        started = await start_delivery_attempt(
            session,
            delivery_attempt_id=command.delivery_attempt_id,
            started_at=checked_at,
        )
    if not started.can_execute:
        return EmailDeliveryResult(
            delivery_attempt_id=started.delivery_attempt_id,
            status=started.status,
            attempt_number=started.attempt_number,
            provider_message_id=context.provider_message_id,
        )

    try:
        destination = _email_destination(
            await identity_resolver(
                context.user_id,
                context.external_subject,
            )
        )
    except EmailDeliveryPortUnavailable:
        return await _record_transient_failure(
            factory=factory,
            command=command,
            failed_at=clock(),
            attempt_number=started.attempt_number,
            error_code="email_identity_unavailable",
        )
    except (TypeError, ValueError):
        return await _suppress(
            factory=factory,
            command=command,
            reason_code="email_destination_unavailable",
            suppressed_at=clock(),
            attempt_number=started.attempt_number,
        )

    try:
        deep_link = await link_builder(context)
        if not isinstance(deep_link, DeliveryDeepLink):
            raise TypeError("link builder returned an invalid deep link")
        if _sha256(deep_link.token) != context.deep_link_token_sha256:
            raise ValueError("delivery token hash does not match")
    except EmailDeliveryPortUnavailable:
        return await _record_transient_failure(
            factory=factory,
            command=command,
            failed_at=clock(),
            attempt_number=started.attempt_number,
            error_code="delivery_link_unavailable",
        )
    except (TypeError, ValueError):
        return await _suppress(
            factory=factory,
            command=command,
            reason_code="delivery_link_invalid",
            suppressed_at=clock(),
            attempt_number=started.attempt_number,
        )

    try:
        message = EmailReminderMessage(
            destination=destination,
            private_url=deep_link.url,
            idempotency_key=context.idempotency_key,
        )
    except (TypeError, ValueError):
        return await _suppress(
            factory=factory,
            command=command,
            reason_code="delivery_link_invalid",
            suppressed_at=clock(),
            attempt_number=started.attempt_number,
        )

    try:
        receipt = await provider(message)
        if not isinstance(receipt, EmailProviderReceipt):
            raise TypeError("email provider returned an invalid receipt")
    except Exception:
        return await _record_transient_failure(
            factory=factory,
            command=command,
            failed_at=clock(),
            attempt_number=started.attempt_number,
            error_code="email_provider_unavailable",
        )

    async with factory.begin() as session:
        completed = await complete_delivery_attempt(
            session,
            delivery_attempt_id=command.delivery_attempt_id,
            provider_message_id=receipt.provider_message_id,
            sent_at=clock(),
        )
    return EmailDeliveryResult(
        delivery_attempt_id=completed.delivery_attempt_id,
        status=completed.status,
        attempt_number=started.attempt_number,
        provider_message_id=completed.provider_message_id,
    )


__all__ = [
    "DeliveryDeepLink",
    "DeliveryLinkBuilder",
    "EMAIL_REMINDER_BODY",
    "EMAIL_REMINDER_SUBJECT",
    "EmailDeliveryCommand",
    "EmailDeliveryContext",
    "EmailDeliveryPortUnavailable",
    "EmailDeliveryResult",
    "EmailDeliveryRetryable",
    "EmailDeliveryUnavailable",
    "EmailIdentityResolver",
    "EmailProviderReceipt",
    "EmailReminderMessage",
    "EmailReminderProvider",
    "build_email_delivery_context_statement",
    "load_email_delivery_context",
    "run_email_delivery",
]
