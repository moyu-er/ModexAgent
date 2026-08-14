# 0002: Graph Node Agent Context Injection

## Objective / constraints

**Objective**: When an agent runs inside a graph node (`BotAgentNode.execute`), it must understand the full graph context — topology, its position, input provenance, output expectations, and END's aggregation semantics — to produce well-targeted deliver content and avoid confusion (waiting for non-activated inputs, misformatting for END, confusing Origin Request with node delivers).

**Constraints**:
- `BotAgentNode.execute` already injects context via two convergent paths: (1) `SYSTEM_REMINDER` history message (`agent_node.py:136-147`) for dynamic per-invocation content, (2) `GraphWorkflowProvider` (`providers.py:800-854`) in the system prompt for stable graph metadata. A third path is forbidden by convergence rule (AGENTS.md rule 1).
- `GraphDeliverTool.description` (`graph_deliver.py:162-181`) is the third existing surface — tool-layer context for downstream targets.
- `self._graph_ref` (`CompiledGraph`) is available in `BotAgentNode.execute` — set by scheduler at `node.py:213` before `execute()`. This is the sole topology data source; `GraphContext` does not hold a graph reference.
- `GraphWorkflowProvider` reads `TurnCustomKey.GRAPH_NODE_DESCRIPTION` from `ctx.runtime.state.custom` at LLM-call time (lazy evaluation via `pipeline.get_or_refresh()`). The timing is safe: `BotAgentNode.execute` sets custom keys before `runner.execute_turn()`, and `current_agent_context.set()` happens inside `execute_turn` before the pipeline resolves.
- Node name uniqueness is enforced by `GraphSpec._validate_structure` (`spec.py:110-126`) in the declarative path (bot_project uses YAML→GraphSpec). The imperative `Graph.add_node` silently overwrites on duplicate name — but production paths use the declarative path, so name uniqueness is a safe assumption for injection design.
- Deliver routing is 100% name-based (`deliver(next_node=name)`, `ctx.dispatch(target=name)`, `edges_from(source=name)`). `node_id` is only a persistence storage key.
- END node (`modex_graph/nodes/end_node.py:32-43`) aggregates all upstream delivers into `ctx.state.result = [payload.content for payload in integrated_input.payloads]` — a list of `GraphPayload` blocks in delivery order. END forces `ON_ALL_PREDS`.

## Settled decisions

### 1. Topology injection to system prompt (GraphWorkflowProvider)

Inject a `### Topology` subsection under `## Graph Node Context` into the system prompt carrying the full DAG: graph name, node names without type labels (only `__start__` and `__end__` carry special semantic labels), all edges, current node highlighted with `← YOU ARE HERE`, END with aggregation-semantic label, and Origin Request linkage text.

**No node descriptions (role_description) in topology** — only names. Descriptions of unrelated branch nodes are noise. An agent only interacts with its direct upstream (with input) and downstream (via deliver).

**Origin Request linkage**: topology declares `__start__ (entry — receives Origin Request)` and includes a trailing paragraph: "Origin Request: the user's input that triggered this graph run. It enters through __start__. You will see it in your input as [Origin Request]." This establishes the mental link between the topology's entry point and the SYSTEM_REMINDER message.

**Data source**: `self._graph_ref` (nodes, edges, entry_node, edges_from). Serialized in `BotAgentNode.execute` into a new `TurnCustomKey.GRAPH_TOPOLOGY_CONTEXT` custom key (string). Read by `GraphWorkflowProvider._fetch_content`.

### 2. Upstream descriptions only at interaction points

Node descriptions (`role_description` via `resolve_description()`) are provided **only** where the agent interacts with that node:
- **Upstream with input**: in `_format_integrated_input`, alongside each `[Input from graph node 'X']` block, annotated as `(upstream node, role: <desc>)`. Only for nodes that actually delivered input.
- **Downstream**: in `GraphDeliverTool.description`, already exists for each target. Enhanced for END with aggregation semantics (decision 5).
- **Not in topology**: unrelated nodes have name only.

**Non-AgentNode upstream** (FunctionNode, DelayNode, etc.): `resolve_description()` returns `"[not found]"`. In this case, no role description is shown (just `(upstream node)`), no forced description.

**`__start__` upstream**: if `__start__` is a direct upstream (i.e., the node is the entry node), its "input" is the Origin Request. No `resolve_description()` call — annotated as "Origin Request (from __start__, user's original input)".

### 3. Missing upstream explanation

When the topology shows N upstream nodes but only M < N delivered input, the agent may wonder if the missing upstream will deliver later. This causes delayed delivers, empty turns, or confused output.

**Solution**: `_format_integrated_input` appends an `[Upstream Status]` block after all input blocks:

```
[Upstream Status]
- researcher: delivered (1 payload)
- analyzer: delivered (1 payload)
- validator: no input — path not activated in this run, no further input expected. Proceed with received input.
```

