"""RunnableAppService — the framework-runnable default application service.

ADR-0052 §2 promoted the runnable pieces into the framework (react /
external strategies, ``create_pool``, backend factories, the model
universe, the input skeletons, the ``AppService`` lifecycle skeleton) but
stopped one step short: the road from a loaded scope declaration to LIVE
pools — load → validate → compile → partition into ``DeclaredPoolBuild`` →
``create_pool`` with framework defaults — existed only as bot_project
wiring (``bot/service/pool/declaration.py`` + ``bot/workspace/wiring``).
Every second consumer had to copy that chain.

This module is the missing generic piece: a CONCRETE :class:`AppService`
that boots every pool declared in ``roots.scope_declaration_path`` through
the production assembly road, with framework defaults and nothing
deployment-specific:

- no MCP registry, no multi-live workspace stack, no WebUI, no personal
  preferences, no session-title/media/graph subsystems — a deployment
  needing those subclasses this (the BotService shape) or wraps the pool
  products itself;
- one service-level message broker shared by the booted pools, one broker
  bridge per pool routing its root agent's output topic to the constructor
  ``output_adapter``;
- the strict shutdown tail (pools → bridges → broker → input adapter →
  shared persistence) rides :meth:`AppService._close_shared_persistence`.

The single-turn face :meth:`RunnableAppService.turn` drives one message
through the REAL dispatch road (request scope → inbox → poller → pipeline)
and returns the frozen :class:`RequestResult` — the same awaitable twin
the ACP adapter consumes.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from modex_agent.adapters.output import OutputAdapter
from modex_agent.app.config import AppConfig
from modex_agent.app.models.assembly import ModelRegistryAssembly
from modex_agent.app.models.registry import ModelRegistry
from modex_agent.app.roots import AppAssemblyRoots
from modex_agent.app.service import AppService
from modex_agent.approval.ui import IMUserInterface
from modex_agent.control.channel import InMemoryControlChannel
from modex_agent.core.emitter import ContentEmitter
from modex_agent.core.llm_struct import (
    LLMTimeoutPolicy,
    RuntimeSafetyPolicy,
    TurnTimeoutPolicy,
)
from modex_agent.core.provider import LLMProvider
from modex_agent.core.scope import RecordScope
from modex_agent.core.session_id import SessionInfo
from modex_agent.hook import HookRunner
from modex_agent.interceptor.chain import InterceptorChain
from modex_agent.messaging.broker_memory import InMemoryMessageBroker
from modex_agent.messaging.models import InputMessage
from modex_agent.multi_agent import SessionRetentionPolicy
from modex_agent.multi_agent.communication.peer_resolution import (
    peer_links_from_declaration,
)
from modex_agent.multi_agent.pool_config.declared import DeclaredPoolBuild
from modex_agent.multi_agent.pool_config.deps import PoolAssemblyDeps
from modex_agent.multi_agent.pool_instance import PoolInstance
from modex_agent.multi_agent.pool_router import PoolRouter, agent_pool_ownership
from modex_agent.multi_agent.session_tree.request_scope import RequestResult
from modex_agent.multi_agent.template import AgentTemplate
from modex_agent.multi_agent.template_registry import AgentTemplateRegistry
from modex_agent.pipeline.adapters import InputAdapter
from modex_agent.pipeline.snapshot import PoolDataSnapshot
from modex_agent.plugins.assembly.pool_factory import (
    create_pool,
    resolve_declared_root_prompt,
)
from modex_agent.scope.compiler import CompiledAgent, ScopeCompilation, compile_scope
from modex_agent.scope.component_registry import ComponentRegistry
from modex_agent.scope.components import AgentType
from modex_agent.scope.defaults import memory_config_for_position
from modex_agent.scope.derivation import DEFAULT_LLM_PROVIDER
from modex_agent.scope.loader import load_scope_declaration
from modex_agent.scope.profile import STANDARD_PROFILES
from modex_agent.scope.spec import AgentSpec, PoolSpec, ScopeSpec
from modex_agent.scope.validator import (
    ScopeValidationIssue,
    validate_declaration,
    validate_effective_configs,
)
from modex_agent.workspace.context import WorkspaceContext
from modex_agent.workspace.paths import WorkspacePaths

if TYPE_CHECKING:
    from modex_agent.persistence.managers import WorkspacePersistenceManager

logger = logging.getLogger(__name__)

__all__ = ["RunnableAppService"]

_MAIN_AGENT_TYPES = frozenset({AgentType.native_main, AgentType.external_main})

#: Supplied-mode workspace resources: the runnable default runs the
#: single-workspace shape, so the assembly pipeline's Stage 1 gets a
#: pre-filled (non-None) bundle and skips registry materialization — the
#: documented deadlock guard in ``WorkspaceMaterializeStage``.
_SUPPLIED_WORKSPACE_RESOURCES: Final[object] = object()


class NullEmitter(ContentEmitter[Any]):
    """The no-op content emitter — drops every emission.

    The headless runnable default has no streaming UI; final replies reach
    the caller through :meth:`RunnableAppService.turn`'s result and the
    output adapter's broker bridge. Deployments with a real channel pass
    their own ``emitter_factory``.
    """

    async def emit_delta(self, delta: str) -> None:
        _ = delta

    async def emit_complete(self, result: Any) -> None:
        _ = result

    async def emit_error(self, error: str) -> None:
        _ = error


def _null_emitter_factory(session_id: str, pool_name: str) -> ContentEmitter[Any]:
    _ = session_id, pool_name
    return NullEmitter()


def _safety_policy_of(config: AppConfig) -> RuntimeSafetyPolicy:
    """Map the AppConfig safety section onto the runtime policy."""
    safety = config.safety
    if safety is None:
        return RuntimeSafetyPolicy()
    return RuntimeSafetyPolicy(
        llm=LLMTimeoutPolicy(
            request_timeout_seconds=safety.llm.request_timeout,
            stream_idle_timeout_seconds=safety.llm.stream_idle_timeout,
            framework_max_retries=safety.llm.max_retries,
            retry_backoff_seconds=tuple(safety.llm.retry_backoff),
        ),
        turn=TurnTimeoutPolicy(
            hook_timeout_seconds=safety.turn.hook_timeout,
            tool_timeout_seconds=safety.turn.tool_timeout,
        ),
    )


def _retention_of(config: AppConfig) -> SessionRetentionPolicy:
    retention = config.multi_agent.session_retention
    return SessionRetentionPolicy(
        max_sessions_per_subagent=retention.max_sessions_per_subagent,
        max_sessions_global=retention.max_sessions_global,
        ttl_seconds=retention.ttl_seconds,
        cleanup_interval_seconds=retention.cleanup_interval_seconds,
    )


def _render_issues(issues: list[ScopeValidationIssue], *, phase: str) -> str:
    rendered = "; ".join(
        f"{issue.rule.value} [{issue.node}]: {issue.message}" for issue in issues
    )
    return f"scope declaration failed {phase} validation ({len(issues)} issue(s)): {rendered}"


@dataclass(frozen=True)
class _PoolBoot:
    """One declared pool's compile products, ready for ``create_pool``."""

    declared: DeclaredPoolBuild
    assembly_deps: PoolAssemblyDeps


