# T10: External agent integration — opencode as graph node and subagent

> Type: `wayfinder:grilling` (HITL)
> Status: **Abandoned** — fully determined by T01 + T08 + T09
> Blocked by: T01, T08

## Question

How do external agents (opencode) integrate with SessionTree?

## Resolution

**Abandoned.** T01 determines: external agent's `execute_streaming` has its own TurnCompletionWaiter (opencode session tree). T08 determines: `BotAgentNode.execute` removes `isinstance(runner, ReActTurnRunner)` assert, uses `tree.deliver` + `tree.wait_quiesce`. T09 determines: external as subagent → tree tracks via SubagentAutoSendHook (same as native). Our tree sees external agent as a single node; opencode-internal subagents are opaque. No independent decision remains.

### Context

External agent current state:
- `ExternalAgent.run()` → `execute_streaming()` → `await asyncio.wait_for(waiter.wait_complete(), timeout)`
- `TurnCompletionWaiter` monitors opencode session tree (root + child sessions via SSE)
- Turn completes when entire session tree quiesces (all idle, 3s window)
- `SubagentAutoSendHook.finally_graph` fires when external turn ends → notifies parent
- opencode's `task` tool has foreground (blocking) and background (async) modes

### Two integration scenarios

**Scenario 1 — External agent as graph node (main agent in a graph pool)**:
- `BotAgentNode.execute` currently asserts `isinstance(runner, ReActTurnRunner)` — blocks external
- Remove assert → `ExternalTurnRunner.process_locked` runs the turn
- `ExternalAgent.run()` → `execute_streaming` → TurnCompletionWaiter (opencode's tree) → turn ends
- Our SessionTree: root node = external main agent session
- If opencode agent dispatches internal subagent → opencode's TurnCompletionWaiter handles it (no our tree involvement)
- Our tree: root node COMPLETED when `execute_streaming` returns
- **No SessionTreeWaiter needed for external main** — opencode handles its own tree

**Scenario 2 — External agent as subagent (dispatched by native main agent)**:
- Native main agent calls `task(target="opencode", ...)` → `tree.deliver(opencode_session, task_envelope)` → tree node DISPATCHED
- opencode subagent runs via `ExternalTurnRunner.process_locked` → `execute_streaming` → TurnCompletionWaiter
- opencode subagent completes → `SubagentAutoSendHook.finally_graph` → `tree.deliver(parent_session, reply_envelope)` → tree marks opencode node COMPLETED
- Our tree tracks: opencode subagent node = DISPATCHED → COMPLETED (via SubagentAutoSendHook)
- **opencode's internal subagents are invisible to our tree** — we only see the top-level opencode subagent

### Key insight

Our SessionTree and opencode's TurnCompletionWaiter operate at different levels:
- **Our tree**: tracks cross-agent message dispatch/consume (native↔native, native↔external)
- **opencode's tree**: tracks opencode-internal session tree (opencode main ↔ opencode subagents)

They don't conflict — our tree sees the external agent as a single node. What happens inside opencode is opaque to us.

### Open questions

- How does `tree.wait_quiesce` know when an external subagent is "complete"? (SubagentAutoSendHook fires → tree.deliver reply → tree node COMPLETED. But what if the hook fails / external process crashes?)
- If opencode main agent is a graph node: does `tree.wait_quiesce` just wait for `execute_streaming` to return? (Yes — opencode handles its own waiting.)
- External subagent crash recovery: if opencode process dies mid-subagent, how does our tree detect this? (SubagentAutoSendHook never fires → tree node stays DISPATCHED → timeout?)
- Does `ExternalTurnRunner` need any modification for tree integration? (Probably not — tree is above the runner.)
