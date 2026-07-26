"""Architecture guard: `route_fn` conditional-edge mechanism is gone from modex_graph.

Per the two-layer routing model cleanup (preparing for ParallelScheduler):
`add_conditional_edges`, `ConditionalEdge`, `conditional_for`, and the
`conditional_edges` field on `CompiledGraph` were removed. Routing is now a
two-layer model:

1. `Command.goto` (str | list[Task] | None) — runtime dynamic routing.
2. `transition` + static edges (`add_edge(src, dst, reason=)`) + default edge.

This AST guard prevents regression. If any of the removed symbols reappear in
`src/modex_graph/`, this test fails loudly.

Prior art: test_dead_code_gone.py (same source-scan pattern).
"""
from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[2] / "src" / "modex_graph"

# Symbols removed in the two-layer routing cleanup.
# - `add_conditional_edges` — Graph builder method for route_fn edges.
# - `ConditionalEdge` — dataclass holding route_fn + destinations.
# - `conditional_for` — CompiledGraph lookup method.
# - `conditional_edges` — CompiledGraph field + Graph builder property.
# - `route_fn` — the callable attribute on ConditionalEdge.
# - `_conditional_edges` — Graph builder private list.
REMOVED_SYMBOLS = (
    "add_conditional_edges",
    "ConditionalEdge",
    "conditional_for",
    "conditional_edges",
    "route_fn",
    "_conditional_edges",
)

# Match whole identifiers, not substrings. `route_fn` is included as a word
# boundary match — it only appears in the removed ConditionalEdge context
# inside modex_graph (no other use of the name exists in the package).
_PATTERN = re.compile(r"\b(" + "|".join(re.escape(s) for s in REMOVED_SYMBOLS) + r")\b")


def test_removed_symbols_absent_from_modex_graph() -> None:
    """None of the removed route_fn symbols may appear in src/modex_graph/."""
    offenders: list[str] = []
    for path in sorted(ROOT.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        hits = set(_PATTERN.findall(text))
        if hits:
            offenders.append(
                f"{path.relative_to(ROOT.parents[1])}: {sorted(hits)}"
            )
    assert not offenders, (
        "Removed route_fn symbols re-introduced in src/modex_graph/:\n  "
        + "\n  ".join(offenders)
    )


def test_guard_symbol_list_is_nonempty() -> None:
    """Sanity: the guard must actually watch something."""
    assert REMOVED_SYMBOLS


def test_graph_has_no_add_conditional_edges_method() -> None:
    """`Graph` class must not have an `add_conditional_edges` attribute.

    If someone calls `g.add_conditional_edges(...)`, they must get a clear
    `AttributeError` (the method does not exist). This complements the
    source-scan test above with a runtime check.
    """
    from modex_graph import Graph

    assert not hasattr(Graph, "add_conditional_edges"), (
        "Graph.add_conditional_edges must be removed — the two-layer routing "
        "model uses transition + static edges only."
    )


def test_compiled_graph_has_no_conditional_for_method() -> None:
    """`CompiledGraph` must not have a `conditional_for` attribute."""
    from modex_graph import CompiledGraph

    assert not hasattr(CompiledGraph, "conditional_for"), (
        "CompiledGraph.conditional_for must be removed — conditional edges "
        "are gone from the two-layer routing model."
    )


def test_compiled_graph_has_no_conditional_edges_field() -> None:
    """`CompiledGraph` dataclass fields must not include `conditional_edges`."""
    import dataclasses

    from modex_graph import CompiledGraph

    field_names = {f.name for f in dataclasses.fields(CompiledGraph)}
    assert "conditional_edges" not in field_names, (
        "CompiledGraph.conditional_edges field must be removed."
    )


def test_conditional_edge_not_exported() -> None:
    """`ConditionalEdge` must not be importable from the modex_graph package."""
    import modex_graph

    assert not hasattr(modex_graph, "ConditionalEdge"), (
        "ConditionalEdge must not be exported from modex_graph."
    )
