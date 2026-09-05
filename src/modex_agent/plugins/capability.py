"""Capability protocol — the cross-slot capability-bundle unit (ADR-0047).

A :class:`Capability` bundles components that belong together (tools +
hooks + prompt sections + pool-level supply) behind ONE registration
name in the 11th ``ComponentSlot`` (``CAPABILITY``). Enablement is
resolved per agent at compile time (C0 predicate + declared overrides),
the bundle's contributions flow into the existing roster merge (C1),
anchors are validated against the final rosters (C2), and assembly
wires per-agent runtime objects (S supply, A assemble). The framework
knows only this protocol — never any concrete capability (SPEC §3.1).

Five phases (SPEC §3.1/§3.3):

===== ======== ========================================= ==========================
Phase Method    Input                                      Output
===== ======== ========================================= ==========================
C0    applies   AgentDeclarationView                       bool
C1    contribute TreePositionView, config                  CapabilityContribution
C2    bind      TreePositionView, config, FinalRosterView  CapabilityBinding
S     supply    PoolSupplyView                             CapabilitySupply | None
A     assemble  CapabilityBinding, AgentContext            CapabilityWiring
===== ======== ========================================= ==========================

Design constraints:
- C0/C1/C2 are deterministic pure functions (SPEC P1): no IO, no clocks,
  no registry reads — violations break the spec-hash byte-stability
  contract.
- This module imports ONLY pydantic + abc + pathlib at runtime. The
  dependency direction is scope→plugins (``scope/compiler.py`` imports
  ``plugins.assembly.spec``); importing ``modex_agent.scope`` here would
  invert it. ``AgentContext`` and ``SystemPromptProvider`` are
  TYPE_CHECKING forward references only.
- Payload types are frozen Pydantic models (rules 10-12: frozen=True,
  extra="forbid").
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    # Forward references only — this module stays import-light at
    # runtime (see module docstring):
    # - ``AgentContext`` types ``Capability.assemble``'s ctx parameter
    #   (same pattern as ``ComponentFactory.create`` in plugins/abc.py).
    # - ``SystemPromptProvider`` is the element type of
    #   ``CapabilityWiring.prompt_providers``; the field itself stays
    #   ``tuple[Any, ...]`` (see its docstring), so this import is a
    #   documentation anchor, not a runtime dependency.
    from modex_agent.core.prompt import SystemPromptProvider  # noqa: F401
    from modex_agent.plugins.assembly.context import AgentContext

__all__ = [
    "AgentDeclaredFields",
    "AgentDeclarationView",
    "Capability",
    "CapabilityBinding",
    "CapabilityConfig",
    "CapabilityContribution",
    "CapabilityError",
    "CapabilitySupply",
    "CapabilityWiring",
    "ChildSummary",
    "CompiledCapability",
    "DerivedToolOrigin",
    "DerivedToolSpec",
    "FinalRosterView",
    "PoolSupplyAgentEntry",
    "PoolSupplyView",
    "PromptSectionSpec",
    "SectionPlacement",
    "ToolReplacementSpec",
    "TreePositionView",
]


class CapabilityError(ValueError):
    """Boot-fail error raised by a capability's ``bind`` on anchor failure.

    Capabilities construct it from their ``TreePositionView`` facts, so the
    message carries pool/agent/capability context plus the repair path
    (e.g. which ``tools: [-x]`` veto dismantled a required anchor). The
    compiler lets it propagate — a broken anchor is a boot failure, never
    a silent degradation (SPEC §11 error matrix).
    """


class CapabilityConfig(BaseModel):
    """Default empty capability config (SPEC §4).

    Capabilities with no knobs share this model: frozen and
    ``extra="forbid"`` — ANY declared config key is rejected at compile
    time, so an empty-config capability fails loud on stray YAML keys.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")


