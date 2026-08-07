"""Business-layer ``SubagentExternalBuilder`` for the bot project (T8).

A bot operator can declare ``execution_strategy: external`` +
``provider_kind: opencode`` on a subagent in ``pool.yml``. When the parent
agent invokes that subagent, ``AgentTemplate._materialize_external``
dispatches to a ``SubagentExternalBuilder``; this module supplies the
concrete implementation the bot wires into ``AgentMaterializeDeps``.

The assembly mirrors the main-agent external path
(:class:`bot.service.external_strategy.ExternalAwareFactory.create_agent`)
with five star-topology adjustments mandated by ADR-0027:

1. **BackendProvider**: :class:`PoolScopedBackendProvider` wrapping a single
   :class:`OpenCodeServerBackend` — same as the main-agent path. The
   :class:`OpenCodeServerManager` singleton handles process lifecycle;
   ``OpenCodeServerBackend.close()`` is a no-op.
2. **ExternalEnvSpec**: per-invocation, ``MODEX_TARGETS`` contains only the
   parent agent (star topology — subagents never talk to peers).
   ``MODEX_AGENT_POOL_MAP`` carries this subagent's own pool plus the
   parent's pool entry (both in the same pool — subagents are registered
   into the parent's pool) so ``modexctl send --to <parent>`` can resolve
   the target pool.
3. **HookRunner**: carries :class:`SubagentAutoSendHook` (T7) with
   ``execution_strategy=EXTERNAL``. ``ExternalTurnRunner`` dispatches
   ``FINALLY_TURN`` so the hook fires on every turn end.
4. **No ``send_to_agent`` tool**: external subagents have no tool surface;
   they reply via ``modexctl send`` to the parent's inbox. Star topology is
   enforced structurally.
5. **ContextManager**: :class:`InMemoryContextManager` — the external CLI
   maintains its own context; ModexAgent only forwards ``current_input``.

``pool_builder.create_pool`` constructs one ``BotSubagentExternalBuilder``
per pool that declares at least one external subagent and injects it into
``AgentMaterializeDeps.subagent_external_builder``. React-only pools
leave the field ``None`` (zero overhead).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from bot.config.webui_config import build_control_origin
from modex_agent.agents.external.backend_provider import (
    PoolScopedBackendProvider,
)
from modex_agent.agents.external.builder import ExternalAgentBuilder
from modex_agent.agents.external.cli_resolver import resolve_modexctl_bin_dir
from modex_agent.agents.external.contracts import ProviderEventParser
from modex_agent.agents.external.os_layer import register_signal_handlers
from modex_agent.agents.external.paths import ProviderKind
from modex_agent.agents.external.providers.opencode.server_backend import (
    OpenCodeServerBackend,
)
from modex_agent.agents.external.providers.opencode.v2_parser import (
    OpenCodeV2EventParser,
)
from modex_agent.agents.external.subagent_builder import (
    SubagentExternalBuilder,
)
from modex_agent.agents.external.types import ExternalEnvSpec
from modex_agent.core.agent import AgentCommKind
from modex_agent.core.constants import ExecutionStrategyKind
from modex_agent.core.llm_struct import RuntimeSafetyPolicy
from modex_agent.hook import HookErrorPolicy, HookRunner, HookSpec
from modex_agent.hook.builtin.subagent_auto_send import SubagentAutoSendHook

if TYPE_CHECKING:
    from modex_agent.core.session_id import SessionInfo
    from modex_agent.multi_agent.descriptor import AgentDescriptor, AgentInstance
    from modex_agent.multi_agent.materialize_deps import AgentMaterializeDeps
    from modex_agent.multi_agent.pool_config.specs import SubagentSpec

logger = logging.getLogger(__name__)

__all__ = [
    "BotSubagentExternalBuilder",
    "register_signal_handlers",
]


# ═══════════════════════════════════════════════════════════════════════════
# BotSubagentExternalBuilder
# ═══════════════════════════════════════════════════════════════════════════


def _modexctl_bin_dir() -> Path:
    """Resolve the ``modexctl`` binary directory for the spawn ``PATH``.

    Delegates to :func:`modex_agent.agents.external.cli_resolver.resolve_modexctl_bin_dir`
    — the single source of truth. The previous inline ``shutil.which`` +
    ``Path(".")`` fallback (which never pointed at a real modexctl and
    caused silent cross-pool messaging failures) is removed.

    Raises:
        ModexctlResolutionError: forwarded from the resolver when all four
            resolution strategies fail. The bot surfaces this at pool
            materialisation time rather than silently corrupting the spawn env.
    """
    return resolve_modexctl_bin_dir()


class BotSubagentExternalBuilder(SubagentExternalBuilder):
    """Business-layer builder for external subagent instances.

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
        """Assemble a fully-wired external subagent :class:`AgentInstance`.

        See the module docstring for the five star-topology adjustments
        versus the main-agent external assembly. The returned
        instance is registered with the pool by
        ``AgentTemplate._materialize_external`` after this returns — this
        method MUST NOT call ``pool.register_resident`` or
        ``deps.on_subagent_created``.
        """
        from bot.scope import BotRecordScope
        from bot.service.builders import build_external_session_map_store

        # ── 0. Resolve per-invocation identity ───────────────────────────
        agent_name = spec.agent_name
        parent_session_str = str(parent_session) if parent_session else ""
        parent_name = parent_session_str.split(".")[-1] if parent_session_str else ""
        session_id = f"{invocation_id or ''}.{agent_name}"

        # workspace_root / workdir / inbox_root come from the workspace
        # path resolver (per-workspace isolation). Fallback to project_dir
        # when no resolver is wired (unit tests, non-workspace paths).
        workspace_dir = self._resolve_workspace_dir(deps)
        inbox_root = self._resolve_inbox_root(deps, workspace_dir)

        # ── 1. ExternalEnvSpec (per-invocation, star-topology targets) ──
        #
        # Dynamism across three time scales:
        #   • per-invocation — DYNAMIC. Each parent→child call rebuilds this
        #     spec with the caller's parent_name.
        #   • per-turn       — STATIC. ExternalAgent._run_turn refreshes
        #     only session_id + workdir via model_copy; agent_pool_map and
        #     targets are frozen for the agent's lifetime. (spec.md claims a
        #     per-turn refresh from CommunicationTargetStore — never
        #     implemented; this is a known spec deviation.)
        #   • runtime-config — STATIC. pool_spec.subagents/peers are read
        #     from disk at bot boot; WebUI peer add/remove mutates only the
        #     native CommunicationTargetStore, not this external snapshot.
        #     A pool restart is required for changes to take effect here.
        #
        # comm_kind=SUBAGENT + parent_session_id: modexctl send uses the
        # parent's full session_id verbatim as target_sid, bypassing
        # ADR-0019 prefix-reuse (which would mint a phantom parent session
        # because subagent session prefixes are invocation_ids, not
        # conversation_ids). The main-agent-as-peer path uses comm_kind=NORMAL
        # and relies on prefix-reuse — the two paths are fully separated
        # in modexctl.
        #
        # agent_pool_map must include the parent so `modexctl send --to <parent>`
        # can resolve the parent's pool. Subagents are registered into the
        # parent's pool (AgentTemplate._materialize_external → pool.register_resident),
        # so the parent's pool is self._pool_name.
        agent_pool_map: dict[str, str] = {agent_name: self._pool_name}
        if parent_name:
            agent_pool_map[parent_name] = self._pool_name
        env_spec = ExternalEnvSpec(
            workspace_root=workspace_dir,
            inbox_root=inbox_root,
            workdir=workspace_dir,
            session_id=session_id,
            agent_name=agent_name,
            provider_session_id="",  # fresh — session_store resolves/commits
            agent_pool_map=agent_pool_map,
            targets=[(parent_name, "")] if parent_name else [],
            comm_kind=AgentCommKind.SUBAGENT,
            parent_session_id=parent_session_str or None,
            modexctl_bin_dir=_modexctl_bin_dir(),
            control_origin=deps.control_origin
            or build_control_origin(self._project_dir / "config"),
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

        # ── 4. PoolScopedBackendProvider (shared singleton server)
        backend_provider = PoolScopedBackendProvider(OpenCodeServerBackend())

        # ── 5. Child-session discovery collaborators ────────────────────
        from bot.service.external_strategy import (
            _build_child_discovery_collaborators,
        )

        child_sink, child_emitter_factory = _build_child_discovery_collaborators(
            session_registry=deps.session_registry,
            session_map_store=session_store,
            provider_kind=spec.provider_kind or ProviderKind.OPENCODE,
            session_factory=deps.session_factory,
        )

        # ── 6. ExternalAgent ────────────────────────────────────────
        agent = ExternalAgentBuilder.build_agent(
            descriptor,
            provider=None,
            backend_provider=backend_provider,
            session_store=session_store,
            parser=parser,
            provider_kind=spec.provider_kind,  # type: ignore[arg-type]
            spec=env_spec,
            base_env=dict(os.environ),
            child_discovery_sink=child_sink,
            session_registry=deps.session_registry,
            session_id_factory=deps.session_factory,
            child_emitter_factory=child_emitter_factory,
        )

        # ── 6. HookRunner carrying SubagentAutoSendHook (T7) ─────────────
        runtime_dir = self._resolve_runtime_dir(deps)
        hook_runner = HookRunner()
        hook_runner.add(
            HookSpec(
                hook=SubagentAutoSendHook(
                    agent_bus=deps.agent_bus,
                    self_name=agent_name,
                    parent_name=parent_name,
                    runtime_dir=runtime_dir or Path("."),
                    trace_enabled=False,
                    execution_strategy=ExecutionStrategyKind.EXTERNAL,
                ),
                on_error=HookErrorPolicy.LOG,
            )
        )

        # ── 7. Assemble pipeline via shared helper (converged with main-agent path).
        from modex_agent.core.llm_struct import RuntimeSafetyPolicy

        safety: RuntimeSafetyPolicy = descriptor.safety_policy or RuntimeSafetyPolicy()
        return ExternalAgentBuilder.assemble_pipeline(
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
        if provider_kind != ProviderKind.OPENCODE:
            raise ValueError(f"Cannot build ProviderEventParser: provider_kind is {provider_kind!r}")
        return OpenCodeV2EventParser()

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

    def _resolve_inbox_root(self, deps: AgentMaterializeDeps, workspace_dir: Path) -> Path:
        """Resolve inbox_root: parent of the per-pool inbox dir.

        Mirrors ``external_strategy.build_external_env_spec``
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
