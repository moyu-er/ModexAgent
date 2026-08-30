"""A3 derived-entries equivalence capture — the T15 pre-change snapshot.

Run on the PRE-MIGRATION HEAD (while ``_derived_entries`` still lives in
``modex_agent/scope/compiler.py``):

    python -m tests.unit.scope.goldens.subagents.capture_derived_entries

Calls the OLD compiler-side tree-derivation function DIRECTLY on two trees
and writes ``derived_entries.json`` next to this script (utf-8, JSON
round-trip — the T6/T9 golden discipline):

- ``fixture`` — a synthetic 3-level nested tree (``root → mid → leaf`` in
  one pool) plus a second pool whose root is peer-linked to the first:
  every tree position that can produce a derived entry, in one capture.
- ``shipped`` — the shipped ``bot.yml`` tree, per agent (including the
  external peer-linked root — the C0 structural-exclusion divergence the
  migration documents).

``tests/unit/scope/test_subagents_capability.py`` re-derives the same
agents' entries through ``SubagentsCapability.contribute`` (the capability
channel's ``derived_tools``) and asserts table equality per agent: tool
names, origins, targets — SPEC §14.4 tree-derivation equivalence / A3.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from modex_agent.scope.compiler import _derived_entries
from modex_agent.scope.loader import load_scope_declaration
from modex_agent.scope.spec import AgentSpec, PoolSpec
from modex_agent.scope.validator import _pools_of

_DIR = Path(__file__).resolve().parent
_DECLARATION_PATH = (
    Path(__file__).resolve().parents[5]
    / "examples"
    / "bot_project"
    / "config"
    / "scopes"
    / "bot.yml"
)


def _children_of(pool: PoolSpec) -> dict[str, list[str]]:
    children: dict[str, list[str]] = {}
    for agent in pool.agents:
        if agent.parent is not None:
            children.setdefault(agent.parent, []).append(agent.name)
    return children


def _capture_pool(pool: PoolSpec) -> dict[str, list[dict[str, Any]]]:
    children = _children_of(pool)
    return {
        agent.name: [
            {"tool": entry.tool, "origin": entry.origin.value, "targets": list(entry.targets)}
            for entry in _derived_entries(agent, pool=pool, children=children)
        ]
        for agent in pool.agents
    }


def _capture() -> dict[str, Any]:
    # The synthetic 3-level tree: nested pool (root → mid → leaf) + a
    # peer-linked root pool. Built as two PoolSpecs directly — the
    # derivation function takes the pool, not the workspace layer.
    nested = PoolSpec(
        name="nested",
        agents=[
            AgentSpec(name="root", description="top"),
            AgentSpec(name="mid", parent="root", description="middle"),
            AgentSpec(name="leaf", parent="mid", description="bottom"),
        ],
    )
    peered = PoolSpec(name="peered", peers=["nested"], agents=[AgentSpec(name="root2")])
    fixture = {"nested": _capture_pool(nested), "peered": _capture_pool(peered)}

    shipped_spec = load_scope_declaration(_DECLARATION_PATH)
    shipped = {pool.name: _capture_pool(pool) for pool in _pools_of(shipped_spec)}
    return {"fixture": fixture, "shipped": shipped}


def main() -> None:
    payload = json.dumps(_capture(), indent=2, ensure_ascii=False) + "\n"
    (_DIR / "derived_entries.json").write_text(payload, encoding="utf-8", newline="\n")


if __name__ == "__main__":
    main()
