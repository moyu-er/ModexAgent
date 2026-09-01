# ADR-0037: BeforeTurnNode and AfterTurnNode — Graph-Internal Turn Lifecycle

Status: implemented (2026-08-09).

## Context

The ReAct agent's turn lifecycle (BEFORE_TURN / AFTER_TURN / FINALLY_TURN
hooks) was dispatched in `ReActAgent.run()`'s `actual_turn()` closure —
outside the graph engine. This created two problems:

1. **No graph-internal continuation mechanism.** When an agent finished a
   turn without satisfying a post-condition (e.g. deliver-retry: agent
   ended without calling the `deliver` tool), the only way to re-run the
   turn was an external loop. `BotAgentNode.execute` contained a hardcoded
   for-loop that called `execute_turn` up to 2 times, injecting a
   system-reminder between attempts. This loop fired FINALLY_TURN per
   retry (a latent issue — verified all 9 `FinallyTurnHook` implementations
   expect once-per-logical-turn), bypassed the graph engine's routing
   capability, and mixed graph-layer concern (continuation) with
   graph-caller concern (orchestration).

2. **No shared terminal mechanics inside the graph.** Cancellation paths
   (`TOOL → END` via dedup_stop, CANCEL_TURN deny policy, no llm_response)
   could route directly to END. A graph-internal continuation decision must
   run on every terminal graph path, independent of the turn-level hooks
   that remain in `actual_turn()`.

Reference projects were surveyed for comparison:
- **OpenCode**: hooks mutate shared `output` objects (data mutation, not
  control flow). No continuation mechanism — hooks can modify LLM
  requests but cannot trigger additional ReAct iterations.
- **oh-my-openagent**: plugins trigger continuation via
  `session.promptAsync()` SDK API call (side-effect). 15+ ad-hoc
  condition checks scattered across one hook. External to the agent loop.

Both systems mix observation and control in the same mechanism. ModexAgent
has a three-layer architecture (Hook / Interceptor / Control) that allows
a cleaner separation, but it was not utilized for continuation.

The full deliberation is in
[docs/deliberations/0001-hook-driven-turn-rerun.md](../deliberations/0001-hook-driven-turn-rerun.md).

## Decision

Add two nodes to the ReAct graph topology: **BeforeTurnNode** (between
START and LLM) and **AfterTurnNode** (between LLM/TOOL and END). These
nodes own the turn-attempt lifecycle and enable graph-internal
continuation.

### Topology

```
Current (4 nodes):
  START → LLM ↔ TOOL → END → GraphNode.END

New (6 nodes):
  START → BEFORE → LLM ↔ TOOL → AFTER → END → GraphNode.END
                         ↑                    ↓
                         └──── continuation ──┘
```

Two nested loops:
- **Inner loop** (iteration): `LLM ↔ TOOL` — one ReAct reasoning+acting cycle
- **Outer loop** (turn attempt): `BEFORE → LLM → AFTER → BEFORE` — one turn attempt; continuation restarts

All graph paths to END go through AFTER. This ensures result construction
and the continuation gate run on cancellation and error paths as well as
normal completion.

### Node responsibilities

**BeforeTurnNode**:
- Increment `state.turn_attempt` (1 on first call, 2 on first continuation, etc.)
- Reset `state.iteration = 0` (fresh iteration budget per turn attempt)
- Route to LLM

**AfterTurnNode**:
- Construct preliminary `AgentResult` from `state.llm_response` +
  `state.message_delta` + `state.phase` (same logic EndNode previously used)
- Write `state.result = result`
- Check `CONTINUATION_REQUEST` flag (boolean presence, value not read):
  - If present AND `turn_attempt < MAX_TURNS` (default 1 (framework), 3 (BotAgentNode)) AND not cancelled:
    pop the flag (one-shot consumption), append the deliver reminder, and
    route to BEFORE (graph continues)
  - Else: route to END (graph ends)

Both nodes are mechanical only. They do not dispatch turn-level hooks.

**EndNode** (simplified):
- Read `state.result` (constructed by AfterTurnNode)
- Emit completion events (`FINAL_OUTPUT` / `ERROR`)
- `state.mark_completed()`
- Deliver to `GraphNode.END`

