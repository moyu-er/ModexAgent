r"""ComponentFactory wrappers for the standard hooks (SPEC §6.7, plan task 12).

Each hook gets a factory that Stage 4 resolves from the
``ComponentRegistry`` and dispatches based on two ``ClassVar``\s read off
the factory — never via ``isinstance`` (rule 9):

- ``hook_runner`` — ``HookRunnerKind.react`` → ``HookRunner.add``;
  ``HookRunnerKind.memory`` → ``memory_system.add_cleanup_hook``.
- ``applies_to`` — ``set[AgentType]`` filter; ``None`` means all types.

Two hooks (``length_guard``, ``run_logging``) have no construction deps
and are wrapped as ``SimpleFactory`` instances (pre-built hook). The
other nine are factory-form: ``create(config, ctx)`` extracts runtime
deps from ``ctx.pool_runtime`` and combines them with serializable
settings from ``config``.

Per-hook table:

| hook                   | factory type                   | applies_to                 | runner  |
|------------------------|--------------------------------|----------------------------|---------|
| inbox_flush            | ReactHookFactory, factory form | {native_main, native_sub}  | react   |
| todo_continuation      | ReactHookFactory, factory form | {native_main, native_sub}  | react   |
| todo_planning_nudge    | ReactHookFactory, factory form | {native_main, native_sub}  | react   |
| deliver_retry          | ReactHookFactory, factory form | {native_main, native_sub}  | react   |
| length_guard           | SimpleFactory (pre-built)      | {native_main, native_sub}  | react   |
| native_env             | ReactHookFactory, factory form | {native_main, native_sub}  | react   |
| run_logging            | SimpleFactory (pre-built)      | {native_main, native_sub}  | react   |
| subagent_auto_send     | ReactHookFactory, factory form | {external_sub, native_sub} | react   |
| memory_trace           | MemoryHookFactory              | {native_main, native_sub}  | memory  |
| todo_reorientation     | MemoryHookFactory              | {native_sub}               | memory  |
| experience_review      | ReactHookFactory, factory form | {native_main}              | react   |
| trace_* (7 names)      | ReactHookFactory, resolver     | {native_main, native_sub}  | react   |

The seven ``trace_*`` factories (``trace_root`` / ``trace_chat`` /
``trace_tool`` / ``trace_handoff`` / ``trace_approval`` /
``trace_agent_start`` / ``trace_iteration``) are the ``tracing``
capability's roster resolvers: thin pickers over the per-agent wiring
artifacts (``TracingCapability.assemble`` is the single construction
authority), dispatched at ``priority=-500`` — the retired code-wired
``DefaultAgentFactory`` trace-hook injection died with that convergence
(ADR-0047 W6).

``deliver_retry`` / ``length_guard`` / ``native_env`` are position-default
roster entries (SPEC §3.2 hook rows, contributed by the compiler to every
native agent's merge base) — the retired code-wired injection sites died
with that convergence.

``subagent_auto_send`` applies to ``external_sub`` via the **strategy
path** (``ExternalExecutionStrategy.assemble``), NOT via Stage 4 hook
dispatch. Stage 4 reads ``applies_to`` and includes ``external_sub``,
but the actual hook registration for external subagents is handled by
the external strategy. This is documented — the factory declares the
intent, the strategy fulfills it.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from pydantic import BaseModel, ConfigDict

from modex_agent.agents.external.cli_resolver import resolve_modexctl_bin_dir
from modex_agent.agents.external.types import ExternalEnvSpec
from modex_agent.core.agent import AgentCommKind
from modex_agent.core.constants import ExecutionStrategyKind
from modex_agent.hook.builtin.deliver_retry import DeliverRetryHook
from modex_agent.hook.builtin.env_injection import NativeEnvInjectionHook
from modex_agent.hook.builtin.inbox_flush import InboxFlushHook
from modex_agent.hook.builtin.length_guard import LengthGuardHook
from modex_agent.hook.builtin.logging import RunLoggingHook
from modex_agent.hook.builtin.loop_detection import LoopDetectionHook
from modex_agent.hook.builtin.subagent_auto_send import SubagentAutoSendHook
from modex_agent.hook.builtin.todo_continuation import TodoContinuationHook
from modex_agent.hook.builtin.todo_planning_nudge import TodoPlanningNudgeHook
from modex_agent.ioc.configs.memory import MemoryConfig
from modex_agent.memory.cleanup_hooks import TodoReorientationHook
from modex_agent.multi_agent.communication.peer_resolution import (
    build_agent_pool_map,
    build_routable_targets,
)
from modex_agent.multi_agent.inbox.consumer import InboxConsumer
from modex_agent.plugins.abc import (
    AgentType,
    HookRunnerKind,
    MemoryHookFactory,
    ReactHookFactory,
    SimpleFactory,
)
from modex_agent.plugins.defaults.capabilities.todo import require_todo_supply
from modex_agent.plugins.defaults.capabilities.tracing import require_tracing_supply

if TYPE_CHECKING:
    from modex_agent.plugins.assembly.context import AgentContext, PoolContext
    from modex_agent.plugins.loader import PluginRegistrationContext
    from modex_agent.trace.memory_trace_hook import MemoryTraceHook

__all__ = [
    "TRACE_PRIORITY",
    "DeliverRetryHookConfig",
    "DeliverRetryHookFactory",
    "InboxFlushHookConfig",
    "InboxFlushHookFactory",
    "LengthGuardHookFactory",
    "MemoryTraceHookConfig",
    "MemoryTraceHookFactory",
    "NativeEnvInjectionHookConfig",
    "NativeEnvInjectionHookFactory",
    "RunLoggingHookFactory",
    "SubagentAutoSendHookConfig",
    "SubagentAutoSendHookFactory",
    "TodoContinuationHookConfig",
    "TodoContinuationHookFactory",
    "TodoPlanningNudgeHookFactory",
    "TodoReorientationHookConfig",
    "TodoReorientationHookFactory",
    "TraceAgentStartHookFactory",
    "TraceApprovalHookFactory",
    "TraceChatHookFactory",
    "TraceHandoffHookFactory",
    "TraceIterationHookFactory",
    "TraceRootHookFactory",
    "TraceToolHookFactory",
    "register_default_hooks",
]

# All native agent types — used by hooks that apply to both native_main
# and native_sub.
_ALL_NATIVE: frozenset[AgentType] = frozenset({AgentType.native_main, AgentType.native_sub})

# Both subagent kinds — native_sub (Stage 4 dispatch) + external_sub
# (strategy-path dispatch, documented).
_SUBAGENT_BOTH: frozenset[AgentType] = frozenset({AgentType.external_sub, AgentType.native_sub})


# ---------------------------------------------------------------------------
# Shared empty config for no-dep hooks
# ---------------------------------------------------------------------------


class _EmptyHookConfig(BaseModel):
    """Minimal frozen config for hooks with no user-configurable settings."""

    model_config = ConfigDict(frozen=True, extra="forbid")


# ---------------------------------------------------------------------------
# Config models (frozen Pydantic, rule 12)
# ---------------------------------------------------------------------------


class InboxFlushHookConfig(BaseModel):
    """Config for ``InboxFlushHookFactory``.

    ``agent_name`` is per-agent (carried by config, not ctx.pool_runtime,
    because it differs between native_main and native_sub).
    ``max_messages_per_flush`` mirrors ``InboxFlushHook``'s default.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    agent_name: str
    max_messages_per_flush: int = 10


