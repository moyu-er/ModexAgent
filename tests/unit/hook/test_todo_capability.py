"""The FW-bundled ``todo`` capability — the T10 migration faces.

Covers:

- **Protocol shape** — ``TodoCapability`` is a pure opt-in bundle
  contributing the two todo tool names, the two hook names
  (react-runner ``todo_continuation`` + memory-runner
  ``todo_reorientation``), and the ``todo.discipline`` section spec.
- **Dual anchor** — ``bind`` fails loudly when either todo tool is
  vetoed (``tools: [-todo_write]``): the two tools move together, and
  the error names pool/agent/capability + BOTH tools + the veto entry.
- **Priority dispatch (B3)** — ``TodoContinuationHookFactory`` declares
  ``priority = -1000``; roster dispatch threads the factory priority
  into the ``HookSpec`` so the continuation hook runs first among
  AfterTurnHook sources (the ordering the retired
  ``register_tree_aware_hooks`` todo branch used to assign).
- **Runtime-gate death** — the historical ``tool_manager.is_registered``
  gate in ``TodoContinuationHook`` is gone: enablement is compile-time
  knowledge, so the scenario the gate used to block (a tool manager
  without ``todo_write``) now runs.
- **Golden split-brain** — the shipped bot.yml's post-migration facets
  vs the machine-captured pre-migration goldens
  (``tests/unit/scope/goldens/todo/``, captured on this wave's parent
  commit), with the two documented facet exemptions.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

from modex_agent.hook import HookPoint, HookRunner
from modex_agent.hook.abc import AfterTurnHook
from modex_agent.hook.builtin.todo_continuation import TodoContinuationHook
from modex_agent.plugins.abc import AgentType, ComponentSlot, HookRunnerKind, SimpleFactory
from modex_agent.plugins.assembly.native_core import _dispatch_hooks
from modex_agent.plugins.capability import (
    CapabilityError,
    PromptSectionSpec,
    TreePositionView,
)
from modex_agent.plugins.defaults import DefaultPlugin
from modex_agent.plugins.defaults.capabilities.todo import TodoCapability
from modex_agent.plugins.defaults.hooks import RunLoggingHookFactory, TodoContinuationHookFactory
from modex_agent.plugins.loader import PluginRegistrationContext
from modex_agent.plugins.registry import ComponentRegistry
from modex_agent.scope.compiler import ToolOrigin, compile_scope
from modex_agent.scope.spec import AgentSpec, PoolSpec, ScopeKind, ScopeSpec
from modex_agent.workspace.context import WorkspaceContext
from modex_agent.workspace.paths import WorkspacePaths
from tests.unit.scope.goldens.assertor import (
    Exemption,
    FacetField,
    Facets,
    GoldenFile,
    assert_facets_equal,
)
from tests.unit.scope.goldens.capture import GoldenPackage, capture_package_facets

if TYPE_CHECKING:
    from modex_agent.core.agent import AgentContext

_DIR = Path(__file__).resolve().parent
_GOLDEN_DIR = _DIR.parent / "scope" / "goldens" / "todo"
_HOOK_SOURCE = (
    Path(__file__).parents[3] / "src" / "modex_agent" / "hook" / "builtin" / "todo_continuation.py"
)

# The shipped bot.yml agents that declared the todo package pre-migration
# (the golden's hook_roster/sections facets differ for exactly these).
_TODO_AGENTS_PATTERN = r"(office-expert|orchestrator|explore|general|reviewer)"


def _registry() -> ComponentRegistry:
    """A registry carrying the FW defaults (the todo capability lives in
    DefaultPlugin — the production registration face)."""
    registry = ComponentRegistry()
    ctx = PluginRegistrationContext(registry)
    DefaultPlugin().register(ctx)
    ctx.flush()
    return registry


def _tree_view() -> TreePositionView:
    return TreePositionView(
        pool_name="p", agent_name="root", is_root=True, parent=None, children=(), peers=()
    )


def _workspace_ctx() -> WorkspaceContext:
    target = Path("/tmp/test_todo_capability_ws")
    return WorkspaceContext(target=target, paths=WorkspacePaths(root=target), is_home=False)


def _compile_hooks(agent: AgentSpec) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Compile one agent; return (final tools, merged hooks)."""
    spec = ScopeSpec(kind=ScopeKind.POOL, pool=PoolSpec(name="p", agents=[agent]))
    compilation = compile_scope(
        spec,
        workspace_ctx=_workspace_ctx(),
        registry=_registry(),
    )
    compiled = compilation.agents[0]
    return tuple(compiled.spec.tools), tuple(compiled.spec.hooks)


