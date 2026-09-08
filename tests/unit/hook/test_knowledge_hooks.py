from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from unittest.mock import MagicMock

from modex_agent.agents.react.state import ReActTurnState
from modex_agent.core.agent import AgentContext
from modex_agent.core.emitter import AgentResult, StopReason
from modex_agent.core.message import ChatMessage, MessageRole
from modex_agent.core.session_id import SessionInfo
from modex_agent.hook.builtin.knowledge_hook import KnowledgeHook
from modex_agent.memory.history import ListMessageHistory
from modex_agent.runtime.enums import AgentKind, TurnCustomKey, TurnPhase
from modex_agent.runtime.models import TurnIdentity
from modex_agent.runtime.services import AgentRuntime, AgentRuntimeServices
from modex_agent.tools.manager import InMemoryToolManager


def _make_context(
    knowledge_dir: str | None = ".",
) -> tuple[AgentContext, ReActTurnState]:
    identity = TurnIdentity(
        agent_id="test",
        session=SessionInfo.from_str("session.agent"),
        turn_id="turn-1",
    )
    state = ReActTurnState(
        identity=identity,
        agent_kind=AgentKind.REACT,
        phase=TurnPhase.RUNNING,
        turn_attempt=1,
    )
    state.custom[TurnCustomKey.MAX_TURNS] = 3
    if knowledge_dir is not None:
        state.custom[TurnCustomKey.GRAPH_KNOWLEDGE_DIR] = knowledge_dir
    runtime = AgentRuntime(services=AgentRuntimeServices(), state=state)
    context = AgentContext(
        system_prompt="test",
        history=ListMessageHistory(),
        tool_manager=InMemoryToolManager(),
        session=identity.session,
        runtime=runtime,
        graph_context=MagicMock(),
        identity=identity,
    )
    return context, state


def _str_content(messages: Sequence[ChatMessage], index: int = 0) -> str:
    content = messages[index].content
    assert isinstance(content, str)
    return content


async def _assert_no_history_append(context: AgentContext) -> None:
    messages = await context.history.to_list()
    assert messages == []


# ============================================================
# before_turn: counter reset
# ============================================================


async def test_counter_reset_zeroes_read_and_write_counts() -> None:
    context, state = _make_context()
    state.custom[TurnCustomKey.GRAPH_KNOWLEDGE_READ_COUNT] = 3
    state.custom[TurnCustomKey.GRAPH_KNOWLEDGE_WRITE_COUNT] = 2
    state.custom[TurnCustomKey.GRAPH_DELIVER_COUNT] = 1

    await KnowledgeHook().before_turn(context)

    assert state.custom[TurnCustomKey.GRAPH_KNOWLEDGE_READ_COUNT] == 0
    assert state.custom[TurnCustomKey.GRAPH_KNOWLEDGE_WRITE_COUNT] == 0


async def test_counter_reset_preserves_deliver_count() -> None:
    context, state = _make_context()
    state.custom[TurnCustomKey.GRAPH_KNOWLEDGE_READ_COUNT] = 5
    state.custom[TurnCustomKey.GRAPH_KNOWLEDGE_WRITE_COUNT] = 4
    state.custom[TurnCustomKey.GRAPH_DELIVER_COUNT] = 2

    await KnowledgeHook().before_turn(context)

    assert state.custom[TurnCustomKey.GRAPH_DELIVER_COUNT] == 2


async def test_counter_reset_missing_react_state_is_noop() -> None:
    context, state = _make_context()
    state.custom[TurnCustomKey.GRAPH_KNOWLEDGE_READ_COUNT] = 3
    context.runtime = None

    await KnowledgeHook().before_turn(context)

    assert state.custom[TurnCustomKey.GRAPH_KNOWLEDGE_READ_COUNT] == 3


# ============================================================
# before_turn: summary injection
# ============================================================


async def test_summary_no_knowledge_dir_is_noop() -> None:
    context, _state = _make_context(knowledge_dir=None)

    await KnowledgeHook().before_turn(context)

    await _assert_no_history_append(context)


