# Layered Configuration Matrix

## Overview

Agent turn configuration is determined by three orthogonal dimensions, each
independent:

| Dimension | Values | Decides |
|-----------|--------|---------|
| **Implementation** | native (ReAct) / external (CLI) | TurnRunner type, whether configurator pipeline runs |
| **Topology** | normal (main) / subagent | Tool set, memory layer, session strategy, comm tools |
| **Mode** | session / graph | Whether graph configurators fire, graph_instance_id propagation |

Graph mode is the **upper layer** — it sits above the normal/subagent split.
Subagents in graph mode remain atomic agents: they carry `graph_instance_id`
(for reply routing) but never receive graph-exclusive tools, hooks, or system
prompt providers. This preserves the separation between "graph workflow node"
(a main agent role) and "subagent" (a helper role), regardless of whether the
parent graph is active.

## Configuration Distribution Matrix

| Config Item | native main session | native main graph | native sub session | native sub graph | ext main session | ext main graph | ext sub session | ext sub graph |
|-------------|:-:|:-:|:-:|:-:|:-:|:-:|:-:|:-:|
| **graph_instance_id** | — | ✅ | — | ✅ | — | ✅ | — | ✅ |
| **graph_context** | — | ✅ | — | ✅ | — | — | — | — |
| **approval** | pool default | None | pool default | None | N/A | N/A | N/A | N/A |
| **deliver tool** | — | ✅ | — | — | — | — | — | — |
| **send_to_peer tool** | ✅ (if peers) | ❌ excluded | — | — | ❌ | ❌ | — | — |
| **knowledge tool** | — | ✅ | — | — | — | — | — | — |
| **MAX_TURNS=3** | default | ✅ | default | default | N/A | N/A | N/A | N/A |
| **topology** | — | ✅ | — | — | — | — | — | — |
| **knowledge keys** | — | ✅ | — | — | — | — | — | — |
| **GraphWorkflowProvider** | empty | ✅ full | empty | empty | — | — | — | — |
| **KnowledgeHook** | no-op | ✅ active | no-op | no-op | — | — | — | — |
| **DeliverRetryHook** | no-op | ✅ active | no-op | no-op | — | — | — | — |
| **SubagentAutoSendHook** | — | — | — (session) | ✅ (reply) | — | — | — (session) | ✅ (reply) |

Graph-exclusive components (deliver tool, knowledge tool, GraphWorkflowProvider,
KnowledgeHook, DeliverRetryHook) fire **only on graph node main agents**
(`is_node_execution=True` + `agent_kind=NORMAL`). Subagents — even in graph
mode — are excluded by the `is_node_execution` gate or by checking the
absence of `GRAPH_TOPOLOGY_CONTEXT` / `GRAPH_KNOWLEDGE_DIR` state keys.

## Gate Mechanisms

### Configurator Pipeline (native only)

6 configurators run in `build_runtime_and_context` via
`TurnContextConfigPipeline.configure(ctx, desc)`. Each has an `applies()`
gate on `TurnContextDescriptor`:

| Configurator | Gate | Fires on |
|--------------|------|----------|
| GraphContextBinding | `graph_instance_id is not None` | All graph turns (main + subagent) |
| GraphApproval | `graph_instance_id is not None` | All graph turns (main + subagent) |
| GraphMaxTurns | `is_node_execution and NORMAL` | Graph node main only |
| GraphTool | `is_node_execution and NORMAL` | Graph node main only |
| GraphTopology | `is_node_execution and NORMAL` | Graph node main only |
| GraphKnowledge | `is_node_execution and NORMAL` | Graph node main only |

### Graph-Aware Components (runtime gate)

Components that check `AgentContext` at runtime (not via configurator):

| Component | Gate expression | File |
|-----------|----------------|------|
| GraphWorkflowProvider | `_is_graph_node_execution(ctx)` — checks `graph_context` + `GRAPH_TOPOLOGY_CONTEXT` state key | `providers.py` |
| KnowledgeHook | `_has_knowledge_config(ctx)` — checks `graph_context` + `GRAPH_KNOWLEDGE_DIR` state key | `knowledge_hook.py` |
| DeliverRetryHook | `ctx.tool_manager.get_tool("deliver") is None` — checks deliver tool existence | `deliver_retry.py` |
| GraphDeliverTool | `agent_context.graph_context is None` | `graph_deliver.py` |
| SubagentAutoSendHook | `ctx.graph_instance_id` — injects into reply metadata | `subagent_auto_send.py` |

The runtime gates check the **product** of the configurator pipeline
(state keys / installed tools), not the configurator inputs. This is more
precise: it tests whether configuration actually ran, not whether it should
have run.

## Data Flow

### Graph Instance ID Propagation

