"""Tests for TurnResumeState and TurnResumeStateStore."""
import pytest

from framework.agents.react.strategy import (
    InMemoryTurnResumeStateStore,
    TurnResumeState,
)


class TestTurnResumeState:
    def test_create(self):
        trs = TurnResumeState(
            iteration=3, tool_calls=[], tool_decisions=["pending"],
            all_new_messages=[],
        )
        assert trs.iteration == 3
        assert trs.resume_node == "tool"
        assert trs.resume_reason == "resume_tools"


class TestInMemoryTurnResumeStateStore:
    @pytest.mark.asyncio
    async def test_save_load_delete(self):
        store = InMemoryTurnResumeStateStore()
        trs = TurnResumeState(iteration=2, tool_calls=[], tool_decisions=[],
                              all_new_messages=[])
        await store.save("sid", trs)
        loaded = await store.load("sid")
        assert loaded is not None
        assert loaded.iteration == 2
        await store.delete("sid")
        assert await store.load("sid") is None