async def test_summary_missing_react_state_is_noop(tmp_path: Path) -> None:
    context, _state = _make_context(knowledge_dir=str(tmp_path))
    context.runtime = None

    await KnowledgeHook().before_turn(context)

    await _assert_no_history_append(context)


async def test_summary_dir_with_no_files_injects_not_created(tmp_path: Path) -> None:
    context, _state = _make_context(knowledge_dir=str(tmp_path))

    await KnowledgeHook().before_turn(context)

    messages = await context.history.to_list()
    assert len(messages) == 1
    assert messages[0].role == MessageRole.SYSTEM_REMINDER
    content = _str_content(messages)
    assert "<knowledge_base>" in content
    assert "Findings: not yet created" in content
    assert "Open questions: not yet created" in content
    assert "action='write'" in content
    assert "pattern='findings'" in content
    assert "pattern='open_questions'" in content


async def test_summary_injects_findings(tmp_path: Path) -> None:
    (tmp_path / "findings.md").write_text("Found API endpoint at /v2", encoding="utf-8")
    context, _state = _make_context(knowledge_dir=str(tmp_path))

    await KnowledgeHook().before_turn(context)

    messages = await context.history.to_list()
    assert len(messages) == 1
    assert messages[0].role == MessageRole.SYSTEM_REMINDER
    content = _str_content(messages)
    assert "<knowledge_base>" in content
    assert "Found API endpoint at /v2" in content
    assert "Findings (current content)" in content
    assert "Open questions: not yet created" in content
    assert "action='read' pattern='findings'" in content


async def test_summary_injects_open_questions(tmp_path: Path) -> None:
    (tmp_path / "open_questions.md").write_text("Why does build fail?", encoding="utf-8")
    context, _state = _make_context(knowledge_dir=str(tmp_path))

    await KnowledgeHook().before_turn(context)

    messages = await context.history.to_list()
    assert len(messages) == 1
    assert messages[0].role == MessageRole.SYSTEM_REMINDER
    content = _str_content(messages)
    assert "<knowledge_base>" in content
    assert "Why does build fail?" in content
    assert "Open questions" in content
    assert "Recent findings" not in content


async def test_summary_injects_both_findings_and_open_questions(tmp_path: Path) -> None:
    (tmp_path / "findings.md").write_text("Discovered the auth flow", encoding="utf-8")
    (tmp_path / "open_questions.md").write_text("Is rate limiting enabled?", encoding="utf-8")
    context, _state = _make_context(knowledge_dir=str(tmp_path))

    await KnowledgeHook().before_turn(context)

    messages = await context.history.to_list()
    assert len(messages) == 1
    content = _str_content(messages)
    assert "Discovered the auth flow" in content
    assert "Is rate limiting enabled?" in content
    assert "Findings (current content)" in content
    assert "Open questions (current content)" in content
    assert "action='read' pattern='findings'" in content
    assert "action='read' pattern='open_questions'" in content


async def test_summary_truncates_long_findings(tmp_path: Path) -> None:
    line = "This is a repeated finding line for truncation testing. " * 5
    content_body = "START_MARKER_UNIQUE\n" + (line + "\n") * 12 + "END_MARKER_UNIQUE"
    (tmp_path / "findings.md").write_text(content_body, encoding="utf-8")
    context, _state = _make_context(knowledge_dir=str(tmp_path))

    await KnowledgeHook().before_turn(context)

    messages = await context.history.to_list()
    assert len(messages) == 1
    content = _str_content(messages)
    assert "truncated" in content
    assert "START_MARKER_UNIQUE" not in content
    assert "END_MARKER_UNIQUE" in content


# ============================================================
# before_turn: counter reset + summary ordering
# ============================================================


async def test_before_turn_resets_counters_then_injects_summary(tmp_path: Path) -> None:
    (tmp_path / "findings.md").write_text("data", encoding="utf-8")
    context, state = _make_context(knowledge_dir=str(tmp_path))
    state.custom[TurnCustomKey.GRAPH_KNOWLEDGE_READ_COUNT] = 5
    state.custom[TurnCustomKey.GRAPH_KNOWLEDGE_WRITE_COUNT] = 3

    await KnowledgeHook().before_turn(context)

    assert state.custom[TurnCustomKey.GRAPH_KNOWLEDGE_READ_COUNT] == 0
    assert state.custom[TurnCustomKey.GRAPH_KNOWLEDGE_WRITE_COUNT] == 0
    messages = await context.history.to_list()
    assert len(messages) == 1


