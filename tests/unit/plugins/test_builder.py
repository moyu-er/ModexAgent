"""TDD tests for the assembly builder.

Written FIRST to drive the implementation of
``src/modex_agent/plugins/assembly/builder.py`` (task 10 of the
scope-converge implementation plan). Asserts:

- ``AssembledAgent`` — frozen dataclass, 8 fields, all default ``None``.
- ``AssemblyBuilder`` — regular mutable class (not frozen), same 8 fields,
  ``register_cleanup`` callback list, async ``build_agent`` + ``cleanup``.
- Cleanup contract (SPEC §6.1): reverse-order execution, exception isolation,
  idempotency.

The cleanup contract is the critical invariant: assembly failure must not
leak resources, one failing cleanup must not block others, and double-cleanup
must be a no-op (no double-free).
"""
from __future__ import annotations

import dataclasses
import inspect

import pytest

from modex_agent.plugins.assembly.builder import AssembledAgent, AssemblyBuilder


# ---- AssembledAgent (frozen output) ----


class TestAssembledAgent:
    def test_is_frozen_dataclass(self) -> None:
        assert dataclasses.is_dataclass(AssembledAgent)
        assert getattr(AssembledAgent, "__dataclass_params__").frozen is True

    def test_has_exactly_9_fields(self) -> None:
        fields = dataclasses.fields(AssembledAgent)
        assert len(fields) == 9

    def test_field_names_exact(self) -> None:
        expected = {
            "agent",
            "pool",
            "strategy_result",
            "workspace_resources",
            "infra",
            "subagent_slot",
            "descriptor",
            "propagated_context",
            "mcp_manager",
        }
        actual = {f.name for f in dataclasses.fields(AssembledAgent)}
        assert actual == expected

    def test_all_fields_default_none(self) -> None:
        """All 9 fields are optional with ``None`` default — builder accumulates
        incrementally, so no field is required at construction time."""
        instance = AssembledAgent()
        assert instance.agent is None
        assert instance.pool is None
        assert instance.strategy_result is None
        assert instance.workspace_resources is None
        assert instance.infra is None
        assert instance.subagent_slot is None
        assert instance.descriptor is None
        assert instance.propagated_context is None

    def test_frozen_immutability(self) -> None:
        instance = AssembledAgent(agent=object())
        with pytest.raises(dataclasses.FrozenInstanceError):
            instance.agent = object()  # type: ignore[misc]

    def test_carries_python_object_references(self) -> None:
        """AssembledAgent holds runtime object references (rule 11 leaf
        value-object escape hatch) — not serialized, just carried.

        Dicts are created before construction so identity (``is``) can be
        verified — a dict literal in the assert would create a new object."""
        sentinel = object()
        infra_dict = {"orchestrator": sentinel}
        slot_dict = {"slot": sentinel}
        instance = AssembledAgent(
            agent=sentinel,
            pool=sentinel,  # type: ignore[arg-type]
            strategy_result=sentinel,
            workspace_resources=sentinel,
            infra=infra_dict,
            subagent_slot=slot_dict,
        )
        assert instance.agent is sentinel
        assert instance.pool is sentinel
        assert instance.strategy_result is sentinel
        assert instance.workspace_resources is sentinel
        assert instance.infra is infra_dict
        assert instance.subagent_slot is slot_dict


# ---- AssemblyBuilder (mutable accumulator) ----


