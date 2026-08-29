"""Framework position-default hooks (T23 — SPEC §3.2 hook rows / W6 P7).

The four retired no-declaration-face injections became compiler-visible
position defaults (deliver_retry / length_guard / native_env) plus one
declaration-driven BIZ hook (model_choice_bind, shipped via bot.yml):

- every NATIVE agent's ``merged_hooks`` gains the position-default names
  through the merge base (the same ± semantics as tools — ``hooks:
  [-deliver_retry]`` vetoes, ``+name`` dedups);
- external agents are structurally excluded (no native hook face);
- every hook entry carries a sourced origin in the bill
  (position-default / capability / declared — SPEC §14.8 zero-unsourced);
- the HOOK-slot factories resolve the names at assembly, deriving their
  per-pool construction deps from the context chain (the tree for
  ``deliver_retry``, the pool/workspace facts for ``native_env``).

The retired ``register_tree_aware_hooks`` convergence function and the
BIZ ``_wire_main_pipeline`` injection sites died with this wave.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

from modex_agent.agents.external.paths import ProviderKind
from modex_agent.agents.external.types import ExternalEnvSpec
from modex_agent.core.agent import AgentCommKind
from modex_agent.hook.builtin.deliver_retry import DeliverRetryHook
from modex_agent.hook.builtin.length_guard import LengthGuardHook
from modex_agent.multi_agent.execution_strategy import PoolAssemblyContext
from modex_agent.plugins.abc import ComponentSlot
from modex_agent.plugins.assembly.context import (
    PoolRuntimeDeps,
    agent_context_chain,
    resolution_context,
)
from modex_agent.plugins.defaults import DefaultPlugin
from modex_agent.plugins.defaults.hooks import (
    DeliverRetryHookFactory,
    LengthGuardHookFactory,
    NativeEnvInjectionHookConfig,
    NativeEnvInjectionHookFactory,
)
from modex_agent.plugins.loader import PluginRegistrationContext
from modex_agent.plugins.registry import ComponentRegistry
from modex_agent.scope.compiler import (
    HookOrigin,
    ProvenanceLayer,
    ScopeCompilation,
    compile_scope,
)
from modex_agent.scope.spec import AgentSpec, PoolSpec, ScopeKind, ScopeSpec
from modex_agent.workspace.context import WorkspaceContext
from modex_agent.workspace.paths import WorkspacePaths

# ─── Compile-level helpers ──────────────────────────────────────────────────


def _workspace_ctx() -> WorkspaceContext:
    target = Path("/tmp/test_position_defaults_ws")
    return WorkspaceContext(target=target, paths=WorkspacePaths(root=target), is_home=False)


def _registry() -> ComponentRegistry:
    registry = ComponentRegistry()
    ctx = PluginRegistrationContext(registry)
    DefaultPlugin().register(ctx)
    ctx.flush()
    return registry


def _pool_spec(*agents: AgentSpec) -> PoolSpec:
    return PoolSpec(name="p", agents=list(agents))


def _compile(*agents: AgentSpec) -> ScopeCompilation:
    return compile_scope(
        ScopeSpec(kind=ScopeKind.POOL, pool=_pool_spec(*agents)),
        workspace_ctx=_workspace_ctx(),
        registry=_registry(),
    )


def _hooks_of(compilation: ScopeCompilation, agent: str) -> list[str]:
    return next(a for a in compilation.agents if a.provenance.agent == agent).spec.hooks


def _hook_row(compilation: ScopeCompilation, agent: str, hook: str) -> Any:
    provenance = next(a for a in compilation.agents if a.provenance.agent == agent).provenance
    return next((row for row in provenance.hooks if row.hook == hook), None)


def _field_row(compilation: ScopeCompilation, agent: str, field: str) -> Any:
    provenance = next(a for a in compilation.agents if a.provenance.agent == agent).provenance
    return next((row for row in provenance.fields if row.field == field), None)


# ─── (1) Default-on: every native agent's roster carries the table ──────────


class TestDefaultOn:
    def test_root_gains_position_default_hooks(self) -> None:
        hooks = _hooks_of(_compile(AgentSpec(name="main")), "main")
        assert hooks[:3] == ["deliver_retry", "length_guard", "native_env"]

    def test_sub_gains_position_default_hooks(self) -> None:
        compilation = _compile(AgentSpec(name="main"), AgentSpec(name="sub", parent="main"))
        assert _hooks_of(compilation, "sub")[:3] == ["deliver_retry", "length_guard", "native_env"]

    def test_external_agents_are_structurally_excluded(self) -> None:
        compilation = _compile(
            AgentSpec(name="main"),
            AgentSpec(
                name="ext",
                parent="main",
                execution_strategy="external",
                provider_kind=ProviderKind.OPENCODE,
            ),
        )
        assert "deliver_retry" not in _hooks_of(compilation, "ext")
        assert "length_guard" not in _hooks_of(compilation, "ext")
        assert "native_env" not in _hooks_of(compilation, "ext")

    def test_external_root_is_excluded(self) -> None:
        compilation = _compile(
            AgentSpec(
                name="opencode",
                execution_strategy="external",
                provider_kind=ProviderKind.OPENCODE,
            ),
        )
        assert _hooks_of(compilation, "opencode") == []


# ─── (2) Veto / dedup: the standard ± semantics ─────────────────────────────


class TestVetoAndDedup:
    def test_minus_veto_removes_deliver_retry(self) -> None:
        compilation = _compile(AgentSpec(name="main", hooks=["-deliver_retry"]))
        assert "deliver_retry" not in _hooks_of(compilation, "main")
        assert "length_guard" in _hooks_of(compilation, "main")

    def test_minus_veto_removes_length_guard(self) -> None:
        compilation = _compile(AgentSpec(name="main", hooks=["-length_guard"]))
        assert "length_guard" not in _hooks_of(compilation, "main")

    def test_minus_veto_removes_native_env(self) -> None:
        compilation = _compile(AgentSpec(name="main", hooks=["-native_env"]))
        assert "native_env" not in _hooks_of(compilation, "main")

    def test_declared_plus_name_dedups_to_one_entry(self) -> None:
        compilation = _compile(AgentSpec(name="main", hooks=["+deliver_retry"]))
        assert _hooks_of(compilation, "main").count("deliver_retry") == 1

    def test_declared_entries_append_after_the_base(self) -> None:
        compilation = _compile(AgentSpec(name="main", hooks=["+user_notice_cleanup"]))
        assert _hooks_of(compilation, "main") == [
            "deliver_retry",
            "length_guard",
            "native_env",
            "loop_detection",
            "user_notice_cleanup",
        ]

    def test_veto_coexists_with_capability_contributions(self) -> None:
        compilation = _compile(
            AgentSpec(name="main", hooks=["-length_guard"], capabilities={"todo": {}})
        )
        hooks = _hooks_of(compilation, "main")
        assert "length_guard" not in hooks
        assert "todo_continuation" in hooks  # the todo capability still contributes


# ─── (3) Provenance: every hook entry is sourced (SPEC §14.8) ───────────────


class TestHookProvenance:
    def test_position_default_origin(self) -> None:
        compilation = _compile(AgentSpec(name="main"))
        row = _hook_row(compilation, "main", "deliver_retry")
        assert row is not None
        assert row.origin is HookOrigin.POSITION_DEFAULT
        assert row.capability is None

    def test_declared_origin(self) -> None:
        compilation = _compile(AgentSpec(name="main", hooks=["+user_notice_cleanup"]))
        row = _hook_row(compilation, "main", "user_notice_cleanup")
        assert row is not None
        assert row.origin is HookOrigin.LOCAL_HOOKS

    def test_capability_origin(self) -> None:
        compilation = _compile(AgentSpec(name="main", capabilities={"todo": {}}))
        row = _hook_row(compilation, "main", "todo_continuation")
        assert row is not None
        assert row.origin is HookOrigin.CAPABILITY_DERIVED
        assert row.capability == "todo"

    def test_hooks_field_row_layer(self) -> None:
        declared = _compile(AgentSpec(name="main", hooks=["+user_notice_cleanup"]))
        assert _field_row(declared, "main", "hooks").layer is ProvenanceLayer.LOCAL
        undeclared = _compile(AgentSpec(name="main"))
        assert _field_row(undeclared, "main", "hooks").layer is ProvenanceLayer.FRAMEWORK

    def test_every_roster_entry_is_sourced(self) -> None:
        """Zero-unsourced (SPEC §14.8): each roster hook has exactly one
        provenance row and vice versa, and every roster TOOL is sourced
        (provenance may carry extra O3 replacement-audit rows — the
        replaced default's record — but never a roster entry without a
        source) — on a mixed declaration (position defaults + capability
        contributions + declared entries)."""
        compilation = _compile(
            AgentSpec(name="main", capabilities={"todo": {}}, hooks=["+user_notice_cleanup"]),
            AgentSpec(name="sub", parent="main", capabilities={"todo": {}}),
        )
        for agent in compilation.agents:
            roster = agent.spec.hooks
            sourced = [row.hook for row in agent.provenance.hooks]
            assert sorted(roster) == sorted(sourced)
            assert set(agent.spec.tools) <= {entry.tool for entry in agent.provenance.tools}


# ─── (4) Factories: registry resolution + chain-derived construction ────────


def _chain(
    registry: ComponentRegistry,
    *,
    spec: Any,
    pool_assembly_ctx: PoolAssemblyContext | None = None,
    session_tree_manager: Any = None,
) -> Any:
    component_ctx = resolution_context(
        registry,
        _workspace_ctx(),
        PoolRuntimeDeps(
            pool_assembly_ctx=pool_assembly_ctx,
            session_tree_manager=session_tree_manager,
        ),
    )
    return agent_context_chain(component_ctx, spec=spec)


def _pool_assembly(
    pool_spec: PoolSpec,
    *,
    peer_links: tuple[Any, ...] = (),
    control_origin: str = "http://127.0.0.1:21800",
) -> PoolAssemblyContext:
    return PoolAssemblyContext(
        pool_name=pool_spec.name,
        pool_spec=pool_spec,
        project_dir=Path("/tmp/bot"),
        data_dir=Path("/tmp/bot/.modex"),
        broker=MagicMock(),
        inbox_server=MagicMock(),
        agent_bus=MagicMock(),
        output_adapter=MagicMock(),
        safety=MagicMock(),
        retention=MagicMock(),
        registry=MagicMock(),
        peer_links=peer_links,
        control_origin=control_origin,
    )


class TestFactoryRegistration:
    def test_length_guard_is_registered_in_default_plugin(self) -> None:
        registry = _registry()
        factory = registry.resolve(ComponentSlot.HOOK, "length_guard")
        assert factory is LengthGuardHookFactory

    def test_deliver_retry_factory_is_chain_form(self) -> None:
        registry = _registry()
        factory = registry.resolve(ComponentSlot.HOOK, "deliver_retry")
        assert isinstance(factory, DeliverRetryHookFactory)


class TestDeliverRetryFactory:
    async def test_tree_derived_from_chain(self) -> None:
        from modex_agent.plugins.defaults.hooks import DeliverRetryHookConfig

        tree = MagicMock()
        compilation = _compile(AgentSpec(name="main"))
        spec = next(a for a in compilation.agents if a.provenance.agent == "main").spec
        ctx = _chain(_registry(), spec=spec, session_tree_manager=tree)
        hook = await DeliverRetryHookFactory().create(DeliverRetryHookConfig(), ctx)
        assert isinstance(hook, DeliverRetryHook)
        assert hook._tree is tree

    def test_priority_is_default_zero(self) -> None:
        assert DeliverRetryHookFactory.priority == 0


class TestLengthGuardFactory:
    async def test_creates_tree_agnostic_hook(self) -> None:
        compilation = _compile(AgentSpec(name="main"))
        spec = next(a for a in compilation.agents if a.provenance.agent == "main").spec
        ctx = _chain(_registry(), spec=spec)
        hook = await LengthGuardHookFactory.create(LengthGuardHookFactory.config_model(), ctx)
        assert isinstance(hook, LengthGuardHook)

    def test_priority_is_default_zero(self) -> None:
        assert LengthGuardHookFactory.priority == 0


class TestNativeEnvFactory:
    """``native_env`` derives its ``ExternalEnvSpec`` from the context chain
    (the retired injection sites passed the same values from the same
    sources): pool facts for pooled agents, workspace facts for poolless
    single-agent assembly."""

    def _spec_of(self, compilation: ScopeCompilation, agent: str) -> Any:
        return next(a for a in compilation.agents if a.provenance.agent == agent).spec

    async def test_main_spec_derives_pool_facts(self) -> None:
        from modex_agent.multi_agent.communication.peer_resolution import PeerLink

        pool_spec = _pool_spec(AgentSpec(name="main"), AgentSpec(name="sub", parent="main"))
        peer = PeerLink(peer_pool="other", peer_agent="other-main", peer_description="peer")
        compilation = _compile(*pool_spec.agents)
        spec = self._spec_of(compilation, "main")
        ctx = _chain(
            _registry(),
            spec=spec,
            pool_assembly_ctx=_pool_assembly(pool_spec, peer_links=(peer,)),
        )
        hook = await NativeEnvInjectionHookFactory().create(NativeEnvInjectionHookConfig(), ctx)
        template: ExternalEnvSpec = hook._template
        assert template.agent_name == "main"
        assert template.comm_kind is AgentCommKind.NORMAL
        assert template.control_origin == "http://127.0.0.1:21800"
        assert template.agent_pool_map == {"main": "p", "sub": "p", "other-main": "other"}
        assert template.targets == [("sub", "sub subagent"), ("other-main", "peer")]
        assert template.workspace_root == Path("/tmp/bot")
        assert template.inbox_root == Path("/tmp/bot/.modex/inbox")

    async def test_sub_spec_derives_declared_parent(self) -> None:
        pool_spec = _pool_spec(AgentSpec(name="main"), AgentSpec(name="sub", parent="main"))
        compilation = _compile(*pool_spec.agents)
        spec = self._spec_of(compilation, "sub")
        ctx = _chain(
            _registry(),
            spec=spec,
            pool_assembly_ctx=_pool_assembly(pool_spec),
        )
        hook = await NativeEnvInjectionHookFactory().create(NativeEnvInjectionHookConfig(), ctx)
        template: ExternalEnvSpec = hook._template
        assert template.agent_name == "sub"
        assert template.comm_kind is AgentCommKind.SUBAGENT
        assert template.agent_pool_map == {"sub": "p", "main": "p"}
        assert template.targets == [("main", "")]

    async def test_poolless_spec_derives_workspace_facts(self) -> None:
        """Poolless single-agent assembly (no ``pool_assembly_ctx`` on the
        chain) gets a minimal self-only spec — nothing to route to."""
        compilation = _compile(AgentSpec(name="react"))
        spec = self._spec_of(compilation, "react")
        ctx = _chain(_registry(), spec=spec)
        hook = await NativeEnvInjectionHookFactory().create(NativeEnvInjectionHookConfig(), ctx)
        template: ExternalEnvSpec = hook._template
        assert template.agent_name == "react"
        assert template.comm_kind is AgentCommKind.NORMAL
        assert template.targets == []
        assert template.workspace_root == Path("/tmp/test_position_defaults_ws")

    async def test_explicit_config_template_wins(self) -> None:
        """A declared ``hook_configs:`` template overrides the derivation —
        the config face stays authoritative for explicit configuration."""
        compilation = _compile(AgentSpec(name="main"))
        spec = self._spec_of(compilation, "main")
        ctx = _chain(_registry(), spec=spec)
        explicit = ExternalEnvSpec(
            workspace_root=Path("/tmp/explicit"),
            inbox_root=Path("/tmp/explicit/.modex/inbox"),
            workdir=Path("/tmp/explicit"),
            session_id="__pending__.main",
            agent_name="main",
            provider_session_id="",
            agent_pool_map={"main": "p"},
            targets=[],
            modexctl_bin_dir=Path("/tmp/bin"),
        )
        config = NativeEnvInjectionHookConfig(env_spec_template=explicit)
        hook = await NativeEnvInjectionHookFactory().create(config, ctx)
        assert hook._template is explicit


# ─── (5) The retired glue is gone ───────────────────────────────────────────


class TestGlueDeaths:
    def test_register_tree_aware_hooks_module_is_gone(self) -> None:
        import importlib.util

        assert importlib.util.find_spec("modex_agent.hook.wiring") is None

    def test_no_assembly_path_references_the_symbol(self) -> None:
        """The W6 injection sites died: no production assembly code calls
        the retired convergence function or injects the four hooks
        unconditionally (G-CAP2 anchors the same invariant mechanically)."""
        import subprocess
        import sys

        result = subprocess.run(  # noqa: S603
            [
                sys.executable,
                "-c",
                (
                    "import subprocess, sys; "
                    "hits = subprocess.run(['git', 'grep', '-l', "
                    "'register_tree_aware_hooks', '--', 'src/', 'examples/'], "
                    "capture_output=True, text=True).stdout; "
                    "sys.exit(1 if hits else 0)"
                ),
            ],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stdout
