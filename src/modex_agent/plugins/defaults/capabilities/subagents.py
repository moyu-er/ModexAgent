"""The FW-bundled ``subagents`` capability — in-plugin tree derivation.

The flagship bundle (SPEC §8.4): the communication trio's tree
derivation — historically the compiler's hardcoded ``_derived_entries``
— IS this capability's :meth:`SubagentsCapability.contribute`. The
compiler keeps only the generic derived-entry machinery (the
``derived_tools`` contribution channel: merge-base position +
origin/targets classification); it knows no capability name and no
tree-derivation rule.

Enablement is the tree predicate (SPEC §3.2): an agent participates in
the communication topology iff it has declared children, is itself
non-root, or its pool carries peer links — so the shipped zero-config
declarations (no ``capabilities:`` block) compile to byte-identical
rosters/origins/targets (SPEC §14.7; pinned by the derived-entries
equivalence golden). A no-children no-peer root derives nothing (the
zero-config equivalence anchor). External agents never run predicates
(SPEC §3.2 C0 structural exclusion) — the retired compiler-side
derivation produced dead-weight roster entries on them (external agents
take no native tool surface; peer replies route via modexctl); that
divergence is documented in the subagents golden exemption table.

The supply wave (SPEC §8.4 supply/facilities rows) completes the
migration: :meth:`SubagentsCapability.supply` builds the pool's
:class:`AgentCommunicationService` (the retired BIZ factory
construction FW-migrated — every constructor dependency is a
skeleton/workspace object), :meth:`SubagentsCapability.assemble` builds
the per-agent :class:`CommunicationTargetStore` from the declared tree
(the retired BIZ root-store + ``AgentTemplate._comm_facilities``
constructions converged) and wires the three section providers — the
retired ``AgentCommunicationSystemPromptProvider``'s briefs, migrated
byte-verbatim (content byte-equality is the acceptance bar; the anchor
position and the one-section-per-brief split are the documented designed
deltas, SPEC §7.3).

The ``subagent_auto_send`` hook rides the roster for every non-root
agent (unconditional-when-contributed — no anchor): the HOOK-slot
factory (``plugins/defaults/hooks.py``) derives the per-agent fields
from the assembly context chain, replacing the retired direct
construction in ``AgentTemplate.materialize``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

from pydantic import BaseModel, ConfigDict

from modex_agent.core.agent import AgentCommKind, ExecutionStrategyKind
from modex_agent.core.prompt import SystemPromptProvider
from modex_agent.multi_agent.address import AgentAddress
from modex_agent.multi_agent.communication import AgentCommunicationService
from modex_agent.multi_agent.tools import (
    SEND_TO_AGENT_TOOL_NAME,
    SEND_TO_PEER_TOOL_NAME,
    CommunicationTarget,
    CommunicationTargetStore,
)
from modex_agent.plugins.abc import AgentType
from modex_agent.plugins.capability import (
    AgentDeclarationView,
    Capability,
    CapabilityBinding,
    CapabilityContribution,
    CapabilityError,
    CapabilitySupply,
    CapabilityWiring,
    DerivedToolOrigin,
    DerivedToolSpec,
    FinalRosterView,
    PoolSupplyView,
    PromptSectionSpec,
    TreePositionView,
)

if TYPE_CHECKING:
    from pathlib import Path

    # Forward reference only (capability.py's import-light pattern): the
    # full-chain context is threaded at assembly time, never imported here.
    from modex_agent.plugins.assembly.context import AgentContext, PoolRuntimeDeps

__all__ = [
    "SUBAGENTS_AUTO_SEND_HOOK_NAME",
    "TASK_TOOL_NAME",
    "SubagentsCapability",
    "SubagentsCapabilityConfig",
    "SubagentsSupply",
    "build_pool_communication_service",
    "require_subagents_supply",
]

#: The task tool's roster name. The V6 authority is
#: ``scope.validator.TASK_TOOL_NAME`` (same value) — the scope package
#: cannot be imported from here (dependency direction is scope→plugins);
#: the equivalence goldens pin the two literals together.
TASK_TOOL_NAME = "task"

#: The auto-send hook's roster name — the HOOK-slot registration name
#: (``plugins/defaults/hooks.py`` registers under the same literal).
SUBAGENTS_AUTO_SEND_HOOK_NAME = "subagent_auto_send"

_DELEGATION_SECTION_ID = "subagents.delegation"
_CONSULTATION_SECTION_ID = "subagents.consultation"
_PEER_SECTION_ID = "subagents.peer"

#: Wiring artifact key carrying the per-agent target store (SPEC §7.2:
#: per-agent wiring objects reach the consuming TOOL factories through
#: ``AgentContext.capability_wirings``).
_TARGET_STORE_ARTIFACT = "target_store"


class SubagentsCapabilityConfig(BaseModel):
    """Empty config — the subagents capability has no knobs (any key
    rejected)."""

    model_config = ConfigDict(frozen=True, extra="forbid")


def build_pool_communication_service(
    *,
    root_agent_name: str,
    pool: object,
    tree: object,
    pool_name: str,
    project_dir: Path | None = None,
    session_registry: object | None = None,
    template_registry: object | None = None,
    scope_path: object | None = None,
    workspace_manager: object | None = None,
    trace_enabled: bool = True,
) -> AgentCommunicationService:
    """The single FW construction authority for the pool communication
    service (SPEC §8.4 supply row — the retired BIZ ``create_pool``
    construction FW-migrated).

    Two callers share this ONE authority: the ``subagents`` capability's
    :meth:`SubagentsCapability.supply` (capability-effective pools) and
    the orchestrator's capability-less fallback (external pools and lone
    roots — pools that compile no ``subagents`` capability but still
    need the router for the control-facade/modexctl plane). The
    ``target_store`` stays ``None`` here: per-agent stores are the
    ASSEMBLE phase's business — the pool root's store binds to the
    service at the root's native assembly
    (:meth:`SubagentsCapability.assemble`).

    ``pool``/``tree``/``session_registry``/``template_registry``/
    ``scope_path``/``workspace_manager`` are the skeleton/workspace
    objects the service router takes (D1: no business resources); they
    are typed ``object`` here because the BIZ fallback calls this
    helper without the framework's concrete types in scope.
    """
    from modex_agent.multi_agent.communication.service import (
        AgentCommunicationService as _Service,
    )

    return _Service(
        source=AgentAddress(name=root_agent_name),
        registry=pool,  # type: ignore[arg-type]
        tree=tree,  # type: ignore[arg-type]
        template_registry=template_registry,  # type: ignore[arg-type]
        pool=pool,  # type: ignore[arg-type]
        pool_name=pool_name,
        project_dir=project_dir,
        session_registry=session_registry,  # type: ignore[arg-type]
        scope_path=scope_path,  # type: ignore[arg-type]
        workspace_manager=workspace_manager,  # type: ignore[arg-type]
        trace_enabled=trace_enabled,
    )


@dataclass(frozen=True)
class SubagentsSupply(CapabilitySupply):
    """The subagents capability's pool-level supply (SPEC §8.4 supply row).

    Carries the pool's :class:`AgentCommunicationService` — the pure
    router every communication consumer in the pool shares: the three
    derived TOOL factories (task / send_to_agent / send_to_peer), the
    control facade (modexctl), and the peer-resolution wiring.

    D4 lifecycle: NO start/stop — the service is a router, not a
    background task (the :class:`CapabilitySupply` no-op defaults are
    the whole lifecycle contract; contrast
    :class:`~modex_agent.plugins.defaults.capabilities.experience.ExperienceSupply`,
    which owns a live curator loop).
    """

    service: AgentCommunicationService


def require_subagents_supply(pool_runtime: PoolRuntimeDeps | None) -> SubagentsSupply:
    """Loud supply read shared by the communication TOOL factories and
    ``assemble`` (SPEC §7.1) — the ``require_todo_supply`` pattern.

    The pool's ``capability_supply['subagents']`` must be the concrete
    :class:`SubagentsSupply` — :meth:`SubagentsCapability.supply` builds
    it iff the capability is effective on some agent in the pool.
    Missing or wrong-typed supply raises with the repair path: a
    roster-referenced communication component is never silently
    skipped.
    """
    supply = pool_runtime.capability_supply.get("subagents") if pool_runtime is not None else None
    if supply is None:
        raise ValueError(
            "subagents components require the pool's 'subagents' capability "
            "supply (capability_supply['subagents']); it is built iff the "
            "subagents capability is effective in the pool — the tree "
            "predicate (declared children / non-root / declared peers) "
            "enables it without any capabilities: block"
        )
    if not isinstance(supply, SubagentsSupply):
        raise ValueError(
            "capability_supply['subagents'] must be SubagentsSupply, got "
            f"{type(supply).__name__}; only SubagentsCapability.supply "
            "builds the subagents supply"
        )
    return supply


# ---------------------------------------------------------------------------
# The three section providers. The delegation brief (v3) is the FULL
# delegation methodology — when to delegate, the six-element brief spec,
# and the lifecycle discipline (dispatch → wait → verify → synthesize).
# The ``task`` tool description carries only the call mechanics and quick
# exclusions and points here; the methodology lives nowhere else.
# ---------------------------------------------------------------------------

#: Constant versions for the static briefs — the content never changes,
#: so the KV-cache prefix never invalidates within a session (SPEC §7.3
#: / E10: static content = constant version).
_DELEGATION_SECTION_VERSION = "subagents.delegation.v3"
_CONSULTATION_SECTION_VERSION = "subagents.consultation.v1"

# The delegation brief — v2 revised the delegation axis from per-step
# triviality to overall complexity (v1's "trivial one-step actions"
# exemption let a 9-call install grind stay in the main context because
# each call was trivial; observed on tb21-all-v8 bn-fit-modify, where
# 20/40 calls were self-contained grind a fresh-context subagent should
# have absorbed). v3 reorganizes the same rules into four subsections
# (when to delegate / when NOT / writing the brief / discipline) and
# absorbs the mechanism notes that previously duplicated the ``task``
# description — no rule was dropped; each now has exactly one home.
_DELEGATION_BRIEF = """\
## Delegating To Subagents

