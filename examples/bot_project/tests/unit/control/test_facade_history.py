"""Seam 1 tests — :class:`BotControlFacade.history()`.

Tests the facade in isolation: mocked ``MessageStore`` returns sample records,
mocked ``TranscriptStore`` returns sample events, mocked ``SessionStore``
returns sample ``SessionInfo``, mocked ``PoolInstance`` carries ``execution_strategy``
with the appropriate ``execution_strategy``. Verifies the facade produces the
correct :class:`HistoryResult` for both native (``react``) and external coding
(``external``) strategies — projection, ordering, limit, soft-delete
inclusion, error paths.

The workspace resolver, message-store provider, and transcript-store provider
are injected as closures so the test never touches a real
``ScopeRegistry`` or memory system.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from bot.control.facade import BotControlFacade, ControlFacadeError
from bot.control.models import (
    AgentSessionRef,
    ControlError,
    HistoryRequest,
    HistorySource,
)
from bot.scope import BotRecordScope
from bot.webui.events import (
    AssistantReasoningEvent,
    AssistantTextEvent,
    ServerEvent,
    ToolCallEvent,
    ToolResultEvent,
    TurnStartEvent,
)
from bot.webui.transcript_store import TranscriptStore

from modex_agent.core.agent import ExecutionStrategyKind
from modex_agent.core.message import MessageRole
from modex_agent.core.session_id import SessionInfo
from modex_agent.memory.core.split_stores import MessageStore
from modex_agent.memory.stores.scoped_in_memory import InMemoryScopedStorage
from modex_agent.persistence.session_store import SessionStore
from modex_agent.scope.spec import AgentSpec, PoolSpec

# ---------------------------------------------------------------------------
# Sample data
# ---------------------------------------------------------------------------

_SESSION_ID = "inv123.coder"
_AGENT_NAME = "coder"
_POOL = "coder_pool"
_WORKSPACE = Path("/home/user/project")

_SAMPLE_MESSAGES: list[dict[str, Any]] = [
    {
        "role": "user",
        "content": "Hello",
        "message_id": "m1",
        "created_at": 1000,
    },
    {
        "role": "assistant",
        "content": "Hi there",
        "message_id": "m2",
        "created_at": 2000,
        "tool_calls": [{"id": "tc1", "function": {"name": "read_file"}}],
    },
    {
        "role": "tool",
        "content": "file contents",
        "tool_call_id": "tc1",
        "tool_name": "read_file",
        "name": "read_file",
        "message_id": "m3",
        "created_at": 3000,
    },
]

_SOFT_DELETED_MESSAGE: dict[str, Any] = {
    "role": "user",
    "content": "old question",
    "message_id": "m0",
    "created_at": 500,
    "_deleted": True,
    "token_count": 10,
    "is_content_json": 0,
}


# ---------------------------------------------------------------------------
# Sample transcript events (external coding / T05)
# ---------------------------------------------------------------------------


def _evt(
    cls: Any,
    *,
    timestamp: int,
    **kwargs: Any,
) -> ServerEvent:
    """Build a ServerEvent with session_id/agent_name prefilled."""
    return cls(
        session_id=_SESSION_ID,
        agent_name=_AGENT_NAME,
        timestamp=timestamp,
        **kwargs,
    )


def _make_transcript_events() -> list[ServerEvent]:
    """Two turns: older turn 1 (text + tool call/result), newer turn 2 (text + reasoning).

    Turn 1 (started_at=1000):
      - AssistantTextEvent "Hello"
      - ToolCallEvent(call_id="c1", tool="read_file")
      - ToolResultEvent(call_id="c1", tool="read_file", result="file contents")

    Turn 2 (started_at=2000):
      - AssistantReasoningEvent "thinking..."  (discarded by projection)
      - AssistantTextEvent "World"
    """
    return [
        _evt(TurnStartEvent, timestamp=1000, turn_id="t1"),
        _evt(AssistantTextEvent, timestamp=1010, turn_id="t1", text="Hello"),
        _evt(
            ToolCallEvent,
            timestamp=1020,
            turn_id="t1",
            call_id="c1",
            tool_name="read_file",
            args={"path": "foo.txt"},
        ),
        _evt(
            ToolResultEvent,
            timestamp=1030,
            turn_id="t1",
            call_id="c1",
            tool_name="read_file",
            result="file contents",
        ),
        _evt(TurnStartEvent, timestamp=2000, turn_id="t2"),
        _evt(
            AssistantReasoningEvent,
            timestamp=2010,
            turn_id="t2",
            text="thinking...",
        ),
        _evt(AssistantTextEvent, timestamp=2020, turn_id="t2", text="World"),
    ]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_session_info(
    session_id: str = _SESSION_ID,
    agent_name: str = _AGENT_NAME,
) -> SessionInfo:
    return SessionInfo(session_id=session_id, agent_name=agent_name)


def _make_pool_spec(
    strategy: ExecutionStrategyKind = ExecutionStrategyKind.REACT,
) -> PoolSpec:
    kwargs: dict[str, Any] = {"name": _AGENT_NAME, "execution_strategy": strategy}
    if strategy == ExecutionStrategyKind.EXTERNAL:
        from modex_agent.core.agent import ProviderKind

        kwargs["provider_kind"] = ProviderKind.PI
    return PoolSpec(name=_POOL, agents=[AgentSpec(**kwargs)])


def _make_request(
    limit: int = 3,
    agent_name: str = _AGENT_NAME,
    pool: str = _POOL,
) -> HistoryRequest:
    return HistoryRequest(
        caller=AgentSessionRef(
            workspace=_WORKSPACE,
            pool=pool,
            session_id=_SESSION_ID,
            agent_name=agent_name,
        ),
        limit=limit,
    )


def _make_facade(
    *,
    session_info: SessionInfo | None = None,
    session_not_found: bool = False,
    messages: list[dict[str, Any]] | None = None,
    real_message_store: MessageStore | None = None,
    pool_spec: PoolSpec | None = None,
    transcript_events: list[ServerEvent] | None = None,
    transcript_store_none: bool = False,
) -> tuple[BotControlFacade, AsyncMock, AsyncMock]:
    """Build a facade with mocked dependencies.

    Returns ``(facade, mock_message_store, mock_transcript_store)`` so the
    test can assert on either store's load call.

    Pass ``session_not_found=True`` to simulate a missing session (the store
    returns ``None``).

    Pass ``transcript_events`` to seed the mock ``TranscriptStore.load`` return
    value (used by external tests). Pass ``transcript_store_none=True``
    to make the transcript-store provider raise ``ControlFacadeError(422,
    code="transcript_store_unavailable")``.

    Pass ``real_message_store`` to use a real ``MessageStore`` implementation
    (e.g. :class:`InMemoryScopedStorage`) instead of the mock — used to verify
    store-layer filtering (COMPACT exclusion) auto-benefits end-to-end.
    """
    # Mock SessionStore
    mock_session_store = MagicMock(spec=SessionStore)
    if session_not_found:
        mock_session_store.get = AsyncMock(return_value=None)
    else:
        mock_session_store.get = AsyncMock(
            return_value=session_info if session_info is not None else _make_session_info()
        )

    # Mock MessageStore
    mock_message_store = MagicMock(spec=MessageStore)
    mock_message_store.load_all_messages = AsyncMock(
        return_value=messages if messages is not None else _SAMPLE_MESSAGES
    )

    # Mock TranscriptStore
    mock_transcript_store = MagicMock(spec=TranscriptStore)
    mock_transcript_store.load = AsyncMock(
        return_value=transcript_events if transcript_events is not None else []
    )

    # Mock resources (only session_index_store + target are accessed)
    effective_pool_spec = pool_spec if pool_spec is not None else _make_pool_spec()

    mock_pool_instance = MagicMock()
    mock_pool_instance.main_execution_strategy = effective_pool_spec.root_agent.execution_strategy
    mock_pool_instance.root_agent_name = effective_pool_spec.root_agent.name
    mock_pool_instance.target_store.list = MagicMock(return_value=[])
    mock_pool_instance.target_store.get = MagicMock(
        return_value=MagicMock(
            execution_strategy=effective_pool_spec.root_agent.execution_strategy
        )
    )

    mock_resources = MagicMock()
    mock_resources.session_index_store = mock_session_store
    mock_resources.target = _WORKSPACE
    mock_resources.pools = {_POOL: mock_pool_instance}

    async def _workspace_resolver(_root: Path) -> Any:  # noqa: ANN401
        return mock_resources

    # Message store provider returns the real store when provided, else the mock
    async def _message_store_provider(_scope: BotRecordScope, _res: Any) -> MessageStore:  # noqa: ANN401
        return real_message_store if real_message_store is not None else mock_message_store

    # Transcript store provider
    if transcript_store_none:
        async def _transcript_store_provider(_res: Any) -> TranscriptStore:  # noqa: ANN401
            raise ControlFacadeError(
                422,
                ControlError(
                    code="transcript_store_unavailable",
                    message="Transcript store is not configured for this workspace",
                ),
            )
    else:
        async def _transcript_store_provider(_res: Any) -> TranscriptStore:  # noqa: ANN401
            return mock_transcript_store

    facade = BotControlFacade(
        workspace_resolver=_workspace_resolver,
        message_store_provider=_message_store_provider,
        transcript_store_provider=_transcript_store_provider,
        home_root=_WORKSPACE,
        relative_base=None,
    )
    return facade, mock_message_store, mock_transcript_store


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestHistoryHappyPath:
    @pytest.mark.asyncio
    async def test_returns_correct_result_with_sample_messages(self) -> None:
        facade, mock_store, _ = _make_facade()
        result = await facade.history(_make_request(limit=3))

        assert result.source == HistorySource.MESSAGE_STORE
        assert result.session_id == _SESSION_ID
        assert result.agent_name == _AGENT_NAME
        assert result.pool == _POOL
        assert result.execution_strategy == "react"
        assert result.effective_limit == 3
        assert len(result.items) == 3
        mock_store.load_all_messages.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_items_ordered_newest_first(self) -> None:
        facade, _, _ = _make_facade()
        result = await facade.history(_make_request(limit=10))

        created_ats = [int(m.created_at or 0) for m in result.items]
        assert created_ats == sorted(created_ats, reverse=True)
        assert created_ats[0] == 3000
        assert created_ats[-1] == 1000

    @pytest.mark.asyncio
    async def test_limit_truncates_result(self) -> None:
        facade, _, _ = _make_facade()
        result = await facade.history(_make_request(limit=2))
        assert len(result.items) == 2
        assert result.items[0].message_id == "m3"
        assert result.items[1].message_id == "m2"

    @pytest.mark.asyncio
    async def test_message_fields_projected_correctly(self) -> None:
        facade, _, _ = _make_facade()
        result = await facade.history(_make_request(limit=10))

        tool_msg = next(m for m in result.items if m.role == "tool")
        assert tool_msg.tool_call_id == "tc1"
        assert tool_msg.tool_name == "read_file"
        assert tool_msg.name == "read_file"
        assert tool_msg.content == "file contents"
        assert tool_msg.message_id == "m3"

        assistant_msg = next(m for m in result.items if m.role == "assistant")
        assert assistant_msg.tool_calls is not None
        assert len(assistant_msg.tool_calls) == 1


# ---------------------------------------------------------------------------
# Empty history
# ---------------------------------------------------------------------------


class TestEmptyHistory:
    @pytest.mark.asyncio
    async def test_empty_messages_returns_200_with_empty_items(self) -> None:
        facade, _, _ = _make_facade(messages=[])
        result = await facade.history(_make_request())
        assert result.items == []
        assert result.source == HistorySource.MESSAGE_STORE


# ---------------------------------------------------------------------------
# Soft-deleted messages
# ---------------------------------------------------------------------------


class TestSoftDeletedIncluded:
    @pytest.mark.asyncio
    async def test_soft_deleted_messages_are_included(self) -> None:
        messages = [_SOFT_DELETED_MESSAGE, *_SAMPLE_MESSAGES]
        facade, _, _ = _make_facade(messages=messages)
        result = await facade.history(_make_request(limit=10))

        message_ids = {m.message_id for m in result.items}
        assert "m0" in message_ids
        assert len(result.items) == 4

    @pytest.mark.asyncio
    async def test_internal_markers_stripped_from_result(self) -> None:
        messages = [_SOFT_DELETED_MESSAGE]
        facade, _, _ = _make_facade(messages=messages)
        result = await facade.history(_make_request(limit=10))

        assert len(result.items) == 1
        msg = result.items[0]
        assert msg.message_id == "m0"
        assert msg.role == "user"
        assert msg.content == "old question"

    @pytest.mark.asyncio
    async def test_soft_deleted_message_sorts_oldest(self) -> None:
        messages = [_SOFT_DELETED_MESSAGE, *_SAMPLE_MESSAGES]
        facade, _, _ = _make_facade(messages=messages)
        result = await facade.history(_make_request(limit=10))

        assert result.items[-1].message_id == "m0"
        assert int(result.items[-1].created_at or 0) == 500


# ---------------------------------------------------------------------------
# COMPACT exclusion — auto-benefit from store-layer filtering
# ---------------------------------------------------------------------------


class TestCompactExclusionAutoBenefit:
    """Verify the facade auto-benefits from store-layer COMPACT filtering.

    ``MessageStore.load_all_messages`` (store layer, todo 1) filters out
    COMPACT-role messages. ``BotControlFacade.history`` calls
    ``load_all_messages()`` directly (facade.py:198) with no role-level
    filtering of its own, so it auto-benefits from that store-layer filter.

    These tests use a REAL :class:`InMemoryScopedStorage` (which implements
    the COMPACT filter) rather than a ``MagicMock`` — a mock returning a
    compact message would pass it straight through (the facade does not
    filter by role), so only a real store can verify the auto-benefit
    end-to-end.
    """

    @pytest.mark.asyncio
    async def test_compact_role_messages_excluded_from_history(self) -> None:
        store = InMemoryScopedStorage()
        await store.save_messages(
            [
                {
                    "role": str(MessageRole.USER),
                    "content": "visible question",
                    "message_id": "m1",
                    "created_at": 1000,
                },
                {
                    "role": str(MessageRole.COMPACT),
                    "content": "compact summary",
                    "message_id": "m2",
                    "created_at": 2000,
                },
                {
                    "role": str(MessageRole.ASSISTANT),
                    "content": "visible reply",
                    "message_id": "m3",
                    "created_at": 3000,
                },
            ]
        )
        facade, _, _ = _make_facade(real_message_store=store)
        result = await facade.history(_make_request(limit=10))

        roles = {m.role for m in result.items}
        assert str(MessageRole.COMPACT) not in roles
        message_ids = {m.message_id for m in result.items}
        assert "m2" not in message_ids
        assert len(result.items) == 2

    @pytest.mark.asyncio
    async def test_soft_deleted_content_returned_matched_by_content(self) -> None:
        store = InMemoryScopedStorage()
        unique_content = "soft-deleted-unique-marker-9f3a"
        await store.save_messages(
            [
                {
                    "role": str(MessageRole.USER),
                    "content": unique_content,
                    "message_id": "m_del",
                    "created_at": 500,
                    "_deleted": True,
                },
                {
                    "role": str(MessageRole.USER),
                    "content": "active message",
                    "message_id": "m1",
                    "created_at": 1000,
                },
            ]
        )
        facade, _, _ = _make_facade(real_message_store=store)
        result = await facade.history(_make_request(limit=10))

        contents = {str(m.content) for m in result.items}
        assert unique_content in contents
        for m in result.items:
            assert "_deleted" not in m.model_dump()


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


class TestErrorPaths:
    @pytest.mark.asyncio
    async def test_main_agent_can_query_subagent_history_without_session_validation(
        self,
    ) -> None:
        """Main agent queries a subagent's history — session must NOT be
        validated against SessionIndexStore (subagent may not be registered
        yet, and the caller is the main agent, not the subagent)."""
        messages = [
            {"role": "user", "content": "task"},
            {"role": "assistant", "content": "done"},
        ]
        facade, _, _ = _make_facade(
            messages=messages,
        )
        request = HistoryRequest(
            caller=AgentSessionRef(
                workspace=_WORKSPACE,
                pool=_POOL,
                session_id="inv456.office-expert",
                agent_name="office-expert",
            ),
            limit=3,
        )
        result = await facade.history(request)
        assert len(result.items) == 2
        assert result.items[0].role == "user"
        assert result.items[1].role == "assistant"

    @pytest.mark.asyncio
    async def test_forbidden_target_rejects_unregistered_agent(self) -> None:
        facade, _, _ = _make_facade()
        request = HistoryRequest(
            caller=AgentSessionRef(
                workspace=_WORKSPACE,
                pool=_POOL,
                session_id="inv999.evil_agent",
                agent_name=_AGENT_NAME,
            ),
            limit=3,
        )
        with pytest.raises(ControlFacadeError) as exc_info:
            await facade.history(request)
        assert exc_info.value.status == 403
        assert exc_info.value.error.code == "forbidden_target"

    @pytest.mark.asyncio
    async def test_forbidden_target_rejects_peer_agent_session(self) -> None:
        facade, _, _ = _make_facade()
        request = HistoryRequest(
            caller=AgentSessionRef(
                workspace=_WORKSPACE,
                pool=_POOL,
                session_id="conv1.evil_agent",
                agent_name=_AGENT_NAME,
            ),
            limit=3,
        )
        with pytest.raises(ControlFacadeError) as exc_info:
            await facade.history(request)
        assert exc_info.value.status == 403
        assert exc_info.value.error.code == "forbidden_target"

    @pytest.mark.asyncio
    async def test_empty_session_id_rejected(self) -> None:
        facade, _, _ = _make_facade()
        request = HistoryRequest(
            caller=AgentSessionRef(
                workspace=_WORKSPACE,
                pool=_POOL,
                session_id="",
                agent_name=_AGENT_NAME,
            ),
            limit=3,
        )
        with pytest.raises(ControlFacadeError) as exc_info:
            await facade.history(request)
        assert exc_info.value.status == 400
        assert exc_info.value.error.code == "invalid_request"


# ---------------------------------------------------------------------------
# External coding transcript path (T05)
# ---------------------------------------------------------------------------


class TestTranscriptHistory:
    """Seam 1 — external strategy reads from the observable transcript."""

    @pytest.mark.asyncio
    async def test_returns_observable_transcript_source(self) -> None:
        spec = _make_pool_spec(strategy=ExecutionStrategyKind.EXTERNAL)
        facade, _, _ = _make_facade(
            pool_spec=spec, transcript_events=_make_transcript_events()
        )
        result = await facade.history(_make_request())
        assert result.source == HistorySource.OBSERVABLE_TRANSCRIPT
        assert result.execution_strategy == "external"
        assert result.session_id == _SESSION_ID
        assert result.agent_name == _AGENT_NAME
        assert result.pool == _POOL
        assert result.effective_limit == 3

    @pytest.mark.asyncio
    async def test_loads_exact_session_id_no_prefix_fanin(self) -> None:
        spec = _make_pool_spec(strategy=ExecutionStrategyKind.EXTERNAL)
        facade, _, mock_transcript = _make_facade(
            pool_spec=spec, transcript_events=_make_transcript_events()
        )
        await facade.history(_make_request())
        mock_transcript.load.assert_awaited_once_with(_SESSION_ID)

    @pytest.mark.asyncio
    async def test_materializes_events_before_limiting(self) -> None:
        spec = _make_pool_spec(strategy=ExecutionStrategyKind.EXTERNAL)
        facade, _, _ = _make_facade(
            pool_spec=spec, transcript_events=_make_transcript_events()
        )
        result = await facade.history(_make_request(limit=1))
        # 3 logical records (text t2, text t1, tool t1) → limited to 1.
        assert len(result.items) == 1
        assert result.items[0].content == "World"

    @pytest.mark.asyncio
    async def test_coalesces_text_blocks_per_turn(self) -> None:
        spec = _make_pool_spec(strategy=ExecutionStrategyKind.EXTERNAL)
        facade, _, _ = _make_facade(
            pool_spec=spec, transcript_events=_make_transcript_events()
        )
        result = await facade.history(_make_request(limit=10))
        assistant_msgs = [m for m in result.items if m.role == "assistant"]
        assert len(assistant_msgs) == 2
        assert {str(m.content) for m in assistant_msgs} == {"Hello", "World"}

    @pytest.mark.asyncio
    async def test_pairs_tool_calls_with_results_by_call_id(self) -> None:
        spec = _make_pool_spec(strategy=ExecutionStrategyKind.EXTERNAL)
        facade, _, _ = _make_facade(
            pool_spec=spec, transcript_events=_make_transcript_events()
        )
        result = await facade.history(_make_request(limit=10))
        tool_msgs = [m for m in result.items if m.role == "tool"]
        assert len(tool_msgs) == 1
        assert tool_msgs[0].content == "file contents"
        assert tool_msgs[0].tool_name == "read_file"

    @pytest.mark.asyncio
    async def test_message_id_absent_from_transcript_records(self) -> None:
        spec = _make_pool_spec(strategy=ExecutionStrategyKind.EXTERNAL)
        facade, _, _ = _make_facade(
            pool_spec=spec, transcript_events=_make_transcript_events()
        )
        result = await facade.history(_make_request(limit=10))
        for item in result.items:
            assert item.message_id is None
            dumped = item.model_dump(exclude_none=True)
            assert "message_id" not in dumped

    @pytest.mark.asyncio
    async def test_no_fabricated_fields(self) -> None:
        spec = _make_pool_spec(strategy=ExecutionStrategyKind.EXTERNAL)
        facade, _, _ = _make_facade(
            pool_spec=spec, transcript_events=_make_transcript_events()
        )
        result = await facade.history(_make_request(limit=10))
        for item in result.items:
            dumped = item.model_dump(exclude_none=True)
            assert "tool_call_id" not in dumped
            assert "tool_calls" not in dumped
            assert "name" not in dumped

    @pytest.mark.asyncio
    async def test_limit_applied_to_logical_records_not_events(self) -> None:
        spec = _make_pool_spec(strategy=ExecutionStrategyKind.EXTERNAL)
        facade, _, mock_transcript = _make_facade(
            pool_spec=spec, transcript_events=_make_transcript_events()
        )
        result = await facade.history(_make_request(limit=2))
        assert len(result.items) == 2
        # load() is called once with the full event list — not pre-limited.
        mock_transcript.load.assert_awaited_once()
        loaded_events = mock_transcript.load.await_args.args[0]
        assert loaded_events == _SESSION_ID

    @pytest.mark.asyncio
    async def test_ordering_newest_first(self) -> None:
        spec = _make_pool_spec(strategy=ExecutionStrategyKind.EXTERNAL)
        facade, _, _ = _make_facade(
            pool_spec=spec, transcript_events=_make_transcript_events()
        )
        result = await facade.history(_make_request(limit=10))
        created_ats = [int(m.created_at or 0) for m in result.items]
        assert created_ats == sorted(created_ats, reverse=True)
        assert created_ats[0] == 2000
        assert created_ats[-1] == 1000

    @pytest.mark.asyncio
    async def test_reasoning_blocks_discarded(self) -> None:
        spec = _make_pool_spec(strategy=ExecutionStrategyKind.EXTERNAL)
        facade, _, _ = _make_facade(
            pool_spec=spec, transcript_events=_make_transcript_events()
        )
        result = await facade.history(_make_request(limit=10))
        assert len(result.items) == 3
        assert all(m.content != "thinking..." for m in result.items)


class TestTranscriptEmpty:
    @pytest.mark.asyncio
    async def test_empty_transcript_returns_empty_items(self) -> None:
        spec = _make_pool_spec(strategy=ExecutionStrategyKind.EXTERNAL)
        facade, _, _ = _make_facade(pool_spec=spec, transcript_events=[])
        result = await facade.history(_make_request())
        assert result.items == []
        assert result.source == HistorySource.OBSERVABLE_TRANSCRIPT


class TestTranscriptStoreUnavailable:
    @pytest.mark.asyncio
    async def test_transcript_store_none_raises_422(self) -> None:
        spec = _make_pool_spec(strategy=ExecutionStrategyKind.EXTERNAL)
        facade, _, _ = _make_facade(
            pool_spec=spec, transcript_store_none=True
        )
        with pytest.raises(ControlFacadeError) as exc_info:
            await facade.history(_make_request())
        assert exc_info.value.status == 422
        assert exc_info.value.error.code == "transcript_store_unavailable"