```
BotAgentNode.execute
  → binding_store.bind(main_session, SessionBinding(task_id=42, ...))
  → tree.deliver(main_session, envelope{metadata: {graph_instance_id: 42}})
    → _maybe_bind_session: binding exists → skip (no overwrite)
    → InboxPoller → main agent turn
      → _build_turn_descriptor → binding_store.get(main_session) → full binding

main agent dispatches subagent
  → SubagentDispatchStrategy.execute
    → should_propagate_graph_instance_id() → True
    → envelope.metadata["graph_instance_id"] = 42
    → tree.deliver(subagent_session, envelope)
      → _maybe_bind_session: no existing binding → auto-create SessionBinding(task_id=42)
      → subagent turn: _build_turn_descriptor → task_id-only binding

subagent replies
  → SubagentAutoSendHook._notify_parent
    → ctx.graph_instance_id = 42 → metadata["graph_instance_id"] = 42
    → tree.deliver(parent_session, envelope)
      → _maybe_bind_session: parent binding exists → skip (no overwrite)
      → parent woken: _build_turn_descriptor → full binding (restored)
```

### Peer Communication (cross-tree)

```
peer agent (pool B, graph_instance_id=99)
  → PeerNormalStrategy.execute
    → should_propagate_graph_instance_id() → False
    → envelope.metadata does NOT contain graph_instance_id
    → target.tree_ref.deliver(our_main_session, envelope)
      → _maybe_bind_session: no graph_instance_id in metadata → no binding created
      → our main agent turn: our binding unaffected
```

Peer sends never propagate `graph_instance_id`. Two independent trees maintain
separate binding stores; cross-tree communication does not contaminate graph
configuration.

## Peer Communication Rules

### Graph mode: peers invisible + `send_to_peer` excluded

In graph mode, peer (NORMAL) targets are filtered out at two layers:

1. **`CommunicationTargetStore`** — `list()` filters NORMAL targets, so
   `list_peers()` returns empty and `send_to_peer`'s enum is empty.
2. **`GraphToolPreset.excluded_base_tools`** — `send_to_peer` is excluded by
   name (`SEND_TO_PEER_TOOL_NAME`) when `GraphToolConfigurator` builds the
   graph-scoped tool manager. The tool does not appear in the LLM's tool list
   at all — a stronger guarantee than an empty enum.

Graph nodes communicate via `deliver` (graph edges), not peer messaging.

| Mechanism | Graph mode behavior |
|-----------|---------------------|
| `CommunicationTargetStore.list()` | Filters out `NORMAL` targets |
| `CommunicationTargetStore.list_peers()` | Returns `[]` (based on `list()`) |
| `GraphToolPreset.build_tool_manager()` | Skips `send_to_peer` (in `excluded_base_tools`) |
| `send_to_peer` tool visibility | **Not in tool list** (excluded by preset) |

`SUBAGENT` targets remain visible (graph nodes can dispatch subagents).
The gate is `ctx.graph_instance_id is not None` on the current
`AgentContext`.

### Session mode: cross-tree peer deliver

In session mode, peer sends go to the receiver's tree via
`target.tree_ref` (or fall back to `deps.tree`). The receiver's
`_ensure_node` creates or reuses a tree node in the receiver's tree.
`should_propagate_graph_instance_id() → False` ensures no graph
contamination.

| Deliver path | `tree_ref` set | `tree_ref` None |
|--------------|----------------|-----------------|
| Target tree | `target.tree_ref.deliver()` | `deps.tree.deliver()` (local) |

## SessionBindingStore

The binding store is the single source of truth for session-level graph
configuration. It replaces envelope-metadata transport for
`graph_node_name`, `is_node_execution`, and `graph_artifacts` — these are
stored once (at `BotAgentNode.execute` time) and recovered via `session_id`
lookup on every turn, including subagent-reply wakeups.

| Binding type | Created by | Fields |
|--------------|-----------|--------|
| Full (main agent) | `BotAgentNode.execute` | task_id, graph_node_name, is_node_execution=True, graph_artifacts |
| Task-id only (subagent) | `tree.deliver._maybe_bind_session` | task_id only |
| None (session mode) | — | No binding exists |

Binding lifecycle:
- `bind(session_id, binding)` — idempotent overwrite (crash recovery safe)
- `get(session_id)` — returns binding or None
- `unbind(session_id)` — called by `BotAgentNode.execute` finally block and
  `on_session_evicted`
- Conflict detection: `_maybe_bind_session` raises `ValueError` if an
  existing binding's `task_id` conflicts with an incoming envelope's
  `graph_instance_id` (detects concurrent graph instances sharing a CACHED
  session)

## Isolation Guarantees

Configuration is per-turn and per-session — different agents, different
sessions, and different modes on the same agent+session never interfere.

### Same agent, same session, different modes

The same agent's same `session_id` can serve a graph-mode turn and a
session-mode turn sequentially. The binding store's lifecycle makes this safe:

```
Graph turn:
  BotAgentNode.execute → binding_store.bind("conv1.main", full_binding)
  → _build_turn_descriptor → binding → 6 configurators fire
  → turn ends → finally → binding_store.unbind("conv1.main")  ← cleanup

Session turn (same "conv1.main"):
  User DM → _build_turn_descriptor → binding_store.get("conv1.main") → None
  → all configurators skip → session-mode AgentContext
```

