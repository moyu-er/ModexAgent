"""Tests for the per-invocation subagent prompt providers.

AppendParentPromptProvider and ForkContextProvider move the invocation-specific
parts of a subagent's system prompt (parent prompt, forked context) out of the
materialize-time baked string and into per-session pipeline providers — mirroring
OutputMdProvider. A reused instance therefore rebuilds these per invocation.

Written test-first: these fail until the providers exist.
"""
from __future__ import annotations

import pytest

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from modex_agent.core.history import ListMessageHistory
from modex_agent.core.message import ChatMessage
from modex_agent.core.scope import MemoryContext
from modex_agent.memory.archive_models import ArchiveChannel
from modex_agent.memory.core.models import CoreMemoryContents
from modex_agent.memory.core.system import MemorySystem
from modex_agent.memory.prompt_pipeline.providers import (
    AppendParentPromptProvider,
    ForkContextProvider,
    ForkContextSpec,
)

# ── AppendParentPromptProvider ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_append_parent_returns_resolved_prompt():
    async def resolver(sid: str) -> str | None:
        return "PARENT_PROMPT"

    out = await AppendParentPromptProvider(resolver, "inv1.scout").get_or_refresh()
    assert "PARENT_PROMPT" in out


@pytest.mark.asyncio
async def test_append_parent_differs_per_session():
    """Two sessions resolve two different parent prompts — the point of making
    this per-invocation rather than baked."""

    async def resolver(sid: str) -> str | None:
        return "PA" if sid == "inv1.scout" else "PB"

    a = await AppendParentPromptProvider(resolver, "inv1.scout").get_or_refresh()
    b = await AppendParentPromptProvider(resolver, "inv2.scout").get_or_refresh()
    assert "PA" in a and "PB" not in a
    assert "PB" in b and "PA" not in b


@pytest.mark.asyncio
async def test_append_parent_empty_when_resolver_returns_none():
    async def resolver(sid: str) -> str | None:
        return None

    out = await AppendParentPromptProvider(resolver, "inv1.scout").get_or_refresh()
    assert out == ""


@pytest.mark.asyncio
async def test_append_parent_swallows_resolver_exception():
    async def resolver(sid: str) -> str | None:
        raise RuntimeError("boom")

    out = await AppendParentPromptProvider(resolver, "inv1.scout").get_or_refresh()
    assert out == ""  # never propagate — pipeline would drop the whole prompt


@pytest.mark.asyncio
async def test_append_parent_caches_within_same_session():
    """Version = session_id → a second get_or_refresh on the same instance does
    not re-call the resolver."""
    calls = {"n": 0}

    async def resolver(sid: str) -> str | None:
        calls["n"] += 1
        return "PROMPT"

    provider = AppendParentPromptProvider(resolver, "inv1.scout")
    await provider.get_or_refresh()
    await provider.get_or_refresh()
    assert calls["n"] == 1


# ── ForkContextProvider ──────────────────────────────────────────────────


class _MockMemory(MemorySystem):
    """Stand-in for the subagent memory_system (injected by load(), not in spec).

    Stub all abstract methods — ForkContextProvider only forwards the instance
    to ``builder.build(subagent_memory_system=...)``; RecordingBuilder ignores it.
    """

    async def initialize(self) -> None: ...
    async def close(self) -> None: ...

    def create_message_history(
        self,
        context: MemoryContext,
        initial_messages: Sequence[ChatMessage | dict[str, Any]] | None = None,
    ) -> ListMessageHistory:
        return ListMessageHistory([])

    async def add_messages(
        self, context: MemoryContext, messages: Sequence[ChatMessage | dict[str, Any]]
    ) -> None: ...

    async def get_history(self, context: MemoryContext) -> list[ChatMessage]:
        return []

    async def get_full_history(
        self, context: MemoryContext, *, limit: int | None = None
    ) -> list[ChatMessage]:
        return []

    async def search(
        self, query: str, context: MemoryContext, limit: int = 5
    ) -> list[dict[str, Any]]:
        return []

    async def clear(self, context: MemoryContext) -> None: ...
    async def get_core_memory(self, context: MemoryContext) -> CoreMemoryContents:
        return CoreMemoryContents()

    async def retrieve_core_memory(
        self, context: MemoryContext, query: str = ""
    ) -> CoreMemoryContents:
        return CoreMemoryContents()

    async def get_history_entries(
        self,
        context: MemoryContext,
        limit: int = 5,
        query: str = "",
        *,
        channel: ArchiveChannel = ArchiveChannel.CONTEXT,
    ) -> list[dict[str, Any]]:
        return []

    def get_providers(self) -> list[Any]:
        return []

    async def prefetch_memories(
        self, query: str, context: MemoryContext
    ) -> str | None:
        return None

    async def get_core_memory_directory(
        self, context: MemoryContext
    ) -> Path | None:
        return None

    async def get_storage_path(self, context: MemoryContext) -> Path | None:
        return None


