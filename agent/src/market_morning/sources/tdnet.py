"""TDnet source adapters.

The fixture remains the default for T0.  The paid API transport is safe at the
code boundary but must not be wired with a real key before the TDnet data-rights
Gate is approved.
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
from src.market_morning.sources.tdnet_api import (
    TdnetApiAdapter,
    build_tdnet_api_adapter_from_env,
)


class TdnetFixtureAdapter(FixtureSourceAdapter):
    """Parses synthetic TDnet rows without owning any network transport."""

    def __init__(
        self,
        *,
        rows: tuple[Mapping[str, Any], ...],
        fetched_at: datetime,
    ) -> None:
        documents = tuple(self._decode_row(row) for row in rows)
        super().__init__(
            provider="fixture_tdnet",
            documents=documents,
            fetched_at=fetched_at,
        )

    @staticmethod
    def _decode_row(row: Mapping[str, Any]) -> FixtureSourceDocument:
        status = required_text(row, "status")
        try:
            lifecycle = SourceLifecycleStatus(status)
        except ValueError as error:
            raise SourceValidationError("status is not a supported lifecycle") from error
        return FixtureSourceDocument(
            document_id=required_text(row, "disclosure_id"),
            revision_key=required_text(row, "revision"),
            original_url=required_text(row, "original_url"),
            raw_content=raw_bytes(row),
            title=required_text(row, "title"),
            document_type=required_text(row, "document_type"),
            published_at=aware_datetime(row, "published_at"),
            lifecycle_status=lifecycle,
            issuer_codes=issuer_codes(row),
            metadata={"fixture": True, "source_status": status},
        )


__all__ = [
    "TdnetApiAdapter",
    "TdnetFixtureAdapter",
    "build_tdnet_api_adapter_from_env",
]
