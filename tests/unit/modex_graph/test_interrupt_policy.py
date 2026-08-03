"""Tests for `GraphInstanceStatus` + `InterruptPolicy` / `CrashPolicy`.

Covers:

- `GraphInstanceStatus` enum membership and `StrEnum` str equality.
- `InterruptPolicy` is an ABC (rule 7) — cannot instantiate directly,
  subclass must implement `handle_interrupt`.
- `CrashPolicy` is the default `InterruptPolicy` implementation; its
  `handle_interrupt` is a no-op that returns `None`.
- Subclass polymorphism: a custom policy overriding `handle_interrupt`
  is invoked correctly and receives the original `GraphInterrupt`
  and `graph_instance_id`.
"""

from __future__ import annotations

import inspect
from abc import ABC

import pytest

from modex_graph import (
    CrashPolicy,
    GraphInstanceStatus,
    GraphInterrupt,
    InterruptPolicy,
)

# ── GraphInstanceStatus ────────────────────────────────────────────────────


class TestGraphInstanceStatus:
    """`GraphInstanceStatus` StrEnum — lifecycle states."""

    def test_is_strenum(self) -> None:
        from enum import StrEnum

        assert issubclass(GraphInstanceStatus, StrEnum)

    def test_six_states(self) -> None:
        expected = {"running", "paused", "stopped", "crashed", "completed", "failed"}
        actual = {member.value for member in GraphInstanceStatus}
        assert actual == expected

    def test_member_names_match_values_uppercased(self) -> None:
        for member in GraphInstanceStatus:
            assert member.name.lower() == member.value

    def test_strenum_str_equality(self) -> None:
        assert GraphInstanceStatus.RUNNING == "running"
        assert GraphInstanceStatus.PAUSED == "paused"
        assert GraphInstanceStatus.STOPPED == "stopped"
        assert GraphInstanceStatus.CRASHED == "crashed"
        assert GraphInstanceStatus.COMPLETED == "completed"
        assert GraphInstanceStatus.FAILED == "failed"

    def test_strenum_is_str_instance(self) -> None:
        for member in GraphInstanceStatus:
            assert isinstance(member, str)

    def test_value_lookup(self) -> None:
        assert GraphInstanceStatus("running") is GraphInstanceStatus.RUNNING
        assert GraphInstanceStatus("crashed") is GraphInstanceStatus.CRASHED

    def test_unknown_value_raises(self) -> None:
        with pytest.raises(ValueError):
            GraphInstanceStatus("unknown")

    def test_recovery_states_distinct(self) -> None:
        """paused/stopped are NOT auto-recovered; crashed IS."""
        recoverable = {GraphInstanceStatus.CRASHED}
        manual_only = {GraphInstanceStatus.PAUSED, GraphInstanceStatus.STOPPED}
        terminal = {GraphInstanceStatus.COMPLETED, GraphInstanceStatus.FAILED}
        active = {GraphInstanceStatus.RUNNING}

        all_states = set(GraphInstanceStatus)
        assert recoverable | manual_only | terminal | active == all_states
        assert not (recoverable & manual_only)
        assert not (recoverable & terminal)
        assert not (manual_only & terminal)


# ── InterruptPolicy ABC ────────────────────────────────────────────────────


