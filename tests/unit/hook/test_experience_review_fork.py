"""TDD tests: ExperienceReviewAgent must use forked conversation history.

The reviewer should receive the parent session's full message list (as
ChatMessage objects) so its ReAct loop can inspect tool_calls, tool_results,
and structured content directly — NOT a flattened text snapshot.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from modex_agent.core.constants import StopReason
from modex_agent.core.emitter import AgentResult
from modex_agent.core.experience.meta import PerFileExperienceMetaStore
from modex_agent.core.message import ChatMessage
from modex_agent.hook.builtin.experience_review import ExperienceReviewHook
from modex_agent.memory.history import ListMessageHistory


def _make_messages() -> list[ChatMessage]:
    """Build a realistic conversation with tool_calls + tool_results."""
    return [
        ChatMessage(role="user", content="How do I configure the memory system?"),
        ChatMessage(
            role="assistant",
            content=None,
            tool_calls=[
                {
                    "id": "tc_1",
                    "type": "function",
                    "function": {"name": "read", "arguments": '{"path":"config.yml"}'},
                }
            ],
        ),
        ChatMessage(role="tool", tool_call_id="tc_1", content="memory: {enabled: true}"),
        ChatMessage(role="assistant", content="Set memory.enabled=true in config.yml."),
    ]


class TestExperienceReviewAgentForkHistory:
    """Verify ExperienceReviewAgent accepts and uses forked message history."""

    async def test_review_accepts_conversation_messages_parameter(self, tmp_path: Path) -> None:
        """review() must accept a ``conversation_messages`` kwarg (fork mode)."""
        from modex_agent.agents.experience.review_agent import ExperienceReviewAgent
        from modex_agent.core.provider import LLMProvider

        provider = MagicMock(spec=LLMProvider)
        agent = ExperienceReviewAgent(provider=provider, max_iterations=2)

        exp_dir = tmp_path / "experiences"
        exp_dir.mkdir(parents=True, exist_ok=True)
        meta = PerFileExperienceMetaStore(exp_dir)

        # Stub the ReAct run so we capture the context
        captured_contexts: list[Any] = []

        async def _mock_run(context, emitter):  # noqa: ANN001, ANN202
            captured_contexts.append(context)
            return AgentResult(content="done", stop_reason=StopReason.COMPLETED)

        agent._react_agent = MagicMock()
        agent._react_agent.run = _mock_run

        messages = _make_messages()
        await agent.review(
            conversation_snapshot="",  # empty snapshot, fork provides context
            conversation_messages=messages,
            experience_dir=exp_dir,
            meta_store=meta,
            invocation_id="fork-test-1",
        )

        assert len(captured_contexts) == 1, "ReAct agent should have run exactly once"
        history = captured_contexts[0].history
        history_list = await history.to_list()
        # The forked messages must appear BEFORE the review user_msg
        roles = [m["role"] for m in history_list]
        assert "user" in roles[0], f"First message should be the original user, got {roles[0]}"
        # Must contain the tool message from the forked history
        assert "tool" in roles, f"Forked tool result must be in history, got roles={roles}"

    async def test_review_uses_fork_messages_when_provided(self, tmp_path: Path) -> None:
        """When conversation_messages is provided, forked messages seed the history."""
        from modex_agent.agents.experience.review_agent import ExperienceReviewAgent
        from modex_agent.core.provider import LLMProvider

        provider = MagicMock(spec=LLMProvider)
        agent = ExperienceReviewAgent(provider=provider, max_iterations=2)

        exp_dir = tmp_path / "experiences"
        exp_dir.mkdir(parents=True, exist_ok=True)
        meta = PerFileExperienceMetaStore(exp_dir)

        captured_histories: list[list[dict[str, Any]]] = []

        async def _mock_run(context, emitter):  # noqa: ANN001, ANN202
            captured_histories.append(await context.history.to_list())
            return AgentResult(content="done", stop_reason=StopReason.COMPLETED)

        agent._react_agent = MagicMock()
        agent._react_agent.run = _mock_run

        messages = _make_messages()
        await agent.review(
            conversation_snapshot="",
            conversation_messages=messages,
            experience_dir=exp_dir,
            meta_store=meta,
            invocation_id="fork-test-2",
        )

        # History must contain ALL 4 forked messages + 1 review instruction message
        assert len(captured_histories) == 1
        history = captured_histories[0]
        assert len(history) >= 4, (
            f"Forked history must contain at least 4 messages, got {len(history)}: {history}"
        )
        # The first message must be the original user query, not the review instruction
        assert history[0]["role"] == "user"
        assert "configure the memory system" in str(history[0].get("content", ""))


class TestExperienceReviewHookPassesForkHistory:
    """Verify ExperienceReviewHook passes raw history (fork) to the reviewer."""

    async def test_hook_passes_history_to_reviewer_not_text_snapshot(self, tmp_path: Path) -> None:
        """Hook must call review() with conversation_messages, not just snapshot text."""
        from modex_agent.agents.experience.review_agent import ExperienceReviewAgent

        exp_dir = tmp_path / "experiences"
        exp_dir.mkdir(parents=True, exist_ok=True)
        meta = PerFileExperienceMetaStore(exp_dir)

        review_agent = MagicMock(spec=ExperienceReviewAgent)
        review_agent.review = AsyncMock(return_value=True)

        hook = ExperienceReviewHook(
            review_agent=review_agent,
            experience_dir=exp_dir,
            meta_store=meta,
            min_messages=2,
            exp_cooldown_turns=3,
        )
        hook._turn_counter = 10

        # Build a realistic history with structured tool_calls
        history_messages = [
            {"role": "user", "content": "debug this error"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "tc_a",
                        "type": "function",
                        "function": {"name": "bash", "arguments": "{}"},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "tc_a", "content": "Error: ModuleNotFoundError"},
            {"role": "assistant", "content": "Found the issue — missing import."},
        ]
        ctx = MagicMock()
        ctx.history = ListMessageHistory(history_messages)

        result = AgentResult(
            content="Found the issue", stop_reason=StopReason.COMPLETED, messages=[]
        )

        await hook.after_turn(ctx, result)
        await asyncio.sleep(0.15)  # let background task run

        review_agent.review.assert_called_once()
        call_kwargs = review_agent.review.call_args.kwargs
        assert "conversation_messages" in call_kwargs, (
            "review() must be called with conversation_messages kwarg (fork mode). "
            f"Got kwargs: {list(call_kwargs.keys())}"
        )
        forked = call_kwargs["conversation_messages"]
        assert len(forked) == 4, f"Expected 4 forked messages, got {len(forked)}"
        # Tool messages must be preserved (not flattened to text)
        roles = [
            getattr(m, "role", None) or (m.get("role") if isinstance(m, dict) else None)
            for m in forked
        ]
        assert "tool" in roles, f"Tool results must survive in forked history, got roles={roles}"