class Capability(ABC):
    """能力包 — the cross-slot capability-bundle unit (ADR-0047, SPEC §3.1).

    Subclasses set ``name`` (the registration name = the declaration key
    under ``capabilities:`` in a scope declaration) and — when they have
    knobs — their own frozen ``config_model``, then override the phases
    they need. Only :meth:`assemble` is abstract; every other phase has
    a default.

    Purity contract (SPEC P1): ``applies``/``contribute``/``bind`` run
    inside ``compile_scope`` and MUST be deterministic pure functions of
    their arguments — they feed the spec-hash byte-stability contract.
    ``supply``/``assemble`` run at assembly time and may read the
    workspace/context chain.
    """

    name: str
    """Registration name — the declaration key (``capabilities: {<name>: …}``)."""

    config_model: ClassVar[type[BaseModel]] = CapabilityConfig
    """Config schema validated at compile time (frozen, extra="forbid").
    Default: the shared empty :class:`CapabilityConfig`."""

    # ── compile time ──

    def applies(self, view: AgentDeclarationView) -> bool:
        """C0: enablement predicate (SPEC §3.2).

        Scans the agent's DECLARED state and decides whether this
        capability auto-applies. Default ``False`` — a pure opt-in
        bundle that only a declared override can enable. Must stay
        pure: never read the final rosters or another capability's
        contributions (that would create an
        enablement←contribution←enablement cycle).
        """
        return False

    def contribute(self, tree: TreePositionView, config: BaseModel) -> CapabilityContribution:
        """C1: pre-merge contribution (SPEC §3.3).

        Contributed tool/hook names enter the roster merge BASE (so the
        component-level veto ``tools: [-x]`` / ``hooks: [-y]`` still
        applies to them), ``tool_replacements`` record O3 same-name
        replacements, ``sections`` are collected for C2 gating, and
        ``derived_tools`` carry tree-derived entries through the compiler's
        derived-entry machinery (origin + targets ride the spec — the
        provenance bill vocabulary, not the plain merge base). Default:
        empty.
        """
        return CapabilityContribution()

    def bind(
        self, tree: TreePositionView, config: BaseModel, final: FinalRosterView
    ) -> CapabilityBinding:
        """C2: post-merge anchor validation + section gating (SPEC §3.3).

        Default: no anchor — the contribution IS the binding (SPEC §4):
        the contributed sections pass through as ``active_sections`` and
        the contributed hooks as ``hooks`` (all vouched) unchanged.
        Overrides with anchors raise at compile time (boot fail) when a
        required tool/hook did not survive the merge, or drop the
        non-tool components their anchors lost from ``hooks``.
        """
        contribution = self.contribute(tree, config)
        return CapabilityBinding(
            active_sections=contribution.sections,
            hooks=contribution.hooks,
        )

    # ── assembly time ──

    def supply(self, view: PoolSupplyView) -> CapabilitySupply | None:
        """S: pool-level supply (SPEC §3.3).

        ``view`` lists the (agent, config) pairs of pool agents this
        capability is effective on. Default ``None`` — no pool-level
        need. Implementations that require a supply they cannot build
        raise loudly (never return a half-built supply).
        """
        return None

    @abstractmethod
    async def assemble(self, binding: CapabilityBinding, ctx: AgentContext) -> CapabilityWiring:
        """A: per-agent wiring (SPEC §3.3).

        Produces the ordered prompt providers (element type
        ``SystemPromptProvider``) and per-agent wiring artifacts. Pool
        supply is read from the context chain; a missing supply raises
        loudly. ``ctx`` is the full-chain
        :class:`~modex_agent.plugins.assembly.context.AgentContext`
        (forward ref — same TYPE_CHECKING pattern as
        ``ComponentFactory.create`` in plugins/abc.py).
        """
        ...


# ---------------------------------------------------------------------------
# Compile-time views (C0/C1/C2 inputs)
# ---------------------------------------------------------------------------


class ChildSummary(BaseModel):
    """Direct-child summary carried by the tree views (SPEC §4)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    description: str = ""


class AgentDeclaredFields(BaseModel):
    """Frozen projection of an agent's DECLARED fields — the C0 predicate's
    field face (SPEC §3.2).

    Mirrors the primitive face of ``AgentSpec`` (``scope/spec.py``):
    enum-typed fields project to their string values (``toolset`` → a
    ``ToolPreset`` value, ``execution_strategy`` → a strategy name,
    ``provider_kind`` → a provider name) so this module never imports
    ``modex_agent.scope`` — the dependency direction is scope→plugins.
    Nested declarations (``memory`` and friends) are intentionally NOT
    projected: predicates read primitive fields only. None-ability and
    defaults mirror ``AgentSpec`` field-for-field.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    toolset: str | None = None
    """Toolset profile name; ``None`` = position-derived default."""

    tools: list[str] | None = None
    """Explicit tool roster declaration; ``None`` = defer to the profile."""

    hooks: list[str] | None = None
    """Hook roster declaration (verbatim, merge prefixes included)."""

    mcp: list[str] = Field(default_factory=list)
    use_terminal: bool = False
    execution_strategy: str = "react"
    """Mirrors ``AgentSpec``'s ``ExecutionStrategyKind.REACT`` default."""

    provider_kind: str | None = None
    eager: bool | None = None
    """Registration timing override; ``None`` = position-derived default."""

    roles: list[str] = Field(default_factory=list)
    description: str = ""


