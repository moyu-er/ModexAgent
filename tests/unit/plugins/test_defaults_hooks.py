"""TDD tests for default hook factories (task 12).

Written FIRST to drive the implementation of
``src/modex_agent/plugins/defaults/hooks.py``. Asserts:

1. **Registration completeness** — ``register_default_hooks`` registers
   exactly 9 hook factories in the HOOK slot with the correct names.
2. **Runner-kind dispatch** — Memory hooks (memory_trace,
   todo_reorientation) declare ``hook_runner=memory``; all others declare
   ``hook_runner=react``. Memory hooks go through
   ``memory_system.add_cleanup_hook``, NOT ``HookRunner.add``.
3. **applies_to filtering** — native_main does NOT receive
   subagent_auto_send; native_sub receives todo_reorientation via the Memory
   channel; external_sub receives subagent_auto_send (via strategy path,
   documented — NOT via Stage 4 hook dispatch).
4. **SimpleFactory vs factory-form** — deliver_retry and run_logging are
   SimpleFactory-wrapped (pre-built instance, no deps); the other 7 are
   custom ReactHookFactory/MemoryHookFactory subclasses with ``create()``
   that extracts deps from ``ctx.pool_runtime``.

Per-hook table (SPEC §6.7):

| hook               | factory type                  | applies_to                   | runner  |
|--------------------|-------------------------------|------------------------------|---------|
| inbox_flush        | ReactHookFactory, factory form| {native_main, native_sub}    | react   |
| todo_continuation  | ReactHookFactory, factory form| {native_main, native_sub}    | react   |
| deliver_retry      | ReactHookFactory, SimpleFactory| {native_main, native_sub}   | react   |
| native_env         | ReactHookFactory, factory form| {native_main, native_sub}    | react   |
| run_logging        | ReactHookFactory, SimpleFactory| {native_main, native_sub}   | react   |
| subagent_auto_send | ReactHookFactory, factory form| {external_sub, native_sub}   | react   |
| memory_trace       | MemoryHookFactory             | {native_main, native_sub}    | memory  |
| todo_reorientation | MemoryHookFactory             | {native_sub}                 | memory  |
| experience_review  | ReactHookFactory, factory form| {native_main}                | react   |
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from pydantic import BaseModel

from modex_agent.plugins.abc import (
    AgentType,
    ComponentFactory,
    HookRunnerKind,
    MemoryHookFactory,
    ReactHookFactory,
    SimpleFactory,
)
from modex_agent.plugins.assembly.context import AgentContext, PoolRuntimeDeps
from modex_agent.plugins.defaults.hooks import (
    DeliverRetryHookFactory,
    ExperienceReviewHookFactory,
    InboxFlushHookFactory,
    MemoryTraceHookFactory,
    NativeEnvInjectionHookFactory,
    RunLoggingHookFactory,
    SubagentAutoSendHookFactory,
    TodoContinuationHookFactory,
    TodoReorientationHookConfig,
    TodoReorientationHookFactory,
    register_default_hooks,
)
from modex_agent.plugins.loader import PluginRegistrationContext
from modex_agent.plugins.registry import ComponentRegistry, ComponentSlot
from modex_agent.runtime.store import TodoItem, TodoStore

# ---- Sentinel agent-type sets from the SPEC table ------------------------

_ALL_NATIVE = {AgentType.native_main, AgentType.native_sub}
_SUBAGENT_BOTH = {AgentType.external_sub, AgentType.native_sub}
_NATIVE_SUB_ONLY = {AgentType.native_sub}
_NATIVE_MAIN_ONLY = {AgentType.native_main}

#: The 9 hook names registered by ``register_default_hooks``, in table order.
_EXPECTED_HOOK_NAMES: tuple[str, ...] = (
    "inbox_flush",
    "todo_continuation",
    "deliver_retry",
    "native_env",
    "run_logging",
    "subagent_auto_send",
    "memory_trace",
    "todo_reorientation",
    "experience_review",
)


class _TodoStore(TodoStore):
    async def save(self, session_id: str, todos: list[TodoItem]) -> None:
        return None

    async def get(self, session_id: str) -> list[TodoItem]:
        return []

    async def delete(self, session_id: str) -> None:
        return None


# ---- Helpers -------------------------------------------------------------


def _register_all() -> ComponentRegistry:
    """Run ``register_default_hooks`` against a fresh registry and return it."""
    registry = ComponentRegistry()
    with PluginRegistrationContext(registry) as ctx:
        register_default_hooks(ctx)
    return registry


def _resolve(registry: ComponentRegistry, name: str) -> ComponentFactory:
    """Resolve a hook factory by name, asserting it exists."""
    factory = registry.resolve(ComponentSlot.HOOK, name)
    assert factory is not None
    return factory


def _applies_to(factory: ComponentFactory) -> set[AgentType]:
    """Read the ``applies_to`` ClassVar/instance-attr as a concrete set.

    ``None`` means "all types" — returned as the full AgentType set so
    membership tests work uniformly.
    """
    raw = getattr(factory, "applies_to", None)
    if raw is None:
        return set(AgentType)
    return set(raw)


def _hook_runner(factory: ComponentFactory) -> HookRunnerKind:
    """Read the ``hook_runner`` ClassVar/instance-attr."""
    runner = getattr(factory, "hook_runner", None)
    assert runner is not None, (
        f"Factory {type(factory).__name__} must declare hook_runner"
    )
    return runner  # type: ignore[no-any-return]


# ---- Registration completeness ------------------------------------------


class TestRegistrationCompleteness:
    def test_registers_exactly_9_hook_factories(self) -> None:
        registry = _register_all()
        hook_map = registry._factories.get(ComponentSlot.HOOK, {})
        assert len(hook_map) == 9

    @pytest.mark.parametrize("name", _EXPECTED_HOOK_NAMES)
    def test_each_expected_name_is_registered(self, name: str) -> None:
        registry = _register_all()
        # resolve raises ComponentNotFoundError if absent.
        factory = registry.resolve(ComponentSlot.HOOK, name)
        assert factory is not None

    def test_no_unexpected_extra_hooks(self) -> None:
        registry = _register_all()
        hook_map = registry._factories.get(ComponentSlot.HOOK, {})
        actual = set(hook_map.keys())
        expected = set(_EXPECTED_HOOK_NAMES)
        assert actual == expected, (
            f"Unexpected hook names: {actual - expected}; "
            f"missing: {expected - actual}"
        )


# ---- Runner-kind dispatch (react vs memory) ------------------------------


class TestRunnerKindDispatch:
    """Memory hooks → ``add_cleanup_hook`` (NOT HookRunner); react → HookRunner.

    The dispatch is read from the ``hook_runner`` ClassVar — never via
    ``isinstance`` (rule 9). These tests assert the ClassVar value so the
    stage can dispatch correctly.
    """

    @pytest.mark.parametrize(
        "name",
        [
            "inbox_flush",
            "todo_continuation",
            "deliver_retry",
            "native_env",
            "run_logging",
            "subagent_auto_send",
            "experience_review",
        ],
    )
    def test_react_hooks_have_react_runner(self, name: str) -> None:
        registry = _register_all()
        factory = _resolve(registry, name)
        assert _hook_runner(factory) is HookRunnerKind.react

    @pytest.mark.parametrize("name", ["memory_trace", "todo_reorientation"])
    def test_memory_hooks_have_memory_runner(self, name: str) -> None:
        """Memory hooks go through ``memory_system.add_cleanup_hook``.

        They do NOT go through ``HookRunner.add`` — the ``hook_runner``
        ClassVar is ``memory``, so the stage dispatches them to the
        memory cleanup runner instead of the ReAct HookRunner.
        """
        registry = _register_all()
        factory = _resolve(registry, name)
        assert _hook_runner(factory) is HookRunnerKind.memory

    def test_memory_hooks_are_memory_hook_factory_subclass(self) -> None:
        """Memory hooks MUST be ``MemoryHookFactory`` subclasses.

        This is a structural assertion complementing the ClassVar check —
        the factory class itself must inherit from ``MemoryHookFactory``,
        not just set the ClassVar to ``memory``.
        """
        registry = _register_all()
        for name in ("memory_trace", "todo_reorientation"):
            factory = _resolve(registry, name)
            assert isinstance(factory, MemoryHookFactory), (
                f"{name} must be a MemoryHookFactory instance, "
                f"got {type(factory).__name__}"
            )

    def test_react_factory_form_hooks_are_react_hook_factory_subclass(
        self,
    ) -> None:
        """Factory-form react hooks inherit from ``ReactHookFactory``."""
        registry = _register_all()
        factory_form_names = [
            "inbox_flush",
            "todo_continuation",
            "native_env",
            "subagent_auto_send",
            "experience_review",
        ]
        for name in factory_form_names:
            factory = _resolve(registry, name)
            assert isinstance(factory, ReactHookFactory), (
                f"{name} must be a ReactHookFactory instance, "
                f"got {type(factory).__name__}"
            )


# ---- SimpleFactory vs factory-form --------------------------------------


class TestFactoryForm:
    def test_deliver_retry_is_simple_factory(self) -> None:
        registry = _register_all()
        factory = registry.resolve(ComponentSlot.HOOK, "deliver_retry")
        assert isinstance(factory, SimpleFactory)

    def test_run_logging_is_simple_factory(self) -> None:
        registry = _register_all()
        factory = registry.resolve(ComponentSlot.HOOK, "run_logging")
        assert isinstance(factory, SimpleFactory)

    def test_simple_factory_hooks_still_have_hook_class_vars(self) -> None:
        """SimpleFactory-wrapped hooks MUST carry ``applies_to`` and
        ``hook_runner`` so the stage can dispatch them without
        ``isinstance`` checks.

        SimpleFactory does not inherit from HookFactory, so these are
        set as instance attributes (same pattern as ``config_model``).
        """
        registry = _register_all()
        for name in ("deliver_retry", "run_logging"):
            factory = registry.resolve(ComponentSlot.HOOK, name)
            assert hasattr(factory, "applies_to"), (
                f"{name} SimpleFactory must have applies_to set"
            )
            assert hasattr(factory, "hook_runner"), (
                f"{name} SimpleFactory must have hook_runner set"
            )

    @pytest.mark.parametrize(
        "name",
        [
            "inbox_flush",
            "todo_continuation",
            "native_env",
            "subagent_auto_send",
            "experience_review",
        ],
    )
    def test_factory_form_hooks_are_not_simple_factory(
        self, name: str
    ) -> None:
        registry = _register_all()
        factory = registry.resolve(ComponentSlot.HOOK, name)
        assert not isinstance(factory, SimpleFactory), (
            f"{name} must be a custom factory subclass, not SimpleFactory"
        )

    def test_factory_form_hooks_have_config_model(self) -> None:
        """Every factory-form hook must declare a frozen Pydantic config."""
        registry = _register_all()
        factory_form_names = [
            "inbox_flush",
            "todo_continuation",
            "native_env",
            "subagent_auto_send",
            "memory_trace",
            "todo_reorientation",
            "experience_review",
        ]
        for name in factory_form_names:
            factory = registry.resolve(ComponentSlot.HOOK, name)
            config_model = getattr(factory, "config_model", None)
            assert config_model is not None, (
                f"{name} factory must declare config_model"
            )
            assert isinstance(config_model, type), (
                f"{name} config_model must be a class"
            )
            assert issubclass(config_model, BaseModel), (
                f"{name} config_model must be a Pydantic BaseModel subclass"
            )


# ---- applies_to filtering per SPEC §6.7 ----------------------------------


class TestAppliesToFiltering:
    """Assert the ``applies_to`` set for each hook matches the SPEC table.

    The filtering simulation: for a given ``AgentType``, only hooks whose
    ``applies_to`` includes that type (or is ``None`` for "all types")
    are dispatched by Stage 4.
    """

    @pytest.mark.parametrize(
        "name,expected",
        [
            ("inbox_flush", _ALL_NATIVE),
            ("todo_continuation", _ALL_NATIVE),
            ("deliver_retry", _ALL_NATIVE),
            ("native_env", _ALL_NATIVE),
            ("run_logging", _ALL_NATIVE),
            ("subagent_auto_send", _SUBAGENT_BOTH),
            ("memory_trace", _ALL_NATIVE),
            ("todo_reorientation", _NATIVE_SUB_ONLY),
            ("experience_review", _NATIVE_MAIN_ONLY),
        ],
    )
    def test_applies_to_matches_spec_table(
        self, name: str, expected: set[AgentType]
    ) -> None:
        registry = _register_all()
        factory = _resolve(registry, name)
        actual = _applies_to(factory)
        assert actual == expected, (
            f"{name}: applies_to {actual} != expected {expected}"
        )

    def test_native_main_does_not_receive_subagent_auto_send(self) -> None:
        """Critical: native_main spec does NOT get subagent_auto_send.

        SubagentAutoSendHook is for subagents only — applying it to the
        main agent would cause the main agent to send itself a result
        notification on every turn end.
        """
        registry = _register_all()
        factory = _resolve(registry, "subagent_auto_send")
        applies = _applies_to(factory)
        assert AgentType.native_main not in applies, (
            "subagent_auto_send must NOT apply to native_main"
        )

    def test_native_sub_receives_todo_reorientation_via_memory(self) -> None:
        """todo_reorientation is also a Memory hook for native_sub."""
        registry = _register_all()
        factory = _resolve(registry, "todo_reorientation")
        assert AgentType.native_sub in _applies_to(factory)
        assert _hook_runner(factory) is HookRunnerKind.memory

    def test_external_sub_receives_subagent_auto_send(self) -> None:
        """external_sub receives subagent_auto_send.

        NOTE: external_sub dispatch is via the **strategy path**
        (ExternalExecutionStrategy), NOT via Stage 4 hook dispatch.
        Stage 4 reads ``applies_to`` and includes external_sub, but the
        actual hook registration for external subagents is handled by
        the external strategy's assemble step, not the generic
        HookRunner wiring. This is documented here — the test asserts
        ``external_sub`` is in ``applies_to``; the strategy-path
        dispatch is verified in the external strategy tests.
        """
        registry = _register_all()
        factory = _resolve(registry, "subagent_auto_send")
        applies = _applies_to(factory)
        assert AgentType.external_sub in applies, (
            "subagent_auto_send must apply to external_sub "
            "(dispatch via strategy path, not Stage 4)"
        )

    def test_experience_review_only_native_main(self) -> None:
        """experience_review is main-agent-only (after_graph spawn)."""
        registry = _register_all()
        factory = _resolve(registry, "experience_review")
        applies = _applies_to(factory)
        assert applies == {AgentType.native_main}
        assert AgentType.native_sub not in applies
        assert AgentType.external_sub not in applies


class TestExperienceReviewChainSupply:
    """Ticket 09: the factory assembles the hook from chain-supplied infra.

    A roster reference must resolve against ``pool_runtime`` supply — the
    bot-global default provider, the pool's memory system, and the
    experience dir from ``pool_assembly_ctx.pool_data``. Missing supply
    fails LOUDLY (a referenced component is never silently skipped).
    """

    @staticmethod
    def _ctx(pool_runtime: PoolRuntimeDeps | None) -> AgentContext:
        return AgentContext(
            registry=ComponentRegistry(),
            workspace_ctx=None,  # type: ignore[arg-type]
            agent_name="default",
            pool_runtime=pool_runtime,
        )

    @staticmethod
    def _pool_runtime(
        *,
        provider: object | None,
        pool_data: object | None = None,
    ) -> PoolRuntimeDeps:
        from unittest.mock import MagicMock

        from modex_agent.plugins.assembly.context import (
            PoolRuntimeDeps as _Deps,
        )

        pool_assembly = MagicMock()
        pool_assembly.pool_data = pool_data
        return _Deps(
            pool_assembly_ctx=pool_assembly,
            experience_review_provider=provider,  # type: ignore[arg-type]
        )

    @staticmethod
    def _pool_data(tmp_path, memory_system: object | None = None) -> object:
        from unittest.mock import MagicMock

        pool_data = MagicMock()
        pool_data.experience_dir = tmp_path / "experiences"
        context_manager = MagicMock()
        context_manager.memory_system = memory_system
        pool_data.context_manager = context_manager
        return pool_data

    async def test_creates_hook_from_chain_supply(self, tmp_path) -> None:
        from unittest.mock import MagicMock

        from modex_agent.core.provider import LLMProvider
        from modex_agent.hook.builtin.experience_review import ExperienceReviewHook
        from modex_agent.memory.core.system import MemorySystem

        memory_system = MagicMock(spec=MemorySystem)
        pool_data = self._pool_data(tmp_path, memory_system=memory_system)
        pool_runtime = self._pool_runtime(
            provider=MagicMock(spec=LLMProvider), pool_data=pool_data
        )
        factory = ExperienceReviewHookFactory()
        hook = await factory.create(
            factory.config_model(), self._ctx(pool_runtime)
        )
        assert isinstance(hook, ExperienceReviewHook)
        assert hook._memory_system is memory_system  # noqa: SLF001
        assert hook._get_dir() == tmp_path / "experiences"  # noqa: SLF001
        assert hook._agent._provider is not None  # noqa: SLF001

    async def test_missing_provider_supply_raises_loud(self, tmp_path) -> None:
        pool_data = self._pool_data(tmp_path, memory_system=object())
        pool_runtime = self._pool_runtime(provider=None, pool_data=pool_data)
        factory = ExperienceReviewHookFactory()
        with pytest.raises(ValueError, match="experience_review_provider"):
            await factory.create(factory.config_model(), self._ctx(pool_runtime))

    async def test_missing_pool_data_raises_loud(self) -> None:
        from unittest.mock import MagicMock

        from modex_agent.core.provider import LLMProvider

        pool_runtime = self._pool_runtime(
            provider=MagicMock(spec=LLMProvider), pool_data=None
        )
        factory = ExperienceReviewHookFactory()
        with pytest.raises(ValueError, match="pool_data"):
            await factory.create(factory.config_model(), self._ctx(pool_runtime))

    async def test_missing_pool_runtime_raises_loud(self) -> None:
        factory = ExperienceReviewHookFactory()
        with pytest.raises(ValueError, match="pool_runtime"):
            await factory.create(factory.config_model(), self._ctx(None))

    async def test_missing_memory_system_raises_loud(self, tmp_path) -> None:
        from unittest.mock import MagicMock

        from modex_agent.core.provider import LLMProvider

        pool_data = self._pool_data(tmp_path, memory_system=None)
        pool_runtime = self._pool_runtime(
            provider=MagicMock(spec=LLMProvider), pool_data=pool_data
        )
        factory = ExperienceReviewHookFactory()
        with pytest.raises(ValueError, match="memory system"):
            await factory.create(factory.config_model(), self._ctx(pool_runtime))


# ---- Filtering simulation (Stage 4 dispatch logic) ----------------------


class TestStage4FilteringSimulation:
    """Simulate the Stage 4 applies_to filtering that the future
    ``HookDispatchStage`` will perform.

    For a given ``AgentType``, collect all hooks whose ``applies_to``
    includes that type. This is the exact logic the stage will use
    (reading the ClassVar, not isinstance).
    """

    @staticmethod
    def _hooks_for(agent_type: AgentType) -> set[str]:
        registry = _register_all()
        hook_map = registry._factories.get(ComponentSlot.HOOK, {})
        result: set[str] = set()
        for name, factory in hook_map.items():
            applies = getattr(factory, "applies_to", None)
            if applies is None or agent_type in applies:
                result.add(name)
        return result

    def test_native_main_gets_7_hooks(self) -> None:
        """native_main receives: inbox_flush, todo_continuation,
        deliver_retry, native_env, run_logging, memory_trace, experience_review.

        It does NOT receive: subagent_auto_send or todo_reorientation.
        """
        hooks = self._hooks_for(AgentType.native_main)
        expected = {
            "inbox_flush",
            "todo_continuation",
            "deliver_retry",
            "native_env",
            "run_logging",
            "memory_trace",
            "experience_review",
        }
        assert hooks == expected

    def test_native_sub_gets_8_hooks(self) -> None:
        """native_sub receives: inbox_flush, todo_continuation,
        deliver_retry, native_env, run_logging, subagent_auto_send,
        memory_trace, todo_reorientation.

        It does NOT receive: experience_review.
        """
        hooks = self._hooks_for(AgentType.native_sub)
        expected = {
            "inbox_flush",
            "todo_continuation",
            "deliver_retry",
            "native_env",
            "run_logging",
            "subagent_auto_send",
            "memory_trace",
            "todo_reorientation",
        }
        assert hooks == expected

    def test_external_sub_gets_only_subagent_auto_send(self) -> None:
        """external_sub receives ONLY subagent_auto_send (via strategy path).

        All other hooks are native-only. The external strategy handles
        its own hook wiring — Stage 4 does not dispatch react hooks to
        external agents.
        """
        hooks = self._hooks_for(AgentType.external_sub)
        assert hooks == {"subagent_auto_send"}

    def test_external_main_gets_no_hooks(self) -> None:
        """external_main receives no default hooks.

        External main agents have their own hook wiring via the external
        strategy; none of the 9 default hooks apply to external_main.
        """
        hooks = self._hooks_for(AgentType.external_main)
        assert hooks == set()


# ---- Factory class structure (direct class access) ----------------------


class TestFactoryClassStructure:
    """Verify the factory classes' ClassVars directly (without registration).

    This catches cases where a factory is registered with the wrong name
    but the class itself is correct — the registration tests above catch
    the name mismatch, these tests catch the ClassVar mismatch.
    """

    def test_inbox_flush_factory_class_vars(self) -> None:
        assert InboxFlushHookFactory.applies_to == _ALL_NATIVE
        assert InboxFlushHookFactory.hook_runner is HookRunnerKind.react

    def test_todo_continuation_factory_class_vars(self) -> None:
        assert TodoContinuationHookFactory.applies_to == _ALL_NATIVE
        assert TodoContinuationHookFactory.hook_runner is HookRunnerKind.react

    def test_native_env_factory_class_vars(self) -> None:
        assert NativeEnvInjectionHookFactory.applies_to == _ALL_NATIVE
        assert NativeEnvInjectionHookFactory.hook_runner is HookRunnerKind.react

    def test_subagent_auto_send_factory_class_vars(self) -> None:
        assert SubagentAutoSendHookFactory.applies_to == _SUBAGENT_BOTH
        assert SubagentAutoSendHookFactory.hook_runner is HookRunnerKind.react

    def test_memory_trace_factory_class_vars(self) -> None:
        assert MemoryTraceHookFactory.applies_to == _ALL_NATIVE
        assert MemoryTraceHookFactory.hook_runner is HookRunnerKind.memory

    def test_todo_reorientation_factory_class_vars(self) -> None:
        assert TodoReorientationHookFactory.applies_to == _NATIVE_SUB_ONLY
        assert TodoReorientationHookFactory.hook_runner is HookRunnerKind.memory

    def test_experience_review_factory_class_vars(self) -> None:
        assert ExperienceReviewHookFactory.applies_to == _NATIVE_MAIN_ONLY
        assert ExperienceReviewHookFactory.hook_runner is HookRunnerKind.react

    def test_deliver_retry_factory_is_simple_factory(self) -> None:
        assert isinstance(DeliverRetryHookFactory, SimpleFactory)

    def test_run_logging_factory_is_simple_factory(self) -> None:
        assert isinstance(RunLoggingHookFactory, SimpleFactory)

    def test_deliver_retry_factory_has_correct_class_vars(self) -> None:
        assert DeliverRetryHookFactory.applies_to == _ALL_NATIVE
        assert DeliverRetryHookFactory.hook_runner is HookRunnerKind.react

    def test_run_logging_factory_has_correct_class_vars(self) -> None:
        assert RunLoggingHookFactory.applies_to == _ALL_NATIVE
        assert RunLoggingHookFactory.hook_runner is HookRunnerKind.react

    def test_all_factory_form_classes_have_config_model(self) -> None:
        for cls in (
            InboxFlushHookFactory,
            TodoContinuationHookFactory,
            NativeEnvInjectionHookFactory,
            SubagentAutoSendHookFactory,
            MemoryTraceHookFactory,
            TodoReorientationHookFactory,
            ExperienceReviewHookFactory,
        ):
            config_model = getattr(cls, "config_model", None)
            assert config_model is not None, (
                f"{cls.__name__} must declare config_model"
            )
            assert issubclass(config_model, BaseModel)


# ---- Memory vs React dispatch boundary -----------------------------------


class TestMemoryVsReactBoundary:
    """The two memory hooks are the ONLY hooks that go through
    ``memory_system.add_cleanup_hook``. All other hooks go through
    ``HookRunner.add``. This is the runner-kind dispatch boundary.
    """

    def test_exactly_2_memory_hooks(self) -> None:
        registry = _register_all()
        hook_map = registry._factories.get(ComponentSlot.HOOK, {})
        memory_hooks = {
            name
            for name, factory in hook_map.items()
            if getattr(factory, "hook_runner", None) is HookRunnerKind.memory
        }
        assert memory_hooks == {"memory_trace", "todo_reorientation"}

    def test_exactly_7_react_hooks(self) -> None:
        registry = _register_all()
        hook_map = registry._factories.get(ComponentSlot.HOOK, {})
        react_hooks = {
            name
            for name, factory in hook_map.items()
            if getattr(factory, "hook_runner", None) is HookRunnerKind.react
        }
        assert react_hooks == {
            "inbox_flush",
            "todo_continuation",
            "deliver_retry",
            "native_env",
            "run_logging",
            "subagent_auto_send",
            "experience_review",
        }


async def test_todo_reorientation_factory_uses_pool_runtime_store() -> None:
    store = _TodoStore()
    factory = TodoReorientationHookFactory()
    ctx = AgentContext(
        registry=MagicMock(),
        workspace_ctx=MagicMock(),
        pool_runtime=PoolRuntimeDeps(todo_store=store),
        agent_name="probe-agent",
    )

    hook = await factory.create(TodoReorientationHookConfig(), ctx)

    assert hook._todo_store is store  # noqa: SLF001


async def test_missing_pool_runtime_raises_value_error_not_assert() -> None:
    """M7: the pool_runtime state checks are ``ValueError``, not ``assert``
    (asserts vanish under ``python -O``, silently producing tree=None /
    consumer=None hooks)."""
    from modex_agent.plugins.defaults.hooks import (
        InboxFlushHookConfig,
        SubagentAutoSendHookConfig,
        TodoContinuationHookConfig,
    )

    ctx = AgentContext(
        registry=MagicMock(),
        workspace_ctx=MagicMock(),
        pool_runtime=None,
        agent_name="probe-agent",
    )

    with pytest.raises(ValueError, match="pool_runtime"):
        await TodoContinuationHookFactory().create(TodoContinuationHookConfig(), ctx)
    with pytest.raises(ValueError, match="pool_runtime"):
        await InboxFlushHookFactory().create(
            InboxFlushHookConfig(agent_name="a"), ctx
        )
    with pytest.raises(ValueError, match="pool_runtime"):
        await SubagentAutoSendHookFactory().create(
            SubagentAutoSendHookConfig(self_name="s"), ctx
        )