You own the `task` tool. Its description lists the available subagents
and what each is for — check it before starting non-trivial work, and
pick the subagent whose strengths match the job. This section is the
full methodology: when to delegate, how to write the brief, and the
discipline that carries a delegation from dispatch to synthesis.

### When to delegate

- Bulk or parallelizable investigation ("find all X", "map how Y works") — a
  fresh context does it cheaper and without polluting yours.
- A complex, self-contained sub-goal — even when each step is trivial
  (install a toolchain, pin down an unfamiliar API, chase one class of
  error): delegate it, your context is the scarce resource.
- Mid-flight escalation: if a self-contained sub-goal keeps generating more
  steps than expected, stop grinding — package the findings so far into the
  brief and hand off the remainder.
- An independent implementation piece that would otherwise crowd your context.
- Verification of a deliverable before you report it — fresh eyes, no
  anchoring on your own assumptions.

### When NOT to delegate

- Simple few-step work: a needle query or a couple of known calls — direct
  tool use is faster.
- Work whose brief would cost more to write than the work itself — deeply
  coupled to your live context, do it yourself.

### Writing the task brief

The subagent sees ONLY the `content` you pass — never
your conversation, reasoning, or tool results. Output quality is directly
proportional to brief quality. Structure every brief with all six elements:

- TASK: the concrete objective — what exactly to do, not a topic.
- CONTEXT: relevant file paths, symbols, patterns, and constraints it must
  know to work autonomously.
