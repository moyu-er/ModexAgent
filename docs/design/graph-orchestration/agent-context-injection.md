# Agent Context Injection — Design Document

> **ADR**: [ADR-0038](../../adr/0038-graph-node-agent-context-injection.md)
> **Deliberation**: [0002-graph-node-agent-context-injection](../../deliberations/0002-graph-node-agent-context-injection.md)
> **Status**: Implemented

## Problem

When `BotAgentNode.execute` runs a ReAct agent turn inside a graph node, the agent receives:

1. **System prompt**: 16 providers, only `GraphWorkflowProvider` (last) injects graph context — a generic "you are a node" message + a Your Role line = `role_description` (one line).
2. **SYSTEM_REMINDER message**: `[Origin Request]:\n<content>` + `[Input from graph node 'X']:\n<content>` — content is annotated with source node name, but no role description, no topology context.
3. **Deliver tool**: lists downstream targets with role descriptions, but no current-node identity, no END aggregation semantics.

The agent does **not** know: which graph it is in, its own node name, the full topology, its position, whether missing upstream nodes will deliver later, how END aggregates multiple deliveries, or that Origin Request comes from `__start__` (not a node deliver).

## Design

Three-layer injection, all via existing convergent paths:

```
┌─────────────────────────────────────────────────────────────┐
│ System Prompt (GraphWorkflowProvider)                        │
│ ┌─────────────────────────────────────────────────────────┐ │
│ │ ## Graph Node Context                                    │ │
│ │ ### Workflow Guidance                                    │ │
│ │   Deliver Content Guidelines                             │ │
│ │   Pattern 1 — Producer (Task/Result/Status)              │ │
│ │   Pattern 2 — Relay (Source/Selection/Summary/Omitted)   │ │
│ │ ### Topology                                             │ │
│ │   Graph name, nodes, edges, YOU ARE HERE,                │ │
│ │   END aggregation label, Origin Request linkage          │ │
│ │ ### Your Role (= role_description)                       │ │
│ └─────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────┘
┌─────────────────────────────────────────────────────────────┐
│ SYSTEM_REMINDER (_format_integrated_input)                   │
│ ┌─────────────────────────────────────────────────────────┐ │
│ │ [Origin Request]: <user input>                           │ │
│ │ [Input from graph node 'X'] (upstream, role: <desc>):    │ │
│ │   <content>                                              │ │
│ │ [Upstream Status] (NEW)                                  │ │
│ │   - X: delivered                                         │ │
│ │   - Y: no input — path not activated, proceed            │ │
│ └─────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────┘
┌─────────────────────────────────────────────────────────────┐
│ Deliver Tool (GraphDeliverTool.description)                  │
│ ┌─────────────────────────────────────────────────────────┐ │
│ │ You are node: <current> (NEW)                            │ │
│ │ Available targets:                                       │ │
│ │   - <downstream>: <role_description>                     │ │
│ │   - __end__: <aggregation semantics + input expectations>│ │
│ └─────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────┘
```

## Implementation

### Step 1: New TurnCustomKey

**File**: `src/modex_agent/runtime/enums.py`

Add `GRAPH_TOPOLOGY_CONTEXT` to the `TurnCustomKey` StrEnum. Value: `"_graph_topology_context"`. Holds the serialized topology string (markdown).

### Step 2: Topology serialization in BotAgentNode

**File**: `examples/bot_project/bot/graph/agent_node.py`

New method `_build_topology_section(self) -> str`:

