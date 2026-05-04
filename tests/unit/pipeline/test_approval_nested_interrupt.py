"""Bug repro: nested GraphInterrupt during handle() resume should send approval prompt.

Follows TDD: RED (write failing test) → GREEN (fix) → REFACTOR.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from framework.approval.constants import ApprovalDecision
from framework.approval.state import ApprovalRequest, ApprovalState
from framework.approval.types import ApprovalAction
from framework.core.graph.interrupt import GraphInterrupt
from framework.pipeline.approval_renderer import ApprovalRenderer, format_approval_prompt


class TestNestedGraphInterruptDuringResume:
    """Bug: when agent.run() inside handle() raises a nested GraphInterrupt,
    the approval prompt MUST be sent and handle() must return None (not crash)."""

    def test_nested_interrupt_prompt_is_sent(self) -> None:
        """Scenario:
        1. User previously triggered a tool that needs approval (state saved)
        2. User now sends /approve
        3. handle() starts agent.run() with resume state
        4. During resumed execution, LLM generates ANOTHER tool that needs approval
        5. ToolNode raises a NEW GraphInterrupt inside agent.run()
        6. handle() MUST catch it, send prompt, and return None
        """
        prompts_sent: list[str] = []

        # Mock user_interface that records prompts
        ui = MagicMock()
        ui.render_message = AsyncMock()

        def _record(session_id: str, text: str) -> None:
            prompts_sent.append(text)

        ui.render_message.side_effect = _record

        # Mock agent that raises GraphInterrupt when run() is called
        nested_req = MagicMock()
        nested_req.tool_name = "write_file"
        nested_req.tool_call_id = "nested-call"
        nested_req.arguments = {"path": "/etc/hosts"}
        nested_req.tier = "hardline"

        agent = MagicMock()
        agent.run = AsyncMock(
            side_effect=GraphInterrupt([nested_req])
        )

        # Build renderer with mocks
        r = ApprovalRenderer(
            approval_workspace=Path("/tmp/test_ar"),
            agent=agent,
            user_interface=ui,
        )

        # Build approved-but-not-resumed state
        state = ApprovalState(
            session_id="s1",
            requests=[
                ApprovalRequest(
                    tool_name="shell", tool_call_id="c1",
                    arguments={"command": "echo hi"}, tier="dangerous", iteration=1,
                ),
            ],
        )
        state.apply("c1", ApprovalDecision.ALLOWED)
        assert state.every_tool_decided is True

        # Mock strategy that returns resume state
        strategy = MagicMock()
        strategy.load_approval_state = AsyncMock(return_value=state)
        strategy.load_resume_state = AsyncMock(return_value=MagicMock())
        strategy.delete_approval_state = AsyncMock()
        strategy.delete_resume_state = AsyncMock()
        strategy.save_approval_state = AsyncMock()

        # Mock context_manager for saving
        ctx_mgr = MagicMock()
        ctx_mgr.save = AsyncMock()

        # Mock context
        agent_ctx = MagicMock()
        agent_ctx.metadata = {}

        # Call handle with /approve
        result = asyncio.run(r.handle(
            action=ApprovalAction.ALLOW,
            approval_state=state,
            agent_context=agent_ctx,
            emitter=MagicMock(),
            session_id="s1",
            context_state=MagicMock(),
            input_metadata={},
            strategy=strategy,
            ctx_mgr=ctx_mgr,
        ))

        # handle() must return None when nested interrupt occurs
        assert result is None

        # The new prompt MUST have been sent
        assert len(prompts_sent) >= 1, (
            f"Expected at least 1 approval prompt from nested GraphInterrupt, "
            f"got {len(prompts_sent)}"
        )
        assert "write_file" in prompts_sent[0]

    def test_nested_interrupt_no_ui_does_not_crash(self) -> None:
        """Without user_interface, nested interrupt should still not crash."""
        agent = MagicMock()
        agent.run = AsyncMock(
            side_effect=GraphInterrupt([MagicMock()])
        )

        r = ApprovalRenderer(approval_workspace=Path("/tmp/test_ar"), agent=agent)

        state = ApprovalState(
            session_id="s1",
            requests=[
                ApprovalRequest(
                    tool_name="shell", tool_call_id="c1",
                    arguments={}, tier="dangerous", iteration=1,
                ),
            ],
        )
        state.apply("c1", ApprovalDecision.ALLOWED)

        strategy = MagicMock()
        strategy.load_approval_state = AsyncMock(return_value=state)
        strategy.load_resume_state = AsyncMock(return_value=MagicMock())
        strategy.delete_approval_state = AsyncMock()
        strategy.delete_resume_state = AsyncMock()
        strategy.save_approval_state = AsyncMock()

        ctx_mgr = MagicMock()
        ctx_mgr.save = AsyncMock()

        # No user_interface — should not crash
        result = asyncio.run(r.handle(
            action=ApprovalAction.ALLOW,
            approval_state=state,
            agent_context=MagicMock(),
            emitter=MagicMock(),
            session_id="s1",
            context_state=MagicMock(),
            input_metadata={},
            strategy=strategy,
            ctx_mgr=ctx_mgr,
        ))
        assert result is None  # nested interrupt → return None, no crash

    def test_nested_interrupt_does_not_corrupt_state(self) -> None:
        """Nested interrupt must NOT affect the OUTER _current_resume context."""
        from framework.core.graph.interrupt import _current_resume

        agent = MagicMock()
        nested_req = MagicMock()
        agent.run = AsyncMock(
            side_effect=GraphInterrupt([nested_req])
        )

        ui_mock = MagicMock()
        ui_mock.render_message = AsyncMock()
        r = ApprovalRenderer(
            approval_workspace=Path("/tmp/test_ar"),
            agent=agent,
            user_interface=ui_mock,
        )

        state = ApprovalState(
            session_id="s1",
            requests=[
                ApprovalRequest(
                    tool_name="shell", tool_call_id="c1",
                    arguments={}, tier="dangerous", iteration=1,
                ),
            ],
        )
        state.apply("c1", ApprovalDecision.ALLOWED)

        strategy = MagicMock()
        strategy.load_approval_state = AsyncMock(return_value=state)
        strategy.load_resume_state = AsyncMock(return_value=MagicMock())
        strategy.delete_approval_state = AsyncMock()
        strategy.delete_resume_state = AsyncMock()
        strategy.save_approval_state = AsyncMock()

        asyncio.run(r.handle(
            action=ApprovalAction.ALLOW,
            approval_state=state,
            agent_context=MagicMock(),
            emitter=MagicMock(),
            session_id="s1",
            context_state=MagicMock(),
            input_metadata={},
            strategy=strategy,
            ctx_mgr=MagicMock(),
        ))

        # _current_resume must be None after handle returns
        # (not leaking the nested resume context)
        assert _current_resume.get(None) is None
