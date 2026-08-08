"""Tests for bot.kb.provider — facade delegating to persistence + retriever.

Verifies the delegation contract (DESIGN.md §6):
  upsert / get / delete / list_keys → persistence
  search                            → retriever (NOT persistence)

Uses AsyncMock so the tests do not require concrete persistence/retriever
implementations (T5/T6 land in parallel).
"""

from __future__ import annotations

from unittest.mock import AsyncMock

from bot.kb.models import KbEntry, KbFilter, KbSearchResult, KbUpsertRequest
from bot.kb.provider import KbProvider


def _sample_entry() -> KbEntry:
    return KbEntry(
        entry_id=1, key="k", value="v",
        created_at=100, updated_at=101,
    )


def _make_provider(
    persistence: AsyncMock | None = None,
    retriever: AsyncMock | None = None,
) -> KbProvider:
    return KbProvider(
        persistence=persistence or AsyncMock(),
        retriever=retriever or AsyncMock(),
    )


async def test_upsert_delegates_to_persistence_when_called() -> None:
    """Given: a KbProvider with a mocked persistence.
    When: upsert is called with a request.
    Then: persistence.upsert is awaited with that exact request and its
    return value is passed through unchanged.
    """
    expected = _sample_entry()
    persistence = AsyncMock()
    persistence.upsert.return_value = expected
    provider = _make_provider(persistence=persistence)
    request = KbUpsertRequest(key="k", value="v")

    result = await provider.upsert(request)

    persistence.upsert.assert_awaited_once_with(request)
    assert result is expected


async def test_get_delegates_to_persistence_when_called() -> None:
    """Given: a KbProvider with a mocked persistence.
    When: get is called with a key and filter.
    Then: persistence.get is awaited with (key, filter) and its return
    value is passed through unchanged.
    """
    expected = _sample_entry()
    persistence = AsyncMock()
    persistence.get.return_value = expected
    provider = _make_provider(persistence=persistence)
    flt = KbFilter(task_id="t1")

    result = await provider.get("deploy-steps", flt)

    persistence.get.assert_awaited_once_with("deploy-steps", flt)
    assert result is expected


async def test_delete_delegates_to_persistence_when_called() -> None:
    """Given: a KbProvider with a mocked persistence.
    When: delete is called with a key and filter.
    Then: persistence.delete is awaited with (key, filter) and its bool
    return value is passed through unchanged.
    """
    persistence = AsyncMock()
    persistence.delete.return_value = True
    provider = _make_provider(persistence=persistence)
    flt = KbFilter(task_id="t1")

    result = await provider.delete("stale-key", flt)

    persistence.delete.assert_awaited_once_with("stale-key", flt)
    assert result is True


async def test_list_keys_delegates_to_persistence_when_called() -> None:
    """Given: a KbProvider with a mocked persistence.
    When: list_keys is called with a filter and a prefix.
    Then: persistence.list_keys is awaited with (filter, prefix) and its
    return value is passed through unchanged.
    """
    expected = ["deploy-steps", "deploy-config"]
    persistence = AsyncMock()
    persistence.list_keys.return_value = expected
    provider = _make_provider(persistence=persistence)
    flt = KbFilter(task_id="t1")

    result = await provider.list_keys(flt, prefix="deploy")

    persistence.list_keys.assert_awaited_once_with(flt, "deploy")
    assert result is expected


async def test_search_delegates_to_retriever_not_persistence_when_called() -> None:
    """Given: a KbProvider with mocked persistence and retriever.
    When: search is called with a query, filter, and limit.
    Then: retriever.search is awaited with (query, filter, limit) and its
    return value is passed through; persistence is never touched.
    """
    expected = [KbSearchResult(entry=_sample_entry(), score=0.9)]
    retriever = AsyncMock()
    retriever.search.return_value = expected
    persistence = AsyncMock()
    provider = _make_provider(persistence=persistence, retriever=retriever)
    flt = KbFilter(task_id="t1")

    result = await provider.search("deploy steps", flt, limit=5)

    retriever.search.assert_awaited_once_with("deploy steps", flt, 5)
    assert result is expected
    persistence.assert_not_called()