class TodoContinuationHookConfig(BaseModel):
    """Config for ``TodoContinuationHookFactory`` — no settings (tree from ctx)."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class DeliverRetryHookConfig(BaseModel):
    """Empty config for ``DeliverRetryHookFactory`` — tree from ctx.

    The hook's tree (the per-pool ``SessionTreeManager``) is supplied
    infrastructure: ``create()`` derives it from the context chain, the
    same tree-from-ctx pattern as ``TodoContinuationHookFactory`` (the
    retired code-wired registration passed the same object from the
    same source).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")


class NativeEnvInjectionHookConfig(BaseModel):
    """Config for ``NativeEnvInjectionHookFactory``.

    ``env_spec_template`` is a frozen Pydantic model (``ExternalEnvSpec``)
    carrying pool-static env fields. Per-turn overrides are applied inside
    the hook from ``ctx.session`` at runtime.

    Absent (the position-default shape): ``create()`` derives the
    template from the context chain — pool declaration facts for pooled
    agents, workspace facts for poolless single-agent assembly.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    env_spec_template: ExternalEnvSpec | None = None


class SubagentAutoSendHookConfig(BaseModel):
    """Empty config for ``SubagentAutoSendHookFactory`` — tree from ctx.

    Every per-agent field the retired direct constructions passed
    (``self_name`` / ``parent_name`` / ``runtime_dir`` /
    ``execution_strategy`` / ``max_result_chars``) is supplied
    infrastructure: ``create()`` derives them from the
    :class:`~modex_agent.plugins.assembly.context.AgentContext` chain
    (agent identity, the declared pool tree, the pool's runtime dir, the
    agent's declared execution strategy).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")


class MemoryTraceHookConfig(BaseModel):
    """Config for the default-off ``MemoryTraceHookFactory``."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class TodoReorientationHookConfig(BaseModel):
    """Empty config for ``TodoReorientationHookFactory``.

    The hook's runtime deps are supplied infrastructure — ``create()``
    reads the todo store from ``capability_supply['todo']`` and derives
    ``has_archive`` from the pool's memory config on the context chain
    (the retired unconditional injections passed the same values).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")




# ---------------------------------------------------------------------------
# Factory-form hooks (ReactHookFactory / MemoryHookFactory subclasses)
# ---------------------------------------------------------------------------


class InboxFlushHookFactory(ReactHookFactory):
    """Factory for ``InboxFlushHook`` — inbox fold-in at turn start.

    ``create()`` constructs an ``InboxConsumer`` from
    ``ctx.pool_runtime.pool_assembly_ctx.inbox_server`` and combines it
    with ``agent_name`` from config.
    """

    config_model: ClassVar[type[BaseModel]] = InboxFlushHookConfig
    applies_to: ClassVar[set[AgentType] | None] = set(_ALL_NATIVE)

    async def create(  # type: ignore[override]
        self, config: InboxFlushHookConfig, ctx: PoolContext
    ) -> InboxFlushHook:
        pool_runtime = ctx.pool_runtime
        if pool_runtime is None:
            raise ValueError("pool_runtime must be filled by PoolAssembleStage")
        pool_assembly = pool_runtime.pool_assembly_ctx
        if pool_assembly is None:
            raise ValueError("pool_assembly_ctx must be filled")
        consumer = InboxConsumer(server=pool_assembly.inbox_server)
        return InboxFlushHook(
            consumer=consumer,
            agent_name=config.agent_name,
            max_messages_per_flush=config.max_messages_per_flush,
        )


class TodoContinuationHookFactory(ReactHookFactory):
    """Factory for ``TodoContinuationHook`` — todo-driven continuation.

    ``create()`` extracts ``tree`` from
    ``ctx.pool_runtime.session_tree_manager`` and the pool's todo store
    from ``capability_supply['todo']`` (the loud supply read — the store
    exists iff the ``todo`` capability is effective in the pool).

    ``priority = -1000``: the continuation hook runs FIRST among
    AfterTurnHook sources (its reminder, including the active todo
    list, lands before other hooks' reminders) — the same priority the
    retired code-wired registration's todo branch assigned.
    """

    config_model: ClassVar[type[BaseModel]] = TodoContinuationHookConfig
    applies_to: ClassVar[set[AgentType] | None] = set(_ALL_NATIVE)
    priority: ClassVar[int] = -1000

    async def create(  # type: ignore[override]
        self,
        config: TodoContinuationHookConfig,
        ctx: PoolContext,  # noqa: ARG002
    ) -> TodoContinuationHook:
        pool_runtime = ctx.pool_runtime
        if pool_runtime is None:
            raise ValueError("pool_runtime must be filled by PoolAssembleStage")
        return TodoContinuationHook(
            tree=pool_runtime.session_tree_manager,
            todo_store=require_todo_supply(pool_runtime).store,
        )


class TodoPlanningNudgeHookFactory(ReactHookFactory):
    """Factory for ``TodoPlanningNudgeHook`` — empty-todo planning nudge.

    ``create()`` reads the pool's todo store from
    ``capability_supply['todo']`` (the loud supply read — the store
    exists iff the ``todo`` capability is effective in the pool).
    """

    config_model: ClassVar[type[BaseModel]] = _EmptyHookConfig
    applies_to: ClassVar[set[AgentType] | None] = set(_ALL_NATIVE)
    priority: ClassVar[int] = 0

    async def create(  # type: ignore[override]
        self,
        config: _EmptyHookConfig,  # noqa: ARG002
        ctx: PoolContext,  # noqa: ARG002
    ) -> TodoPlanningNudgeHook:
        pool_runtime = ctx.pool_runtime
        if pool_runtime is None:
            raise ValueError("pool_runtime must be filled by PoolAssembleStage")
        return TodoPlanningNudgeHook(
            todo_store=require_todo_supply(pool_runtime).store,
        )


class DeliverRetryHookFactory(ReactHookFactory):
    """Factory for ``DeliverRetryHook`` — deliver-omission continuation.

    ``create()`` derives the ``tree`` from
    ``ctx.pool_runtime.session_tree_manager`` (the tree-from-ctx pattern)
    — the same per-pool object the retired code-wired registration
    received from its two callers. A chain without ``pool_runtime``
    (poolless single-agent assembly) yields ``tree=None`` — a first-class
    state the hook itself tolerates (the subtree-active check degrades to
    absent); pooled assemblies always fill the pool layer.
    """

    config_model: ClassVar[type[BaseModel]] = DeliverRetryHookConfig
    applies_to: ClassVar[set[AgentType] | None] = set(_ALL_NATIVE)

    async def create(  # type: ignore[override]
        self,
        config: DeliverRetryHookConfig,  # noqa: ARG002
        ctx: PoolContext,
    ) -> DeliverRetryHook:
        pool_runtime = ctx.pool_runtime
        tree = pool_runtime.session_tree_manager if pool_runtime is not None else None
        return DeliverRetryHook(tree=tree)


class NativeEnvInjectionHookFactory(ReactHookFactory):
    """Factory for ``NativeEnvInjectionHook`` — ``MODEX_*`` env contextvars.

    ``create()`` returns the hook with the ``env_spec_template`` from
    config when one is declared; otherwise it derives the template from
    the context chain (see :func:`_derive_native_env_spec`). Per-turn
    overrides are applied inside the hook from ``ctx.session`` at
    ``before_graph`` time.
    """

    config_model: ClassVar[type[BaseModel]] = NativeEnvInjectionHookConfig
    applies_to: ClassVar[set[AgentType] | None] = set(_ALL_NATIVE)

    async def create(  # type: ignore[override]
        self,
        config: NativeEnvInjectionHookConfig,
        ctx: AgentContext,
    ) -> NativeEnvInjectionHook:
        if config.env_spec_template is not None:
            return NativeEnvInjectionHook(env_spec_template=config.env_spec_template)
        return NativeEnvInjectionHook(env_spec_template=_derive_native_env_spec(ctx))


class SubagentAutoSendHookFactory(ReactHookFactory):
    """Factory for ``SubagentAutoSendHook`` — subagent result notification.

    ``create()`` derives every per-agent field from the
    :class:`~modex_agent.plugins.assembly.context.AgentContext` chain
    (the tree-from-ctx pattern — the retired ``AgentTemplate.materialize``
    direct construction passed the same values from the same sources):

    - ``tree`` ← ``ctx.pool_runtime.session_tree_manager``;
    - ``self_name`` ← ``ctx.agent_name`` (the agent-layer identity);
    - ``parent_name`` ← the DECLARED parent of ``ctx.agent_name`` in
      ``pool_runtime.pool_assembly_ctx.pool_spec`` (the chain carries the
      spec's tree facts — a non-root agent's declared parent is its
      notification target, including the parent_session-less cold-start
      case the retired construction skipped entirely);
    - ``runtime_dir`` ← the pool's ``pool_data.runtime_dir``
      (``None`` tolerated — the hook falls back to ``Path(".")``);
    - ``execution_strategy`` ← ``ctx.spec.execution_strategy``.

    The ``subagents`` capability contributes ``subagent_auto_send`` to
    the roster of every non-root native agent, so the roster dispatch in
    ``assemble_native_agent`` is the single registration path. Applies to
    ``external_sub`` via the **strategy path**
    (``ExternalExecutionStrategy.assemble``), NOT via Stage 4.
    """

    config_model: ClassVar[type[BaseModel]] = SubagentAutoSendHookConfig
    applies_to: ClassVar[set[AgentType] | None] = set(_SUBAGENT_BOTH)

    async def create(  # type: ignore[override]
        self, config: SubagentAutoSendHookConfig, ctx: AgentContext
    ) -> SubagentAutoSendHook:
        del config  # every field is supplied infrastructure (chain-derived)
        pool_runtime = ctx.pool_runtime
        if pool_runtime is None:
            raise ValueError("pool_runtime must be filled by PoolAssembleStage")
        spec = ctx.spec
        if spec is None:
            raise ValueError(
                "subagent_auto_send requires the per-agent spec reference on "
                "the context chain (AgentContext.spec)"
            )
        pool_assembly = pool_runtime.pool_assembly_ctx
        if pool_assembly is None:
            raise ValueError(
                "subagent_auto_send requires the pool assembly context on "
                "the chain (pool_runtime.pool_assembly_ctx) — it carries the "
                "declared pool tree the parent name derives from"
            )
        parent = next(
            (
                agent.parent
                for agent in pool_assembly.pool_spec.agents
                if agent.name == ctx.agent_name
            ),
            None,
        )
        if parent is None:
            raise ValueError(
                f"subagent_auto_send: agent {ctx.agent_name!r} has no "
                f"declared parent in pool {pool_assembly.pool_name!r} — the "
                "hook is a non-root roster entry, but the declaration tree "
                "marks this agent as a root"
            )
        runtime_dir: Path | None = (
            pool_assembly.pool_data.runtime_dir if pool_assembly.pool_data is not None else None
        )
        return SubagentAutoSendHook(
            tree=pool_runtime.session_tree_manager,
            self_name=ctx.agent_name,
            parent_name=parent,
            runtime_dir=runtime_dir,
            execution_strategy=ExecutionStrategyKind(spec.execution_strategy),
        )


class MemoryTraceHookFactory(MemoryHookFactory):
    """Lazily create the roster-enabled memory telemetry subscriber."""

    config_model: ClassVar[type[BaseModel]] = MemoryTraceHookConfig
    applies_to: ClassVar[set[AgentType] | None] = set(_ALL_NATIVE)

    async def create(
        self,
        config: BaseModel,
        ctx: PoolContext,  # noqa: ARG002
    ) -> MemoryTraceHook:
        from modex_agent.trace.memory_trace_hook import MemoryTraceHook

        store = require_tracing_supply(ctx.pool_runtime).store
        return MemoryTraceHook(store)


class TodoReorientationHookFactory(MemoryHookFactory):
    """Factory for ``TodoReorientationHook`` — post-cleanup todo reminder.

    Memory hook (``hook_runner=memory``): dispatched via
    ``memory_system.add_cleanup_hook``, NOT ``HookRunner.add``. Applies to
    every native agent (main + subagent) — the roster→memory-runner
    dispatch is the SINGLE registration path since the two unconditional
    injection points died with the todo supply convergence (SPEC §8.2 B2).

    ``create()`` reads the todo store from ``capability_supply['todo']``
    (the loud supply read) and derives ``has_archive`` from the chain:
    a native MAIN's memory follows the pool memory config (archive
    enabled → the reminder's archive-summaries paragraph — the value the
    retired ``create_pool`` injection computed); a native SUB's
    session-only memory never archives → ``False`` (the retired
    ``AgentTemplate.materialize`` injection hardcoded the same).
    """

    config_model: ClassVar[type[BaseModel]] = TodoReorientationHookConfig
    applies_to: ClassVar[set[AgentType] | None] = set(_ALL_NATIVE)

    async def create(  # type: ignore[override]
        self, config: TodoReorientationHookConfig, ctx: AgentContext
    ) -> TodoReorientationHook:
        del config
        pool_runtime = ctx.pool_runtime
        if pool_runtime is None:
            raise ValueError("pool_runtime must be filled by PoolAssembleStage")
        has_archive = False
        if ctx.spec is not None and ctx.spec.agent_type is AgentType.native_main:
            pool_assembly = pool_runtime.pool_assembly_ctx
            memory_cfg: MemoryConfig | None = (
                pool_assembly.assembly_deps.memory
                if pool_assembly is not None and pool_assembly.assembly_deps is not None
                else None
            )
            has_archive = (
                memory_cfg is not None
                and memory_cfg.archive is not None
                and memory_cfg.archive.enabled
            )
        return TodoReorientationHook(
            todo_store=require_todo_supply(pool_runtime).store,
            has_archive=has_archive,
        )



# ---------------------------------------------------------------------------
# Trace span-hook factories (the `tracing` capability's roster resolvers)
# ---------------------------------------------------------------------------

#: The trace family's dispatch priority. The retired code-wired
#: registration added the span hooks FIRST on the agent's hook runner
#: (registration order = execution order); the roster dispatch lands
#: merge-base entries ahead of declared ones anyway, and this negative
#: priority keeps the family ahead of every priority-0 hook regardless
#: of roster order — preserving the load-bearing orderings (root span
#: seeds before every consumer reads it; RootSpanHook's metrics stash
#: lands before TrainingDataHook's token sum at FINALLY_GRAPH).
TRACE_PRIORITY: int = -500


def _trace_hook_from_wiring(name: str, ctx: AgentContext) -> Any:
    """Resolve one pre-built span-hook instance from the per-agent
    tracing wiring artifacts (the require-supply pattern).

    ``TracingCapability.assemble`` is the family's SINGLE construction
    authority — one ``TraceSessionState`` shared across the seven hooks,
    built in execution order (root first, tool before handoff). The
    wiring's ``by_name`` mapping (registration name → instance) is the
    lookup face; a missing wiring (the capability not effective on this
    agent — the roster would not carry the name) or a tier-dropped
    instance raises loudly: a roster-referenced trace hook is never
    silently skipped.
    """
    wirings = ctx.capability_wirings
    wiring = wirings.get("tracing") if wirings is not None else None
    if wiring is None:
        raise ValueError(
            f"hook {name!r} requires the tracing capability's per-agent wiring "
            "(capability_wirings['tracing']); it is produced iff the tracing "
            "capability is effective on this agent — declare "
            "capabilities: {tracing: {…}}"
        )
    by_name = wiring.artifacts.get("by_name", {})
    hook = by_name.get(name)
    if hook is None:
        raise ValueError(
            f"hook {name!r} is absent from the tracing wiring's hook set "
            f"({sorted(by_name)}); the trace_spans tier or a hooks: [-…] "
            "veto dropped it — lower the tier or remove the veto"
        )
    return hook


class _TraceHookFactory(ReactHookFactory):
    """Shared shape for the seven span-hook factories: thin resolvers
    over the per-agent wiring artifacts (see
    :func:`_trace_hook_from_wiring`); all construction lives in
    ``TracingCapability.assemble``."""

    priority: ClassVar[int] = TRACE_PRIORITY

    async def create(self, config: BaseModel, ctx: AgentContext) -> Any:
        del config
        return _trace_hook_from_wiring(self.hook_name, ctx)

    @property
    def hook_name(self) -> str:
        raise NotImplementedError


class TraceRootHookFactory(_TraceHookFactory):
    config_model: ClassVar[type[BaseModel]] = _EmptyHookConfig
    applies_to: ClassVar[set[AgentType] | None] = set(_ALL_NATIVE)

    @property
    def hook_name(self) -> str:
        return "trace_root"


class TraceChatHookFactory(_TraceHookFactory):
    config_model: ClassVar[type[BaseModel]] = _EmptyHookConfig
    applies_to: ClassVar[set[AgentType] | None] = set(_ALL_NATIVE)

    @property
    def hook_name(self) -> str:
        return "trace_chat"


class TraceToolHookFactory(_TraceHookFactory):
    config_model: ClassVar[type[BaseModel]] = _EmptyHookConfig
    applies_to: ClassVar[set[AgentType] | None] = set(_ALL_NATIVE)

    @property
    def hook_name(self) -> str:
        return "trace_tool"


class TraceHandoffHookFactory(_TraceHookFactory):
    config_model: ClassVar[type[BaseModel]] = _EmptyHookConfig
    applies_to: ClassVar[set[AgentType] | None] = set(_ALL_NATIVE)

    @property
    def hook_name(self) -> str:
        return "trace_handoff"


class TraceApprovalHookFactory(_TraceHookFactory):
    config_model: ClassVar[type[BaseModel]] = _EmptyHookConfig
    applies_to: ClassVar[set[AgentType] | None] = set(_ALL_NATIVE)

    @property
    def hook_name(self) -> str:
        return "trace_approval"


class TraceAgentStartHookFactory(_TraceHookFactory):
    config_model: ClassVar[type[BaseModel]] = _EmptyHookConfig
    applies_to: ClassVar[set[AgentType] | None] = set(_ALL_NATIVE)

    @property
    def hook_name(self) -> str:
        return "trace_agent_start"


class TraceIterationHookFactory(_TraceHookFactory):
    config_model: ClassVar[type[BaseModel]] = _EmptyHookConfig
    applies_to: ClassVar[set[AgentType] | None] = set(_ALL_NATIVE)

    @property
    def hook_name(self) -> str:
        return "trace_iteration"


# ---------------------------------------------------------------------------
# SimpleFactory hooks (pre-built instances, no deps)
# ---------------------------------------------------------------------------


def _make_simple_hook_factory(
    hook_instance: Any,
    config_model: type[BaseModel],
    applies_to: set[AgentType],
    hook_runner: HookRunnerKind = HookRunnerKind.react,
) -> SimpleFactory:
    """Build a ``SimpleFactory`` with hook-dispatch ClassVars set.

    ``SimpleFactory`` does not inherit from ``HookFactory``, so
    ``applies_to`` and ``hook_runner`` are set as instance attributes
    (same pattern as ``config_model``). The stage reads them via
    ``factory.applies_to`` / ``factory.hook_runner`` (declared on
    :class:`ComponentFactory`) — never via ``isinstance``.
    """
    factory = SimpleFactory(
        instance=hook_instance,
        config_model=config_model,
        applies_to=applies_to,
        hook_runner=hook_runner,
    )
    return factory


LengthGuardHookFactory: SimpleFactory = _make_simple_hook_factory(
    hook_instance=LengthGuardHook(),
    config_model=_EmptyHookConfig,
    applies_to=set(_ALL_NATIVE),
)

LoopDetectionHookFactory: SimpleFactory = _make_simple_hook_factory(
    hook_instance=LoopDetectionHook(),
    config_model=_EmptyHookConfig,
    applies_to=set(_ALL_NATIVE),
)

RunLoggingHookFactory: SimpleFactory = _make_simple_hook_factory(
    hook_instance=RunLoggingHook(),
    config_model=_EmptyHookConfig,
    applies_to=set(_ALL_NATIVE),
)


def _derive_native_env_spec(ctx: AgentContext) -> ExternalEnvSpec:
    """Derive the ``native_env`` hook's template from the context chain.

    The retired injection sites (``_wire_main_pipeline`` for mains,
    ``AgentTemplate.materialize`` for subagents) passed these exact
    values from these exact sources:

    - Pooled agents read the pool assembly context: ``project_dir`` /
      ``pool_name`` / ``pool_spec`` / ``peer_links`` /
      ``control_origin``. A native MAIN maps the whole declared tree +
      peer roots (``build_agent_pool_map`` / ``build_routable_targets``
      — the same functions the external-pool env spec builds on); a
      native SUB maps itself + its DECLARED parent (star topology: the
      parent is the subagent's only routable target — the same
      declared-parent derivation as ``SubagentAutoSendHookFactory``).
    - Poolless single-agent assembly (no pool assembly context on the
      chain — the declared single-agent seam) builds a minimal spec from
      the workspace layer: nothing to route to, so an empty pool map
      and target list.

    ``session_id`` / ``agent_name`` are placeholders overridden per turn
    inside the hook from ``ctx.session``.
    """
    pool_runtime = ctx.pool_runtime
    pool_assembly = pool_runtime.pool_assembly_ctx if pool_runtime is not None else None
    if pool_assembly is not None:
        if ctx.spec is not None and ctx.spec.agent_type is AgentType.native_sub:
            pool_map: dict[str, str] = {ctx.agent_name: pool_assembly.pool_name}
            targets: list[tuple[str, str]] = []
            parent = next(
                (
                    agent.parent
                    for agent in pool_assembly.pool_spec.agents
                    if agent.name == ctx.agent_name
                ),
                None,
            )
            if parent is not None:
                pool_map[parent] = pool_assembly.pool_name
                targets.append((parent, ""))
            comm_kind = AgentCommKind.SUBAGENT
        else:
            pool_map = build_agent_pool_map(
                pool_assembly.pool_name, pool_assembly.pool_spec, pool_assembly.peer_links
            )
            targets = build_routable_targets(pool_assembly.pool_spec, pool_assembly.peer_links)
            comm_kind = AgentCommKind.NORMAL
        return ExternalEnvSpec(
            workspace_root=pool_assembly.project_dir,
            inbox_root=pool_assembly.project_dir / ".modex" / "inbox",
            workdir=pool_assembly.project_dir,
            session_id=f"__pending__.{ctx.agent_name}",
            agent_name=ctx.agent_name,
            provider_session_id="",
            agent_pool_map=pool_map,
            targets=targets,
            modexctl_bin_dir=resolve_modexctl_bin_dir(),
            comm_kind=comm_kind,
            control_origin=pool_assembly.control_origin,
        )
    workspace_root = ctx.workspace_ctx.target
    return ExternalEnvSpec(
        workspace_root=workspace_root,
        inbox_root=workspace_root / ".modex" / "inbox",
        workdir=workspace_root,
        session_id=f"__pending__.{ctx.agent_name}",
        agent_name=ctx.agent_name,
        provider_session_id="",
        agent_pool_map={},
        targets=[],
        modexctl_bin_dir=resolve_modexctl_bin_dir(),
        comm_kind=AgentCommKind.NORMAL,
    )


# ---------------------------------------------------------------------------
# register_default_hooks — the registration entry point
# ---------------------------------------------------------------------------


def register_default_hooks(ctx: PluginRegistrationContext) -> None:
    """Register all 19 default hook factories into *ctx*.

    Called by ``DefaultPlugin.register()`` (task 14) or directly by the
    test harness. Each factory is registered under the HOOK slot with a
    name matching the hook's logical identifier. The stage resolves
    factories by name and dispatches based on ``hook_runner`` and
    ``applies_to`` ClassVars.
    """
    ctx.register_hook("inbox_flush", InboxFlushHookFactory())
    ctx.register_hook("todo_continuation", TodoContinuationHookFactory())
    ctx.register_hook("todo_planning_nudge", TodoPlanningNudgeHookFactory())
    ctx.register_hook("deliver_retry", DeliverRetryHookFactory())
    ctx.register_hook("length_guard", LengthGuardHookFactory)
    ctx.register_hook("loop_detection", LoopDetectionHookFactory)
    ctx.register_hook("native_env", NativeEnvInjectionHookFactory())
    ctx.register_hook("run_logging", RunLoggingHookFactory)
    ctx.register_hook("subagent_auto_send", SubagentAutoSendHookFactory())
    ctx.register_hook("memory_trace", MemoryTraceHookFactory())
    ctx.register_hook("todo_reorientation", TodoReorientationHookFactory())
    # The `tracing` capability's seven span-hook resolvers (priority
    # -500; construction authority: TracingCapability.assemble).
    ctx.register_hook("trace_root", TraceRootHookFactory())
    ctx.register_hook("trace_chat", TraceChatHookFactory())
    ctx.register_hook("trace_tool", TraceToolHookFactory())
    ctx.register_hook("trace_handoff", TraceHandoffHookFactory())
    ctx.register_hook("trace_approval", TraceApprovalHookFactory())
    ctx.register_hook("trace_agent_start", TraceAgentStartHookFactory())
    ctx.register_hook("trace_iteration", TraceIterationHookFactory())