```python
def _build_topology_section(self) -> str:
    if self._graph_ref is None:
        return ""
    graph = self._graph_ref
    lines: list[str] = []
    lines.append(f"Graph: {graph.name}")
    lines.append(f"You are node: **{self.name}**")
    lines.append("")
    lines.append("Nodes:")

    for name, node in graph.nodes.items():
        if name == GraphNode.START:
            lines.append(f"- {name} (entry — receives Origin Request)")
        elif name == GraphNode.END:
            lines.append(
                f"- {name} (terminal — collects all upstream deliveries "
                f"in order, concatenates into the graph's final reply list)"
            )
        elif name == self.name:
            lines.append(f"- {name} ← YOU ARE HERE")
        else:
            lines.append(f"- {name}")

    lines.append("")
    lines.append("Edges:")
    for edge in graph.edges:
        lines.append(f"- {edge.source} → {edge.target}")

    # Upstream/downstream summary
    upstream = [e.source for e in graph.edges if e.target == self.name]
    downstream = [e.target for e in graph.edges if e.source == self.name]
    lines.append("")
    lines.append(f"Your upstream (nodes that deliver to you): {', '.join(upstream) or '(none)'}")
    lines.append(f"Your downstream (nodes you can deliver to): {', '.join(downstream) or '(none)'}")

    # Origin Request linkage
    lines.append("")
    lines.append(
        "Origin Request: the user's input that triggered this graph run. "
        "It enters through __start__ and is the root task every node works towards. "
        "You will see it in your input as [Origin Request]."
    )
    return "\n".join(lines)
```

In `execute()`, after setting `GRAPH_NODE_DESCRIPTION` (line 194), add:

```python
agent_context.runtime.state.custom[TurnCustomKey.GRAPH_TOPOLOGY_CONTEXT] = (
    self._build_topology_section()
)
```

### Step 3: GraphWorkflowProvider extension

**File**: `src/modex_agent/memory/prompt_pipeline/providers.py`

Restructure `GraphWorkflowProvider._fetch_content()` (line 816-854) to emit a single `## Graph Node Context` H2 with three H3 subsections: `### Workflow Guidance` (deliver guidelines with Producer + Relay patterns), `### Topology` (from `GRAPH_TOPOLOGY_CONTEXT`), and `### Your Role` (from `GRAPH_NODE_DESCRIPTION`):

```python
parts: list[str] = ["## Graph Node Context", ""]

# --- ### Workflow Guidance ---
parts.append("### Workflow Guidance")
parts.append("")
parts.append(
    "Your deliver `content` is the ONLY information downstream nodes receive "
    "about your work. Make it self-contained.\n\n"
    "**Pattern 1 — Producer** (you produce new work):\n"
    "- Task: What you were asked to do (one or two sentences).\n"
    "- Result: What you produced or found.\n"
    "- Status: Done / partial / blocked.\n\n"
    "**Pattern 2 — Relay** (you selectively pass upstream content downstream):\n"
    "Use this when your role is to filter and summarize upstream content for "
    "downstream nodes, not to produce new content. Do NOT forward upstream "
    "content verbatim — select and transform it.\n"
    "- Source: Which upstream node(s) this content is derived from.\n"
    "- Selection: What you included and why it's relevant to the downstream node.\n"
    "- Summary: The filtered/summarized content.\n"
    "- Omitted: What you excluded (briefly — so downstream knows what's missing)."
)

# --- ### Topology ---
topology = ctx.runtime.state.custom.get(TurnCustomKey.GRAPH_TOPOLOGY_CONTEXT)
if topology:
    parts.append("")
    parts.append("### Topology")
    parts.append("")
    parts.append(topology)

# --- ### Your Role ---
role = ctx.runtime.state.custom.get(TurnCustomKey.GRAPH_NODE_DESCRIPTION) or ""
if role:
    parts.append("")
    parts.append("### Your Role")
    parts.append("")
    parts.append(role)

return "\n".join(parts)
```

### Step 4: _format_integrated_input enhancement

**File**: `examples/bot_project/bot/graph/agent_node.py`

Enhance `_format_integrated_input` to add upstream role descriptions and the `[Upstream Status]` block:

```python
def _format_integrated_input(self, integrated_input: IntegratedInput) -> str:
    if not integrated_input.payloads:
        # Still need Upstream Status if there are missing upstream nodes
        status = self._build_upstream_status(delivered_sources=set())
        return status  # may be "" if no upstream nodes at all

    groups: dict[str, list[str]] = {}
    source_descs: dict[str, str] = {}
    for payload in integrated_input.payloads:
        source_name = self._resolve_source_name(payload.source_node)
        content = payload.content
        text = content.content if hasattr(content, "content") else str(content)
        groups.setdefault(source_name, []).append(text)
        # Resolve upstream role description (once per source)
        if source_name not in source_descs:
            source_descs[source_name] = self._resolve_upstream_desc(source_name)

    lines: list[str] = []
    for source_name, texts in groups.items():
        combined = "\n".join(texts)
        desc = source_descs.get(source_name, "")
        annotation = f" (upstream node, role: {desc})" if desc else " (upstream node)"
        lines.append(f"[Input from graph node '{source_name}']{annotation}:\n{combined}")

    delivered = set(groups.keys())
    status = self._build_upstream_status(delivered)
    if status:
        lines.append(status)

    return "\n\n".join(lines)

def _resolve_upstream_desc(self, source_name: str) -> str:
    if self._graph_ref is None:
        return ""
    node = self._graph_ref.nodes.get(source_name)
    if node is None:
        return ""
    from modex_agent.agents.agent_node import AgentNode
    if isinstance(node, AgentNode):
        desc = node.resolve_description()
        return "" if desc == "[not found]" else desc
    return ""  # Non-agent nodes: no description

def _build_upstream_status(self, delivered_sources: set[str]) -> str:
    if self._graph_ref is None:
        return ""
    all_upstream = [
        e.source for e in self._graph_ref.edges
        if e.target == self.name and e.source != GraphNode.START
    ]
    if not all_upstream:
        return ""
    lines = ["[Upstream Status]"]
    for source in all_upstream:
        if source in delivered_sources:
            lines.append(f"- {source}: delivered")
        else:
            lines.append(
                f"- {source}: no input — path not activated in this run, "
                f"no further input expected. Proceed with received input."
            )
    return "\n".join(lines)
```

### Step 5: GraphDeliverTool enhancement

**File**: `src/modex_agent/tools/graph_deliver.py`

In `GraphDeliverTool.description` property (line 162-181), add current node identity header:

```python
@property
def description(self) -> str:
    targets = self._store.list()
    available = (
        "\n".join(f"  - {target.name}: {target.description}" for target in targets)
        if targets else "  (none)"
    )
    current = self._store._current  # current node name
    return (
        f"You are node: {current}\n"
        "Route your work output to a downstream node.\n"
        f"Available targets:\n{available}\n\n"
        "Choose the target that matches your node's purpose. "
        "Read each target's description — it tells you what that "
        "downstream node expects. Tailor your `content` to the "
        "chosen target based on its description. "
        "You MUST specify a target."
    )
```

Replace the END description in `GraphDeliverTargetStore.list()` (line 79-92):

```python
if edge.target == GraphNode.END:
    targets.append(
        GraphDeliverTarget(
            name=GraphNode.END,
            description=(
                "Terminal node. Collects all upstream deliveries in delivery "
                "order and concatenates them into the graph's final reply "
                "(a list of content blocks).\n\n"
                "How it processes your input:\n"
                "- Your deliver content becomes one block in the final reply list.\n"
                "- All upstream nodes that deliver to __end__ contribute one block each.\n"
                "- The complete reply = [block_1, block_2, ...] in delivery order.\n\n"
                "What you should deliver:\n"
                "- A self-contained, user-facing segment of the final reply.\n"
                "- Write it as polished content — the user sees this directly.\n"
                "- Do not include internal reasoning or tool call traces.\n\n"
                "If multiple nodes deliver to __end__: each contribution is a separate "
                "block. Coordinate your scope via the topology. Delivery order (not "
                "topology order) determines block order in the final reply.\n\n"
                "Deliver here ONLY when your task is fully complete. Do not deliver "
                "to __end__ and another target in the same turn."
            ),
        )
    )
    continue
```

## Agent-Facing Injection Templates

### System prompt — Graph Node Context section (example)

