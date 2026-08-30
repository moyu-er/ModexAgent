"""LoopDetectionHook — helper functions and the two-stage episode state machine."""

import pytest

from modex_agent.control.exceptions import LoopDetectedError
from modex_agent.core.message import ChatMessage
from modex_agent.core.types import MessageRole, ToolCall
from modex_agent.hook.builtin.loop_detection import (
    LoopDetectionHook,
    _identity_preview,
    _round_identity,
    _tool_calls_count,
    _tool_calls_fingerprint,
    _trailing_repeat_run,
)
from modex_agent.runtime.enums import TurnCustomKey


class TestToolFingerprint:
    def test_openai_dict_format(self):
        calls = [
            {
                "id": "c1",
                "type": "function",
                "function": {"name": "read", "arguments": '{"path": "/a"}'},
            },
            {
                "id": "c2",
                "type": "function",
                "function": {"name": "ls", "arguments": '{"path": "/b"}'},
            },
        ]
        fp = _tool_calls_fingerprint(calls)
        assert ("read", '{"path": "/a"}') in fp
        assert ("ls", '{"path": "/b"}') in fp

    def test_toolcall_dataclass_format(self):
        calls = [ToolCall(tool_name="read", arguments={"path": "/a"}, call_id="c1")]
        fp = _tool_calls_fingerprint(calls)
        assert ("read", '{"path": "/a"}') in fp

    def test_ignores_call_id_and_order(self):
        a = [
            ToolCall(tool_name="read", arguments={"path": "/a"}, call_id="c1"),
            ToolCall(tool_name="ls", arguments={"path": "/b"}, call_id="c2"),
        ]
        b = [
            ToolCall(tool_name="ls", arguments={"path": "/b"}, call_id="zzz"),
            ToolCall(tool_name="read", arguments={"path": "/a"}, call_id="yyy"),
        ]
        assert _tool_calls_fingerprint(a) == _tool_calls_fingerprint(b)

    def test_arguments_key_order_invariant(self):
        a = [ToolCall(tool_name="x", arguments={"a": 1, "b": 2})]
        b = [ToolCall(tool_name="x", arguments={"b": 2, "a": 1})]
        assert _tool_calls_fingerprint(a) == _tool_calls_fingerprint(b)

    def test_empty(self):
        assert _tool_calls_fingerprint([]) == frozenset()

    def test_count_keeps_duplicates(self):
        # The fingerprint is a set (dedupes), but the count must reflect the
        # real number of calls so duplicate batches aren't seen as identical.
        dup = [
            ToolCall(tool_name="read", arguments={"path": "/a"}, call_id="c1"),
            ToolCall(tool_name="read", arguments={"path": "/a"}, call_id="c2"),
        ]
        single = [ToolCall(tool_name="read", arguments={"path": "/a"}, call_id="c1")]
        assert _tool_calls_fingerprint(dup) == _tool_calls_fingerprint(single)
        assert _tool_calls_count(dup) == 2
        assert _tool_calls_count(single) == 1
        assert _tool_calls_count(None) == 0


def _tc(name: str = "read", path: str = "/a") -> ToolCall:
    return ToolCall(tool_name=name, arguments={"path": path}, call_id="c")


def _round(name: str = "read", path: str = "/a", n: int = 1) -> ChatMessage:
    """Assistant message carrying ``n`` identical tool calls."""
    return ChatMessage(
        role=MessageRole.ASSISTANT,
        content=None,
        tool_calls=[_tc(name, path) for _ in range(n)],
    )


def _tool_result() -> ChatMessage:
    return ChatMessage(role=MessageRole.TOOL, content="result", tool_call_id="1", name="read")