# ============================================================
# after_turn: retry enforcement
# ============================================================


async def test_retry_require_read_missing_sets_continuation() -> None:
    context, state = _make_context()
    state.custom[TurnCustomKey.GRAPH_KNOWLEDGE_REQUIRE_READ] = True
    state.custom[TurnCustomKey.GRAPH_KNOWLEDGE_READ_COUNT] = 0
    state.custom[TurnCustomKey.GRAPH_KNOWLEDGE_HAS_READABLE] = True

    await KnowledgeHook().after_turn(
        context,
        AgentResult(content="done", stop_reason=StopReason.COMPLETED),
    )

    assert state.custom[TurnCustomKey.CONTINUATION_REQUEST] is True
    messages = await context.history.to_list()
    assert len(messages) == 1
    assert messages[0].role == MessageRole.SYSTEM_REMINDER
    content = _str_content(messages)
    assert "knowledge base" in content
    assert "read" in content


async def test_retry_require_write_missing_sets_continuation() -> None:
    context, state = _make_context()
    state.custom[TurnCustomKey.GRAPH_KNOWLEDGE_REQUIRE_WRITE] = True
    state.custom[TurnCustomKey.GRAPH_KNOWLEDGE_WRITE_COUNT] = 0

    await KnowledgeHook().after_turn(
        context,
        AgentResult(content="done", stop_reason=StopReason.COMPLETED),
    )

    assert state.custom[TurnCustomKey.CONTINUATION_REQUEST] is True
    messages = await context.history.to_list()
    assert len(messages) == 1
    content = _str_content(messages)
    assert "write" in content


async def test_retry_require_read_satisfied_does_nothing() -> None:
    context, state = _make_context()
    state.custom[TurnCustomKey.GRAPH_KNOWLEDGE_REQUIRE_READ] = True
    state.custom[TurnCustomKey.GRAPH_KNOWLEDGE_READ_COUNT] = 1

    await KnowledgeHook().after_turn(
        context,
        AgentResult(content="done", stop_reason=StopReason.COMPLETED),
    )

    assert TurnCustomKey.CONTINUATION_REQUEST not in state.custom
    await _assert_no_history_append(context)


async def test_retry_require_write_satisfied_does_nothing() -> None:
    context, state = _make_context()
    state.custom[TurnCustomKey.GRAPH_KNOWLEDGE_REQUIRE_WRITE] = True
    state.custom[TurnCustomKey.GRAPH_KNOWLEDGE_WRITE_COUNT] = 1

    await KnowledgeHook().after_turn(
        context,
        AgentResult(content="done", stop_reason=StopReason.COMPLETED),
    )

    assert TurnCustomKey.CONTINUATION_REQUEST not in state.custom
    await _assert_no_history_append(context)


async def test_retry_no_requirements_does_nothing() -> None:
    context, state = _make_context()

    await KnowledgeHook().after_turn(
        context,
        AgentResult(content="done", stop_reason=StopReason.COMPLETED),
    )

    assert TurnCustomKey.CONTINUATION_REQUEST not in state.custom
    await _assert_no_history_append(context)


async def test_retry_both_required_both_missing_mentions_both() -> None:
    context, state = _make_context()
    state.custom[TurnCustomKey.GRAPH_KNOWLEDGE_REQUIRE_READ] = True
    state.custom[TurnCustomKey.GRAPH_KNOWLEDGE_REQUIRE_WRITE] = True
    state.custom[TurnCustomKey.GRAPH_KNOWLEDGE_READ_COUNT] = 0
    state.custom[TurnCustomKey.GRAPH_KNOWLEDGE_WRITE_COUNT] = 0
    state.custom[TurnCustomKey.GRAPH_KNOWLEDGE_HAS_READABLE] = True

    await KnowledgeHook().after_turn(
        context,
        AgentResult(content="done", stop_reason=StopReason.COMPLETED),
    )

    assert state.custom[TurnCustomKey.CONTINUATION_REQUEST] is True
    messages = await context.history.to_list()
    assert len(messages) == 1
    content = _str_content(messages)
    assert "read and write" in content