class AgentDeclarationView(BaseModel):
    """C0 predicate input: tree position + the agent's declared fields
    (SPEC §3.2).

    Read-only DECLARED state (pre-merge) — never the final rosters and
    never another capability's contributions.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    pool_name: str
    agent_name: str
    is_root: bool
    parent: str | None
    children: tuple[ChildSummary, ...]
    """Direct children only."""

    peers: tuple[str, ...]
    declared: AgentDeclaredFields


class TreePositionView(BaseModel):
    """C1/C2 tree input: the same tree facts as
    :class:`AgentDeclarationView` without the declared fields (SPEC §4).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    pool_name: str
    agent_name: str
    is_root: bool
    parent: str | None
    children: tuple[ChildSummary, ...]
    """Direct children only."""

    peers: tuple[str, ...]


class FinalRosterView(BaseModel):
    """C2 input: the post-merge rosters (SPEC §3.3).

    ``tools``/``hooks`` are the FINAL merged lists — anchor checks and
    section gating see exactly what survived the ``+/-`` merge.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    tools: tuple[str, ...]
    hooks: tuple[str, ...]


# ---------------------------------------------------------------------------
# C1/C2 outputs
# ---------------------------------------------------------------------------


class ToolReplacementSpec(BaseModel):
    """Contribution-level tool replacement declaration (SPEC §4, the O3
    same-name replacement pattern).

    Generalizes the compiler's historical ACI special case: when both
    names survive the merge, ``replaced_tool`` is served by
    ``replacement_tool``'s implementation. The compiler-side provenance
    record is the scope layer's separate concern — this module never
    imports it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    replaced_tool: str
    replacement_tool: str


class DerivedToolOrigin(StrEnum):
    """Origin vocabulary for tree-derived tool entries (SPEC §8.4 / A3).

    The capability-channel face of the compile product's provenance
    vocabulary: the scope compiler maps each member onto the
    identically-valued ``ToolOrigin`` member (``scope/compiler.py`` owns
    the full classification enum — capability.py stays import-light, and
    the dependency direction is scope→plugins). Values are the bill's
    wire format; a member with no ``ToolOrigin`` counterpart fails the
    compile loudly.
    """

    DERIVED_TASK = "derived_task"
    DERIVED_SEND_TO_AGENT = "derived_send_to_agent"
    DERIVED_SEND_TO_PEER = "derived_send_to_peer"


class DerivedToolSpec(BaseModel):
    """One tree-derived tool entry a capability contributes (SPEC §8.4 A3).

    Unlike a plain ``tools`` contribution (which enters the roster merge
    base and classifies as CAPABILITY_DERIVED), a derived entry rides the
    compiler's derived-entry machinery: the tool name enters the base in
    spec order and the compiled provenance entry carries the declared
    origin plus the per-target list — the tree facts the capability
    already holds (direct children, parent, peer pool names).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    tool: str
    origin: DerivedToolOrigin
    targets: tuple[str, ...] = ()


class SectionPlacement(StrEnum):
    """Fixed system-prompt anchor for a capability section."""

    HEAD = "head"
    TAIL = "tail"


class PromptSectionSpec(BaseModel):
    """One contributed prompt section (SPEC §4).

    ``section_id`` is namespaced ``"<capability>.<section>"`` by
    convention (e.g. ``"todo.discipline"``). ``placement`` selects one of
    two fixed anchors; ``order`` sorts sections within that anchor.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    section_id: str
    order: int
    placement: SectionPlacement = SectionPlacement.HEAD
    # Open extension payload (rule 14): per-section config is genuinely
    # open — keys are capability-private and feed assemble().
    config: dict[str, Any] = Field(default_factory=dict)


