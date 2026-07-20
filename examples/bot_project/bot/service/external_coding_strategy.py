"""ExternalCodingExecutionStrategy - assembles external_coding pools (ADR-0025, ticket 4).

Transitional home: lives in ``examples/bot_project/bot/service/`` (NOT in
``src/modex_agent/agents/external_coding/``) because ``assemble()`` calls the
bot-side wiring helpers (now inlined here from the deleted
``_external_coding_wiring.py``) which use bot-layer types (``PoolSpec``,
``AppConfig``, persistence manager). A future ticket may relocate this class
to ``src/modex_agent/agents/external_coding/strategy.py`` once the bot-layer
dependencies are abstracted away.

The strategy is stateless: ``assemble()`` is called once per pool at build
time. It performs the provider-availability gate (``shutil.which``) and
builds the ``external_coding_deps`` dict that ``ExternalCodingAwareFactory``
reads to construct an ``ExternalCodingAgent``. The dict is returned in a
:class:`StrategyAssembly`'s transitional ``external_coding_deps`` field.

Ticket 6: the ``_external_coding_wiring.py`` file is deleted; its content
(``build_external_coding_deps``, ``read_provider_kind``,
``provider_executable_for``, ``build_external_coding_backend``,
``build_external_coding_parser``, ``build_external_coding_env_spec``,
``ExternalCodingAwareFactory``, ``_OpenCodeFallbackBackend``) lives here as
private methods/classes. The strategy also inherits
:class:`_PoolAssemblyMixin` so it can build the placeholder
provider/terminal/tools/skill_manager that ``ExternalCodingAwareFactory``
still requires as constructor args (behavior preservation per ticket 6; a
future ticket will eliminate this unnecessary building).

``agent`` and ``turn_runner`` are ``None`` - the ``ExternalCodingAgent``
instance + ``ExternalTurnRunner`` are created downstream by the factory +
pipeline.
"""

from __future__ import annotations

import logging
import os
import shutil
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import yaml

from modex_agent.agents.external_coding.agent import StreamingProviderBackend
from modex_agent.agents.external_coding.backend_provider import PoolScopedBackendProvider
from modex_agent.agents.external_coding.builder import ExternalCodingAgentBuilder
from modex_agent.agents.external_coding.cli_resolver import resolve_modexctl_bin_dir
from modex_agent.agents.external_coding.contracts import ProviderEventParser
from modex_agent.agents.external_coding.paths import ProviderKind
from modex_agent.agents.external_coding.providers.opencode_backend import OpenCodeBackend
from modex_agent.agents.external_coding.providers.opencode_server_backend import (
    OpenCodeServerBackend,
    SSEUnavailableError,
)
from modex_agent.agents.external_coding.providers.opencode_sse_parser import OpenCodeSSEParser
from modex_agent.agents.external_coding.providers.pi_backend import PiBackend
from modex_agent.agents.external_coding.providers.pi_parser import PiEventParser
from modex_agent.agents.external_coding.types import (
    BackendResult,
    Emission,
    ExecOptions,
    ExternalEnvSpec,
)
from modex_agent.multi_agent.descriptor import AgentDescriptor, AgentInstance
from modex_agent.multi_agent.execution_strategy import (
    ExecutionStrategy as ExecutionStrategyABC,
)
from modex_agent.multi_agent.execution_strategy import (
    PoolAssemblyContext,
    StrategyAssembly,
)
from modex_agent.multi_agent.factory import DefaultAgentFactory
from modex_agent.multi_agent.pool_config import PoolStore
from modex_agent.multi_agent.pool_config.specs import PoolSpec

from ._assembly_helpers import _PoolAssemblyMixin

logger = logging.getLogger(__name__)

__all__ = [
    "ExternalCodingAwareFactory",
    "ExternalCodingExecutionStrategy",
    "ProviderUnavailableError",
    "build_external_coding_env_spec",
]