async def test_retry_existing_continuation_request_still_injects_reminder() -> None:
    context, state = _make_context()
    state.custom[TurnCustomKey.GRAPH_KNOWLEDGE_REQUIRE_READ] = True
    state.custom[TurnCustomKey.GRAPH_KNOWLEDGE_READ_COUNT] = 0
    state.custom[TurnCustomKey.GRAPH_KNOWLEDGE_HAS_READABLE] = True
    state.custom[TurnCustomKey.CONTINUATION_REQUEST] = True

    await KnowledgeHook().after_turn(
        context,
        AgentResult(content="done", stop_reason=StopReason.COMPLETED),
    )

    assert state.custom[TurnCustomKey.CONTINUATION_REQUEST] is True
    messages = await context.history.to_list()
    assert len(messages) == 1
    assert messages[0].role == MessageRole.SYSTEM_REMINDER


async def test_retry_turn_cancelled_skips() -> None:
    context, state = _make_context()
    state.custom[TurnCustomKey.GRAPH_KNOWLEDGE_REQUIRE_READ] = True
    state.custom[TurnCustomKey.GRAPH_KNOWLEDGE_READ_COUNT] = 0

    await KnowledgeHook().after_turn(
        context,
        AgentResult(content="partial", stop_reason=StopReason.TURN_CANCELLED),
    )

    assert TurnCustomKey.CONTINUATION_REQUEST not in state.custom
    await _assert_no_history_append(context)


async def test_retry_error_result_skips() -> None:
    context, state = _make_context()
    state.custom[TurnCustomKey.GRAPH_KNOWLEDGE_REQUIRE_READ] = True
    state.custom[TurnCustomKey.GRAPH_KNOWLEDGE_READ_COUNT] = 0

    await KnowledgeHook().after_turn(
        context,
        AgentResult(error="failed", stop_reason=StopReason.ERROR),
    )

    assert TurnCustomKey.CONTINUATION_REQUEST not in state.custom
    await _assert_no_history_append(context)


async def test_retry_max_turns_boundary_injects_reminder_without_flag() -> None:
    context, state = _make_context()
    state.custom[TurnCustomKey.GRAPH_KNOWLEDGE_REQUIRE_READ] = True
    state.custom[TurnCustomKey.GRAPH_KNOWLEDGE_READ_COUNT] = 0
    state.custom[TurnCustomKey.GRAPH_KNOWLEDGE_HAS_READABLE] = True
    state.turn_attempt = 3

    await KnowledgeHook().after_turn(
        context,
        AgentResult(content="done", stop_reason=StopReason.COMPLETED),
    )

    assert TurnCustomKey.CONTINUATION_REQUEST not in state.custom
    messages = await context.history.to_list()
    assert len(messages) == 1
    assert messages[0].role == MessageRole.SYSTEM_REMINDER


async def test_retry_missing_react_state_skips() -> None:
    context, state = _make_context()
    state.custom[TurnCustomKey.GRAPH_KNOWLEDGE_REQUIRE_READ] = True
    context.runtime = None

    await KnowledgeHook().after_turn(
        context,
        AgentResult(content="done", stop_reason=StopReason.COMPLETED),
    )

    assert TurnCustomKey.CONTINUATION_REQUEST not in state.custom


# ============================================================
# after_turn: require_read exemption when KB has no readable content
# ============================================================


async def test_retry_require_read_exempt_when_kb_empty() -> None:
    context, state = _make_context()
    state.custom[TurnCustomKey.GRAPH_KNOWLEDGE_REQUIRE_READ] = True
    state.custom[TurnCustomKey.GRAPH_KNOWLEDGE_READ_COUNT] = 0
    state.custom[TurnCustomKey.GRAPH_KNOWLEDGE_HAS_READABLE] = False

    await KnowledgeHook().after_turn(
        context,
        AgentResult(content="done", stop_reason=StopReason.COMPLETED),
    )

    assert TurnCustomKey.CONTINUATION_REQUEST not in state.custom
    await _assert_no_history_append(context)


