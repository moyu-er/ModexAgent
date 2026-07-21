# 04 — graph_patterns: conditional

**What to build:** A reusable conditional-branching graph pattern module under `examples/graph_patterns/`, proving `modex_graph`'s retained API (`NodeResult(transition=...)` + static edges) composes into a realistic if/else workflow.

The module provides:

- `ConditionalNode[S](predicate: Callable[[S], str])` — a `Node[S]` subclass whose `execute` returns `NodeResult(transition=predicate(ctx.state))`. The predicate inspects state and returns a transition string; the graph topology (declared via `add_edge(source, target, reason=...)`) routes to the matching branch.
- `SwitchNode[S](cases: dict[str, Callable[[S], bool]], default: str)` — a multi-branch variant. Each case is a `(transition_key, predicate)` pair; the first matching predicate's key becomes the transition. If none match, `default` is used.
- An example graph builder `build_conditional_graph(...)` that wires a `ConditionalNode` to two branch nodes and a merge node, demonstrating the complete if/else + merge topology.

`tests/unit/examples/graph_patterns/test_conditional.py` verifies:

- `ConditionalNode` routes to the correct branch based on predicate output (predicate returns `"high"` → high branch executes; predicate returns `"low"` → low branch executes).
- `SwitchNode` routes to the first matching case; falls through to `default` when no case matches.
- A complete if/else + merge graph produces the expected final state (both branches can merge into a common downstream node via default edges).

This ticket uses only `modex_graph` API that is already unit-tested (`NodeResult.transition`, static edges, default edges) — it does not depend on tickets 01/02/03. The pattern is an example, not framework code (lives under `examples/` per ADR-0007 rule 9).

**Blocked by:** None — can start immediately. Uses existing retained API.

**Status:** ready-for-agent

- [ ] `examples/graph_patterns/__init__.py` package marker created (exports public pattern classes — will be extended by tickets 05 and 06)
- [ ] `examples/graph_patterns/conditional.py` provides `ConditionalNode[S](predicate: Callable[[S], str])` returning `NodeResult(transition=predicate(state))`
- [ ] `examples/graph_patterns/conditional.py` provides `SwitchNode[S](cases: dict[str, Callable[[S], bool]], default: str)` multi-branch variant
- [ ] `examples/graph_patterns/conditional.py` provides `build_conditional_graph(...)` example if/else + merge topology
- [ ] `tests/unit/examples/graph_patterns/__init__.py` package marker created
- [ ] `tests/unit/examples/graph_patterns/test_conditional.py` verifies `ConditionalNode` routes to correct branch based on predicate output
- [ ] `tests/unit/examples/graph_patterns/test_conditional.py` verifies `SwitchNode` routes to first matching case; falls through to `default` when no case matches
- [ ] `tests/unit/examples/graph_patterns/test_conditional.py` verifies complete if/else + merge graph produces expected final state