```markdown
## Graph Node Context

### Workflow Guidance

Your deliver `content` is the ONLY information downstream nodes receive about your work. Make it self-contained.

**Pattern 1 — Producer** (you produce new work):
- Task: What you were asked to do (one or two sentences).
- Result: What you produced or found.
- Status: Done / partial / blocked.

**Pattern 2 — Relay** (you selectively pass upstream content downstream):
- Source: Which upstream node(s) this content is derived from.
- Selection: What you included and why it's relevant to the downstream node.
- Summary: The filtered/summarized content.
- Omitted: What you excluded (briefly — so downstream knows what's missing).

### Topology

Graph: review_cycle
You are node: **writer**

Nodes:
- __start__ (entry — receives Origin Request)
- coder
- reviewer
- writer ← YOU ARE HERE
- __end__ (terminal — collects all upstream deliveries in order, concatenates into the graph's final reply list)

Edges:
- __start__ → coder
- coder → reviewer
- reviewer → coder
- reviewer → writer
- writer → __end__

Your upstream (nodes that deliver to you): reviewer
Your downstream (nodes you can deliver to): __end__

Origin Request: the user's input that triggered this graph run. It enters through __start__ and is the root task every node works towards. You will see it in your input as [Origin Request].

### Your Role

write agent
```

### SYSTEM_REMINDER message (example)

```
[Origin Request]:
写一篇综述

[Input from graph node 'reviewer'] (upstream node, role: review agent):
The code has 3 issues: missing error handling, no tests, style inconsistencies.

[Upstream Status]
- reviewer: delivered
```

### SYSTEM_REMINDER with missing upstream (example)

```
[Origin Request]:
分析这篇论文

[Input from graph node 'researcher'] (upstream node, role: research agent):
Found 3 papers on topic X...

[Upstream Status]
- researcher: delivered
- validator: no input — path not activated in this run, no further input expected. Proceed with received input.
```

### Deliver tool description (example)

```
You are node: writer
Route your work output to a downstream node.
Available targets:
  - __end__: Terminal node. Collects all upstream deliveries in delivery order and concatenates them into the graph's final reply (a list of content blocks).

How it processes your input:
- Your deliver content becomes one block in the final reply list.
- All upstream nodes that deliver to __end__ contribute one block each.
- The complete reply = [block_1, block_2, ...] in delivery order.

What you should deliver:
- A self-contained, user-facing segment of the final reply.
- Write it as polished content — the user sees this directly.
- Do not include internal reasoning or tool call traces.

If multiple nodes deliver to __end__: each contribution is a separate block. Coordinate your scope via the topology. Delivery order (not topology order) determines block order in the final reply.

Deliver here ONLY when your task is fully complete. Do not deliver to __end__ and another target in the same turn.

Choose the target that matches your node's purpose...
```

## Testing

| Test | Scope |
|------|-------|
| `test_build_topology_section_start_end_labels` | `__start__` and `__end__` get special semantic labels; other nodes get no type label; current node gets `← YOU ARE HERE` |
| `test_build_topology_section_none_graph_ref` | Guard: returns "" when `self._graph_ref is None` |
| `test_format_integrated_input_with_upstream_desc` | Upstream role annotation per source |
| `test_format_integrated_input_missing_upstream` | `[Upstream Status]` block: delivered + missing with "proceed" wording |
| `test_format_integrated_input_no_upstream` | Empty upstream (entry node case): no Upstream Status block |
| `test_graph_workflow_provider_with_topology` | Provider reads `GRAPH_TOPOLOGY_CONTEXT`, appends to system prompt |
| `test_graph_workflow_provider_without_topology` | Provider skips when key absent (backward compatible) |
| `test_deliver_tool_end_description` | END description contains aggregation semantics |
| `test_deliver_tool_current_node_identity` | Description header includes "You are node: X" |
| `test_relay_pattern_in_guidelines` | Deliver guidelines include both Producer and Relay patterns |
| `test_timing_safety` | Topology key set before `execute_turn`; provider reads it after `current_agent_context.set` |
| `test_consumed_pending_filtered_on_non_resume` | AgentNode filters CONSUMED_PENDING on all paths (not just resume) |
| `test_empty_when_all_consumed_pending` | All delivers consumed → empty IntegratedInput (agent uses existing session memory) |
| `test_resume_snapshot_still_prepended` | Resume snapshot still prepended after filtering |
| `test_re_execution_skips_origin_request` | Re-execution (session has messages) skips `[Origin Request]` duplication |
| `test_first_execution_includes_origin_request` | First execution (empty session) includes `[Origin Request]` |
| `test_start_payload_is_skipped_not_duplicated` | `__start__` payloads not rendered as `[Input from graph node '__start__']` |
| `test_framework_sentinel_annotated_and_status_skipped` | Framework sentinels annotated as "framework feedback", Upstream Status skipped |

