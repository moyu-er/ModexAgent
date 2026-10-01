"""FW-bundled DATA_NAMESPACE registration — the ``default`` graph-state model (W6).

Slot honesty (SPEC §19 Errata-8, W6): the DATA_NAMESPACE slot had zero
bundled producers — the framework's one generic namespace (the default
graph state class) was hand-imported by deployment graph wiring (the bot
project built ``state_classes = {"default": DefaultGraphState}`` directly,
never slot-registering it). This module registers it under the bundled
``default`` name so declarative ``state_schema`` fields can reference the
type and the slot's producer story is real.

The consumer is the framework graph state-schema compiler
(:func:`modex_agent.plugins.assembly.graph_schema.resolve_namespace_model`),
wired into ``GraphOrchestrator`` by deployment graph wiring via
``build_state_schema_compiler``.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from modex_agent.plugins.loader import PluginRegistrationContext
from modex_agent.scope.components import SimpleFactory
from modex_graph import DefaultGraphState

__all__ = [
    "NamespaceConfig",
    "register_default_namespaces",
]

#: The bundled namespace name — same ``default`` convention as the bundled
#: LLM_PROVIDER and MEMORY_SYSTEM factories.
DEFAULT_NAMESPACE_NAME = "default"


class NamespaceConfig(BaseModel):
    """Empty config schema — a data namespace is a bare model-class registration."""

    model_config = ConfigDict(frozen=True, extra="forbid")


def register_default_namespaces(ctx: PluginRegistrationContext) -> None:
    """Register the bundled ``default`` graph-state data namespace.

    The DATA_NAMESPACE slot stores ``SimpleFactory`` instances whose wrapped
    value is the model CLASS (``type[BaseModel]``) — the graph state-schema
    compiler reads the class directly without calling ``create()``.
    """
    ctx.register_namespace(
        DEFAULT_NAMESPACE_NAME, SimpleFactory(DefaultGraphState, NamespaceConfig)
    )