class TestAssemblyBuilder:
    def test_is_regular_class_not_frozen_dataclass(self) -> None:
        """Rule 11: never use ``@dataclass(frozen=True)`` on classes with
        behavior. AssemblyBuilder has behavior (register_cleanup, build_agent,
        cleanup) so it must be a regular mutable class."""
        assert not dataclasses.is_dataclass(AssemblyBuilder)
        assert not hasattr(AssemblyBuilder, "__dataclass_params__")

    def test_all_fields_start_none(self) -> None:
        builder = AssemblyBuilder()
        assert builder.agent is None
        assert builder.pool is None
        assert builder.strategy_result is None
        assert builder.workspace_resources is None
        assert builder.infra is None
        assert builder.subagent_slot is None
        assert builder.descriptor is None

    def test_fields_are_mutable(self) -> None:
        """Adversarial probe: builder must be mutable (not frozen). Each field
        is assignable after construction."""
        sentinel = object()
        builder = AssemblyBuilder()
        builder.agent = sentinel
        builder.pool = sentinel  # type: ignore[assignment]
        builder.strategy_result = sentinel
        builder.workspace_resources = sentinel
        builder.infra = {"key": sentinel}
        builder.subagent_slot = {"slot": sentinel}
        builder.descriptor = sentinel
        assert builder.agent is sentinel
        assert builder.pool is sentinel
        assert builder.strategy_result is sentinel
        assert builder.workspace_resources is sentinel
        assert builder.infra == {"key": sentinel}
        assert builder.subagent_slot == {"slot": sentinel}
        assert builder.descriptor is sentinel

    def test_each_builder_instance_has_independent_state(self) -> None:
        """Adversarial probe: mutable state must not leak across instances
        (no class-level mutable defaults)."""
        b1 = AssemblyBuilder()
        b1.agent = object()
        b2 = AssemblyBuilder()
        assert b2.agent is None

    def test_register_cleanup_signature(self) -> None:
        sig = inspect.signature(AssemblyBuilder.register_cleanup)
        params = list(sig.parameters)
        assert params == ["self", "coro_fn"]

    def test_build_agent_is_async(self) -> None:
        assert inspect.iscoroutinefunction(AssemblyBuilder.build_agent)

    def test_cleanup_is_async(self) -> None:
        assert inspect.iscoroutinefunction(AssemblyBuilder.cleanup)

    async def test_build_agent_returns_assembled_agent_with_accumulated_fields(self) -> None:
        sentinel = object()
        builder = AssemblyBuilder()
        builder.agent = sentinel
        builder.pool = sentinel  # type: ignore[assignment]
        builder.strategy_result = sentinel
        builder.workspace_resources = sentinel
        builder.infra = {"k": sentinel}
        builder.subagent_slot = {"s": sentinel}
        builder.descriptor = sentinel

        assembled = await builder.build_agent()

        assert isinstance(assembled, AssembledAgent)
        assert assembled.agent is sentinel
        assert assembled.pool is sentinel
        assert assembled.strategy_result is sentinel
        assert assembled.workspace_resources is sentinel
        assert assembled.infra == {"k": sentinel}
        assert assembled.subagent_slot == {"s": sentinel}
        assert assembled.descriptor is sentinel

    async def test_build_agent_with_no_fields_set_returns_all_none(self) -> None:
        builder = AssemblyBuilder()
        assembled = await builder.build_agent()
        assert isinstance(assembled, AssembledAgent)
        assert assembled.agent is None
        assert assembled.pool is None
        assert assembled.strategy_result is None
        assert assembled.workspace_resources is None
        assert assembled.infra is None
        assert assembled.subagent_slot is None
        assert assembled.descriptor is None


# ---- Cleanup contract: reverse order (SPEC §6.1) ----


class TestCleanupReverseOrder:
    """SPEC §6.1: 'builder.cleanup() 按逆序销毁已累积的资源'.

    Register A, B, C (in forward order) → cleanup must run C, B, A (reverse).
    This mirrors the resource lifecycle: workspace_resources registered first
    (base of stack), agent registered last (top of stack) → agent torn down
    first, workspace_resources torn down last.
    """

    async def test_cleanup_runs_in_reverse_registration_order(self) -> None:
        call_order: list[str] = []

        async def cleanup_a() -> None:
            call_order.append("A")

        async def cleanup_b() -> None:
            call_order.append("B")

        async def cleanup_c() -> None:
            call_order.append("C")

        builder = AssemblyBuilder()
        builder.register_cleanup(cleanup_a)
        builder.register_cleanup(cleanup_b)
        builder.register_cleanup(cleanup_c)

        await builder.cleanup()

        assert call_order == ["C", "B", "A"]

    async def test_cleanup_reverse_with_single_callback(self) -> None:
        call_order: list[str] = []

        async def cleanup_only() -> None:
            call_order.append("only")

        builder = AssemblyBuilder()
        builder.register_cleanup(cleanup_only)

        await builder.cleanup()

        assert call_order == ["only"]

    async def test_cleanup_no_callbacks_is_noop(self) -> None:
        builder = AssemblyBuilder()
        await builder.cleanup()  # must not raise


