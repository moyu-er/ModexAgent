"""TDD tests for default hook factories (task 12).

Written FIRST to drive the implementation of
``src/modex_agent/plugins/defaults/hooks.py``. Asserts:

1. **Registration completeness** — ``register_default_hooks`` registers
   exactly 10 hook factories in the HOOK slot with the correct names.
2. **Runner-kind dispatch** — Memory hooks (memory_trace,
   todo_reorientation) declare ``hook_runner=memory``; all others declare
   ``hook_runner=react``. Memory hooks go through
   ``memory_system.add_cleanup_hook``, NOT ``HookRunner.add``.
3. **applies_to filtering** — native_main does NOT receive
   subagent_auto_send; todo_reorientation reaches BOTH native types via
   the Memory channel (the roster→memory-runner dispatch is its single
   registration path since the todo supply convergence); external_sub
   receives subagent_auto_send (via strategy path, documented — NOT via
   Stage 4 hook dispatch).
4. **SimpleFactory vs factory-form** — length_guard and run_logging are
   SimpleFactory-wrapped (pre-built instance, no deps); the other 8 are
   custom ReactHookFactory/MemoryHookFactory subclasses with ``create()``
   that extracts deps from ``ctx.pool_runtime`` (deliver_retry derives
   the tree, native_env the env template).

Per-hook table (SPEC §6.7):

| hook                  | factory type                  | applies_to                   | runner  |
|-----------------------|-------------------------------|------------------------------|---------|
| inbox_flush           | ReactHookFactory, factory form| {native_main, native_sub}    | react   |
| todo_continuation     | ReactHookFactory, factory form| {native_main, native_sub}    | react   |
| deliver_retry         | ReactHookFactory, factory form| {native_main, native_sub}    | react   |
| length_guard          | ReactHookFactory, SimpleFactory| {native_main, native_sub}   | react   |
| native_env            | ReactHookFactory, factory form| {native_main, native_sub}    | react   |
| run_logging           | ReactHookFactory, SimpleFactory| {native_main, native_sub}   | react   |
| subagent_auto_send    | ReactHookFactory, factory form| {external_sub, native_sub}   | react   |
| memory_trace          | MemoryHookFactory             | {native_main, native_sub}    | memory  |
| todo_reorientation    | MemoryHookFactory             | {native_main, native_sub}    | memory  |
| todo_planning_nudge   | ReactHookFactory, factory form| {native_main, native_sub}    | react   |
| experience_review     | ReactHookFactory, factory form| {native_main}                | react   |
(The deprecated task-delegation nudge factory remains absent per the
capability migration deletion ledger; the todo planning nudge returned
with the todo capability bundle.)
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast
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
from modex_agent.plugins.defaults.capabilities.experience import ExperienceSupply
from modex_agent.plugins.defaults.capabilities.experience.hook_factory import (
    ExperienceReviewHookConfig,
    ExperienceReviewHookFactory,
)
from modex_agent.plugins.defaults.hooks import (
    DeliverRetryHookFactory,
    InboxFlushHookFactory,
    LengthGuardHookFactory,
    MemoryTraceHookFactory,
    NativeEnvInjectionHookFactory,
    RunLoggingHookFactory,
    SubagentAutoSendHookFactory,
    TodoContinuationHookFactory,
    TodoPlanningNudgeHookFactory,
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
_NATIVE_MAIN_ONLY = {AgentType.native_main}

#: The 11 hook names registered by ``register_default_hooks``, in table order.
_EXPECTED_HOOK_NAMES: tuple[str, ...] = (
    "inbox_flush",
    "todo_continuation",
    "todo_planning_nudge",
    "deliver_retry",
    "length_guard",
    "native_env",
    "loop_detection",
    "run_logging",
    "subagent_auto_send",
    "memory_trace",
    "todo_reorientation",
    "experience_review",
    "trace_root",
    "trace_chat",
    "trace_tool",
    "trace_handoff",
    "trace_approval",
    "trace_agent_start",
    "trace_iteration",
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
    """Run ``register_default_hooks`` plus the experience feature's hook
    registration against a fresh registry and return it (the hook factory
    is owned by the experience capability package now)."""
    from modex_agent.plugins.defaults.capabilities.experience.registration import (
        register_experience_feature,
    )

    registry = ComponentRegistry()
    with PluginRegistrationContext(registry) as ctx:
        register_default_hooks(ctx)
        register_experience_feature(ctx)
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
    assert runner is not None, f"Factory {type(factory).__name__} must declare hook_runner"
    return runner  # type: ignore[no-any-return]


# ---- Registration completeness ------------------------------------------


class TestRegistrationCompleteness:
    def test_registers_exactly_11_hook_factories(self) -> None:
        registry = _register_all()
        hook_map = registry._factories.get(ComponentSlot.HOOK, {})
        assert len(hook_map) == 19

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
            f"Unexpected hook names: {actual - expected}; missing: {expected - actual}"
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
            "todo_planning_nudge",
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
                f"{name} must be a MemoryHookFactory instance, got {type(factory).__name__}"
            )

    def test_react_factory_form_hooks_are_react_hook_factory_subclass(
        self,
    ) -> None:
        """Factory-form react hooks inherit from ``ReactHookFactory``."""
        registry = _register_all()
        factory_form_names = [
            "inbox_flush",
            "todo_continuation",
            "todo_planning_nudge",
            "native_env",
            "subagent_auto_send",
            "experience_review",
        ]
        for name in factory_form_names:
            factory = _resolve(registry, name)
            assert isinstance(factory, ReactHookFactory), (
                f"{name} must be a ReactHookFactory instance, got {type(factory).__name__}"
            )


# ---- SimpleFactory vs factory-form --------------------------------------


class TestFactoryForm:
    def test_length_guard_is_simple_factory(self) -> None:
        registry = _register_all()
        factory = registry.resolve(ComponentSlot.HOOK, "length_guard")
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
        for name in ("length_guard", "run_logging"):
            factory = registry.resolve(ComponentSlot.HOOK, name)
            assert hasattr(factory, "applies_to"), f"{name} SimpleFactory must have applies_to set"
            assert hasattr(factory, "hook_runner"), (
                f"{name} SimpleFactory must have hook_runner set"
            )

    @pytest.mark.parametrize(
        "name",
        [
            "inbox_flush",
            "todo_continuation",
            "todo_planning_nudge",
            "deliver_retry",
            "native_env",
            "subagent_auto_send",
            "experience_review",
        ],
    )
    def test_factory_form_hooks_are_not_simple_factory(self, name: str) -> None:
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
            "todo_planning_nudge",
            "native_env",
            "subagent_auto_send",
            "memory_trace",
            "todo_reorientation",
            "experience_review",
        ]
        for name in factory_form_names:
            factory = registry.resolve(ComponentSlot.HOOK, name)
            config_model = getattr(factory, "config_model", None)
            assert config_model is not None, f"{name} factory must declare config_model"
            assert isinstance(config_model, type), f"{name} config_model must be a class"
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
            ("todo_planning_nudge", _ALL_NATIVE),
            ("deliver_retry", _ALL_NATIVE),
            ("native_env", _ALL_NATIVE),
            ("run_logging", _ALL_NATIVE),
            ("subagent_auto_send", _SUBAGENT_BOTH),
            ("memory_trace", _ALL_NATIVE),
            ("todo_reorientation", _ALL_NATIVE),
            ("experience_review", _NATIVE_MAIN_ONLY),
        ],
    )
    def test_applies_to_matches_spec_table(self, name: str, expected: set[AgentType]) -> None:
        registry = _register_all()
        factory = _resolve(registry, name)
        actual = _applies_to(factory)
        assert actual == expected, f"{name}: applies_to {actual} != expected {expected}"

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
    """The factory assembles the hook from chain-supplied infra.

    A roster reference must resolve against the pool's ``experience``
    capability supply — the review provider (the deployment's default
    LLM, fail-soft when absent per §10.6), the catalog — plus the pool's
    memory system from ``pool_assembly_ctx.pool_data``. Missing supply
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
        from modex_agent.plugins.assembly.context import (
            PoolRuntimeDeps as _Deps,
        )
        from modex_agent.plugins.defaults.capabilities.experience import ExperienceSupply
        from modex_agent.plugins.defaults.capabilities.experience.catalog import (
            ExperienceCatalog,
        )
        from modex_agent.plugins.defaults.capabilities.experience.config import (
            ExperiencePoolConfig,
            ExperienceReviewConfig,
        )
        from modex_agent.plugins.defaults.capabilities.experience.metadata import (
            PerFileExperienceMetaStore,
        )

        # A REAL supply (the factory type-checks it); the dir is never
        # touched — these tests pin the missing-infra raises, not review IO.
        exp_dir = Path("/tmp/experience-supply-test")
        meta_store = PerFileExperienceMetaStore(exp_dir)
        supply = ExperienceSupply(
            pool_name="default",
            catalog=ExperienceCatalog(experience_dir=exp_dir, meta_store=meta_store),
            experience_dir=exp_dir,
            meta_store=meta_store,
            pool_config=ExperiencePoolConfig(),
            review_config_by_agent={"default": ExperienceReviewConfig()},
            review_provider=provider,
        )
        pool_assembly = MagicMock()
        pool_assembly.pool_data = pool_data
        return _Deps(
            pool_assembly_ctx=pool_assembly,
            capability_supply={"experience": supply},
        )

    @staticmethod
    def _pool_data(tmp_path, memory_system: object | None = None) -> object:
        from unittest.mock import MagicMock

        pool_data = MagicMock()
        context_manager = MagicMock()
        context_manager.memory_system = memory_system
        pool_data.context_manager = context_manager
        return pool_data

    async def test_creates_hook_from_chain_supply(self, tmp_path) -> None:
        from unittest.mock import MagicMock

        from modex_agent.core.provider import LLMProvider
        from modex_agent.memory.core.system import MemorySystem
        from modex_agent.plugins.defaults.capabilities.experience.review_hook import (
            ExperienceReviewHook,
        )

        memory_system = MagicMock(spec=MemorySystem)
        pool_data = self._pool_data(tmp_path, memory_system=memory_system)
        pool_runtime = self._pool_runtime(provider=MagicMock(spec=LLMProvider), pool_data=pool_data)
        factory = ExperienceReviewHookFactory()
        hook = await factory.create(ExperienceReviewHookConfig(), self._ctx(pool_runtime))
        assert isinstance(hook, ExperienceReviewHook)
        assert hook._memory_system is memory_system  # noqa: SLF001
        assert cast("ExperienceSupply", pool_runtime.capability_supply["experience"]).review_agent_for("default") is not None

    async def test_missing_provider_is_fail_soft(self, tmp_path) -> None:
        """§10.6: no review LLM → the hook still builds; the reviewer is
        absent and reviews skip with a warning at run time."""
        pool_data = self._pool_data(tmp_path, memory_system=object())
        pool_runtime = self._pool_runtime(provider=None, pool_data=pool_data)
        factory = ExperienceReviewHookFactory()
        hook = await factory.create(ExperienceReviewHookConfig(), self._ctx(pool_runtime))
        assert (
            cast("ExperienceSupply", pool_runtime.capability_supply["experience"]).review_agent_for(
                "default"
            )
            is None
        )
        assert hook.name == "experience_review_hook"

    async def test_missing_pool_data_raises_loud(self) -> None:
        from unittest.mock import MagicMock

        from modex_agent.core.provider import LLMProvider

        pool_runtime = self._pool_runtime(provider=MagicMock(spec=LLMProvider), pool_data=None)
        factory = ExperienceReviewHookFactory()
        with pytest.raises(ValueError, match="pool_data"):
            await factory.create(ExperienceReviewHookConfig(), self._ctx(pool_runtime))

    async def test_missing_pool_runtime_raises_loud(self) -> None:
        factory = ExperienceReviewHookFactory()
        with pytest.raises(ValueError, match="pool_runtime"):
            await factory.create(ExperienceReviewHookConfig(), self._ctx(None))

    async def test_missing_capability_supply_raises_loud(self) -> None:
        from modex_agent.plugins.assembly.context import PoolRuntimeDeps as _Deps

        pool_runtime = _Deps()
        factory = ExperienceReviewHookFactory()
        with pytest.raises(ValueError, match="experience"):
            await factory.create(ExperienceReviewHookConfig(), self._ctx(pool_runtime))

    async def test_missing_memory_system_raises_loud(self, tmp_path) -> None:
        from unittest.mock import MagicMock

        from modex_agent.core.provider import LLMProvider

        pool_data = self._pool_data(tmp_path, memory_system=None)
        pool_runtime = self._pool_runtime(provider=MagicMock(spec=LLMProvider), pool_data=pool_data)
        factory = ExperienceReviewHookFactory()
        with pytest.raises(ValueError, match="memory system"):
            await factory.create(ExperienceReviewHookConfig(), self._ctx(pool_runtime))


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
        assert TodoReorientationHookFactory.applies_to == _ALL_NATIVE
        assert TodoReorientationHookFactory.hook_runner is HookRunnerKind.memory

    def test_experience_review_factory_class_vars(self) -> None:
        assert ExperienceReviewHookFactory.applies_to == _NATIVE_MAIN_ONLY
        assert ExperienceReviewHookFactory.hook_runner is HookRunnerKind.react

    def test_length_guard_factory_is_simple_factory(self) -> None:
        assert isinstance(LengthGuardHookFactory, SimpleFactory)

    def test_run_logging_factory_is_simple_factory(self) -> None:
        assert isinstance(RunLoggingHookFactory, SimpleFactory)

    def test_deliver_retry_factory_has_correct_class_vars(self) -> None:
        assert DeliverRetryHookFactory.applies_to == _ALL_NATIVE
        assert DeliverRetryHookFactory.hook_runner is HookRunnerKind.react

    def test_length_guard_factory_has_correct_class_vars(self) -> None:
        assert LengthGuardHookFactory.applies_to == _ALL_NATIVE
        assert LengthGuardHookFactory.hook_runner is HookRunnerKind.react

    def test_run_logging_factory_has_correct_class_vars(self) -> None:
        assert RunLoggingHookFactory.applies_to == _ALL_NATIVE
        assert RunLoggingHookFactory.hook_runner is HookRunnerKind.react

    def test_todo_planning_nudge_factory_has_correct_class_vars(self) -> None:
        assert TodoPlanningNudgeHookFactory.applies_to == _ALL_NATIVE
        assert TodoPlanningNudgeHookFactory.hook_runner is HookRunnerKind.react
        assert TodoPlanningNudgeHookFactory.priority == 0

    def test_all_factory_form_classes_have_config_model(self) -> None:
        for cls in (
            InboxFlushHookFactory,
            TodoContinuationHookFactory,
            TodoPlanningNudgeHookFactory,
            DeliverRetryHookFactory,
            NativeEnvInjectionHookFactory,
            SubagentAutoSendHookFactory,
            MemoryTraceHookFactory,
            TodoReorientationHookFactory,
            ExperienceReviewHookFactory,
        ):
            config_model = getattr(cls, "config_model", None)
            assert config_model is not None, f"{cls.__name__} must declare config_model"
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


