"""RuntimeAssembler 单元测试 — 覆盖 clean/full/partial 三种装配路径。"""

from __future__ import annotations

import pytest

from framework.agents.react.assembler import RuntimeAssembler, RuntimeServicesConfig
from framework.agents.react.approval import ApprovalRuntime
from framework.control.channel import InMemoryControlChannel
from framework.control.runtime import ControlRuntime
from framework.control.store import InMemoryControlStore
from framework.control.types import ControlCommandType
from framework.interceptor.chain import InterceptorChain
from framework.runtime.services import AgentRuntime


class _FakeClassifier:
    async def classify(self, tool_call, ctx) -> str:
        return "normal"


class _FakeHandler:
    async def handle(self, cmd, ctx, scope) -> None:
        pass


class _FakeStore:
    async def save(self, key, state) -> None:
        pass

    async def load(self, key):
        return None

    async def delete(self, key) -> None:
        pass


class TestRuntimeAssembler:

    async def test_assemble_clean_mode(self):
        config = RuntimeServicesConfig(mode="clean")
        runtime = await RuntimeAssembler.assemble(config)
        assert isinstance(runtime, AgentRuntime)
        assert runtime.hooks is None
        assert runtime.interceptors is None
        assert runtime.approval is None
        assert runtime.control is None

    async def test_assemble_full_with_all_services(self):
        config = RuntimeServicesConfig(
            mode="full",
            interceptors=[],
            approval_classifier=_FakeClassifier(),
            control_channel=InMemoryControlChannel(),
            control_store=InMemoryControlStore(),
            command_handlers=[(ControlCommandType.CANCEL_RUN, _FakeHandler())],
            turn_store=_FakeStore(),
        )
        runtime = await RuntimeAssembler.assemble(config)
        assert isinstance(runtime, AgentRuntime)
        assert isinstance(runtime.interceptors, InterceptorChain)
        assert isinstance(runtime.approval, ApprovalRuntime)
        assert isinstance(runtime.control, ControlRuntime)
        assert runtime.turn_store is config.turn_store

    async def test_assemble_full_approval_only(self):
        config = RuntimeServicesConfig(
            mode="full",
            approval_classifier=_FakeClassifier(),
        )
        runtime = await RuntimeAssembler.assemble(config)
        assert isinstance(runtime, AgentRuntime)
        assert isinstance(runtime.approval, ApprovalRuntime)
        assert runtime.control is None

    async def test_assemble_full_control_only(self):
        config = RuntimeServicesConfig(
            mode="full",
            control_channel=InMemoryControlChannel(),
            control_store=InMemoryControlStore(),
            command_handlers=[(ControlCommandType.CANCEL_RUN, _FakeHandler())],
        )
        runtime = await RuntimeAssembler.assemble(config)
        assert isinstance(runtime, AgentRuntime)
        assert isinstance(runtime.control, ControlRuntime)
        assert runtime.approval is None

    async def test_assemble_full_neither(self):
        config = RuntimeServicesConfig(mode="full")
        runtime = await RuntimeAssembler.assemble(config)
        assert isinstance(runtime, AgentRuntime)
        assert runtime.approval is None
        assert runtime.control is None

    async def test_assemble_is_not_singleton(self):
        config = RuntimeServicesConfig(mode="full")
        r1 = await RuntimeAssembler.assemble(config)
        r2 = await RuntimeAssembler.assemble(config)
        assert r1 is not r2