## Agent Node Input Idempotency

Agent nodes have a second persistence layer the engine does not know about: ReAct session memory (`MessageStore`). Upstream input injected as `SYSTEM_REMINDER` survives across invocations. On crash recovery, the engine's default `_integrate_upstream` re-collects all consumable delivers (including `CONSUMED_PENDING` from the crashed invocation), which would duplicate the `SYSTEM_REMINDER` in the agent's session history.

### Fix: two-layer idempotency

**Layer 1 — `AgentNode._integrate_upstream` override** (`src/modex_agent/agents/agent_node.py`):

Always filters `CONSUMED_PENDING` delivers (the engine default only filters them on GraphInterrupt resume):

```python
# Engine default (Node._integrate_upstream):
if is_resume:
    delivers = [d for d in delivers if d.status == PENDING]

# AgentNode override — always filter:
delivers = [d for d in delivers if d.status == PENDING]
```

Behavior by scenario:
- **First execution**: no `CONSUMED_PENDING` exists → no change.
- **Crash recovery, no new delivers**: returns empty `IntegratedInput` → agent runs with existing session memory (which already contains the upstream input from the crashed attempt).
- **Crash recovery, new delivers arrived**: only new `PENDING` delivers consumed and injected.
- **GraphInterrupt resume**: `CONSUMED_PENDING` filtered (same as before), `RESUME` snapshot prepended.

**Layer 2 — `BotAgentNode.execute` re-execution detection** (`examples/bot_project/bot/graph/agent_node.py`):

Checks if session already has messages (`await agent_context.history.to_list()`). On re-execution (crash recovery / resume), skips `[Origin Request]` duplication — the session already has it from the prior invocation:

```python
existing_messages = await agent_context.history.to_list()
is_re_execution = len(existing_messages) > 0

if is_re_execution:
    # Only inject new upstream input (if any)
    reminder = wrap_system_reminder(upstream) if upstream else ""
else:
    # First execution: [Origin Request] + upstream input
    sections = ["[Origin Request]:\n" + str(ctx.user_input.content), upstream]
    reminder = wrap_system_reminder("\n\n".join(sections))
```

### Why this is a justified override (not a divergence)

The engine's `Node._integrate_upstream` is designed for stateless nodes (`FunctionNode`, `DelayNode`) — re-consuming delivers on crash recovery is correct because those nodes need the original input to re-execute from scratch. `AgentNode` has persistent session memory that already holds the input — re-consuming would pollute the memory. The override changes one line (removing the `if is_resume:` guard) and preserves the same method structure. This is the correct convergence point: `AgentNode` is the base class for all agent-backed nodes, and the override applies to all of them uniformly.

## Files Touched

| File | Change |
|------|--------|
| `src/modex_agent/runtime/enums.py` | Add `GRAPH_TOPOLOGY_CONTEXT` to `TurnCustomKey` |
| `src/modex_agent/agents/agent_node.py` | `DESCRIPTION_NOT_FOUND` constant; `_integrate_upstream` override (always filter `CONSUMED_PENDING`) |
| `examples/bot_project/bot/graph/agent_node.py` | `_build_topology_section`, `_resolve_upstream_desc`, `_build_upstream_status`, enhanced `_format_integrated_input` (start/framework sentinel handling), topology key write in `execute`, re-execution detection for `[Origin Request]` idempotency |
| `src/modex_agent/memory/prompt_pipeline/providers.py` | `GraphWorkflowProvider._fetch_content` restructured into `## Graph Node Context` H2 with `### Workflow Guidance` / `### Topology` / `### Your Role` H3 subsections; deliver guidelines gain Relay pattern |
| `src/modex_agent/tools/graph_deliver.py` | `GraphDeliverTool.description` adds current-node header; `GraphDeliverTargetStore` END description replaced with aggregation semantics; non-END description filters `DESCRIPTION_NOT_FOUND` sentinel |