# ─── Protocol shape ─────────────────────────────────────────────────────────


class TestProtocolShape:
    def test_registered_in_capability_slot(self) -> None:
        registry = _registry()
        assert registry.resolve(ComponentSlot.CAPABILITY, "todo") is not None
        assert isinstance(registry.resolve_capability("todo"), TodoCapability)

    def test_applies_default_false(self) -> None:
        declaration = AgentSpec(name="main", capabilities={"todo": {}})
        spec = ScopeSpec(kind=ScopeKind.POOL, pool=PoolSpec(name="p", agents=[declaration]))
        # No declared override → the pure opt-in predicate never auto-applies.
        assert TodoCapability().applies(MagicMock()) is False
        compilation = compile_scope(spec, workspace_ctx=_workspace_ctx(), registry=_registry())
        assert [cap.name for cap in compilation.agents[0].spec.capabilities] == ["todo"]

    def test_contribute_shape(self) -> None:
        contribution = TodoCapability().contribute(_tree_view(), TodoCapability().config_model())
        assert contribution.tools == ("todo_write", "todo_read")
        assert contribution.hooks == ("todo_continuation", "todo_reorientation")
        assert contribution.sections == (PromptSectionSpec(section_id="todo.discipline", order=30),)
        assert contribution.tool_replacements == ()

    def test_config_rejects_unknown_keys(self) -> None:
        with pytest.raises(ValidationError):
            TodoCapability().config_model.model_validate({"bogus": 1})


# ─── Dual anchor (C2) ───────────────────────────────────────────────────────


class TestDualAnchor:
    def test_both_tools_and_hooks_reach_merged_rosters(self) -> None:
        tools, hooks = _compile_hooks(AgentSpec(name="main", capabilities={"todo": {}}))

        assert "todo_write" in tools
        assert "todo_read" in tools
        assert "todo_continuation" in hooks
        assert "todo_reorientation" in hooks

    def test_veto_todo_write_fails_loud_naming_both_tools(self) -> None:
        agent = AgentSpec(
            name="main",
            capabilities={"todo": {}},
            tools=["-todo_write"],
        )

        with pytest.raises(CapabilityError) as excinfo:
            _compile_hooks(agent)

        message = str(excinfo.value)
        assert "'todo'" in message  # capability
        assert "'p'" in message  # pool
        assert "'main'" in message  # agent
        assert "todo_write" in message and "todo_read" in message  # BOTH tools
        assert "tools: [-todo_write]" in message  # the veto entry + repair path

    def test_veto_todo_read_fails_loud_naming_both_tools(self) -> None:
        agent = AgentSpec(
            name="main",
            capabilities={"todo": {}},
            tools=["-todo_read"],
        )

        with pytest.raises(CapabilityError, match="todo_write.*todo_read|todo_read.*todo_write"):
            _compile_hooks(agent)

    def test_capability_false_disables_whole_bundle(self) -> None:
        tools, hooks = _compile_hooks(
            AgentSpec(name="main", capabilities={"todo": False}, tools=["-todo_write"])
        )

        assert "todo_write" not in tools and "todo_read" not in tools
        assert "todo_continuation" not in hooks and "todo_reorientation" not in hooks

    def test_hook_veto_is_component_surgery_not_anchor_failure(self) -> None:
        tools, hooks = _compile_hooks(
            AgentSpec(
                name="main",
                capabilities={"todo": {}},
                hooks=["-todo_continuation"],
            )
        )

        assert "todo_write" in tools and "todo_read" in tools
        assert "todo_continuation" not in hooks
        assert "todo_reorientation" in hooks

    def test_binding_carries_the_section_spec(self) -> None:
        spec = ScopeSpec(
            kind=ScopeKind.POOL,
            pool=PoolSpec(name="p", agents=[AgentSpec(name="main", capabilities={"todo": {}})]),
        )
        compilation = compile_scope(spec, workspace_ctx=_workspace_ctx(), registry=_registry())
        binding = compilation.agents[0].spec.capabilities[0].binding
        assert binding.active_sections == (
            PromptSectionSpec(section_id="todo.discipline", order=30),
        )


# ─── Priority dispatch (B3) ─────────────────────────────────────────────────


class _EmptyHookConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class _RecordingAfterTurnHook(AfterTurnHook):
    def __init__(self, log: list[str]) -> None:
        self._log = log

    @property
    def name(self) -> str:
        return "priority_recorder"

    async def after_turn(self, ctx: AgentContext, result: object = None) -> None:
        self._log.append(self.name)


