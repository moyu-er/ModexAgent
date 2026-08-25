"""Tests for individual SystemPromptProvider implementations."""

from __future__ import annotations

import hashlib
import os
import re
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from modex_agent.memory.hooks import MemoryHookRunner
from modex_agent.memory.prompt_pipeline.providers import (
    BasePromptProvider,
    CoreMemoryProvider,
    ExperienceProvider,
    RuntimeProvider,
    SkillProvider,
)

# -- BasePromptProvider --


@pytest.mark.asyncio
async def test_base_prompt_returns_content():
    provider = BasePromptProvider("You are a helpful assistant.")
    result = await provider.get_or_refresh()
    assert result == "You are a helpful assistant."


@pytest.mark.asyncio
async def test_base_prompt_never_refreshes():
    provider = BasePromptProvider("original")
    await provider.get_or_refresh()
    assert provider.last_version == "static"
    result = await provider.get_or_refresh()
    assert result == "original"


@pytest.mark.asyncio
async def test_base_prompt_empty_string():
    provider = BasePromptProvider("")
    result = await provider.get_or_refresh()
    assert result == ""


# -- RuntimeProvider --


@pytest.mark.asyncio
async def test_runtime_contains_date_and_platform():
    provider = RuntimeProvider()
    result = await provider.get_or_refresh()
    assert "Platform:" in result
    assert "Working Directory:" not in result


@pytest.mark.asyncio
async def test_runtime_version_changes_hourly():
    provider = RuntimeProvider()
    await provider.get_or_refresh()
    assert provider.last_version is not None
    assert provider.last_version.endswith(":no-dir")


@pytest.mark.asyncio
async def test_runtime_includes_upstream_working_directory():
    ws = Path("D:/projects/demo")
    provider = RuntimeProvider(working_directory=ws)

    result = await provider.get_or_refresh()

    assert f"Working Directory: {ws}" in result
    assert "workspace" not in result.lower()


@pytest.mark.asyncio
async def test_runtime_versions_are_isolated_by_working_directory():
    first = RuntimeProvider(working_directory=Path("D:/projects/one"))
    second = RuntimeProvider(working_directory=Path("D:/projects/two"))

    await first.get_or_refresh()
    await second.get_or_refresh()

    assert first.last_version != second.last_version


@pytest.mark.asyncio
async def test_runtime_without_working_directory_does_not_reuse_previous_value():
    with_directory = RuntimeProvider(working_directory=Path("D:/projects/one"))
    without_directory = RuntimeProvider()

    await with_directory.get_or_refresh()
    result = await without_directory.get_or_refresh()

    assert "Working Directory:" in (await with_directory.get_or_refresh())
    assert "Working Directory:" not in result


@pytest.mark.asyncio
async def test_runtime_declares_cpu_memory_and_resource_limits():
    """TB2.1: the agent must SEE the machine's real limits — 190 tesseract
    workers on a 1-CPU/2GB container OOM-killed the whole process twice."""
    provider = RuntimeProvider()
    result = await provider.get_or_refresh()
    assert f"CPU cores: {os.cpu_count() or 1}" in result
    mem_line = re.search(r"^Memory: (\d+) MiB$", result, flags=re.MULTILINE)
    assert mem_line is not None, "Memory line missing on a host with detectable RAM"
    assert "Memory is a hard limit:" in result
    assert "OOM" in result
    # CPU is advisory (IO-bound tasks may exceed cores), memory is not.
    assert "CPU cores are a guide" in result
    assert result.index("Platform:") < result.index("CPU cores:")


@pytest.mark.asyncio
async def test_runtime_omits_memory_lines_when_ram_undetectable(monkeypatch):
    """RAM undetectable (helper returns 0): Memory + limits lines drop, CPU
    line still emits (cpu_count never fails)."""
    import modex_agent.memory.prompt_pipeline.providers as providers_module

    monkeypatch.setattr(providers_module, "_physical_memory_mib", lambda: 0)
    provider = RuntimeProvider(working_directory=Path("D:/projects/demo"))
    result = await provider.get_or_refresh()
    assert f"CPU cores: {os.cpu_count() or 1}" in result
    assert "Memory:" not in result
    assert "Memory is a hard limit:" not in result
    assert "Working Directory:" in result


def test_physical_memory_mib_sysconf_math(monkeypatch, tmp_path):
    """Linux path: page_size × pages converted to MiB (2 GiB → 2048)."""
    from modex_agent.memory.prompt_pipeline.providers import _physical_memory_mib

    _isolate_cgroup_roots(monkeypatch, tmp_path)
    page_size, pages = 4096, 524_288
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(
        os,
        "sysconf",
        lambda name: {"SC_PAGE_SIZE": page_size, "SC_PHYS_PAGES": pages}[name],
        raising=False,
    )
    assert _physical_memory_mib() == 2048


def test_physical_memory_mib_returns_plain_int():
    """The contract is a plain int (MiB) — never float — on every path."""
    from modex_agent.memory.prompt_pipeline.providers import _physical_memory_mib

    value = _physical_memory_mib()
    assert isinstance(value, int)
    assert value >= 0


# -- cgroup-aware resource detection (containers must see their real limits) --


def _isolate_cgroup_roots(monkeypatch, tmp_path) -> tuple[Path, Path]:
    """Point both cgroup roots at empty tmp dirs; returns (v2_root, v1_root).

    Hermetic regardless of host: no real /sys/fs/cgroup is ever read, and the
    roots do not exist on disk until a test writes files into them.
    """
    import modex_agent.memory.prompt_pipeline.providers as providers_module

    v2_root = tmp_path / "cgroup-v2"
    v1_root = tmp_path / "cgroup-v1"
    monkeypatch.setattr(providers_module, "_CGROUP_V2_ROOT", v2_root)
    monkeypatch.setattr(providers_module, "_CGROUP_V1_ROOT", v1_root)
    return v2_root, v1_root


def test_cgroup_cpu_quota_v2_exact_cores(monkeypatch, tmp_path):
    """v2 cpu.max "200000 100000" = 2 cores enforced by the kernel."""
    from modex_agent.memory.prompt_pipeline.providers import _cgroup_cpu_quota

    v2_root, _ = _isolate_cgroup_roots(monkeypatch, tmp_path)
    v2_root.mkdir()
    (v2_root / "cpu.max").write_text("200000 100000\n")

    assert _cgroup_cpu_quota() == 2


def test_cgroup_cpu_quota_v2_max_falls_to_cpu_count(monkeypatch, tmp_path):
    """v2 cpu.max "max 100000" = unlimited → None → physical os.cpu_count()."""
    from modex_agent.memory.prompt_pipeline.providers import (
        _cgroup_cpu_quota,
        _effective_cpu_count,
    )

    v2_root, _ = _isolate_cgroup_roots(monkeypatch, tmp_path)
    v2_root.mkdir()
    (v2_root / "cpu.max").write_text("max 100000\n")
    monkeypatch.setattr(os, "cpu_count", lambda: 7)

    assert _cgroup_cpu_quota() is None
    assert _effective_cpu_count() == 7