class CapabilityContribution(BaseModel):
    """C1 output: what the capability contributes to the merge base
    (SPEC §4).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    tools: tuple[str, ...] = ()
    """Tool names entering the roster merge base."""

    derived_tools: tuple[DerivedToolSpec, ...] = ()
    """Tree-derived tool entries entering the merge base THROUGH the
    derived-entry machinery (origin + targets preserved — SPEC §8.4 A3).
    The compiler routes these identically to its historical hardcoded
    tree derivation; a name listed here should not also appear in
    ``tools`` (the derived channel owns its classification)."""

    tool_replacements: tuple[ToolReplacementSpec, ...] = ()
    hooks: tuple[str, ...] = ()
    """Hook names entering merged_hooks."""

    sections: tuple[PromptSectionSpec, ...] = ()


class CapabilityBinding(BaseModel):
    """C2 output: the capability's compile product for one agent (SPEC §4).

    ``active_sections`` is the gated surviving section set; ``hooks`` is
    the FINAL hook set this capability vouches for (post-anchor gating);
    ``payload`` threads capability-private compile results through to
    assemble().
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    active_sections: tuple[PromptSectionSpec, ...] = ()
    hooks: tuple[str, ...] = ()
    """The contributed hook names this capability vouches for. The
    compiler keeps a contributed hook in ``merged_hooks`` iff at least one
    contributing capability's binding lists it here — an anchored
    capability drops the names its anchors did not survive for (SPEC §3.3
    "锚存活才带非工具件"). Gating only ever removes contributed names;
    handwritten roster entries are the declaration's, never the
    binding's, to remove."""
    # Open extension payload (rule 14): capability-private compile
    # product — keys and shapes are defined by each capability and
    # threaded opaquely to assemble().
    payload: dict[str, Any] = Field(default_factory=dict)


class CompiledCapability(BaseModel):
    """The JSON-serializable compile product of one effective capability
    (SPEC §6) — one element of ``AssemblySpec.capabilities``.

    Carries only frozen data (name + validated config + binding): the
    capability OBJECT never enters the compile product (spec-hash
    byte-stability) — assembly re-resolves by name from the registry.
    O3 tool-replacement declarations live ONLY in the scope layer's
    ``ToolReplacement`` provenance records (applied by the compiler at
    the post-merge application point) — no parallel copy here.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    """The capability registration name (the ``capabilities:`` key)."""

    # Open extension payload (rule 14): the capability's config AFTER
    # ``config_model`` validation, dumped to its serializable face — keys
    # are capability-private and vary per config_model.
    config: dict[str, Any] = Field(default_factory=dict)
    binding: CapabilityBinding


# ---------------------------------------------------------------------------
# S/A faces (assembly time)
# ---------------------------------------------------------------------------


class CapabilitySupply(ABC):  # noqa: B024 — deliberate marker ABC
    """S-phase pool-level supply marker base (SPEC §4).

    An empty marker ABC: each capability defines its own supply shape
    (stores, services, background runners). Consumers validate the
    concrete type at this real extension boundary — the same justified
    ``isinstance`` exemption as
    ``ComponentRegistry.resolve_namespace_model``.

    Pool-scoped background workers (SPEC §8.3 D4): a supply MAY own
    background tasks. ``supply()`` CONSTRUCTS objects only; pool assembly
    (:class:`~modex_agent.plugins.assembly.stages.pool_assemble.PoolAssembleStage`)
    calls :meth:`start` exactly once per supply right after the
    aggregation, and pool teardown calls :meth:`stop` — BOTH teardown
    roads (the pipeline's cleanup-on-failure and
    ``AgentPool.shutdown_all``) ride the same idempotent stop, so a
    supply that started a worker never leaks it. Supplies without
    background work keep the no-op defaults (the marker semantics are
    unchanged for them).
    """

    async def start(self) -> None:
        """Start this supply's pool-scoped background workers (no-op default).

        Called by pool assembly after the supply is constructed. Must be
        idempotent — teardown may call :meth:`stop` more than once across
        the failure/shutdown roads.
        """
        return None

    async def stop(self) -> None:
        """Stop this supply's background workers (no-op default). Idempotent."""
        return None


