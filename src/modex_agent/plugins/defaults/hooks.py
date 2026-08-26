r"""ComponentFactory wrappers for 11 standard hooks (SPEC §6.7, plan task 12).

Each hook gets a factory that the future ``HookDispatchStage`` (Stage 4)
resolves from the ``ComponentRegistry`` and dispatches based on two
``ClassVar``\s read off the factory — never via ``isinstance`` (rule 9):

- ``hook_runner`` — ``HookRunnerKind.react`` → ``HookRunner.add``;
  ``HookRunnerKind.memory`` → ``memory_system.add_cleanup_hook``.
- ``applies_to`` — ``set[AgentType]`` filter; ``None`` means all types.

Two hooks (``deliver_retry``, ``run_logging``) have no construction deps
and are wrapped as ``SimpleFactory`` instances (pre-built hook). The
    other nine are factory-form: ``create(config, ctx)`` extracts runtime
    deps from ``ctx.pool_runtime`` and combines them with serializable
    settings from ``config``.

Per-hook table:

| hook                   | factory type                   | applies_to                 | runner  |
|------------------------|--------------------------------|----------------------------|---------|
| inbox_flush            | ReactHookFactory, factory form | {native_main, native_sub}  | react   |
| todo_continuation      | ReactHookFactory, factory form | {native_main, native_sub}  | react   |
| deliver_retry          | SimpleFactory (pre-built)      | {native_main, native_sub}  | react   |
| native_env             | ReactHookFactory, factory form | {native_main, native_sub}  | react   |
| run_logging            | SimpleFactory (pre-built)      | {native_main, native_sub}  | react   |
| subagent_auto_send     | ReactHookFactory, factory form | {external_sub, native_sub} | react   |
| memory_trace           | MemoryHookFactory              | {native_main, native_sub}  | memory  |
| todo_reorientation     | MemoryHookFactory              | {native_sub}               | memory  |
| experience_review      | ReactHookFactory, factory form | {native_main}              | react   |
| task_delegation_nudge  | ReactHookFactory, factory form | {native_main, native_sub}  | react   |
| todo_planning_nudge    | ReactHookFactory, factory form | {native_main, native_sub}  | react   |

The two nudge hooks are self-gating (tool presence checked at runtime), so
roster references on agents without the relevant tools are silent no-ops.

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

from modex_agent.agents.external.types import ExternalEnvSpec
from modex_agent.core.constants import ExecutionStrategyKind
from modex_agent.hook.builtin.deliver_retry import DeliverRetryHook
from modex_agent.hook.builtin.env_injection import NativeEnvInjectionHook
from modex_agent.hook.builtin.experience_review import ExperienceReviewHook
from modex_agent.hook.builtin.inbox_flush import InboxFlushHook
from modex_agent.hook.builtin.logging import RunLoggingHook
from modex_agent.hook.builtin.subagent_auto_send import SubagentAutoSendHook
from modex_agent.hook.builtin.task_delegation_nudge import TaskDelegationNudgeHook
from modex_agent.hook.builtin.todo_continuation import TodoContinuationHook
from modex_agent.hook.builtin.todo_planning_nudge import TodoPlanningNudgeHook
from modex_agent.memory.cleanup_hooks import TodoReorientationHook
from modex_agent.memory.snapshot import (
    DEFAULT_SNAPSHOT_MAX_CONTENT_LEN,
    DEFAULT_SNAPSHOT_MAX_MESSAGES,
)
from modex_agent.multi_agent.inbox.consumer import InboxConsumer
from modex_agent.plugins.abc import (
    AgentType,
    HookRunnerKind,
    MemoryHookFactory,
    ReactHookFactory,
    SimpleFactory,
)

if TYPE_CHECKING:
    from modex_agent.plugins.assembly.context import AssemblyContext, PoolContext
    from modex_agent.plugins.loader import PluginRegistrationContext
    from modex_agent.trace.memory_trace_hook import MemoryTraceHook

__all__ = [
    "DeliverRetryHookFactory",
    "ExperienceReviewHookConfig",
    "ExperienceReviewHookFactory",
    "InboxFlushHookConfig",
    "InboxFlushHookFactory",
    "MemoryTraceHookConfig",
    "MemoryTraceHookFactory",
    "NativeEnvInjectionHookConfig",
    "NativeEnvInjectionHookFactory",
    "RunLoggingHookFactory",
    "SubagentAutoSendHookConfig",
    "SubagentAutoSendHookFactory",
    "TaskDelegationNudgeHookFactory",
    "TodoContinuationHookFactory",
    "TodoPlanningNudgeHookFactory",
    "TodoReorientationHookConfig",
    "TodoReorientationHookFactory",
    "register_default_hooks",
]

# All native agent types — used by hooks that apply to both native_main
# and native_sub.
_ALL_NATIVE: frozenset[AgentType] = frozenset(
    {AgentType.native_main, AgentType.native_sub}
)

# Both subagent kinds — native_sub (Stage 4 dispatch) + external_sub
# (strategy-path dispatch, documented).
_SUBAGENT_BOTH: frozenset[AgentType] = frozenset(
    {AgentType.external_sub, AgentType.native_sub}
)


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


class NativeEnvInjectionHookConfig(BaseModel):
    """Config for ``NativeEnvInjectionHookFactory``.

    ``env_spec_template`` is a frozen Pydantic model (``ExternalEnvSpec``)
    carrying pool-static env fields. The hook merges per-turn overrides
    from ``ctx.session`` at runtime.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    env_spec_template: ExternalEnvSpec


