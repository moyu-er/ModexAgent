"""T12 — the ``todo.discipline`` section's capability-channel migration (todo 12).

The byte-parity proof: the golden files under ``goldens/`` were
machine-captured on the PRE-MIGRATION HEAD (commit 974db59db) while
``TodoAwareSystemPromptProvider`` still rendered the 2b position:

- ``todo_section_pre_migration.txt`` — the bare TodoAware section output
  (sha256 60d7457393ae249ede40a727ab804bc0c1745390cd686fdc6edc656e7cd047d0,
  632 chars);
- ``todo_prompt_pre_migration.txt`` — the composed prompt (base + todo
  section + AgentComm delegation; sha256
  7f1877f2230f7b264d3d8a70d3f37a0127d821ec26bc506a7201f0343f406348,
  3178 chars — byte-identical to T6's ``capability_sections_baseline.txt``
  at that commit).

After the migration the section renders through the capability channel
(``TodoCapability.assemble`` → the static section provider → the
``capability_sections`` anchor) and the provider CLASS is deleted: the
capability anchor is the sole deliverer at the same composed-prompt
position (the anchor renders BEFORE the retired 2b slot, so base → todo
section → AgentComm order is preserved). These tests pin:

- BYTE parity — the channel's section == the captured TodoAware bytes and
  the composed prompt is unchanged (SPEC §8.2 section row: 内容逐字搬家);
- the anchor geometry — fork (2a) < todo section < AgentComm (2c, still
  inline until its own package wave);
- scoping parity — only todo-capability agents get the section: the
  compile-time gate replaces the retired runtime tool-registration gate;
- the KV-cache version contract (SPEC §7.3 / E10) — static content means a
  constant version, so repeated ``load()`` calls never re-fetch;
- the death facts — ``TodoAwareSystemPromptProvider`` and the
  ``TodoPlanningNudgeHook`` factory residue are gone from the tree.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from modex_agent.core.constants import RuntimeInfoKey
from modex_agent.core.prompt import SystemPromptProvider
from modex_agent.memory.hooks import MemoryHookRunner
from modex_agent.memory.prompt_pipeline.providers import ForkContextSpec
from modex_agent.memory.system import MemorySystemContextManager
from modex_agent.plugins.capability import CapabilityBinding, PromptSectionSpec
from modex_agent.plugins.defaults import DefaultPlugin
from modex_agent.plugins.defaults.capabilities.todo import TodoCapability
from modex_agent.plugins.loader import PluginRegistrationContext
from modex_agent.plugins.registry import ComponentRegistry
from modex_agent.scope.compiler import compile_scope
from modex_agent.scope.spec import AgentSpec, PoolSpec, ScopeKind, ScopeSpec
from modex_agent.workspace.context import WorkspaceContext
from modex_agent.workspace.paths import WorkspacePaths

_DIR = Path(__file__).resolve().parent
_GOLDEN_DIR = _DIR / "goldens"
_ROOT = _DIR.parents[2]

_BASELINE_PROMPT = "You are the baseline agent."
_DISCIPLINE_SECTION = PromptSectionSpec(section_id="todo.discipline", order=30)


# ---- Harness (T6's shapes) ---------------------------------------------------


def _mock_memory_system() -> MagicMock:
    mock_system = MagicMock()
    mock_system.ensure_within_budget = AsyncMock()
    mock_system.retrieve_core_memory = AsyncMock(
        return_value=MagicMock(soul="", user="", memory="")
    )
    mock_system.get_core_memory_directory = AsyncMock(return_value=None)
    mock_system.get_storage_path = AsyncMock(return_value=None)
    mock_system.get_providers = MagicMock(return_value=[])
    mock_system.prefetch_memories = AsyncMock(return_value=None)
    mock_system.get_history = AsyncMock(return_value=[])
    mock_system.create_message_history = MagicMock(return_value=MagicMock())
    mock_system.hook_runner = MemoryHookRunner()
    mock_system.pruned_manager = None
    return mock_system


def _tool_manager(*names: str) -> Any:
    from modex_agent.core.tool_manager import InMemoryToolManager, Tool

    manager = InMemoryToolManager()
    for name in names:
        tool = MagicMock(spec=Tool)
        tool.name = name
        manager.register(tool)
    return manager


def _ctx_mgr(**kwargs: Any) -> MemorySystemContextManager:
    kwargs.setdefault("memory_system", _mock_memory_system())
    kwargs.setdefault("base_system_prompt", _BASELINE_PROMPT)
    return MemorySystemContextManager(**kwargs)


def _workspace_ctx() -> WorkspaceContext:
    target = Path("/tmp/test-todo-section-ws")
    return WorkspaceContext(target=target, paths=WorkspacePaths(root=target), is_home=False)


def _registry() -> ComponentRegistry:
    """The production DefaultPlugin registration face (sync, no loader)."""
    registry = ComponentRegistry()
    ctx = PluginRegistrationContext(registry)
    DefaultPlugin().register(ctx)
    ctx.flush()
    return registry


def _compile_todo_agent(*, capabilities: dict[str, Any] | None = None) -> Any:
    """Compile a bare root agent through the REAL compiler + registry."""
    spec = ScopeSpec(
        kind=ScopeKind.POOL,
        pool=PoolSpec(
            name="p",
            agents=[AgentSpec(name="root", capabilities=capabilities or {})],
        ),
    )
    return compile_scope(spec, workspace_ctx=_workspace_ctx(), registry=_registry()).agents[0]


async def _assembled_prompt(mgr: MemorySystemContextManager, **load_kwargs: Any) -> str:
    state = await mgr.load(load_kwargs.pop("session_id", "sess-1"), **load_kwargs)
    assert state.system_prompt_pipeline is not None
    return await state.system_prompt_pipeline.get_or_refresh()


async def _section_provider_from_assemble() -> SystemPromptProvider:
    """The REAL production path: compile → bind → assemble → provider."""
    compiled = _compile_todo_agent(capabilities={"todo": {}})
    binding = compiled.spec.capabilities[0].binding
    wiring = await TodoCapability().assemble(binding, MagicMock())
    providers = wiring.prompt_providers
    assert len(providers) == 1
    return providers[0]


# ---- (a) Byte parity — the section content ----------------------------------


class TestSectionByteParity:
    async def test_channel_section_bytes_match_pre_migration_todo_aware_output(self) -> None:
        golden = (_GOLDEN_DIR / "todo_section_pre_migration.txt").read_text(encoding="utf-8")
        provider = await _section_provider_from_assemble()

        content = await provider.get_or_refresh()

        assert content == golden

    async def test_composed_prompt_bytes_match_pre_migration(self) -> None:
        """base → todo section (capability channel) — the composed prompt
        equals the pre-migration capture byte-for-byte (the anchor
        renders before the retired 2b slot, so the section order is
        preserved; the capture was REGENERATED at the subagents
        migration when the retired AgentComm composite left the composed
        prompt — the delegation brief's own byte parity is pinned by
        ``test_subagents_supply.py`` against its own pre-migration
        capture)."""
        golden = (_GOLDEN_DIR / "todo_prompt_pre_migration.txt").read_text(encoding="utf-8")
        provider = await _section_provider_from_assemble()
        mgr = _ctx_mgr()
        mgr.set_capability_sections((provider,))

        prompt = await _assembled_prompt(
            mgr, tool_manager=_tool_manager("todo_read", "todo_write", "task")
        )

        assert prompt == golden


# ---- (b) assemble() wiring shape ----------------------------------------------


class TestAssembleWiring:
    async def test_provider_built_iff_discipline_section_active(self) -> None:
        capability = TodoCapability()

        active = await capability.assemble(
            CapabilityBinding(active_sections=(_DISCIPLINE_SECTION,)), MagicMock()
        )
        inactive = await capability.assemble(CapabilityBinding(active_sections=()), MagicMock())

        assert len(active.prompt_providers) == 1
        assert inactive.prompt_providers == ()
        assert active.artifacts == {}

    async def test_production_binding_carries_the_section(self) -> None:
        compiled = _compile_todo_agent(capabilities={"todo": {}})

        binding = compiled.spec.capabilities[0].binding

        assert binding.active_sections == (_DISCIPLINE_SECTION,)


# ---- (c) Anchor geometry -------------------------------------------------------


class TestAnchorPosition:
    async def test_fork_before_todo_section(self) -> None:
        builder = MagicMock()
        builder.build = AsyncMock(return_value="<parent_history>FORK-MARKER</parent_history>")
        provider = await _section_provider_from_assemble()
        mgr = _ctx_mgr(
            fork_context_spec=ForkContextSpec(
                builder=builder, agent_type="native_sub", fork_max_messages=20
            )
        )
        mgr.set_capability_sections((provider,))

        prompt = await _assembled_prompt(
            mgr,
            session_id="inv1.sub",
            runtime_info={RuntimeInfoKey.PARENT_SESSION_ID: "inv0.main"},
            tool_manager=_tool_manager("todo_read", "todo_write", "task"),
        )

        # fork (2a) < capability block; the retired AgentComm (2c)
        # position died with the subagents migration — a runtime
        # task-tool registration renders nothing.
        assert "FORK-MARKER" in prompt
        assert "## Task Tracking" in prompt
        assert "## Delegating To Subagents" not in prompt
        fork_pos = prompt.index("FORK-MARKER")
        todo_pos = prompt.index("## Task Tracking")
        assert fork_pos < todo_pos


# ---- (d) Scoping parity — compile-time enablement is the only gate -------------


class TestScopingParity:
    async def test_agent_without_capability_gets_no_section(self) -> None:
        """The old gate (TodoAware rendering iff the tool manager carried
        both todo tools) is dead; the compile-time gate is the only gate.
        A no-capability agent has no binding — nothing to assemble — so
        no section renders even when the tool manager happens to carry
        todo tools (a harness-only shape: on the shipped tree todo tools
        enter rosters only through the capability)."""
        mgr = _ctx_mgr()

        prompt = await _assembled_prompt(
            mgr, tool_manager=_tool_manager("todo_read", "todo_write", "task")
        )

        assert "## Task Tracking" not in prompt
        assert "## Delegating To Subagents" not in prompt  # 2c died with subagents

    def test_no_capability_agent_compiles_to_empty_capabilities(self) -> None:
        compiled = _compile_todo_agent(capabilities={})

        assert compiled.spec.capabilities == ()
        assert "todo_write" not in compiled.spec.tools


# ---- (e) KV-cache version contract (SPEC §7.3 / E10) ---------------------------


class TestVersionContract:
    async def test_constant_version_keeps_prompt_stable_across_loads(self) -> None:
        provider = await _section_provider_from_assemble()
        mgr = _ctx_mgr()
        mgr.set_capability_sections((provider,))

        prompt_first = await _assembled_prompt(
            mgr, tool_manager=_tool_manager("todo_read", "todo_write")
        )
        version_first = provider.last_version
        prompt_second = await _assembled_prompt(
            mgr, tool_manager=_tool_manager("todo_read", "todo_write")
        )

        # static content ⇒ constant version ⇒ stable prefix within a session
        assert prompt_first == prompt_second
        assert provider.last_version == version_first
        assert version_first is not None
        # the section survives the second load (channel is load-stable)
        assert "## Task Tracking" in prompt_second


# ---- (f) Death facts -------------------------------------------------------------


# Runtime/generated state (never source): the bot's `.modex` SQLite state,
# runtime-populated `experiences`/`subworkspace`, logs, and caches.
_SKIPPED_DIRS = {
    "__pycache__",
    "node_modules",
    "dist",
    "target",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".modex",
    "experiences",
    "subworkspace",
    "logs",
}


def _iter_source_files() -> Any:
    for base in (_ROOT / "src", _ROOT / "examples"):
        for path in base.rglob("*"):
            if not path.is_file():
                continue
            if any(part in _SKIPPED_DIRS for part in path.parts):
                continue
            yield path


def _source_tree_contains(needle: str) -> list[str]:
    marker = needle.encode("utf-8")
    return [str(path) for path in _iter_source_files() if marker in path.read_bytes()]


class TestTodoAwareProviderDeath:
    def test_no_todo_aware_references_in_src_or_examples(self) -> None:
        """The plan's acceptance grep: ``TodoAwareSystemPromptProvider``
        is gone from src/ and examples/ (code AND docs)."""
        assert _source_tree_contains("TodoAwareSystemPromptProvider") == []

    def test_provider_class_not_importable(self) -> None:
        import pytest

        with pytest.raises(ImportError):
            from modex_agent.memory.prompt_pipeline.providers import (  # noqa: F401
                TodoAwareSystemPromptProvider,
            )

    def test_prompt_constant_gone_from_providers(self) -> None:
        source = (
            _ROOT / "src" / "modex_agent" / "memory" / "prompt_pipeline" / "providers.py"
        ).read_text(encoding="utf-8")
        assert "_TODO_TASK_DISCIPLINE_PROMPT" not in source

    def test_load_has_no_2b_append(self) -> None:
        source = (_ROOT / "src" / "modex_agent" / "memory" / "system.py").read_text(
            encoding="utf-8"
        )
        assert "TodoAwareSystemPromptProvider" not in source