class TestInterruptPolicyABC:
    """`InterruptPolicy` is an ABC (rule 7) with one abstract method."""

    def test_is_abc(self) -> None:
        assert issubclass(InterruptPolicy, ABC)

    def test_cannot_instantiate_directly(self) -> None:
        with pytest.raises(TypeError):
            InterruptPolicy()  # type: ignore[abstract]

    def test_has_handle_interrupt_abstract(self) -> None:
        assert "handle_interrupt" in InterruptPolicy.__abstractmethods__

    def test_handle_interrupt_is_abstractmethod(self) -> None:
        # The attribute on the ABC is an abstractmethod descriptor.
        raw = InterruptPolicy.__dict__["handle_interrupt"]
        assert getattr(raw, "__isabstractmethod__", False) is True

    def test_handle_interrupt_is_async(self) -> None:
        assert inspect.iscoroutinefunction(InterruptPolicy.handle_interrupt)

    def test_handle_interrupt_signature(self) -> None:
        sig = inspect.signature(InterruptPolicy.handle_interrupt)
        params = list(sig.parameters.keys())
        # self + interrupt + graph_instance_id
        assert params == ["self", "interrupt", "graph_instance_id"]

    def test_subclass_without_impl_cannot_instantiate(self) -> None:
        class Incomplete(InterruptPolicy):
            pass

        with pytest.raises(TypeError):
            Incomplete()  # type: ignore[abstract]


# ── CrashPolicy default ────────────────────────────────────────────────────


class TestCrashPolicy:
    """`CrashPolicy` is the default `InterruptPolicy` — no-op `handle_interrupt`."""

    def test_is_interrupt_policy_subclass(self) -> None:
        assert issubclass(CrashPolicy, InterruptPolicy)

    def test_instantiable(self) -> None:
        CrashPolicy()

    def test_handle_interrupt_is_async(self) -> None:
        assert inspect.iscoroutinefunction(CrashPolicy.handle_interrupt)

    def test_handle_interrupt_is_coroutine_function(self) -> None:
        """Default policy method is a coroutine function (sanity check)."""
        assert inspect.iscoroutinefunction(CrashPolicy.handle_interrupt)

    async def test_handle_interrupt_is_noop(self) -> None:
        """Default policy returns None."""
        policy = CrashPolicy()
        interrupt = GraphInterrupt(value="payload", node_name="n")
        result = await policy.handle_interrupt(
            interrupt=interrupt,
            graph_instance_id=42,
        )
        assert result is None

    def test_handle_interrupt_overrides_abstract(self) -> None:
        """CrashPolicy's own dict has a concrete handle_interrupt."""
        assert "handle_interrupt" in CrashPolicy.__dict__
        raw = CrashPolicy.__dict__["handle_interrupt"]
        assert getattr(raw, "__isabstractmethod__", False) is False


# ── Subclass polymorphism ──────────────────────────────────────────────────


class TestSubclassPolymorphism:
    """A custom policy overriding `handle_interrupt` is invoked correctly."""

    async def test_custom_policy_receives_arguments(self) -> None:
        seen: dict[str, object] = {}

        class RecordingPolicy(InterruptPolicy):
            async def handle_interrupt(
                self,
                interrupt: GraphInterrupt,
                graph_instance_id: int,
            ) -> None:
                seen["interrupt"] = interrupt
                seen["graph_instance_id"] = graph_instance_id

        interrupt = GraphInterrupt(value="payload", node_name="tool_node")
        policy: InterruptPolicy = RecordingPolicy()
        await policy.handle_interrupt(
            interrupt=interrupt,
            graph_instance_id=99,
        )
        assert seen["interrupt"] is interrupt
        assert seen["graph_instance_id"] == 99

    async def test_custom_policy_isolation_from_crashpolicy(self) -> None:
        """Subclassing CrashPolicy and overriding still works."""

        class CustomCrash(CrashPolicy):
            async def handle_interrupt(
                self,
                interrupt: GraphInterrupt,
                graph_instance_id: int,
            ) -> None:
                await super().handle_interrupt(interrupt, graph_instance_id)

        policy = CustomCrash()
        ret = await policy.handle_interrupt(
            interrupt=GraphInterrupt(value="x"),
            graph_instance_id=1,
        )
        assert ret is None

    def test_subclass_typed_as_interrupt_policy(self) -> None:
        class MyPolicy(InterruptPolicy):
            async def handle_interrupt(
                self,
                interrupt: GraphInterrupt,
                graph_instance_id: int,
            ) -> None:
                return None

        p: InterruptPolicy = MyPolicy()
        assert isinstance(p, InterruptPolicy)