class RecordingBuilder:
    """Fake ContextForkBuilder that returns canned XML and records calls."""

    def __init__(self, xml: str = "<fork>CTX</fork>") -> None:
        self._xml = xml
        self.build_calls: list[dict] = []

    async def build(self, **kw) -> str:
        self.build_calls.append(kw)
        return self._xml


def _spec(builder) -> ForkContextSpec:
    return ForkContextSpec(
        builder=builder,
        agent_type="planner",
        fork_max_messages=10,
    )


# The parent arrives as an authoritative session-id string (threaded from the
# dispatch envelope via runtime_info), not via a resolver callback.
_PARENT_SID = "abc.main"


@pytest.mark.asyncio
async def test_fork_provider_wraps_builder_xml():
    builder = RecordingBuilder("<fork>FORK_BODY</fork>")
    out = await ForkContextProvider(
        _spec(builder), "inv1.planner", _MockMemory(), _PARENT_SID
    ).get_or_refresh()

    assert "## Fork Context" in out
    assert "FORK_BODY" in out
    call = builder.build_calls[-1]
    assert call["agent_type"] == "planner"
    assert call["invocation_id"] == "inv1"  # derived via session_id_prefix_of
    assert call["parent_name"] == "main"  # derived from the parent session id


@pytest.mark.asyncio
async def test_fork_provider_empty_when_builder_returns_empty():
    """Parent presence is now gated at load(); the provider always has a parent.
    An empty fork body still yields an empty section."""
    builder = RecordingBuilder("")

    out = await ForkContextProvider(
        _spec(builder), "inv1.planner", _MockMemory(), _PARENT_SID
    ).get_or_refresh()
    assert out == ""


@pytest.mark.asyncio
async def test_fork_provider_swallows_builder_exception():
    class BoomBuilder(RecordingBuilder):
        async def build(self, **kw):
            raise RuntimeError("fork failed")

    builder = BoomBuilder()
    out = await ForkContextProvider(
        _spec(builder), "inv1.planner", _MockMemory(), _PARENT_SID
    ).get_or_refresh()
    assert out == ""


@pytest.mark.asyncio
async def test_fork_provider_differs_per_session():
    """Each session forks its own parent snapshot — the per-invocation contract."""

    class PerSessionBuilder(RecordingBuilder):
        async def build(self, **kw):
            return f"<fork>{kw['invocation_id']}</fork>"

    builder = PerSessionBuilder()
    spec = _spec(builder)
    a = await ForkContextProvider(spec, "inv1.planner", _MockMemory(), _PARENT_SID).get_or_refresh()
    b = await ForkContextProvider(spec, "inv2.planner", _MockMemory(), _PARENT_SID).get_or_refresh()
    assert "inv1" in a and "inv2" not in a
    assert "inv2" in b and "inv1" not in b


# ── Provider convergence (T5) — deprecated providers ─────────────────────


