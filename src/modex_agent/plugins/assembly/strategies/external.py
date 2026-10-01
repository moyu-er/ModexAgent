"""ExternalExecutionStrategy — assembles external pools (ADR-0025, ticket 4).

Promoted from ``examples/bot_project/bot/service/external_strategy.py`` (W4a,
plan SD-7): the relocation TODO in that module's docstring is closed here
(home decided by the layering tree — see the package docstring).

The strategy is stateless: ``assemble_main()`` is called once per pool at
build time. Since W5 it owns its runtime CONSTRUCTION end to end — the
former ``ExternalAwareFactory`` (a ``DefaultAgentFactory`` subclass that
stubbed the base class's react attributes and re-implemented
``create_agent``) is deleted: the strategy performs the
provider-availability gate (``shutil.which``), builds its typed products
(backend / parser / session map store / env spec), dispatches the declared
HOOK roster through the same ``dispatch_hooks`` the native path uses, and
constructs the ``ExternalAgent`` + ``ExternalTurnRunner`` + pipeline
directly through :func:`modex_agent.multi_agent.factory.assemble_external_pipeline`
— the SAME helper the subagent path uses. The built main returns as
``StrategyAssembly.main`` (:class:`MainAssembly`); ``create_pool``
registers it.

The former ``_PoolAssemblyMixin`` inheritance was vestigial (external
assembly builds none of the React-only collaborators) and is not carried
over.
"""

from __future__ import annotations

import logging
import os
import shutil
from collections.abc import Callable, Sequence
from pathlib import Path

from modex_agent.agents.external.agent import StreamingProviderBackend
from modex_agent.agents.external.backend_provider import PoolScopedBackendProvider
from modex_agent.agents.external.builder import ExternalAgentBuilder
from modex_agent.agents.external.child_discovery import (
    ExternalChildSessionDiscoverySink,
)
from modex_agent.agents.external.cli_resolver import resolve_modexctl_bin_dir
from modex_agent.agents.external.contracts import ProviderEventParser
from modex_agent.agents.external.events import ExternalEvent
from modex_agent.agents.external.providers.opencode.server_backend import (
    OpenCodeServerBackend,
)
from modex_agent.agents.external.providers.opencode.v2_parser import (
    OpenCodeV2EventParser,
)
from modex_agent.agents.external.types import (
    ExternalEnvSpec,
)
from modex_agent.core import AgentCommKind
from modex_agent.core.agent import ProviderKind
from modex_agent.core.emitter import ContentEmitter
from modex_agent.core.external_session import ExternalSessionMapStore
from modex_agent.core.llm_struct import RuntimeSafetyPolicy
from modex_agent.core.scope import RecordScope
from modex_agent.core.session_id import SessionIdFactory
from modex_agent.hook import HookErrorPolicy, HookRunner, HookSpec
from modex_agent.messaging.agent_messages import AgentAddress
from modex_agent.multi_agent.communication.peer_resolution import (
    PeerLink,
    build_agent_pool_map,
    build_routable_targets,
)
from modex_agent.multi_agent.descriptor import AgentDescriptor, AgentInstance, ContextStrategy
from modex_agent.multi_agent.execution_strategy import (
    ExecutionStrategy as ExecutionStrategyABC,
)
from modex_agent.multi_agent.execution_strategy import (
    MainAssembly,
    PoolAssemblyContext,
    StrategyAssembly,
    SubagentAssembly,
)
from modex_agent.multi_agent.factory import assemble_external_pipeline
from modex_agent.multi_agent.materialize_deps import AgentMaterializeDeps
from modex_agent.persistence.session_registry import SessionRegistry
from modex_agent.plugins.assembly.backend_factory import build_external_session_map_store
from modex_agent.plugins.assembly.context import AgentContext
from modex_agent.scope.execution_kind import strategy_name_of
from modex_agent.scope.runtime_ownership import BUNDLED_EXTERNAL_OWNERSHIP, RuntimeOwnership
from modex_agent.scope.spec import PoolSpec

logger = logging.getLogger(__name__)