def _dispatch_spec(hooks: list[str]) -> MagicMock:
    spec = MagicMock()
    spec.hooks = hooks
    spec.hook_configs = {}
    spec.agent_type = AgentType.native_main
    spec.memory_system = None
    return spec


def _supply_ctx() -> Any:
    """A full-chain ctx carrying the pool's todo supply (the factories'
    read surface since the supply convergence)."""
    from modex_agent.plugins.assembly.context import PoolContext, PoolRuntimeDeps
    from modex_agent.plugins.defaults.capabilities.todo import TodoSupply

    ctx = MagicMock(spec=PoolContext)
    ctx.pool_runtime = PoolRuntimeDeps(
        session_tree_manager=MagicMock(),
        capability_supply={"todo": TodoSupply(store=MagicMock(name="todo_store"))},
    )
    return ctx


class TestPriorityDispatch:
    def test_factory_declares_negative_priority(self) -> None:
        assert TodoContinuationHookFactory.priority == -1000

    def test_default_factory_priority_is_zero(self) -> None:
        # The generic mechanism default: factories that declare nothing
        # dispatch at priority 0 (HookSpec's own default).
        assert TodoContinuationHookFactory().priority == -1000
        assert RunLoggingHookFactory.priority == 0

    async def test_dispatch_threads_factory_priority_into_hook_spec(self) -> None:
        registry = _registry()
        runner = HookRunner()

        await _dispatch_hooks(
            _dispatch_spec(["todo_continuation"]), registry, _supply_ctx(), runner, None
        )

        (spec,) = runner.hook_specs
        assert spec.hook.name == "todo_continuation"
        assert spec.priority == -1000

    async def test_todo_continuation_dispatches_before_priority_zero_hook(self) -> None:
        log: list[str] = []
        registry = _registry()
        ctx = PluginRegistrationContext(registry)
        DefaultPlugin().register(ctx)
        ctx.register_hook(
            "priority_recorder",
            SimpleFactory(
                _RecordingAfterTurnHook(log),
                _EmptyHookConfig,
                hook_runner=HookRunnerKind.react,
            ),
        )
        ctx.flush()
        runner = HookRunner()

        # The recorder is registered FIRST — the priority sort must still
        # put the -1000 continuation hook ahead of it.
        await _dispatch_hooks(
            _dispatch_spec(["priority_recorder", "todo_continuation"]),
            registry,
            _supply_ctx(),
            runner,
            None,
        )
        assert [s.hook.name for s in runner.hook_specs] == [
            "priority_recorder",
            "todo_continuation",
        ]

        todo_hook = next(s.hook for s in runner.hook_specs if s.hook.name == "todo_continuation")
        with patch.object(
            todo_hook,
            "after_turn",
            new=AsyncMock(side_effect=lambda *a, **k: log.append("todo_continuation")),
        ):
            await runner.dispatch(HookPoint.AFTER_TURN, MagicMock())

        assert log == ["todo_continuation", "priority_recorder"]


# ─── Runtime-gate death ─────────────────────────────────────────────────────


class TestRuntimeGateDeath:
    def test_gate_code_is_gone(self) -> None:
        hook_source = _HOOK_SOURCE.read_text(encoding="utf-8")
        assert "is_registered" not in hook_source
        assert "tool_manager" not in hook_source

    async def test_hook_runs_without_todo_write_registered(self, tmp_path: Path) -> None:
        """The scenario the runtime gate used to block — a tool manager
        without ``todo_write`` — now runs: enablement is compile-time
        knowledge (the hook exists only where the capability is
        effective), so the hook acts on the store, not on the tool
        registry."""
        from modex_agent.agents.react.state import ReActTurnState
        from modex_agent.core.agent import AgentContext
        from modex_agent.core.constants import StopReason
        from modex_agent.core.emitter import AgentResult
        from modex_agent.core.session_id import SessionInfo
        from modex_agent.core.tool_manager import InMemoryToolManager
        from modex_agent.core.types import TodoStatus
        from modex_agent.memory.history import ListMessageHistory
        from modex_agent.runtime.enums import AgentKind, TurnCustomKey, TurnPhase
        from modex_agent.runtime.models import TurnIdentity
        from modex_agent.runtime.services import AgentRuntime, AgentRuntimeServices
        from modex_agent.runtime.store import JsonFileTodoStore, TodoItem

        identity = TurnIdentity(
            agent_id="test", session=SessionInfo.from_str("session.agent"), turn_id="turn-1"
        )
        state = ReActTurnState(
            identity=identity,
            agent_kind=AgentKind.REACT,
            phase=TurnPhase.RUNNING,
            turn_attempt=1,
        )
        store = JsonFileTodoStore(tmp_path)
        await store.save(
            str(identity.session),
            [TodoItem(content="gate death", status=TodoStatus.PENDING)],
        )
        context = AgentContext(
            system_prompt="test",
            history=ListMessageHistory(),
            tool_manager=InMemoryToolManager(),  # empty — todo_write NOT registered
            session=identity.session,
            runtime=AgentRuntime(services=AgentRuntimeServices(), state=state),
            graph_context=MagicMock(),
            identity=identity,
        )

        await TodoContinuationHook(todo_store=store).after_turn(
            context,
            AgentResult(content="done", stop_reason=StopReason.COMPLETED),
        )

        assert state.custom[TurnCustomKey.CONTINUATION_REQUEST] is True
        messages = await context.history.to_list()
        assert len(messages) == 1