class TestTrailingRepeatRun:
    def test_counts_consecutive_rounds(self):
        msgs = [_round(), _tool_result(), _round()]
        trailing = _trailing_repeat_run(msgs)
        assert trailing is not None
        (_, count), rounds = trailing
        assert count == 1
        assert rounds == 2

    def test_stops_at_user(self):
        msgs = [_round(), _round(), ChatMessage(role=MessageRole.USER, content="hi"), _round()]
        trailing = _trailing_repeat_run(msgs)
        assert trailing is not None
        assert trailing[1] == 1

    def test_prior_turn_loop_does_not_leak_across_user(self):
        msgs = [_round() for _ in range(10)]
        msgs.append(ChatMessage(role=MessageRole.USER, content="try again"))
        msgs.append(_round())
        trailing = _trailing_repeat_run(msgs)
        assert trailing is not None
        assert trailing[1] == 1

    def test_toolless_assistant_transparent(self):
        # A tool-less assistant text between two identical tool rounds does
        # not break the run — pausing to comment and resuming is still a loop.
        msgs = [_round(), ChatMessage(role=MessageRole.ASSISTANT, content="hmm"), _round()]
        trailing = _trailing_repeat_run(msgs)
        assert trailing is not None
        assert trailing[1] == 2

    def test_system_reminder_transparent(self):
        # Framework-injected reminders (including this hook's own) must not
        # break the run — otherwise the reminder would reset its own counter.
        reminder = ChatMessage(
            role=MessageRole.SYSTEM_REMINDER, content="<system-reminder>x</system-reminder>"
        )
        msgs = [_round(), reminder, _round()]
        trailing = _trailing_repeat_run(msgs)
        assert trailing is not None
        assert trailing[1] == 2

    def test_agent_role_transparent(self):
        # Only a pure user message stops the scan; agent-role input does not.
        msgs = [_round(), ChatMessage(role=MessageRole.AGENT, content="note"), _round()]
        trailing = _trailing_repeat_run(msgs)
        assert trailing is not None
        assert trailing[1] == 2

    def test_compact_marker_transparent(self):
        msgs = [_round(), ChatMessage(role=MessageRole.COMPACT, content="summary"), _round()]
        trailing = _trailing_repeat_run(msgs)
        assert trailing is not None
        assert trailing[1] == 2

    def test_different_identity_breaks_run(self):
        msgs = [_round(), _round(), _round("read", "/b"), _round()]
        trailing = _trailing_repeat_run(msgs)
        assert trailing is not None
        assert trailing[1] == 1

    def test_batch_round(self):
        # A round repeating a multi-tool batch keeps counting as one identity.
        batch = ChatMessage(
            role=MessageRole.ASSISTANT,
            content=None,
            tool_calls=[_tc("read", "/a"), _tc("ls", "/b")],
        )
        msgs = [_tool_result(), batch, batch]
        trailing = _trailing_repeat_run(msgs)
        assert trailing is not None
        (_, count), rounds = trailing
        assert count == 2
        assert rounds == 2

    def test_duplicate_batch_differs_from_single_call(self):
        # [read/a, read/a] in one round vs [read/a] — same fingerprint set,
        # different count: different identities, run broken.
        msgs = [_round(n=2), _round(n=1)]
        trailing = _trailing_repeat_run(msgs)
        assert trailing is not None
        assert trailing[1] == 1

    def test_no_tool_rounds_returns_none(self):
        msgs = [ChatMessage(role=MessageRole.ASSISTANT, content="plain")]
        assert _trailing_repeat_run(msgs) is None

    def test_empty_history_returns_none(self):
        assert _trailing_repeat_run([]) is None

    def test_scan_cap_pins_count(self):
        # 100 identical rounds, no user boundary: capped scan counts 23,
        # unbounded scan counts 100.
        msgs = [_round() for _ in range(100)]
        capped = _trailing_repeat_run(msgs, scan_cap=23)
        assert capped is not None
        assert capped[1] == 23
        assert _trailing_repeat_run(msgs)[1] == 100

    def test_scan_cap_budget_consumed_by_rounds_only(self):
        # Transparent messages (tool result, reminder, tool-less text)
        # interleave freely without consuming the scan budget.
        msgs = [
            _round(),
            _tool_result(),
            ChatMessage(
                role=MessageRole.SYSTEM_REMINDER, content="<system-reminder>x</system-reminder>"
            ),
            ChatMessage(role=MessageRole.ASSISTANT, content="hmm"),
            _round(),
            _round(),
        ]
        assert _trailing_repeat_run(msgs, scan_cap=2)[1] == 2
        assert _trailing_repeat_run(msgs, scan_cap=3)[1] == 3
        assert _trailing_repeat_run(msgs)[1] == 3

    def test_scan_cap_not_reached_counts_exactly(self):
        msgs = [_round() for _ in range(5)]
        assert _trailing_repeat_run(msgs, scan_cap=23)[1] == 5


class TestRoundIdentity:
    def test_toolless_assistant_is_none(self):
        msg = ChatMessage(role=MessageRole.ASSISTANT, content="hi")
        assert _round_identity(msg) is None

    def test_round_carries_fingerprint_and_count(self):
        identity = _round_identity(_round())
        assert identity is not None
        fp, count = identity
        assert ("read", '{"path": "/a"}') in fp
        assert count == 1


