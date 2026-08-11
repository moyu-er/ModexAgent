# T08: Graph node integration — how BotAgentNode.execute uses tree

> Type: `wayfinder:grilling` (HITL)
> Status: **Abandoned** — fully determined by T05 + T11
> Blocked by: T01, T02

## Question

How does `BotAgentNode.execute` use SessionTree?

## Resolution

**Abandoned.** T05 determines: `tree.deliver` + `tree.wait_quiesce` + poller drives turn. T11 determines: graph scheduling exits with no executable node → FAILED if END not reached. T01 determines: graph metadata propagates via envelope metadata. The integration flow is: `BotAgentNode.execute` → `tree.deliver(root_session, graph_input_envelope)` → `tree.wait_quiesce(tree_id)` → return. No independent decision remains.

### Context

Current `BotAgentNode.execute`:
- Directly calls `runner.execute_turn(agent_context, ...)` (bypasses inbox/poller)
- Post-build mutation: deliver tool, graph_context, approval=None, MAX_TURNS, topology
- Auto-deliver if no pending delivers
- Asserts `isinstance(runner, ReActTurnRunner)` — external agent not supported

New direction (from deliberation):
- BotAgentNode delivers graph input via `tree.deliver(root_session, graph_input_envelope)` 
- Tree writes to inbox → pool's normal dispatch drives the turn (InboxPoller or tree scheduler)
- BotAgentNode waits for tree quiesce: `await tree.wait_quiesce(root_session)`
- Turn is driven by normal pool dispatch, NOT by execute directly
- Graph context propagates via envelope metadata (graph_instance_id, is_node_execution)

### Proposed flow

```
BotAgentNode.execute(ctx, integrated_input):
  1. Ensure session (CACHED)
  2. Get or create tree for session (pool.get_or_create_session_tree(session_id))
  3. Build graph input envelope (carries: integrated_input, graph_instance_id, 
     is_node_execution=True, topology, deliver_tool_ref, etc.)
  4. tree.deliver(session_id, envelope) 
     → tree creates/updates root node (new version if needed)
     → inbox gets the message
  5. await tree.wait_quiesce(session_id)
     → tree monitors: root node + all child nodes (subagents)
     → pool dispatch drives turns (agent consumes inbox, runs ReAct, may dispatch subagents)
     → subagents complete → tree nodes complete
     → all nodes complete + no pending inbox → quiesce
  6. Check _has_pending_delivers()
     → True: return (node has output)
     → False: return (node has no output — graph may be FAILED)
```

### "node.run monitors tree completion without blind waiting"

User said: "整个 node.run 都应该监听 tree 的完成情况, 不盲目监听"

This means:
- `node.run` (modex_graph) calls `await execute(ctx, input)`
- `execute` calls `tree.wait_quiesce(session_id)` 
- `wait_quiesce` only waits if there ARE pending nodes (no blind waiting on empty tree)
- If no subagent dispatched → tree quiesces immediately → execute returns
- If subagent dispatched → tree waits → subagent completes → tree quiesces → execute returns

### Key questions

- Does BotAgentNode still configure the agent context (deliver tool, graph_context, etc.)? 
  - If turn is driven by pool dispatch (process_locked), configuration happens in TurnContextConfigPipeline (from envelope metadata)
  - BotAgentNode's 70-line post-build mutation → extracted to configurators
  - BotAgentNode becomes thin: deliver input + wait quiesce + check delivers

- How does graph_instance_id propagate?
  - Envelope metadata carries `graph_instance_id`
  - `turn_runner._process_locked_inner` reads metadata → stamps into turn state
  - Configurators read turn state → apply graph config

- How does external agent work?
  - Remove `isinstance(runner, ReActTurnRunner)` assert
  - External agent's `execute_streaming` has its own TurnCompletionWaiter (opencode session tree)
  - Tree tracks external subagent completion via SubagentAutoSendHook
  - `tree.wait_quiesce` works the same (waits for all tree nodes complete)

- How does the graph input envelope carry topology / deliver_tool?
  - Envelope metadata: `graph_instance_id`, `is_node_execution`, `graph_node_name`
  - Configurator uses `graph_instance_id` to resolve graph context (from GraphOrchestrator or registry)
  - Deliver tool: pre-built by BotAgentNode, passed via... how? (Can't put a Tool object in envelope metadata)
  - **Option**: BotAgentNode registers deliver tool in a per-session registry, configurator retrieves it
  - **Option**: BotAgentNode passes a "deliver tool factory" that configurator invokes

### Open questions for grilling

- Does BotAgentNode still call `runner.execute_turn` directly, or fully delegate to pool dispatch?
  - If fully delegate: graph turn = normal turn (ultimate convergence), but how does BotAgentNode know when the turn is done? (tree.wait_quiesce)
  - If hybrid: BotAgentNode calls execute_turn, then wait_quiesce — but execute_turn already ran the turn, what does wait_quiesce add? (waits for subagents)
- How does deliver tool get installed if configuration happens in process_locked (not BotAgentNode)?
- What about the `is_re_execution` detection (crash recovery)? Tree versioning handles this — new version = new execution.
