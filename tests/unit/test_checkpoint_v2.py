"""Tests for Phase 2 CheckpointStore — dict format, AgentCheckpoint, ApprovalDenialContext."""

import pytest
from pathlib import Path

from framework.control.checkpoint import (
    AgentCheckpoint,
    ApprovalDenialContext,
    CheckpointStore,
    JsonFileCheckpointStore,
    JsonFileRuntimeStateStore,
    NoOpCheckpointStore,
    NoOpRuntimeStateStore,
    RuntimeStateStore,
)
from framework.control.exceptions import TerminationReason


class TestJsonFileCheckpointStore:
    async def test_save_and_load_dict(self, tmp_path: Path):
        store = JsonFileCheckpointStore(tmp_path)
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
        store = JsonFileCheckpointStore(tmp_path)
        assert await store.load("nonexistent") is None

    async def test_clear_removes(self, tmp_path: Path):
        store = JsonFileCheckpointStore(tmp_path)
        await store.save("test_cp", {"messages": [], "iteration": 0})
        await store.clear("test_cp")
        assert await store.load("test_cp") is None


class TestNoOpCheckpointStore:
    async def test_save_load_clear(self):
        store = NoOpCheckpointStore()
        await store.save("x", {"messages": [], "iteration": 0})
        assert await store.load("x") is None
        await store.clear("x")


class TestRuntimeStateStoreAliases:
    def test_aliases_keep_checkpoint_compatibility(self):
        assert RuntimeStateStore is CheckpointStore
        assert JsonFileRuntimeStateStore is JsonFileCheckpointStore
        assert NoOpRuntimeStateStore is NoOpCheckpointStore


class TestAgentCheckpoint:
    def test_defaults(self):
        cp = AgentCheckpoint(
            checkpoint_id="cp1",
            session_id="s1",
        )
        assert cp.checkpoint_id == "cp1"
        assert cp.session_id == "s1"
        assert cp.version == 1
        assert cp.messages == []
        assert cp.termination is None
        assert cp.denial_context is None


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