- SCOPE: research-only (search/read/analyze) or implementation (write/edit).
- OUTPUT: exactly what to return in its final reply.
- VERIFICATION: how to verify success (e.g., the test command to run).
- BOUNDARIES: what NOT to do, out-of-scope items, files it must not touch.

A one-line brief like "fix the bug" is insufficient. If your brief is only a
few lines, it is probably too thin.

### Delegation discipline

- Dispatch independent tasks in parallel — multiple `task` calls in a single
  message — but no more than 3 is suggested, and give parallel subagents
  disjoint files and resources so they cannot conflict.
- After dispatching, end your turn and wait for the result notification. You
  may continue only with non-overlapping work.
- Do not duplicate delegated work yourself — integrate the result instead.
- Verify implementation results before relying on them — run the brief's
  VERIFICATION step or spot-check the change. A success claim is a report,
  not proof.
- Synthesize research results before answering — each subagent saw only its
  slice. Merge findings into one whole picture, reconcile conflicts, and
  never forward a single narrow result as the complete answer.
- To continue a subagent session (follow-up, corrections), pass its
  `invocation_id` instead of re-dispatching from scratch.
"""

_CONSULTATION_BRIEF = (
    "## Consulting Your Parent\n\n"
    "Use `send_to_agent` only to ask your parent a question or request a "
    "decision when you cannot proceed without input. Do not use it to report "
    "results or progress."
)


class _StaticBriefProvider(SystemPromptProvider):
    """Static brief section — the retired sub-provider's exact bytes."""

    def __init__(self, version: str, content: str) -> None:
        super().__init__()
        self._version = version
        self._content = content

    async def _fetch_version(self) -> str:
        return self._version

    async def _fetch_content(self) -> str:
        return self._content


