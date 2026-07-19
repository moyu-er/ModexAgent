"""Business-layer ``SubagentExternalCodingBuilder`` for the bot project (T8).

A bot operator can declare ``execution_strategy: external_coding`` +
``provider_kind: opencode`` on a subagent in ``pool.yml``. When the parent
agent invokes that subagent, ``AgentTemplate._materialize_external``
dispatches to a ``SubagentExternalCodingBuilder``; this module supplies the
concrete implementation the bot wires into ``AgentMaterializeDeps``.

The assembly mirrors the main-agent external-coding path
(:class:`bot.service.external_coding_strategy.ExternalCodingAwareFactory.create_agent`)
with five star-topology adjustments mandated by ADR-0027:

1. **BackendProvider**: :class:`CachingBackendProvider` (T6) backed by
   :class:`BotBackendFactory` instead of :class:`PoolScopedBackendProvider`
   wrapping a single pre-built backend. Each external subagent invocation
   gets its own provider instance so warm backends are LRU-cached per
   ``modex_session_id`` and bounded across the process.
2. **ExternalEnvSpec**: per-invocation, ``MODEX_TARGETS`` contains only the
   parent agent (star topology — subagents never talk to peers).
   ``MODEX_AGENT_POOL_MAP`` carries only this subagent's own pool.
3. **HookRunner**: carries :class:`SubagentAutoSendHook` (T7) with
   ``execution_strategy=EXTERNAL_CODING`` and the per-workdir
   ``external_outbox_path``. ``ExternalTurnRunner`` dispatches
   ``FINALLY_TURN`` so the hook fires on every turn end.
4. **No ``send_to_agent`` tool**: external subagents have no tool surface;
   they reply via ``modexctl send`` to the parent's inbox. Star topology is
   enforced structurally.
5. **ContextManager**: :class:`InMemoryContextManager` — the external CLI
   maintains its own context; ModexAgent only forwards ``current_input``.

``pool_builder.create_pool`` constructs one ``BotSubagentExternalCodingBuilder``
per pool that declares at least one external subagent and injects it into
``AgentMaterializeDeps.subagent_external_coding_builder``. React-only pools
leave the field ``None`` (zero overhead).
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any

from modex_agent.agents.external_coding.backend_provider import (
    BackendFactory,
    CachingBackendProvider,
)
from modex_agent.agents.external_coding.builder import ExternalCodingAgentBuilder
from modex_agent.agents.external_coding.contracts import ProviderEventParser
from modex_agent.agents.external_coding.os_layer import register_signal_handlers
from modex_agent.agents.external_coding.paths import ExternalPaths, ProviderKind
from modex_agent.agents.external_coding.providers.opencode_parser import (
    OpenCodeEventParser,
)
from modex_agent.agents.external_coding.providers.opencode_server_backend import (
    OpenCodeServerBackend,
)
from modex_agent.agents.external_coding.providers.pi_backend import PiBackend
from modex_agent.agents.external_coding.providers.pi_parser import PiEventParser
from modex_agent.agents.external_coding.subagent_builder import (
    SubagentExternalCodingBuilder,
)
from modex_agent.agents.external_coding.types import ExternalEnvSpec
from modex_agent.core.constants import ExecutionStrategyKind
from modex_agent.core.context import InMemoryContextManager
from modex_agent.core.llm_struct import RuntimeSafetyPolicy
from modex_agent.hook import HookErrorPolicy, HookRunner, HookSpec
from modex_agent.hook.builtin.subagent_auto_send import SubagentAutoSendHook

if TYPE_CHECKING:
    from modex_agent.agents.external_coding.agent import StreamingProviderBackend
    from modex_agent.core.session_id import SessionInfo
    from modex_agent.multi_agent.descriptor import AgentDescriptor, AgentInstance
    from modex_agent.multi_agent.materialize_deps import AgentMaterializeDeps
    from modex_agent.multi_agent.pool_config.specs import SubagentSpec
    from modex_agent.pipeline.pipeline import AgentPipeline

logger = logging.getLogger(__name__)

__all__ = [
    "BotBackendFactory",
    "BotSubagentExternalCodingBuilder",
    "register_signal_handlers",
]


# ═══════════════════════════════════════════════════════════════════════════
# BotBackendFactory
# ═══════════════════════════════════════════════════════════════════════════


class BotBackendFactory(BackendFactory):
    """Bot-side :class:`BackendFactory` for external-coding subagents.

    Creates a fresh backend per ``provider_kind`` on cache miss.
    :meth:`is_warm` partitions the caching strategy:

    - ``OPENCODE`` → warm (``OpenCodeServerBackend`` holds one long-lived
      ``opencode serve`` SSE process per ``modex_session_id``, LRU-capped
      by :class:`CachingBackendProvider`).
    - ``PI`` → stateless (``PiBackend`` spawns a per-turn subprocess that
      is reaped on turn end; one shared instance per ``provider_kind``).
    """

    def create(self, provider_kind: ProviderKind) -> StreamingProviderBackend:
        if provider_kind == ProviderKind.OPENCODE:
            return OpenCodeServerBackend()
        if provider_kind == ProviderKind.PI:
            return PiBackend()
        raise ValueError(f"Unsupported provider_kind: {provider_kind}")

    def is_warm(self, provider_kind: ProviderKind) -> bool:
        # OpenCodeServerBackend owns a long-lived ``opencode serve`` process
        # that should be cached per modex_session_id. PiBackend is stateless
        # — every turn spawns a fresh subprocess that is reaped on completion.
        return provider_kind == ProviderKind.OPENCODE


# ═══════════════════════════════════════════════════════════════════════════
# BotSubagentExternalCodingBuilder
# ═══════════════════════════════════════════════════════════════════════════


def _modexctl_bin_dir() -> Path:
    """Resolve the ``modexctl`` binary directory for the spawn ``PATH``.

    Mirrors :func:`bot.service.external_coding_strategy._modexctl_bin_dir`:
    ``shutil.which`` first, falling back to ``.`` if not on PATH (logged).
    """
    exe = shutil.which("modexctl")
    if exe:
        return Path(exe).parent
    logger.warning("modexctl not found on PATH; falling back to '.' for modexctl_bin_dir")
    return Path(".")


class BotSubagentExternalCodingBuilder(SubagentExternalCodingBuilder):
    """Business-layer builder for external-coding subagent instances.

    One instance per pool that declares at least one external subagent.
    Holds the pool-level collaborators that do not vary per invocation
    (``app_config``, ``persistence``, ``project_dir``, ``data_dir``,
    ``workspace_path_resolver``, ``pool_name``) so per-invocation
    :meth:`build` only assembles the per-turn pieces.

    A :class:`CachingBackendProvider` is constructed at materialize time
    (once per agent_name). Warm backends are cached per
    ``modex_session_id`` within the provider, so different invocations of
    the same subagent get different warm backends, bounded by
    ``MAX_WARM_BACKENDS``. Pool shutdown closes backends via the agent's
    ``stop()`` → ``BackendProvider.close_all()`` (wired in T2/T6).
    """

    def __init__(
        self,
        *,
        pool_name: str,
        project_dir: Path,
        data_dir: Path,
        app_config: Any | None = None,
        persistence: Any | None = None,
    ) -> None:
        self._pool_name = pool_name
        self._project_dir = project_dir
        self._data_dir = data_dir
        self._app_config = app_config
        self._persistence = persistence

    async def build(
        self,
        spec: SubagentSpec,
        descriptor: AgentDescriptor,
        parent_session: SessionInfo | str | None,
        invocation_id: str | None,
        deps: AgentMaterializeDeps,
    ) -> AgentInstance:
        """Assemble a fully-wired external-coding subagent :class:`AgentInstance`.

        See the module docstring for the five star-topology adjustments
        versus the main-agent external-coding assembly. The returned
        instance is registered with the pool by
        ``AgentTemplate._materialize_external`` after this returns — this
        method MUST NOT call ``pool.register_resident`` or
        ``deps.on_subagent_created``.
        """
        from bot.scope import BotRecordScope
        from bot.service.builders import build_external_session_map_store

        # ── 0. Resolve per-invocation identity ───────────────────────────
        agent_name = spec.agent_name
        parent_name = (
            str(parent_session).split(".")[-1] if parent_session else ""
        )
        session_id = f"{invocation_id or ''}.{agent_name}"

        # workspace_root / workdir / inbox_root come from the workspace
        # path resolver (per-workspace isolation). Fallback to project_dir
        # when no resolver is wired (unit tests, non-workspace paths).
        workspace_dir = self._resolve_workspace_dir(deps)
        inbox_root = self._resolve_inbox_root(deps, workspace_dir)

        # ── 1. ExternalEnvSpec (per-invocation, star-topology targets) ──
        env_spec = ExternalEnvSpec(
            workspace_root=workspace_dir,
            inbox_root=inbox_root,
            workdir=workspace_dir,
            session_id=session_id,
            agent_name=agent_name,
            provider_session_id="",  # fresh — session_store resolves/commits
            agent_pool_map={agent_name: self._pool_name},
            targets=[(parent_name, "")] if parent_name else [],
            modexctl_bin_dir=_modexctl_bin_dir(),
        )

        # ── 2. ExternalSessionMapStore (FILE or SQLite per PersistenceConfig)
        session_store = build_external_session_map_store(
            self._app_config,
            self._persistence,
            workspace_dir,
            BotRecordScope(pool=self._pool_name),
        )

        # ── 3. ProviderEventParser per provider_kind ─────────────────────
        parser = self._build_parser(spec.provider_kind)

        # ── 4. CachingBackendProvider (per-invocation; closes on agent.stop)
        backend_provider = CachingBackendProvider(BotBackendFactory())

        # ── 5. ExternalCodingAgent ────────────────────────────────────────
        agent = ExternalCodingAgentBuilder.build_agent(
            descriptor,
            provider=None,
            backend_provider=backend_provider,
            session_store=session_store,
            parser=parser,
            provider_kind=spec.provider_kind,  # type: ignore[arg-type]
            spec=env_spec,
            base_env=dict(os.environ),
        )

        # ── 6. HookRunner carrying SubagentAutoSendHook (T7) ─────────────
        runtime_dir = self._resolve_runtime_dir(deps)
        outbox_path = ExternalPaths(workspace_dir).outbox
        hook_runner = HookRunner()
        hook_runner.add(
            HookSpec(
                hook=SubagentAutoSendHook(
                    agent_bus=deps.agent_bus,
                    self_name=agent_name,
                    parent_name=parent_name,
                    runtime_dir=runtime_dir or Path("."),
                    trace_enabled=False,
                    execution_strategy=ExecutionStrategyKind.EXTERNAL_CODING,
                    external_outbox_path=outbox_path,
                ),
                on_error=HookErrorPolicy.LOG,
            )
        )

        # ── 7. Assemble pipeline via shared helper (converged with main-agent path).
        from modex_agent.core.llm_struct import RuntimeSafetyPolicy

        safety: RuntimeSafetyPolicy = descriptor.safety_policy or RuntimeSafetyPolicy()
        return ExternalCodingAgentBuilder.assemble_pipeline(
            descriptor,
            agent,
            broker=deps.broker,
            safety=safety,
            hook_runner=hook_runner,
            session_registry=deps.session_registry,
            control_channel=None,
            context_manager=None,
        )

    # ── Helpers ───────────────────────────────────────────────────────────

    @staticmethod
    def _build_parser(provider_kind: ProviderKind | None) -> ProviderEventParser:
        if provider_kind == ProviderKind.OPENCODE:
            return OpenCodeEventParser()
        if provider_kind == ProviderKind.PI:
            return PiEventParser()
        raise ValueError(
            f"Cannot build ProviderEventParser: provider_kind is {provider_kind!r}"
        )

    def _resolve_workspace_dir(self, deps: AgentMaterializeDeps) -> Path:
        """Resolve the per-invocation workdir from deps or fallback to project_dir."""
        if deps.project_dir is not None:
            return deps.project_dir
        resolver = deps.workspace_path_resolver
        if resolver is not None:
            runtime_dir = resolver.runtime_dir()
            if runtime_dir is not None:
                return _workspace_root_from_runtime_dir(runtime_dir)
        return self._project_dir

    def _resolve_inbox_root(
        self, deps: AgentMaterializeDeps, workspace_dir: Path
    ) -> Path:
        """Resolve inbox_root: parent of the per-pool inbox dir.

        Mirrors ``external_coding_strategy.build_external_coding_env_spec``
        which sets ``inbox_root=inbox_dir.parent`` where
        ``inbox_dir = data_dir / "inbox" / pool_name``.
        """
        return self._data_dir / "inbox"

    def _resolve_runtime_dir(self, deps: AgentMaterializeDeps) -> Path | None:
        resolver = deps.workspace_path_resolver
        if resolver is not None:
            return resolver.runtime_dir()
        return None


def _workspace_root_from_runtime_dir(runtime_dir: Path) -> Path:
    """Climb from ``<workspace>/.modex/runtime_state/<pool>`` to ``<workspace>``.

    The runtime_dir path layout is fixed by
    :class:`modex_agent.workspace.paths.WorkspacePaths` — three levels
    (``.modex`` / ``runtime_state`` / ``<pool>``) sit below the workspace
    root. Resolve via ``parents[2]`` and fall back to ``runtime_dir`` if
    the layout does not match (defensive — a future paths change would
    surface as a wrong workdir, not a crash).
    """
    try:
        return runtime_dir.resolve().parents[2]
    except IndexError:
        return runtime_dir