@pytest.mark.asyncio
async def test_h1_output_md_provider_not_in_pipeline():
    from modex_agent.core.agent import AgentCommKind
    from modex_agent.memory.injection import RestrictedInjectionPolicy
    from modex_agent.memory.system import MemorySystemContextManager

    mgr = MemorySystemContextManager(
        memory_system=_MockMemory(),  # type: ignore[arg-type]
        output_base_dir=Path("/tmp/test_output"),
        injection_policy=RestrictedInjectionPolicy(),
        comm_kind=AgentCommKind.SUBAGENT,
    )
    state = await mgr.load(session_id="inv1.scout")
    assert state.system_prompt_pipeline is not None
    prompt = await state.system_prompt_pipeline.get_or_refresh()
    assert "OUTPUT.md" not in prompt
    assert "work is lost" not in prompt


def test_h2_output_md_provider_class_deprecated():
    from modex_agent.memory.prompt_pipeline.providers import OutputMdProvider

    assert "[DEPRECATED]" in (OutputMdProvider.__doc__ or "")


def test_h3_consult_content_no_output_reference():
    from modex_agent.core.agent import AgentCommKind
    from modex_agent.memory.prompt_pipeline.providers import (
        _SubagentConsultationSubProvider,
    )

    provider = _SubagentConsultationSubProvider(None, AgentCommKind.SUBAGENT)
    assert "OUTPUT" not in provider.content()


def test_h4_consult_content_no_deliverable_reference():
    from modex_agent.core.agent import AgentCommKind
    from modex_agent.memory.prompt_pipeline.providers import (
        _SubagentConsultationSubProvider,
    )

    provider = _SubagentConsultationSubProvider(None, AgentCommKind.SUBAGENT)
    assert "deliverable" not in provider.content().lower()


def test_h5_consult_content_guides_against_reporting_results():
    from modex_agent.core.agent import AgentCommKind
    from modex_agent.memory.prompt_pipeline.providers import (
        _SubagentConsultationSubProvider,
    )

    provider = _SubagentConsultationSubProvider(None, AgentCommKind.SUBAGENT)
    assert "Do not use it to report results" in provider.content()


def test_h6_consult_content_no_prefixes():
    from modex_agent.core.agent import AgentCommKind
    from modex_agent.memory.prompt_pipeline.providers import (
        _SubagentConsultationSubProvider,
    )

    provider = _SubagentConsultationSubProvider(None, AgentCommKind.SUBAGENT)
    content = provider.content()
    assert "QUESTION" not in content
    assert "NEED_DECISION" not in content


def test_h7_consult_content_has_ask_parent_question():
    from modex_agent.core.agent import AgentCommKind
    from modex_agent.memory.prompt_pipeline.providers import (
        _SubagentConsultationSubProvider,
    )

    provider = _SubagentConsultationSubProvider(None, AgentCommKind.SUBAGENT)
    assert "ask your parent a question" in provider.content()


def test_h8_dispatch_applies_returns_false():
    from modex_agent.core.agent import AgentCommKind
    from modex_agent.memory.prompt_pipeline.providers import (
        _SubagentDispatchSubProvider,
    )

    provider = _SubagentDispatchSubProvider(None, AgentCommKind.NORMAL)
    assert provider.applies() is False


def test_h9_dispatch_provider_class_deprecated():
    from modex_agent.memory.prompt_pipeline.providers import (
        _SubagentDispatchSubProvider,
    )

    assert "[DEPRECATED]" in (_SubagentDispatchSubProvider.__doc__ or "")


@pytest.mark.asyncio
async def test_h10_no_dispatch_in_subagent_prompt():
    from modex_agent.core.agent import AgentCommKind
    from modex_agent.memory.prompt_pipeline.providers import (
        AgentCommunicationSystemPromptProvider,
    )

    provider = AgentCommunicationSystemPromptProvider(None, AgentCommKind.SUBAGENT)
    result = await provider.get_or_refresh()
    assert "Dispatching Subagents" not in result
    assert "PROGRESS_UPDATE" not in result
