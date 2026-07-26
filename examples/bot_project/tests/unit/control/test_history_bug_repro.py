"""Regression guard: mixed pool history path selection.

Simulates the REAL coder pool configuration:
  - main agent: orchestrator (react)
  - subagent: coder (external_coding, opencode)

When orchestrator dispatches a task to coder and then queries coder's history,
the facade must read from TranscriptStore (external_coding path), not
MessageStore (native path). Guards against the regression where the facade used
``main_execution_strategy`` instead of the subagent's ``execution_strategy``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from bot.control.facade import BotControlFacade
from bot.control.models import (
    AgentSessionRef,
    HistoryRequest,
    HistorySource,
)
from bot.scope import BotRecordScope
from bot.webui.events import (
    AssistantTextEvent,
    ServerEvent,
    ToolCallEvent,
    ToolResultEvent,
    TurnStartEvent,
)
from bot.webui.transcript_store import TranscriptStore

from modex_agent.core.agent import AgentCommKind
from modex_agent.core.constants import ExecutionStrategyKind
from modex_agent.memory.core.split_stores import MessageStore
from modex_agent.multi_agent.tools import CommunicationTarget, CommunicationTargetStore

_WORKSPACE = Path("/home/user/project")
_POOL = "coder"
_MAIN_AGENT = "orchestrator"
_SUBAGENT = "coder"
_INVOCATION_ID = "abc12345"
_SUBAGENT_SESSION = f"{_INVOCATION_ID}.{_SUBAGENT}"


def _make_transcript_events() -> list[ServerEvent]:
    """One turn with a text block and a tool call/result pair."""
    return [
        TurnStartEvent(session_id=_SUBAGENT_SESSION, agent_name=_SUBAGENT, timestamp=1000, turn_id="t1"),
        AssistantTextEvent(session_id=_SUBAGENT_SESSION, agent_name=_SUBAGENT, timestamp=1010, turn_id="t1", text="Implementing the fix"),
        ToolCallEvent(session_id=_SUBAGENT_SESSION, agent_name=_SUBAGENT, timestamp=1020, turn_id="t1", call_id="c1", tool_name="edit", args={}),
        ToolResultEvent(session_id=_SUBAGENT_SESSION, agent_name=_SUBAGENT, timestamp=1030, turn_id="t1", call_id="c1", tool_name="edit", result="edited"),
    ]


def _make_facade_mixed_pool(
    *,
    transcript_events: list[ServerEvent] | None = None,
    native_messages: list[dict[str, Any]] | None = None,
) -> tuple[BotControlFacade, MagicMock, MagicMock]:
    """Build a facade simulating the real coder pool.

    - main agent: orchestrator (react) — no native messages for the subagent session
    - subagent: coder (external_coding) — transcript has events

    The mock MessageStore returns empty (external subagent has no native memory).
    The mock TranscriptStore returns the sample events.
    """
    # Mock MessageStore — returns empty for the subagent session (external has no native memory)
    mock_message_store = MagicMock(spec=MessageStore)
    mock_message_store.load_all_messages = AsyncMock(
        return_value=native_messages if native_messages is not None else []
    )

    # Mock TranscriptStore — has the external subagent's events
    mock_transcript_store = MagicMock(spec=TranscriptStore)
    mock_transcript_store.load = AsyncMock(
        return_value=transcript_events if transcript_events is not None else _make_transcript_events()
    )

    # Build CommunicationTargetStore with the external subagent
    target_store = CommunicationTargetStore(for_subagent=False)
    target_store.add(CommunicationTarget(
        name=_SUBAGENT,
        kind=AgentCommKind.SUBAGENT,
        pool_name=_POOL,
        execution_strategy=ExecutionStrategyKind.EXTERNAL_CODING,
        description="external coding subagent",
    ))

    # Mock PoolInstance: main=react (orchestrator), subagent=coder(external_coding)
    mock_pool_instance = MagicMock()
    mock_pool_instance.main_execution_strategy = ExecutionStrategyKind.REACT  # orchestrator is react
    mock_pool_instance.main_agent_name = _MAIN_AGENT
    mock_pool_instance.target_store = target_store

    mock_resources = MagicMock()
    mock_resources.target = _WORKSPACE
    mock_resources.pools = {_POOL: mock_pool_instance}

    async def _workspace_resolver(_root: Path) -> Any:  # noqa: ANN401
        return mock_resources

    async def _message_store_provider(_scope: BotRecordScope, _res: Any) -> MessageStore:  # noqa: ANN401
        return mock_message_store

    async def _transcript_store_provider(_res: Any) -> TranscriptStore:  # noqa: ANN401
        return mock_transcript_store

    facade = BotControlFacade(
        workspace_resolver=_workspace_resolver,
        agent_pool_map={_MAIN_AGENT: _POOL, _SUBAGENT: _POOL},
        message_store_provider=_message_store_provider,
        transcript_store_provider=_transcript_store_provider,
        home_root=_WORKSPACE,
        relative_base=None,
    )
    return facade, mock_message_store, mock_transcript_store


class TestMixedPoolExternalSubagentHistory:
    """The bug: main=react, subagent=external_coding → wrong path selected."""

    @pytest.mark.asyncio
    async def test_external_subagent_uses_transcript_not_messagestore(self) -> None:
        """Querying an external_coding subagent's history must read from
        TranscriptStore, not MessageStore."""
        facade, mock_message_store, mock_transcript_store = _make_facade_mixed_pool()

        request = HistoryRequest(
            caller=AgentSessionRef(
                workspace=_WORKSPACE,
                pool=_POOL,
                session_id=_SUBAGENT_SESSION,
                agent_name=_SUBAGENT,
            ),
            limit=3,
        )
        result = await facade.history(request)

        # The source MUST be OBSERVABLE_TRANSCRIPT, not MESSAGE_STORE.
        assert result.source == HistorySource.OBSERVABLE_TRANSCRIPT, (
            f"Expected OBSERVABLE_TRANSCRIPT for external_coding subagent, "
            f"got {result.source}. The facade used main_execution_strategy "
            f"instead of the subagent's execution_strategy."
        )
        # TranscriptStore.load MUST be called.
        mock_transcript_store.load.assert_awaited_once_with(_SUBAGENT_SESSION)
        # MessageStore.load_all_messages MUST NOT be called.
        mock_message_store.load_all_messages.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_external_subagent_returns_nonempty_history(self) -> None:
        """The external subagent HAS history in TranscriptStore — must not be empty."""
        facade, _, _ = _make_facade_mixed_pool()

        request = HistoryRequest(
            caller=AgentSessionRef(
                workspace=_WORKSPACE,
                pool=_POOL,
                session_id=_SUBAGENT_SESSION,
                agent_name=_SUBAGENT,
            ),
            limit=3,
        )
        result = await facade.history(request)

        # Must have items (from transcript), not empty.
        assert len(result.items) > 0, (
            "External subagent history is empty — the facade walked the native "
            "MessageStore path (which has no records for external subagents) "
            "instead of the TranscriptStore path."
        )

    @pytest.mark.asyncio
    async def test_execution_strategy_reflects_subagent_not_main(self) -> None:
        """The result's execution_strategy field must be 'external_coding',
        not 'react' (which is the main agent's strategy)."""
        facade, _, _ = _make_facade_mixed_pool()

        request = HistoryRequest(
            caller=AgentSessionRef(
                workspace=_WORKSPACE,
                pool=_POOL,
                session_id=_SUBAGENT_SESSION,
                agent_name=_SUBAGENT,
            ),
            limit=3,
        )
        result = await facade.history(request)

        assert result.execution_strategy == "external_coding", (
            f"Expected 'external_coding' (subagent's strategy), "
            f"got {result.execution_strategy!r} (main agent's strategy)."
        )


class TestNativeSubagentStillWorks:
    """Regression guard: native subagent (react) history must still work
    after the fix — it should use MessageStore, not TranscriptStore."""

    @pytest.mark.asyncio
    async def test_native_subagent_uses_messagestore(self) -> None:
        """Querying a native (react) subagent's history must read from
        MessageStore, not TranscriptStore."""
        native_messages = [
            {"role": "user", "content": "explore task", "message_id": "m1", "created_at": 1000},
            {"role": "assistant", "content": "found it", "message_id": "m2", "created_at": 2000},
        ]

        mock_message_store = MagicMock(spec=MessageStore)
        mock_message_store.load_all_messages = AsyncMock(return_value=native_messages)

        mock_transcript_store = MagicMock(spec=TranscriptStore)
        mock_transcript_store.load = AsyncMock(return_value=[])

        target_store = CommunicationTargetStore(for_subagent=False)
        target_store.add(CommunicationTarget(
            name="explore",
            kind=AgentCommKind.SUBAGENT,
            pool_name=_POOL,
            execution_strategy=ExecutionStrategyKind.REACT,
            description="native explore subagent",
        ))

        mock_pool_instance = MagicMock()
        mock_pool_instance.main_execution_strategy = ExecutionStrategyKind.REACT
        mock_pool_instance.main_agent_name = _MAIN_AGENT
        mock_pool_instance.target_store = target_store

        mock_resources = MagicMock()
        mock_resources.target = _WORKSPACE
        mock_resources.pools = {_POOL: mock_pool_instance}

        async def _workspace_resolver(_root: Path) -> Any:  # noqa: ANN401
            return mock_resources

        async def _message_store_provider(_scope: BotRecordScope, _res: Any) -> MessageStore:  # noqa: ANN401
            return mock_message_store

        async def _transcript_store_provider(_res: Any) -> TranscriptStore:  # noqa: ANN401
            return mock_transcript_store

        facade = BotControlFacade(
            workspace_resolver=_workspace_resolver,
            agent_pool_map={_MAIN_AGENT: _POOL, "explore": _POOL},
            message_store_provider=_message_store_provider,
            transcript_store_provider=_transcript_store_provider,
            home_root=_WORKSPACE,
            relative_base=None,
        )

        request = HistoryRequest(
            caller=AgentSessionRef(
                workspace=_WORKSPACE,
                pool=_POOL,
                session_id="inv123.explore",
                agent_name="explore",
            ),
            limit=3,
        )
        result = await facade.history(request)

        assert result.source == HistorySource.MESSAGE_STORE
        assert len(result.items) == 2
        mock_message_store.load_all_messages.assert_awaited_once()
        mock_transcript_store.load.assert_not_awaited()


class TestExternalMainAgentSelfHistory:
    """External coding main agent (e.g. opencode pool) querying its own history.

    This is the opencode pool configuration:
      main agent: opencode (external_coding)

    The main agent's own session has NO MessageStore records (external agents
    skip native memory assembly). Its history lives in TranscriptStore. The
    facade must select the transcript path via main_execution_strategy.
    """

    @pytest.mark.asyncio
    async def test_external_main_agent_uses_transcript_path(self) -> None:
        mock_message_store = MagicMock(spec=MessageStore)
        mock_message_store.load_all_messages = AsyncMock(return_value=[])

        mock_transcript_store = MagicMock(spec=TranscriptStore)
        mock_transcript_store.load = AsyncMock(
            return_value=[
                TurnStartEvent(session_id="conv1.opencode", agent_name="opencode", timestamp=1000, turn_id="t1"),
                AssistantTextEvent(session_id="conv1.opencode", agent_name="opencode", timestamp=1010, turn_id="t1", text="Working on it"),
            ]
        )

        mock_pool_instance = MagicMock()
        mock_pool_instance.main_execution_strategy = ExecutionStrategyKind.EXTERNAL_CODING
        mock_pool_instance.main_agent_name = "opencode"
        mock_pool_instance.target_store.list = MagicMock(return_value=[])
        mock_pool_instance.target_store.get = MagicMock(return_value=None)

        mock_resources = MagicMock()
        mock_resources.target = _WORKSPACE
        mock_resources.pools = {"opencode": mock_pool_instance}

        async def _workspace_resolver(_root: Path) -> Any:  # noqa: ANN401
            return mock_resources

        async def _message_store_provider(_scope: BotRecordScope, _res: Any) -> MessageStore:  # noqa: ANN401
            return mock_message_store

        async def _transcript_store_provider(_res: Any) -> TranscriptStore:  # noqa: ANN401
            return mock_transcript_store

        facade = BotControlFacade(
            workspace_resolver=_workspace_resolver,
            agent_pool_map={"opencode": "opencode"},
            message_store_provider=_message_store_provider,
            transcript_store_provider=_transcript_store_provider,
            home_root=_WORKSPACE,
            relative_base=None,
        )

        request = HistoryRequest(
            caller=AgentSessionRef(
                workspace=_WORKSPACE,
                pool="opencode",
                session_id="conv1.opencode",
                agent_name="opencode",
            ),
            limit=3,
        )
        result = await facade.history(request)

        assert result.source == HistorySource.OBSERVABLE_TRANSCRIPT
        assert result.execution_strategy == "external_coding"
        assert len(result.items) > 0
        mock_transcript_store.load.assert_awaited_once_with("conv1.opencode")
        mock_message_store.load_all_messages.assert_not_awaited()


class TestSubagentCannotReadMainAgentHistory:
    """D26 authorization: a subagent must NOT read the main agent's history.

    Only self-history and subagent-under-caller are authorized. Reading the
    main agent's session by a non-main caller is forbidden (403).
    """

    @pytest.mark.asyncio
    async def test_subagent_reading_main_agent_history_raises_403(self) -> None:
        from bot.control.facade import ControlFacadeError

        mock_message_store = MagicMock(spec=MessageStore)
        mock_message_store.load_all_messages = AsyncMock(return_value=[])

        mock_transcript_store = MagicMock(spec=TranscriptStore)
        mock_transcript_store.load = AsyncMock(return_value=[])

        target_store = CommunicationTargetStore(for_subagent=False)
        target_store.add(CommunicationTarget(
            name=_SUBAGENT,
            kind=AgentCommKind.SUBAGENT,
            pool_name=_POOL,
            execution_strategy=ExecutionStrategyKind.EXTERNAL_CODING,
            description="external coding subagent",
        ))

        mock_pool_instance = MagicMock()
        mock_pool_instance.main_execution_strategy = ExecutionStrategyKind.REACT
        mock_pool_instance.main_agent_name = _MAIN_AGENT
        mock_pool_instance.target_store = target_store

        mock_resources = MagicMock()
        mock_resources.target = _WORKSPACE
        mock_resources.pools = {_POOL: mock_pool_instance}

        async def _workspace_resolver(_root: Path) -> Any:  # noqa: ANN401
            return mock_resources

        async def _message_store_provider(_scope: BotRecordScope, _res: Any) -> MessageStore:  # noqa: ANN401
            return mock_message_store

        async def _transcript_store_provider(_res: Any) -> TranscriptStore:  # noqa: ANN401
            return mock_transcript_store

        facade = BotControlFacade(
            workspace_resolver=_workspace_resolver,
            agent_pool_map={_SUBAGENT: _POOL},
            message_store_provider=_message_store_provider,
            transcript_store_provider=_transcript_store_provider,
            home_root=_WORKSPACE,
            relative_base=None,
        )

        request = HistoryRequest(
            caller=AgentSessionRef(
                workspace=_WORKSPACE,
                pool=_POOL,
                session_id="abc12345.orchestrator",
                agent_name=_SUBAGENT,
            ),
            limit=3,
        )
        with pytest.raises(ControlFacadeError) as exc_info:
            await facade.history(request)
        assert exc_info.value.status == 403
        assert exc_info.value.error.code == "forbidden_target"