class ProviderUnavailableError(Exception):
    """Raised by :meth:`ExternalCodingExecutionStrategy.assemble` when the
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
# Backend (moved from _external_coding_wiring.py)
# ═══════════════════════════════════════════════════════════════════════════


class _OpenCodeFallbackBackend(StreamingProviderBackend):
    """SSE-first backend with automatic subprocess fallback.

    OpenCode SSE (``opencode serve``) is the hardcoded default. If the
    SSE server fails to start, each turn falls back to subprocess
    (``opencode run``). The fallback is sticky - once SSE fails, all
    subsequent turns use subprocess to avoid repeated startup failures.
    """

    def __init__(self) -> None:
        self._sse_backend = OpenCodeServerBackend()
        self._subprocess_backend = OpenCodeBackend()
        self._fallback_active = False

    async def execute_streaming(
        self,
        opts: ExecOptions,
        env: dict[str, str],
        on_emission: Callable[[Emission], Awaitable[None]],
    ) -> BackendResult:
        if not self._fallback_active:
            try:
                return await self._sse_backend.execute_streaming(
                    opts, env, on_emission
                )
            except SSEUnavailableError as exc:
                logger.warning(
                    "OpenCode SSE backend unavailable, falling back to subprocess: %s",
                    exc,
                )
                self._fallback_active = True
        return await self._subprocess_backend.execute_streaming(
            opts, env, on_emission
        )

    async def close(self) -> None:
        first_error: BaseException | None = None
        try:
            await self._sse_backend.close()
        except BaseException as exc:
            first_error = exc
        try:
            await self._subprocess_backend.close()
        except BaseException as exc:
            if first_error is None:
                first_error = exc
        if first_error is not None:
            raise first_error


# ═══════════════════════════════════════════════════════════════════════════
# Factory (moved from _external_coding_wiring.py)
# ═══════════════════════════════════════════════════════════════════════════


class ExternalCodingAwareFactory(DefaultAgentFactory):
    """``DefaultAgentFactory`` subclass that builds ``ExternalCodingAgent`` instances.

    Fully overrides :meth:`create_agent` to skip ALL react-only construction:
    no ``BotModelProvider``, no ``FilteredToolManager``, no ``SkillManager``,
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
        external_coding_deps: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        # Do NOT call super().__init__() — it builds react-only defaults
        # (RuntimeContextManager, InboxProducer/Consumer, etc.) that
        # ExternalTurnRunner never uses.
        self._external_coding_deps: dict[str, Any] = external_coding_deps or {}
        self._control_channel = kwargs.get("control_channel")
        self._session_registry = kwargs.get("session_registry")
        self._observability_config = kwargs.get("observability_config")
        # Stub the attributes the base class would set, in case any code path
        # reads them via super() (defense-in-depth; ExternalTurnRunner doesn't
        # use them, but a future base-class method could).
        self._default_llm_provider = kwargs.get("default_llm_provider")
        self._default_tool_manager = kwargs.get("default_tool_manager")
        self._skill_manager = kwargs.get("skill_manager")
        self._sanitizer = kwargs.get("sanitizer")
        self._command_interceptor = kwargs.get("command_interceptor")
        self._subagent_service = kwargs.get("subagent_service")
        self._inbox_server = kwargs.get("inbox_server")
        self._default_hooks = list(kwargs.get("default_hooks") or [])
        self._default_hook_runner = kwargs.get("default_hook_runner")
        self._default_interceptor_chain = kwargs.get("default_interceptor_chain")
        self._default_turn_store = kwargs.get("default_turn_store")
        self._trace_store = kwargs.get("trace_store")
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
        skill_manager: Any | None = None,  # ignored
        sanitizer: Any | None = None,  # ignored
        command_interceptor: Any | None = None,  # ignored
        subagent_service: Any | None = None,  # ignored
        hooks: list[Any] | None = None,  # ignored — ExternalTurnRunner doesn't dispatch hooks
        output_adapter: Any | None = None,
        context_manager_factory: Any | None = None,  # ignored
    ) -> AgentInstance:
        """Build an ExternalCodingAgent + ExternalTurnRunner + minimal pipeline.

        Overrides :meth:`DefaultAgentFactory.create_agent` to skip ALL
        react-only construction. ``ExternalTurnRunner`` doesn't use any of
        those objects — it builds a minimal ``AgentContext`` (empty history +
        empty ``InMemoryToolManager``) and calls ``agent.run()`` directly.
        The external agent communicates via ``modexctl send`` CLI, not
        ``send_to_agent``.
        """
        from modex_agent.agents.external_coding.turn_runner import ExternalTurnRunner
        from modex_agent.core.context import InMemoryContextManager
        from modex_agent.core.llm_struct import RuntimeSafetyPolicy
        from modex_agent.messaging.broker_bridge import (
            BrokerInputAdapter,
            BrokerOutputAdapter,
        )
        from modex_agent.messaging.broker_memory import InMemoryMessageBroker
        from modex_agent.multi_agent.router import DefaultMeshRouter
        from modex_agent.pipeline.adapters import OutputAdapter
        from modex_agent.pipeline.pipeline import AgentPipeline
        from modex_agent.pipeline.turn_session_registry import TurnSessionRegistry

        # 1. Agent — provider=None (ExternalCodingAgentBuilder ignores it;
        #    the external CLI owns its own model configuration). ADR-0027:
        #    wrap the pool-scoped backend in a BackendProvider so the agent
        #    borrows it per turn instead of holding a fixed reference.
        deps = self._external_coding_deps
        missing = [
            name
            for name in ("backend", "session_store", "parser", "provider_kind", "spec")
            if deps.get(name) is None
        ]
        if missing:
            raise ValueError(
                f"ExternalCodingAwareFactory missing external_coding deps: {', '.join(missing)}"
            )
        backend_provider = PoolScopedBackendProvider(deps["backend"])
        agent = ExternalCodingAgentBuilder.build_agent(
            descriptor,
            provider=None,
            backend_provider=backend_provider,
            session_store=deps["session_store"],
            parser=deps["parser"],
            provider_kind=deps["provider_kind"],
            spec=deps["spec"],
            base_env=deps.get("base_env"),
        )

        # 2. Assemble pipeline via shared helper (converged with subagent path).
        safety: RuntimeSafetyPolicy = descriptor.safety_policy or RuntimeSafetyPolicy()
        return ExternalCodingAgentBuilder.assemble_pipeline(
            descriptor,
            agent,
            broker=broker,
            safety=safety,
            hook_runner=None,  # main agents don't fire FINALLY_TURN
            session_registry=self._session_registry,
            control_channel=self._control_channel,
            output_adapter=output_adapter if isinstance(output_adapter, OutputAdapter) else None,
            context_manager=context_manager,
        )