def test_cgroup_cpu_quota_v2_fractional_ceils_up(monkeypatch, tmp_path):
    """v2 cpu.max "50000 100000" = 0.5 cores → ceil → 1 (never 0 workers)."""
    from modex_agent.memory.prompt_pipeline.providers import _cgroup_cpu_quota

    v2_root, _ = _isolate_cgroup_roots(monkeypatch, tmp_path)
    v2_root.mkdir()
    (v2_root / "cpu.max").write_text("50000 100000\n")

    assert _cgroup_cpu_quota() == 1


def test_cgroup_cpu_quota_v1_unlimited_quota_is_none(monkeypatch, tmp_path):
    """v1 cpu.cfs_quota_us = -1 means unlimited → None."""
    from modex_agent.memory.prompt_pipeline.providers import _cgroup_cpu_quota

    _, v1_root = _isolate_cgroup_roots(monkeypatch, tmp_path)
    cpu_dir = v1_root / "cpu"
    cpu_dir.mkdir(parents=True)
    (cpu_dir / "cpu.cfs_quota_us").write_text("-1\n")
    (cpu_dir / "cpu.cfs_period_us").write_text("100000\n")

    assert _cgroup_cpu_quota() is None


def test_cgroup_cpu_quota_v1_fractional_ceils_up(monkeypatch, tmp_path):
    """v1 quota 150000 / period 100000 = 1.5 cores → ceil → 2."""
    from modex_agent.memory.prompt_pipeline.providers import _cgroup_cpu_quota

    _, v1_root = _isolate_cgroup_roots(monkeypatch, tmp_path)
    cpu_dir = v1_root / "cpu"
    cpu_dir.mkdir(parents=True)
    (cpu_dir / "cpu.cfs_quota_us").write_text("150000\n")
    (cpu_dir / "cpu.cfs_period_us").write_text("100000\n")

    assert _cgroup_cpu_quota() == 2


def test_cgroup_memory_limit_v2_bytes_to_mib(monkeypatch, tmp_path):
    """v2 memory.max 2147483648 bytes = 2048 MiB."""
    from modex_agent.memory.prompt_pipeline.providers import _cgroup_memory_limit_mib

    v2_root, _ = _isolate_cgroup_roots(monkeypatch, tmp_path)
    v2_root.mkdir()
    (v2_root / "memory.max").write_text("2147483648\n")

    assert _cgroup_memory_limit_mib() == 2048


def test_cgroup_memory_limit_v2_max_is_unlimited(monkeypatch, tmp_path):
    """v2 memory.max "max" = no limit → None."""
    from modex_agent.memory.prompt_pipeline.providers import _cgroup_memory_limit_mib

    v2_root, _ = _isolate_cgroup_roots(monkeypatch, tmp_path)
    v2_root.mkdir()
    (v2_root / "memory.max").write_text("max\n")

    assert _cgroup_memory_limit_mib() is None


def test_cgroup_memory_limit_v1_unlimited_sentinel_is_none(monkeypatch, tmp_path):
    """v1 memory.limit_in_bytes at the PAGE_COUNTER_MAX sentinel → None."""
    from modex_agent.memory.prompt_pipeline.providers import _cgroup_memory_limit_mib

    _, v1_root = _isolate_cgroup_roots(monkeypatch, tmp_path)
    memory_dir = v1_root / "memory"
    memory_dir.mkdir(parents=True)
    (memory_dir / "memory.limit_in_bytes").write_text("9223372036854771712\n")

    assert _cgroup_memory_limit_mib() is None


def test_cgroup_memory_limit_v1_bytes_to_mib(monkeypatch, tmp_path):
    """v1 memory.limit_in_bytes 1073741824 bytes = 1024 MiB."""
    from modex_agent.memory.prompt_pipeline.providers import _cgroup_memory_limit_mib

    _, v1_root = _isolate_cgroup_roots(monkeypatch, tmp_path)
    memory_dir = v1_root / "memory"
    memory_dir.mkdir(parents=True)
    (memory_dir / "memory.limit_in_bytes").write_text("1073741824\n")

    assert _cgroup_memory_limit_mib() == 1024


@pytest.mark.asyncio
async def test_runtime_uses_physical_view_when_cgroup_files_absent(monkeypatch, tmp_path):
    """非容器正常场景: with no cgroup files at all, the Runtime section must
    keep reporting the physical view (os.cpu_count + sysconf) — byte-identical
    to the pre-cgroup behavior on bare metal."""
    _isolate_cgroup_roots(monkeypatch, tmp_path)
    monkeypatch.setattr(os, "cpu_count", lambda: 4)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(
        os,
        "sysconf",
        lambda name: {"SC_PAGE_SIZE": 4096, "SC_PHYS_PAGES": 524_288}[name],
        raising=False,
    )
    provider = RuntimeProvider()
    result = await provider.get_or_refresh()
    assert "CPU cores: 4" in result
    assert "Memory: 2048 MiB" in result
    assert "Memory is a hard limit:" in result


@pytest.mark.asyncio
async def test_runtime_section_reports_cgroup_limits_not_host_values(monkeypatch, tmp_path):
    """End-to-end: a --cpus 2 --memory 2048m container must render
    "CPU cores: 2" / "Memory: 2048 MiB" from cgroup files — not the host's
    cpu_count/RAM (the 190-tesseract-worker OOM root cause)."""
    v2_root, _ = _isolate_cgroup_roots(monkeypatch, tmp_path)
    v2_root.mkdir()
    (v2_root / "cpu.max").write_text("200000 100000\n")
    (v2_root / "memory.max").write_text("2147483648\n")
    # Host view would report 64 if the cgroup limit were ignored.
    monkeypatch.setattr(os, "cpu_count", lambda: 64)

    provider = RuntimeProvider()
    result = await provider.get_or_refresh()
    assert "CPU cores: 2" in result
    assert "CPU cores: 64" not in result
    assert "Memory: 2048 MiB" in result
    assert "Memory is a hard limit:" in result


# -- SkillProvider --


@pytest.mark.asyncio
async def test_skill_never_refreshes():
    provider = SkillProvider("skill content")
    await provider.get_or_refresh()
    assert provider.last_version == "static"
    result = await provider.get_or_refresh()
    assert result == "skill content"


@pytest.mark.asyncio
async def test_skill_empty_when_no_content():
    provider = SkillProvider("")
    result = await provider.get_or_refresh()
    assert result == ""


# -- CoreMemoryProvider --


@pytest.mark.asyncio
async def test_knowledge_never_refreshes_during_react():
    provider = CoreMemoryProvider("knowledge content")
    await provider.get_or_refresh()
    assert provider.last_version == "static"