@dataclass(frozen=True)
class _DeclarationBoot:
    """The whole declaration's boot products: the loaded spec + per-pool."""

    spec: ScopeSpec
    pools: dict[str, _PoolBoot]


@dataclass(frozen=True)
class _PoolData(PoolDataSnapshot):
    """The runnable default's concrete per-turn snapshot (no extra fields)."""


async def _build_pool_data(
    *,
    app_config: AppConfig,
    persistence: WorkspacePersistenceManager | None,
    pool_name: str,
    root_agent: AgentSpec,
    provider: LLMProvider | None,
    assembly_deps: PoolAssemblyDeps,
    paths: WorkspacePaths,
    base_system_prompt: str,
) -> _PoolData:
    """Build one pool's memory system + runtime stores (the react per-turn
    snapshot). The generic core of the bot's ``build_pool_data``: FILE-or-
    configured backends via the backend factories, the char-based token
    estimator, framework ``RecordScope``."""
    from modex_agent.agents.react.state import ReActRuntimeStateCodec
    from modex_agent.core.turn.codec import RuntimeStateCodecRegistry
    from modex_agent.core.turn.enums import AgentKind
    from modex_agent.memory.injection import FullInjectionPolicy
    from modex_agent.memory.injection.archive import ArchiveInjectionConfig
    from modex_agent.memory.system import MemorySystemContextManager
    from modex_agent.plugins.assembly.backend_factory import (
        build_memory_registry,
        build_turn_state_store,
    )
    from modex_agent.plugins.assembly.memory_factory import create_memory

    memory_cfg = assembly_deps.memory
    if memory_cfg is None:
        raise ValueError("PoolAssemblyDeps.memory must be non-None for a react pool")

    memory_dir = paths.memory_dir(pool_name)
    memory_dir.mkdir(parents=True, exist_ok=True)
    registry = build_memory_registry(
        app_config, persistence, memory_dir, RecordScope()
    )
    memory_system = create_memory(
        memory_cfg, provider, memory_dir, store_registry=registry
    )
    await memory_system.initialize()

    codec_registry = RuntimeStateCodecRegistry(
        {AgentKind.REACT: ReActRuntimeStateCodec()}
    )
    turn_store = build_turn_state_store(
        app_config,
        persistence,
        paths.runtime_dir(pool_name, "turns"),
        codec_registry,
    )
    context_manager = MemorySystemContextManager(
        memory_system=memory_system,
        default_agent_id=root_agent.name,
        default_agent_role="main",
        base_system_prompt=base_system_prompt,
        injection_policy=FullInjectionPolicy(),
        archive_injection_config=ArchiveInjectionConfig(
            count=memory_cfg.archive.max_archive_inject,
            max_chars=memory_cfg.archive.archive_inject_max_chars,
            step_chars=memory_cfg.archive.archive_inject_step_chars,
            min_chars=memory_cfg.archive.archive_inject_min_chars,
        )
        if memory_cfg.archive is not None and memory_cfg.archive.enabled
        else ArchiveInjectionConfig(count=0),
        roles=list(root_agent.roles),
    )
    return _PoolData(
        context_manager=context_manager,
        turn_store=turn_store,
        trace_store=None,
        memory_dir=memory_dir,
        runtime_dir=paths.runtime_dir(pool_name, "turns").parent,
        pruned_manager=memory_system.pruned_manager,
    )


