"""Graph-spec reference extraction for the scope boot (ticket 07, V10).

Extracts the (pool, agent) references the loaded graph specs declare —
the phase-1 V10 input face. Kept separate from the boot orchestrator
(``declaration.py``) because it reads RESOURCE config (graph YAML files)
with its own resolution rules, mirroring ``BotAgentNodeConfig``:
``node_type == "agent"`` nodes carry a required ``config.agent`` with
``config.pool`` defaulting to ``"default"``.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import yaml

from modex_agent.scope.validator import GraphAgentReference


def extract_graph_agent_refs(graphs_dirs: Sequence[Path]) -> list[GraphAgentReference]:
    """Collect agent-node references from the first EXISTING graphs dir.

    The preference order (workspace-local first, global template second)
    mirrors the resources.py copytree resolution: a workspace that has
    materialized its own ``config/graphs`` no longer reads the global
    template. A malformed agent node (missing/non-string ``config.agent``)
    fails loudly here rather than at graph-run time — the graph loader's
    free-form ``NodeSpec.config`` would let it through to
    ``BotAgentNodeFactory.create``.
    """
    refs: list[GraphAgentReference] = []
    for graphs_dir in graphs_dirs:
        if not graphs_dir.exists():
            continue
        for spec_path in sorted(graphs_dir.glob("*.yml")):
            refs.extend(_refs_of_spec(spec_path))
        break
    return refs


def _refs_of_spec(spec_path: Path) -> list[GraphAgentReference]:
    raw: object = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        return []
    graph_name = str(raw.get("name") or spec_path.stem)
    refs: list[GraphAgentReference] = []
    nodes = raw.get("nodes") or []
    for node in nodes:
        if not isinstance(node, dict) or node.get("node_type") != "agent":
            continue
        config = node.get("config") or {}
        node_name = str(node.get("name") or "?")
        if not isinstance(config, dict) or not isinstance(config.get("agent"), str):
            raise ValueError(
                f"graph {graph_name!r} node {node_name!r}: agent nodes require "
                "a string config.agent (mirror of BotAgentNodeConfig)"
            )
        refs.append(
            GraphAgentReference(
                graph=graph_name,
                node=node_name,
                pool=str(config.get("pool") or "default"),
                agent=str(config["agent"]),
            )
        )
    return refs
