from __future__ import annotations

from collections.abc import Callable
from enum import StrEnum
from typing import Never, assert_never

from modex_graph import (
    InvocationContext,
    InvocationStatus,
    NodeInvocationRecord,
    NodeStateStore,
)


class CrashPosition(StrEnum):
    BEFORE = "before"
    AFTER = "after"


class FaultInjectingNodeStateStore(NodeStateStore):
    def __init__(
        self,
        wrapped: NodeStateStore,
        *,
        crash_points: dict[str, CrashPosition] | None = None,
        crash_between: list[tuple[str, str]] | None = None,
        exception: Exception | None = None,
    ) -> None:
        super().__init__(wrapped.graph_instance_id)
        self._wrapped = wrapped
        self._crash_points = dict(crash_points or {})
        self._crash_between = set(crash_between or [])
        self._exception = exception or RuntimeError("injected crash")
        self._last_method: str | None = None
        self._used_up = False

    def crash_before(self, method_name: str) -> None:
        self._crash_points[method_name] = CrashPosition.BEFORE

    def crash_after(self, method_name: str) -> None:
        self._crash_points[method_name] = CrashPosition.AFTER

    def _raise_injected(self) -> Never:
        self._used_up = True
        raise self._exception

    def _invoke[ResultT](self, method_name: str, operation: Callable[[], ResultT]) -> ResultT:
        if self._used_up:
            return operation()

        previous_method = self._last_method
        if previous_method is not None and (
            previous_method,
            method_name,
        ) in self._crash_between:
            self._raise_injected()

        position = self._crash_points.get(method_name)
        match position:
            case CrashPosition.BEFORE:
                self._raise_injected()
            case CrashPosition.AFTER | None:
                pass
            case unreachable:
                assert_never(unreachable)

        result = operation()
        self._last_method = method_name

        match position:
            case CrashPosition.AFTER:
                self._raise_injected()
            case CrashPosition.BEFORE | None:
                pass
            case unreachable:
                assert_never(unreachable)
        return result

    def begin_invocation(self, node_id: str) -> InvocationContext:
        return self._invoke("begin_invocation", lambda: self._wrapped.begin_invocation(node_id))

    def complete_invocation(self, invocation: InvocationContext) -> None:
        self._invoke(
            "complete_invocation",
            lambda: self._wrapped.complete_invocation(invocation),
        )

    def crash_invocation(self, invocation: InvocationContext) -> None:
        self._invoke(
            "crash_invocation",
            lambda: self._wrapped.crash_invocation(invocation),
        )

    def cancel_invocation(self, invocation: InvocationContext) -> None:
        self._invoke(
            "cancel_invocation",
            lambda: self._wrapped.cancel_invocation(invocation),
        )

    def finalize_invocation(self, invocation: InvocationContext) -> None:
        self._invoke(
            "finalize_invocation",
            lambda: self._wrapped.finalize_invocation(invocation),
        )

    def load_latest(self, node_id: str) -> NodeInvocationRecord | None:
        return self._invoke("load_latest", lambda: self._wrapped.load_latest(node_id))

    def load_latest_completed(self, node_id: str) -> NodeInvocationRecord | None:
        return self._invoke(
            "load_latest_completed",
            lambda: self._wrapped.load_latest_completed(node_id),
        )

    def load_by_invocation_id(
        self,
        node_id: str,
        invocation_id: int,
    ) -> NodeInvocationRecord | None:
        return self._invoke(
            "load_by_invocation_id",
            lambda: self._wrapped.load_by_invocation_id(node_id, invocation_id),
        )

    def query_versions(
        self,
        node_id: str,
        status_filter: set[InvocationStatus] | None = None,
    ) -> list[NodeInvocationRecord]:
        return self._invoke(
            "query_versions",
            lambda: self._wrapped.query_versions(node_id, status_filter),
        )

    def list_nodes(self) -> list[str]:
        return self._invoke("list_nodes", self._wrapped.list_nodes)

    def query_all(
        self,
        status_filter: set[InvocationStatus],
    ) -> list[NodeInvocationRecord]:
        return self._invoke("query_all", lambda: self._wrapped.query_all(status_filter))

    def clear(self) -> None:
        self._invoke("clear", self._wrapped.clear)
