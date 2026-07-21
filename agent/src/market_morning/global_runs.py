"""Validated global edition run specifications and immutable manifests."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from uuid import UUID

from src.market_morning.calendar.service import CalendarScenario
from src.market_morning.market_snapshots import MarketInstrument

_RUN_VERSIONS = {"0700": 1, "0715": 2, "0730": 3, "0800": 4, "0830": 5, "late": 6}
_INSTRUMENT_POSITIONS = {
    instrument: position for position, instrument in enumerate(MarketInstrument)
}


def _required(value: str, *, field_name: str, maximum: int) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > maximum:
        raise ValueError(f"{field_name} must contain 1 to {maximum} characters")
    return normalized


def _aware(value: datetime, *, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value


def scheduled_run_version(attempt_key: str) -> int:
    try:
        return _RUN_VERSIONS[attempt_key]
    except KeyError as error:
        raise ValueError("attempt_key is unsupported") from error


@dataclass(frozen=True, slots=True)
class GlobalEditionRunSpec:
    edition_date: date
    generation_key: str
    attempt_key: str
    scenario: CalendarScenario | str
    email_permitted: bool
    late: bool
    reason_code: str | None
    started_at: datetime

    def __post_init__(self) -> None:
        if type(self.edition_date) is not date:
            raise ValueError("edition_date must be a date")
        attempt_key = _required(self.attempt_key, field_name="attempt_key", maximum=16)
        scheduled_run_version(attempt_key)
        if (attempt_key == "late") != self.late:
            raise ValueError("late must agree with attempt_key")
        if self.late and self.email_permitted:
            raise ValueError("late runs cannot permit email")
        expected_key = f"global-edition-run:{self.edition_date.isoformat()}:{attempt_key}"
        if self.generation_key != expected_key:
            raise ValueError("generation_key does not match edition date and attempt")
        object.__setattr__(self, "generation_key", expected_key)
        object.__setattr__(self, "attempt_key", attempt_key)
        object.__setattr__(self, "scenario", CalendarScenario(self.scenario))
        if self.reason_code is not None:
            object.__setattr__(
                self,
                "reason_code",
                _required(self.reason_code, field_name="reason_code", maximum=64),
            )
        _aware(self.started_at, field_name="started_at")


@dataclass(frozen=True, slots=True)
class GlobalEditionSnapshotItem:
    snapshot_id: str
    instrument: MarketInstrument | str

    def __post_init__(self) -> None:
        try:
            canonical_id = str(UUID(self.snapshot_id))
        except (ValueError, AttributeError) as error:
            raise ValueError("snapshot_id must be a UUID") from error
        object.__setattr__(self, "snapshot_id", canonical_id)
        object.__setattr__(self, "instrument", MarketInstrument(self.instrument))


def encode_global_run_spec(spec: GlobalEditionRunSpec) -> dict[str, Any]:
    return {
        "edition_date": spec.edition_date.isoformat(),
        "generation_key": spec.generation_key,
        "attempt_key": spec.attempt_key,
        "scenario": spec.scenario.value,
        "email_permitted": spec.email_permitted,
        "late": spec.late,
        "reason_code": spec.reason_code,
        "started_at": spec.started_at.isoformat(),
        "run_version": scheduled_run_version(spec.attempt_key),
    }


def build_global_run_manifest(
    spec: GlobalEditionRunSpec,
    *,
    snapshot_items: tuple[GlobalEditionSnapshotItem, ...],
) -> dict[str, Any]:
    ordered = sorted(
        snapshot_items,
        key=lambda item: _INSTRUMENT_POSITIONS[item.instrument],
    )
    return {
        "schema_version": 1,
        "run": encode_global_run_spec(spec),
        "market_snapshots": [
            {
                "snapshot_id": item.snapshot_id,
                "instrument": item.instrument.value,
            }
            for item in ordered
        ],
    }


def _sha256(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def global_run_spec_sha256(spec: GlobalEditionRunSpec) -> str:
    return _sha256(encode_global_run_spec(spec))


def global_run_manifest_sha256(manifest: dict[str, Any]) -> str:
    return _sha256(manifest)


__all__ = [
    "GlobalEditionRunSpec",
    "GlobalEditionSnapshotItem",
    "build_global_run_manifest",
    "encode_global_run_spec",
    "global_run_manifest_sha256",
    "global_run_spec_sha256",
    "scheduled_run_version",
]