# ─── Golden split-brain (machine-captured pre-migration facets) ──────────────

_TODO_EXEMPTIONS = (
    Exemption(
        package="todo",
        facet_field=FacetField.HOOK_ROSTER,
        agent_pattern=_TODO_AGENTS_PATTERN,
        reason=(
            "hook roster now declarative — the same todo_continuation and "
            "todo_reorientation hooks are delivered through the roster "
            "channel (factory dispatch); the golden predates declarable "
            "hook rosters, where these hooks were assembly-time injections"
        ),
    ),
    Exemption(
        package="todo",
        facet_field=FacetField.SECTIONS,
        agent_pattern=_TODO_AGENTS_PATTERN,
        reason=(
            "todo.discipline section spec now declarative (order=30) — the "
            "golden predates declarable sections (empty pre-migration). The "
            "byte-parity content provider has LANDED (todo 12): the section "
            "renders through the capability channel with bytes identical to "
            "the retired TodoAwareSystemPromptProvider output, pinned by "
            "tests/unit/memory/test_todo_section.py against the "
            "pre-migration capture "
            "goldens/todo_{section,prompt}_pre_migration.txt"
        ),
    ),
)

# The experience wave (T13) contributed experience.injection to the
# SHIPPED experience agents — a foreign-package delta riding this
# golden's sections facet. Reviewer is already covered by the
# todo-scoped pattern above (its sections facet carries a todo delta
# anyway); "default" — not a todo agent — needs its own entry. The table
# rides only the pools whose golden carries the experience tool.
_EXPERIENCE_SECTIONS_ON_TODO_GOLDEN = (
    Exemption(
        package="todo",
        facet_field=FacetField.SECTIONS,
        agent_pattern=r"(default|reviewer)",
        reason=(
            "experience.injection section spec now declarative on the "
            "shipped experience agents (order=50) — the experience "
            "capability wave (T13) contributed it; the golden predates the "
            "experience migration"
        ),
    ),
)

# The subagents wave (T15) rides this golden's facets on every native
# topology agent (hook roster + sections) and on the external peer pool
# (the retired dead-weight derived entry + the supply-key projection
# switch) — the same cross-golden contamination pattern.
_SUBAGENTS_HOOK_ON_TODO_GOLDEN = (
    Exemption(
        package="todo",
        facet_field=FacetField.HOOK_ROSTER,
        agent_pattern=r"(office-expert|explore|general)",
        reason=(
            "subagent_auto_send is now a roster entry the subagents "
            "capability contributes for every non-root agent — the golden "
            "predates the subagents migration (T15)"
        ),
    ),
)
_SUBAGENTS_SECTIONS_ON_TODO_GOLDEN = (
    Exemption(
        package="todo",
        facet_field=FacetField.SECTIONS,
        agent_pattern=r"(default|office-expert|orchestrator|explore|general|reviewer)",
        reason=(
            "subagents.delegation/consultation/peer section specs now "
            "declarative (orders 40/41/42) — the golden predates the "
            "subagents migration (T15); the content providers land with the "
            "subagents supply wave (two-step)"
        ),
    ),
)
_SUBAGENTS_ON_EXTERNAL_POOL_TODO_GOLDEN = (
    Exemption(
        package="todo",
        facet_field=FacetField.TOOL_ROSTER,
        agent_pattern=r"opencode",
        reason=(
            "the retired compiler-side tree derivation produced a dead-weight "
            "send_to_peer entry on the external root; SPEC §3.2 C0 structural "
            "exclusion means subagents predicates never run for external "
            "agents (T15)"
        ),
    ),
    Exemption(
        package="todo",
        facet_field=FacetField.SUPPLY_KEYS,
        agent_pattern=r"opencode",
        reason=(
            "the capture's subagents supply-key projection switched to "
            "compile-product authority with the subagents migration (T15); "
            "the external opencode pool compiles no capabilities"
        ),
    ),
)

