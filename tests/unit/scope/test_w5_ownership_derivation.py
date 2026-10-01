"""W5 completion red anchors — ownership is a compile-visible contract.

``RuntimeOwnership`` lives in ``scope.runtime_ownership`` (the declaration
vocabulary layer) and is fully derived:

* the EXECUTION_STRATEGY slot's factory ``probe()`` surfaces a
  ``StrategyManifest`` — the synchronous ownership face the scope
  compiler reads without importing ``multi_agent``;
* an unregistered plugin strategy name fails at COMPILE (the V13
  precedent: one cycle earlier than the assembly-time slot resolution);
* V12 (EXTERNAL_CAPABILITIES) and the position-default hook face key on
  ``ownership.owns_context`` instead of the ``provider_kind``
  discriminator — the V12 rule id and message text stay byte-identical
  (pinned by ``test_w5_v12_semantics.py``);
* ``needs_memory=False`` / ``supports_approval=False`` are enforced:
  a memory-surface or root-approval request on such a strategy is a
  compile-time error with repair guidance;
* probing never changes compiled output (spec-hash byte-stability).

The imports of the factory face (``StrategyComponentFactory``) are inside
the tests that need them so red-anchor runs observe the behavior failures
of the compile/validate rules rather than a module-level collection
error.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from modex_agent.approval.config import ApprovalConfig
from modex_agent.core.agent import ExecutionStrategyKind, ProviderKind
from modex_agent.scope.compiler import compile_scope
from modex_agent.scope.component_registry import (
    ComponentNotFoundError,
    ComponentRegistry,
)
from modex_agent.scope.components import ComponentSlot
from modex_agent.scope.runtime_ownership import RuntimeOwnership, StrategyManifest
from modex_agent.scope.spec import (
    AgentSpec,
    MemoryDeclaration,
    PoolSpec,
    ScopeKind,
    ScopeSpec,
)
from modex_agent.scope.validator import RuleId, validate_declaration
from modex_agent.workspace.context import WorkspaceContext
from modex_agent.workspace.paths import WorkspacePaths

OWNING_NAME = "owning_loop"

OWNING_OWNERSHIP = RuntimeOwnership(
    needs_llm_provider=False,
    needs_main_agent_tools=False,
    needs_memory=False,
    supports_approval=False,
    supports_subagents=False,
    owns_context=True,
)


def _workspace_ctx(tmp_path: Path) -> WorkspaceContext:
    return WorkspaceContext(
        target=tmp_path,
        paths=WorkspacePaths(root=tmp_path / ".modex"),
        is_home=False,
    )


def _pool_spec(agent: AgentSpec) -> ScopeSpec:
    return ScopeSpec(kind=ScopeKind.POOL, pool=PoolSpec(name="p", agents=[agent]))


def _owning_strategy():  # type: ignore[no-untyped-def]
    from modex_agent.multi_agent.execution_strategy import (
        ExecutionStrategy,
        PoolAssemblyContext,
        StrategyAssembly,
    )

    class _OwningStrategy(ExecutionStrategy):
        """Third loop shape owning its whole runtime — the derivation probe."""

        @property
        def name(self) -> str:
            return OWNING_NAME

        @property
        def ownership(self) -> RuntimeOwnership:
            return OWNING_OWNERSHIP

        async def assemble_main(self, ctx: PoolAssemblyContext) -> StrategyAssembly:
            raise NotImplementedError

    return _OwningStrategy()


def _registry_with_owning_strategy() -> ComponentRegistry:
    """A registry whose EXECUTION_STRATEGY slot carries the owning loop."""
    from modex_agent.multi_agent.execution_strategy import StrategyComponentFactory

    registry = ComponentRegistry()
    registry.register(
        ComponentSlot.EXECUTION_STRATEGY,
        OWNING_NAME,
        StrategyComponentFactory(_owning_strategy()),
    )
    return registry


# ── T-A: unknown plugin strategy name fails at compile (V13 precedent) ─────


def test_unknown_strategy_name_fails_at_compile(tmp_path: Path) -> None:
    """A plugin strategy name that is not registered in the
    EXECUTION_STRATEGY slot is a COMPILE-time boot error — one cycle
    earlier than the assembly-time slot resolution."""
    spec = _pool_spec(AgentSpec(name="root", execution_strategy="ghost_loop"))

    with pytest.raises(ComponentNotFoundError, match="ghost_loop"):
        compile_scope(
            spec, workspace_ctx=_workspace_ctx(tmp_path), registry=ComponentRegistry()
        )


def test_plugin_strategy_name_without_registry_fails_loud(tmp_path: Path) -> None:
    """registry=None supports only the bundled enum shapes — a plugin
    name without a registry is a loud error (the compile_scope
    registry=None guard, same shape as the capabilities guard)."""
    spec = _pool_spec(AgentSpec(name="root", execution_strategy="ghost_loop"))

    with pytest.raises(ValueError, match="ghost_loop"):
        compile_scope(spec, workspace_ctx=_workspace_ctx(tmp_path))


# ── T-B/T-C: needs_memory / supports_approval are enforced at compile ──────


def test_memory_request_on_memoryless_strategy_fails_at_compile(tmp_path: Path) -> None:
    """A memory-surface request (memory: overrides) against a strategy
    declaring needs_memory=False is a compile error with repair
    guidance."""
    spec = _pool_spec(
        AgentSpec(
            name="root",
            execution_strategy=ExecutionStrategyKind.EXTERNAL,
            provider_kind=ProviderKind.OPENCODE,
            memory=MemoryDeclaration(archive_enabled=True),
        )
    )

    with pytest.raises(ValueError, match="needs_memory=False"):
        compile_scope(spec, workspace_ctx=_workspace_ctx(tmp_path))


def test_memory_system_request_on_memoryless_strategy_fails_at_compile(
    tmp_path: Path,
) -> None:
    spec = _pool_spec(
        AgentSpec(
            name="root",
            execution_strategy=ExecutionStrategyKind.EXTERNAL,
            provider_kind=ProviderKind.OPENCODE,
            memory_system="custom",
        )
    )

    with pytest.raises(ValueError, match="needs_memory=False"):
        compile_scope(spec, workspace_ctx=_workspace_ctx(tmp_path))


def test_root_approval_on_approvalless_strategy_is_accepted(tmp_path: Path) -> None:
    """A root approval block against a strategy declaring
    supports_approval=False is ACCEPTED by the compiler (the friendly-form
    contract: the bill reports approval NOT applicable, MED4) — only the
    memory-surface mismatch is a compile error."""
    spec = _pool_spec(
        AgentSpec(
            name="root",
            execution_strategy=ExecutionStrategyKind.EXTERNAL,
            provider_kind=ProviderKind.OPENCODE,
            approval=ApprovalConfig(enabled=True),
        )
    )

    compilation = compile_scope(spec, workspace_ctx=_workspace_ctx(tmp_path))
    agent = compilation.agents[0]
    assert agent.defaults.approval_eligible is True  # position default kept


# ── T-D: V12 derives from owns_context (probe face, custom strategy) ───────


def test_v12_fires_for_probe_resolved_owns_context_strategy() -> None:
    """V12 fires for a THIRD strategy whose probed manifest declares
    owns_context — byte-identical rule id and message text."""
    registry = _registry_with_owning_strategy()
    spec = _pool_spec(
        AgentSpec(
            name="owning",
            execution_strategy=OWNING_NAME,
            capabilities={"todo": {}},
        )
    )

    issues = validate_declaration(spec, registry=registry)

    assert len(issues) == 1
    assert issues[0].rule is RuleId.EXTERNAL_CAPABILITIES
    assert issues[0].node == "owning"
    assert issues[0].message == (
        "pool 'p': external agent 'owning' declares capabilities — explicit "
        "capability declarations are invalid for external agents because "
        "external agents take no native component face; remove the "
        "capabilities block (V12)"
    )


# ── T-E: position-default hooks derive from owns_context ───────────────────


def test_position_default_hooks_excluded_for_owns_context_strategy(tmp_path: Path) -> None:
    """A strategy declaring owns_context=True takes no native hook face:
    the SPEC §3.2 position-default rows stay out of its compiled roster
    (declaration-only), while a react root keeps them."""
    registry = _registry_with_owning_strategy()
    owning_spec = _pool_spec(
        AgentSpec(
            name="owning",
            execution_strategy=OWNING_NAME,
            hooks=["+session_title"],
        )
    )

    owning = compile_scope(
        owning_spec, workspace_ctx=_workspace_ctx(tmp_path), registry=registry
    ).agents[0]

    assert owning.spec.hooks == ["session_title"]

    react_spec = _pool_spec(AgentSpec(name="root", hooks=["+session_title"]))
    react = compile_scope(react_spec, workspace_ctx=_workspace_ctx(tmp_path)).agents[0]

    assert "deliver_retry" in react.spec.hooks
    assert "session_title" in react.spec.hooks


def test_registry_registration_wins_over_bundled_shape(tmp_path: Path) -> None:
    """Resolution order: the registry's slot registration outranks the
    bundled enum shape — an override of ``react`` declaring
    owns_context=True changes compiled hook output."""
    from modex_agent.multi_agent.execution_strategy import StrategyComponentFactory

    registry = ComponentRegistry()
    registry.register(
        ComponentSlot.EXECUTION_STRATEGY,
        "react",
        StrategyComponentFactory(_owning_strategy_with_name("react")),
    )
    spec = _pool_spec(AgentSpec(name="root", hooks=["+session_title"]))

    compiled = compile_scope(
        spec, workspace_ctx=_workspace_ctx(tmp_path), registry=registry
    ).agents[0]

    assert compiled.spec.hooks == ["session_title"]


def _owning_strategy_with_name(name: str):  # type: ignore[no-untyped-def]
    from modex_agent.multi_agent.execution_strategy import (
        ExecutionStrategy,
        PoolAssemblyContext,
        StrategyAssembly,
    )

    class _NamedStrategy(ExecutionStrategy):
        @property
        def name(self) -> str:
            return name

        @property
        def ownership(self) -> RuntimeOwnership:
            return RuntimeOwnership(owns_context=True)

        async def assemble_main(self, ctx: PoolAssemblyContext) -> StrategyAssembly:
            raise NotImplementedError

    return _NamedStrategy()


# ── T-F: the probe face — EXECUTION_STRATEGY factories surface manifests ───


async def test_bundled_strategy_factories_probe_manifests() -> None:
    from modex_agent.plugins.defaults import DefaultPlugin
    from modex_agent.plugins.loader import ComponentRegistryLoader, PluginDiscoveryConfig

    registry = ComponentRegistry()
    await ComponentRegistryLoader.load(
        registry,
        PluginDiscoveryConfig(bundled_factories=(DefaultPlugin(),), project_plugin_paths=()),
    )

    for name, ownership in (
        ("react", RuntimeOwnership()),
        (
            "external",
            RuntimeOwnership(
                needs_llm_provider=False,
                needs_main_agent_tools=False,
                needs_memory=False,
                supports_approval=False,
                supports_subagents=False,
                owns_context=True,
            ),
        ),
    ):
        factory = registry.resolve(ComponentSlot.EXECUTION_STRATEGY, name)
        manifest = factory.probe()
        assert isinstance(manifest, StrategyManifest), (
            f"{name} factory probe must surface a StrategyManifest, got {type(manifest).__name__}"
        )
        assert manifest == StrategyManifest(name=name, ownership=ownership)


# ── T-G: probing never changes compiled output (spec-hash byte-stability) ──


def test_registry_probing_keeps_spec_hash_identical(tmp_path: Path) -> None:
    """Compiling with the strategy slot REGISTERED (probe taken) vs
    ``registry=None`` (bundled fallback) yields the identical spec hash —
    the probe is derivation-only, never a compiled-output input. The
    registry carries ONLY the strategy slot, so the capability protocol
    stays out of the comparison."""
    from modex_agent.multi_agent.execution_strategy import StrategyComponentFactory
    from modex_agent.plugins.assembly.strategies import ReactExecutionStrategy
    from modex_agent.scope.seam import spec_hash

    registry = ComponentRegistry()
    registry.register(
        ComponentSlot.EXECUTION_STRATEGY,
        "react",
        StrategyComponentFactory(ReactExecutionStrategy()),
    )
    spec = _pool_spec(AgentSpec(name="root", hooks=["+session_title"], tools=["+bash"]))
    ctx = _workspace_ctx(tmp_path)

    without_registry = compile_scope(spec, workspace_ctx=ctx)
    with_registry = compile_scope(spec, workspace_ctx=ctx, registry=registry)

    assert spec_hash(without_registry) == spec_hash(with_registry)