# ---- Cleanup contract: exception isolation ----


class TestCleanupExceptionIsolation:
    """SPEC §6.1: one failing cleanup must not block subsequent cleanups.

    If B's cleanup raises, A and C must still run. This ensures partial
    resource leaks don't cascade — releasing DB connection A shouldn't be
    blocked by broker B's stop() failing.
    """

    async def test_exception_in_middle_cleanup_does_not_block_others(self) -> None:
        call_order: list[str] = []

        async def cleanup_a() -> None:
            call_order.append("A")

        async def cleanup_b() -> None:
            call_order.append("B")
            raise RuntimeError("B failed")

        async def cleanup_c() -> None:
            call_order.append("C")

        builder = AssemblyBuilder()
        builder.register_cleanup(cleanup_a)
        builder.register_cleanup(cleanup_b)
        builder.register_cleanup(cleanup_c)

        # Must not raise — exception is caught and isolated.
        await builder.cleanup()

        # Reverse order: C ran, B ran (and raised), A still ran.
        assert call_order == ["C", "B", "A"]

    async def test_exception_in_first_cleanup_does_not_block_rest(self) -> None:
        """The FIRST cleanup to run (last registered) raises — the rest still run."""
        call_order: list[str] = []

        async def cleanup_a() -> None:
            call_order.append("A")

        async def cleanup_b() -> None:
            call_order.append("B")

        async def cleanup_c() -> None:
            call_order.append("C")
            raise RuntimeError("C failed")

        builder = AssemblyBuilder()
        builder.register_cleanup(cleanup_a)
        builder.register_cleanup(cleanup_b)
        builder.register_cleanup(cleanup_c)

        await builder.cleanup()

        assert call_order == ["C", "B", "A"]

    async def test_exception_in_last_cleanup_does_not_block_earlier(self) -> None:
        """The LAST cleanup to run (first registered) raises — earlier ones already ran."""
        call_order: list[str] = []

        async def cleanup_a() -> None:
            call_order.append("A")
            raise RuntimeError("A failed")

        async def cleanup_b() -> None:
            call_order.append("B")

        async def cleanup_c() -> None:
            call_order.append("C")

        builder = AssemblyBuilder()
        builder.register_cleanup(cleanup_a)
        builder.register_cleanup(cleanup_b)
        builder.register_cleanup(cleanup_c)

        await builder.cleanup()

        assert call_order == ["C", "B", "A"]

    async def test_all_cleanups_raise_all_still_attempted(self) -> None:
        """Every cleanup raises — every one is still attempted (no short-circuit)."""
        call_order: list[str] = []

        async def cleanup_a() -> None:
            call_order.append("A")
            raise RuntimeError("A")

        async def cleanup_b() -> None:
            call_order.append("B")
            raise RuntimeError("B")

        async def cleanup_c() -> None:
            call_order.append("C")
            raise RuntimeError("C")

        builder = AssemblyBuilder()
        builder.register_cleanup(cleanup_a)
        builder.register_cleanup(cleanup_b)
        builder.register_cleanup(cleanup_c)

        await builder.cleanup()

        assert call_order == ["C", "B", "A"]


# ---- Cleanup contract: idempotency ----


