"""AgentTemplate — preset definition + subagent construction entry.

``AgentTemplate.materialize`` folds the old
``AgentCommunicationService._create_dynamic_subagent`` god-method into a deep
module on the template (ADR-0015 D3, Design B). It is the subagent-only
construction path: normals are registered by business wiring via factory
defaults, never via materialize. ``comm_kind`` is always ``SUBAGENT``;
``parent_session`` gates the FORK context feature.

Construction is direct (not via ``AssemblyPipeline``): the pipeline is a
per-pool main-agent orchestrator (stages 1-3, SPEC Errata-5), while subagent
construction needs per-invocation data (``parent_session``,
``invocation_id``, materialize deps). Since ticket 10 the per-invocation
data rides the per-agent ``AgentContext`` chain carrier — the same
mechanism the native core uses — alongside the per-pool materialize deps.

W5 template/plugins inversion: the concrete assembly body (native
component assembly through the plugins assembly core, ``EXTERNAL``
dispatch through the strategy registry) lives BEHIND the
:class:`~modex_agent.multi_agent.materializer.AgentMaterializer` seam —
injected onto :class:`AgentMaterializeDeps` by the pool assembly wiring
(``create_pool``). This module keeps what it owns: the delegation
snapshot + sandbox resolution, the materializer dispatch, and the shared
delegation boundary every strategy gets.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from modex_agent.core.tool_vocabulary import ToolPreset
from modex_agent.memory.config import MemoryConfig
from modex_agent.multi_agent.materializer import (
    StaticDelegationRootProvider,
    subagent_workspace_root,
)
from modex_agent.scope.spec import AgentSpec

if TYPE_CHECKING:
    from modex_agent.core.session_id import SessionInfo
    from modex_agent.core.turn.approval_decision import ApprovalAuditStore
    from modex_agent.multi_agent.descriptor import AgentInstance
    from modex_agent.multi_agent.materialize_deps import AgentMaterializeDeps
    from modex_agent.sandbox.delegation import DelegationSnapshot
    from modex_agent.sandbox.settings import SandboxSettings
    from modex_agent.scope.assembly_spec import AssemblySpec
    from modex_agent.scope.spec import PoolSpec


logger = logging.getLogger(__name__)


def _declared_depth(pool_spec: PoolSpec, agent_name: str) -> int:
    """Delegation depth from the declared tree (root = 0, spawn +1).

    Walks the ``parent`` chain up to the root; the chain length IS the
    generation. An unknown name or a broken chain stops at 0 — the
    runtime budget check (task dispatch) reads the snapshot's depth, so
    a hand-built context without a declared tree simply reports depth 0.
    """
    depth = 0
    seen: set[str] = set()
    current: str | None = agent_name
    while current is not None and current not in seen:
        seen.add(current)
        parent = next(
            (agent.parent for agent in pool_spec.agents if agent.name == current),
            None,
        )
        if parent is None:
            return depth
        depth += 1
        current = parent
    return depth


def _pool_sandbox_settings(deps: AgentMaterializeDeps) -> SandboxSettings | None:
    """The pool root's declared sandbox settings, including dormant tiers.

    Reads the same ``interceptor_configs["sandbox_guard"]`` declaration
    the interceptor factory consumes (one declaration, two assemblies —
    the ``_declared_sandbox_settings`` pattern from the bot's pipeline
    wiring). ``None`` only when no section is declared. DEFAULT does not
    activate a substrate, but the declared permission face is preserved
    through :func:`resolve_agent_sandbox` for delegation.
    """
    from modex_agent.sandbox.settings import SandboxSettings

    pool_assembly = deps.pool_assembly_ctx
    if pool_assembly is None:
        return None
    raw = (pool_assembly.pool_spec.root_agent.interceptor_configs or {}).get(
        "sandbox_guard"
    )
    if raw is None:
        return None
    section = raw.get("sandbox", {}) if isinstance(raw, dict) else {}
    settings = SandboxSettings.model_validate(section)
    return settings


@dataclass
class AgentTemplate:
    """Preset definition for a dynamically creatable subagent type.

    Communication tools (``send_to_agent``) arrive via the derived roster
    entries the ``subagents`` capability injects at compile time — they
    resolve through the TOOL-slot factories at assembly, never by
    materialize-time side registration.

    ``toolset_profile`` is the node's RESOLVED toolset profile (position
    default + ``toolset`` override) — the read-only guard reads it.
    ``context_mode`` controls memory inheritance. ``mcp`` lists registry
    server names resolved via ``bot.config.mcp_registry``.

    ``compiled_spec`` is the ScopeCompiler's per-agent
    :class:`AssemblySpec` — REQUIRED for materialization (the declaration
    is the assembly input; there is no roster re-derivation road).
    """

    spec: AgentSpec
    toolset_profile: ToolPreset = ToolPreset.READ_WRITE
    memory: MemoryConfig | None = None
    compiled_spec: AssemblySpec | None = None
    children: tuple[AgentSpec, ...] = ()
    """Declared DIRECT children (SPEC §3.2) — non-empty only for mid-level
    agents of a nested declaration tree. The ``subagents`` capability's
    assemble reads the DECLARED pool tree (the chain's pool assembly
    context) for the same children when building the per-agent
    ``CommunicationTargetStore`` the derived ``task`` TOOL-slot factory
    resolves against; grandchildren never appear here (each child
    dispatches its own)."""

    async def materialize(
        self,
        parent_session: SessionInfo | str | None,
        invocation_id: str | None,
        deps: AgentMaterializeDeps,
    ) -> AgentInstance:
        """Validate before building; every strategy shares post-build delegation metadata."""
        from modex_agent.sandbox.delegation import DelegationSnapshot, resolve_agent_sandbox

        materializer = deps.materializer
        if materializer is None:
            raise ValueError(
                f"Subagent {self.spec.name!r} cannot materialize: no "
                "AgentMaterializer is wired on AgentMaterializeDeps (the "
                "pool assembly wiring injects the concrete implementation "
                "at create_pool time)"
            )
        root = subagent_workspace_root(deps)
        pool_settings = _pool_sandbox_settings(deps)
        settings = resolve_agent_sandbox(self.spec.sandbox, pool_settings, root)
        snapshot = DelegationSnapshot(
            workspace_root=root, settings=settings, depth=self._declared_depth(deps),
        )
        instance = await materializer.materialize(
            self,
            parent_session,
            invocation_id,
            deps,
            snapshot=snapshot,
            settings=settings,
        )
        try:
            await self._wire_delegation_boundary(
                instance, snapshot, settings, approval_audit=deps.approval_audit,
            )
        except BaseException as failure:
            try:
                stopped = await instance.stop()
                if not stopped:
                    failure.add_note(
                        "Agent cleanup retained resources because turns did not drain"
                    )
            except BaseException as cleanup_error:
                if cleanup_error is not failure:
                    failure.add_note(
                        f"Agent cleanup after delegation wiring failure also failed: "
                        f"{cleanup_error!r}"
                    )
            raise
        return instance

    async def _wire_delegation_boundary(
        self,
        instance: AgentInstance,
        snapshot: DelegationSnapshot,
        settings: SandboxSettings,
        *,
        approval_audit: ApprovalAuditStore | None,
    ) -> None:
        """Report real capabilities; install checks only where the runner executes them."""
        from dataclasses import replace as _replace

        from modex_agent.approval.security import guard_only_runtime
        from modex_agent.core.agent import ExecutionStrategyKind
        from modex_agent.runtime.services import AgentRuntimeServices
        from modex_agent.sandbox.decision import SecurityDecisionService
        from modex_agent.sandbox.delegation import (
            delegation_denial_message,
        )
        from modex_agent.sandbox.settings import SandboxBackend
        from modex_agent.sandbox.shell_plan import resolved_substrate
        from modex_agent.sandbox.types import EnforcementLevel
        from modex_agent.scope.execution_kind import strategy_name_of

        builder = instance.pipeline._turn_runner.turn_context_builder if instance.pipeline else None
        native = strategy_name_of(self.spec.execution_strategy) != ExecutionStrategyKind.EXTERNAL
        checks_run = native and builder is not None
        resolved = await resolved_substrate(instance.pipeline.interceptor_chain) if checks_run and instance.pipeline else None
        limits = (
            "Shell/input guards are best effort, not containment of dynamic code; HOST has no kernel isolation.",
            "Only catalogued file targets are checked; custom/MCP tools and secondary tool effects are not contained.",
        ) if checks_run else (
            "Provider-hosted tools bypass framework guards; no provider-neutral permission capability is available. "
            "Declared roots/surface are metadata only, not enforced; provider kernel enforcement is unknown.",
        )
        snapshot = snapshot.model_copy(update={
            "backend": resolved.backend if resolved else (SandboxBackend.HOST if checks_run else None),
            "enforcement": resolved.enforcement if resolved else (EnforcementLevel.NONE if checks_run else None),
            "file_guards": checks_run,
            "limitations": (*limits, *((resolved.degraded_reason,) if resolved and resolved.degraded_reason else ())),
        })
        instance.delegation = snapshot
        if not checks_run:
            logger.warning("Delegation %s: %s", self.spec.name, limits[0])
            return

        guard_only = guard_only_runtime(
            decision=SecurityDecisionService(
                settings=settings,
                workspace_root_provider=StaticDelegationRootProvider(snapshot.workspace_root),
            ),
            deny_message_builder=lambda reason, tool_name, target: delegation_denial_message(
                tool_name, target, snapshot
            ),
        )
        assert builder is not None
        base = builder.runtime_services
        builder.runtime_services = (
            _replace(
                base,
                approval=guard_only,
                guard_only_approval=guard_only,
                delegation=snapshot,
                approval_audit=approval_audit,
            )
            if base is not None
            else AgentRuntimeServices(
                approval=guard_only,
                guard_only_approval=guard_only,
                delegation=snapshot,
                approval_audit=approval_audit,
            )
        )

    def _declared_depth(self, deps: AgentMaterializeDeps) -> int:
        """This subagent's delegation depth from the declared pool tree."""
        pool_assembly = deps.pool_assembly_ctx
        if pool_assembly is None:
            return 0
        return _declared_depth(pool_assembly.pool_spec, self.spec.name)
