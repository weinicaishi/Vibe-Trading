"""Offline company-IR fixture adapter boundary.

Only explicitly whitelisted synthetic company feeds belong in this module.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

from src.market_morning.sources._decoded_fixture import (
    aware_datetime,
    raw_bytes,
    required_text,
)
from src.market_morning.sources.base import (
    SourceHealth,
    SourceLifecycleStatus,
    SourceValidationError,
)
from src.market_morning.sources.fixture import FixtureSourceAdapter, FixtureSourceDocument

_PROVIDER_KEY = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")


@dataclass(frozen=True, slots=True)
class CompanyIrFeedConfig:
    issuer_code: str
    provider_key: str
    allowed_base_url: str

    def __post_init__(self) -> None:
        issuer_code = self.issuer_code.strip()
        provider_key = self.provider_key.strip().lower()
        if not issuer_code or not issuer_code.isdigit():
            raise SourceValidationError("issuer_code must contain digits")
        if _PROVIDER_KEY.fullmatch(provider_key) is None:
            raise SourceValidationError("provider_key is invalid")
        allowed = urlsplit(self.allowed_base_url)
        if allowed.scheme != "https" or not allowed.hostname:
            raise SourceValidationError("allowed_base_url must be an HTTPS URL")
        if not allowed.path.endswith("/"):
            raise SourceValidationError("allowed_base_url path must end with /")
        object.__setattr__(self, "issuer_code", issuer_code)
        object.__setattr__(self, "provider_key", provider_key)


class CompanyIrFixtureAdapter(FixtureSourceAdapter):
    """One isolated, explicitly whitelisted synthetic company IR feed."""

    def __init__(
        self,
        *,
        config: CompanyIrFeedConfig,
        rows: tuple[Mapping[str, Any], ...],
        fetched_at: datetime,
        available: bool = True,
    ) -> None:
        self.config = config
        self._available = available
        documents = tuple(self._decode_row(row) for row in rows)
        super().__init__(
            provider=f"fixture_company_ir_{config.provider_key}",
            documents=documents,
            fetched_at=fetched_at,
        )

    def _decode_row(self, row: Mapping[str, Any]) -> FixtureSourceDocument:
        original_url = required_text(row, "original_url")
        if not self._is_allowed_url(original_url):
            raise SourceValidationError("company IR URL is outside the whitelist")
        return FixtureSourceDocument(
            document_id=required_text(row, "document_id"),
            revision_key=required_text(row, "revision"),
            original_url=original_url,
            raw_content=raw_bytes(row),
            title=required_text(row, "title"),
            document_type=required_text(row, "document_type"),
            published_at=aware_datetime(row, "published_at"),
            lifecycle_status=SourceLifecycleStatus.ACTIVE,
            issuer_codes=(self.config.issuer_code,),
            metadata={"fixture": True, "company_ir_provider": self.config.provider_key},
        )

    def _is_allowed_url(self, candidate_url: str) -> bool:
        allowed = urlsplit(self.config.allowed_base_url)
        candidate = urlsplit(candidate_url)
        return (
            candidate.scheme == "https"
            and candidate.hostname is not None
            and candidate.hostname.lower() == allowed.hostname.lower()
            and candidate.port == allowed.port
            and candidate.path.startswith(allowed.path)
            and candidate.username is None
            and candidate.password is None
        )

    async def health(self) -> SourceHealth:
        if self._available:
            return await super().health()
        return SourceHealth(
            ok=False,
            checked_at=self._fetched_at,
            detail_code="synthetic_fixture_unavailable",
        )


__all__ = ["CompanyIrFeedConfig", "CompanyIrFixtureAdapter"]
