"""Watchlist transaction and policy tests without a live MySQL instance."""

from __future__ import annotations

import asyncio
from collections import deque
from datetime import datetime
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import mysql

from src.market_morning.models import AnalyticsEvent, AuditLog, WatchlistItem
from src.market_morning.watchlist import (
    WatchlistLimitReached,
    WatchlistMutationStatus,
    WatchlistValidationError,
    add_watchlist_item,
    list_watchlist_items,
    normalize_watchlist_label,
    remove_watchlist_item,
    update_watchlist_item,
)

USER_ID = "11111111-1111-4111-8111-111111111111"
ISSUER_ID = "22222222-2222-4222-8222-222222222222"
NOW = datetime(2026, 7, 20, 23, 0, 0)


class _Result:
    def __init__(self, *, scalar=None, one=None, rows=None):
        self._scalar = scalar
        self._one = one
        self._rows = rows or []

    def scalar_one_or_none(self):
        return self._scalar

    def scalar_one(self):
        return self._scalar

    def one(self):
        return self._one

    def all(self):
        return self._rows


class _Session:
    def __init__(self, *results: _Result):
        self.results = deque(results)
        self.statements = []
        self.added = []
        self.flush_count = 0

    async def execute(self, statement):
        self.statements.append(statement)
        return self.results.popleft()

    def add(self, value):
        self.added.append(value)

    async def flush(self):
        self.flush_count += 1


def _user():
    return SimpleNamespace(
        user_id=USER_ID,
        account_status="active",
        deleted_at=None,
    )


def _issuer():
    return SimpleNamespace(
        issuer_id=ISSUER_ID,
        issuer_code="987A",
        legal_name_ja="株式会社瀬戸内半導体",
        market_segment="グロース",
        active_status="active",
    )


def _item(*, label: str | None = None) -> WatchlistItem:
    return WatchlistItem(
        watchlist_item_id="33333333-3333-4333-8333-333333333333",
        user_id=USER_ID,
        issuer_id=ISSUER_ID,
        user_label=label,
        sort_order=0,
        created_at=NOW,
        updated_at=NOW,
    )


def test_label_normalization_preserves_meaning_and_rejects_controls() -> None:
    assert normalize_watchlist_label("  決算　フォロー  ") == "決算 フォロー"
    assert normalize_watchlist_label("   ") is None
    with pytest.raises(WatchlistValidationError, match="control"):
        normalize_watchlist_label("決算\u200bフォロー")


def test_add_serializes_on_user_row_and_appends_audit() -> None:
    session = _Session(
        _Result(scalar=_user()),
        _Result(scalar=_issuer()),
        _Result(scalar=None),
        _Result(one=(9, 8)),
    )

    result = asyncio.run(
        add_watchlist_item(
            session,
            user_id=USER_ID,
            issuer_id=ISSUER_ID,
            user_label=" 決算　フォロー ",
            now=NOW,
        )
    )

    assert result.status == WatchlistMutationStatus.ADDED
    assert result.active_count == 10
    assert result.item is not None
    assert result.item.sort_order == 9
    assert result.item.user_label == "決算 フォロー"
    assert session.flush_count == 1
    assert len(session.added) == 3
    audit = next(value for value in session.added if isinstance(value, AuditLog))
    event = next(value for value in session.added if isinstance(value, AnalyticsEvent))
    assert audit.action == "watchlist.added"
    assert audit.details == {"issuer_id": ISSUER_ID}
    assert event.event_name == "watchlist_added"
    assert event.properties == {"active_count": 10, "operation_status": "added"}
    assert "user_label" not in event.properties
    first_sql = str(session.statements[0].compile(dialect=mysql.dialect()))
    assert "FOR UPDATE" in first_sql