class SubagentAutoSendHookConfig(BaseModel):
    """Config for ``SubagentAutoSendHookFactory``.

    ``self_name`` / ``parent_name`` / ``runtime_dir`` /
    ``execution_strategy`` / ``max_result_chars`` are per-agent settings
    that differ between native and external subagents. ``tree`` is
    extracted from ``ctx.pool_runtime.session_tree_manager`` at
    ``create()`` time.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    self_name: str
    parent_name: str = "main"
    runtime_dir: Path | None = None
    execution_strategy: ExecutionStrategyKind = ExecutionStrategyKind.REACT
    max_result_chars: int = SubagentAutoSendHook.NOTIFY_MAX_RESULT_CHARS


class MemoryTraceHookConfig(BaseModel):
    """Config for the default-off ``MemoryTraceHookFactory``."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class TodoReorientationHookConfig(BaseModel):
    """Config for ``TodoReorientationHookFactory``.

    ``has_archive`` controls the archive-summaries paragraph in the
    reminder wording. The todo store itself is supplied infrastructure —
    ``create()`` reads ``pool_runtime.todo_store`` (``None`` on harnesses
    without pool-level todo infra; the hook skips the todo section
    gracefully).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    has_archive: bool = False


class ExperienceReviewHookConfig(BaseModel):
    """Config for ``ExperienceReviewHookFactory`` — trigger thresholds.

    The hook's runtime deps (the review agent's LLM provider, the memory
    system, the experience dir + meta store) are SUPPLIED INFRASTRUCTURE:
    ``create()`` reads them from the context chain (ticket 09) — the
    provider from ``pool_runtime.experience_review_provider`` (the
    bot-global default), the memory system + experience dir from
    ``pool_runtime.pool_assembly_ctx.pool_data``. Only serializable
    thresholds live in config.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    min_messages: int = 10
    exp_cooldown_turns: int = 3
    max_iterations: int = 50
    snapshot_max_messages: int = DEFAULT_SNAPSHOT_MAX_MESSAGES
    snapshot_max_content_len: int = DEFAULT_SNAPSHOT_MAX_CONTENT_LEN


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
    ``ctx.pool_runtime.session_tree_manager`` and the pool-level
    ``todo_store`` from ``ctx.pool_runtime`` (the supplied-infra seam).
    """

    config_model: ClassVar[type[BaseModel]] = TodoContinuationHookConfig
    applies_to: ClassVar[set[AgentType] | None] = set(_ALL_NATIVE)

    async def create(  # type: ignore[override]
        self, config: TodoContinuationHookConfig, ctx: PoolContext  # noqa: ARG002
    ) -> TodoContinuationHook:
        pool_runtime = ctx.pool_runtime
        if pool_runtime is None:
            raise ValueError("pool_runtime must be filled by PoolAssembleStage")
        return TodoContinuationHook(
            tree=pool_runtime.session_tree_manager,
            todo_store=pool_runtime.todo_store,
        )


class TaskDelegationNudgeHookFactory(ReactHookFactory):
    """Factory for ``TaskDelegationNudgeHook`` — idle-subagent dispatch nudge.

    Zero construction deps: the hook queries the tool manager and the
    dispatch tool's live roster at runtime (targets change as the store
    mutates). Self-gating — registering it on an agent without the ``task``
    tool is a silent no-op.
    """

    config_model: ClassVar[type[BaseModel]] = _EmptyHookConfig
    applies_to: ClassVar[set[AgentType] | None] = set(_ALL_NATIVE)

    async def create(  # type: ignore[override]
        self, config: BaseModel, ctx: PoolContext  # noqa: ARG002
    ) -> TaskDelegationNudgeHook:
        return TaskDelegationNudgeHook()


class TodoPlanningNudgeHookFactory(ReactHookFactory):
    """Factory for ``TodoPlanningNudgeHook`` — empty-todo planning nudge.

    ``create()`` reads the pool-level ``todo_store`` from
    ``ctx.pool_runtime`` (the supplied-infra seam). ``None`` is acceptable —
    harnesses without a pool todo store get a silently skipping hook.
    Self-gating via ``todo_write`` tool presence.
    """

    config_model: ClassVar[type[BaseModel]] = _EmptyHookConfig
    applies_to: ClassVar[set[AgentType] | None] = set(_ALL_NATIVE)

    async def create(  # type: ignore[override]
        self, config: BaseModel, ctx: PoolContext  # noqa: ARG002
    ) -> TodoPlanningNudgeHook:
        pool_runtime = ctx.pool_runtime
        todo_store = pool_runtime.todo_store if pool_runtime is not None else None
        return TodoPlanningNudgeHook(todo_store=todo_store)


class NativeEnvInjectionHookFactory(ReactHookFactory):
    """Factory for ``NativeEnvInjectionHook`` — ``MODEX_*`` env contextvars.

    ``create()`` returns the hook with the ``env_spec_template`` from
    config. Per-turn overrides are applied inside the hook from
    ``ctx.session`` at ``before_graph`` time.
    """

    config_model: ClassVar[type[BaseModel]] = NativeEnvInjectionHookConfig
    applies_to: ClassVar[set[AgentType] | None] = set(_ALL_NATIVE)

    async def create(  # type: ignore[override]
        self, config: NativeEnvInjectionHookConfig, ctx: AssemblyContext  # noqa: ARG002
    ) -> NativeEnvInjectionHook:
        return NativeEnvInjectionHook(env_spec_template=config.env_spec_template)


class SubagentAutoSendHookFactory(ReactHookFactory):
    """Factory for ``SubagentAutoSendHook`` — subagent result notification.

    ``create()`` extracts ``tree`` from
    ``ctx.pool_runtime.session_tree_manager`` and combines it with
    per-agent settings from config.

    Applies to ``external_sub`` via the **strategy path**
    (``ExternalExecutionStrategy.assemble``), NOT via Stage 4. Stage 4
    reads ``applies_to`` and includes ``external_sub``, but the external
    strategy handles the actual hook registration for external
    subagents.
    """

    config_model: ClassVar[type[BaseModel]] = SubagentAutoSendHookConfig
    applies_to: ClassVar[set[AgentType] | None] = set(_SUBAGENT_BOTH)

    async def create(  # type: ignore[override]
        self, config: SubagentAutoSendHookConfig, ctx: PoolContext
    ) -> SubagentAutoSendHook:
        pool_runtime = ctx.pool_runtime
        if pool_runtime is None:
            raise ValueError("pool_runtime must be filled by PoolAssembleStage")
        return SubagentAutoSendHook(
            tree=pool_runtime.session_tree_manager,
            self_name=config.self_name,
            parent_name=config.parent_name,
            runtime_dir=config.runtime_dir,
            execution_strategy=config.execution_strategy,
            max_result_chars=config.max_result_chars,
        )


class MemoryTraceHookFactory(MemoryHookFactory):
    """Lazily create the roster-enabled memory telemetry subscriber."""

    config_model: ClassVar[type[BaseModel]] = MemoryTraceHookConfig
    applies_to: ClassVar[set[AgentType] | None] = set(_ALL_NATIVE)

    async def create(
        self, config: BaseModel, ctx: PoolContext  # noqa: ARG002
    ) -> MemoryTraceHook:
        from modex_agent.trace.memory_trace_hook import MemoryTraceHook

        store = None
        pool_runtime = ctx.pool_runtime
        if pool_runtime is not None:
            pool_assembly = pool_runtime.pool_assembly_ctx
            if pool_assembly is not None and pool_assembly.pool_data is not None:
                store = pool_assembly.pool_data.trace_store
        return MemoryTraceHook(store)


class TodoReorientationHookFactory(MemoryHookFactory):
    """Factory for ``TodoReorientationHook`` — post-cleanup todo reminder.

    Memory hook (``hook_runner=memory``): dispatched via
    ``memory_system.add_cleanup_hook``, NOT ``HookRunner.add``.

    ``create()`` returns the hook with ``has_archive`` from config.
    ``todo_store`` is not yet on ``PoolRuntimeDeps`` — ``None`` is
    passed (the hook skips the todo section gracefully when
    ``todo_store`` is ``None``).
    """

    config_model: ClassVar[type[BaseModel]] = TodoReorientationHookConfig
    applies_to: ClassVar[set[AgentType] | None] = {AgentType.native_sub}

    async def create(  # type: ignore[override]
        self, config: TodoReorientationHookConfig, ctx: PoolContext  # noqa: ARG002
    ) -> TodoReorientationHook:
        return TodoReorientationHook(
            todo_store=(
                ctx.pool_runtime.todo_store if ctx.pool_runtime is not None else None
            ),
            has_archive=config.has_archive,
        )


class ExperienceReviewHookFactory(ReactHookFactory):
    """Factory for ``ExperienceReviewHook`` — background conversation review.

    Main-agent only (``applies_to={native_main}``). The hook spawns an
    ``ExperienceReviewAgent`` after graph execution to create/update
    EXPERIENCE.md files.

    Ticket 09 (supplied infra): ``create()`` assembles the hook from the
    context chain — the review agent is built on
    ``pool_runtime.experience_review_provider`` (the bot-global default
    provider, supplied by the orchestrator), the memory system and the
    experience dir come from ``pool_assembly_ctx.pool_data``, and the
    meta store is derived from the experience dir. Missing supply raises
    loudly — a roster-referenced component is never silently skipped.
    """

    config_model: ClassVar[type[BaseModel]] = ExperienceReviewHookConfig
    applies_to: ClassVar[set[AgentType] | None] = {AgentType.native_main}

    async def create(  # type: ignore[override]
        self, config: ExperienceReviewHookConfig, ctx: PoolContext
    ) -> ExperienceReviewHook:
        from modex_agent.agents.experience.review_agent import ExperienceReviewAgent
        from modex_agent.core.experience import PerFileExperienceMetaStore

        pool_runtime = ctx.pool_runtime
        if pool_runtime is None:
            raise ValueError(
                "experience_review requires pool_runtime; reference it from "
                "a pool roster assembled through the pipeline"
            )
        provider = pool_runtime.experience_review_provider
        if provider is None:
            raise ValueError(
                "experience_review requires the bot-global default LLM "
                "provider supply (pool_runtime.experience_review_provider); "
                "the orchestrator resolves it at pool assembly"
            )
        pool_data = (
            pool_runtime.pool_assembly_ctx.pool_data
            if pool_runtime.pool_assembly_ctx is not None
            else None
        )
        if pool_data is None or pool_data.experience_dir is None:
            raise ValueError(
                "experience_review requires the pool's pool_data "
                "(memory system + experience dir); configure the pool's "
                "experience resources"
            )
        memory_system = pool_data.context_manager.memory_system
        if memory_system is None:
            raise ValueError(
                "experience_review requires the pool's memory system"
            )
        experience_dir = pool_data.experience_dir
        review_agent = ExperienceReviewAgent(
            provider=provider,
            max_iterations=config.max_iterations,
        )
        return ExperienceReviewHook(
            review_agent=review_agent,
            memory_system=memory_system,
            experience_dir=experience_dir,
            meta_store=PerFileExperienceMetaStore(experience_dir),
            min_messages=config.min_messages,
            exp_cooldown_turns=config.exp_cooldown_turns,
            snapshot_max_messages=config.snapshot_max_messages,
            snapshot_max_content_len=config.snapshot_max_content_len,
        )


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


DeliverRetryHookFactory: SimpleFactory = _make_simple_hook_factory(
    hook_instance=DeliverRetryHook(),
    config_model=_EmptyHookConfig,
    applies_to=set(_ALL_NATIVE),
)

RunLoggingHookFactory: SimpleFactory = _make_simple_hook_factory(
    hook_instance=RunLoggingHook(),
    config_model=_EmptyHookConfig,
    applies_to=set(_ALL_NATIVE),
)


# ---------------------------------------------------------------------------
# register_default_hooks — the registration entry point
# ---------------------------------------------------------------------------


def register_default_hooks(ctx: PluginRegistrationContext) -> None:
    """Register all 11 default hook factories into *ctx*.

    Called by ``DefaultPlugin.register()`` (task 14) or directly by the
    test harness. Each factory is registered under the HOOK slot with a
    name matching the hook's logical identifier. The stage resolves
    factories by name and dispatches based on ``hook_runner`` and
    ``applies_to`` ClassVars.
    """
    ctx.register_hook("inbox_flush", InboxFlushHookFactory())
    ctx.register_hook("todo_continuation", TodoContinuationHookFactory())
    ctx.register_hook("deliver_retry", DeliverRetryHookFactory)
    ctx.register_hook("native_env", NativeEnvInjectionHookFactory())
    ctx.register_hook("run_logging", RunLoggingHookFactory)
    ctx.register_hook("subagent_auto_send", SubagentAutoSendHookFactory())
    ctx.register_hook("memory_trace", MemoryTraceHookFactory())
    ctx.register_hook("todo_reorientation", TodoReorientationHookFactory())
    ctx.register_hook("experience_review", ExperienceReviewHookFactory())
    ctx.register_hook("task_delegation_nudge", TaskDelegationNudgeHookFactory())
    ctx.register_hook("todo_planning_nudge", TodoPlanningNudgeHookFactory())