def _declared_pools(spec: ScopeSpec) -> list[PoolSpec]:
    """Every pool the declaration hosts, in declaration order."""
    if spec.pool is not None:
        return [spec.pool]
    if spec.workspace is not None:
        return list(spec.workspace.pools)
    return []


def _pool_root(compilation: ScopeCompilation, pool_name: str) -> CompiledAgent:
    """The single compiled main agent of one declared pool."""
    pool_agents = [
        agent for agent in compilation.agents if agent.provenance.pool == pool_name
    ]
    roots = [agent for agent in pool_agents if agent.spec.agent_type in _MAIN_AGENT_TYPES]
    if len(roots) != 1:
        raise ValueError(
            f"pool {pool_name!r}: expected exactly one main agent in the "
            f"declaration, found {len(roots)}"
        )
    return roots[0]


def _declared_pool_build(
    spec: ScopeSpec, compilation: ScopeCompilation, pool: PoolSpec
) -> DeclaredPoolBuild:
    """Partition one pool's compiled agents and seed its templates."""
    root = _pool_root(compilation, pool.name)
    pool_agents = [
        agent for agent in compilation.agents if agent.provenance.pool == pool.name
    ]
    subagents = tuple(agent for agent in pool_agents if agent is not root)
    declared_agents = {agent.name: agent for agent in pool.agents}
    parents = {agent.name: agent.parent for agent in pool.agents}
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
            child
            for child in subagents
            if parents[child.provenance.agent] == root.provenance.agent
        ),
        template_registry=AgentTemplateRegistry(seeded={pool.name: templates}),
        pool=pool,
        peer_links=peer_links_from_declaration(spec).get(pool.name, ()),
    )


