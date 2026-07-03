"""Tests for the per-invocation subagent prompt providers.

AppendParentPromptProvider and ForkContextProvider move the invocation-specific
parts of a subagent's system prompt (parent prompt, forked context) out of the
materialize-time baked string and into per-session pipeline providers — mirroring
OutputMdProvider. A reused instance therefore rebuilds these per invocation.

Written test-first: these fail until the providers exist.
"""
from __future__ import annotations

from pathlib import Path

import pytest

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


class _MockMemory:
    """Stand-in for the subagent memory_system (injected by load(), not in spec)."""


class RecordingBuilder:
    """Fake ContextForkBuilder that returns canned XML and records calls."""

    def __init__(self, xml: str = "<fork>CTX</fork>") -> None:
        self._xml = xml
        self.build_calls: list[dict] = []
        self.cleanup_calls: list[str] = []

    async def build(self, **kw) -> str:
        self.build_calls.append(kw)
        return self._xml

    def register_for_cleanup(self, *, session_id, **_kw) -> None:
        self.cleanup_calls.append(session_id)

    def cleanup(self, session_id: str) -> None:
        self.cleanup_calls.append(session_id)


def _spec(builder, *, parent_resolver) -> ForkContextSpec:
    return ForkContextSpec(
        builder=builder,
        agent_type="planner",
        fork_max_messages=10,
        fork_workspace=Path("/tmp/fork"),
        template_memory=None,
        parent_session_resolver=parent_resolver,
    )


def _parent_named(name: str):
    async def resolver(sid: str):
        # A plain str: the provider only does str(parent).split(".")[-1] and
        # forwards parent_session to the builder. (Instance-level __str__ on a
        # SimpleNamespace is ignored — dunders resolve on the type.)
        return f"abc.{name}"

    return resolver


@pytest.mark.asyncio
async def test_fork_provider_wraps_builder_xml():
    builder = RecordingBuilder("<fork>FORK_BODY</fork>")
    out = await ForkContextProvider(
        _spec(builder, parent_resolver=_parent_named("main")), "inv1.planner", _MockMemory()
    ).get_or_refresh()

    assert "## Fork Context" in out
    assert "FORK_BODY" in out
    call = builder.build_calls[-1]
    assert call["agent_type"] == "planner"
    assert call["invocation_id"] == "inv1"  # derived via session_id_prefix_of
    assert call["parent_name"] == "main"


@pytest.mark.asyncio
async def test_fork_provider_registers_cleanup_for_session():
    builder = RecordingBuilder()
    await ForkContextProvider(
        _spec(builder, parent_resolver=_parent_named("main")), "inv1.planner", _MockMemory()
    ).get_or_refresh()
    assert "inv1.planner" in builder.cleanup_calls


@pytest.mark.asyncio
async def test_fork_provider_empty_when_no_parent():
    builder = RecordingBuilder()

    async def none_resolver(sid: str):
        return None

    out = await ForkContextProvider(
        _spec(builder, parent_resolver=none_resolver), "inv1.planner", _MockMemory()
    ).get_or_refresh()
    assert out == ""
    assert builder.build_calls == []  # never built without a parent


@pytest.mark.asyncio
async def test_fork_provider_swallows_builder_exception():
    class BoomBuilder(RecordingBuilder):
        async def build(self, **kw):
            raise RuntimeError("fork failed")

    builder = BoomBuilder()
    out = await ForkContextProvider(
        _spec(builder, parent_resolver=_parent_named("main")), "inv1.planner", _MockMemory()
    ).get_or_refresh()
    assert out == ""


@pytest.mark.asyncio
async def test_fork_provider_differs_per_session():
    """Each session forks its own parent snapshot — the per-invocation contract."""

    class PerSessionBuilder(RecordingBuilder):
        async def build(self, **kw):
            return f"<fork>{kw['invocation_id']}</fork>"

    builder = PerSessionBuilder()
    spec = _spec(builder, parent_resolver=_parent_named("main"))
    a = await ForkContextProvider(spec, "inv1.planner", _MockMemory()).get_or_refresh()
    b = await ForkContextProvider(spec, "inv2.planner", _MockMemory()).get_or_refresh()
    assert "inv1" in a and "inv2" not in a
    assert "inv2" in b and "inv1" not in b