# ═══════════════════════════════════════════════════════════════════════════
# Module-level wiring helpers (moved from _external_coding_wiring.py).
# Kept module-level (not methods) because:
#  - ``build_external_coding_env_spec`` is imported by tests directly.
#  - The helpers are pure functions with no strategy state; making them
#    methods would be a forced OOP wrapper.
# ═══════════════════════════════════════════════════════════════════════════


def _modexctl_bin_dir() -> Path:
    """Resolve the ``modexctl`` binary directory for the spawn ``PATH``.

    Delegates to :func:`modex_agent.agents.external_coding.cli_resolver.resolve_modexctl_bin_dir`
    — the single source of truth. The previous inline ``shutil.which`` +
    ``Path(".")`` fallback (which never pointed at a real modexctl and
    caused silent cross-pool messaging failures when the bot was launched
    without the venv ``Scripts`` directory on PATH) is removed.

    Raises:
        ModexctlResolutionError: forwarded from the resolver when all four
            resolution strategies fail. Surfaced at pool assembly time.
    """
    return resolve_modexctl_bin_dir()


def _build_agent_pool_map(
    pool_name: str, pool_spec: PoolSpec, project_dir: Path
) -> dict[str, str]:
    pool_map: dict[str, str] = {pool_spec.main.agent_name: pool_name}
    for sub in pool_spec.subagents:
        pool_map[sub.agent_name] = pool_name
    store = PoolStore(base_dir=project_dir)
    for peer in pool_spec.peers:
        try:
            peer_spec = store.read_pool(peer)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Pool '%s': cannot read peer pool %r for agent_pool_map: %s",
                pool_name, peer, exc,
            )
            continue
        pool_map[peer_spec.main.agent_name] = peer
    return pool_map


def _build_targets(
    pool_name: str, pool_spec: PoolSpec, project_dir: Path
) -> list[tuple[str, str]]:
    targets: list[tuple[str, str]] = []
    for sub in pool_spec.subagents:
        targets.append((sub.agent_name, sub.description or f"{sub.agent_name} subagent"))
    store = PoolStore(base_dir=project_dir)
    for peer in pool_spec.peers:
        try:
            peer_spec = store.read_pool(peer)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Pool '%s': cannot read peer pool %r for targets: %s",
                pool_name, peer, exc,
            )
            continue
        desc = peer_spec.main.description or f"Peer pool {peer}'s main agent"
        targets.append((peer_spec.main.agent_name, desc))
    return targets


