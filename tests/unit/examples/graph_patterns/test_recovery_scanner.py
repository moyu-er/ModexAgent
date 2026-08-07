# ruff: noqa: ANN401, S101

from __future__ import annotations

import asyncio
import sqlite3
import sys
from importlib import import_module
from pathlib import Path
from typing import Any

import pytest

from modex_graph import (
    EdgeSpec,
    FunctionNodeFactory,
    GraphContext,
    GraphInstanceStatus,
    GraphMetadata,
    GraphNode,
    GraphSpec,
    GraphState,
    NodeRegistry,
    NodeSpec,
)

_EXAMPLES_DIR = Path(__file__).parent.parent.parent.parent.parent / "examples"
if str(_EXAMPLES_DIR) not in sys.path:
    sys.path.insert(0, str(_EXAMPLES_DIR))

_recovery_scanner = import_module("graph_patterns.recovery_scanner")
RecoveryScanner = _recovery_scanner.RecoveryScanner
assemble_sqlite_orchestrator = _recovery_scanner.assemble_sqlite_orchestrator
open_workspace_connection = _recovery_scanner.open_workspace_connection


class ScannerState(GraphState):
    pass


def _registry(function: Any) -> NodeRegistry:
    registry = NodeRegistry()
    registry.register("function", FunctionNodeFactory({"run": function}))
    return registry


def _spec() -> GraphSpec:
    return GraphSpec(
        name="scanner_smoke",
        nodes=[NodeSpec(name="run", node_type="function", config={"function": "run"})],
        edges=[
            EdgeSpec(source=GraphNode.START, target="run"),
            EdgeSpec(source="run", target=GraphNode.END),
        ],
        state_class="scanner",
    )


async def test_scanner_assembles_sqlite_orchestrator_and_recovers_on_startup(
    tmp_path: Path,
) -> None:
    connection = open_workspace_connection(tmp_path)
    try:
        orchestrator, spec_store, instance_store = assemble_sqlite_orchestrator(
            connection,
            node_registry=_registry(lambda ctx: None),
            state_classes={"scanner": ScannerState},
        )
        spec_id = spec_store.save(_spec())
        graph_instance_id = await orchestrator.create_and_run(spec_id)
        scanner = RecoveryScanner(
            orchestrator,
            instance_store,
            interval_seconds=0.01,
            max_recovery_attempts=3,
        )

        scanner_task = asyncio.create_task(scanner.run())
        await asyncio.sleep(0)
        instance_store.update_status(graph_instance_id, GraphInstanceStatus.CRASHED)
        for _ in range(100):
            metadata = instance_store.load(graph_instance_id)
            if metadata is not None and metadata.status == GraphInstanceStatus.COMPLETED:
                break
            await asyncio.sleep(0.001)
        scanner_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await scanner_task

        metadata = instance_store.load(graph_instance_id)
        assert metadata is not None
        assert metadata.status == GraphInstanceStatus.COMPLETED
        assert connection.execute("SELECT 1").fetchone() == (1,)
        assert (tmp_path / ".modex" / "state.db").is_file()
    finally:
        connection.close()


async def test_scanner_marks_instance_failed_when_retry_budget_is_exhausted() -> None:
    def fail(ctx: GraphContext[Any]) -> None:
        raise RuntimeError("recovery failed")

    connection = sqlite3.connect(":memory:")
    try:
        orchestrator, spec_store, instance_store = assemble_sqlite_orchestrator(
            connection,
            node_registry=_registry(fail),
            state_classes={"scanner": ScannerState},
        )
        spec_id = spec_store.save(_spec())
        graph_instance_id = 38001
        instance_store.save(
            GraphMetadata(
                graph_instance_id=graph_instance_id,
                spec_id=spec_id,
                parent_instance_id=None,
                parent_node=None,
                status=GraphInstanceStatus.CRASHED,
            )
        )
        scanner = RecoveryScanner(
            orchestrator,
            instance_store,
            interval_seconds=60,
            max_recovery_attempts=1,
        )

        recovered = await scanner.recover_once()

        assert recovered == []
        metadata = instance_store.load(graph_instance_id)
        assert metadata is not None
        assert metadata.status == GraphInstanceStatus.FAILED
    finally:
        connection.close()