Three-part wording for missing upstream: (1) "no input received", (2) "path not activated in this run", (3) "no further input expected. Proceed with received input." — the third part is critical; it explicitly releases the agent from waiting.

**Computation**: `all_upstream = [e.source for e in graph.edges if e.target == self.name and e.source != GraphNode.START]`; `delivered = {resolve_source_name(p.source_node) for p in payloads}`; `missing = [s for s in all_upstream if s not in delivered]`.

**`__start__` excluded** — its "input" is the Origin Request, handled separately. It never appears in Upstream Status as "missing".

**Trigger mode awareness**: first version uses `ON_ALL_PREDS` wording (path not activated). `ON_RECEIVE` scenarios (future invocation may deliver more) are a deferred optimization with different wording ("not yet delivered — may arrive in a future invocation").

### 4. Deliver tool enhancement: current node identity

`GraphDeliverTool.description` header gains "You are node: {current}" — `self._store._current` already holds the current node name. This gives the agent its own identity at the tool layer, complementing the topology's `← YOU ARE HERE` marker.

### 5. END description enhancement (aggregation semantics)

END's description in `GraphDeliverTool` is replaced from the current one-liner ("Deliver here ONLY when your task is fully complete...") with a detailed description covering:

- **Aggregation behavior**: "Collects all upstream deliveries in delivery order and concatenates them into the graph's final reply (a list of content blocks)."
- **Input→output transformation**: "Your deliver content becomes one block in the final reply list. All upstream nodes that deliver to __end__ contribute one block each, ordered by delivery time."
- **Input expectations**: "A self-contained, user-facing segment of the final reply. Write it as polished content — the user sees this directly. Do not include internal reasoning or tool call traces."
- **Multi-upstream guidance**: "If multiple nodes deliver to __end__: each contribution is a separate block. Coordinate your scope via the topology. Delivery order (not topology order) determines block order."
- **Preserved guidance**: "Deliver here ONLY when your task is fully complete. Do not deliver to __end__ and another target in the same turn."

**Topology-level END label** (all nodes see): `__end__ (terminal — collects all upstream deliveries in order, concatenates into the graph's final reply list)`.

### 6. Deliver content template: Producer + Relay patterns

The `GraphWorkflowProvider` deliver content guidelines (currently Task/Result/Status only) gain a second pattern:

**Pattern 1 — Producer** (existing, agent produces new work):
- Task: What you were asked to do.
- Result: What you produced or found.
- Status: Done / partial / blocked.