class TestIdentityPreview:
    def test_single_call(self):
        identity = _round_identity(_round())
        assert identity is not None
        assert _identity_preview(identity) == 'read({"path": "/a"})'

    def test_duplicate_batch_suffix(self):
        identity = _round_identity(_round(n=2))
        assert identity is not None
        assert _identity_preview(identity) == 'read({"path": "/a"}) ×2'

    def test_multi_tool_batch_sorted(self):
        msg = ChatMessage(
            role=MessageRole.ASSISTANT,
            content=None,
            tool_calls=[_tc("ls", "/b"), _tc("read", "/a")],
        )
        identity = _round_identity(msg)
        assert identity is not None
        assert _identity_preview(identity) == ('ls({"path": "/b"}); read({"path": "/a"})')


def _make_ctx(messages=None):
    """Build an AgentContext with a real ReActTurnState (for state.custom)."""
    from modex_agent.agents.react.state import ReActTurnState
    from modex_agent.core.agent import AgentContext
    from modex_agent.core.session_id import SessionInfo
    from modex_agent.core.tool_manager import InMemoryToolManager
    from modex_agent.memory.history import ListMessageHistory
    from modex_agent.runtime.enums import AgentKind, TurnPhase
    from modex_agent.runtime.models import TurnIdentity
    from modex_agent.runtime.services import AgentRuntime, AgentRuntimeServices

    state = ReActTurnState(
        identity=TurnIdentity(agent_id="t", session=SessionInfo.from_str("s"), turn_id="u"),
        agent_kind=AgentKind.REACT,
        phase=TurnPhase.CREATED,
    )
    return AgentContext(
        system_prompt="",
        history=ListMessageHistory(messages or []),
        tool_manager=InMemoryToolManager(),
        session=SessionInfo.from_str("s.x"),
        max_iterations=20,
        identity=state.identity,
        runtime=AgentRuntime(services=AgentRuntimeServices(), state=state),
    )


async def _reminder_count(history) -> int:
    """Number of system_reminder messages currently in history."""
    messages = await history.to_list()
    return sum(1 for m in messages if m.role == MessageRole.SYSTEM_REMINDER)