def test_add_is_idempotent_when_issuer_is_already_active() -> None:
    existing = _item(label="長期観察")
    session = _Session(
        _Result(scalar=_user()),
        _Result(scalar=_issuer()),
        _Result(scalar=existing),
        _Result(one=(1, 0)),
    )

    result = asyncio.run(
        add_watchlist_item(
            session,
            user_id=USER_ID,
            issuer_id=ISSUER_ID,
            user_label="変更しない",
            now=NOW,
        )
    )

    assert result.status == WatchlistMutationStatus.ALREADY_ACTIVE
    assert result.item is not None
    assert result.item.user_label == "長期観察"
    assert session.added == []
    assert session.flush_count == 0


def test_tenth_item_is_allowed_but_eleventh_is_rejected() -> None:
    session = _Session(
        _Result(scalar=_user()),
        _Result(scalar=_issuer()),
        _Result(scalar=None),
        _Result(one=(10, 9)),
    )

    with pytest.raises(WatchlistLimitReached) as error:
        asyncio.run(
            add_watchlist_item(
                session,
                user_id=USER_ID,
                issuer_id=ISSUER_ID,
                now=NOW,
            )
        )

    assert error.value.limit == 10
    assert session.added == []


def test_remove_is_idempotent_when_no_active_item_exists() -> None:
    session = _Session(
        _Result(scalar=_user()),
        _Result(scalar=None),
        _Result(scalar=4),
    )

    result = asyncio.run(
        remove_watchlist_item(
            session,
            user_id=USER_ID,
            issuer_id=ISSUER_ID,
            now=NOW,
        )
    )

    assert result.status == WatchlistMutationStatus.ALREADY_REMOVED
    assert result.active_count == 4
    assert session.added == []


def test_remove_soft_deletes_and_appends_audit() -> None:
    item = _item(label="長期観察")
    session = _Session(
        _Result(scalar=_user()),
        _Result(scalar=item),
        _Result(scalar=5),
    )

    result = asyncio.run(
        remove_watchlist_item(
            session,
            user_id=USER_ID,
            issuer_id=ISSUER_ID,
            now=NOW,
        )
    )

    assert result.status == WatchlistMutationStatus.REMOVED
    assert result.active_count == 4
    assert item.removed_at == NOW
    audit = next(value for value in session.added if isinstance(value, AuditLog))
    event = next(value for value in session.added if isinstance(value, AnalyticsEvent))
    assert audit.action == "watchlist.removed"
    assert event.event_name == "watchlist_removed"
    assert event.properties["active_count"] == 4
    assert session.flush_count == 1


def test_update_changes_label_and_order_under_user_lock() -> None:
    item = _item(label="長期観察")
    session = _Session(
        _Result(scalar=_user()),
        _Result(scalar=_issuer()),
        _Result(scalar=item),
        _Result(scalar=3),
    )

    result = asyncio.run(
        update_watchlist_item(
            session,
            user_id=USER_ID,
            issuer_id=ISSUER_ID,
            user_label=" 決算　フォロー ",
            sort_order=2,
            now=NOW,
        )
    )

    assert result.status == WatchlistMutationStatus.UPDATED
    assert result.active_count == 3
    assert result.item is not None
    assert result.item.user_label == "決算 フォロー"
    assert result.item.sort_order == 2
    audit = next(value for value in session.added if isinstance(value, AuditLog))
    event = next(value for value in session.added if isinstance(value, AnalyticsEvent))
    assert audit.action == "watchlist.updated"
    assert event.event_name == "watchlist_updated"
    assert "user_label" not in event.properties
    first_sql = str(session.statements[0].compile(dialect=mysql.dialect()))
    assert "FOR UPDATE" in first_sql


def test_list_is_user_scoped_but_does_not_lock_read_requests() -> None:
    item = _item(label="長期観察")
    session = _Session(
        _Result(scalar=_user()),
        _Result(rows=[(item, _issuer())]),
    )

    result = asyncio.run(list_watchlist_items(session, user_id=USER_ID))

    assert result[0].issuer_code == "987A"
    user_sql = str(session.statements[0].compile(dialect=mysql.dialect()))
    list_sql = str(session.statements[1].compile(dialect=mysql.dialect()))
    assert "FOR UPDATE" not in user_sql
    assert "mm_watchlist_items.user_id" in list_sql
