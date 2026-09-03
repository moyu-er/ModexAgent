"""ExternalExecutionStrategy - assembles external pools (ADR-0025, ticket 4).

Transitional home: lives in ``examples/bot_project/bot/service/`` (NOT in
``src/modex_agent/agents/external/``) because ``assemble()`` calls the
bot-side wiring helpers (now inlined here from the deleted
``_external_wiring.py``) which use bot-layer types (``PoolSpec``,
``AppConfig``, persistence manager). A future ticket may relocate this class
to ``src/modex_agent/agents/external/strategy.py`` once the bot-layer
dependencies are abstracted away.

The strategy is stateless: ``assemble()`` is called once per pool at build
time. It performs the provider-availability gate (``shutil.which``) and
builds the ``external_deps`` dict that ``ExternalAwareFactory``
reads to construct an ``ExternalAgent``. The dict is returned in a
:class:`StrategyAssembly`'s transitional ``external_deps`` field.

Ticket 6: the ``_external_wiring.py`` file is deleted; its content
(``build_external_deps``, ``read_provider_kind``,
``provider_executable_for``, ``build_external_backend``,
``build_external_parser``, ``build_external_env_spec``,
``ExternalAwareFactory``) lives here as private methods/classes. The
strategy also inherits :class:`_PoolAssemblyMixin`, but external assembly
builds none of the React-only provider, terminal, tool, or skill-resolver
collaborators.

``agent`` and ``turn_runner`` are ``None`` - the ``ExternalAgent``
instance + ``ExternalTurnRunner`` are created downstream by the factory +
pipeline.
"""

from __future__ import annotations

import logging
import os
import shutil
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from bot.config.webui_config import build_control_origin
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
from modex_agent.agents.external.session_store import ExternalSessionMapStore
from modex_agent.agents.external.types import (
    ExternalEnvSpec,
)
from modex_agent.core import AgentCommKind
from modex_agent.core.agent import ExecutionStrategyKind, ProviderKind
from modex_agent.core.emitter import ContentEmitter
from modex_agent.core.llm_struct import RuntimeSafetyPolicy
from modex_agent.core.provider import LLMProvider
from modex_agent.core.session_id import SessionIdFactory
from modex_agent.hook import HookErrorPolicy, HookRunner, HookSpec
from modex_agent.multi_agent.address import AgentAddress
from modex_agent.multi_agent.communication.peer_resolution import (
    PeerLink,
    build_agent_pool_map,
    build_routable_targets,
)
from modex_agent.multi_agent.descriptor import AgentDescriptor, AgentInstance
from modex_agent.multi_agent.execution_strategy import (
    ExecutionStrategy as ExecutionStrategyABC,
)
from modex_agent.multi_agent.execution_strategy import (
    PoolAssemblyContext,
    StrategyAssembly,
    SubagentAssembly,
)
from modex_agent.multi_agent.factory import DefaultAgentFactory
from modex_agent.multi_agent.materialize_deps import AgentMaterializeDeps
from modex_agent.persistence.session_registry import SessionRegistry
from modex_agent.plugins.assembly.context import AgentContext
from modex_agent.scope.spec import PoolSpec

from .builders import _PoolAssemblyMixin

logger = logging.getLogger(__name__)

__all__ = [
    "ExternalAwareFactory",
    "ExternalExecutionStrategy",
    "ProviderUnavailableError",
    "build_external_env_spec",
]


class ProviderUnavailableError(Exception):
    """Raised by :meth:`ExternalExecutionStrategy.assemble` when the
    provider CLI is not on ``PATH``.

    ``pool_builder.create_pool`` catches this to skip main-agent registration
    for the pool, leaving the pool structurally intact (broker bridge,
    communication tool, inbox) so other pools are unaffected. Behavior
    equivalent to today's inline ``shutil.which`` skip-pool path.

    The ``executable`` attribute carries the CLI name that was missing so
    ``pool_builder`` can log the original-style warning message.
    """

    def __init__(self, executable: str) -> None:
        self.executable = executable
        super().__init__(f"Provider CLI {executable!r} not on PATH")


# ═══════════════════════════════════════════════════════════════════════════
# Backend (moved from _external_wiring.py)
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

    Shared by the main-agent path (``ExternalAwareFactory.create_agent``)
    and the subagent path (``ExternalExecutionStrategy._assemble_subagent``)
    so both get identical child-capture wiring.

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
# Factory (moved from _external_wiring.py)
# ═══════════════════════════════════════════════════════════════════════════