class TestLoopDetectionHook:
    async def test_under_window_no_action(self):
        ctx = _make_ctx([_round() for _ in range(9)])
        hook = LoopDetectionHook(window_size=10, observation_rounds=2)
        await hook.before_iteration(ctx)
        assert await _reminder_count(ctx.history) == 0
        assert ctx.runtime.state.custom.get(TurnCustomKey.LOOP_EPISODE) is None

    async def test_window_injects_reminder_and_records_episode(self):
        ctx = _make_ctx([_round() for _ in range(10)])
        hook = LoopDetectionHook(window_size=10, observation_rounds=2)
        await hook.before_iteration(ctx)
        assert await _reminder_count(ctx.history) == 1
        reminder = ctx.history._messages[-1]
        assert reminder.role == MessageRole.SYSTEM_REMINDER
        assert "<system-reminder>" in reminder.content
        assert "read" in reminder.content
        assert "10" in reminder.content
        assert ctx.runtime.state.custom[TurnCustomKey.LOOP_EPISODE] == {
            "fp": 'read({"path": "/a"})',
            "rounds": 10,
            "checks": 0,
        }

    async def test_observation_round_silent(self):
        ctx = _make_ctx([_round() for _ in range(10)])
        hook = LoopDetectionHook(window_size=10, observation_rounds=2)
        await hook.before_iteration(ctx)  # inject
        await ctx.history.append(_round())  # round 11 — agent ignored reminder
        await hook.before_iteration(ctx)  # observe, no re-remind, no raise
        assert await _reminder_count(ctx.history) == 1
        episode = ctx.runtime.state.custom[TurnCustomKey.LOOP_EPISODE]
        assert episode["rounds"] == 10
        assert episode["checks"] == 1

    async def test_hard_exit_after_observation(self):
        ctx = _make_ctx([_round() for _ in range(10)])
        hook = LoopDetectionHook(window_size=10, observation_rounds=2)
        await hook.before_iteration(ctx)  # inject at 10
        # Real-loop shape: one check per iteration, one round per check.
        await ctx.history.append(_round())  # round 11 — agent ignored reminder
        await hook.before_iteration(ctx)  # checks=1 — observe
        await ctx.history.append(_round())  # round 12 — still repeating
        with pytest.raises(LoopDetectedError) as ei:
            await hook.before_iteration(ctx)  # checks=2 — exit
        content = ei.value.user_content
        assert ei.value.loop_type == "tool"
        assert "<loop_detected" not in content
        assert "Loop detected" in content
        assert 'read({"path": "/a"})' in content
        assert "12 consecutive" in content
        assert "after round 10" in content
        assert "2 more rounds" in content

    async def test_user_boundary_no_detection(self):
        msgs = [_round() for _ in range(10)]
        msgs.append(ChatMessage(role=MessageRole.USER, content="again"))
        msgs.extend([_round() for _ in range(3)])
        ctx = _make_ctx(msgs)
        hook = LoopDetectionHook(window_size=10, observation_rounds=2)
        await hook.before_iteration(ctx)
        assert await _reminder_count(ctx.history) == 0

    async def test_reminder_is_transparent_to_next_scan(self):
        # The injected reminder must not break the trailing run it belongs
        # to — the observation round after injection still counts.
        ctx = _make_ctx([_round() for _ in range(10)])
        hook = LoopDetectionHook(window_size=10, observation_rounds=1)
        await hook.before_iteration(ctx)  # inject at 10
        await ctx.history.append(_round())  # round 11
        with pytest.raises(LoopDetectedError):
            await hook.before_iteration(ctx)  # 11 >= 10 + 1

    async def test_loop_break_forgives_episode(self):
        ctx = _make_ctx([_round() for _ in range(10)])
        hook = LoopDetectionHook(window_size=10, observation_rounds=2)
        await hook.before_iteration(ctx)  # inject for read/a
        # Agent changes call — episode must be cleared, no exit.
        await ctx.history.append(_round("read", "/other"))
        await hook.before_iteration(ctx)
        assert ctx.runtime.state.custom.get(TurnCustomKey.LOOP_EPISODE) is None
        # Same old loop resumes and rebuilds to the window — fresh reminder,
        # fresh observation window (no exit without a new reminder).
        for _ in range(10):
            await ctx.history.append(_round())
        await hook.before_iteration(ctx)  # trailing read/a run is 10 again
        assert await _reminder_count(ctx.history) == 2
        assert ctx.runtime.state.custom[TurnCustomKey.LOOP_EPISODE]["rounds"] == 10

    async def test_switched_loop_gets_own_episode(self):
        ctx = _make_ctx([_round() for _ in range(10)])
        hook = LoopDetectionHook(window_size=10, observation_rounds=2)
        await hook.before_iteration(ctx)  # inject for read/a
        # A different loop reaches the window while the old episode exists.
        for _ in range(10):
            await ctx.history.append(_round("read", "/b"))
        await hook.before_iteration(ctx)
        # New reminder for the new identity, episode replaced.
        assert ctx.runtime.state.custom[TurnCustomKey.LOOP_EPISODE]["fp"] == (
            'read({"path": "/b"})'
        )

    async def test_entry_above_window_cross_run(self):
        # History already carries 15 identical rounds when the turn starts
        # (cross-run accumulation): inject with the true run length, then
        # exit after the observation window measured from that length.
        ctx = _make_ctx([_round() for _ in range(15)])
        hook = LoopDetectionHook(window_size=10, observation_rounds=2)
        await hook.before_iteration(ctx)  # inject at 15
        assert ctx.runtime.state.custom[TurnCustomKey.LOOP_EPISODE]["rounds"] == 15
        await ctx.history.append(_round())  # round 16
        await hook.before_iteration(ctx)  # checks=1 — observe
        await ctx.history.append(_round())  # round 17
        with pytest.raises(LoopDetectedError) as ei:
            await hook.before_iteration(ctx)  # checks=2 — exit
        assert "after round 15" in ei.value.user_content
        assert "17 consecutive" in ei.value.user_content

    async def test_fresh_turn_reinjects(self):
        # Episode state is per-turn: a new turn (fresh state) over the same
        # loop history re-injects instead of jumping straight to the exit.
        history_messages = [_round() for _ in range(10)]
        ctx1 = _make_ctx(history_messages)
        hook = LoopDetectionHook(window_size=10, observation_rounds=2)
        await hook.before_iteration(ctx1)
        assert await _reminder_count(ctx1.history) == 1

        ctx2 = _make_ctx(history_messages)  # same loop, new turn state
        await hook.before_iteration(ctx2)
        assert await _reminder_count(ctx2.history) == 1
        assert ctx2.runtime.state.custom[TurnCustomKey.LOOP_EPISODE]["rounds"] == 10

    async def test_observation_rounds_zero_gives_one_grace_round(self):
        # observation_rounds=0: the injection check returns, and the very
        # next check exits — the round generated between them is the one
        # LLM decision that saw the reminder.
        ctx = _make_ctx([_round() for _ in range(2)])
        hook = LoopDetectionHook(window_size=2, observation_rounds=0)
        await hook.before_iteration(ctx)  # inject at 2
        await ctx.history.append(_round())  # round 3 — agent ignored reminder
        with pytest.raises(LoopDetectedError):
            await hook.before_iteration(ctx)  # 3 >= 2 + 0

    async def test_disabled_noop(self):
        ctx = _make_ctx([_round() for _ in range(10)])
        hook = LoopDetectionHook(window_size=10, enabled=False)
        await hook.before_iteration(ctx)
        assert await _reminder_count(ctx.history) == 0
        assert ctx.runtime.state.custom.get(TurnCustomKey.LOOP_EPISODE) is None

    async def test_scan_cap_pins_count_and_clamps_anchor(self):
        # 100 identical rounds, no user boundary (post-compaction shape):
        # count pins at the cap (23), reminder reports a lower bound, and
        # the stored anchor clamps to cap - observation (21).
        ctx = _make_ctx([_round() for _ in range(100)])
        hook = LoopDetectionHook(window_size=10, observation_rounds=2)
        await hook.before_iteration(ctx)
        episode = ctx.runtime.state.custom[TurnCustomKey.LOOP_EPISODE]
        assert episode["rounds"] == 21
        reminder = (await ctx.history.to_list())[-1]
        assert "at least 23" in reminder.content

    async def test_saturated_entry_exits_without_livelock(self):
        # THE livelock regression: an exit keyed on absolute run growth
        # would stall forever under a pinned count — checks-based exit
        # must still terminate the turn after the observation window.
        ctx = _make_ctx([_round() for _ in range(100)])
        hook = LoopDetectionHook(window_size=10, observation_rounds=2)
        await hook.before_iteration(ctx)  # inject (count pinned at 23)
        await ctx.history.append(_round())
        await hook.before_iteration(ctx)  # checks=1 — observe, no raise
        assert ctx.runtime.state.custom[TurnCustomKey.LOOP_EPISODE]["checks"] == 1
        await ctx.history.append(_round())
        with pytest.raises(LoopDetectedError) as ei:
            await hook.before_iteration(ctx)  # checks=2 — exit despite pin
        assert "at least 23" in ei.value.user_content
        assert "after round 21" in ei.value.user_content
        assert "2 more rounds" in ei.value.user_content

    async def test_user_steer_resets_episode(self):
        # A mid-turn user steer (InjectionDrainer) breaks the run: the old
        # episode must be forgiven, and a resumed loop re-earns a fresh
        # reminder cycle — never a silent exit against a pre-steer episode.
        ctx = _make_ctx([_round() for _ in range(10)])
        hook = LoopDetectionHook(window_size=10, observation_rounds=2)
        await hook.before_iteration(ctx)  # inject
        await ctx.history.append(ChatMessage(role=MessageRole.USER, content="steer"))
        for _ in range(3):
            await ctx.history.append(_round())
        await hook.before_iteration(ctx)  # below window — no raise
        assert ctx.runtime.state.custom.get(TurnCustomKey.LOOP_EPISODE) is None
        for _ in range(7):
            await ctx.history.append(_round())  # run rebuilds to 10
        await hook.before_iteration(ctx)  # fresh reminder, fresh window
        assert await _reminder_count(ctx.history) == 2
        assert ctx.runtime.state.custom[TurnCustomKey.LOOP_EPISODE]["rounds"] == 10

    async def test_trailing_none_clears_episode(self):
        ctx = _make_ctx([_round() for _ in range(10)])
        hook = LoopDetectionHook(window_size=10, observation_rounds=2)
        await hook.before_iteration(ctx)  # inject
        await ctx.history.append(ChatMessage(role=MessageRole.USER, content="stop?"))
        await hook.before_iteration(ctx)  # no trailing tool round at all
        assert ctx.runtime.state.custom.get(TurnCustomKey.LOOP_EPISODE) is None

    async def test_no_react_state_noop(self):
        # Clean mode / non-ReAct context: get_react_state is None — skip.
        from modex_agent.core.agent import AgentContext
        from modex_agent.core.session_id import SessionInfo
        from modex_agent.core.tool_manager import InMemoryToolManager
        from modex_agent.memory.history import ListMessageHistory

        ctx = AgentContext(
            system_prompt="",
            history=ListMessageHistory([_round() for _ in range(10)]),
            tool_manager=InMemoryToolManager(),
            session=SessionInfo.from_str("s.x"),
            max_iterations=20,
        )
        hook = LoopDetectionHook(window_size=10)
        await hook.before_iteration(ctx)
        assert await _reminder_count(ctx.history) == 0