@pytest.mark.asyncio
async def test_knowledge_empty_when_no_content():
    provider = CoreMemoryProvider("")
    result = await provider.get_or_refresh()
    assert result == ""


# -- ExperienceProvider --


@pytest.mark.asyncio
async def test_experience_default_static():
    provider = ExperienceProvider("experience content")
    await provider.get_or_refresh()
    assert provider.last_version == "static"


@pytest.mark.asyncio
async def test_experience_empty_when_no_content():
    provider = ExperienceProvider("")
    result = await provider.get_or_refresh()
    assert result == ""


# -- AgentCommunicationSystemPromptProvider --


def _make_tool_manager(targets: list, *, with_task_tool: bool = True):
    from modex_agent.core.tool_manager import InMemoryToolManager
    from modex_agent.multi_agent.address import AgentAddress
    from modex_agent.multi_agent.tools import (
        CommunicationTargetStore,
        SendToAgentTool,
        SendToPeerTool,
        TaskDispatchTool,
    )

    store = CommunicationTargetStore()
    for t in targets:
        store.add(t)
    tool = SendToAgentTool(
        store=store,
        source=AgentAddress(name="main"),
        service=MagicMock(),
    )
    mgr = InMemoryToolManager()
    mgr.register(tool)
    # Shared store: both tools see the same targets, matching production wiring.
    # `task` registered by default — production registers it when subagents exist.
    if with_task_tool:
        mgr.register(
            TaskDispatchTool(
                store=store,
                source=AgentAddress(name="main"),
                service=MagicMock(),
            )
        )
    mgr.register(
        SendToPeerTool(
            store=store,
            source=AgentAddress(name="main"),
            service=MagicMock(),
        )
    )
    return mgr


class _NoToolManager:
    def get_tool(self, name: str):
        return None

    def is_registered(self, name: str) -> bool:
        return False


@pytest.mark.asyncio
async def test_comm_provider_no_tool_manager_emits_nothing():
    from modex_agent.memory.prompt_pipeline.providers import (
        AgentCommunicationSystemPromptProvider,
    )

    provider = AgentCommunicationSystemPromptProvider(None, None)
    result = await provider.get_or_refresh()
    assert result == ""
    assert provider.last_version == "comm:none"


@pytest.mark.asyncio
async def test_comm_provider_no_send_to_agent_tool_emits_nothing():
    from modex_agent.memory.prompt_pipeline.providers import (
        AgentCommunicationSystemPromptProvider,
    )

    provider = AgentCommunicationSystemPromptProvider(_NoToolManager(), None)  # type: ignore[arg-type]
    result = await provider.get_or_refresh()
    assert result == ""
    assert provider.last_version == "comm:none"


@pytest.mark.asyncio
async def test_comm_provider_peer_target_emits_peer_contract():
    from modex_agent.core.agent import AgentCommKind
    from modex_agent.memory.prompt_pipeline.providers import (
        AgentCommunicationSystemPromptProvider,
    )
    from modex_agent.multi_agent.tools import CommunicationTarget

    targets = [
        CommunicationTarget(
            name="research-main",
            kind=AgentCommKind.NORMAL,
            tree_ref=MagicMock(),
        ),
    ]
    provider = AgentCommunicationSystemPromptProvider(
        _make_tool_manager(targets), AgentCommKind.NORMAL
    )
    result = await provider.get_or_refresh()
    assert "Remote Agents" in result
    assert "research-main" in result
    assert "`send_to_peer`" in result
    assert provider.last_version is not None
    assert provider.last_version.startswith("comm:peer:")


@pytest.mark.asyncio
async def test_comm_provider_subagent_only_targets_emit_only_delegation():
    """Subagent targets produce neither the peer nor the consultation
    contract — the peer contract fires only for remote targets with
    ``tree_ref``, and the consultation contract requires SUBAGENT
    comm_kind. With the ``task`` tool registered (the seam's default,
    matching production wiring), only the delegation section fires."""
    from modex_agent.core.agent import AgentCommKind
    from modex_agent.memory.prompt_pipeline.providers import (
        AgentCommunicationSystemPromptProvider,
    )
    from modex_agent.multi_agent.tools import CommunicationTarget

    targets = [
        CommunicationTarget(name="scout", kind=AgentCommKind.SUBAGENT),
    ]
    provider = AgentCommunicationSystemPromptProvider(
        _make_tool_manager(targets), AgentCommKind.NORMAL
    )
    result = await provider.get_or_refresh()
    assert "Remote Agents" not in result
    assert "Consulting Your Parent" not in result
    assert "## Delegating To Subagents" in result
    assert "`task`" in result
    assert provider.last_version == "comm:delegate"


@pytest.mark.asyncio
async def test_comm_provider_subagent_kind_emits_consultation_contract():
    from modex_agent.core.agent import AgentCommKind
    from modex_agent.memory.prompt_pipeline.providers import (
        AgentCommunicationSystemPromptProvider,
    )

    provider = AgentCommunicationSystemPromptProvider(None, AgentCommKind.SUBAGENT)
    result = await provider.get_or_refresh()
    assert "Consulting Your Parent" in result
    assert "send_to_agent" in result
    assert "OUTPUT" not in result
    assert "deliverable" not in result
    assert "QUESTION" not in result
    assert "NEED_DECISION" not in result
    assert "Do not use it to report results" in result
    assert provider.last_version == "comm:consult"


@pytest.mark.asyncio
async def test_comm_provider_mixed_targets_emit_only_peer_contract():
    """Only the peer contract fires when both peer and subagent targets
    exist — subagent targets contribute nothing to the comm output."""
    from modex_agent.core.agent import AgentCommKind
    from modex_agent.memory.prompt_pipeline.providers import (
        AgentCommunicationSystemPromptProvider,
    )
    from modex_agent.multi_agent.tools import CommunicationTarget

    targets = [
        CommunicationTarget(
            name="peer-a",
            kind=AgentCommKind.NORMAL,
            tree_ref=MagicMock(),
        ),
        CommunicationTarget(
            name="subagent-b",
            kind=AgentCommKind.SUBAGENT,
        ),
    ]
    provider = AgentCommunicationSystemPromptProvider(
        _make_tool_manager(targets), AgentCommKind.NORMAL
    )
    result = await provider.get_or_refresh()
    assert "Remote Agents" in result
    assert provider.last_version is not None
    assert "peer:" in provider.last_version


@pytest.mark.asyncio
async def test_comm_provider_version_combines_sub_modules():
    """Peer and delegation sub-modules both contribute version fragments —
    the seam registers the ``task`` tool alongside the send tools, matching
    production wiring."""
    from modex_agent.core.agent import AgentCommKind
    from modex_agent.memory.prompt_pipeline.providers import (
        AgentCommunicationSystemPromptProvider,
    )
    from modex_agent.multi_agent.tools import CommunicationTarget

    targets = [
        CommunicationTarget(
            name="alpha",
            kind=AgentCommKind.NORMAL,
            tree_ref=MagicMock(),
        ),
        CommunicationTarget(
            name="beta",
            kind=AgentCommKind.SUBAGENT,
        ),
    ]
    provider = AgentCommunicationSystemPromptProvider(
        _make_tool_manager(targets), AgentCommKind.NORMAL
    )
    await provider.get_or_refresh()
    assert provider.last_version == "comm:peer:alpha|delegate"


