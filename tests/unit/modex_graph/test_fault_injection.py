from __future__ import annotations

import pytest
from fault_injection import CrashPosition, FaultInjectingNodeStateStore

from modex_graph import InMemoryNodeStateStore, InvocationStatus, NodeStateStore


def test_crash_before_raises_without_calling_wrapped_method() -> None:
    wrapped = InMemoryNodeStateStore(graph_instance_id=101)
    store = FaultInjectingNodeStateStore(
        wrapped,
        crash_points={"begin_invocation": CrashPosition.BEFORE},
    )

    with pytest.raises(RuntimeError, match="^injected crash$"):
        store.begin_invocation("worker")

    assert wrapped.load_latest("worker") is None


def test_crash_after_raises_with_wrapped_method_effect_visible() -> None:
    wrapped = InMemoryNodeStateStore(graph_instance_id=101)
    store = FaultInjectingNodeStateStore(wrapped)
    store.crash_after("begin_invocation")

    with pytest.raises(RuntimeError, match="^injected crash$"):
        store.begin_invocation("worker")

    latest = wrapped.load_latest("worker")
    assert latest is not None
    assert latest.status is InvocationStatus.RUNNING


def test_crash_is_one_shot_and_second_call_passes_through() -> None:
    wrapped = InMemoryNodeStateStore(graph_instance_id=101)
    store = FaultInjectingNodeStateStore(wrapped)
    store.crash_before("begin_invocation")

    with pytest.raises(RuntimeError, match="^injected crash$"):
        store.begin_invocation("worker")

    invocation = store.begin_invocation("worker")

    assert invocation.version == 0
    assert wrapped.load_latest("worker") is not None


def test_crash_between_raises_after_first_method_before_second_method() -> None:
    wrapped = InMemoryNodeStateStore(graph_instance_id=101)
    store = FaultInjectingNodeStateStore(
        wrapped,
        crash_between=[("begin_invocation", "complete_invocation")],
    )
    invocation = store.begin_invocation("worker")

    with pytest.raises(RuntimeError, match="^injected crash$"):
        store.complete_invocation(invocation)

    latest = wrapped.load_latest("worker")
    assert latest is not None
    assert latest.status is InvocationStatus.RUNNING


def test_non_crashed_methods_delegate_and_preserve_store_identity() -> None:
    wrapped = InMemoryNodeStateStore(graph_instance_id=101)
    store: NodeStateStore = FaultInjectingNodeStateStore(wrapped)

    invocation = store.begin_invocation("worker")
    store.complete_invocation(invocation)

    assert store.graph_instance_id == wrapped.graph_instance_id
    assert store.load_latest("worker") == wrapped.load_latest("worker")
    assert store.load_latest_completed("worker") == wrapped.load_latest_completed("worker")
    assert store.load_by_invocation_id("worker", invocation.invocation_id) is not None
    assert store.query_versions("worker") == wrapped.query_versions("worker")
    assert store.list_nodes() == ["worker"]
    assert store.query_all({InvocationStatus.COMPLETED}) == wrapped.query_all(
        {InvocationStatus.COMPLETED}
    )


def test_configurable_exception_is_raised() -> None:
    wrapped = InMemoryNodeStateStore(graph_instance_id=101)
    injected = LookupError("custom injected crash")
    store = FaultInjectingNodeStateStore(wrapped, exception=injected)
    store.crash_before("clear")

    with pytest.raises(LookupError, match="^custom injected crash$") as captured:
        store.clear()

    assert captured.value is injected
