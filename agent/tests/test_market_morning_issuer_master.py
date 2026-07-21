"""Issuer-master and deterministic search contracts for Market Morning."""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy.dialects import mysql

from src.market_morning.issuer_master import (
    IssuerChangeType,
    IssuerMasterValidationError,
    IssuerSourceColumns,
    build_snapshot_metadata,
    diff_issuer_master,
    normalize_issuer_code,
    parse_issuer_rows,
)
from src.market_morning.issuer_search import (
    IssuerCatalogEntry,
    build_issuer_search_statement,
    search_catalog,
)

FIXTURE_PATH = (
    Path(__file__).parent
    / "fixtures"
    / "market_morning"
    / "issuer_search_benchmark.json"
)


def test_issue_codes_support_numeric_and_sicc_letter_positions() -> None:
    assert normalize_issuer_code("７２０３") == "7203"
    assert normalize_issuer_code("987a") == "987A"
    assert normalize_issuer_code("９ａ７６") == "9A76"


@pytest.mark.parametrize("value", ["A876", "98A7", "987B", "12345", "12-3", ""])
def test_invalid_issue_codes_fail_closed(value: str) -> None:
    with pytest.raises(IssuerMasterValidationError):
        normalize_issuer_code(value)


def test_parse_source_rows_uses_explicit_column_mapping_and_sorts_codes() -> None:
    rows = [
        {"コード": "987a", "銘柄名": "株式会社 瀬戸内半導体", "市場": "グロース"},
        {"コード": "7203", "銘柄名": "トヨタ自動車株式会社", "市場": "プライム"},
    ]

    parsed = parse_issuer_rows(
        rows,
        columns=IssuerSourceColumns(
            issuer_code="コード",
            legal_name_ja="銘柄名",
            market_segment="市場",
        ),
    )

    assert [record.issuer_code for record in parsed] == ["7203", "987A"]
    assert parsed[1].normalized_search_key == "瀬戸内半導体"


def test_duplicate_code_rejects_whole_snapshot() -> None:
    rows = [
        {"issuer_code": "7203", "legal_name_ja": "会社甲", "market_segment": "プライム"},
        {"issuer_code": "７２０３", "legal_name_ja": "会社乙", "market_segment": "プライム"},
    ]

    with pytest.raises(IssuerMasterValidationError, match="duplicate issuer code 7203"):
        parse_issuer_rows(rows)


def test_snapshot_metadata_requires_aware_time_and_hashes_source() -> None:
    metadata = build_snapshot_metadata(
        source_provider="fixture",
        source_version="2026-07",
        source_url="https://example.invalid/issuer-fixture.csv",
        snapshot_date=date(2026, 7, 31),
        fetched_at=datetime(2026, 8, 3, 0, 0, tzinfo=timezone.utc),
        raw_content=b"synthetic issuer fixture",
    )

    assert metadata.checksum_sha256 == (
        "f271beeeae7dd8cfeda42cf42357cfbb7a1e57caa0d922f7025352706a59deee"
    )


def test_diff_reports_each_supported_change_without_overwriting_history() -> None:
    previous = parse_issuer_rows(
        [
            {"issuer_code": "1001", "legal_name_ja": "青空株式会社", "market_segment": "プライム"},
            {"issuer_code": "1002", "legal_name_ja": "未来株式会社", "market_segment": "スタンダード"},
            {"issuer_code": "1003", "legal_name_ja": "旧会社株式会社", "market_segment": "グロース"},
        ]
    )
    current = parse_issuer_rows(
        [
            {"issuer_code": "1001", "legal_name_ja": "青空テック株式会社", "market_segment": "プライム"},
            {"issuer_code": "1002", "legal_name_ja": "未来株式会社", "market_segment": "プライム"},
            {"issuer_code": "987A", "legal_name_ja": "新会社株式会社", "market_segment": "グロース"},
        ]
    )

    changes = diff_issuer_master(previous, current)

    assert [(change.issuer_code, change.change_type) for change in changes] == [
        ("1001", IssuerChangeType.LEGAL_NAME_CHANGED),
        ("1002", IssuerChangeType.MARKET_SEGMENT_CHANGED),
        ("1003", IssuerChangeType.DELISTED),
        ("987A", IssuerChangeType.ADDED),
    ]


def _benchmark_fixture() -> tuple[list[IssuerCatalogEntry], list[dict[str, str]]]:
    payload = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    assert payload["fixture_kind"] == "synthetic"
    catalog = [
        IssuerCatalogEntry(
            issuer_id=item["issuer_id"],
            issuer_code=item["issuer_code"],
            legal_name_ja=item["legal_name_ja"],
            market_segment=item["market_segment"],
            approved_aliases=tuple(item["approved_aliases"]),
        )
        for item in payload["catalog"]
    ]
    return catalog, payload["cases"]


def test_synthetic_50_query_benchmark_places_target_in_top_three() -> None:
    catalog, cases = _benchmark_fixture()
    assert len(cases) == 50

    hits = 0
    for case in cases:
        top_three = search_catalog(catalog, case["query"], limit=3)
        if case["target"] in {match.issuer_code for match in top_three}:
            hits += 1

    assert hits >= 45


def test_search_does_not_guess_unknown_company() -> None:
    catalog, _ = _benchmark_fixture()

    assert search_catalog(catalog, "存在しない会社") == ()


def test_mysql_search_statement_is_bounded_and_filters_approved_aliases() -> None:
    statement = build_issuer_search_statement("987a", limit=7)
    assert statement is not None
    sql = str(
        statement.compile(
            dialect=mysql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )

    assert "mm_issuers.issuer_code = '987A'" in sql
    assert "mm_issuer_aliases.review_status = 'approved'" in sql
    assert "mm_issuer_aliases.effective_to IS NULL" in sql
    assert "LIMIT 7" in sql