**StartNode** (routing change):
- Fresh start: deliver to BEFORE (was LLM)
- Resume from approval: deliver to `state.resume_target` (TOOL) — bypasses
  BEFORE (correct: resume doesn't need iteration reset or turn_attempt increment)

### Continuation mechanism

The continuation flag (`TurnCustomKey.CONTINUATION_REQUEST`) is
**boolean-only** — its presence means "continue", its value is never read.
`DeliverRetryHook` observes `AFTER_LLM_RESPONSE` and only sets this flag.
When `AfterTurnNode` grants the request, it appends the reminder through the
converged `wrap_system_reminder()` + `history.append()` path before routing
to BEFORE. The reminder is not carried by the flag.

The node owns injection because `AFTER_LLM_RESPONSE` fires before `LLMNode`
appends the assistant message. Injection in the hook would produce
`[reminder, assistant_message]`; injection in `AfterTurnNode`, which runs
after `LLMNode`, produces the required `[assistant_message, reminder]` order
so the reminder is the latest message on the continuation attempt.

Separation of concerns:
- **Hook** owns: observing an omitted deliver call and requesting
  continuation (set flag)
- **AfterTurnNode** owns: whether to grant continuation (turn budget check),
  reminder injection mechanics, and routing

This separates observation (hook) from control (node), consistent with
the framework's "hooks return None" principle.

### Hook dispatch ownership

`BEFORE_TURN` and `AFTER_TURN` stay in `ReActAgent.run()`'s `actual_turn()`
closure. `FINALLY_TURN` stays in `run()`'s `finally` block. None of these
turn-level hooks are dispatched through the graph runtime, and all fire at
most once per logical turn (`run()` call), not once per continuation attempt.

### State additions

`ReActTurnState` gains one field:

| Field | Type | Default | Purpose |
|---|---|---|---|
| `turn_attempt` | `int` | `0` | Current turn attempt number (1=first, 2=first continuation, etc.) |

`TurnCustomKey` gains three entries:

| Key | Purpose |
|---|---|
| `GRAPH_DELIVER_COUNT` | Deliver call count (written by GraphDeliverTool, read by hooks) |
| `CONTINUATION_REQUEST` | Boolean-only flag; presence = "continue" (written by hooks, read+popped by AfterTurnNode) |
| `MAX_TURNS` | Hard cap on turn attempts; default 1 (framework), 3 (BotAgentNode) (written by caller, read by AfterTurnNode) |

### Edge changes

```
START → BEFORE       # fresh start (was START → LLM)
START → TOOL         # approval resume (unchanged)
BEFORE → LLM         # new
LLM   → TOOL         # unchanged
LLM   → AFTER        # stop / error / max_iter (was LLM → END)
TOOL  → LLM          # unchanged
TOOL  → AFTER        # cancellation (was TOOL → END)
AFTER → END          # normal end
AFTER → BEFORE       # continuation (new)
END   → GraphNode.END  # unchanged
```

## Consequences

### Positive

1. **Graph-internal continuation.** The graph engine handles continuation
   via topology (AFTER → BEFORE edge), not an external loop. This is the
   first ReAct graph with an explicit turn-attempt loop in its topology.

2. **Cleaner separation than reference projects.** OpenCode mixes
   observation and control in hook output mutation. oh-my-openagent mixes
   them in SDK API calls. ModexAgent separates: hooks observe and signal
   (boolean flag), nodes perform continuation mechanics (budget check,
   correctly ordered reminder injection, and routing).

3. **Uniform graph-terminal mechanics.** Cancellation paths (dedup_stop,
   CANCEL_TURN, no llm_response) now flow through AfterTurnNode, so result
   construction and continuation suppression are consistent.

4. **EndNode simplified.** Result construction moves to AfterTurnNode,
   which needs it for the continuation decision. EndNode becomes a thin
   completion-event emitter.

5. **BotAgentNode for-loop removed.** Deliver-retry logic moves from
   graph-caller code to a hook + graph topology. BotAgentNode calls
   `execute_turn` once.

6. **Turn-level hooks fire once per logical turn.** Replacing
   BotAgentNode's repeated `execute_turn` calls with graph-internal
   continuation keeps BEFORE_TURN, AFTER_TURN, and FINALLY_TURN at their
   `run()`-level dispatch points.

7. **Extensible continuation.** Any hook can set `CONTINUATION_REQUEST`
   for any reason — deliver-retry, todo-continuation, quality-check.
   AfterTurnNode is generic; policy lives in hooks.

### Negative

1. **Continuation attempts are not turn-level hook boundaries.**
   BEFORE_TURN and AFTER_TURN remain once-per-`run()` lifecycle points.
   Policies that need per-attempt observation must use graph-level state or
   an existing iteration/node hook rather than assuming a continuation
   redispatches turn-level hooks.

2. **Graph topology is more complex.** 6 nodes and 11 edges instead of 4
   and 8. The `AFTER → BEFORE` edge creates a cycle (same category as
   the existing `TOOL → LLM` cycle — `cycle_detection="warn"` logs a
   warning, which is expected and acceptable).

3. **`max_iterations` interaction.** `state.iteration` resets to 0 in
   BeforeTurnNode. Each turn attempt gets a fresh iteration budget. The
   legacy engine-level `compile(max_iterations=N)` safety net was removed
   (engine default is unlimited; the `LLMNode` business gate is the sole
   cap) — formerly it had to be set to `business_max * MAX_TURNS` to allow
   for continuations (e.g. business 25 × MAX_TURNS 3 = compile with 75).

4. **AfterTurnNode constructs preliminary result.** The result is
   constructed before EndNode runs and stored on `state.result`. EndNode
   emits completion from that same result, and `actual_turn()` dispatches
   AFTER_TURN with it after the graph returns. This makes result ownership
   earlier in the graph than before, even though hook ownership stays
   outside the graph.

### Approval impact

Approval is not affected. Verified against all approval scenarios:

- **Suspend**: `ctx.interrupt(tx)` raises `GraphInterrupt` in ToolNode —
  graph exits before reaching AfterTurnNode. AFTER_TURN does not fire
  (correct — turn is suspended, not ended). FINALLY_TURN fires in
  `run()`'s finally.
- **Resume**: StartNode routes to `state.resume_target` (TOOL), bypassing
  BeforeTurnNode. `turn_attempt` is not incremented on resume (correct —
  same turn attempt). TOOL completes → LLM → AFTER performs the
  continuation check → END; AFTER_TURN dispatches from `actual_turn()`
  after the engine returns.
- **Deny + CANCEL_TURN**: now flows `TOOL → AFTER → END` (was
   `TOOL → END`). AfterTurnNode sees `phase == CANCELLED`, skips
   continuation, and routes to END.

## Alternatives considered

1. **HookAction return enum** (`AfterTurnHook → HookAction.RERUN`):
   rejected — reintroduces the veto mechanism deliberately removed by
   design; multiple-hook conflict resolution; changes return type across
   all 11 hook points.

2. **Prompt injection API** (`runtime.inject_prompt()`): rejected —
   ModexAgent turns are driven from above (graph → BotAgentNode →
   TurnRunner → ReActAgent), not from below. No `session.promptAsync()`
   equivalent. Larger API surface than needed.

3. **Interceptor `around_turn` loop**: rejected — interceptor calls
   `next_call()` multiple times. Loop lives outside the graph engine.
   Doesn't utilize graph routing capability. User feedback: "seems like
   current implementation + turn loop."

4. **EndNode checks continuation**: rejected — makes EndNode less deep
   (dual responsibility: result assembly + continuation check). User
   feedback: prefer separate nodes to keep EndNode simple.

5. **Keep BotAgentNode for-loop**: rejected — doesn't satisfy "avoid
   graph-layer design" goal. FINALLY_TURN fires per-retry. Graph-caller
   mixes orchestration with continuation.

## Related

- [Deliberation record](../deliberations/0001-hook-driven-turn-rerun.md)
- ADR-0033: Generalized Graph Engine (the `modex_graph` engine this builds on)
- ADR-0036: Node ID, START/END, GraphPayload breaking changes (graph I/O model)
- `src/modex_agent/agents/react/graph.py`: `build_react_graph()` (topology)
- `src/modex_agent/agents/react/nodes/`: node implementations
- `src/modex_agent/hook/abc.py`: `HookPoint`, `BeforeTurnHook`, `AfterTurnHook`
- `src/modex_agent/runtime/enums.py`: `TurnCustomKey`
- `examples/bot_project/bot/graph/agent_node.py`: BotAgentNode graph caller