@pytest.mark.asyncio
async def test_comm_provider_without_task_tool_emits_no_delegation():
    """Delegation guidance is gated on ``task`` tool presence — a manager
    without it emits the peer contract but no delegation section or version
    fragment."""
    from modex_agent.core.agent import AgentCommKind
    from modex_agent.memory.prompt_pipeline.providers import (
        AgentCommunicationSystemPromptProvider,
    )
    from modex_agent.multi_agent.tools import CommunicationTarget

    targets = [
        CommunicationTarget(
            name="alpha",
            kind=AgentCommKind.NORMAL,
            tree_ref=MagicMock(),
        ),
    ]
    provider = AgentCommunicationSystemPromptProvider(
        _make_tool_manager(targets, with_task_tool=False), AgentCommKind.NORMAL
    )
    result = await provider.get_or_refresh()
    assert "Remote Agents" in result
    assert "Delegating To Subagents" not in result
    assert provider.last_version == "comm:peer:alpha"


def _mock_memory_system() -> MagicMock:
    """Minimal MemorySystem double for MemorySystemContextManager.load().

    Mirrors the fixture in tests/unit/memory/test_single_assemble.py: load()
    drives the injection policy + provider prefetch, so the double covers
    every async probe the real pipeline issues.
    """
    mock_system = MagicMock()
    mock_system.ensure_within_budget = AsyncMock()
    mock_system.retrieve_core_memory = AsyncMock(
        return_value=MagicMock(soul="", user="", memory=""),
    )
    mock_system.get_core_memory_directory = AsyncMock(return_value=None)
    mock_system.get_storage_path = AsyncMock(return_value=None)
    mock_system.get_history_entries = AsyncMock(return_value=[])
    mock_system.get_providers = MagicMock(return_value=[])
    mock_system.prefetch_memories = AsyncMock(return_value=None)
    mock_system.get_history = AsyncMock(return_value=[])
    mock_system.create_message_history = MagicMock(return_value=MagicMock())
    mock_system.hook_runner = MemoryHookRunner()
    mock_system.pruned_manager = None
    return mock_system


@pytest.mark.asyncio
async def test_delegation_guidance_welded_to_declaration_position(tmp_path):
    """Convergence weld: the compiler-derived ``task`` entry (declaration
    position — root with declared children) is what gates the delegation
    guidance, through the REAL MemorySystemContextManager prompt pipeline,
    not a hand-built provider.

    Positive: a root with a declared child compiles a ``task`` entry; a
    tool manager carrying that entry (what Stage 4 registers from the
    derived spec) yields a system prompt containing the delegation section.
    Negative: a childless root compiles no ``task`` entry; its prompt
    lacks the section. This is the regression net for the system.py
    wiring seam — dropping AgentCommunicationSystemPromptProvider from
    the pipeline would pass the provider unit tests above while silently
    stripping delegation guidance from every declaration-road pool.
    """
    from modex_agent.memory.system import MemorySystemContextManager
    from modex_agent.scope.compiler import compile_scope
    from modex_agent.scope.spec import AgentSpec, PoolSpec, ScopeKind, ScopeSpec
    from modex_agent.workspace.context import WorkspaceContext
    from modex_agent.workspace.paths import WorkspacePaths

    def _effective_tools(*, with_child: bool) -> list[str]:
        agents = [AgentSpec(name="root")]
        if with_child:
            agents.append(AgentSpec(name="child", parent="root"))
        compilation = compile_scope(
            ScopeSpec(kind=ScopeKind.POOL, pool=PoolSpec(name="weld", agents=agents)),
            workspace_ctx=WorkspaceContext(
                target=tmp_path,
                paths=WorkspacePaths(root=tmp_path),
                is_home=False,
            ),
            default_llm_provider="default",
        )
        root = next(a for a in compilation.agents if a.effective.agent == "root")
        return root.effective.tools

    async def _pool_prompt(effective_tools: list[str]) -> str:
        # Stage 4 equivalent: register the derived entries into the tool
        # manager the agent will carry.
        mgr = _make_tool_manager([], with_task_tool="task" in effective_tools)
        ctx_mgr = MemorySystemContextManager(
            memory_system=_mock_memory_system(),
            base_system_prompt="base",
        )
        return await ctx_mgr.build_system_prompt(tool_manager=mgr)

    with_child_tools = _effective_tools(with_child=True)
    childless_tools = _effective_tools(with_child=False)
    assert "task" in with_child_tools
    assert "task" not in childless_tools

    assert "## Delegating To Subagents" in await _pool_prompt(with_child_tools)
    assert "## Delegating To Subagents" not in await _pool_prompt(childless_tools)


@pytest.mark.asyncio
async def test_comm_provider_version_changes_when_target_added():
    from modex_agent.core.agent import AgentCommKind
    from modex_agent.memory.prompt_pipeline.providers import (
        AgentCommunicationSystemPromptProvider,
    )
    from modex_agent.multi_agent.tools import CommunicationTarget, SendToAgentTool

    targets = [
        CommunicationTarget(name="alpha", kind=AgentCommKind.NORMAL, tree_ref=MagicMock()),
    ]
    mgr = _make_tool_manager(targets)
    provider = AgentCommunicationSystemPromptProvider(mgr, AgentCommKind.NORMAL)
    await provider.get_or_refresh()
    v1 = provider.last_version
    tool = mgr.get_tool("send_to_agent")
    assert isinstance(tool, SendToAgentTool)
    tool.add_target(
        CommunicationTarget(name="beta", kind=AgentCommKind.NORMAL, tree_ref=MagicMock())
    )
    await provider.get_or_refresh()
    v2 = provider.last_version
    assert v1 != v2
    assert v2 is not None and "beta" in v2


@pytest.mark.asyncio
async def test_comm_provider_contract_does_not_expose_pool_concepts():
    from modex_agent.core.agent import AgentCommKind
    from modex_agent.memory.prompt_pipeline.providers import (
        AgentCommunicationSystemPromptProvider,
    )
    from modex_agent.multi_agent.tools import CommunicationTarget

    targets = [
        CommunicationTarget(name="x", kind=AgentCommKind.NORMAL, tree_ref=MagicMock()),
    ]
    provider = AgentCommunicationSystemPromptProvider(
        _make_tool_manager(targets), AgentCommKind.NORMAL
    )
    result = await provider.get_or_refresh()
    low = result.lower()
    assert "pool" not in low
    assert "main agent" not in low
    assert "peer pool" not in low


