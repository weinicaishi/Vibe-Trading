"""Validation helpers for already-decoded, offline provider fixtures."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from src.market_morning.sources.base import SourceValidationError


def required_text(row: Mapping[str, Any], field_name: str) -> str:
    value = row.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise SourceValidationError(f"{field_name} is required")
    return value.strip()


def aware_datetime(row: Mapping[str, Any], field_name: str) -> datetime:
    value = row.get(field_name)
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise SourceValidationError(f"{field_name} must be ISO-8601") from error
    if not isinstance(value, datetime):
        raise SourceValidationError(f"{field_name} must be an ISO-8601 datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise SourceValidationError(f"{field_name} must be timezone-aware")
    return value


def raw_bytes(row: Mapping[str, Any]) -> bytes:
    value = row.get("raw_content")
    if isinstance(value, str):
        value = value.encode("utf-8")
    if not isinstance(value, bytes) or not value:
        raise SourceValidationError("raw_content must not be empty")
    return value


def issuer_codes(row: Mapping[str, Any]) -> tuple[str, ...]:
    value = row.get("issuer_codes")
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise SourceValidationError("issuer_codes must be a non-empty sequence")
    normalized = tuple(str(item).strip() for item in value if str(item).strip())
    if not normalized:
        raise SourceValidationError("issuer_codes must be a non-empty sequence")
    return normalized


__all__ = ["aware_datetime", "issuer_codes", "raw_bytes", "required_text"]
