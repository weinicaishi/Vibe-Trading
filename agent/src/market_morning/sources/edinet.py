"""EDINET adapters.

The fixture remains the default for T0.  The v2 transport is production-safe
at the code boundary but must not be wired with a real key before the data
rights Gate is approved.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from src.market_morning.sources._decoded_fixture import (
    aware_datetime,
    issuer_codes,
    raw_bytes,
    required_text,
)
from src.market_morning.sources.base import SourceLifecycleStatus, SourceValidationError
from src.market_morning.sources.fixture import FixtureSourceAdapter, FixtureSourceDocument
from src.market_morning.sources.edinet_api import (
    EdinetApiV2Adapter,
    build_edinet_api_v2_adapter_from_env,
)


class EdinetFixtureAdapter(FixtureSourceAdapter):
    """Parses synthetic EDINET metadata; it does not perform XBRL analysis."""

    def __init__(
        self,
        *,
        rows: tuple[Mapping[str, Any], ...],
        fetched_at: datetime,
    ) -> None:
        documents = tuple(self._decode_row(row) for row in rows)
        super().__init__(
            provider="fixture_edinet",
            documents=documents,
            fetched_at=fetched_at,
        )

    @staticmethod
    def _decode_row(row: Mapping[str, Any]) -> FixtureSourceDocument:
        status = row.get("status", "withdrawn" if row.get("withdrawn") is True else "active")
        if not isinstance(status, str):
            raise SourceValidationError("status is not a supported lifecycle")
        try:
            lifecycle = SourceLifecycleStatus(status.strip())
        except ValueError as error:
            raise SourceValidationError("status is not a supported lifecycle") from error
        revision_key = required_text(row, "metadata_updated_at")
        aware_datetime(row, "metadata_updated_at")
        return FixtureSourceDocument(
            document_id=required_text(row, "document_id"),
            revision_key=revision_key,
            original_url=required_text(row, "original_url"),
            raw_content=raw_bytes(row),
            title=required_text(row, "title"),
            document_type=required_text(row, "document_type"),
            published_at=aware_datetime(row, "submitted_at"),
            lifecycle_status=lifecycle,
            issuer_codes=issuer_codes(row),
            metadata={"fixture": True, "metadata_updated_at": revision_key},
        )


__all__ = [
    "EdinetApiV2Adapter",
    "EdinetFixtureAdapter",
    "build_edinet_api_v2_adapter_from_env",
]