# -- AgentRoleContractProvider --


def _role_provider(roles: list[str]):
    from modex_agent.memory.prompt_pipeline.providers import (
        AgentRoleContractProvider,
    )

    return AgentRoleContractProvider(roles)


@pytest.mark.asyncio
async def test_role_contract_reviewer_injects_verification_tag():
    from modex_agent.core.constants import AgentRole

    provider = _role_provider([AgentRole.REVIEWER.value])
    result = await provider.get_or_refresh()
    assert '<verification status="passed|failed' in result
    assert "reason=" in result


@pytest.mark.asyncio
async def test_role_contract_implementer_requires_verification_after_changes():
    from modex_agent.core.constants import AgentRole

    provider = _role_provider([AgentRole.IMPLEMENTER.value])
    result = await provider.get_or_refresh()
    low = result.lower()
    assert "verification" in low
    # Must mention run-tests / lint / build / typecheck style verification
    assert any(tok in low for tok in ("test", "lint", "build", "typecheck"))


@pytest.mark.asyncio
async def test_role_contract_coordinator_describes_reviewer_format_and_dispatch():
    from modex_agent.core.constants import AgentRole

    provider = _role_provider([AgentRole.COORDINATOR.value])
    result = await provider.get_or_refresh()
    assert '<verification status="passed|failed' in result
    low = result.lower()
    assert "dispatch" in low or "implementer" in low
    assert "failed" in low


@pytest.mark.asyncio
async def test_role_contract_planner_injects_planning_contract():
    from modex_agent.core.constants import AgentRole

    provider = _role_provider([AgentRole.PLANNER.value])
    result = await provider.get_or_refresh()
    low = result.lower()
    assert "planning" in low or "plan" in low


@pytest.mark.asyncio
async def test_role_contract_scout_injects_exploration_contract():
    from modex_agent.core.constants import AgentRole

    provider = _role_provider([AgentRole.SCOUT.value])
    result = await provider.get_or_refresh()
    low = result.lower()
    assert "explor" in low or "scout" in low or "map" in low


@pytest.mark.asyncio
async def test_role_contract_oracle_injects_consulting_contract():
    from modex_agent.core.constants import AgentRole

    provider = _role_provider([AgentRole.ORACLE.value])
    result = await provider.get_or_refresh()
    low = result.lower()
    assert "consult" in low or "architect" in low or "design" in low or "oracle" in low


@pytest.mark.asyncio
async def test_role_contract_communicator_injects_communication_contract():
    from modex_agent.core.constants import AgentRole

    provider = _role_provider([AgentRole.COMMUNICATOR.value])
    result = await provider.get_or_refresh()
    low = result.lower()
    assert "commun" in low or "relay" in low


@pytest.mark.asyncio
async def test_role_contract_custom_role_injects_nothing_and_does_not_error():
    provider = _role_provider(["office-expert"])
    result = await provider.get_or_refresh()
    assert result == ""


@pytest.mark.asyncio
async def test_role_contract_multiple_roles_inject_all_matching_contracts():
    from modex_agent.core.constants import AgentRole

    provider = _role_provider([AgentRole.REVIEWER.value, AgentRole.PLANNER.value])
    result = await provider.get_or_refresh()
    assert '<verification status="passed|failed' in result
    low = result.lower()
    assert "planning" in low or "plan" in low
    assert result.count("## ") >= 2


@pytest.mark.asyncio
async def test_role_contract_empty_roles_injects_nothing():
    provider = _role_provider([])
    result = await provider.get_or_refresh()
    assert result == ""


@pytest.mark.asyncio
async def test_role_contract_byte_stable_across_get_or_refresh_calls():
    from modex_agent.core.constants import AgentRole

    provider = _role_provider(
        [AgentRole.REVIEWER.value, AgentRole.PLANNER.value, AgentRole.SCOUT.value]
    )
    first = await provider.get_or_refresh()
    second = await provider.get_or_refresh()
    third = await provider.get_or_refresh()
    assert first == second == third
    v1 = provider.last_version
    await provider.get_or_refresh()
    assert provider.last_version == v1


@pytest.mark.asyncio
async def test_role_contract_version_changes_with_role_set():
    from modex_agent.core.constants import AgentRole

    p_reviewer = _role_provider([AgentRole.REVIEWER.value])
    p_multi = _role_provider([AgentRole.REVIEWER.value, AgentRole.PLANNER.value])
    await p_reviewer.get_or_refresh()
    await p_multi.get_or_refresh()
    assert p_reviewer.last_version != p_multi.last_version


@pytest.mark.asyncio
async def test_role_contract_version_ignores_unrecognized_roles():
    from modex_agent.core.constants import AgentRole

    p_pure = _role_provider([AgentRole.REVIEWER.value])
    p_mixed = _role_provider([AgentRole.REVIEWER.value, "office-expert"])
    await p_pure.get_or_refresh()
    await p_mixed.get_or_refresh()
    # Same recognized set → same version (unrecognized roles don't affect version)
    assert p_pure.last_version == p_mixed.last_version


@pytest.mark.asyncio
async def test_role_contract_version_independent_of_input_order():
    from modex_agent.core.constants import AgentRole

    p_ab = _role_provider([AgentRole.REVIEWER.value, AgentRole.PLANNER.value])
    p_ba = _role_provider([AgentRole.PLANNER.value, AgentRole.REVIEWER.value])
    await p_ab.get_or_refresh()
    await p_ba.get_or_refresh()
    assert p_ab.last_version == p_ba.last_version


@pytest.mark.asyncio
async def test_role_contract_order_preserved_in_content():
    """Content order follows the input role list (so reviewer-before-planner
    yields reviewer contract before planner contract)."""
    from modex_agent.core.constants import AgentRole

    p_rp = _role_provider([AgentRole.REVIEWER.value, AgentRole.PLANNER.value])
    p_pr = _role_provider([AgentRole.PLANNER.value, AgentRole.REVIEWER.value])
    rp = await p_rp.get_or_refresh()
    pr = await p_pr.get_or_refresh()
    reviewer_marker = "Role Contract — Reviewer"
    planner_marker = "Role Contract — Planner"
    assert rp.index(reviewer_marker) < rp.index(planner_marker)
    assert pr.index(planner_marker) < pr.index(reviewer_marker)


# -- GraphWorkflowProvider --