async def test_todo_reorientation_factory_uses_pool_supply_store() -> None:
    from modex_agent.plugins.defaults.capabilities.todo import TodoSupply

    store = _TodoStore()
    factory = TodoReorientationHookFactory()
    ctx = AgentContext(
        registry=MagicMock(),
        workspace_ctx=MagicMock(),
        pool_runtime=PoolRuntimeDeps(capability_supply={"todo": TodoSupply(store=store)}),
        agent_name="probe-agent",
    )

    hook = await factory.create(TodoReorientationHookConfig(), ctx)

    assert hook._todo_store is store  # noqa: SLF001
    assert hook._has_archive is False  # noqa: SLF001


async def test_todo_planning_nudge_factory_uses_pool_supply_store() -> None:
    from modex_agent.plugins.defaults.capabilities.todo import TodoSupply
    from modex_agent.plugins.defaults.hooks import TodoPlanningNudgeHookFactory

    store = _TodoStore()
    factory = TodoPlanningNudgeHookFactory()
    ctx = AgentContext(
        registry=MagicMock(),
        workspace_ctx=MagicMock(),
        pool_runtime=PoolRuntimeDeps(capability_supply={"todo": TodoSupply(store=store)}),
        agent_name="probe-agent",
    )

    hook = await factory.create(MagicMock(), ctx)

    assert hook._todo_store is store  # noqa: SLF001