__all__ = [
    "ExternalExecutionStrategy",
    "ProviderUnavailableError",
    "build_external_env_spec",
]


class ProviderUnavailableError(Exception):
    """Raised by :meth:`ExternalExecutionStrategy.assemble` when the
    provider CLI is not on ``PATH``.

    ``create_pool`` catches this to skip main-agent registration for the
    pool, leaving the pool structurally intact (broker bridge, communication
    tool, inbox) so other pools are unaffected.

    The ``executable`` attribute carries the CLI name that was missing so
    the pool factory can log the original-style warning message.
    """

    def __init__(self, executable: str) -> None:
        self.executable = executable
        super().__init__(f"Provider CLI {executable!r} not on PATH")


# ═══════════════════════════════════════════════════════════════════════════
# Child discovery collaborators (moved from the deleted bot ``_external_wiring.py``)
# ═══════════════════════════════════════════════════════════════════════════


def _build_child_discovery_collaborators(
    *,
    session_registry: SessionRegistry | None,
    session_map_store: ExternalSessionMapStore,
    provider_kind: ProviderKind,
    session_factory: SessionIdFactory,
) -> tuple[
    ExternalChildSessionDiscoverySink | None,
    Callable[[str], ContentEmitter[ExternalEvent]] | None,
]:
    """Build child-session discovery sink + emitter factory.

    Shared by the main-agent path (``ExternalExecutionStrategy.
    assemble_main``) and the subagent path
    (``ExternalExecutionStrategy._assemble_subagent``) so both get
    identical child-capture wiring.

    Returns ``(sink, emitter_factory)``. ``sink`` is ``None`` when
    ``session_registry`` is unavailable. ``emitter_factory`` is ``None`` —
    the WebUI-injected emitter factory is set later via ``set_emitter_factory``
    which propagates to ``set_child_emitter_factory`` on the agent.
    """
    sink: ExternalChildSessionDiscoverySink | None = None
    if session_registry is not None:
        sink = ExternalChildSessionDiscoverySink(
            session_factory=session_factory,
            session_registry=session_registry,
            session_map_store=session_map_store,
            provider_kind=provider_kind,
        )
    return sink, None


# ═══════════════════════════════════════════════════════════════════════════
# Module-level wiring helpers (moved from the deleted bot ``_external_wiring.py``).
# Kept module-level (not methods) because:
#  - ``build_external_env_spec`` is imported by tests directly.
#  - The helpers are pure functions with no strategy state; making them
#    methods would be a forced OOP wrapper.
# ═══════════════════════════════════════════════════════════════════════════


def _modexctl_bin_dir() -> Path:
    """Resolve the ``modexctl`` binary directory for the spawn ``PATH``.

    Delegates to :func:`modex_agent.agents.external.cli_resolver.resolve_modexctl_bin_dir`
    — the single source of truth. The previous inline ``shutil.which`` +
    ``Path(".")`` fallback (which never pointed at a real modexctl and
    caused silent cross-pool messaging failures when the bot was launched
    without the venv ``Scripts`` directory on PATH) is removed.

    Raises:
        ModexctlResolutionError: forwarded from the resolver when all four
            resolution strategies fail. Surfaced at pool assembly time.
    """
    return resolve_modexctl_bin_dir()