@pytest.mark.asyncio
async def test_graph_workflow_provider_emits_deliver_routing_guidance() -> None:
    """Prompt text must match the deliver tool's target-required behavior:
    no 'auto-delivered to ALL downstream nodes' claim (multi-edge raises
    RoutingError); instead describe single-edge auto-deliver / END fallback
    and require an explicit target."""
    from modex_agent.agents.react.state import ReActTurnState
    from modex_agent.core.agent import AgentContext, current_agent_context
    from modex_agent.core.history import MessageHistory
    from modex_agent.core.session_id import SessionInfo
    from modex_agent.core.tool_manager import InMemoryToolManager
    from modex_agent.memory.prompt_pipeline.providers import GraphWorkflowProvider
    from modex_agent.runtime.enums import TurnCustomKey
    from modex_agent.runtime.models import TurnIdentity
    from modex_agent.runtime.services import AgentRuntime, AgentRuntimeServices
    from modex_graph.context import GraphContext

    state = ReActTurnState(identity=TurnIdentity(agent_id="test", session=SessionInfo.from_str("test.planner"), turn_id="t1"))
    state.custom[TurnCustomKey.GRAPH_TOPOLOGY_CONTEXT] = ""
    state.custom[TurnCustomKey.GRAPH_DOWNSTREAM_HAS_AGENT] = True
    ctx = AgentContext(
        system_prompt="",
        history=MagicMock(spec=MessageHistory),
        tool_manager=InMemoryToolManager(),
        session=SessionInfo.from_str("test.planner"),
        graph_context=MagicMock(spec=GraphContext),
        runtime=AgentRuntime(services=AgentRuntimeServices(), state=state),
    )
    token = current_agent_context.set(ctx)
    try:
        provider = GraphWorkflowProvider()
        result = await provider._fetch_content()
        assert result.startswith("## Graph Node Context\n")
        assert result.count("\n## ") == 0
        assert "### Workflow Guidance\n" in result
        assert "**Pattern 1 — Producer**" in result
        assert "**Pattern 2 — Relay**" in result
        assert "### Topology\n" not in result
        assert "### Your Role\n" not in result
        assert "MUST call `deliver`" in result
        assert "auto-delivered to ALL downstream nodes" not in result
        assert "Final Reply" not in result
        assert "ONE assistant" not in result
    finally:
        current_agent_context.reset(token)


@pytest.mark.asyncio
async def test_graph_workflow_provider_emits_topology_and_role_from_turn_state() -> None:
    from modex_agent.core.agent import AgentContext, current_agent_context
    from modex_agent.core.history import MessageHistory
    from modex_agent.core.session_id import SessionInfo
    from modex_agent.core.tool_manager import InMemoryToolManager
    from modex_agent.memory.prompt_pipeline.providers import GraphWorkflowProvider
    from modex_agent.runtime.enums import TurnCustomKey
    from modex_agent.runtime.models import TurnStateBase
    from modex_agent.runtime.services import AgentRuntime
    from modex_graph.context import GraphContext

    runtime = MagicMock(spec=AgentRuntime)
    runtime.state = MagicMock(spec=TurnStateBase)
    runtime.state.custom = {
        TurnCustomKey.GRAPH_TOPOLOGY_CONTEXT: "planner -> implementer",
        TurnCustomKey.GRAPH_NODE_DESCRIPTION: "Review the implementation.",
    }
    ctx = AgentContext(
        system_prompt="",
        history=MagicMock(spec=MessageHistory),
        tool_manager=InMemoryToolManager(),
        session=SessionInfo.from_str("test.reviewer"),
        runtime=runtime,
        graph_context=MagicMock(spec=GraphContext),
    )
    token = current_agent_context.set(ctx)
    try:
        provider = GraphWorkflowProvider()
        result = await provider._fetch_content()
        assert "### Topology\n\nplanner -> implementer\n" in result
        assert "### Your Role\n\nReview the implementation." in result
        assert result.index("### Workflow Guidance") < result.index("### Topology")
        assert result.index("### Topology") < result.index("### Your Role")
    finally:
        current_agent_context.reset(token)


@pytest.mark.asyncio
async def test_graph_workflow_provider_emits_knowledge_base_section_when_knowledge_dir_set() -> None:
    from modex_agent.core.agent import AgentContext, current_agent_context
    from modex_agent.core.history import MessageHistory
    from modex_agent.core.session_id import SessionInfo
    from modex_agent.core.tool_manager import InMemoryToolManager
    from modex_agent.memory.prompt_pipeline.providers import GraphWorkflowProvider
    from modex_agent.runtime.enums import TurnCustomKey
    from modex_agent.runtime.models import TurnStateBase
    from modex_agent.runtime.services import AgentRuntime
    from modex_graph.context import GraphContext

    runtime = MagicMock(spec=AgentRuntime)
    runtime.state = MagicMock(spec=TurnStateBase)
    runtime.state.custom = {
        TurnCustomKey.GRAPH_TOPOLOGY_CONTEXT: "",
        TurnCustomKey.GRAPH_KNOWLEDGE_DIR: "/tmp/knowledge",
    }
    ctx = AgentContext(
        system_prompt="",
        history=MagicMock(spec=MessageHistory),
        tool_manager=InMemoryToolManager(),
        session=SessionInfo.from_str("test.reviewer"),
        runtime=runtime,
        graph_context=MagicMock(spec=GraphContext),
    )
    token = current_agent_context.set(ctx)
    try:
        provider = GraphWorkflowProvider()
        result = await provider._fetch_content()
        assert "### Knowledge Base\n" in result
        assert "`knowledge_base`" in result
        assert "findings" in result
        assert "decisions" in result
        assert "open_questions" in result
        assert "changelog" in result
        assert result.index("### Workflow Guidance") < result.index("### Knowledge Base")
    finally:
        current_agent_context.reset(token)


@pytest.mark.asyncio
async def test_graph_workflow_provider_omits_knowledge_base_section_when_knowledge_dir_not_set() -> None:
    from modex_agent.core.agent import AgentContext, current_agent_context
    from modex_agent.core.history import MessageHistory
    from modex_agent.core.session_id import SessionInfo
    from modex_agent.core.tool_manager import InMemoryToolManager
    from modex_agent.memory.prompt_pipeline.providers import GraphWorkflowProvider
    from modex_agent.runtime.enums import TurnCustomKey
    from modex_agent.runtime.models import TurnStateBase
    from modex_agent.runtime.services import AgentRuntime
    from modex_graph.context import GraphContext

    runtime = MagicMock(spec=AgentRuntime)
    runtime.state = MagicMock(spec=TurnStateBase)
    runtime.state.custom = {
        TurnCustomKey.GRAPH_TOPOLOGY_CONTEXT: "planner -> implementer",
        TurnCustomKey.GRAPH_NODE_DESCRIPTION: "Review the implementation.",
    }
    ctx = AgentContext(
        system_prompt="",
        history=MagicMock(spec=MessageHistory),
        tool_manager=InMemoryToolManager(),
        session=SessionInfo.from_str("test.reviewer"),
        runtime=runtime,
        graph_context=MagicMock(spec=GraphContext),
    )
    token = current_agent_context.set(ctx)
    try:
        provider = GraphWorkflowProvider()
        result = await provider._fetch_content()
        assert "### Knowledge Base" not in result
        assert "### Topology\n" in result
        assert "### Your Role\n" in result
    finally:
        current_agent_context.reset(token)