**Pattern 2 — Relay** (new, agent selectively passes upstream content downstream):
- Source: Which upstream node(s) this content is derived from.
- Selection: What you included and why it's relevant to the downstream node.
- Summary: The filtered/summarized content, written for the downstream node's needs.
- Omitted: What you excluded (briefly — so downstream knows what's not here).

**Key constraint**: Relay is NOT verbatim forwarding. The agent must filter, transform, and summarize based on its understanding of what the downstream node needs. Verbatim forwarding defeats the node's filtering role — the downstream could receive directly if no transformation were needed.

**Self-selection**: the agent chooses the pattern based on its role. This is guidance, not enforcement — the deliver tool accepts any string content.

### 7. ADR-0038 written

The decision qualifies for an ADR (hard to reverse: injection format becomes agent contract; surprising without rationale: why topology in system prompt vs. deliver tool; genuine tradeoff: 4 alternatives considered and rejected). See `docs/adr/0038-graph-node-agent-context-injection.md`.

### 8. Agent node input idempotency

Agent nodes have persistent session memory (ReAct `MessageStore`) that the engine does not know about. Upstream input injected as `SYSTEM_REMINDER` survives across invocations. On crash recovery, the engine's default `_integrate_upstream` re-collects all consumable delivers (including `CONSUMED_PENDING`), which would duplicate the `SYSTEM_REMINDER`.

**Fix**: `AgentNode` overrides `_integrate_upstream` to always filter `CONSUMED_PENDING` (not just on resume). `BotAgentNode.execute` detects re-execution (session has existing messages) and skips `[Origin Request]` duplication.

This is a justified override of the engine's `Node._integrate_upstream` — the engine is designed for stateless nodes; agent nodes have a second persistence layer. The override changes one line (removing the `if is_resume:` guard) and preserves the same method structure.

## Assumptions

1. **`self._graph_ref` is always set in production** — both `LinearScheduler` and `ParallelScheduler` pass `graph=self.graph` to `node.run()`. The `None` case is test-only (direct `node.run(ctx)` without graph). Injection logic guards `None` and skips topology/context injection gracefully.

2. **Node name uniqueness in production** — declarative path (`GraphSpec`) enforces it at compile time. Imperative path silently overwrites, but production ReAct graphs use fixed constant names and bot_project uses YAML specs. Name duplication is a graph-construction bug, not an injection-design responsibility.

3. **`GraphWorkflowProvider` lazy evaluation timing is safe** — verified: `BotAgentNode.execute` sets `graph_context` (line 155) and `GRAPH_NODE_DESCRIPTION` (line 194) before `runner.execute_turn()` (line 197). `current_agent_context.set()` happens inside `execute_turn` → `agent.run()` (line 242). Pipeline resolves at LLM call time (after `current_agent_context.set`). New `GRAPH_TOPOLOGY_CONTEXT` key follows the same timing pattern.

4. **`ON_ALL_PREDS` is the dominant trigger mode** — bot_project's `review_cycle.yml` uses `parallel` + `on_all_preds`. The missing-upstream wording ("path not activated") is correct for this mode. `ON_RECEIVE` scenarios are deferred.

5. **AgentNode subclasses have `resolve_description()`** — returns `role_description` for agent nodes, `"[not found]"` for non-agent nodes. The injection handles both cases (show role if available, show type label if not).

6. **Relay pattern is guidance, not enforcement** — the deliver tool accepts any string. Agents self-select Producer vs. Relay based on their role and the deliver guidelines. No schema validation on content structure.

## Exceptions

- **`self._graph_ref is None`**: topology injection, upstream description lookup, and missing-upstream computation are all skipped. The agent runs with the existing minimal context (GraphWorkflowProvider's generic guidance + Origin Request + raw upstream content without descriptions). This is the test/no-scheduler path — not a production scenario.

- **Non-AgentNode upstream**: `resolve_description()` returns `"[not found]"`. The injection shows no role description (just `(upstream node)`). The agent knows the upstream exists, but not its type or role description.

- **`__start__` as upstream**: the entry node's "input" is the Origin Request, not a node deliver. It is excluded from the `[Upstream Status]` block and handled as `[Origin Request]` in the SYSTEM_REMINDER. The topology section links them via the `__start__ (entry — receives Origin Request)` label.

- **`ON_RECEIVE` trigger mode**: the missing-upstream wording ("path not activated, no further input expected") is incorrect for `ON_RECEIVE` (future invocations may deliver more). First version uses `ON_ALL_PREDS` wording only. `ON_RECEIVE` support is a deferred optimization.

- **Large graphs (10+ nodes)**: topology injection adds token cost proportional to node/edge count. No truncation strategy in v1. If token budget becomes a problem, future optimization: show only reachable subgraph from current node (upstream chain + downstream chain), not the full DAG.

## Recommendation

Implement in the following order (each step is independently testable):

1. **`TurnCustomKey.GRAPH_TOPOLOGY_CONTEXT`** — new enum value in `runtime/enums.py`.
2. **`BotAgentNode.execute` topology serialization** — `_build_topology_section()` method rendering `self._graph_ref` into a markdown string (nodes + edges + current highlight + END label + Origin Request linkage). Guard `self._graph_ref is None`. Write to `agent_context.runtime.state.custom[GRAPH_TOPOLOGY_CONTEXT]`.
3. **`GraphWorkflowProvider._fetch_content` extension** — restructure into single `## Graph Node Context` H2 with `### Workflow Guidance`, `### Topology`, `### Your Role` H3 subsections. Read `GRAPH_TOPOLOGY_CONTEXT` from custom state for the `### Topology` subsection. Update deliver content guidelines with Producer + Relay patterns.
4. **`_format_integrated_input` enhancement** — add upstream role description per source, add `[Upstream Status]` block with missing-upstream explanation.
5. **`GraphDeliverTool` enhancement** — add "You are node: X" header, replace END description with aggregation-semantics version.
6. **Tests** — topology rendering, missing-upstream computation, END description, Relay pattern guidance, `None` graph_ref guard, timing safety.

## Flip conditions

1. **If topology injection token cost is too high for large graphs** — switch from full-DAG to reachable-subgraph (upstream chain + downstream chain from current node). The data source (`self._graph_ref`) and injection path (`GraphWorkflowProvider`) stay the same; only the serialization scope changes.

2. **If `ON_RECEIVE` trigger mode becomes common** — the missing-upstream wording must distinguish `ON_ALL_PREDS` ("path not activated, no further input expected") from `ON_RECEIVE` ("not yet delivered, may arrive in future invocation"). The trigger mode is readable from `self.trigger` or `graph.default_trigger`; the computation adds a per-upstream trigger check.

3. **If Relay pattern is misused (agents forward verbatim)** — add a deliver tool warning when content length exceeds upstream input length by a threshold (heuristic, not enforcement). Or add a `deliver_mode` parameter to the deliver tool (`produce` / `relay`) that surfaces the pattern choice to the tool layer.

4. **If END aggregation semantics change** (e.g., END merges instead of concatenates, or applies a template) — the END description in `GraphDeliverTool` and the topology-level END label must be updated to match the new `EndNode.execute` behavior. The description is a claim about behavior; it must track the code.