class TestCleanupIdempotency:
    """SPEC §6.1: cleanup must be idempotent — calling it twice must not
    double-free resources. The second call is a no-op.

    This prevents double-free bugs when assembly failure triggers cleanup
    and the caller's ``finally`` block also calls cleanup.
    """

    async def test_cleanup_twice_second_call_is_noop(self) -> None:
        call_count = 0

        async def cleanup_once() -> None:
            nonlocal call_count
            call_count += 1

        builder = AssemblyBuilder()
        builder.register_cleanup(cleanup_once)

        await builder.cleanup()
        await builder.cleanup()

        assert call_count == 1

    async def test_cleanup_thrice_all_extra_calls_are_noops(self) -> None:
        call_count = 0

        async def cleanup_once() -> None:
            nonlocal call_count
            call_count += 1

        builder = AssemblyBuilder()
        builder.register_cleanup(cleanup_once)

        await builder.cleanup()
        await builder.cleanup()
        await builder.cleanup()

        assert call_count == 1

    async def test_idempotent_with_multiple_callbacks(self) -> None:
        call_order: list[str] = []

        async def cleanup_a() -> None:
            call_order.append("A")

        async def cleanup_b() -> None:
            call_order.append("B")

        builder = AssemblyBuilder()
        builder.register_cleanup(cleanup_a)
        builder.register_cleanup(cleanup_b)

        await builder.cleanup()
        await builder.cleanup()
        await builder.cleanup()

        assert call_order == ["B", "A"]

    async def test_register_after_cleanup_does_not_run_on_second_call(self) -> None:
        """If a cleanup is registered AFTER cleanup() already ran, the second
        cleanup() call is still a no-op — the idempotency flag takes precedence."""
        call_count = 0

        async def cleanup_first() -> None:
            nonlocal call_count
            call_count += 1

        async def cleanup_second() -> None:
            nonlocal call_count
            call_count += 100

        builder = AssemblyBuilder()
        builder.register_cleanup(cleanup_first)

        await builder.cleanup()

        builder.register_cleanup(cleanup_second)
        await builder.cleanup()

        assert call_count == 1


# ---- Integration: full assembly + cleanup lifecycle ----


class TestAssemblyLifecycle:
    """End-to-end: accumulate fields, build agent, cleanup tears down resources."""

    async def test_full_lifecycle(self) -> None:
        """Simulates: builder accumulates workspace_resources → infra → pool → agent,
        registers cleanup for each, builds AssembledAgent, then cleanup tears them
        down in reverse (agent → pool → infra → workspace_resources)."""
        teardown_order: list[str] = []

        class FakePool:
            async def shutdown_all(self) -> None:
                teardown_order.append("pool")

        class FakeBridge:
            async def stop(self) -> None:
                teardown_order.append("bridge")

        class FakePoller:
            async def stop(self) -> None:
                teardown_order.append("poller")

        class FakeAgent:
            async def stop(self) -> None:
                teardown_order.append("agent")

        pool = FakePool()
        bridge = FakeBridge()
        poller = FakePoller()
        agent = FakeAgent()

        builder = AssemblyBuilder()
        # Registration order matches the SPEC's teardown order reversed:
        # workspace_resources (base) → infra → pool → agent (top).
        builder.register_cleanup(poller.stop)  # workspace_resources layer
        builder.register_cleanup(bridge.stop)  # infra layer
        builder.register_cleanup(pool.shutdown_all)  # pool layer
        builder.register_cleanup(agent.stop)  # agent layer

        builder.workspace_resources = object()
        builder.infra = {"orchestrator": object()}
        builder.pool = pool  # type: ignore[assignment]
        builder.agent = agent

        assembled = await builder.build_agent()
        assert assembled.agent is agent
        assert assembled.pool is pool

        await builder.cleanup()

        # Reverse: agent → pool → bridge → poller
        assert teardown_order == ["agent", "pool", "bridge", "poller"]

        # Idempotent.
        await builder.cleanup()
        assert teardown_order == ["agent", "pool", "bridge", "poller"]