class PoolSupplyAgentEntry(BaseModel):
    """One pool agent a capability is effective on, with its validated
    config (SPEC §4).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    agent_name: str
    # Open extension payload (rule 14): the validated capability config
    # (post config_model validation) — shapes vary per capability.
    config: dict[str, Any]


class PoolSupplyView(BaseModel):
    """S input: the pool's effective view of one capability (SPEC §4).

    Lists every (agent, config) pair the capability is effective on in
    the pool. Diverging configs are the capability's own arbitration
    (SPEC OQ1) — supply() raises if it cannot resolve them.

    The resource fields (todo 11) are the pool-assembly workspace
    distilled to the handles a ``supply()`` may construct from —
    ``PoolAssembleStage`` populates them from ``PoolAssemblyContext``.
    They are optional so harness/fixture views without a live pool keep
    constructing; a capability needing one that is absent raises loudly
    in its own ``supply()``. ``persistence`` is a bot-supplied manager
    (``WorkspacePersistenceManager``) — ``Any`` is the documented
    framework/bot escape hatch (same class as ``PoolAssemblyContext.persistence``).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    pool_name: str
    entries: tuple[PoolSupplyAgentEntry, ...]
    root_agent_name: str | None = None
    """The pool's root (main) agent name — the keying agent for per-main
    pool storage (the retired BIZ experience dir layout
    ``experiences/<pool>/<main>``). Populated by the aggregation from the
    spec set's first entry (root first, then subagents in declaration
    order); ``None`` on hand-built harness views."""
    data_dir: Path | None = None
    """The pool's workspace data root — fallback base for pool-scoped storage."""
    runtime_dir: Path | None = None
    """The pool's on-disk runtime dir (``<data>/runtime_state/<pool>``) —
    ``None`` when no pool data is materialized."""
    persistence_backend: str | None = None
    """The workspace persistence backend value (``"file"`` / ``"sqlite"``);
    ``None`` = no app config."""
    persistence: Any | None = None
    """The workspace persistence manager (``None`` in FILE mode); supplies
    the shared SQLite connection a supply may build adapters on."""
    default_llm_provider: Any | None = None
    """The deployment-level default LLM provider (the orchestrator-resolved
    bot-global default). Supplies may build LLM-driven supply-side background
    workers (e.g. the experience reviewer) on it; ``None`` when the deployment
    has none. ``Any`` is the documented escape hatch (same class as
    ``persistence`` — typing it would break this module's import-light
    contract)."""
    pool: Any | None = None
    """The pool's :class:`~modex_agent.multi_agent.pool.AgentPool` (the
    agent registry + pool handles) — the router-class supplies resolve
    against it. ``Any`` is the documented escape hatch (same class as
    ``persistence`` — typing it would break this module's import-light
    contract); ``None`` on hand-built harness views."""
    session_tree: Any | None = None
    """The pool's session tree manager — the per-pool skeleton object
    supplies may thread into pool-scoped services (``Any`` escape hatch,
    same class as ``pool``; ``None`` on hand-built harness views)."""
    template_registry: Any | None = None
    """The pool's subagent template registry (``Any`` escape hatch, same
    class as ``pool``; ``None`` when the pool carries none)."""
    session_registry: Any | None = None
    """The pool's session registry (``Any`` escape hatch, same class as
    ``pool``; ``None`` when the deployment has none)."""
    scope_path: Any | None = None
    """The pool's :class:`~modex_agent.workspace.scope_path.ScopePath`
    (``Any`` escape hatch, same class as ``pool``; ``None`` on hand-built
    harness views)."""
    workspace_manager: Any | None = None
    """The workspace manager (resolver) bound to the pool's assembly
    context (``Any`` escape hatch, same class as ``persistence``;
    ``None`` on hand-built harness views)."""
    project_dir: Path | None = None
    """The deployment project dir the pool assembles under."""
    trace_enabled: bool = True
    """The observability-derived trace toggle (``app_config.observability
    .trace_backend != OFF``); supplies building trace-path-aware services
    thread it through."""
    trace_store: Any | None = None
    """The pool's caller-carried trace store (``pool_data.trace_store``,
    the BIZ snapshot field): the tracing capability's supply ADOPTS this
    instance when present instead of building its own — the harbor trial
    path injects its collector-backed ``PoolTraceStore`` through it, and
    the adoption keeps the store's lifecycle with its owner (``Any``
    escape hatch, same class as ``persistence``; ``None`` on hand-built
    harness views)."""


class CapabilityWiring(BaseModel):
    """A-phase output: per-agent wiring (SPEC §4).

    ``prompt_providers`` map positionally to ``active_sections`` and feed
    their fixed memory-context anchors (ascending section ``order``);
    ``artifacts`` carry per-agent runtime objects (e.g. communication
    target stores) to the factories that consume them.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    prompt_providers: tuple[Any, ...] = ()
    """Ordered prompt-section providers. Element type:
    ``SystemPromptProvider`` (:mod:`modex_agent.core.prompt`,
    forward-imported under TYPE_CHECKING). ``Any`` is a documented
    rule-3 escape — the same class of escape as
    ``ComponentFactory.create``'s return type: a concrete element type
    would force a runtime import and break this module's import-light
    contract (see module docstring)."""

    # Open extension payload (rule 14): per-agent wiring objects keyed
    # by capability-private names — shapes vary per capability.
    artifacts: dict[str, Any] = Field(default_factory=dict)