async def test_retry_require_read_exempt_but_write_still_required() -> None:
    context, state = _make_context()
    state.custom[TurnCustomKey.GRAPH_KNOWLEDGE_REQUIRE_READ] = True
    state.custom[TurnCustomKey.GRAPH_KNOWLEDGE_REQUIRE_WRITE] = True
    state.custom[TurnCustomKey.GRAPH_KNOWLEDGE_READ_COUNT] = 0
    state.custom[TurnCustomKey.GRAPH_KNOWLEDGE_WRITE_COUNT] = 0
    state.custom[TurnCustomKey.GRAPH_KNOWLEDGE_HAS_READABLE] = False

    await KnowledgeHook().after_turn(
        context,
        AgentResult(content="done", stop_reason=StopReason.COMPLETED),
    )

    assert state.custom[TurnCustomKey.CONTINUATION_REQUEST] is True
    messages = await context.history.to_list()
    assert len(messages) == 1
    content = _str_content(messages)
    assert "write" in content
    assert "read" not in content


# ============================================================
# before_turn: has_readable computation
# ============================================================


async def test_before_turn_sets_has_readable_false_when_kb_empty(tmp_path: Path) -> None:
    context, state = _make_context(knowledge_dir=str(tmp_path))

    await KnowledgeHook().before_turn(context)

    assert state.custom[TurnCustomKey.GRAPH_KNOWLEDGE_HAS_READABLE] is False


async def test_before_turn_sets_has_readable_true_when_findings_exist(tmp_path: Path) -> None:
    (tmp_path / "findings.md").write_text("discovery", encoding="utf-8")
    context, state = _make_context(knowledge_dir=str(tmp_path))

    await KnowledgeHook().before_turn(context)

    assert state.custom[TurnCustomKey.GRAPH_KNOWLEDGE_HAS_READABLE] is True


async def test_before_turn_sets_has_readable_true_when_open_questions_exist(
    tmp_path: Path,
) -> None:
    (tmp_path / "open_questions.md").write_text("why?", encoding="utf-8")
    context, state = _make_context(knowledge_dir=str(tmp_path))

    await KnowledgeHook().before_turn(context)

    assert state.custom[TurnCustomKey.GRAPH_KNOWLEDGE_HAS_READABLE] is True


async def test_before_turn_sets_has_readable_false_when_files_empty(tmp_path: Path) -> None:
    (tmp_path / "findings.md").write_text("   \n\n", encoding="utf-8")
    (tmp_path / "open_questions.md").write_text("", encoding="utf-8")
    context, state = _make_context(knowledge_dir=str(tmp_path))

    await KnowledgeHook().before_turn(context)

    assert state.custom[TurnCustomKey.GRAPH_KNOWLEDGE_HAS_READABLE] is False


# ============================================================
# Non-graph session isolation (hook must be no-op)
# ============================================================


async def test_before_turn_noop_in_non_graph_session(tmp_path: Path) -> None:
    (tmp_path / "findings.md").write_text("data", encoding="utf-8")
    context, state = _make_context(knowledge_dir=str(tmp_path))
    context.graph_context = None
    state.custom[TurnCustomKey.GRAPH_KNOWLEDGE_READ_COUNT] = 5

    await KnowledgeHook().before_turn(context)

    assert state.custom[TurnCustomKey.GRAPH_KNOWLEDGE_READ_COUNT] == 5
    await _assert_no_history_append(context)


async def test_after_turn_noop_in_non_graph_session() -> None:
    context, state = _make_context()
    context.graph_context = None
    state.custom[TurnCustomKey.GRAPH_KNOWLEDGE_REQUIRE_READ] = True
    state.custom[TurnCustomKey.GRAPH_KNOWLEDGE_READ_COUNT] = 0

    await KnowledgeHook().after_turn(
        context,
        AgentResult(content="done", stop_reason=StopReason.COMPLETED),
    )

    assert TurnCustomKey.CONTINUATION_REQUEST not in state.custom
    await _assert_no_history_append(context)