Two AgentContext objects are **independent per-turn constructs**
(`build_runtime_and_context` creates a fresh one each call). The agent
singleton itself is stateless — all state lives in AgentContext, discarded
after the turn.

### Same agent, different sessions

Different sessions of the same agent have completely isolated bindings:

```
conv1.main (graph mode):
  binding_store["conv1.main"] = full_binding(task_id=42)

conv2.main (session mode):
  binding_store["conv2.main"] → None (no binding)

conv3.main (different graph instance):
  binding_store["conv3.main"] = full_binding(task_id=99)
```

`InMemorySessionBindingStore` is a `dict[session_id, SessionBinding]` — each
session_id is an independent key. No cross-session reads, no shared state.

### Isolation layers

| Layer | Mechanism | Scope |
|-------|-----------|-------|
| **session_id** | binding_store dict key | Different sessions of same agent never share bindings |
| **AgentContext** | Per-turn fresh dataclass | Each turn gets a new context; agent singleton is stateless |
| **Pool** | binding_store is per-pool (created in `factory.py`) | Different pools have independent stores |
| **Temporal** | `BotAgentNode.execute` finally unbinds | Graph turn ends → binding cleared → next turn on same session is session-mode |
| **Concurrency** | InboxPoller single-flight (`inflight` dict) | Same session has at most one turn at a time |
| **Crash** | InMemory store lost on process death | No stale bindings after restart |

### Edge case verification

| Scenario | Result |
|----------|--------|
| Graph turn running, DM arrives | Single-flight blocks → DM waits in inbox → graph turn ends + unbind → DM turn: no binding → session mode |
| Graph turn crashes, process restarts | InMemory store lost → no binding → graph recovery re-binds → safe |
| Two graph instances share CACHED session | `_maybe_bind_session` task_id conflict → `ValueError` |
| Graph turn cancelled (CancelledError) | `try/finally` guarantees unbind |
| Session turn first, graph turn later | Session turn: no binding created → graph turn: bind on empty store → no conflict |
| Same agent, session A (graph) + session B (session) | Different dict keys → A has binding, B has None → no interference |

## Test Coverage

`tests/unit/pipeline/test_layered_config_matrix.py` — 28 tests covering:

1. **Configurator matrix** (4 tests): all 4 native combinations (session/graph
   × main/subagent) verify correct configurator firing
2. **Graph-aware component exclusion** (7 tests): GraphWorkflowProvider,
   KnowledgeHook, DeliverRetryHook correctly exclude subagents in graph mode
3. **Communication propagation** (5 tests): SubagentDispatch + ParentReply
   propagate graph_instance_id; PeerNormal does not
4. **Binding store round-trip** (4 tests): parent binding survives subagent
   reply; subagent binding is task_id-only; unbind cleanup
5. **External agent** (1 test): external gets graph_instance_id only, no
   graph tools/hooks/providers
6. **Peer communication — graph mode** (4 tests): NORMAL targets hidden in
   `list()`/`get()`/`has()`/`description`; SUBAGENT targets still visible.
   `send_to_peer` tool excluded from graph tool manager via
   `GraphToolPreset.excluded_base_tools`
7. **Peer communication — session cross-tree** (2 tests): peer deliver uses
   `target.tree_ref`; falls back to `deps.tree`; no graph_instance_id in
   envelope

## File Reference

| File | Role |
|------|------|
| `session_tree/session_binding.py` | SessionBinding + SessionBindingStore ABC + InMemory impl |
| `session_tree/manager.py` | `_maybe_bind_session` auto-creation + `binding_store` property |
| `pipeline/turn_context_config.py` | 6 configurators + TurnContextDescriptor |
| `pipeline/turn_runner.py` | `_build_turn_descriptor` reads from binding store only |
| `pipeline/turn_context_builder.py` | `session_binding_store` property on TurnContextBuilder |
| `agents/external/turn_runner.py` | Reads task_id from binding store (no configurator pipeline) |
| `memory/prompt_pipeline/providers.py` | `GraphWorkflowProvider` + `_is_graph_node_execution` |
| `hook/builtin/knowledge_hook.py` | `KnowledgeHook` + `_has_knowledge_config` |
| `hook/builtin/deliver_retry.py` | `DeliverRetryHook` (deliver tool existence gate) |
| `multi_agent/communication/strategies/base.py` | `should_propagate_graph_instance_id` hook |
| `multi_agent/communication/strategies/subagent_dispatch.py` | Override: True |
| `multi_agent/communication/strategies/parent_reply.py` | Override: True |
| `multi_agent/communication/strategies/peer_normal.py` | Default: False (no override) |
| `multi_agent/tools.py` | `CommunicationTargetStore.list_subagents()`/`list_peers()` + `SEND_TO_PEER_TOOL_NAME` + `SendToPeerTool` |
| `tools/graph_tool_preset.py` | `GraphToolPreset.excluded_base_tools` — excludes `send_to_peer` in graph mode |
| `multi_agent/message_format.py` | Peer reply contract instructs `send_to_peer` (not `task`) |