def _boot_declared_pools(
    *,
    declaration_path: Path,
    project_dir: Path,
    data_dir: Path,
    registry: ComponentRegistry,
) -> _DeclarationBoot:
    """Load → validate (V1-V12) → compile the declaration; partition per pool.

    Boot failure is fatal and loud — every validation issue is carried in
    the message, mirroring the production boot contract.
    """
    spec = load_scope_declaration(declaration_path)
    issues = validate_declaration(
        spec, profiles=STANDARD_PROFILES.declarations(), registry=registry
    )
    if issues:
        raise ValueError(_render_issues(issues, phase="phase-1 (declaration shape)"))
    compilation = compile_scope(
        spec,
        workspace_ctx=WorkspaceContext(
            target=project_dir,
            paths=WorkspacePaths(root=data_dir),
            is_home=True,
        ),
        default_llm_provider=DEFAULT_LLM_PROVIDER,
        registry=registry,
    )
    issues = validate_effective_configs(
        spec, [agent.effective for agent in compilation.agents]
    )
    if issues:
        raise ValueError(_render_issues(issues, phase="phase-2 (effective values)"))
    pools = _declared_pools(spec)
    if not pools:
        raise ValueError(
            f"scope declaration {declaration_path} declares no pool — the "
            "runnable default boots pools from the declaration"
        )
    return _DeclarationBoot(
        spec=spec,
        pools={
            pool.name: _PoolBoot(
                declared=_declared_pool_build(spec, compilation, pool),
                assembly_deps=PoolAssemblyDeps(
                    memory=memory_config_for_position(
                        _pool_root(compilation, pool.name).defaults
                    )
                ),
            )
            for pool in pools
        },
    )


