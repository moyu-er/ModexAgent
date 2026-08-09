# Graph Node Agent Context Injection

Inject full graph topology, Origin Request linkage, upstream/downstream node descriptions, missing-upstream explanation, and END aggregation semantics into agent context when an agent runs inside a graph node. All injection flows through two existing convergent paths — `GraphWorkflowProvider` (system prompt) and `_format_integrated_input` (SYSTEM_REMINDER) — plus the `GraphDeliverTool` description; no third parallel path is introduced.

## Context

When `BotAgentNode.execute` runs a ReAct agent turn inside a graph node, the agent sees only: (a) a generic "you are a node in a graph workflow" prompt, (b) upstream payloads as `[Input from graph node 'X']` messages, (c) available deliver targets in the deliver tool. The agent does **not** know: which graph it is in, its own node name/identity, the full topology, its position (upstream/downstream), the Origin Request's source and nature, whether missing upstream nodes will deliver later, or how END aggregates multiple deliveries into the final reply. This causes agents to confuse Origin Request with node delivers, wait indefinitely for inputs from non-activated paths, and produce deliver content in formats misaligned with END's aggregation semantics.

## Decision

Inject structured graph context across three layers, all via existing convergent paths:

1. **System prompt (GraphWorkflowProvider)**: a `## Graph Node Context` section (single H2) with `### Topology` subsection carrying the full DAG (node names without type labels, edges, current node highlighted, END aggregation label, Origin Request linkage). Node descriptions are **not** included here — only structural skeleton, to avoid noise from irrelevant nodes.

2. **SYSTEM_REMINDER (_format_integrated_input)**: upstream node descriptions alongside delivered content (only for nodes that actually delivered input), plus an `[Upstream Status]` block explaining which upstream nodes delivered and which paths were not activated (with explicit "no further input expected, proceed" guidance).

3. **Deliver tool (GraphDeliverTool.description)**: current node identity ("You are node: X") + enhanced END description with aggregation semantics (delivery-order concatenation into reply list, input expectations for direct upstream).

4. **Deliver content template**: two patterns — **Producer** (Task/Result/Status, existing) and **Relay** (Source/Selection/Summary/Omitted, new). Relay is for nodes that selectively pass summarized upstream content downstream; verbatim forwarding is discouraged because it defeats the node's filtering role.

5. **Agent node input idempotency**: `AgentNode` overrides `Node._integrate_upstream` to always filter `CONSUMED_PENDING` delivers (the engine default only filters them on GraphInterrupt resume). Agent session memory (ReAct `MessageStore`) persists upstream input across invocations — crash recovery must not re-inject already-consumed delivers. `BotAgentNode.execute` detects re-execution (session has existing messages) and skips `[Origin Request]` duplication, injecting only new upstream input.

## Considered Options

- **Topology as a separate SYSTEM_REMINDER message**: rejected — topology is stable per-turn metadata, not dynamic input; system prompt is the correct home for "who am I" context, and SYSTEM_REMINDER interleaving would bury it among input messages.
- **Per-node context only in deliver tool**: rejected — the agent needs topology *before* deciding what to deliver; tool description is evaluated at tool-call time, too late for reasoning context.
- **Full node descriptions in topology**: rejected as noise — an agent only interacts with its direct upstream (with input) and downstream (via deliver). Descriptions of unrelated branch nodes add tokens without actionable value.
- **Third injection path (new provider + new message type)**: rejected per convergence rule — existing two paths (GraphWorkflowProvider + _format_integrated_input) cover stable-metadata and dynamic-input use cases; a third path would diverge the injection mechanism.

## Consequences

- The `## Graph Node Context` / `### Topology` section format becomes a contract with the agent: changing the format (node listing order, END description wording, Origin Request linkage text) may require re-tuning agent prompts that depend on specific phrasing.
- `_format_integrated_input` gains a dependency on `self._graph_ref` for upstream description lookup and missing-upstream computation — it must guard `None` (test/no-scheduler scenarios).
- The Relay deliver pattern adds a second content template; agents must self-select the appropriate pattern based on their role. This is guidance, not enforcement — the deliver tool accepts any string content.
- END's description in `GraphDeliverTool` becomes semantically detailed (aggregation behavior, input expectations) — this replaces the current hardcoded one-liner and is the primary guidance for END's direct upstream nodes.
- `AgentNode._integrate_upstream` diverges from the engine's `Node._integrate_upstream` — it always filters `CONSUMED_PENDING` delivers instead of only on resume. This is a justified override: agent nodes have a second persistence layer (session memory) that the engine does not know about. Re-consuming already-injected delivers would duplicate `SYSTEM_REMINDER` messages in the agent's session history.
