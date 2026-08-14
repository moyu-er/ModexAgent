from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from bot.kb.fts5_retriever import Fts5Retriever
from bot.kb.models import KbEntry, KbFilter, KbSearchResult, KbUpsertRequest
from bot.kb.sqlite_utils import build_filter_clauses
from bot.persistence.migration import BotWorkspaceMigrationRunner

from modex_agent.persistence import ConnectionManager, DatabaseKind


@pytest.fixture
async def migrated_connection(
    tmp_path: Path,
) -> AsyncIterator[ConnectionManager]:
    connection = ConnectionManager(tmp_path / "state.db", DatabaseKind.WORKSPACE)
    await connection.open()
    await BotWorkspaceMigrationRunner(connection).run_pending()
    yield connection
    await connection.close()


async def _insert(
    connection: ConnectionManager,
    entry_id: int,
    request: KbUpsertRequest,
) -> None:
    async with connection.transaction() as transaction:
        await transaction.execute(
            """
            INSERT INTO kb_entries
                (entry_id, key, value, task_id, session_id, category, tags,
                 created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                entry_id,
                request.key,
                request.value,
                request.task_id,
                request.session_id,
                request.category,
                request.tags,
                entry_id,
                entry_id,
            ),
        )


def test_shared_filter_builder_preserves_alias_and_three_state_semantics() -> None:
    clauses, params = build_filter_clauses(
        KbFilter(task_id=None, session_id="", category="project"),
        alias="e.",
    )

    assert clauses == ["e.session_id = ?", "e.category = ?"]
    assert params == ["", "project"]


async def test_search_finds_basic_cjk_match_with_positive_score(
    migrated_connection: ConnectionManager,
) -> None:
    await _insert(
        migrated_connection,
        1,
        KbUpsertRequest(key="deploy", value="部署流程"),
    )

    results = await Fts5Retriever(migrated_connection).search("部署流", KbFilter())

    assert [result.entry.key for result in results] == ["deploy"]
    assert results[0].score > 0


async def test_search_returns_empty_when_no_entry_matches(
    migrated_connection: ConnectionManager,
) -> None:
    await _insert(
        migrated_connection,
        1,
        KbUpsertRequest(key="deploy", value="deploy steps"),
    )

    results = await Fts5Retriever(migrated_connection).search("nonexistent", KbFilter())

    assert results == []


async def test_search_matches_cjk_with_trigram_tokenizer(
    migrated_connection: ConnectionManager,
) -> None:
    await _insert(
        migrated_connection,
        1,
        KbUpsertRequest(key="design", value="知识库设计"),
    )

    results = await Fts5Retriever(migrated_connection).search("知识库", KbFilter())

    assert [result.entry.key for result in results] == ["design"]


async def test_search_matches_english_text(
    migrated_connection: ConnectionManager,
) -> None:
    await _insert(
        migrated_connection,
        1,
        KbUpsertRequest(key="deploy", value="deploy steps"),
    )

    results = await Fts5Retriever(migrated_connection).search("deploy", KbFilter())

    assert [result.entry.key for result in results] == ["deploy"]


async def test_task_id_none_searches_across_all_tasks(
    migrated_connection: ConnectionManager,
) -> None:
    await _insert(
        migrated_connection,
        1,
        KbUpsertRequest(key="one", value="shared knowledge", task_id="task1"),
    )
    await _insert(
        migrated_connection,
        2,
        KbUpsertRequest(key="two", value="shared knowledge", task_id="task2"),
    )

    results = await Fts5Retriever(migrated_connection).search("shared", KbFilter(task_id=None))

    assert {result.entry.task_id for result in results} == {"task1", "task2"}


async def test_task_id_value_returns_only_matching_task(
    migrated_connection: ConnectionManager,
) -> None:
    await _insert(
        migrated_connection,
        1,
        KbUpsertRequest(key="one", value="shared knowledge", task_id="task1"),
    )
    await _insert(
        migrated_connection,
        2,
        KbUpsertRequest(key="two", value="shared knowledge", task_id="task2"),
    )

    results = await Fts5Retriever(migrated_connection).search("shared", KbFilter(task_id="task1"))

    assert [result.entry.task_id for result in results] == ["task1"]


async def test_empty_task_id_returns_only_public_entries(
    migrated_connection: ConnectionManager,
) -> None:
    await _insert(
        migrated_connection,
        1,
        KbUpsertRequest(key="public", value="shared knowledge"),
    )
    await _insert(
        migrated_connection,
        2,
        KbUpsertRequest(key="private", value="shared knowledge", task_id="task1"),
    )

    results = await Fts5Retriever(migrated_connection).search("shared", KbFilter(task_id=""))

    assert [result.entry.key for result in results] == ["public"]


async def test_category_filter_returns_only_matching_category(
    migrated_connection: ConnectionManager,
) -> None:
    await _insert(
        migrated_connection,
        1,
        KbUpsertRequest(key="project", value="shared knowledge", category="project"),
    )
    await _insert(
        migrated_connection,
        2,
        KbUpsertRequest(key="personal", value="shared knowledge", category="personal"),
    )

    results = await Fts5Retriever(migrated_connection).search(
        "shared", KbFilter(category="project")
    )

    assert [result.entry.category for result in results] == ["project"]


async def test_search_enforces_limit(
    migrated_connection: ConnectionManager,
) -> None:
    for entry_id in range(1, 11):
        await _insert(
            migrated_connection,
            entry_id,
            KbUpsertRequest(key=f"key-{entry_id}", value="common deployment steps"),
        )

    results = await Fts5Retriever(migrated_connection).search("common", KbFilter(), limit=3)

    assert len(results) == 3


async def test_search_returns_empty_for_sanitized_empty_query(
    migrated_connection: ConnectionManager,
) -> None:
    await _insert(
        migrated_connection,
        1,
        KbUpsertRequest(key="deploy", value="deploy steps"),
    )

    results = await Fts5Retriever(migrated_connection).search("*** --- !!!", KbFilter())

    assert results == []


async def test_search_returns_typed_result_with_entry_and_score(
    migrated_connection: ConnectionManager,
) -> None:
    await _insert(
        migrated_connection,
        1,
        KbUpsertRequest(key="deploy", value="deploy steps", tags="release"),
    )

    results = await Fts5Retriever(migrated_connection).search("deploy", KbFilter())

    assert type(results[0]) is KbSearchResult
    assert results[0].entry == KbEntry(
        entry_id=1,
        key="deploy",
        value="deploy steps",
        tags="release",
        created_at=1,
        updated_at=1,
    )
    assert results[0].score > 0


async def test_session_id_filter_returns_only_matching_session(
    migrated_connection: ConnectionManager,
) -> None:
    await _insert(
        migrated_connection,
        1,
        KbUpsertRequest(key="one", value="shared knowledge", session_id="session1"),
    )
    await _insert(
        migrated_connection,
        2,
        KbUpsertRequest(key="two", value="shared knowledge", session_id="session2"),
    )

    results = await Fts5Retriever(migrated_connection).search(
        "shared", KbFilter(session_id="session2")
    )

    assert [result.entry.session_id for result in results] == ["session2"]


async def test_session_id_none_searches_across_all_sessions(
    migrated_connection: ConnectionManager,
) -> None:
    await _insert(
        migrated_connection,
        1,
        KbUpsertRequest(key="one", value="shared knowledge", session_id="session1"),
    )
    await _insert(
        migrated_connection,
        2,
        KbUpsertRequest(key="two", value="shared knowledge", session_id="session2"),
    )

    results = await Fts5Retriever(migrated_connection).search(
        "shared", KbFilter(session_id=None)
    )

    assert {result.entry.session_id for result in results} == {"session1", "session2"}


async def test_empty_session_id_returns_only_public_entries(
    migrated_connection: ConnectionManager,
) -> None:
    await _insert(
        migrated_connection,
        1,
        KbUpsertRequest(key="public", value="shared knowledge"),
    )
    await _insert(
        migrated_connection,
        2,
        KbUpsertRequest(key="private", value="shared knowledge", session_id="session1"),
    )

    results = await Fts5Retriever(migrated_connection).search(
        "shared", KbFilter(session_id="")
    )

    assert [result.entry.key for result in results] == ["public"]


async def test_category_none_searches_across_all_categories(
    migrated_connection: ConnectionManager,
) -> None:
    await _insert(
        migrated_connection,
        1,
        KbUpsertRequest(key="one", value="shared knowledge", category="project"),
    )
    await _insert(
        migrated_connection,
        2,
        KbUpsertRequest(key="two", value="shared knowledge", category="personal"),
    )

    results = await Fts5Retriever(migrated_connection).search(
        "shared", KbFilter(category=None)
    )

    assert {result.entry.category for result in results} == {"project", "personal"}


async def test_empty_category_returns_only_uncategorized_entries(
    migrated_connection: ConnectionManager,
) -> None:
    await _insert(
        migrated_connection,
        1,
        KbUpsertRequest(key="uncategorized", value="shared knowledge"),
    )
    await _insert(
        migrated_connection,
        2,
        KbUpsertRequest(
            key="categorized", value="shared knowledge", category="project"
        ),
    )

    results = await Fts5Retriever(migrated_connection).search(
        "shared", KbFilter(category="")
    )

    assert [result.entry.key for result in results] == ["uncategorized"]


async def test_bm25_ranking_returns_more_relevant_results_first(
    migrated_connection: ConnectionManager,
) -> None:
    await _insert(
        migrated_connection,
        1,
        KbUpsertRequest(key="frequent", value="deploy deploy deploy deploy deploy"),
    )
    await _insert(
        migrated_connection,
        2,
        KbUpsertRequest(
            key="sparse",
            value="deploy with many other words that dilute the relevance score significantly",
        ),
    )

    results = await Fts5Retriever(migrated_connection).search("deploy", KbFilter())

    assert len(results) == 2
    assert results[0].entry.key == "frequent"
    assert results[0].score >= results[1].score
