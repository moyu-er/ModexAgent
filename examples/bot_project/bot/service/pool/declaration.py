"""Scope-declaration boot (tickets 07/10/11 — every pool).

Boot sequence (SPEC §3/§7): load ``config/scopes/bot.yml`` ONCE per
workspace build → validate the FULL declaration (phase 1: V1-V5/V7/V10
graph cross-check/V11; phase 2: V6/V9 over the compiled effective
configs) → compile (ticket 06). EVERY declared pool consumes the products
through ``create_pool`` (ticket 11 — the dual-road pivot set is gone).

Boot failure is fatal: any validation issue raises :class:`ScopeBootError`
carrying ALL issues — a silent skip would resurface later as a runtime
"no template for X".

Migration boundary (Oracle#3): this module performs ZERO per-agent
component construction. No tool, hook, or communication-tool construction
symbol may be imported here (import-level architecture test). Supplied infra
stays with the ``create_pool`` branch: the communication router service +
per-agent store (SPEC §3.3 pool-layer facilities), LLM slot pre-resolution
(orchestrator-side registry dispatch), and todo/terminal infra (FW-side
since 06). MCP tool loading and the interceptor/command roster resolution
moved to the FW pipeline in ticket 10; memory/experience/notice wiring
migrated to position-derived defaults + roster-referenced HOOK-slot
components in ticket 09; peer bus/tree references resolve through the FW
resolution service at workspace materialize time (ticket 13).
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from bot.service.pool.declaration_graphs import extract_graph_agent_refs
from modex_agent.ioc.configs.app import AppConfig
from modex_agent.ioc.configs.observability import ObservabilityConfig, TraceBackend
from modex_agent.multi_agent.communication.peer_resolution import (
    PeerLink,
    peer_links_from_declaration,
)
from modex_agent.multi_agent.template import AgentTemplate
from modex_agent.multi_agent.template_registry import AgentTemplateRegistry
from modex_agent.persistence.config import PersistenceConfig
from modex_agent.plugins.abc import AgentType, ComponentSlot
from modex_agent.plugins.defaults.capabilities.tracing import TracingCapabilityConfig
from modex_agent.plugins.registry import ComponentRegistry
from modex_agent.scope.compiler import CompiledAgent, ScopeCompilation, compile_scope
from modex_agent.scope.loader import load_scope_declaration
from modex_agent.scope.profile import STANDARD_PROFILES
from modex_agent.scope.spec import AgentSpec, PoolSpec, ScopeKind, ScopeSpec
from modex_agent.scope.validator import (
    ScopeValidationIssue,
    validate_declaration,
    validate_effective_configs,
)
from modex_agent.workspace.context import WorkspaceContext
from modex_agent.workspace.paths import WorkspacePaths

logger = logging.getLogger(__name__)

_MAIN_AGENT_TYPES: Final[frozenset[AgentType]] = frozenset(
    {AgentType.native_main, AgentType.external_main}
)


class ScopeBootError(ValueError):
    """Startup failure in the scope-declaration boot sequence."""

    def __init__(self, issues: Sequence[ScopeValidationIssue], *, phase: str) -> None:
        self.issues = list(issues)
        rendered = "; ".join(
            f"{issue.rule.value} [{issue.node}]: {issue.message}" for issue in self.issues
        )
        super().__init__(
            f"scope declaration failed {phase} validation ({len(self.issues)} issue(s)): {rendered}"
        )


@dataclass(frozen=True)
class ScopeBoot:
    """Boot products: the loaded declaration tree + its compilation."""

    spec: ScopeSpec
    compilation: ScopeCompilation


@dataclass(frozen=True)
class DeclaredPoolBuild:
    """The declaration-road products one pool consumes at ``create_pool``.

    ``root`` drives the main agent's assembly spec; ``subagents`` seed the
    template registry (lazy materialization reads ``compiled_spec``);
    ``root_children`` are the root's DIRECT children (SPEC §3.2 — the root's
    per-agent communication target store lists them, never grandchildren);
    ``template_registry`` is pre-seeded from the compilation.
    ``pool`` is the declared pool (peers + agent declarations — the single
    pool face create_pool and the strategies read, replacing the legacy
    PoolSpec); ``peer_links`` are this pool's declared links (the env-spec
    agent-pool map reads the peer roots' declared names).
    """

    root: CompiledAgent
    subagents: tuple[CompiledAgent, ...]
    root_children: tuple[CompiledAgent, ...]
    template_registry: AgentTemplateRegistry
    pool: PoolSpec
    peer_links: tuple[PeerLink, ...]


def boot_scope_declaration(
    *,
    declaration_path: Path,
    project_dir: Path,
    data_dir: Path,
    graphs_dirs: Sequence[Path],
    default_llm_provider: str,
    registry: ComponentRegistry | None = None,
    observability: ObservabilityConfig | None = None,
) -> ScopeBoot:
    """Load ``declaration_path`` → validate (V1-V11, incl. the V10 graph
    cross-check) → compile.

    Args:
        declaration_path: the scope declaration file (production:
            ``config/scopes/bot.yml``; tests may pass fixture paths).
        project_dir / data_dir: the workspace-boot pair threaded into every
            compiled ``AssemblySpec.workspace_ctx`` — the same projection
            ``create_pool`` performs on the legacy road.
        graphs_dirs: candidate graph-spec directories in preference order
            (workspace-local first, global template second — mirroring
            the resources.py copytree resolution). The first existing
            directory supplies the V10 references.
        default_llm_provider: the BIZ default LLM provider component name.
        registry: the boot ComponentRegistry, threaded into
            ``compile_scope`` for the capability compile protocol (C0/C1/C2).
            ``None`` disables capability resolution — a declaration with
            capabilities then fails loudly at compile.
        observability: the deployment's global observability config —
            drives the tracing fallback (see
            :func:`boot_scope_spec`); ``None`` → the FW defaults.

    Raises:
        ScopeBootError: any phase-1/phase-2 validation issue (all issues
        are carried in the message), or a malformed agent-node
        reference in a graph spec.
    """
    spec = load_scope_declaration(declaration_path)
    return boot_scope_spec(
        spec,
        project_dir=project_dir,
        data_dir=data_dir,
        graphs_dirs=graphs_dirs,
        default_llm_provider=default_llm_provider,
        registry=registry,
        observability=observability,
    )


def boot_scope_spec(
    spec: ScopeSpec,
    *,
    project_dir: Path,
    data_dir: Path,
    graphs_dirs: Sequence[Path],
    default_llm_provider: str,
    registry: ComponentRegistry | None = None,
    observability: ObservabilityConfig | None = None,
) -> ScopeBoot:
    """Validate + compile an ALREADY-LOADED declaration tree.

    Same machine as :func:`boot_scope_declaration`, entered with a spec the
    caller built or adjusted in memory (e.g. eval's approval-off rewrite)
    instead of read from disk. Validation sees the ADJUSTED tree — callers
    must keep the tree shape-valid (frozen ``model_copy`` updates only).

    ``observability`` (the deployment's global observability config)
    drives the tracing fallback (:func:`apply_tracing_fallback`) BEFORE
    validation: global-config tracing stays effective on deployments
    whose declarations predate the ``capabilities: {tracing: {…}}`` face
    (zero behavior change on the shipped tree). ``None`` → the FW
    defaults (backend=FILE, tier=STANDARD — the retired factory fallback
    ``ObservabilityConfig()``), so tracing stays on exactly as the
    retired code-wired path kept it for config-less harnesses.
    """
    spec = apply_tracing_fallback(spec, observability or ObservabilityConfig(), registry)
    graph_refs = extract_graph_agent_refs(graphs_dirs)
    issues = validate_declaration(
        spec,
        profiles=STANDARD_PROFILES.declarations(),
        graph_agent_refs=graph_refs,
    )
    if issues:
        raise ScopeBootError(issues, phase="phase-1 (declaration shape)")
    compilation = compile_scope(
        spec,
        workspace_ctx=WorkspaceContext(
            target=project_dir,
            paths=WorkspacePaths(root=data_dir),
            is_home=False,
        ),
        default_llm_provider=default_llm_provider,
        registry=registry,
    )
    issues = validate_effective_configs(spec, [agent.effective for agent in compilation.agents])
    if issues:
        raise ScopeBootError(issues, phase="phase-2 (effective values)")
    _log_replacements(compilation)
    return ScopeBoot(spec=spec, compilation=compilation)


def apply_tracing_fallback(
    spec: ScopeSpec,
    observability: ObservabilityConfig,
    registry: ComponentRegistry | None = None,
) -> ScopeSpec:
    """Inject the ``tracing`` capability override from the GLOBAL
    observability config — the BIZ fallback (ADR-0047).

    The shipped declarations do not carry ``capabilities: {tracing: …}``;
    global-config tracing keeps working through this pre-compile spec
    mutation: when ``trace_backend != off``, every NATIVE agent in the
    tree gains ``capabilities: {"tracing": <global config subset>}``
    unless it already declares ``tracing`` itself (a declaration always
    wins — the fallback never overwrites). External agents never receive
    the capability (C0/V12 structural exclusion). ``trace_backend=off``
    injects nothing — tracing dark. A ``None`` registry (hand-built
    harness boots) injects nothing either: the capability protocol
    resolves through the registry, so a registry-less compile cannot
    carry injected capabilities (T17's hermetic-strip discipline).

    This is the template for future "global config feature → capability"
    migrations: the deployment-level default becomes a compile-input
    override, keeping the capability protocol the single enablement
    authority (the retired code-wired hook injection died with the
    tracing capability convergence).
    """
    if registry is None or observability.trace_backend is TraceBackend.OFF:
        return spec
    if "tracing" not in registry.names(ComponentSlot.CAPABILITY):
        # A registry without the FW DefaultPlugin (hand-rolled harness
        # registries) cannot resolve the capability — injecting it would
        # V13-fail the boot. Production registries always carry it.
        return spec
    override = TracingCapabilityConfig.from_observability(observability).model_dump()
    pools: list[PoolSpec] = (
        [spec.pool]
        if spec.pool is not None
        else list(spec.workspace.pools if spec.workspace else [])
    )
    changed = False
    mutated_pools: list[PoolSpec] = []
    for pool in pools:
        mutated_agents: list[AgentSpec] = []
        pool_changed = False
        for agent in pool.agents:
            if agent.provider_kind is not None:  # external — structurally excluded
                mutated_agents.append(agent)
                continue
            if agent.capabilities is not None and "tracing" in agent.capabilities:
                mutated_agents.append(agent)  # declaration wins
                continue
            capabilities = {**(agent.capabilities or {}), "tracing": override}
            mutated_agents.append(agent.model_copy(update={"capabilities": capabilities}))
            pool_changed = True
        mutated_pools.append(
            pool.model_copy(update={"agents": mutated_agents}) if pool_changed else pool
        )
        changed = changed or pool_changed
    if not changed:
        return spec
    if spec.pool is not None:
        return spec.model_copy(update={"pool": mutated_pools[0]})
    assert spec.workspace is not None
    return spec.model_copy(
        update={"workspace": spec.workspace.model_copy(update={"pools": mutated_pools})}
    )


def declared_pool_build(boot: ScopeBoot, pool_name: str) -> DeclaredPoolBuild:
    """Partition one pool's compiled agents and seed its templates.

    The root is the single compiled agent whose ``agent_type`` is a main
    type; every other agent is a lazy subagent seeded into the template
    registry with its compiled spec.
    """
    root = declared_pool_root(boot, pool_name)
    if root is None:
        raise ValueError(
            f"scope declaration declares no pool {pool_name!r} — cannot boot "
            "it from the declaration road"
        )
    pool_agents = [agent for agent in boot.compilation.agents if agent.provenance.pool == pool_name]
    subagents = tuple(agent for agent in pool_agents if agent is not root)
    scope_pool = _pool_of(boot.spec, pool_name)
    parents = _declared_parents(scope_pool)
    # Ticket 09: template memory is position-derived — ``None`` lets
    # ``assemble_native_agent``'s ``_merge_memory`` fall back to the
    # session-only preset with the compiled spec's memory overrides
    # applied (identical product, one derivation point).
    # Ticket 12: a template carries its DIRECT children (SPEC §3.2) so the
    # mid-level agent's per-agent target store lists exactly them —
    # grandchildren belong to the child's own ``task``, never the parent's.
    declared_agents = {agent.name: agent for agent in scope_pool.agents}
    templates = {
        agent.provenance.agent: AgentTemplate(
            spec=declared_agents[agent.provenance.agent],
            toolset_profile=agent.defaults.toolset_profile,
            memory=None,
            compiled_spec=agent.spec,
            children=tuple(
                declared_agents[child.provenance.agent]
                for child in subagents
                if parents[child.provenance.agent] == agent.provenance.agent
            ),
        )
        for agent in subagents
    }
    return DeclaredPoolBuild(
        root=root,
        subagents=subagents,
        root_children=tuple(
            child for child in subagents if parents[child.provenance.agent] == root.provenance.agent
        ),
        template_registry=AgentTemplateRegistry(seeded={pool_name: templates}),
        pool=scope_pool,
        peer_links=peer_links_from_declaration(boot.spec).get(pool_name, ()),
    )


def declared_pool_root(boot: ScopeBoot, pool_name: str) -> CompiledAgent | None:
    """The compiled root agent of a declaration-hosted pool, else ``None``.

    Ticket 14's single assembly-deps road: every hosted pool's deps derive
    from the compiled root's position defaults regardless of its
    create_pool road (the 02 equivalence makes both sources identical).
    """
    pool_agents = [agent for agent in boot.compilation.agents if agent.provenance.pool == pool_name]
    if not pool_agents:
        return None
    roots = [agent for agent in pool_agents if agent.spec.agent_type in _MAIN_AGENT_TYPES]
    if len(roots) != 1:
        raise ValueError(
            f"pool {pool_name!r}: expected exactly one main agent in the "
            f"declaration, found {len(roots)}"
        )
    return roots[0]


# ─── Workspace resource selection (ticket 14) ──────────────────────────────


def load_scope_declaration_opt(path: Path) -> ScopeSpec | None:
    """Load the scope declaration, ``None`` when the file is absent.

    Malformed declarations propagate their load errors — the boot fails
    loudly rather than silently falling back to the legacy roster road.
    """
    if not path.exists():
        return None
    return load_scope_declaration(path)


def workspace_layer_present(spec: ScopeSpec | None) -> bool:
    """The single stack-shape mechanism (ticket 14, N15).

    A workspace-layer declaration boots the multi-live stack; its absence
    (pool-as-root or no declaration) boots the single-workspace
    deployment. The ``workspace.enabled`` config flag is dead — the
    declaration form IS the switch.
    """
    return spec is not None and spec.kind is ScopeKind.WORKSPACE


def apply_workspace_resource_selection(
    app_config: AppConfig, scope_spec: ScopeSpec | None
) -> AppConfig:
    """Resolve the workspace declaration's resource-selection overrides onto
    the service config view (SPEC §3.1 — 继承父层 + 声明差异).

    ``persistence.backend`` and ``paths.data_dir_name`` declared at the
    workspace layer override the service-level values; absent fields (and
    pool-as-root / absent declarations) change nothing — the domain config
    stands, keeping no-declaration deployments on today's data layout.
    Every workspace-scoped consumer reads the returned view, so the
    backend selection has one authority (``app_config.persistence.backend``)
    with the declaration as an override source resolved once at boot.
    """
    if scope_spec is None or scope_spec.workspace is None:
        return app_config
    ws = scope_spec.workspace
    updates: dict[str, PersistenceConfig | object] = {}
    if ws.persistence is not None and ws.persistence.backend is not app_config.persistence.backend:
        updates["persistence"] = PersistenceConfig(backend=ws.persistence.backend)
    if ws.paths is not None and ws.paths.data_dir_name != app_config.paths.data_dir_name:
        updates["paths"] = app_config.paths.model_copy(
            update={"data_dir_name": ws.paths.data_dir_name}
        )
    if not updates:
        return app_config
    return app_config.model_copy(update=updates)


def validate_workspace_mcp_set(
    scope_spec: ScopeSpec | None, registry_servers: Iterable[str]
) -> None:
    """Loud boot check: declared workspace MCP names must exist in the registry.

    Skipped (with a warning) when the registry is empty — a deployment with
    no MCP servers configured is degenerate, not a typo; agents' selections
    already fail soft there (the FW loader warns and continues).
    """
    if scope_spec is None or scope_spec.workspace is None:
        return
    declared = scope_spec.workspace.mcp
    if declared is None:
        return
    known = set(registry_servers)
    if not known:
        logger.warning(
            "[scope-boot] workspace %r declares MCP servers %s but the MCP "
            "registry is empty — the set is dormant until servers are "
            "configured",
            scope_spec.workspace.name,
            list(declared),
        )
        return
    missing = [name for name in declared if name not in known]
    if missing:
        from bot.config.mcp_registry import UnknownMcpServer

        raise UnknownMcpServer(
            f"workspace declaration {scope_spec.workspace.name!r} references "
            f"MCP servers not in the registry: {missing}"
        )


def workspace_mcp_prewarm_names(
    scope_spec: ScopeSpec | None, registry_names: Sequence[str]
) -> list[str]:
    """The shared-registry pre-warm set (ticket 14).

    A declared workspace MCP set scopes the pre-warm to exactly those
    servers (the workspace's shared infrastructure selection); undeclared
    workspaces pre-warm the full registry (ADR-0017 behavior). Servers
    outside the declared set still connect lazily via ``acquire``.
    """
    if scope_spec is not None and scope_spec.workspace is not None:
        declared = scope_spec.workspace.mcp
        if declared is not None:
            return list(declared)
    return list(registry_names)


# ─── Internal helpers ──────────────────────────────────────────────────────


def _pool_of(spec: ScopeSpec, pool_name: str) -> PoolSpec:
    """The declared pool spec of ``pool_name`` (declaration order)."""
    if spec.pool is not None:
        # Pool-as-root (ticket 14): the single declared pool IS the root
        # scope — it boots straight through with no workspace layer.
        if spec.pool.name != pool_name:
            raise ValueError(
                f"pool-as-root declaration declares pool {spec.pool.name!r}, not {pool_name!r}"
            )
        return spec.pool
    pools = spec.workspace.pools if spec.workspace is not None else []
    for pool in pools:
        if pool.name == pool_name:
            return pool
    raise ValueError(f"scope declaration declares no pool {pool_name!r} (workspace form)")


def _declared_parents(pool: PoolSpec) -> dict[str, str | None]:
    """Declared parent name per agent in the pool (``None`` for the root)."""
    return {agent.name: agent.parent for agent in pool.agents}


def _log_replacements(compilation: ScopeCompilation) -> None:
    """Log the O3 same-name replacement records (ACI boot accounting).

    Ticket 06's runtime half: the compiler is a pure function, so the
    ``edit ← aci`` replacement records surface here, at boot.
    """
    for agent in compilation.agents:
        for replacement in agent.provenance.replacements:
            logger.info(
                "scope: pool '%s' agent '%s': tool '%s' replaced by '%s' (capability %s)",
                agent.provenance.pool,
                agent.provenance.agent,
                replacement.default_tool,
                replacement.replacement_tool,
                replacement.capability,
            )