async def test_todo_planning_nudge_factory_raises_without_todo_supply() -> None:
    """A pool runtime without the todo supply means the capability is not
    effective anywhere in the pool — a roster-referenced nudge factory
    must fail loudly, never build a store-less hook."""
    factory = TodoPlanningNudgeHookFactory()
    ctx = AgentContext(
        registry=MagicMock(),
        workspace_ctx=MagicMock(),
        pool_runtime=PoolRuntimeDeps(capability_supply={}),
        agent_name="probe-agent",
    )

    with pytest.raises(ValueError, match="todo"):
        await factory.create(MagicMock(), ctx)


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
        await InboxFlushHookFactory().create(InboxFlushHookConfig(agent_name="a"), ctx)
    with pytest.raises(ValueError, match="pool_runtime"):
        await SubagentAutoSendHookFactory().create(SubagentAutoSendHookConfig(), ctx)
    with pytest.raises(ValueError, match="pool_runtime"):
        await TodoPlanningNudgeHookFactory().create(MagicMock(), ctx)


# ---- SubagentAutoSendHookFactory: tree-from-ctx (B4) ------------------------


def _auto_send_ctx(
    *,
    agent_name: str = "sub",
    pool_spec_agents: tuple[tuple[str, str | None], ...] = (("root", None), ("sub", "root")),
    runtime_dir: Any = None,
    execution_strategy: str = "react",
    session_tree_manager: Any = None,
) -> AgentContext:
    """A full-chain ctx carrying the declared pool tree the factory
    derives its per-agent fields from."""
    from modex_agent.multi_agent.execution_strategy import PoolAssemblyContext
    from modex_agent.plugins.assembly.spec import AssemblySpec
    from modex_agent.scope.spec import AgentSpec, PoolSpec

    pool_spec = PoolSpec(
        name="p",
        agents=[AgentSpec(name=name, parent=parent) for name, parent in pool_spec_agents],
    )
    pool_data = MagicMock()
    pool_data.runtime_dir = runtime_dir
    pool_assembly = MagicMock(spec=PoolAssemblyContext)
    pool_assembly.pool_name = "p"
    pool_assembly.pool_spec = pool_spec
    pool_assembly.pool_data = pool_data
    spec = MagicMock(spec=AssemblySpec)
    spec.execution_strategy = execution_strategy
    return AgentContext(
        registry=MagicMock(),
        workspace_ctx=MagicMock(),
        pool_runtime=PoolRuntimeDeps(
            session_tree_manager=session_tree_manager,
            pool_assembly_ctx=pool_assembly,
        ),
        agent_name=agent_name,
        spec=spec,
    )