class _PeerSectionProvider(SystemPromptProvider):
    """Remote-agent reply contract — the retired ``_PeerCommSubProvider``
    migrated.

    Reads the LIVE per-agent target store at render time: peer NORMAL
    targets join the root's store at workspace-materialize time
    (``resolve_peer_targets``), AFTER assembly — the provider's version
    follows the remote-target name set (a name-set change = one
    re-fetch, the KV-cache refresh contract), and empty content (no
    remote targets yet) renders nothing — byte-equal to the retired
    provider's gate.
    """

    def __init__(self, store: CommunicationTargetStore) -> None:
        super().__init__()
        self._store = store

    def _remote_target_names(self) -> list[str]:
        return sorted(t.name for t in self._store.list_peers() if t.tree_ref is not None)

    async def _fetch_version(self) -> str:
        return "subagents.peer:" + ",".join(self._remote_target_names())

    async def _fetch_content(self) -> str:
        names = self._remote_target_names()
        if not names:
            return ""
        name_list = "\n".join(f"  - {name}" for name in names)
        return (
            "## Communicating With Remote Agents\n\n"
            "Some agents you can reach via `send_to_peer` cannot see anything "
            "you produce normally — not this reply, not your reasoning, not your "
            "tool output. For these agents the ONLY way they ever hear from you "
            "is a `send_to_peer` call aimed at them.\n\n"
            "Agents that require explicit sends:\n"
            f"{name_list}\n\n"
            "Replies are OPTIONAL. Only call `send_to_peer` back when the sender "
            "actually needs your response — do NOT acknowledge just to be polite, "
            "and do NOT ping-pong. If the incoming message does not require action "
            "from you, end your turn without replying.\n"
        )


