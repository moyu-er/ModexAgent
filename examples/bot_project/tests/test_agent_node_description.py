from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
import yaml
from bot.graph.agent_node import BotAgentNode
from bot.graph.agent_node_factory import BotAgentNodeConfig, BotAgentNodeFactory
from pydantic import ValidationError

from modex_agent.agents.agent_node import AgentNode
from modex_agent.multi_agent.address import AgentAddress
from modex_agent.multi_agent.descriptor import AgentDescriptor
from modex_agent.multi_agent.template import AgentTemplate
from modex_agent.scope.spec import AgentSpec
from modex_graph.constants import GraphNode
from modex_graph.context import GraphContext
from modex_graph.graph import Graph
from modex_graph.spec import NodeSpec

_BOT_PROJECT = Path(__file__).resolve().parents[1]


def _resolver(role_description: str, *, pool_name: str = "p") -> MagicMock:
    instance = SimpleNamespace(
        descriptor=AgentDescriptor(
            address=AgentAddress(name="a"),
            role_description=role_description,
        )
    )
    pool = SimpleNamespace(pool=SimpleNamespace(get=lambda _name: instance))
    resolver = MagicMock()
    resolver.resolve_workspace.return_value = SimpleNamespace(pools={pool_name: pool})
    return resolver


def _lazy_resolver(
    template: AgentTemplate | None, *, pool_name: str = "p"
) -> MagicMock:
    """Pool stand-in with NO live instance — the lazy-agent cold-start face.

    ``get_template`` mirrors :meth:`AgentPool.get_template`, the same
    existence/materialization source the InboxPoller reads."""
    agent_pool = SimpleNamespace(
        get=lambda _name: None,
        get_template=lambda _name: template,
    )
    resolver = MagicMock()
    resolver.resolve_workspace.return_value = SimpleNamespace(
        pools={pool_name: SimpleNamespace(pool=agent_pool)}
    )
    return resolver


def test_graph_node_description_overrides_agent_description() -> None:
    node = BotAgentNode(
        "a",
        "p",
        _resolver("Agent desc"),
        node_description="Graph role X",
    )

    assert node.resolve_description() == "Graph role X"


def test_agent_description_is_used_without_graph_node_description() -> None:
    node = BotAgentNode(
        "a",
        "p",
        _resolver("Agent desc"),
        node_description=None,
    )

    assert node.resolve_description() == "Agent desc"


def test_empty_string_graph_node_description_falls_back_to_agent_description() -> None:
    node = BotAgentNode(
        "a",
        "p",
        _resolver("Agent desc"),
        node_description="",
    )

    assert node.resolve_description() == "Agent desc"


def test_description_sentinel_is_used_when_descriptions_are_empty() -> None:
    node = BotAgentNode(
        "a",
        "p",
        _resolver(""),
        node_description=None,
    )

    assert node.resolve_description() == AgentNode.DESCRIPTION_NOT_FOUND


def test_lazy_agent_description_resolves_from_compiled_declaration() -> None:
    """Ticket 08 AC (b): fresh boot, no instance — the description comes from
    the template registry (the compiled declaration's runtime carrier seeded
    at boot), so resolve_description does not raise."""
    template = AgentTemplate(
        spec=AgentSpec(name="a", description="Declared leaf role")
    )
    node = BotAgentNode("a", "p", _lazy_resolver(template), node_description=None)

    assert node.resolve_description() == "Declared leaf role"


def test_lazy_agent_template_without_description_uses_sentinel() -> None:
    template = AgentTemplate(spec=AgentSpec(name="a", description=""))
    node = BotAgentNode("a", "p", _lazy_resolver(template))

    assert node.resolve_description() == AgentNode.DESCRIPTION_NOT_FOUND


def test_unknown_agent_description_uses_sentinel() -> None:
    """No live instance AND no template: the framework sentinel, not an
    exception — V10 owns typo'd references at startup."""
    node = BotAgentNode("a", "p", _lazy_resolver(None))

    assert node.resolve_description() == AgentNode.DESCRIPTION_NOT_FOUND


def test_factory_passes_graph_node_description_to_node() -> None:
    resolver = _resolver("Agent desc", pool_name="default")
    config = BotAgentNodeConfig.model_validate({"agent": "a", "description": "D"})
    spec = NodeSpec(name="n", node_type="agent", config=config.model_dump())

    node = BotAgentNodeFactory(resolver).create(spec)

    assert isinstance(node, BotAgentNode)
    assert node.resolve_description() == "D"


def test_config_rejects_misspelled_description() -> None:
    with pytest.raises(ValidationError):
        BotAgentNodeConfig.model_validate({"agent": "a", "descriptionn": "typo"})


def test_graph_artifacts_normalize_description_sentinel() -> None:
    node = BotAgentNode("a", "default", _resolver("", pool_name="default"))
    graph: Graph[Any] = Graph("description-normalization")
    graph.add_node("n", node)
    graph.add_edge(GraphNode.START, "n")
    graph.add_edge("n", GraphNode.END)
    node._graph_ref = graph.compile()
    ctx = MagicMock(spec=GraphContext)
    ctx.graph_instance_id = None

    artifacts = node._build_graph_artifacts(ctx)

    assert artifacts.node_description == ""


def test_review_cycle_agent_node_descriptions_validate() -> None:
    raw = yaml.safe_load(
        (_BOT_PROJECT / "config" / "graphs" / "review_cycle.yml").read_text(
            encoding="utf-8"
        )
    )

    configs = {
        node["name"]: BotAgentNodeConfig.model_validate(node["config"])
        for node in raw["nodes"]
        if node["node_type"] == "agent"
    }

    assert set(configs) == {"reviewer", "coder"}
    assert all(
        isinstance(config.description, str) and config.description
        for config in configs.values()
    )