@pytest.mark.asyncio
async def test_graph_workflow_provider_empty_when_no_graph_context() -> None:
    from modex_agent.core.agent import AgentContext, current_agent_context
    from modex_agent.core.history import MessageHistory
    from modex_agent.core.session_id import SessionInfo
    from modex_agent.core.tool_manager import InMemoryToolManager
    from modex_agent.memory.prompt_pipeline.providers import GraphWorkflowProvider

    ctx = AgentContext(
        system_prompt="",
        history=MagicMock(spec=MessageHistory),
        tool_manager=InMemoryToolManager(),
        session=SessionInfo.from_str("test.planner"),
        graph_context=None,
    )
    token = current_agent_context.set(ctx)
    try:
        provider = GraphWorkflowProvider()
        result = await provider._fetch_content()
        assert result == ""
    finally:
        current_agent_context.reset(token)


@pytest.mark.asyncio
async def test_graph_workflow_provider_end_only_emits_final_reply_pattern() -> None:
    """When downstream has END only (no AgentNode), emit Final Reply pattern (numbered 1), not Producer/Relay."""
    from modex_agent.agents.react.state import ReActTurnState
    from modex_agent.core.agent import AgentContext, current_agent_context
    from modex_agent.core.history import MessageHistory
    from modex_agent.core.session_id import SessionInfo
    from modex_agent.core.tool_manager import InMemoryToolManager
    from modex_agent.memory.prompt_pipeline.providers import GraphWorkflowProvider
    from modex_agent.runtime.enums import TurnCustomKey
    from modex_agent.runtime.models import TurnIdentity
    from modex_agent.runtime.services import AgentRuntime, AgentRuntimeServices
    from modex_graph.context import GraphContext

    state = ReActTurnState(identity=TurnIdentity(agent_id="test", session=SessionInfo.from_str("test.end"), turn_id="t1"))
    state.custom[TurnCustomKey.GRAPH_TOPOLOGY_CONTEXT] = ""
    state.custom[TurnCustomKey.GRAPH_DOWNSTREAM_HAS_END] = True
    ctx = AgentContext(
        system_prompt="",
        history=MagicMock(spec=MessageHistory),
        tool_manager=InMemoryToolManager(),
        session=SessionInfo.from_str("test.end"),
        graph_context=MagicMock(spec=GraphContext),
        runtime=AgentRuntime(services=AgentRuntimeServices(), state=state),
    )
    token = current_agent_context.set(ctx)
    try:
        provider = GraphWorkflowProvider()
        result = await provider._fetch_content()
        assert "**Deliver Content Guidelines**" in result
        assert "Final Reply" in result
        assert "Producer" not in result
        assert "Relay" not in result
        assert "**Pattern 1 — Final Reply**" in result
        assert "ONE assistant" in result
        assert "never as 'node X'" in result
        assert "self-contained section of the whole reply" in result
        assert "write the complete answer" in result
    finally:
        current_agent_context.reset(token)


@pytest.mark.asyncio
async def test_graph_workflow_provider_both_downstream_emits_all_patterns() -> None:
    """When downstream has both AgentNode and END, emit all 3 patterns numbered 1, 2, 3."""
    from modex_agent.agents.react.state import ReActTurnState
    from modex_agent.core.agent import AgentContext, current_agent_context
    from modex_agent.core.history import MessageHistory
    from modex_agent.core.session_id import SessionInfo
    from modex_agent.core.tool_manager import InMemoryToolManager
    from modex_agent.memory.prompt_pipeline.providers import GraphWorkflowProvider
    from modex_agent.runtime.enums import TurnCustomKey
    from modex_agent.runtime.models import TurnIdentity
    from modex_agent.runtime.services import AgentRuntime, AgentRuntimeServices
    from modex_graph.context import GraphContext

    state = ReActTurnState(identity=TurnIdentity(agent_id="test", session=SessionInfo.from_str("test.both"), turn_id="t1"))
    state.custom[TurnCustomKey.GRAPH_TOPOLOGY_CONTEXT] = ""
    state.custom[TurnCustomKey.GRAPH_DOWNSTREAM_HAS_AGENT] = True
    state.custom[TurnCustomKey.GRAPH_DOWNSTREAM_HAS_END] = True
    ctx = AgentContext(
        system_prompt="",
        history=MagicMock(spec=MessageHistory),
        tool_manager=InMemoryToolManager(),
        session=SessionInfo.from_str("test.both"),
        graph_context=MagicMock(spec=GraphContext),
        runtime=AgentRuntime(services=AgentRuntimeServices(), state=state),
    )
    token = current_agent_context.set(ctx)
    try:
        provider = GraphWorkflowProvider()
        result = await provider._fetch_content()
        assert "Producer" in result
        assert "Relay" in result
        assert "Final Reply" in result
        assert "**Pattern 1 — Producer**" in result
        assert "**Pattern 2 — Relay**" in result
        assert "**Pattern 3 — Final Reply**" in result
        assert "ONE assistant" in result
    finally:
        current_agent_context.reset(token)


@pytest.mark.asyncio
async def test_graph_workflow_provider_no_downstream_omits_deliver_guidelines() -> None:
    """When no downstream targets (both flags False), omit Deliver Content Guidelines entirely."""
    from modex_agent.agents.react.state import ReActTurnState
    from modex_agent.core.agent import AgentContext, current_agent_context
    from modex_agent.core.history import MessageHistory
    from modex_agent.core.session_id import SessionInfo
    from modex_agent.core.tool_manager import InMemoryToolManager
    from modex_agent.memory.prompt_pipeline.providers import GraphWorkflowProvider
    from modex_agent.runtime.enums import TurnCustomKey
    from modex_agent.runtime.models import TurnIdentity
    from modex_agent.runtime.services import AgentRuntime, AgentRuntimeServices
    from modex_graph.context import GraphContext

    state = ReActTurnState(identity=TurnIdentity(agent_id="test", session=SessionInfo.from_str("test.none"), turn_id="t1"))
    state.custom[TurnCustomKey.GRAPH_TOPOLOGY_CONTEXT] = ""
    ctx = AgentContext(
        system_prompt="",
        history=MagicMock(spec=MessageHistory),
        tool_manager=InMemoryToolManager(),
        session=SessionInfo.from_str("test.none"),
        graph_context=MagicMock(spec=GraphContext),
        runtime=AgentRuntime(services=AgentRuntimeServices(), state=state),
    )
    token = current_agent_context.set(ctx)
    try:
        provider = GraphWorkflowProvider()
        result = await provider._fetch_content()
        assert "**Deliver Content Guidelines**" not in result
        assert "Producer" not in result
        assert "Final Reply" not in result
        assert "MUST call `deliver`" in result
    finally:
        current_agent_context.reset(token)


