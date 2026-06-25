"""RuntimeAssembler tests — clean/full/partial assembly paths."""

from __future__ import annotations

import pytest

from modex_agent.agents.react.assembler import RuntimeAssembler, RuntimeServicesConfig
from modex_agent.agents.react.approval import ApprovalRuntime
from modex_agent.interceptor.chain import InterceptorChain
from modex_agent.runtime.services import AgentRuntime


class _FakeClassifier:
    async def classify(self, tool_call, ctx) -> str:
        return "normal"


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

    async def test_assemble_full_with_all_services(self):
        config = RuntimeServicesConfig(
            mode="full",
            interceptors=[],
            approval_classifier=_FakeClassifier(),
            turn_store=_FakeStore(),
        )
        runtime = await RuntimeAssembler.assemble(config)
        assert isinstance(runtime, AgentRuntime)
        assert isinstance(runtime.interceptors, InterceptorChain)
        assert isinstance(runtime.approval, ApprovalRuntime)
        assert runtime.turn_store is config.turn_store

    async def test_assemble_full_approval_only(self):
        config = RuntimeServicesConfig(
            mode="full",
            approval_classifier=_FakeClassifier(),
        )
        runtime = await RuntimeAssembler.assemble(config)
        assert isinstance(runtime.approval, ApprovalRuntime)

    async def test_assemble_full_neither(self):
        config = RuntimeServicesConfig(mode="full")
        runtime = await RuntimeAssembler.assemble(config)
        assert runtime.approval is None

    async def test_assemble_is_not_singleton(self):
        config = RuntimeServicesConfig(mode="full")
        r1 = await RuntimeAssembler.assemble(config)
        r2 = await RuntimeAssembler.assemble(config)
        assert r1 is not r2