class SubagentsCapability(Capability):
    """The communication trio as a tree-derived capability bundle.

    Five-phase shape: ``applies`` is the tree predicate (SPEC §3.2
    verbatim — the zero-config equivalence anchor); ``contribute``
    derives the entries from the tree facts (children ⇒ ``task`` +
    delegation section; non-root ⇒ ``send_to_agent`` +
    ``subagent_auto_send`` hook + consultation section; root with peers ⇒
    ``send_to_peer`` + peer section); ``bind`` is the V6 dual check (the
    richer-error second check behind the phase-2 validator —
    child-carrying agents keep ``task``); ``supply`` builds the pool's
    communication service; ``assemble`` builds the per-agent target
    store from the declared tree, binds the root's store onto the
    service, and wires the three section providers.
    """

    name = "subagents"
    config_model: ClassVar[type[BaseModel]] = SubagentsCapabilityConfig

    def applies(self, view: AgentDeclarationView) -> bool:
        # 该 agent 参与通信拓扑才启用：有子、或非根、或有 peer。
        # 无子无 peer 的根 = 今日行为（通信三件套一样不得）——零配置等价的锚点。
        return bool(view.children) or not view.is_root or bool(view.peers)

    def contribute(self, tree: TreePositionView, config: BaseModel) -> CapabilityContribution:
        del config  # knob-free
        derived: list[DerivedToolSpec] = []
        hooks: list[str] = []
        sections: list[PromptSectionSpec] = []
        if tree.children:
            derived.append(
                DerivedToolSpec(
                    tool=TASK_TOOL_NAME,
                    origin=DerivedToolOrigin.DERIVED_TASK,
                    targets=tuple(child.name for child in tree.children),
                )
            )
            sections.append(PromptSectionSpec(section_id=_DELEGATION_SECTION_ID, order=40))
        # parent is not None ⇔ not is_root (the compiler's tree views
        # derive is_root from the parent reference).
        parent = tree.parent
        if parent is not None:
            derived.append(
                DerivedToolSpec(
                    tool=SEND_TO_AGENT_TOOL_NAME,
                    origin=DerivedToolOrigin.DERIVED_SEND_TO_AGENT,
                    targets=(parent,),
                )
            )
            hooks.append(SUBAGENTS_AUTO_SEND_HOOK_NAME)
            sections.append(PromptSectionSpec(section_id=_CONSULTATION_SECTION_ID, order=41))
        if tree.is_root and tree.peers:
            derived.append(
                DerivedToolSpec(
                    tool=SEND_TO_PEER_TOOL_NAME,
                    origin=DerivedToolOrigin.DERIVED_SEND_TO_PEER,
                    targets=tuple(tree.peers),
                )
            )
            sections.append(PromptSectionSpec(section_id=_PEER_SECTION_ID, order=42))
        return CapabilityContribution(
            derived_tools=tuple(derived),
            hooks=tuple(hooks),
            sections=tuple(sections),
        )

    def bind(
        self, tree: TreePositionView, config: BaseModel, final: FinalRosterView
    ) -> CapabilityBinding:
        """C2: the V6 dual check — child-carrying agents keep ``task``.

        The phase-2 validator's V6 stays the skeleton-integrity check
        (it fires on every effective toolset, including trees that
        disable this capability); this bind is the richer-error second
        check on the capability-effective path, naming pool, agent,
        capability, the vetoed anchor, and the repair path.
        """
        if tree.children and TASK_TOOL_NAME not in final.tools:
            children = ", ".join(child.name for child in tree.children)
            raise CapabilityError(
                f"capability 'subagents' on pool {tree.pool_name!r} agent "
                f"{tree.agent_name!r} has declared children ({children}) but "
                f"the final tool roster drops the {TASK_TOOL_NAME!r} tool — "
                "the subtree would be silently unreachable (V6 dual check). "
                "Repair: remove the 'tools: [-task]' veto (or the wholesale "
                "tools entry that omits it), or remove the children."
            )
        return super().bind(tree, config, final)

    def supply(self, view: PoolSupplyView) -> SubagentsSupply:
        """Build the pool's communication service — the retired BIZ
        ``create_pool`` construction FW-migrated (SPEC §8.4, OQ2's
        ratified capability-embedded option).

        Constructor parity with the retired BIZ site: the same
        skeleton/workspace objects (pool, tree, template registry,
        session registry, scope path, workspace manager, project dir,
        trace toggle) threaded through the aggregation's
        :class:`PoolSupplyView`; the router's ``source`` is the pool's
        ROOT agent address (per-sender identity resolves at send time
        from the runtime context — the tools carry their own source).
        The service-level ``target_store`` stays ``None``: the pool
        root's per-agent store binds at the root's native assembly.
        """
        if view.root_agent_name is None:
            raise ValueError(
                f"capability 'subagents' on pool {view.pool_name!r} cannot "
                "build its supply: the pool's root agent name is unavailable "
                "(the aggregation populates it from the pool's compiled spec "
                "set)"
            )
        if view.pool is None or view.session_tree is None:
            raise ValueError(
                f"capability 'subagents' on pool {view.pool_name!r} cannot "
                "build its supply: the pool assembly context carries no live "
                "AgentPool / session tree manager (the skeleton handles the "
                "service router constructs from)"
            )
        return SubagentsSupply(
            service=build_pool_communication_service(
                root_agent_name=view.root_agent_name,
                pool=view.pool,
                tree=view.session_tree,
                pool_name=view.pool_name,
                project_dir=view.project_dir,
                session_registry=view.session_registry,
                template_registry=view.template_registry,
                scope_path=view.scope_path,
                workspace_manager=view.workspace_manager,
                trace_enabled=view.trace_enabled,
            )
        )

    async def assemble(self, binding: CapabilityBinding, ctx: AgentContext) -> CapabilityWiring:
        """Per-agent wiring: the target store + the three section providers.

        The per-agent :class:`CommunicationTargetStore` is built from the
        DECLARED tree (the chain's ``pool_assembly_ctx.pool_spec`` — the
        retired BIZ root-store and ``AgentTemplate._comm_facilities``
        constructions converged): the pool ROOT always gets a store
        (direct children as SUBAGENT entries; possibly empty — peer
        NORMAL targets join it at workspace materialize), mid-level
        agents get a store of exactly their DIRECT children, leaves get
        ``None`` (their derived ``send_to_agent`` entry builds its own
        subagent-mode store).

        The root's assembly also binds the store onto the supply's
        service (the topology-gate fallback carrier for direct callers
        without a per-sender set — the control facade). The three
        section providers ride ``binding.active_sections`` (compile-time
        enablement — the retired provider's runtime tool-probing gates
        died with it).
        """
        pool_runtime = ctx.pool_runtime
        if pool_runtime is None:
            raise ValueError("pool_runtime must be filled by PoolAssembleStage")
        supply = require_subagents_supply(pool_runtime)
        pool_assembly = pool_runtime.pool_assembly_ctx
        if pool_assembly is None:
            raise ValueError(
                "subagents assemble requires the pool assembly context on "
                "the chain (pool_runtime.pool_assembly_ctx) — it carries the "
                "declared pool tree the per-agent target store derives from"
            )
        spec = ctx.spec
        is_main = spec is not None and spec.agent_type is AgentType.native_main
        children = [
            agent for agent in pool_assembly.pool_spec.agents if agent.parent == ctx.agent_name
        ]
        store: CommunicationTargetStore | None = None
        if children or is_main:
            store = CommunicationTargetStore()
            for child in children:
                store.add(
                    CommunicationTarget(
                        name=child.name,
                        kind=AgentCommKind.SUBAGENT,
                        description=child.description,
                        execution_strategy=ExecutionStrategyKind(child.execution_strategy),
                    )
                )
        if is_main and store is not None:
            supply.service.set_target_store(store)

        providers: list[SystemPromptProvider] = []
        for section in binding.active_sections:
            if section.section_id == _DELEGATION_SECTION_ID:
                providers.append(
                    _StaticBriefProvider(_DELEGATION_SECTION_VERSION, _DELEGATION_BRIEF)
                )
            elif section.section_id == _CONSULTATION_SECTION_ID:
                providers.append(
                    _StaticBriefProvider(_CONSULTATION_SECTION_VERSION, _CONSULTATION_BRIEF)
                )
            elif section.section_id == _PEER_SECTION_ID:
                # The peer section is root-only (the contribute gate); a
                # root is native_main, whose assembly always builds a store.
                if store is None:
                    raise ValueError(
                        "subagents.peer section requires the pool root's "
                        "per-agent target store (unreachable: the peer "
                        "section is contributed only for roots, whose "
                        "assembly always builds one)"
                    )
                providers.append(_PeerSectionProvider(store))
        return CapabilityWiring(
            prompt_providers=tuple(providers),
            artifacts={_TARGET_STORE_ARTIFACT: store},
        )