def build_external_env_spec(
    pool_name: str,
    pool_spec: PoolSpec,
    peer_links: Sequence[PeerLink],
    inbox_dir: Path,
    workspace_dir: Path,
    root_agent_name: str,
    control_origin: str,
) -> ExternalEnvSpec:
    """Build the ``ExternalEnvSpec`` for an external pool.

    Kept as a module-level function because tests import it directly.

    Dynamism across two time scales (no per-invocation dimension — main
    agents are assembled once at boot):
      • per-turn       — STATIC. ``ExternalAgent._run_turn`` refreshes
        only session_id + workdir via model_copy; agent_pool_map and
        targets are frozen for the agent's lifetime. (spec.md claims a
        per-turn refresh from CommunicationTargetStore — never implemented;
        known spec deviation.)
      • runtime-config — STATIC. the declared agents/peer links are read
        from the scope declaration at boot; a pool restart is required
        for changes to take effect here.

    The ``targets`` ⊆ ``agent_pool_map.keys()`` invariant holds: every
    name in targets (non-root agents + peer roots) has a pool_map entry,
    so ``modexctl send --to <any target>`` always resolves. The main
    agent's own name is in pool_map but not in targets (main never
    self-sends; modexctl rejects self-send explicitly).
    """
    return ExternalEnvSpec(
        workspace_root=workspace_dir,
        inbox_root=inbox_dir.parent,
        workdir=workspace_dir,
        session_id=f"__pending__.{root_agent_name}",
        agent_name=root_agent_name,
        provider_session_id="",
        agent_pool_map=build_agent_pool_map(pool_name, pool_spec, peer_links),
        targets=build_routable_targets(pool_spec, peer_links),
        modexctl_bin_dir=_modexctl_bin_dir(),
        control_origin=control_origin,
    )


# ═══════════════════════════════════════════════════════════════════════════
# Strategy
# ═══════════════════════════════════════════════════════════════════════════


