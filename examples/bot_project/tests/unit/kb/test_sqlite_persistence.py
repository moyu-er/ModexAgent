from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from bot.kb.models import KbEntry, KbFilter, KbUpsertRequest
from bot.kb.sqlite_persistence import SqliteKbPersistence
from bot.persistence.migration import BotWorkspaceMigrationRunner

from modex_agent.persistence import ConnectionManager, DatabaseKind
from modex_agent.persistence.connection import Transaction

type SqliteFixture = tuple[SqliteKbPersistence, ConnectionManager]


@pytest.fixture
async def sqlite_persistence(tmp_path: Path) -> AsyncIterator[SqliteFixture]:
    connection = ConnectionManager(tmp_path / "state.db", DatabaseKind.WORKSPACE)
    await connection.open()
    await BotWorkspaceMigrationRunner(connection).run_pending()
    yield SqliteKbPersistence(connection), connection
    await connection.close()


async def test_upsert_new_entry_returns_generated_entry(
    sqlite_persistence: SqliteFixture,
) -> None:
    store, _ = sqlite_persistence

    entry = await store.upsert(KbUpsertRequest(key="alpha", value="first"))

    assert isinstance(entry, KbEntry)
    assert entry.entry_id > 0
    assert entry.key == "alpha"
    assert entry.value == "first"


async def test_upsert_existing_task_session_key_updates_value(
    sqlite_persistence: SqliteFixture,
) -> None:
    store, _ = sqlite_persistence
    await store.upsert(
        KbUpsertRequest(
            key="alpha", value="first", task_id="task1", session_id="session1"
        )
    )

    entry = await store.upsert(
        KbUpsertRequest(
            key="alpha", value="second", task_id="task1", session_id="session1"
        )
    )

    assert entry.value == "second"


async def test_upsert_same_task_key_for_different_sessions_keeps_both_entries(
    sqlite_persistence: SqliteFixture,
) -> None:
    store, _ = sqlite_persistence
    first = await store.upsert(
        KbUpsertRequest(
            key="shared", value="one", task_id="task1", session_id="session1"
        )
    )
    second = await store.upsert(
        KbUpsertRequest(
            key="shared", value="two", task_id="task1", session_id="session2"
        )
    )

    stored_first = await store.get(
        "shared", KbFilter(task_id="task1", session_id="session1")
    )
    stored_second = await store.get(
        "shared", KbFilter(task_id="task1", session_id="session2")
    )
    assert stored_first == first
    assert stored_second == second
    assert first.entry_id != second.entry_id


