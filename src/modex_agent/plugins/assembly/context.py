"""Assembly pipeline context types — pure value-object dependency carriers.

Three frozen dataclasses that define the assembly pipeline's dependency
carrier (SPEC §6.5). These types hold Python object references (not
serialized across module boundaries), so frozen dataclass is the
appropriate form per rule 11 (leaf value-object escape hatch) and
rule 12 (runtime-object container — NOT Pydantic ``BaseModel``).

Design constraints:
- Pure data carriers — no behavior methods.
- ``TYPE_CHECKING`` guards for types not defined here (ComponentRegistry)
  and for avoiding import cycles with ``multi_agent``, ``hook``,
  ``control``, and ``workspace`` packages.
- ``from __future__ import annotations`` makes all annotations strings
  at runtime, so TYPE_CHECKING imports never trigger ImportError.

Context chain (SPEC §3.3, ticket 04) — :class:`WorkspaceContext` →
:class:`PoolContext` → :class:`AgentContext`: three frozen layer
carriers that type-generalize :class:`AssemblyContext`'s three-layer
shape. The full-chain object :class:`AgentContext` joins every layer
via multiple inheritance, so a factory's ``create(ctx=...)`` parameter
declaration IS its capability boundary (the type is the readable layer
— a factory declaring ``PoolContext`` cannot type-reach workspace-layer
fields).
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from modex_agent.commands.models import CommandProcessor
    from modex_agent.control.channel import InMemoryControlChannel
    from modex_agent.core.emitter import ContentEmitter
    from modex_agent.core.provider import LLMProvider
    from modex_agent.core.session_id import SessionInfo
    from modex_agent.hook.notification import AgentNotificationService
    from modex_agent.interceptor.chain import InterceptorChain
    from modex_agent.multi_agent.execution_strategy import (
        PoolAssemblyContext,
    )
    from modex_agent.multi_agent.pool import AgentPool
    from modex_agent.multi_agent.session_tree.manager import SessionTreeManager
    from modex_agent.multi_agent.tools import CommunicationTargetStore

    # Forward references to types defined elsewhere:
    # - ComponentRegistry (``plugins/registry.py``)
    from modex_agent.plugins.assembly.spec import AssemblySpec
    from modex_agent.plugins.capability import CapabilitySupply, CapabilityWiring
    from modex_agent.plugins.registry import ComponentRegistry
    from modex_agent.scope.spec import WorkspaceSpec
    from modex_agent.tools.mcp.registry import McpConnectionRegistry
    from modex_agent.tools.terminal.managers import TerminalManagerBase
    from modex_agent.tools.terminal.persistent_bash import PersistentBashTool
    from modex_agent.tools.terminal.process_registry import ProcessRegistry
    from modex_agent.tools.workspace_scoped import WorkspaceRootProvider

    # Aliased: the per-workspace identity/paths type must not collide with
    # the assembly-time WorkspaceContext LAYER carrier defined below.
    from modex_agent.workspace.context import WorkspaceContext as WorkspaceIdentity
    from modex_agent.workspace.registry import ScopeRegistry


@dataclass(frozen=True)
class SupplyInfra:
    """Typed supply-mode infra carrier (C2 fix — replaces ``dict[str, Any]``).

    The orchestrator (``create_pool``) pre-builds the deployment resources
    and hands them to the pipeline through this struct; the stages consume
    typed fields instead of probing string keys — key mismatches surface at
    construction time instead of as ``KeyError`` deep in a stage.
    """

    pool_assembly_ctx: PoolAssemblyContext | None = None
    pool: AgentPool | None = None
    pool_specs: tuple[AssemblySpec, ...] = ()
    """The pool's COMPLETE compiled spec set (root first, then subagents
    in declaration order) — the capability supply aggregation input
    (SPEC §7.1). ``PoolAssembleStage`` aggregates over exactly this set,
    so capabilities effective only on subagents still get their
    pool-level supply. Empty → the stage aggregates over the pipeline
    input spec alone (framework tests / legacy supply shapes)."""
    notification_service: AgentNotificationService | None = None
    """The pool's notification service (ticket 09): supplied by the
    orchestrator so HOOK-slot factories dispatched at Stage 4 (e.g.
    ``user_notice_cleanup``) can resolve it — the strategies leave the
    StrategyAssembly field ``None``. Takes precedence over the strategy
    product."""
    default_llm_provider: LLMProvider | None = None
    """The deployment-level default LLM provider (the orchestrator-resolved
    bot-global default): threaded into the capability supply views so
    LLM-driven supply-side background workers can build on it (the
    ``experience`` reviewer is the bundled consumer — the retired
    experience-specific ``experience_review_provider`` typed field died
    with the supply-face convergence). ``None`` on the legacy roster road
    (whose specs never reference such workers)."""


@dataclass(frozen=True)
class PoolRuntimeDeps:
    """Per-pool runtime objects — the Pool layer of :class:`AssemblyContext`.

    Carries the runtime objects produced/filled by ``PoolAssembleStage``:
    the session tree, control channel, notification service, binding store,
    and the input ``PoolAssemblyContext``. All fields are Python object
    references (not serialized).

    Runtime-object container per rule 12 — NOT Pydantic ``BaseModel``.
    Frozen per rule 11 (leaf value-object, no behavior).
    """

    session_tree_manager: SessionTreeManager | None = None
    control_channel: InMemoryControlChannel | None = None
    notification_service: AgentNotificationService | None = None
    binding_store: CommunicationTargetStore | None = None
    pool_assembly_ctx: PoolAssemblyContext | None = None
    root_provider: WorkspaceRootProvider | None = None
    mcp_registry: McpConnectionRegistry | None = None
    emitter_factory: Callable[[str], ContentEmitter[Any]] | None = None
    terminal_manager: TerminalManagerBase | None = None
    # Pool-unique process registry (same ownership chain as
    # terminal_manager; the no-manager-without-registry invariant is
    # enforced by PoolAssembleStage). All terminal tool factories share
    # this single instance.
    process_registry: ProcessRegistry | None = None
    # Pool-level extensions resolved by PoolAssembleStage (ticket 10) from
    # the spec's INTERCEPTOR / COMMAND_HANDLER rosters against this
    # enriched context. ``None`` = no roster additions — the orchestrator
    # falls back to the workspace-shared chain / the passed-in command
    # processor.
    interceptor_chain: InterceptorChain | None = None
    command_processor: CommandProcessor | None = None
    # Pool-unique fallback persistent bash (set iff terminal_manager is
    # None). The FW bash factory resolves to this same instance so the
    # roster-resolved ``bash`` IS the tool whose ``bash_input`` companion
    # shares its session.
    persistent_bash: PersistentBashTool | None = None
    # Pool-level capability supply (SPEC §7.1): ONE aggregated mapping per
    # pool, keyed by capability registration name. Aggregated by
    # ``PoolAssembleStage`` over the pool's compiled specs; the subagent
    # materialization path receives the SAME mapping via
    # ``AgentMaterializeDeps.capability_supply``. The typed supply fields
    # above converge onto this face in their own W3/W4 waves.
    capability_supply: Mapping[str, CapabilitySupply] = MappingProxyType({})


@dataclass(frozen=True)
class AssemblyContext:
    """Layered assembly context (SPEC §6.5).

    Three layers:
    - **Global** — ``registry`` + ``workspace_registry``. Survives workspace
      eviction (does NOT hold evictable references).
    - **Workspace** — ``workspace_ctx`` + ``workspace_resources`` +
      ``workspace_spec``. Scoped to one workspace.
    - **Pool** — ``pool_runtime``. Filled by ``PoolAssembleStage``.

    Runtime-object container per rule 12 — NOT Pydantic ``BaseModel``.
    Frozen per rule 11 (leaf value-object, no behavior).
    """

    # Global layer
    registry: ComponentRegistry

    # Workspace layer
    workspace_ctx: WorkspaceIdentity
    workspace_registry: ScopeRegistry[Any] | None = None
    workspace_resources: Any | None = None
    workspace_spec: WorkspaceSpec | None = None
    """The declared workspace resource selection (ticket 14, SPEC §3.1) —
    the workspace layer of the scope declaration (memory backend, path
    layout, MCP server set, hosted pools). ``None`` = undeclared (the
    service-level domain config stands; pool-as-root deployments carry no
    workspace layer by construction)."""

    # Pool layer
    pool_runtime: PoolRuntimeDeps | None = None
    llm_provider: LLMProvider | None = None

    # Supply-mode infra: the orchestrator (create_pool) pre-builds the
    # per-pool deployment resources (PoolAssemblyContext + AgentPool) and
    # passes them via this typed struct; InfraAssembleStage copies it to
    # ``builder.infra`` verbatim. This is the BIZ cutover path: create_pool
    # builds deployment resources inline, and the pipeline stages use them
    # without rebuilding.
    infra: SupplyInfra | None = None


def resolution_context(
    registry: ComponentRegistry,
    workspace_ctx: WorkspaceIdentity,
    pool_runtime: PoolRuntimeDeps,
) -> AssemblyContext:
    return AssemblyContext(
        registry=registry,
        workspace_ctx=workspace_ctx,
        pool_runtime=pool_runtime,
    )


@dataclass(frozen=True)
class WorkspaceContext:
    """Workspace-layer carrier of the assembly context chain (SPEC §3.3).

    Holds the path layout (``workspace_ctx``) and workspace-level
    resource handles, including the MCP shared-connection handle
    (``mcp_registry``, ADR-0017). Path knowledge lives ONLY at this
    layer — tool configs carry zero workspace/pool data-path fields
    (SPEC §3.7).

    NOT the per-workspace runtime identity type
    :class:`modex_agent.workspace.context.WorkspaceContext` (target /
    paths, used per-turn) — that type is the ``workspace_ctx`` FIELD
    here. This carrier is the assembly-time LAYER surface a factory
    declares to read workspace-scoped data.

    Runtime-object container per rule 12 — NOT Pydantic ``BaseModel``.
    Frozen per rule 11 (leaf value-object, no behavior).
    """

    workspace_ctx: WorkspaceIdentity
    workspace_registry: ScopeRegistry[Any] | None = None
    workspace_resources: Any | None = None
    workspace_spec: WorkspaceSpec | None = None
    """The declared workspace resource selection (ticket 14, SPEC §3.1).
    Redeclared from :class:`AssemblyContext` with the same annotation and
    default (diamond field-alignment rule)."""
    mcp_registry: McpConnectionRegistry | None = None


@dataclass(frozen=True)
class PoolContext:
    """Pool-layer carrier of the assembly context chain (SPEC §3.3).

    Holds the pool runtime dependencies — terminal manager, capability
    supply, session tree, notification service — all inside
    :class:`PoolRuntimeDeps` — plus the pool-scoped LLM provider. Memory
    handles join this layer when their construction migrates into the
    chain (tickets 09/10).

    Runtime-object container per rule 12 — NOT Pydantic ``BaseModel``.
    Frozen per rule 11 (leaf value-object, no behavior).
    """

    pool_runtime: PoolRuntimeDeps | None = None
    llm_provider: LLMProvider | None = None


@dataclass(frozen=True, kw_only=True)
class AgentContext(WorkspaceContext, PoolContext, AssemblyContext):
    """Full-chain carrier + agent layer of the context chain (SPEC §3.3).

    Joins every layer via multiple inheritance: the resolver passes ONE
    full-chain object to every factory, and a factory's ``create(ctx=...)``
    declaration picks the readable surface via subtyping — ``PoolContext``
    (pool layer only), ``WorkspaceContext`` (workspace layer only),
    ``AssemblyContext`` (legacy pre-ticket view, still legal), or this
    type (full chain). The declared parameter type is the capability
    boundary (mypy-enforced).

    Agent-layer fields carry what the deleted per-invocation special-case
    context once held (agent identity, parent, invocation data, per-agent
    spec reference) — per-invocation data flows through this one carrier
    (ticket 10).

    NOT the per-turn runtime context :class:`modex_agent.core.agent.AgentContext`
    received by ``Tool.execute`` — this carrier exists only at
    assembly time.

    Agent-layer fields are keyword-only; ``agent_name`` is required so a
    chain can never exist without agent identity.

    Runtime-object container per rule 12 — NOT Pydantic ``BaseModel``.
    Frozen per rule 11 (leaf value-object, no behavior).
    """

    agent_name: str
    parent_session: SessionInfo | str | None = None
    invocation_id: str | None = None
    spec: AssemblySpec | None = None
    llm_defaults: Any | None = None
    """The agent's descriptor-level LLM defaults (the
    :class:`~modex_agent.plugins.assembly.native_core.LlmDefaults` object
    feeding ``AgentLLMConfig`` — model name, temperature, max output
    tokens). Threaded by ``assemble_native_agent`` so capability
    ``assemble()`` phases can derive per-agent model facts (the tracing
    capability's span attributes) without reaching into the descriptor;
    ``None`` on hand-built chains (capability assembles then degrade to
    model-less spans, matching the retired ``llm_config is None`` path)."""
    capability_wirings: Mapping[str, CapabilityWiring] | None = None
    """The per-agent capability wiring products (SPEC §7.2 A-phase),
    keyed by capability registration name. ``assemble_native_agent``
    runs the capability dispatch BEFORE tool resolution and threads the
    wirings here, so TOOL/HOOK factories resolve per-agent wiring
    artifacts (e.g. the ``subagents`` capability's per-agent
    communication target store) off the SAME chain they already read.
    ``None`` when the agent compiles no capabilities."""


def agent_context_chain(
    ctx: AssemblyContext,
    *,
    spec: AssemblySpec,
    parent_session: SessionInfo | str | None = None,
    invocation_id: str | None = None,
    llm_defaults: Any | None = None,
) -> AgentContext:
    """Derive the per-agent full-chain view from the legacy context.

    Migration-period bridge (ticket 04): every existing
    ``AssemblyContext`` construction flow keeps producing the legacy
    view; this function lifts it into the layered chain at the
    factory-resolution boundary. The MCP shared handle is surfaced at
    the workspace layer from where it lives today (``pool_runtime``);
    construction ownership moves in later tickets (05/09/10).
    """
    pool_runtime = ctx.pool_runtime
    return AgentContext(
        registry=ctx.registry,
        workspace_ctx=ctx.workspace_ctx,
        workspace_registry=ctx.workspace_registry,
        workspace_resources=ctx.workspace_resources,
        workspace_spec=ctx.workspace_spec,
        pool_runtime=pool_runtime,
        llm_provider=ctx.llm_provider,
        infra=ctx.infra,
        mcp_registry=(pool_runtime.mcp_registry if pool_runtime is not None else None),
        agent_name=spec.agent_name,
        parent_session=parent_session,
        invocation_id=invocation_id,
        spec=spec,
        llm_defaults=llm_defaults,
    )