class ExternalExecutionStrategy(ExecutionStrategyABC):
    """Assemble external pools (OpenCode CLI harness) — owns its runtime."""

    @property
    def name(self) -> str:
        return "external"

    @property
    def ownership(self) -> RuntimeOwnership:
        """External: the CLI harness owns model config, tools, memory,
        context, and approval; no subagent templates ride the pool.
        Returns the scope-owned bundled constant — the single
        declaration both the strategy and the compile-time fallback
        read."""
        return BUNDLED_EXTERNAL_OWNERSHIP

    def validate_pool_spec(self, pool: PoolSpec) -> None:
        """Reject pools incompatible with the external shape.

        Invariants (this method extends the ownership-derived base rule):

        * **No subagents** — derived from ``ownership.supports_subagents``
          (the base :meth:`ExecutionStrategy.validate_pool_spec`
          enforces it): external main agents have no tool surface and
          cannot dispatch subagent tasks.
        * **``provider_kind`` required** — the CLI kind (``opencode``)
          must be set so the strategy knows which backend + parser to build.

        Raises :class:`ValueError` on violation. This runs at pool-assembly
        time as defense-in-depth on top of declaration validation.
        """
        super().validate_pool_spec(pool)
        if pool.root_agent.provider_kind is None:
            raise ValueError(
                f"Pool {pool.name!r}: execution_strategy 'external' requires a provider_kind"
            )

    # ── Provider-kind / backend / parser resolution ──────────────────────

    def _read_provider_kind(self, pool_spec: PoolSpec) -> ProviderKind:
        """The declared root's provider kind (spec validation guarantees it
        is set for external pools; the historical pool.yml fallback died
        with the legacy road)."""
        provider_kind = pool_spec.root_agent.provider_kind
        if provider_kind is None:
            raise ValueError(
                f"Pool {pool_spec.name!r}: execution_strategy 'external' "
                "requires a provider_kind"
            )
        return provider_kind

    @staticmethod
    def _provider_executable_for(kind: ProviderKind) -> str:
        return kind.value

    def _build_external_backend(self, kind: ProviderKind) -> StreamingProviderBackend:
        if kind != ProviderKind.OPENCODE:
            raise ValueError(f"Unsupported provider_kind: {kind!r}")
        return OpenCodeServerBackend()

    def _build_external_parser(self, kind: ProviderKind) -> ProviderEventParser:
        if kind != ProviderKind.OPENCODE:
            raise ValueError(f"Unsupported provider_kind: {kind!r}")
        return OpenCodeV2EventParser()

    # ── Assemble ─────────────────────────────────────────────────────────

    async def _assemble_roster_hooks(self, ctx: PoolAssemblyContext) -> HookRunner | None:
        """PA-04: consume the DECLARED hook roster through ``dispatch_hooks``.

        The external main agent reuses the SAME declared-roster dispatch
        the native path uses — one mechanism, no hand-rolled
        session_title factory resolution. Reads the dispatch inputs
        (component registry + compiled assembly spec + workspace
        resources) off the pool assembly context; when they are absent
        (framework-style tests) the runner stays ``None`` — behavior
        unchanged.

        Only react-runner hooks are declared for this executor: the
        external CLI owns memory/tools, so memory hooks are structurally
        excluded (the native capability exclusions ride ``applies_to``).
        """
        from modex_agent.plugins.assembly.context import (
            AssemblyContext,
            agent_context_chain,
        )
        from modex_agent.plugins.assembly.native_core import dispatch_hooks

        registry = ctx.component_registry
        spec = ctx.assembly_spec
        if registry is None or spec is None or not spec.hooks:
            return None
        base = AssemblyContext(
            registry=registry,
            workspace_ctx=spec.workspace_ctx,
            workspace_resources=ctx.workspace_resources,
        )
        chain = agent_context_chain(base, spec=spec)
        hook_runner = HookRunner()
        await dispatch_hooks(spec, registry, chain, hook_runner, None)
        if not hook_runner.hook_specs:
            return None
        return hook_runner

    async def assemble_main(self, ctx: PoolAssemblyContext) -> StrategyAssembly:
        """Build the external main runtime directly (W5).

        Performs the provider-availability gate (``shutil.which`` →
        raises :class:`ProviderUnavailableError` when the CLI is missing),
        builds the typed external products (backend / parser / session
        map store / env spec — strategy-local, never a deps dict),
        dispatches the declared HOOK roster, and constructs the
        ``ExternalAgent`` + ``ExternalTurnRunner`` + pipeline through the
        shared :func:`assemble_external_pipeline` helper. The built main
        returns as ``StrategyAssembly.main``; ``create_pool`` registers
        it into the pool.

        Does NOT build provider/terminal_manager/tools/skill_resolver/
        context_manager/cassette_recorder/root_provider or any other
        react-only collaborator — ``ExternalTurnRunner`` doesn't use any
        of those.
        """
        from modex_agent.adapters.output import OutputAdapter
        from modex_agent.multi_agent.descriptor import AgentLLMConfig
        from modex_agent.plugins.assembly.native_core import LlmDefaults

        pool_name = ctx.pool_name
        pool_spec = ctx.pool_spec
        main_spec = pool_spec.root_agent
        peer_links = ctx.peer_links
        data_dir: Path = ctx.data_dir
        workspace_handle = ctx.workspace_handle

        # 1. Provider availability gate.
        provider_kind = self._read_provider_kind(pool_spec)
        executable = self._provider_executable_for(provider_kind)
        if shutil.which(executable) is None:
            raise ProviderUnavailableError(executable)

        # 2. Typed external products (backend/session_store/parser/env_spec).
        #    ``inbox_dir`` mirrors the path ``create_pool``
        #    computes (``data_dir / "inbox" / pool_name``) — the
        #    external env spec resolves inbox-relative paths from
        #    ``inbox_dir.parent``.
        scope_path = ctx.scope_path
        workspace_dir = ctx.project_dir
        if workspace_handle is not None:
            workspace_dir = workspace_handle.current
        if scope_path is not None:
            workspace_dir = scope_path.workspace_root
        inbox_dir = data_dir / "inbox" / pool_name
        session_store = build_external_session_map_store(
            ctx.app_config,
            ctx.persistence,
            workspace_dir,
            ctx.record_scope if ctx.record_scope is not None else RecordScope(),
        )
        spec = build_external_env_spec(
            pool_name,
            pool_spec,
            peer_links,
            inbox_dir,
            workspace_dir,
            main_spec.name,
            ctx.control_origin,
        )

        # 3. Descriptor (mirrors the retired orchestrator-side
        #    ``_register_external_main_agent`` construction byte-for-byte).
        llm_defaults = (
            ctx.model_assembly.default_llm_defaults()
            if ctx.model_assembly is not None
            else LlmDefaults()
        )
        descriptor = AgentDescriptor(
            address=AgentAddress(name=main_spec.name),
            llm_config=AgentLLMConfig(
                model=llm_defaults.model,
                temperature=llm_defaults.temperature,
                max_output_tokens=llm_defaults.max_output_tokens,
                reasoning_effort=llm_defaults.reasoning_effort,
                model_info=llm_defaults.model_info,
            ),
            system_prompt_template=ctx.root_system_prompt,
            max_iterations=main_spec.max_steps,
            execution_strategy=strategy_name_of(main_spec.execution_strategy),
            context_strategy=ContextStrategy.PERSISTENT,
            safety_policy=ctx.safety,
            comm_kind=AgentCommKind.NORMAL,
            memory_config=(ctx.assembly_deps.memory if ctx.assembly_deps is not None else None),
            roles=list(main_spec.roles),
            role_description=main_spec.description,
        )

        # 4. Agent — provider=None (ExternalAgentBuilder ignores it; the
        #    external CLI owns its own model configuration). ADR-0027: wrap
        #    the pool-scoped backend in a BackendProvider so the agent
        #    borrows it per turn instead of holding a fixed reference.
        session_id_factory = SessionIdFactory()
        child_discovery_sink, child_emitter_factory = _build_child_discovery_collaborators(
            session_registry=ctx.session_registry,
            session_map_store=session_store,
            provider_kind=provider_kind,
            session_factory=session_id_factory,
        )
        agent = ExternalAgentBuilder.build_agent(
            descriptor,
            provider=None,
            backend_provider=PoolScopedBackendProvider(
                self._build_external_backend(provider_kind)
            ),
            session_store=session_store,
            parser=self._build_external_parser(provider_kind),
            provider_kind=provider_kind,
            spec=spec,
            base_env=dict(os.environ),
            child_discovery_sink=child_discovery_sink,
            session_registry=ctx.session_registry,
            session_id_factory=session_id_factory,
            child_emitter_factory=child_emitter_factory,
        )

        # 5. Pipeline via the shared helper (converged with the subagent
        #    path) — same construction the deleted ExternalAwareFactory used.
        instance: AgentInstance = assemble_external_pipeline(
            descriptor,
            agent,
            broker=ctx.broker,
            safety=ctx.safety,
            hook_runner=await self._assemble_roster_hooks(ctx),
            session_registry=ctx.session_registry,
            control_channel=ctx.control_channel,
            output_adapter=(
                ctx.output_adapter if isinstance(ctx.output_adapter, OutputAdapter) else None
            ),
            context_manager=None,
            session_binding_store=None,
        )

        # 6. Return the strategy-built main; create_pool registers it.
        return StrategyAssembly(main=MainAssembly(descriptor, instance))

    async def assemble_sub(
        self,
        ctx: AgentContext,
        deps: AgentMaterializeDeps,
    ) -> SubagentAssembly:
        """Assemble an external subagent — absorbs the 7-step logic from the
        deleted ``BotSubagentExternalBuilder.build()`` (ADR-0027 convergence).

        The 7 steps: (1) env_spec (2) session_store (3) parser (4) backend
        (5) child_discovery (6) ExternalAgent (7) HookRunner carrying the
        auto-send hook (the HOOK-slot factory, resolved explicitly —
        external subagents never run the native roster dispatch). Pipeline
        assembly (``assemble_pipeline``) runs here too — the caller
        (``AgentTemplate.materialize``) only injects the emitter +
        registers the returned pair into the pool.

        Ticket 10: the per-invocation data (``parent_session``,
        ``invocation_id``, agent identity, and the per-agent spec
        reference) is read from the full-chain :class:`AgentContext`;
        ``deps`` carries the per-pool materialize connections (tree,
        broker, session registry, path resolver) the deleted
        special-case context's ``factories`` field used to mirror.

        Returns a :class:`SubagentAssembly` carrying the ``AgentDescriptor``
        and the fully-built ``AgentInstance`` (external agent + minimal
        pipeline + the auto-send hook on the pipeline's hook runner).
        """
        spec = ctx.spec
        if spec is None:
            raise ValueError(
                "AgentContext.spec is None — cannot assemble external "
                "subagent without the per-agent spec reference"
            )

        agent_name = ctx.agent_name
        parent_session_str = str(ctx.parent_session) if ctx.parent_session else ""
        parent_name = parent_session_str.split(".")[-1] if parent_session_str else ""
        session_id = f"{ctx.invocation_id or ''}.{agent_name}"

        scope_path = deps.scope_path
        pool_name = scope_path.pool_name if scope_path is not None and scope_path.pool_name else "main"
        project_dir = deps.project_dir or Path(".")
        workspace_dir = (
            scope_path.workspace_root if scope_path is not None else project_dir
        )
        data_dir = deps.data_dir or workspace_dir / ".modex"
        inbox_root = data_dir / "inbox"

        descriptor = AgentDescriptor(
            address=AgentAddress(name=agent_name),
            execution_strategy=strategy_name_of(spec.execution_strategy),
            provider_kind=ProviderKind(spec.provider_kind) if spec.provider_kind else None,
            comm_kind=AgentCommKind.SUBAGENT,
            max_iterations=spec.max_iterations,
            system_prompt_template="",
            safety_policy=deps.safety,
            roles=list(spec.roles),
            role_description=spec.description,
        )

        agent_pool_map: dict[str, str] = {agent_name: pool_name}
        if parent_name:
            agent_pool_map[parent_name] = pool_name
        env_spec = ExternalEnvSpec(
            workspace_root=workspace_dir,
            inbox_root=inbox_root,
            workdir=workspace_dir,
            session_id=session_id,
            agent_name=agent_name,
            provider_session_id="",
            agent_pool_map=agent_pool_map,
            targets=[(parent_name, "")] if parent_name else [],
            comm_kind=AgentCommKind.SUBAGENT,
            parent_session_id=parent_session_str or None,
            modexctl_bin_dir=resolve_modexctl_bin_dir(),
            control_origin=deps.control_origin,
        )

        session_store = build_external_session_map_store(
            deps.app_config,
            deps.persistence,
            workspace_dir,
            deps.record_scope if deps.record_scope is not None else RecordScope(),
        )

        provider_kind = ProviderKind(spec.provider_kind) if spec.provider_kind else ProviderKind.OPENCODE
        parser = self._build_external_parser(provider_kind)
        backend_provider = PoolScopedBackendProvider(
            self._build_external_backend(provider_kind)
        )

        child_sink, child_emitter_factory = _build_child_discovery_collaborators(
            session_registry=deps.session_registry,
            session_map_store=session_store,
            provider_kind=provider_kind,
            session_factory=deps.session_factory or SessionIdFactory(),
        )

        agent = ExternalAgentBuilder.build_agent(
            descriptor,
            provider=None,
            backend_provider=backend_provider,
            session_store=session_store,
            parser=parser,
            provider_kind=provider_kind,
            spec=env_spec,
            base_env=dict(os.environ),
            child_discovery_sink=child_sink,
            session_registry=deps.session_registry,
            session_id_factory=deps.session_factory or SessionIdFactory(),
            child_emitter_factory=child_emitter_factory,
        )

        # The auto-send hook rides the SAME HOOK-slot factory the native
        # roster dispatch uses (the chain carries the tree, the declared
        # parent, the runtime dir, and the per-agent spec the factory
        # derives its fields from) — external subagents never run the
        # native capability dispatch, so the strategy resolves the
        # factory explicitly; the per-agent construction logic has ONE
        # home (plugins/defaults/hooks.py).
        from modex_agent.scope.components import ComponentSlot

        hook_factory = ctx.registry.resolve(ComponentSlot.HOOK, "subagent_auto_send")
        hook = await hook_factory.create(hook_factory.config_model.model_validate({}), ctx)
        hook_runner = HookRunner()
        hook_runner.add(
            HookSpec(
                hook=hook,
                on_error=HookErrorPolicy.LOG,
            )
        )

        instance = assemble_external_pipeline(
            descriptor,
            agent,
            broker=deps.broker,
            safety=deps.safety or RuntimeSafetyPolicy(),
            hook_runner=hook_runner,
            session_registry=deps.session_registry,
            control_channel=None,
            output_adapter=None,
            context_manager=None,
            session_binding_store=None,
        )

        return SubagentAssembly(descriptor=descriptor, instance=instance)