async def test_upsert_raises_runtime_error_when_inserted_row_cannot_be_read(
    sqlite_persistence: SqliteFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, _ = sqlite_persistence
    monkeypatch.setattr(Transaction, "query_one", AsyncMock(return_value=None))

    with pytest.raises(RuntimeError, match="upsert succeeded but row not found"):
        await store.upsert(KbUpsertRequest(key="alpha", value="first"))


async def test_upsert_same_key_for_different_tasks_keeps_both_entries(
    sqlite_persistence: SqliteFixture,
) -> None:
    store, _ = sqlite_persistence
    await store.upsert(KbUpsertRequest(key="shared", value="one", task_id="task1"))

    await store.upsert(KbUpsertRequest(key="shared", value="two", task_id="task2"))

    assert await store.get("shared", KbFilter(task_id="task1")) is not None
    assert await store.get("shared", KbFilter(task_id="task2")) is not None


async def test_get_returns_matching_entry(sqlite_persistence: SqliteFixture) -> None:
    store, _ = sqlite_persistence
    expected = await store.upsert(
        KbUpsertRequest(
            key="alpha",
            value="first",
            task_id="task1",
            session_id="session1",
            category="notes",
            tags="red,blue",
        )
    )

    entry = await store.get("alpha", KbFilter(task_id="task1"))

    assert entry == expected


async def test_get_returns_none_when_key_is_missing(
    sqlite_persistence: SqliteFixture,
) -> None:
    store, _ = sqlite_persistence

    entry = await store.get("missing", KbFilter())

    assert entry is None


async def test_get_task_filter_excludes_other_task(
    sqlite_persistence: SqliteFixture,
) -> None:
    store, _ = sqlite_persistence
    await store.upsert(KbUpsertRequest(key="alpha", value="first", task_id="task1"))

    entry = await store.get("alpha", KbFilter(task_id="task2"))

    assert entry is None


async def test_delete_existing_entry_returns_true(
    sqlite_persistence: SqliteFixture,
) -> None:
    store, _ = sqlite_persistence
    await store.upsert(KbUpsertRequest(key="alpha", value="first"))

    deleted = await store.delete("alpha", KbFilter(task_id=""))

    assert deleted is True


async def test_delete_missing_entry_returns_false(
    sqlite_persistence: SqliteFixture,
) -> None:
    store, _ = sqlite_persistence

    deleted = await store.delete("missing", KbFilter())

    assert deleted is False


async def test_list_keys_returns_all_keys_sorted(
    sqlite_persistence: SqliteFixture,
) -> None:
    store, _ = sqlite_persistence
    await store.upsert(KbUpsertRequest(key="charlie", value="three"))
    await store.upsert(KbUpsertRequest(key="alpha", value="one"))
    await store.upsert(KbUpsertRequest(key="bravo", value="two"))

    keys = await store.list_keys(KbFilter())

    assert keys == ["alpha", "bravo", "charlie"]


async def test_list_keys_prefix_returns_only_matches(
    sqlite_persistence: SqliteFixture,
) -> None:
    store, _ = sqlite_persistence
    await store.upsert(KbUpsertRequest(key="app.one", value="one"))
    await store.upsert(KbUpsertRequest(key="app.two", value="two"))
    await store.upsert(KbUpsertRequest(key="other", value="three"))

    keys = await store.list_keys(KbFilter(), prefix="app.")

    assert keys == ["app.one", "app.two"]


async def test_list_keys_prefix_treats_wildcards_as_literals(
    sqlite_persistence: SqliteFixture,
) -> None:
    store, _ = sqlite_persistence
    await store.upsert(KbUpsertRequest(key="rate%exact", value="one"))
    await store.upsert(KbUpsertRequest(key="rate-other", value="two"))
    await store.upsert(KbUpsertRequest(key="under_score", value="three"))
    await store.upsert(KbUpsertRequest(key="underXscore", value="four"))

    percent_keys = await store.list_keys(KbFilter(), prefix="rate%")
    underscore_keys = await store.list_keys(KbFilter(), prefix="under_")

    assert percent_keys == ["rate%exact"]
    assert underscore_keys == ["under_score"]


async def test_none_task_filter_searches_across_all_tasks(
    sqlite_persistence: SqliteFixture,
) -> None:
    store, _ = sqlite_persistence
    await store.upsert(KbUpsertRequest(key="public", value="zero"))
    await store.upsert(KbUpsertRequest(key="one", value="one", task_id="task1"))
    await store.upsert(KbUpsertRequest(key="two", value="two", task_id="task2"))

    keys = await store.list_keys(KbFilter(task_id=None))

    assert keys == ["one", "public", "two"]


async def test_empty_task_filter_returns_only_public_entries(
    sqlite_persistence: SqliteFixture,
) -> None:
    store, _ = sqlite_persistence
    await store.upsert(KbUpsertRequest(key="public", value="zero"))
    await store.upsert(KbUpsertRequest(key="private", value="one", task_id="task1"))

    keys = await store.list_keys(KbFilter(task_id=""))

    assert keys == ["public"]


async def test_value_task_filter_returns_only_matching_task(
    sqlite_persistence: SqliteFixture,
) -> None:
    store, _ = sqlite_persistence
    await store.upsert(KbUpsertRequest(key="one", value="one", task_id="task1"))
    await store.upsert(KbUpsertRequest(key="two", value="two", task_id="task2"))

    keys = await store.list_keys(KbFilter(task_id="task1"))

    assert keys == ["one"]


async def test_none_session_filter_searches_across_all_sessions(
    sqlite_persistence: SqliteFixture,
) -> None:
    store, _ = sqlite_persistence
    await store.upsert(KbUpsertRequest(key="public", value="zero"))
    await store.upsert(KbUpsertRequest(key="one", value="one", session_id="session1"))
    await store.upsert(KbUpsertRequest(key="two", value="two", session_id="session2"))

    keys = await store.list_keys(KbFilter(session_id=None))

    assert keys == ["one", "public", "two"]


async def test_empty_session_filter_returns_only_public_entries(
    sqlite_persistence: SqliteFixture,
) -> None:
    store, _ = sqlite_persistence
    await store.upsert(KbUpsertRequest(key="public", value="zero"))
    await store.upsert(KbUpsertRequest(key="private", value="one", session_id="session1"))

    keys = await store.list_keys(KbFilter(session_id=""))

    assert keys == ["public"]


async def test_value_session_filter_returns_only_matching_session(
    sqlite_persistence: SqliteFixture,
) -> None:
    store, _ = sqlite_persistence
    await store.upsert(KbUpsertRequest(key="one", value="one", session_id="session1"))
    await store.upsert(KbUpsertRequest(key="two", value="two", session_id="session2"))

    keys = await store.list_keys(KbFilter(session_id="session1"))

    assert keys == ["one"]


async def test_get_with_none_session_returns_entry_regardless_of_session(
    sqlite_persistence: SqliteFixture,
) -> None:
    store, _ = sqlite_persistence
    await store.upsert(KbUpsertRequest(key="alpha", value="first", session_id="session1"))

    entry = await store.get("alpha", KbFilter(session_id=None))

    assert entry is not None
    assert entry.session_id == "session1"


async def test_get_with_empty_session_returns_only_public_entry(
    sqlite_persistence: SqliteFixture,
) -> None:
    store, _ = sqlite_persistence
    await store.upsert(KbUpsertRequest(key="shared", value="pub", task_id="t1"))
    await store.upsert(
        KbUpsertRequest(key="shared", value="priv", task_id="t2", session_id="session1")
    )

    entry = await store.get("shared", KbFilter(session_id=""))

    assert entry is not None
    assert entry.session_id == ""


async def test_get_with_value_session_returns_only_matching_entry(
    sqlite_persistence: SqliteFixture,
) -> None:
    store, _ = sqlite_persistence
    await store.upsert(KbUpsertRequest(key="shared", value="pub", task_id="t1"))
    await store.upsert(
        KbUpsertRequest(key="shared", value="priv", task_id="t2", session_id="session1")
    )

    entry = await store.get("shared", KbFilter(session_id="session1"))

    assert entry is not None
    assert entry.session_id == "session1"


async def test_none_category_filter_searches_across_all_categories(
    sqlite_persistence: SqliteFixture,
) -> None:
    store, _ = sqlite_persistence
    await store.upsert(KbUpsertRequest(key="uncategorized", value="zero"))
    await store.upsert(KbUpsertRequest(key="one", value="one", category="project"))
    await store.upsert(KbUpsertRequest(key="two", value="two", category="personal"))

    keys = await store.list_keys(KbFilter(category=None))

    assert keys == ["one", "two", "uncategorized"]


async def test_empty_category_filter_returns_only_uncategorized_entries(
    sqlite_persistence: SqliteFixture,
) -> None:
    store, _ = sqlite_persistence
    await store.upsert(KbUpsertRequest(key="uncategorized", value="zero"))
    await store.upsert(
        KbUpsertRequest(key="categorized", value="one", category="project")
    )

    keys = await store.list_keys(KbFilter(category=""))

    assert keys == ["uncategorized"]


async def test_value_category_filter_returns_only_matching_category(
    sqlite_persistence: SqliteFixture,
) -> None:
    store, _ = sqlite_persistence
    await store.upsert(KbUpsertRequest(key="one", value="one", category="project"))
    await store.upsert(KbUpsertRequest(key="two", value="two", category="personal"))

    keys = await store.list_keys(KbFilter(category="project"))

    assert keys == ["one"]


async def test_get_with_none_category_returns_entry_regardless_of_category(
    sqlite_persistence: SqliteFixture,
) -> None:
    store, _ = sqlite_persistence
    await store.upsert(KbUpsertRequest(key="alpha", value="first", category="project"))

    entry = await store.get("alpha", KbFilter(category=None))

    assert entry is not None
    assert entry.category == "project"


async def test_get_with_empty_category_returns_only_uncategorized_entry(
    sqlite_persistence: SqliteFixture,
) -> None:
    store, _ = sqlite_persistence
    await store.upsert(KbUpsertRequest(key="shared", value="uncat", task_id="t1"))
    await store.upsert(
        KbUpsertRequest(key="shared", value="cat", task_id="t2", category="project")
    )

    entry = await store.get("shared", KbFilter(category=""))

    assert entry is not None
    assert entry.category == ""


async def test_get_with_value_category_returns_only_matching_entry(
    sqlite_persistence: SqliteFixture,
) -> None:
    store, _ = sqlite_persistence
    await store.upsert(KbUpsertRequest(key="shared", value="uncat", task_id="t1"))
    await store.upsert(
        KbUpsertRequest(key="shared", value="cat", task_id="t2", category="project")
    )

    entry = await store.get("shared", KbFilter(category="project"))

    assert entry is not None
    assert entry.category == "project"


async def test_combined_filter_returns_only_entry_matching_all_dimensions(
    sqlite_persistence: SqliteFixture,
) -> None:
    store, _ = sqlite_persistence
    await store.upsert(
        KbUpsertRequest(
            key="shared",
            value="match",
            task_id="task1",
            session_id="session1",
            category="project",
        )
    )
    await store.upsert(
        KbUpsertRequest(
            key="shared",
            value="other",
            task_id="task2",
            session_id="session2",
            category="personal",
        )
    )

    entry = await store.get(
        "shared",
        KbFilter(task_id="task1", session_id="session1", category="project"),
    )

    assert entry is not None
    assert entry.value == "match"

    mismatched = await store.get(
        "shared",
        KbFilter(task_id="task1", session_id="session1", category="personal"),
    )

    assert mismatched is None


async def test_upsert_trigger_adds_entry_to_fts_index(
    sqlite_persistence: SqliteFixture,
) -> None:
    store, connection = sqlite_persistence

    entry = await store.upsert(
        KbUpsertRequest(key="alpha", value="searchable value", tags="searchable-tag")
    )

    row = await connection.query_one(
        "SELECT rowid, value, tags FROM kb_entries_fts WHERE rowid = ?",
        (entry.entry_id,),
    )
    assert row is not None
    assert tuple(row) == (entry.entry_id, "searchable value", "searchable-tag")


async def test_delete_trigger_removes_entry_from_fts_index(
    sqlite_persistence: SqliteFixture,
) -> None:
    store, connection = sqlite_persistence
    entry = await store.upsert(KbUpsertRequest(key="alpha", value="searchable value"))

    await store.delete("alpha", KbFilter(task_id=""))

    row = await connection.query_one(
        "SELECT rowid FROM kb_entries_fts WHERE rowid = ?",
        (entry.entry_id,),
    )
    assert row is None


async def test_entry_id_is_positive_signed_64_bit_integer(
    sqlite_persistence: SqliteFixture,
) -> None:
    store, _ = sqlite_persistence

    entry = await store.upsert(KbUpsertRequest(key="alpha", value="first"))

    assert isinstance(entry.entry_id, int)
    assert 0 < entry.entry_id <= (2**63) - 1


async def test_timestamps_are_positive_epoch_milliseconds(
    sqlite_persistence: SqliteFixture,
) -> None:
    store, _ = sqlite_persistence

    entry = await store.upsert(KbUpsertRequest(key="alpha", value="first"))

    assert isinstance(entry.created_at, int)
    assert isinstance(entry.updated_at, int)
    assert entry.created_at > 0
    assert entry.updated_at > 0