class ExternalAwareFactory(DefaultAgentFactory):
    """``DefaultAgentFactory`` subclass that builds ``ExternalAgent`` instances.

    Fully overrides :meth:`create_agent` to skip ALL react-only construction:
    no ``BotModelProvider``, no ``FilteredToolManager``, no ``SkillResolver``,
    no ``TurnContextBuilder``, no ``ApprovalResumer``/``ApprovalRenderer``,
    no hooks, no ``RuntimeContextManager``. ``ExternalTurnRunner`` does not
    use any of those — it builds a minimal ``AgentContext`` (empty history +
    empty ``InMemoryToolManager``) and calls ``agent.run()`` directly.

    Overrides :meth:`__init__` to skip ``DefaultAgentFactory.__init__`` (which
    builds react-only defaults like ``RuntimeContextManager`` and
    ``InboxProducer``/``InboxConsumer``). Accepts the same kwargs for signature
    compatibility with ``_build_agent_factory`` but ignores the react-only
    ones.
    """

    def __init__(
        self,
        *args: Any,
        external_deps: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        # Do NOT call super().__init__() — it builds react-only defaults
        # (RuntimeContextManager, InboxProducer/Consumer, etc.) that
        # ExternalTurnRunner never uses.
        self._external_deps: dict[str, Any] = external_deps or {}
        self._control_channel = kwargs.get("control_channel")
        self._session_registry = kwargs.get("session_registry")
        # Stub the attributes the base class would set, in case any code path
        # reads them via super() (defense-in-depth; ExternalTurnRunner doesn't
        # use them, but a future base-class method could). The retired
        # observability_config / trace_store kwargs died with the `tracing`
        # capability convergence (span hooks are capability-contributed roster
        # entries; external agents take no native hook surface).
        self._default_llm_provider = kwargs.get("default_llm_provider")
        self._default_tool_manager = kwargs.get("default_tool_manager")
        self._sanitizer = kwargs.get("sanitizer")
        self._command_interceptor = kwargs.get("command_interceptor")
        self._subagent_service = kwargs.get("subagent_service")
        self._inbox_server = kwargs.get("inbox_server")
        self._default_hooks = list(kwargs.get("default_hooks") or [])
        self._default_hook_runner = kwargs.get("default_hook_runner")
        self._default_interceptor_chain = kwargs.get("default_interceptor_chain")
        self._default_turn_store = kwargs.get("default_turn_store")
        self._session_binding_store = kwargs.get("session_binding_store")
        self._inbox_producer = None
        self._inbox_consumer = None
        self._runtime_context_manager: Any = None

    async def create_agent(
        self,
        descriptor: AgentDescriptor,
        session_id: str | None = None,
        context_manager: Any | None = None,
        broker: Any | None = None,
        tool_manager: Any | None = None,  # ignored — external uses empty InMemoryToolManager
        skill_resolver: Any | None = None,  # ignored
        sanitizer: Any | None = None,  # ignored
        command_interceptor: Any | None = None,  # ignored
        subagent_service: Any | None = None,  # ignored
        hooks: list[Any] | None = None,  # ignored — ExternalTurnRunner doesn't dispatch hooks
        output_adapter: Any | None = None,
        context_manager_factory: Any | None = None,  # ignored
        llm_provider: LLMProvider | None = None,  # ignored — the external CLI owns model config
    ) -> AgentInstance:
        """Build an ExternalAgent + ExternalTurnRunner + minimal pipeline.

        Overrides :meth:`DefaultAgentFactory.create_agent` to skip ALL
        react-only construction. ``ExternalTurnRunner`` doesn't use any of
        those objects — it builds a minimal ``AgentContext`` (empty history +
        empty ``InMemoryToolManager``) and calls ``agent.run()`` directly.
        The external agent communicates via ``modexctl send`` CLI, not
        the ``task`` tool.
        """
        from modex_agent.adapters.output import OutputAdapter
        from modex_agent.core.llm_struct import RuntimeSafetyPolicy
        from modex_agent.core.session_id import SessionIdFactory

        # 1. Agent — provider=None (ExternalAgentBuilder ignores it;
        #    the external CLI owns its own model configuration). ADR-0027:
        #    wrap the pool-scoped backend in a BackendProvider so the agent
        #    borrows it per turn instead of holding a fixed reference.
        deps = self._external_deps
        missing = [
            name
            for name in ("backend", "session_store", "parser", "provider_kind", "spec")
            if deps.get(name) is None
        ]
        if missing:
            raise ValueError(f"ExternalAwareFactory missing external deps: {', '.join(missing)}")
        backend_provider = PoolScopedBackendProvider(deps["backend"])

        session_id_factory = SessionIdFactory()
        child_discovery_sink, child_emitter_factory = _build_child_discovery_collaborators(
            session_registry=self._session_registry,
            session_map_store=deps["session_store"],
            provider_kind=deps["provider_kind"],
            session_factory=session_id_factory,
        )

        agent = ExternalAgentBuilder.build_agent(
            descriptor,
            provider=None,
            backend_provider=backend_provider,
            session_store=deps["session_store"],
            parser=deps["parser"],
            provider_kind=deps["provider_kind"],
            spec=deps["spec"],
            base_env=deps.get("base_env"),
            child_discovery_sink=child_discovery_sink,
            session_registry=self._session_registry,
            session_id_factory=session_id_factory,
            child_emitter_factory=child_emitter_factory,
        )

        # 2. Assemble pipeline via shared helper (converged with subagent path).
        safety: RuntimeSafetyPolicy = descriptor.safety_policy or RuntimeSafetyPolicy()
        return ExternalAgentBuilder.assemble_pipeline(
            descriptor,
            agent,
            broker=broker,
            safety=safety,
            hook_runner=None,  # main agents don't fire FINALLY_GRAPH
            session_registry=self._session_registry,
            control_channel=self._control_channel,
            output_adapter=output_adapter if isinstance(output_adapter, OutputAdapter) else None,
            context_manager=context_manager,
            session_binding_store=self._session_binding_store,
        )


# ═══════════════════════════════════════════════════════════════════════════
# Module-level wiring helpers (moved from _external_wiring.py).
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
    project_dir: Path,
    inbox_dir: Path,
    workspace_dir: Path,
    root_agent_name: str,
) -> ExternalEnvSpec:
    """Build the ``ExternalEnvSpec`` for an external pool.

    Kept as a module-level function because tests import it directly
    (``test_builders_inbox.py``).

    Dynamism across two time scales (no per-invocation dimension — main
    agents are assembled once at boot):
      • per-turn       — STATIC. ``ExternalAgent._run_turn`` refreshes
        only session_id + workdir via model_copy; agent_pool_map and
        targets are frozen for the agent's lifetime. (spec.md claims a
        per-turn refresh from CommunicationTargetStore — never implemented;
        known spec deviation.)
      • runtime-config — STATIC. the declared agents/peer links are read
        from the scope declaration at bot boot; a pool restart is required
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
        control_origin=build_control_origin(project_dir / "config"),
    )


# ═══════════════════════════════════════════════════════════════════════════
# Strategy
# ═══════════════════════════════════════════════════════════════════════════


class ExternalExecutionStrategy(_PoolAssemblyMixin, ExecutionStrategyABC):
    """Assemble external pools (Pi / OpenCode CLI harness).

    Inherits the shared ``_build_*`` helpers from :class:`_PoolAssemblyMixin`
    so ``assemble()`` can build the placeholder provider/terminal/tools/skill
    that ``ExternalAwareFactory`` still requires as constructor args
    (behavior preservation per ticket 6).
    """

    @property
    def name(self) -> str:
        return "external"

    @property
    def supports_subagents(self) -> bool:
        return False

    @property
    def requires_main_agent_tools(self) -> bool:
        return False

    @property
    def requires_llm_provider(self) -> bool:
        return False

    def validate_pool_spec(self, pool: PoolSpec) -> None:
        """Reject pools incompatible with the external shape.

        Two invariants (this method is the single enforcement point — the
        legacy store-side branches were deleted in ticket 6):

        * **No subagents** - external main agents have no tool surface
          and cannot dispatch subagent tasks. Subagent templates on an
          external pool are a configuration error.
        * **``provider_kind`` required** - the CLI kind (``pi`` / ``opencode``)
          must be set so the strategy knows which backend + parser to build.

        Raises :class:`ValueError` on violation. This runs at pool-assembly
        time as defense-in-depth on top of declaration validation.
        """
        if len(pool.agents) > 1:
            raise ValueError(
                f"Pool {pool.name!r}: execution_strategy 'external' does not support subagents"
            )
        if pool.root_agent.provider_kind is None:
            raise ValueError(
                f"Pool {pool.name!r}: execution_strategy 'external' requires a provider_kind"
            )

    # ── Provider-kind / backend / parser resolution ──────────────────────
    # (moved from _external_wiring.py as private methods)

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
    def _provider_kind_from_str(value: str) -> ProviderKind:
        if value == ProviderKind.OPENCODE.value:
            return ProviderKind.OPENCODE
        raise ValueError(f"Unsupported provider_kind: {value!r} (only 'opencode' is supported)")

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

    def _build_external_deps(
        self,
        *,
        pool_name: str,
        pool_spec: PoolSpec,
        peer_links: Sequence[PeerLink],
        project_dir: Path,
        inbox_dir: Path,
        workspace_dir: Path,
        root_agent_name: str,
        base_env: dict[str, str] | None = None,
        app_config: Any | None = None,
        persistence: Any | None = None,
    ) -> dict[str, Any]:
        provider_kind = self._read_provider_kind(pool_spec)
        backend = self._build_external_backend(provider_kind)
        parser = self._build_external_parser(provider_kind)
        from bot.scope import BotRecordScope
        from bot.service.builders import build_external_session_map_store

        session_store = build_external_session_map_store(
            app_config,
            persistence,
            workspace_dir,
            BotRecordScope(pool=pool_name),
        )
        spec = build_external_env_spec(
            pool_name,
            pool_spec,
            peer_links,
            project_dir,
            inbox_dir,
            workspace_dir,
            root_agent_name,
        )
        return {
            "backend": backend,
            "session_store": session_store,
            "parser": parser,
            "provider_kind": provider_kind,
            "spec": spec,
            "base_env": dict(base_env) if base_env is not None else dict(os.environ),
        }

    # ── Assemble ─────────────────────────────────────────────────────────

    async def assemble_main(self, ctx: PoolAssemblyContext) -> StrategyAssembly:
        """Build external deps only; all react-only fields are ``None``.

        Performs the provider-availability gate (``shutil.which`` -> raises
        :class:`ProviderUnavailableError` when the CLI is missing) and builds
        the ``external_deps`` dict that
        :meth:`ExternalAwareFactory.create_agent` reads to construct an
        ``ExternalAgent`` + ``ExternalTurnRunner`` + minimal pipeline.

        Does NOT build provider/terminal_manager/tools/skill_resolver/
        context_manager/cassette_recorder/root_provider or any other
        react-only collaborator — ``ExternalTurnRunner`` doesn't use any of
        those. ``ExternalAwareFactory.create_agent`` constructs the
        agent + turn_runner + pipeline directly from
        ``external_deps``.
        """
        pool_name = ctx.pool_name
        pool_spec = ctx.pool_spec
        peer_links = ctx.peer_links
        project_dir: Path = ctx.project_dir
        data_dir: Path = ctx.data_dir
        workspace_handle = ctx.workspace_handle

        # 1. Provider availability gate.
        provider_kind = self._read_provider_kind(pool_spec)
        executable = self._provider_executable_for(provider_kind)
        if shutil.which(executable) is None:
            raise ProviderUnavailableError(executable)

        # 2. External deps (backend/session_store/parser/env_spec).
        #    ``inbox_dir`` mirrors the path ``pool_builder.create_pool``
        #    computes (``data_dir / "inbox" / pool_name``) — the
        #    external env spec resolves inbox-relative paths from
        #    ``inbox_dir.parent``.
        workspace_dir = workspace_handle.current if workspace_handle is not None else project_dir
        inbox_dir = data_dir / "inbox" / pool_name
        external_deps = self._build_external_deps(
            pool_name=pool_name,
            pool_spec=pool_spec,
            peer_links=peer_links,
            project_dir=project_dir,
            inbox_dir=inbox_dir,
            workspace_dir=workspace_dir,
            root_agent_name=pool_spec.root_agent.name,
            base_env=dict(os.environ),
            app_config=ctx.app_config,
            persistence=ctx.persistence,
        )

        # 3. Return assembly with ONLY ``external_deps`` populated.
        #    All react-only fields default to ``None`` (built by the factory,
        #    not the strategy).
        return StrategyAssembly(
            agent=None,
            turn_runner=None,
            external_deps=external_deps,
            extra_cleanup=(),
        )

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

        from bot.scope import BotRecordScope
        from bot.service.builders import build_external_session_map_store

        agent_name = ctx.agent_name
        parent_session_str = str(ctx.parent_session) if ctx.parent_session else ""
        parent_name = parent_session_str.split(".")[-1] if parent_session_str else ""
        session_id = f"{ctx.invocation_id or ''}.{agent_name}"

        scope_path = deps.scope_path
        pool_name = scope_path.pool_name if scope_path is not None and scope_path.pool_name else "main"
        project_dir = deps.project_dir or Path(".")
        data_dir = deps.data_dir or project_dir / ".modex"
        workspace_dir = project_dir
        inbox_root = data_dir / "inbox"

        descriptor = AgentDescriptor(
            address=AgentAddress(name=agent_name),
            execution_strategy=ExecutionStrategyKind(spec.execution_strategy),
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
            control_origin=deps.control_origin
            or build_control_origin(project_dir / "config"),
        )

        session_store = build_external_session_map_store(
            deps.app_config,
            deps.persistence,
            workspace_dir,
            BotRecordScope(pool=pool_name),
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
        from modex_agent.plugins.abc import ComponentSlot

        hook_factory = ctx.registry.resolve(ComponentSlot.HOOK, "subagent_auto_send")
        hook = await hook_factory.create(hook_factory.config_model(), ctx)
        hook_runner = HookRunner()
        hook_runner.add(
            HookSpec(
                hook=hook,
                on_error=HookErrorPolicy.LOG,
            )
        )

        instance = ExternalAgentBuilder.assemble_pipeline(
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
