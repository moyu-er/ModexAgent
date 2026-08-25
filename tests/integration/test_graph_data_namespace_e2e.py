# ruff: noqa: ANN401, S101

"""W6 E2E -- custom DATA_NAMESPACE plugin type reaches graph state_schema compilation.

Proves the W6 wiring target: a plugin-registered data-namespace type (a
Pydantic model registered through the real ``PluginRegistrationContext``)
resolves when the bot's graph compile path compiles a ``state_schema`` spec
that references it by name.

RED on the pre-W6 baseline: the BIZ graph wiring
(``bot/workspace/wiring/resources.py`` step 8) constructs
``GraphOrchestrator`` without a state-schema compiler, so the orchestrator's
self-built two-arg ``GraphSpecCompiler`` never sees registry-registered
types — ``create_instance`` on a ``state_schema`` spec raises
``ValueError: GraphSpec.state_schema is set but no state_schema_compiler
was injected``. Turns green when the BIZ wiring passes the registry-built
compiler (``build_state_schema_compiler``).

Boot follows the existing graph integration suite pattern
(``tests/integration/graph_orchestration/``): sqlite stores +
``SqliteCoordinatorFactory``, mirroring the BIZ construction kwargs; spec
YAML loads through ``GraphSpecLoader`` the same way ``resources.py`` boots
workspace graphs.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict

from modex_agent.graph.spec_loader import GraphSpecLoader
from modex_agent.orchestration import GraphOrchestrator, SqliteCoordinatorFactory
from modex_agent.plugins.abc import ComponentSlot, SimpleFactory
from modex_agent.plugins.assembly.graph_schema import build_state_schema_compiler
from modex_agent.plugins.defaults import DefaultPlugin
from modex_agent.plugins.loader import (
    ComponentRegistryLoader,
    Plugin,
    PluginDiscoveryConfig,
    PluginRegistrationContext,
)
from modex_agent.plugins.registry import ComponentRegistry
from modex_graph import (
    DefaultGraphState,
    FieldSpec,
    FunctionNodeFactory,
    GraphContext,
    GraphState,
    NodeRegistry,
    SqliteGraphInstanceStore,
    SqliteGraphIORecordStore,
    SqliteGraphSpecStore,
)

pytestmark = pytest.mark.integration

_PROBE_TYPE_NAME = "probe_graph_state"


class _EmptyConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ProbeGraphState(BaseModel):
    """The data-namespace payload -- the type the graph state_schema references."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    probe_marker: str = "w6-default"


class _ProbeNamespacePlugin(Plugin):
    """Registers the probe state type in the data-namespace slot on demand."""

    config_model = _EmptyConfig

    def register(self, ctx: PluginRegistrationContext) -> None:
        ctx.register_namespace(
            _PROBE_TYPE_NAME, SimpleFactory(ProbeGraphState, _EmptyConfig)
        )


_PROBE_GRAPH_YAML = """\
name: probe_state_schema_graph
version: "1.0"
state_schema:
  note:
    type: probe_graph_state
    initial:
      probe_marker: w6-probe-initial
nodes:
  - name: work
    node_type: function
    config:
      function: work
edges:
  - source: __start__
    target: work
  - source: work
    target: __end__
"""


def _noop(ctx: GraphContext[Any]) -> None:
    return None


def _build_graph_orchestrator(
    connection: sqlite3.Connection,
    state_schema_compiler: Callable[[dict[str, FieldSpec]], type[GraphState]] | None = None,
) -> tuple[GraphOrchestrator, SqliteGraphSpecStore]:
    """Construct the graph orchestrator the way the BIZ wiring does."""
    node_registry = NodeRegistry()
    node_registry.register("function", FunctionNodeFactory({"work": _noop}))
    state_classes = {"default": DefaultGraphState}
    spec_store = SqliteGraphSpecStore(connection)
    orchestrator = GraphOrchestrator(
        node_registry=node_registry,
        state_classes=state_classes,
        spec_store=spec_store,
        instance_store=SqliteGraphInstanceStore(connection),
        coordinator_factory=SqliteCoordinatorFactory(connection=connection),
        io_store=SqliteGraphIORecordStore(connection),
        state_schema_compiler=state_schema_compiler,
    )
    return orchestrator, spec_store


async def test_custom_data_namespace_type_reaches_state_schema_compilation(
    tmp_path: Path,
) -> None:
    registry = ComponentRegistry()
    await ComponentRegistryLoader.load(
        registry,
        PluginDiscoveryConfig(
            bundled_factories=(DefaultPlugin(), _ProbeNamespacePlugin()),
            project_plugin_paths=(),
        ),
    )
    # The bundled defaults register NOTHING in the data-namespace slot; the
    # probe plugin supplies the only registration (on-demand semantics).
    assert registry.names(ComponentSlot.DATA_NAMESPACE) == (_PROBE_TYPE_NAME,)

    graphs_dir = tmp_path / "graphs"
    graphs_dir.mkdir()
    (graphs_dir / "probe_state_schema.yml").write_text(_PROBE_GRAPH_YAML, encoding="utf-8")

    connection = sqlite3.connect(tmp_path / "state.db")
    state_schema_compiler = build_state_schema_compiler(registry)
    orchestrator, spec_store = _build_graph_orchestrator(connection, state_schema_compiler)
    try:
        loaded = GraphSpecLoader(
            spec_store, compiler=orchestrator._compiler
        ).load_from_dir(graphs_dir)
        assert [spec.name for spec in loaded] == ["probe_state_schema_graph"]
        spec = loaded[0]
        assert spec.state_schema is not None

        # The graph compile path: create_instance loads the spec and runs it
        # through the orchestrator's GraphSpecCompiler.
        record = next(r for r in spec_store.list_records() if r.name == spec.name)
        graph_instance_id = await orchestrator.create_instance(record.spec_id)
        assert graph_instance_id > 0

        # The compiled state schema CONTAINS the probe type: the field's
        # resolved annotation IS the plugin-registered model, and validating
        # state input produces a probe instance (resolved through the same
        # compiler the orchestrator's GraphSpecCompiler holds).
        state_cls = state_schema_compiler(spec.state_schema)
        note_field = state_cls.model_fields["note"]
        assert note_field.annotation is ProbeGraphState
        validated = state_cls.model_validate({"note": {"probe_marker": "w6-checked"}})
        # Dynamic create_model product -- getattr is the dynamic-model seam.
        note_value = getattr(validated, "note")
        assert isinstance(note_value, ProbeGraphState)
        assert note_value.probe_marker == "w6-checked"

        # The RUN path also constructs state from the compiled schema
        # (``_create_state`` resolves through the compiler, not the
        # state-class mapping). Regression anchor: this previously raised
        # ``KeyError: None`` after the run had already been accepted.
        run_instance_id = await orchestrator.create_and_run(record.spec_id)
        assert run_instance_id > 0
    finally:
        await orchestrator.cleanup()
        connection.close()