def build_external_coding_env_spec(
    pool_name: str,
    pool_spec: PoolSpec,
    project_dir: Path,
    inbox_dir: Path,
    workspace_dir: Path,
    main_agent_name: str,
) -> ExternalEnvSpec:
    """Build the ``ExternalEnvSpec`` for an external_coding pool.

    Kept as a module-level function because tests import it directly
    (``test_builders_inbox.py``).

    Dynamism across two time scales (no per-invocation dimension — main
    agents are assembled once at boot):
      • per-turn       — STATIC. ``ExternalCodingAgent._run_turn`` refreshes
        only session_id + workdir via model_copy; agent_pool_map and
        targets are frozen for the agent's lifetime. (spec.md claims a
        per-turn refresh from CommunicationTargetStore — never implemented;
        known spec deviation.)
      • runtime-config — STATIC. pool_spec.subagents/peers are read from
        disk at bot boot; WebUI peer add/remove mutates only the native
        CommunicationTargetStore, not this external snapshot. A pool
        restart is required for changes to take effect here.

    The ``targets`` ⊆ ``agent_pool_map.keys()`` invariant holds: every
    name in targets (subagents + peers' mains) has a pool_map entry, so
    ``modexctl send --to <any target>`` always resolves. The main agent's
    own name is in pool_map but not in targets (main never self-sends;
    modexctl rejects self-send explicitly).
    """
    return ExternalEnvSpec(
        workspace_root=workspace_dir,
        inbox_root=inbox_dir.parent,
        workdir=workspace_dir,
        session_id=f"__pending__.{main_agent_name}",
        agent_name=main_agent_name,
        provider_session_id="",
        agent_pool_map=_build_agent_pool_map(pool_name, pool_spec, project_dir),
        targets=_build_targets(pool_name, pool_spec, project_dir),
        modexctl_bin_dir=_modexctl_bin_dir(),
    )


# ═══════════════════════════════════════════════════════════════════════════
# Strategy
# ═══════════════════════════════════════════════════════════════════════════


