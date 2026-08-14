# T09: Peer communication closure in graph context — resolution

> Type: `wayfinder:grilling` (HITL)
> Status: **Resolved**
> Blocks: T08

## Question

How to disable peer (NORMAL agent) communication in graph context, while keeping it enabled in normal chat?

## Resolution

**Simple approach: `TaskDispatchTool` checks `ctx.graph_context is not None` per-turn. No pool-level state mutation. No Configurator (deferred to TurnContextConfigPipeline integration — see MAP "Not yet specified").**

### Peer communication and Tree: unified with external input

Peer messages are **another tree's external input** — isomorphic to user input from the receiving tree's perspective:

| Message source | Sending tree | Receiving tree |
|---|---|---|
| User input (EXTERNAL_INPUT) | N/A | External input → new root node version, no MessageTrack |
| Subagent dispatch (TASK_REQUEST) | Creates track (DISPATCHED) | Internal message → new child node version |
| Subagent reply (AGENT_RESULT) | Creates track (DISPATCHED) | Internal message → new parent node version |
| **Peer message (AGENT_MESSAGE)** | **No track (not subagent)** | **External input → new root node version, no MessageTrack** |

Peer = another tree's root node. A peer message is the receiving tree's external input, exactly like user input. This is the **convergence point**: all external sources (user / peer) trigger root node new version, no track creation.

### `tree.deliver` handling for peer messages

```
tree.deliver(target_session_id, envelope):
  if envelope.message_type == TASK_REQUEST or AGENT_RESULT:
    → subagent communication → create MessageTrack (DISPATCHED) → bus.send
  elif envelope.message_type == EXTERNAL_INPUT or AGENT_MESSAGE (peer):
    → external input → no track → bus.send directly
```

Peer messages pass through `tree.deliver` without track creation — same path as EXTERNAL_INPUT. **Peer 完全不影响发送方 tree** — 无 track, 无 quiesce 影响, 无 tree status 变化. 接收池的 tree 通过 InboxPoller 消费后 `on_dispatch_start` 创建/复用接收池的 node (版本由 dispatch 创建, 非 deliver). 两个 tree 完全独立.

### Graph context: tool-level filter, not tree-level block

Graph context disabling peer is a **semantic constraint of graph scheduling** (graph node's work is scoped to its own pool), not a tree-level mechanism. Tree handles peer messages fine — the constraint is that graph node's `TaskDispatchTool` doesn't offer peer targets to the LLM.

### Mechanism

`TaskDispatchTool` is a pool-level singleton. `CommunicationTargetStore` is pool-level shared. Neither is modified. Instead, per-turn filtering based on `ctx.graph_context`:

```python
class TaskDispatchTool(Tool):
    
    def _visible_targets(self, ctx: AgentContext) -> list[CommunicationTarget]:
        """Per-turn filtered view. Does NOT modify the shared store."""
        targets = self._store.list()  # read-only, returns list copy
        if ctx.graph_context is not None:
            # Graph context: subagents only, no peers
            targets = [t for t in targets if t.kind == AgentCommKind.SUBAGENT]
        return targets
    
    @property
    def description(self) -> str:
        # Note: description is property (no ctx access).
        # For graph context, the dynamic schema (get_dynamic_schema) filters.
        # The static description shows all targets — acceptable: LLM primarily
        # uses the schema enum, not the description, to pick target_agent.
        return self._build_description()
    
    def get_dynamic_schema(self, ctx: AgentContext | None = None) -> dict[str, Any]:
        schema = super().get_dynamic_schema()
        if ctx is not None and ctx.graph_context is not None:
            # Filter target_agent enum to subagents only
            visible = self._visible_targets(ctx)
            function = dict(schema.get("function", {}))
            parameters = dict(function.get("parameters", {}))
            properties = dict(parameters.get("properties", {}))
            if "target_agent" in properties:
                properties["target_agent"] = {
                    **properties["target_agent"],
                    "enum": [t.name for t in visible if t.kind == AgentCommKind.SUBAGENT],
                }
            parameters["properties"] = properties
            function["parameters"] = parameters
            return {**schema, "function": function}
        return schema
    
    async def execute(self, **kwargs: Any) -> str:
        target_agent = str(kwargs.get("target_agent", ""))
        ctx = _current_agent_context()
        if ctx is not None:
            visible = self._visible_targets(ctx)
            if not any(t.name == target_agent for t in visible):
                available = ", ".join(t.name for t in visible)
                return f"Error: '{target_agent}' is not available in this context. Available: {available}"
        # ... existing dispatch logic
```

### Why this is safe for concurrent sessions

- `ctx` (AgentContext) is **per-turn fresh** — constructed in `build_runtime_and_context`
- `ctx.graph_context` is set per-turn by graph context binding (configurator or direct)
- `self._store.list()` is **read-only** — returns a new list, does not modify the store
- **No state stored on the tool instance** — no `_graph_mode` flag, no cached filtered list
- Two concurrent turns (graph + normal on same CACHED session) each pass their own `ctx` → filtering is independent

### What `get_dynamic_schema` needs

Current `get_dynamic_schema` does not receive `ctx`. It needs to access `ctx` via `current_agent_context` contextvar (same pattern as `execute`). This is a minor signature/implementation change — the contextvar is already set by `TurnRunner` before tool execution.

### Migration path (deferred)

When TurnContextConfigPipeline is implemented (see MAP "Not yet specified"):
- `PeerCommunicationConfigurator`: `applies() → desc.graph_context is not None` → sets `ctx.runtime.state.custom[TurnCustomKey.GRAPH_MODE] = True`
- `TaskDispatchTool._visible_targets`: checks `ctx.runtime.state.custom.get(TurnCustomKey.GRAPH_MODE)` instead of `ctx.graph_context`
- Functionally identical — just moves the switch from `graph_context` to a typed custom key, consistent with other graph configurators

## Comments

### Resolved in this grilling session

- **Simple approach: per-turn `ctx.graph_context` check** — confirmed, no pool-level mutation
- **`get_dynamic_schema` filters enum + `execute` validates** — LLM sees only subagents, peer targets rejected if attempted
- **No Configurator (deferred to TurnContextConfigPipeline)** — simple check is sufficient for now
- **Migration path documented** — Configurator integration is a future ticket, not blocking Tree delivery
- **Peer messages = receiving tree's external input** — isomorphic to user input. No MessageTrack on either side. Triggers root node new version on receiving tree. Convergence: all external sources (user / peer) are the same from the receiving tree's perspective.
- **`tree.deliver` passes peer messages through without track** — same path as EXTERNAL_INPUT
- **Graph context peer closure is tool-level, not tree-level** — tree handles peer messages fine; constraint is on graph scheduling semantics
