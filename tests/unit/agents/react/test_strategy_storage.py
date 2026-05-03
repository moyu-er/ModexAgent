"""TDD: SuspendStrategy must expose public storage operations for Pipeline."""
import pytest
from framework.agents.react.strategy import (
    SuspendStrategy, SuspendResumeStrategy, InlineWaitStrategy,
)
from framework.agents.react.state import InMemoryTurnResumeStateStore
from framework.approval.store import LocalFileApprovalStateStore
from framework.approval.state import ApprovalRequest
from framework.control.channel import InMemoryControlChannel
from pathlib import Path
import tempfile


class TestSuspendStrategyStorageAPI:
    """SuspendStrategy must expose public CRUD for approval/resume state."""

    def test_inline_wait_strategy_has_no_op_storage(self):
        """InlineWaitStrategy has no persistence — storage operations are no-ops."""
        strategy = InlineWaitStrategy(InMemoryControlChannel())
        # All storage ops should be safe no-ops (no persistence)
        assert hasattr(strategy, "load_approval_state")
        assert hasattr(strategy, "save_approval_state")
        assert hasattr(strategy, "delete_approval_state")
        assert hasattr(strategy, "load_resume_state")
        assert hasattr(strategy, "delete_resume_state")

        import asyncio
        async def _test():
            assert await strategy.load_approval_state("s1") is None
            await strategy.delete_approval_state("s1")  # no-op, no error
        asyncio.run(_test())

    def test_suspend_resume_strategy_loads_saved_state(self, tmp_path):
        """Saved approval state must be loadable via public API."""
        workspace = tmp_path / "approval"
        approval_store = LocalFileApprovalStateStore(workspace)
        resume_store = InMemoryTurnResumeStateStore()
        strategy = SuspendResumeStrategy(approval_store, resume_store)

        import asyncio
        from framework.approval.state import ApprovalState

        async def _test():
            state = ApprovalState(
                session_id="s1",
                requests=[ApprovalRequest(
                    tool_name="shell", tool_call_id="1",
                    arguments={}, tier="dangerous", iteration=1,
                )],
            )
            await strategy.save_approval_state(state)
            loaded = await strategy.load_approval_state("s1")
            assert loaded is not None
            assert loaded.session_id == "s1"
            assert len(loaded.requests) == 1
            assert loaded.requests[0].tool_name == "shell"

            await strategy.delete_approval_state("s1")
            assert await strategy.load_approval_state("s1") is None
        asyncio.run(_test())