_CAPABILITY_ORIGIN_RECLASSIFICATION_REASON = (
    "origin reclassified SUPPLEMENT→CAPABILITY_DERIVED — the channel's true name, SPEC §9"
)

_NATIVE_AGENTS_PATTERN = r"(default|office-expert|orchestrator|explore|general|reviewer)"
_POSITION_DEFAULT_HOOKS_REASON = (
    "deliver_retry / length_guard / native_env are compiler position-default "
    "roster rows (SPEC §3.2 hook rows, T23) and model_choice_bind a declared "
    "roster entry on the native mains — the golden predates the W6 glue "
    "eradication (code-wired injections then)"
)


def _position_default_hook_exemption_for(golden: Mapping[str, Facets]) -> tuple[Exemption, ...]:
    """The T23 position-default hook rows ride every NATIVE pool's hook
    roster — external agents are structurally excluded, so the external
    pool's facets carry no drift and the table must not ride its call
    (the assertor's unused-exemption check is per call)."""
    origins = {
        tool.origin.value
        for facets in golden.values()
        for tool in facets.tool_roster
        if tool.origin.value.startswith("derived_")
    }
    if not ({"derived_task", "derived_send_to_agent"} & origins):
        return ()
    return (
        Exemption(
            package="todo",
            facet_field=FacetField.HOOK_ROSTER,
            agent_pattern=_NATIVE_AGENTS_PATTERN,
            reason=_POSITION_DEFAULT_HOOKS_REASON,
        ),
    )


def _capability_origin_exemptions_for(golden: Mapping[str, Facets]) -> tuple[Exemption, ...]:
    affected_agents = sorted(
        agent
        for agent, facets in golden.items()
        if any(tool.origin is ToolOrigin.SUPPLEMENT for tool in facets.tool_roster)
    )
    if not affected_agents:
        return ()
    return (
        Exemption(
            package="todo",
            facet_field=FacetField.TOOL_ROSTER,
            agent_pattern=f"({'|'.join(affected_agents)})",
            reason=_CAPABILITY_ORIGIN_RECLASSIFICATION_REASON,
        ),
    )


def _subagents_exemptions_for(golden: Mapping[str, Facets]) -> tuple[Exemption, ...]:
    """The subagents-wave exemptions riding one pool's comparison — derived
    from the golden's own derived-entry origins: a pool carrying
    task/send_to_agent entries has native topology agents (hook + sections
    deltas); a pool carrying ONLY derived_send_to_peer is the external peer
    pool (dead-weight entry + projection deltas)."""
    origins = {
        tool.origin.value
        for facets in golden.values()
        for tool in facets.tool_roster
        if tool.origin.value.startswith("derived_")
    }
    if "derived_task" in origins or "derived_send_to_agent" in origins:
        return _SUBAGENTS_HOOK_ON_TODO_GOLDEN + _SUBAGENTS_SECTIONS_ON_TODO_GOLDEN
    if origins:
        return _SUBAGENTS_ON_EXTERNAL_POOL_TODO_GOLDEN
    return ()


class TestGoldenSplitBrain:
    async def test_shipped_bot_facets_match_pre_migration_goldens(self) -> None:
        actual = await capture_package_facets(GoldenPackage.TODO)

        assert sorted(actual) == ["coder", "default", "opencode", "review"]
        for pool, document in actual.items():
            golden = GoldenFile.model_validate_json(
                (_GOLDEN_DIR / f"{pool}.json").read_text(encoding="utf-8")
            ).root
            # The assertor's unused-exemption check is per call: pools with
            # no todo-effective agent (opencode — external, no capabilities)
            # have no todo facet deltas, so the todo exemption table must
            # not ride their comparison. The subagents wave's deltas ride
            # every pool with topology-participating agents.
            pool_has_todo = any("todo" in facets.effective_set for facets in golden.values())
            exemptions = _TODO_EXEMPTIONS if pool_has_todo else ()
            # The experience wave's section delta (the "default" exemption
            # above) rides only pools whose golden carries the experience
            # tool — this capture is todo-scoped, so effective_set never
            # shows "experience"; the full tool_roster facet does.
            pool_declares_experience = any(
                any(tool.name == "experience" for tool in facets.tool_roster)
                for facets in golden.values()
            )
            if pool_declares_experience:
                exemptions += _EXPERIENCE_SECTIONS_ON_TODO_GOLDEN
            exemptions += _subagents_exemptions_for(golden)
            exemptions += _capability_origin_exemptions_for(golden)
            exemptions += _position_default_hook_exemption_for(golden)
            assert_facets_equal(document.root, golden, "todo", exemptions)