async def test_auto_send_factory_derives_fields_from_the_chain() -> None:
    from modex_agent.hook.builtin.subagent_auto_send import SubagentAutoSendHook
    from modex_agent.plugins.defaults.hooks import SubagentAutoSendHookConfig

    runtime_dir = MagicMock(name="runtime_dir")
    tree = MagicMock(name="tree")
    ctx = _auto_send_ctx(runtime_dir=runtime_dir, session_tree_manager=tree)

    hook = await SubagentAutoSendHookFactory().create(SubagentAutoSendHookConfig(), ctx)

    assert isinstance(hook, SubagentAutoSendHook)
    assert hook._self_name == "sub"  # noqa: SLF001
    assert hook._parent_name == "root"  # noqa: SLF001
    assert hook._runtime_dir is runtime_dir  # noqa: SLF001
    assert hook._execution_strategy.value == "react"  # noqa: SLF001
    assert hook._tree is tree  # noqa: SLF001


async def test_auto_send_factory_loud_when_the_chain_lacks_the_pool_tree() -> None:
    from modex_agent.plugins.assembly.spec import AssemblySpec
    from modex_agent.plugins.defaults.hooks import SubagentAutoSendHookConfig

    spec = MagicMock(spec=AssemblySpec)
    spec.execution_strategy = "react"
    ctx = AgentContext(
        registry=MagicMock(),
        workspace_ctx=MagicMock(),
        pool_runtime=PoolRuntimeDeps(),  # no pool_assembly_ctx
        agent_name="sub",
        spec=spec,
    )

    with pytest.raises(ValueError, match="pool_assembly_ctx"):
        await SubagentAutoSendHookFactory().create(SubagentAutoSendHookConfig(), ctx)


async def test_auto_send_factory_loud_for_a_root_agent() -> None:
    """The hook is a non-root roster entry; a root agent reaching this
    factory means the declaration tree and the roster disagree."""
    from modex_agent.plugins.defaults.hooks import SubagentAutoSendHookConfig

    ctx = _auto_send_ctx(agent_name="root")

    with pytest.raises(ValueError, match="declared parent"):
        await SubagentAutoSendHookFactory().create(SubagentAutoSendHookConfig(), ctx)