@pytest.mark.asyncio
async def test_graph_workflow_provider_version_encodes_downstream_types() -> None:
    """Version encodes downstream types before the description hash."""
    from modex_agent.agents.react.state import ReActTurnState
    from modex_agent.core.agent import AgentContext, current_agent_context
    from modex_agent.core.history import MessageHistory
    from modex_agent.core.session_id import SessionInfo
    from modex_agent.core.tool_manager import InMemoryToolManager
    from modex_agent.memory.prompt_pipeline.providers import GraphWorkflowProvider
    from modex_agent.runtime.enums import TurnCustomKey
    from modex_agent.runtime.models import TurnIdentity
    from modex_agent.runtime.services import AgentRuntime, AgentRuntimeServices
    from modex_graph.context import GraphContext

    def _make_ctx(has_agent: bool, has_end: bool) -> AgentContext:
        state = ReActTurnState(identity=TurnIdentity(agent_id="test", session=SessionInfo.from_str("test.v"), turn_id="t1"))
        state.custom[TurnCustomKey.GRAPH_TOPOLOGY_CONTEXT] = ""
        state.custom[TurnCustomKey.GRAPH_DOWNSTREAM_HAS_AGENT] = has_agent
        state.custom[TurnCustomKey.GRAPH_DOWNSTREAM_HAS_END] = has_end
        return AgentContext(
            system_prompt="",
            history=MagicMock(spec=MessageHistory),
            tool_manager=InMemoryToolManager(),
            session=SessionInfo.from_str("test.v"),
            graph_context=MagicMock(spec=GraphContext),
            runtime=AgentRuntime(services=AgentRuntimeServices(), state=state),
        )

    provider = GraphWorkflowProvider()
    # graph:11
    ctx = _make_ctx(True, True)
    token = current_agent_context.set(ctx)
    try:
        expected = f"graph:11:{hashlib.sha1(b'').hexdigest()[:8]}"
        assert await provider._fetch_version() == expected
    finally:
        current_agent_context.reset(token)
    # graph:10
    ctx = _make_ctx(True, False)
    token = current_agent_context.set(ctx)
    try:
        expected = f"graph:10:{hashlib.sha1(b'').hexdigest()[:8]}"
        assert await provider._fetch_version() == expected
    finally:
        current_agent_context.reset(token)
    # graph:01
    ctx = _make_ctx(False, True)
    token = current_agent_context.set(ctx)
    try:
        expected = f"graph:01:{hashlib.sha1(b'').hexdigest()[:8]}"
        assert await provider._fetch_version() == expected
    finally:
        current_agent_context.reset(token)
    # graph:00
    ctx = _make_ctx(False, False)
    token = current_agent_context.set(ctx)
    try:
        expected = f"graph:00:{hashlib.sha1(b'').hexdigest()[:8]}"
        assert await provider._fetch_version() == expected
    finally:
        current_agent_context.reset(token)


@pytest.mark.asyncio
async def test_graph_workflow_provider_version_changes_with_node_description() -> None:
    from modex_agent.agents.react.state import ReActTurnState
    from modex_agent.core.agent import AgentContext, current_agent_context
    from modex_agent.core.history import MessageHistory
    from modex_agent.core.session_id import SessionInfo
    from modex_agent.core.tool_manager import InMemoryToolManager
    from modex_agent.memory.prompt_pipeline.providers import GraphWorkflowProvider
    from modex_agent.runtime.enums import TurnCustomKey
    from modex_agent.runtime.models import TurnIdentity
    from modex_agent.runtime.services import AgentRuntime, AgentRuntimeServices
    from modex_graph.context import GraphContext

    def _make_ctx(description: str) -> AgentContext:
        state = ReActTurnState(
            identity=TurnIdentity(
                agent_id="test",
                session=SessionInfo.from_str("test.description"),
                turn_id="t1",
            )
        )
        state.custom[TurnCustomKey.GRAPH_TOPOLOGY_CONTEXT] = ""
        state.custom[TurnCustomKey.GRAPH_DOWNSTREAM_HAS_AGENT] = True
        state.custom[TurnCustomKey.GRAPH_DOWNSTREAM_HAS_END] = True
        state.custom[TurnCustomKey.GRAPH_NODE_DESCRIPTION] = description
        return AgentContext(
            system_prompt="",
            history=MagicMock(spec=MessageHistory),
            tool_manager=InMemoryToolManager(),
            session=SessionInfo.from_str("test.description"),
            graph_context=MagicMock(spec=GraphContext),
            runtime=AgentRuntime(services=AgentRuntimeServices(), state=state),
        )

    provider = GraphWorkflowProvider()
    versions: list[str] = []
    for description in ("Reviewer role", "Coder role", "Reviewer role"):
        token = current_agent_context.set(_make_ctx(description))
        try:
            versions.append(await provider._fetch_version())
        finally:
            current_agent_context.reset(token)

    assert versions[0] != versions[1]
    assert versions[0] == versions[2]


@pytest.mark.asyncio
async def test_graph_workflow_provider_version_empty_description_is_stable() -> None:
    from modex_agent.agents.react.state import ReActTurnState
    from modex_agent.core.agent import AgentContext, current_agent_context
    from modex_agent.core.history import MessageHistory
    from modex_agent.core.session_id import SessionInfo
    from modex_agent.core.tool_manager import InMemoryToolManager
    from modex_agent.memory.prompt_pipeline.providers import GraphWorkflowProvider
    from modex_agent.runtime.enums import TurnCustomKey
    from modex_agent.runtime.models import TurnIdentity
    from modex_agent.runtime.services import AgentRuntime, AgentRuntimeServices
    from modex_graph.context import GraphContext

    def _make_ctx(description: str | None) -> AgentContext:
        state = ReActTurnState(
            identity=TurnIdentity(
                agent_id="test",
                session=SessionInfo.from_str("test.empty-description"),
                turn_id="t1",
            )
        )
        state.custom[TurnCustomKey.GRAPH_TOPOLOGY_CONTEXT] = ""
        state.custom[TurnCustomKey.GRAPH_DOWNSTREAM_HAS_AGENT] = True
        state.custom[TurnCustomKey.GRAPH_DOWNSTREAM_HAS_END] = False
        if description is not None:
            state.custom[TurnCustomKey.GRAPH_NODE_DESCRIPTION] = description
        return AgentContext(
            system_prompt="",
            history=MagicMock(spec=MessageHistory),
            tool_manager=InMemoryToolManager(),
            session=SessionInfo.from_str("test.empty-description"),
            graph_context=MagicMock(spec=GraphContext),
            runtime=AgentRuntime(services=AgentRuntimeServices(), state=state),
        )

    provider = GraphWorkflowProvider()
    token = current_agent_context.set(_make_ctx(""))
    try:
        empty_version = await provider._fetch_version()
    finally:
        current_agent_context.reset(token)

    token = current_agent_context.set(_make_ctx(None))
    try:
        absent_version = await provider._fetch_version()
    finally:
        current_agent_context.reset(token)

    assert empty_version == absent_version
