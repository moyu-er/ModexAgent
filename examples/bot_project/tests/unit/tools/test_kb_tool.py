from __future__ import annotations

from unittest.mock import AsyncMock

from bot.kb.models import KbAction, KbEntry, KbFilter, KbSearchResult, KbUpsertRequest
from bot.tools import kb as kb_module
from bot.tools.kb import KbTool


def _entry() -> KbEntry:
    return KbEntry(
        entry_id=1,
        key="deploy-steps",
        value="Build, test, deploy",
        task_id="task-1",
        session_id="session-1",
        category="project",
        created_at=100,
        updated_at=101,
    )


def _tool(
    provider: AsyncMock,
    *,
    task_id: str | None = "task-1",
    session_id: str | None = "session-1",
) -> KbTool:
    return KbTool(
        provider=provider,
        task_id_provider=lambda: task_id,
        session_id_provider=lambda: session_id,
    )


async def test_set_saves_scoped_knowledge_and_returns_confirmation() -> None:
    provider = AsyncMock()
    provider.upsert.return_value = _entry()
    tool = _tool(provider)

    result = await tool.execute(
        action="set",
        key="deploy-steps",
        value="Build, test, deploy",
        category="project",
    )

    provider.upsert.assert_awaited_once_with(
        KbUpsertRequest(
            key="deploy-steps",
            value="Build, test, deploy",
            task_id="task-1",
            session_id="session-1",
            category="project",
        )
    )
    assert result == "Saved: deploy-steps (category: project)"


async def test_get_retrieves_scoped_knowledge_as_structured_text() -> None:
    provider = AsyncMock()
    provider.get.return_value = _entry()
    tool = _tool(provider)

    result = await tool.execute(action="get", key="deploy-steps")

    provider.get.assert_awaited_once_with(
        "deploy-steps",
        KbFilter(task_id="task-1", session_id="session-1"),
    )
    assert "[deploy-steps]" in result
    assert "--------------------------------------------------" in result
    assert "Build, test, deploy" in result
    for internal_field in (
        "entry_id",
        "task_id",
        "session_id",
        "created_at",
        "updated_at",
    ):
        assert internal_field not in result


async def test_get_returns_not_found_when_key_is_missing() -> None:
    provider = AsyncMock()
    provider.get.return_value = None
    tool = _tool(provider)

    result = await tool.execute(action="get", key="missing")

    assert result == "Not found: missing"


async def test_search_returns_consumer_facing_structured_text() -> None:
    provider = AsyncMock()
    provider.search.return_value = [KbSearchResult(entry=_entry(), score=0.75)]
    tool = _tool(provider)

    result = await tool.execute(
        action="search",
        query="deploy",
        category="project",
        limit=5,
    )

    provider.search.assert_awaited_once_with(
        "deploy",
        KbFilter(
            task_id="task-1",
            session_id="session-1",
            category="project",
        ),
        5,
    )
    assert "Found 1 result(s):" in result
    assert "[deploy-steps]" in result
    assert "score: 0.75" in result
    for internal_field in (
        "entry_id",
        "task_id",
        "session_id",
        "created_at",
        "updated_at",
    ):
        assert internal_field not in result


async def test_search_returns_no_results_message_when_empty() -> None:
    provider = AsyncMock()
    provider.search.return_value = []
    tool = _tool(provider)

    result = await tool.execute(action="search", query="missing")

    assert result == "No results found."


async def test_search_truncates_long_result_value() -> None:
    provider = AsyncMock()
    long_entry = _entry().model_copy(update={"value": "x" * 201})
    provider.search.return_value = [KbSearchResult(entry=long_entry, score=0.75)]
    tool = _tool(provider)

    result = await tool.execute(action="search", query="deploy")

    assert f"{'x' * 200}..." in result
    assert "x" * 201 not in result


async def test_delete_returns_deleted_status() -> None:
    provider = AsyncMock()
    provider.delete.return_value = True
    tool = _tool(provider)

    result = await tool.execute(action="delete", key="deploy-steps")

    provider.delete.assert_awaited_once_with(
        "deploy-steps",
        KbFilter(task_id="task-1", session_id="session-1"),
    )
    assert result == "Deleted: deploy-steps"


async def test_delete_returns_not_found_when_key_is_missing() -> None:
    provider = AsyncMock()
    provider.delete.return_value = False
    tool = _tool(provider)

    result = await tool.execute(action="delete", key="missing")

    assert result == "Not found: missing"


async def test_list_returns_keys_filtered_by_prefix() -> None:
    provider = AsyncMock()
    provider.list_keys.return_value = ["deploy-config", "deploy-steps"]
    tool = _tool(provider)

    result = await tool.execute(action="list", prefix="deploy-")

    provider.list_keys.assert_awaited_once_with(
        KbFilter(task_id="task-1", session_id="session-1"),
        "deploy-",
    )
    assert result == "2 key(s):\n- deploy-config\n- deploy-steps"


async def test_list_returns_no_keys_message_when_empty() -> None:
    provider = AsyncMock()
    provider.list_keys.return_value = []
    tool = _tool(provider)

    result = await tool.execute(action="list")

    assert result == "No keys found."


async def test_unknown_action_returns_error_json() -> None:
    tool = _tool(AsyncMock())

    result = await tool.execute(action="unknown")

    assert result == '{"error": "unknown action"}'


def test_action_dispatch_imports_shared_kb_action_enum() -> None:
    assert kb_module.KbAction is KbAction


async def test_missing_task_id_uses_global_task_filter() -> None:
    provider = AsyncMock()
    provider.get.return_value = None
    tool = _tool(provider, task_id=None)

    await tool.execute(action="get", key="deploy-steps")

    provider.get.assert_awaited_once_with(
        "deploy-steps",
        KbFilter(task_id=None, session_id="session-1"),
    )


async def test_missing_session_id_uses_global_session_filter() -> None:
    provider = AsyncMock()
    provider.get.return_value = None
    tool = _tool(provider, session_id=None)

    await tool.execute(action="get", key="deploy-steps")

    provider.get.assert_awaited_once_with(
        "deploy-steps",
        KbFilter(task_id="task-1", session_id=None),
    )


def test_description_explains_when_to_save_and_look_up_knowledge() -> None:
    tool = _tool(AsyncMock())

    assert "Save and look up knowledge" in tool.description


def test_description_does_not_expose_internal_terms() -> None:
    tool = _tool(AsyncMock())

    for internal_term in ("upsert", "FTS5", "KbFilter", "trigram"):
        assert internal_term not in tool.description


def test_tool_name_is_kb() -> None:
    tool = _tool(AsyncMock())

    assert tool.name == "kb"
