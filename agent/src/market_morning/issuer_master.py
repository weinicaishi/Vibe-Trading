"""Deterministic contracts for issuer-master snapshots and diffs.

This module deliberately accepts already-decoded tabular rows.  A licensed
source adapter is responsible for downloading and decoding its workbook or
CSV into mappings; the domain layer must not depend on one provider's current
file layout.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from typing import Any

from src.market_morning.normalization import normalize_issuer_search_key


# Since 2024, newly assigned Japanese issue codes can contain letters in the
# second and/or fourth position. B, E, I, O, Q, V and Z are excluded by SICC.
_ISSUE_CODE_PATTERN = re.compile(r"^[0-9][0-9ACDFGHJKLMNPRSTUWXY][0-9][0-9ACDFGHJKLMNPRSTUWXY]$")


class IssuerMasterValidationError(ValueError):
    """Raised when an issuer snapshot is incomplete or internally ambiguous."""


def normalize_issuer_code(value: object) -> str:
    """Normalize and validate a four-character Japanese issue code."""
    normalized = unicodedata.normalize("NFKC", str(value or "")).strip().upper()
    if not _ISSUE_CODE_PATTERN.fullmatch(normalized):
        raise IssuerMasterValidationError(
            "issuer code must be four characters and follow the JPX/SICC letter-position rules"
        )
    return normalized


@dataclass(frozen=True, slots=True)
class IssuerSourceColumns:
    """Explicit source-column mapping selected by a licensed adapter."""

    issuer_code: str = "issuer_code"
    legal_name_ja: str = "legal_name_ja"
    market_segment: str = "market_segment"


@dataclass(frozen=True, slots=True)
class IssuerMasterRecord:
    issuer_code: str
    legal_name_ja: str
    normalized_search_key: str
    market_segment: str


@dataclass(frozen=True, slots=True)
class IssuerSnapshotMetadata:
    source_provider: str
    source_version: str
    source_url: str
    snapshot_date: date
    fetched_at: datetime
    checksum_sha256: str


class IssuerChangeType(StrEnum):
    ADDED = "added"
    DELISTED = "delisted"
    LEGAL_NAME_CHANGED = "legal_name_changed"
    MARKET_SEGMENT_CHANGED = "market_segment_changed"


@dataclass(frozen=True, slots=True)
class IssuerMasterChange:
    change_type: IssuerChangeType
    issuer_code: str
    previous: IssuerMasterRecord | None
    current: IssuerMasterRecord | None


def build_snapshot_metadata(
    *,
    source_provider: str,
    source_version: str,
    source_url: str,
    snapshot_date: date,
    fetched_at: datetime,
    raw_content: bytes,
) -> IssuerSnapshotMetadata:
    """Build auditable metadata without persisting or logging source bytes."""
    provider = source_provider.strip()
    version = source_version.strip()
    url = source_url.strip()
    if not provider or not version or not url:
        raise IssuerMasterValidationError("snapshot provider, version and source URL are required")
    if fetched_at.tzinfo is None or fetched_at.utcoffset() is None:
        raise IssuerMasterValidationError("fetched_at must be timezone-aware")
    if not raw_content:
        raise IssuerMasterValidationError("snapshot source content must not be empty")
    return IssuerSnapshotMetadata(
        source_provider=provider,
        source_version=version,
        source_url=url,
        snapshot_date=snapshot_date,
        fetched_at=fetched_at,
        checksum_sha256=hashlib.sha256(raw_content).hexdigest(),
    )


def parse_issuer_rows(
    rows: Iterable[Mapping[str, Any]],
    *,
    columns: IssuerSourceColumns = IssuerSourceColumns(),
) -> tuple[IssuerMasterRecord, ...]:
    """Validate decoded source rows and return a code-sorted snapshot.

    Duplicate codes fail the entire snapshot.  Silently accepting the last row
    would make source corrections and malformed files indistinguishable.
    """
    records: dict[str, IssuerMasterRecord] = {}
    for row_number, row in enumerate(rows, start=2):
        try:
            code = normalize_issuer_code(row.get(columns.issuer_code))
            legal_name = unicodedata.normalize(
                "NFKC", str(row.get(columns.legal_name_ja) or "")
            ).strip()
            market_segment = unicodedata.normalize(
                "NFKC", str(row.get(columns.market_segment) or "")
            ).strip()
        except IssuerMasterValidationError as exc:
            raise IssuerMasterValidationError(f"row {row_number}: {exc}") from exc
        if not legal_name or not market_segment:
            raise IssuerMasterValidationError(
                f"row {row_number}: legal name and market segment are required"
            )
        search_key = normalize_issuer_search_key(legal_name)
        if not search_key:
            raise IssuerMasterValidationError(f"row {row_number}: legal name normalizes to empty")
        if code in records:
            raise IssuerMasterValidationError(f"row {row_number}: duplicate issuer code {code}")
        records[code] = IssuerMasterRecord(
            issuer_code=code,
            legal_name_ja=legal_name,
            normalized_search_key=search_key,
            market_segment=market_segment,
        )
    if not records:
        raise IssuerMasterValidationError("issuer snapshot contains no records")
    return tuple(records[code] for code in sorted(records))


def diff_issuer_master(
    previous: Iterable[IssuerMasterRecord],
    current: Iterable[IssuerMasterRecord],
) -> tuple[IssuerMasterChange, ...]:
    """Report additions, delistings, legal-name changes and segment changes."""
    previous_by_code = _unique_by_code(previous, label="previous")
    current_by_code = _unique_by_code(current, label="current")
    changes: list[IssuerMasterChange] = []

    for code in sorted(previous_by_code.keys() | current_by_code.keys()):
        old = previous_by_code.get(code)
        new = current_by_code.get(code)
        if old is None:
            changes.append(IssuerMasterChange(IssuerChangeType.ADDED, code, None, new))
            continue
        if new is None:
            changes.append(IssuerMasterChange(IssuerChangeType.DELISTED, code, old, None))
            continue
        if old.legal_name_ja != new.legal_name_ja:
            changes.append(
                IssuerMasterChange(IssuerChangeType.LEGAL_NAME_CHANGED, code, old, new)
            )
        if old.market_segment != new.market_segment:
            changes.append(
                IssuerMasterChange(IssuerChangeType.MARKET_SEGMENT_CHANGED, code, old, new)
            )
    return tuple(changes)


def _unique_by_code(
    records: Iterable[IssuerMasterRecord], *, label: str
) -> dict[str, IssuerMasterRecord]:
    indexed: dict[str, IssuerMasterRecord] = {}
    for record in records:
        if record.issuer_code in indexed:
            raise IssuerMasterValidationError(
                f"{label} issuer master contains duplicate code {record.issuer_code}"
            )
        indexed[record.issuer_code] = record
    return indexed


__all__ = [
    "IssuerChangeType",
    "IssuerMasterChange",
    "IssuerMasterRecord",
    "IssuerMasterValidationError",
    "IssuerSnapshotMetadata",
    "IssuerSourceColumns",
    "build_snapshot_metadata",
    "diff_issuer_master",
    "normalize_issuer_code",
    "parse_issuer_rows",
]
