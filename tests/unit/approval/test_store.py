"""Tests for ApprovalStateStore implementations."""
import tempfile
from pathlib import Path

import pytest

from framework.approval.constants import ApprovalDecision
from framework.approval.state import ApprovalRequest, ApprovalState
from framework.approval.store import (
    InMemoryApprovalStateStore,
    LocalFileApprovalStateStore,
)


class TestInMemoryApprovalStateStore:
    @pytest.mark.asyncio
    async def test_save_and_load(self):
        store = InMemoryApprovalStateStore()
        reqs = [ApprovalRequest("t1", "c1", {}, "dangerous", 1)]
        state = ApprovalState(session_id="s1", requests=reqs)
        await store.save(state)
        loaded = await store.load("s1")
        assert loaded is not None
        assert loaded.session_id == "s1"

    @pytest.mark.asyncio
    async def test_load_nonexistent(self):
        store = InMemoryApprovalStateStore()
        assert await store.load("no_session") is None

    @pytest.mark.asyncio
    async def test_delete(self):
        store = InMemoryApprovalStateStore()
        reqs = [ApprovalRequest("t1", "c1", {}, "dangerous", 1)]
        await store.save(ApprovalState(session_id="s1", requests=reqs))
        await store.delete("s1")
        assert await store.load("s1") is None


class TestLocalFileApprovalStateStore:
    @pytest.mark.asyncio
    async def test_save_load_delete(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = LocalFileApprovalStateStore(Path(tmp))
            reqs = [ApprovalRequest("t1", "c1", {"p": "/x"}, "dangerous", 1)]
            state = ApprovalState(session_id="s2", requests=reqs)
            state.apply("c1", ApprovalDecision.ALLOWED)
            await store.save(state)
            loaded = await store.load("s2")
            assert loaded is not None
            assert loaded.every_tool_decided is True
            await store.delete("s2")
            assert await store.load("s2") is None
