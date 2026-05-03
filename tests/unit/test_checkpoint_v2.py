"""Tests for RuntimeStateStore dict format and ApprovalDenialContext."""
import pytest
from pathlib import Path

from framework.control.checkpoint import (
    ApprovalDenialContext,
    JsonFileRuntimeStateStore,
    NoOpRuntimeStateStore,
    RuntimeStateStore,
)


class TestJsonFileRuntimeStateStore:
    async def test_save_and_load_dict(self, tmp_path: Path):
        store = JsonFileRuntimeStateStore(tmp_path)
        data = {
            "messages": [{"role": "assistant", "content": "hello"}],
            "termination": None,
            "denial_context": None,
            "cancelled_tool_ids": [],
            "iteration": 1,
        }
        await store.save("test_cp", data)
        loaded = await store.load("test_cp")
        assert loaded == data

    async def test_load_returns_none_for_missing(self, tmp_path: Path):
        store = JsonFileRuntimeStateStore(tmp_path)
        assert await store.load("nonexistent") is None

    async def test_clear_removes(self, tmp_path: Path):
        store = JsonFileRuntimeStateStore(tmp_path)
        await store.save("test_cp", {"messages": [], "iteration": 0})
        await store.clear("test_cp")
        assert await store.load("test_cp") is None


class TestNoOpRuntimeStateStore:
    async def test_save_load_clear(self):
        store = NoOpRuntimeStateStore()
        await store.save("x", {"messages": [], "iteration": 0})
        assert await store.load("x") is None
        await store.clear("x")


class TestRuntimeStateStoreProtocol:
    def test_protocol_is_runtime_state_store(self):
        """Verify RuntimeStateStore is a proper Protocol."""
        # JsonFileRuntimeStateStore implements the protocol
        assert hasattr(JsonFileRuntimeStateStore, "save")
        assert hasattr(JsonFileRuntimeStateStore, "load")
        assert hasattr(JsonFileRuntimeStateStore, "clear")
        # NoOpRuntimeStateStore implements the protocol
        assert hasattr(NoOpRuntimeStateStore, "save")
        assert hasattr(NoOpRuntimeStateStore, "load")
        assert hasattr(NoOpRuntimeStateStore, "clear")


class TestApprovalDenialContext:
    def test_fields(self):
        ctx = ApprovalDenialContext(
            tool_name="shell",
            tool_call_id="tc1",
            arguments={"cmd": "rm"},
            tier="dangerous",
            denied_at=100.0,
            reason="denied by user",
            session_id="s1",
        )
        assert ctx.tool_name == "shell"
        assert ctx.tier == "dangerous"
        assert ctx.reason == "denied by user"