class RunnableAppService(AppService):
    """The framework-runnable default: one declaration, every declared pool.

    Composes the ``AppService`` generic blocks (component-registry load,
    shared persistence open pair, pool routing store) with the declaration
    boot road and ``create_pool`` invocation. The declaration is the single
    authority — pools, agents, tools, hooks and LLM providers all come from
    ``roots.scope_declaration_path`` + the component registry (bundled
    defaults + the plugins under ``roots.plugins_dir``).
    """

    def __init__(
        self,
        config_dir: Path,
        input_adapter: InputAdapter,
        output_adapter: OutputAdapter,
        emitter_factory: Callable[[str, str], ContentEmitter[Any]] | None = None,
        *,
        roots: AppAssemblyRoots | None = None,
        resource_root: Path | None = None,
        app_config: AppConfig | None = None,
        app_config_filename: str = "app.yml",
    ) -> None:
        self._preloaded_app_config = app_config
        self._app_config_filename = app_config_filename
        super().__init__(
            config_dir,
            input_adapter,
            output_adapter,
            emitter_factory or _null_emitter_factory,
            roots=roots,
            resource_root=resource_root if resource_root is not None else config_dir.parent,
        )
        self._broker: InMemoryMessageBroker | None = None
        self._control_channel = InMemoryControlChannel()
        self._router_task: asyncio.Task[None] | None = None

    @property
    def pools(self) -> Mapping[str, PoolInstance]:
        """The booted pool instances, keyed by declared pool name."""
        return dict(self._pools)

    async def initialize(self) -> None:
        """config → registry → persistence → routing store → declaration boot
        → ``create_pool`` per declared pool → broker bridges → router."""
        if self._app_config is None:
            self._app_config = (
                self._preloaded_app_config
                if self._preloaded_app_config is not None
                else AppConfig.from_yaml(self.config_dir / self._app_config_filename)
            )
        app_config = self._app_config
        assert app_config is not None
        try:
            self._component_registry = await self._load_component_registry()
            await self._open_shared_persistence(app_config)
            self._pool_session_store = await self._build_pool_session_store(app_config)
            assert self._component_registry is not None
            assert self._pool_session_store is not None

            data_dir = self.roots.home_data_dir(app_config.paths.data_dir_name)
            boot = _boot_declared_pools(
                declaration_path=self.roots.scope_declaration_path,
                project_dir=self.roots.resource_root,
                data_dir=data_dir,
                registry=self._component_registry,
            )

            # The model universe: config_dir's model.yml when present (the
            # per-turn selection proxy then drives memory compression and
            # the ``multi`` slot), else the placeholder registry.
            model_yml = self.config_dir / "model.yml"
            model_registry = (
                ModelRegistry.from_yaml(model_yml) if model_yml.exists() else None
            )
            model_assembly = ModelRegistryAssembly(model_registry)
            memory_provider = (
                model_assembly.selection_provider() if model_registry is not None else None
            )

            self._broker = InMemoryMessageBroker()
            await self._broker.start()
            paths = WorkspacePaths(root=data_dir)
            for name, pool_boot in boot.pools.items():
                root_agent = pool_boot.declared.pool.root_agent
                pool_data = await _build_pool_data(
                    app_config=app_config,
                    persistence=self._home_persistence,
                    pool_name=name,
                    root_agent=root_agent,
                    provider=memory_provider,
                    assembly_deps=pool_boot.assembly_deps,
                    paths=paths,
                    base_system_prompt=await resolve_declared_root_prompt(
                        pool_boot.declared,
                        self.roots.resource_root,
                        self._component_registry,
                    ),
                )
                instance = await create_pool(
                    pool_name=name,
                    declared=pool_boot.declared,
                    assembly_deps=pool_boot.assembly_deps,
                    project_dir=self.roots.resource_root,
                    data_dir=data_dir,
                    broker=self._broker,
                    output_adapter=self.output_adapter,
                    safety=_safety_policy_of(app_config),
                    retention=_retention_of(app_config),
                    im_ui=IMUserInterface(output_adapter=self.output_adapter),
                    shared_hooks=[],
                    shared_hook_runner=HookRunner(),
                    shared_interceptor_chain=InterceptorChain(),
                    control_origin="http://127.0.0.1:0",
                    model_assembly=model_assembly,
                    default_llm_provider_name=pool_boot.declared.root.spec.llm_provider,
                    control_channel=self._control_channel,
                    emitter_factory=self.emitter_factory,
                    app_config=app_config,
                    persistence=self._home_persistence,
                    pool_data=pool_data,
                    component_registry=self._component_registry,
                    workspace_registry=object(),
                    workspace_resources=_SUPPLIED_WORKSPACE_RESOURCES,
                )
                await instance.broker_bridge.start()
                self._pools[name] = instance
            self.pool_router = PoolRouter(
                input_adapter=self.input_adapter,
                broker=self._broker,
                pools=self._pools,
                session_store=self._pool_session_store,
                default_pool=next(iter(self._pools)) if len(self._pools) == 1 else None,
                agent_pool_ownership=agent_pool_ownership(boot.spec),
            )
            logger.info(
                "RunnableAppService booted %d pool(s): %s",
                len(self._pools),
                list(self._pools),
            )
        except BaseException:
            await self.stop()
            raise

    async def start(self) -> None:
        """Start the input adapter and route its messages until shutdown."""
        await self.input_adapter.start()
        if self.pool_router is None:
            raise RuntimeError("initialize() must run before start()")
        self._router_task = asyncio.create_task(self.pool_router.run())
        try:
            await self._shutdown_event.wait()
        finally:
            self._router_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._router_task
            self._router_task = None

    async def turn(
        self, content: str, *, session: str = "demo", pool_name: str | None = None
    ) -> RequestResult:
        """Drive ONE turn through a pool's main agent on the real dispatch road.

        ``session`` is the conversation prefix; the routed session id is
        ``<session>.<root agent name>``. The returned :class:`RequestResult`
        is frozen after the turn's single quiesce — ``agent_result`` carries
        the final answer on a finished turn.
        """
        instance = self._pool_instance(pool_name)
        session_id = f"{session}.{instance.root_agent_name}"
        message = InputMessage(
            content=content,
            session=SessionInfo(
                session_id=session_id, agent_name=instance.root_agent_name
            ),
        )
        pool = instance.pool
        # Request-scope admission (the ACP-adapter road): fix the session's
        # admission policy, take the two-phase token, then run the turn.
        await instance.tree_manager.register_request_scoped(session_id)
        reservation = await pool.begin_request(session_id)
        return await pool.run_input(session_id, message, reservation=reservation)

    async def stop(self) -> None:
        """Tear down in strict order; shared persistence closes last."""
        self._shutdown_event.set()
        if self._router_task is not None:
            self._router_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._router_task
            self._router_task = None
        for instance in self._pools.values():
            with contextlib.suppress(BaseException):
                await instance.pool.shutdown_all()
            with contextlib.suppress(BaseException):
                await instance.broker_bridge.stop()
        self._pools.clear()
        if self._broker is not None:
            with contextlib.suppress(BaseException):
                await self._broker.stop()
            self._broker = None
        with contextlib.suppress(BaseException):
            await self.input_adapter.stop()
        await self._close_shared_persistence()

    def _pool_instance(self, pool_name: str | None) -> PoolInstance:
        if not self._pools:
            raise RuntimeError("no pools booted — initialize() must run first")
        if pool_name is None:
            if len(self._pools) != 1:
                raise ValueError(
                    f"several pools booted {sorted(self._pools)} — pass pool_name"
                )
            return next(iter(self._pools.values()))
        try:
            return self._pools[pool_name]
        except KeyError:
            raise ValueError(
                f"no pool {pool_name!r} among the booted pools {sorted(self._pools)}"
            ) from None