class ExternalCodingExecutionStrategy(_PoolAssemblyMixin, ExecutionStrategyABC):
    """Assemble external_coding pools (Pi / OpenCode CLI harness).

    Inherits the shared ``_build_*`` helpers from :class:`_PoolAssemblyMixin`
    so ``assemble()`` can build the placeholder provider/terminal/tools/skill
    that ``ExternalCodingAwareFactory`` still requires as constructor args
    (behavior preservation per ticket 6).
    """

    @property
    def name(self) -> str:
        return "external_coding"

    @property
    def supports_subagents(self) -> bool:
        return False

    @property
    def requires_main_agent_tools(self) -> bool:
        return False

    def validate_pool_spec(self, spec: PoolSpec) -> None:
        """Reject pools incompatible with the external_coding shape.

        Two invariants (mirrors the validation branches that lived in
        ``pool_config/store.py`` before ticket 6 - those branches are deleted
        in ticket 6; this method is the single enforcement point):

        * **No subagents** - external_coding main agents have no tool surface
          and cannot dispatch subagent tasks. Subagent templates on an
          external_coding pool are a configuration error.
        * **``provider_kind`` required** - the CLI kind (``pi`` / ``opencode``)
          must be set so the strategy knows which backend + parser to build.

        Raises :class:`ValueError` on violation. This runs at pool-assembly
        time as defense-in-depth; ``pool_config/store.py`` no longer enforces
        these (ticket 6 deleted those branches).
        """
        if spec.subagents:
            raise ValueError(
                f"Pool {spec.name!r}: execution_strategy 'external_coding' "
                f"does not support subagents"
            )
        if spec.main.provider_kind is None:
            raise ValueError(
                f"Pool {spec.name!r}: execution_strategy 'external_coding' "
                f"requires a provider_kind"
            )

    # ── Provider-kind / backend / parser resolution ──────────────────────
    # (moved from _external_coding_wiring.py as private methods)

    def _read_provider_kind(self, pool_spec: PoolSpec, project_dir: Path) -> ProviderKind:
        if pool_spec.main.provider_kind is not None:
            return pool_spec.main.provider_kind
        pool_yml = project_dir / "config" / "pools" / pool_spec.name / "pool.yml"
        if not pool_yml.exists():
            return ProviderKind.PI
        data: Any = yaml.safe_load(pool_yml.read_text(encoding="utf-8")) or {}
        kind = data.get("provider_kind", "pi")
        if not isinstance(kind, str):
            return ProviderKind.PI
        return self._provider_kind_from_str(kind)

    @staticmethod
    def _provider_kind_from_str(value: str) -> ProviderKind:
        if value == ProviderKind.OPENCODE.value:
            return ProviderKind.OPENCODE
        return ProviderKind.PI

    @staticmethod
    def _provider_executable_for(kind: ProviderKind) -> str:
        return kind.value

    def _build_external_coding_backend(self, kind: ProviderKind) -> StreamingProviderBackend:
        if kind == ProviderKind.OPENCODE:
            return _OpenCodeFallbackBackend()
        return PiBackend(provider=None)

    def _build_external_coding_parser(self, kind: ProviderKind) -> ProviderEventParser:
        if kind == ProviderKind.OPENCODE:
            return OpenCodeSSEParser()
        return PiEventParser()

    def _build_external_coding_deps(
        self,
        *,
        pool_name: str,
        pool_spec: PoolSpec,
        project_dir: Path,
        inbox_dir: Path,
        workspace_dir: Path,
        main_agent_name: str,
        base_env: dict[str, str] | None = None,
        app_config: Any | None = None,
        persistence: Any | None = None,
    ) -> dict[str, Any]:
        provider_kind = self._read_provider_kind(pool_spec, project_dir)
        backend = self._build_external_coding_backend(provider_kind)
        parser = self._build_external_coding_parser(provider_kind)
        from bot.scope import BotRecordScope
        from bot.service.builders import build_external_session_map_store

        session_store = build_external_session_map_store(
            app_config,
            persistence,
            workspace_dir,
            BotRecordScope(pool=pool_name),
        )
        spec = build_external_coding_env_spec(
            pool_name, pool_spec, project_dir, inbox_dir, workspace_dir, main_agent_name
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

    async def assemble(self, ctx: PoolAssemblyContext) -> StrategyAssembly:
        """Build external_coding deps only; all react-only fields are ``None``.

        Performs the provider-availability gate (``shutil.which`` -> raises
        :class:`ProviderUnavailableError` when the CLI is missing) and builds
        the ``external_coding_deps`` dict that
        :meth:`ExternalCodingAwareFactory.create_agent` reads to construct an
        ``ExternalCodingAgent`` + ``ExternalTurnRunner`` + minimal pipeline.

        Does NOT build provider/terminal_manager/tools/skill_manager/
        context_manager/cassette_recorder/root_provider or any other
        react-only collaborator — ``ExternalTurnRunner`` doesn't use any of
        those. ``ExternalCodingAwareFactory.create_agent`` constructs the
        agent + turn_runner + pipeline directly from
        ``external_coding_deps``.
        """
        pool_name = ctx.pool_name
        pool_spec = ctx.pool_spec
        project_dir: Path = ctx.project_dir
        data_dir: Path = ctx.data_dir
        workspace_handle = ctx.workspace_handle

        # 1. Provider availability gate.
        provider_kind = self._read_provider_kind(pool_spec, project_dir)
        executable = self._provider_executable_for(provider_kind)
        if shutil.which(executable) is None:
            raise ProviderUnavailableError(executable)

        # 2. External deps (backend/session_store/parser/env_spec).
        #    ``inbox_dir`` mirrors the path ``pool_builder.create_pool``
        #    computes (``data_dir / "inbox" / pool_name``) — the
        #    external_coding env spec resolves inbox-relative paths from
        #    ``inbox_dir.parent``.
        workspace_dir = (
            workspace_handle.current if workspace_handle is not None else project_dir
        )
        inbox_dir = data_dir / "inbox" / pool_name
        external_coding_deps = self._build_external_coding_deps(
            pool_name=pool_name,
            pool_spec=pool_spec,
            project_dir=project_dir,
            inbox_dir=inbox_dir,
            workspace_dir=workspace_dir,
            main_agent_name=pool_spec.main.agent_name,
            base_env=dict(os.environ),
            app_config=ctx.app_config,
            persistence=ctx.persistence,
        )

        # 3. Return assembly with ONLY ``external_coding_deps`` populated.
        #    All react-only fields default to ``None`` (built by the factory,
        #    not the strategy).
        return StrategyAssembly(
            agent=None,
            turn_runner=None,
            external_coding_deps=external_coding_deps,
            extra_cleanup=(),
        )
