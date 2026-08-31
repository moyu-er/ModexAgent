# 0001: Hook-Driven Turn Rerun

## Objective / constraints

**Objective**: Move deliver-retry logic from the graph layer (`BotAgentNode.execute` for-loop) to the ReAct layer, using a hook capability enhancement. Keep the graph layer thin — `BotAgentNode` should call `execute_turn` once, not loop.

**Constraints**:
- Hook return type is `None` — the veto/result mechanism was removed by design (hook/AGENTS.md Rule: "Hooks return None — observation only").
- Per-turn state (`TurnStateBase.custom` via `TurnCustomKey`) is the established hook-to-agent communication channel (hook/AGENTS.md Rule 1).
- `ReActAgent.run()` owns the turn lifecycle: `actual_turn()` closure dispatches BEFORE_TURN → graph engine → AFTER_ITERATION → AFTER_TURN; `finally` block dispatches FINALLY_TURN once.
- `BotAgentNode.execute` currently loops `execute_turn` up to 2 times, injecting a system-reminder between attempts. Each `execute_turn` call triggers a full `agent.run()` including FINALLY_TURN — so FINALLY_TURN fires multiple times for one logical turn.
- Reference patterns: oh-my-openagent triggers ReAct continuation via `session.promptAsync()` (side-effect API call, not return value); OpenCode hooks mutate shared `output` objects (data mutation, not control flow).

## Settled decisions

1. **Retry loop lives in `ReActAgent.run()`, not in `TurnRunner.execute_turn()`** — a deliver-retry is within one logical turn (same session, same user input, same task). Task registration, turn UUID, context save, and cleanup should happen once per logical turn. Each retry goes through `actual_turn()` (BEFORE_TURN → graph → AFTER_TURN), but FINALLY_TURN fires once (in the `finally` block). This is cleaner than the current BotAgentNode for-loop which fires FINALLY_TURN per-retry.

2. **FINALLY_TURN fires once per logical turn** — verified all 9 `FinallyTurnHook` implementations. None depend on per-retry dispatch. The current per-retry behavior is a latent issue, not intentional. (See Exceptions.)

3. **Per-turn state persists across retries** — `ReActAgent.run()` uses the same `context.runtime.state` object throughout. `state.custom` is the same dict across retries. `GRAPH_DELIVER_COUNT` accumulates (deliver tool increments each call); `RERUN_REQUEST` is cleared between retries by the retry loop.

4. **Continuation is graph-internal, not an external loop** (revised) — the graph engine handles continuation via a conditional `END → LLM` routing edge. No loop in `ReActAgent.run()`, no interceptor wrapping. The graph topology itself expresses the continuation.

5. **BeforeTurnNode + AfterTurnNode pair** (revised from EndNode-check approach) — add two new nodes to the ReAct graph, bracketing LLM and TOOL. EndNode stays simple (result assembly only). The before/after pair handles turn-level lifecycle (setup + continuation check), cooperating with each other.

6. **Iteration count resets on continuation** — when AfterTurnNode routes back to BeforeTurnNode, `state.iteration` resets to 0. Each continuation round gets a fresh iteration budget. `max_iterations` is per-turn-attempt, not per-logical-turn.

7. **DeliverRetryHook appends reminder via AFTER_LLM_RESPONSE** — `DeliverRetryHook` is an `AfterLLMResponseHook` (not `AfterTurnHook`); it fires when the LLM returns stop, before `AfterTurnNode` runs. The hook checks `GRAPH_DELIVER_COUNT`, appends the reminder text to `agent_ctx.history` via the converged path (`wrap_system_reminder()` + `history.append({"role": SYSTEM_REMINDER, ...})`), and sets `CONTINUATION_REQUEST`. `AfterTurnNode` is mechanical only — it constructs the result, writes `state.result`, checks the flag, and routes to `BeforeTurnNode` or `EndNode` (no hook dispatch in the node).

8. **CONTINUATION_REQUEST is boolean-only** (revised per user feedback) — the flag's value is never read; presence = "continue". The reminder text is injected into history by the hook as a side-effect, not carried by the flag. This avoids a value that is set but never read.

9. **ADR-0037 written** — the decision qualifies for an ADR (hard to reverse: topology change + hook dispatch relocation; surprising without rationale: 6-node ReAct graph; genuine tradeoff: 5 alternatives considered). See `docs/adr/0037-before-after-turn-nodes-graph-internal-continuation.md`.

## Assumptions

1. **Per-turn state flags are not a "veto"** — a hook writing `state.custom[RERUN_REQUEST]` is state injection (the established pattern), not control-flow veto (which was removed). The hook observes and injects; the agent reads and decides. The distinction: veto *stops or modifies the current turn*; state flag *signals a post-turn action* after the turn completes normally.

2. **`ReActAgent.run()` is the right retry boundary** — a deliver-retry is within one logical turn (same session, same user input, same task). Task registration, turn UUID, context save, and cleanup should happen once per logical turn, not per retry. The current BotAgentNode for-loop calling `execute_turn` multiple times fires these once per retry — likely a latent issue, not intentional design.

3. **`max_reruns` default 0 = backward compatible** — normal chat never sets `RERUN_REQUEST`, so the retry loop runs once and exits. No behavior change for non-graph contexts.

4. **GRAPH_DELIVER_COUNT accumulates across retries** — the deliver tool increments it each call. In retry 2, if the agent called deliver, count > 0 → hook doesn't set RERUN_REQUEST → loop exits. No need to reset the counter between retries.

## Exceptions

- **FINALLY_TURN behavior changes (verified safe)**: Currently fires per-retry (BotAgentNode calls `execute_turn` N times). With the new design, fires once per logical turn. Investigated all 9 `FinallyTurnHook` implementations: `SubagentAutoSendHook` (sends result to parent — once is better, avoids intermediate results), `TrainingDataHook` (one trajectory per logical turn is correct), `TurnOutcomeNotifyHook` (one notification is correct), `CassetteFlushHook` (one flush is correct), `TraceCollectorHook` (one cleanup is correct). None depend on per-retry dispatch. The current multiple-FINALLY_TURN behavior is a latent issue from the BotAgentNode for-loop, not an intentional design. Moving the retry loop to `ReActAgent.run()` fixes this.

## Recommendation

**Add BeforeTurnNode + AfterTurnNode to the ReAct graph. AFTER_TURN hook dispatch moves into AfterTurnNode. Continuation flows graph-internally via AfterTurnNode → BeforeTurnNode.**

### Topology

Current (4 nodes, 8 edges):
```
GraphNode.START → START → LLM ↔ TOOL → END → GraphNode.END
```

New (6 nodes, 12 edges):
```
GraphNode.START → START → BEFORE → LLM ↔ TOOL → AFTER → END → GraphNode.END
                         ↑                    ↑     ↓
                         └──── continuation ───┘     │
                                                    cancel
                         TOOL ──────────────────────→┘
                         LLM (error/max_iter) ──────→┘
```

**All paths to END go through AFTER.** This ensures AFTER_TURN always fires, including on cancellation/error/max-iterations. AfterTurnNode checks `state.phase == CANCELLED` and skips continuation on cancelled turns.

Two nested loops:
- **Inner loop** (iteration): `LLM ↔ TOOL` — one ReAct reasoning+acting cycle
- **Outer loop** (turn attempt): `BEFORE → LLM → AFTER → BEFORE` — one turn attempt, continuation restarts

### Node responsibilities

**StartNode** (modified routing):
- Fresh start: deliver to `BEFORE` (was `LLM`)
- Resume from approval: deliver to `state.resume_target` (TOOL) — bypasses BEFORE (correct: resume doesn't need iteration reset)

**BeforeTurnNode** (new, positioned: START → BEFORE → LLM):
- `state.turn_attempt += 1` (first call = 1, continuation = 2, etc.)
- `state.iteration = 0` (fresh iteration budget per turn attempt)
- Dispatch `BEFORE_TURN` hook (moved from `run()`'s `actual_turn()`)
- Route to LLM

**AfterTurnNode** (new, positioned: all paths → AFTER → END):
- Construct preliminary `AgentResult` from `state.llm_response` + `state.message_delta` + `state.phase` (same logic EndNode currently uses)
- Write `state.result = result` (so `run()` and EndNode can read it)
- Dispatch `AFTER_TURN` hook with the result — **hook injects reminder + sets flag here**
- Read `state.custom[CONTINUATION_REQUEST]` (set by hook)
- If flag present AND `state.turn_attempt < MAX_TURNS` (default 3) AND `state.phase != CANCELLED`:
  - `state.custom.pop(CONTINUATION_REQUEST)` — **consume flag (one-shot, used once)**
  - Route to BEFORE (graph continues)
- Else:
  - Route to END (pass-through, graph ends)

**EndNode** (simplified):
- Read `state.result` (constructed by AfterTurnNode)
- Emit completion events (`FINAL_OUTPUT` / `ERROR`)
- `state.mark_completed()`
- Deliver to `GraphNode.END`
- No more result construction (moved to AfterTurnNode)

### Hook integration: the user's proposal

The user's idea: **AfterTurnHook injects reminder content into history AND sets the continuation flag.** This works because:

1. `ReActTurnState` extends both `GraphState` and `TurnStateBase` — **one object**. `ctx.state.custom` and `agent_ctx.runtime.state.custom` are the **same dict**. Hooks and nodes share state directly.
2. AfterTurnNode dispatches `AFTER_TURN` with a constructed `AgentResult`. The hook receives `ctx: AgentContext, result: AgentResult` — same signature as before.
3. The hook does the injection via the converged path:

```python
class DeliverRetryHook(AfterTurnHook):
    async def after_turn(self, ctx: AgentContext, result: AgentResult) -> None:
        if result.stop_reason == StopReason.TURN_CANCELLED:
            return  # Don't request continuation on cancelled turns
        count = ctx.runtime.state.custom.get(TurnCustomKey.GRAPH_DELIVER_COUNT, 0)
        if count == 0:
            from modex_agent.core.message_utils import wrap_system_reminder
            from modex_agent.core.types import MessageRole
            reminder = "You ended without calling the `deliver` tool. You MUST call `deliver` before finishing."
            await ctx.history.append({
                "role": str(MessageRole.SYSTEM_REMINDER),
                "content": wrap_system_reminder(reminder),
            })
            # Flag is boolean-only — presence means "continue", value is not read.
            # Reminder delivery is the hook's side-effect, not the flag's payload.
            ctx.runtime.state.custom[TurnCustomKey.CONTINUATION_REQUEST] = True
```

4. AfterTurnNode reads the flag AFTER the hook dispatches:
```python
# In AfterTurnNode.execute():
result = self._build_result(state)  # preliminary AgentResult
state.result = result
await ctx.runtime.dispatch_hook(ReActHookPoint.AFTER_TURN, ctx, data={"result": result})

# Now check continuation — boolean presence check, value not read
max_turns = state.custom.get(TurnCustomKey.MAX_TURNS, 3)
if (TurnCustomKey.CONTINUATION_REQUEST in state.custom
    and state.turn_attempt < max_turns
    and state.phase != TurnPhase.CANCELLED):
    state.custom.pop(TurnCustomKey.CONTINUATION_REQUEST)  # consume (one-shot)
    self.deliver(result, ReActNode.BEFORE, ctx)  # graph continues
else:
    self.deliver(result, ReActNode.END, ctx)  # graph ends
```

**Key: the flag is one-shot, boolean-only.** AfterTurnNode pops it. The value (`True`) is never read — the hook's job is to inject the reminder into history (side-effect) and set the flag (signal). AfterTurnNode's job is to check flag presence and route. Separation of concerns: hook owns *what to remind* and *whether to request continuation*; node owns *whether to grant it* (turn budget check) and *routing*.

### State additions

**`ReActTurnState` fields**:

| Field | Type | Default | Writer | Reader | Purpose |
|---|---|---|---|---|---|
| `turn_attempt` | `int` | `0` | BeforeTurnNode (`+= 1`) | AfterTurnNode | Current turn attempt number (1=first, 2=first continuation, etc.) |

**`TurnCustomKey` additions**:

| Key | Writer | Reader | Purpose |
|---|---|---|---|
| `GRAPH_DELIVER_COUNT` | `GraphDeliverTool.execute()` | `DeliverRetryHook` | Deliver call count in this turn |
| `CONTINUATION_REQUEST` | `DeliverRetryHook` (AfterTurnHook) | `AfterTurnNode` | Boolean-only flag; presence = "continue", value not read. Reminder text is injected into history by the hook as a side-effect. |
| `MAX_TURNS` | `BotAgentNode.execute()` | `AfterTurnNode` | Hard cap; default 3 (3 total turn attempts) |

### Approval impact analysis (user's concern)

**Approval is NOT affected.** Verified against the code:

| Scenario | Current path | New path | Impact |
|---|---|---|---|
| Normal execution | START → LLM → TOOL → LLM → END | START → BEFORE → LLM → TOOL → LLM → AFTER → END | AFTER_TURN fires in AfterTurnNode instead of `run()`. Same result. |
| Approval suspend | TOOL → `ctx.interrupt(tx)` → GraphInterrupt → graph exits | Same (interrupt happens in TOOL, before AFTER) | AfterTurnNode never runs. AFTER_TURN doesn't fire (correct — turn is suspended, not ended). FINALLY_TURN fires in `run()`'s finally. **No change.** |
| Approval resume | START → `resume_target=TOOL` → TOOL → LLM → END | START → TOOL (bypasses BEFORE) → LLM → AFTER → END | BEFORE bypassed on resume (correct — no iteration reset). AFTER_TURN fires after resume completes. **No change.** |
| Deny + CANCEL_TURN | TOOL → END (cancelled) | TOOL → AFTER → END (cancelled) | AFTER_TURN now fires on cancellation (improvement — `TurnOutcomeNotifyHook` can notify cancellation). AfterTurnNode checks `phase == CANCELLED`, skips continuation. **No regression; slight improvement.** |
| Max iterations | LLM → END | LLM → AFTER → END | AFTER_TURN fires. AfterTurnNode sees result, no continuation flag set (hook checks deliver count, not max iterations). **No change.** |

**Why approval is safe**:
1. `ctx.interrupt(tx)` raises `GraphInterrupt` in ToolNode — graph exits before reaching AFTER. AfterTurnNode never runs.
2. On resume, `StartNode` routes to `state.resume_target` (TOOL), bypassing BEFORE. `turn_attempt` is NOT incremented on resume (correct — it's the same turn attempt).
3. After TOOL completes on resume, it routes to LLM (normal) or AFTER (cancellation). LLM eventually routes to AFTER. AFTER_TURN fires once, continuation check happens.
4. The `TOOL → END` cancellation paths (`dedup_stop`, `CANCEL_TURN`, `no llm_response`) now go `TOOL → AFTER → END`. AfterTurnNode constructs the cancelled result, dispatches AFTER_TURN, sees `phase == CANCELLED`, skips continuation, routes to END. **This is an improvement**: existing hooks like `TurnOutcomeNotifyHook` now fire on cancellation paths that previously bypassed AFTER_TURN.

### Edge changes summary

```
# Current edges
START → LLM          # fresh start
START → TOOL         # approval resume
LLM   → TOOL         # has tool calls
LLM   → END          # stop / error / max_iter
TOOL  → LLM          # normal (tools executed, back to LLM)
TOOL  → END          # cancellation (dedup_stop, CANCEL_TURN, no llm_response)
END   → GraphNode.END

# New edges
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

### `BEFORE_TURN` / `AFTER_TURN` dispatch relocation

Currently dispatched in `ReActAgent.run()`'s `actual_turn()` closure:
```python
async def actual_turn():
    await ctx.runtime.dispatch_hook(BEFORE_TURN, ctx)
    await engine.run_async(graph_ctx)
    await ctx.runtime.dispatch_hook(AFTER_TURN, ctx, result)
```

New: dispatched inside graph nodes:
- `BeforeTurnNode.execute()`: dispatches `BEFORE_TURN`
- `AfterTurnNode.execute()`: dispatches `AFTER_TURN` (with constructed result)

`actual_turn()` in `run()` simplifies to:
```python
async def actual_turn():
    await engine.run_async(graph_ctx)
    # BEFORE_TURN and AFTER_TURN now fire inside the graph
```

**FINALLY_TURN stays in `run()`'s `finally` block** — fires once per logical turn (once per `run()` call), not per turn attempt. This is correct: FINALLY_TURN is cleanup (SubagentAutoSendHook, TraceCollectorHook), should fire once.

### What changes for existing hooks

| Hook | Current | New | Impact |
|---|---|---|---|
| `BEFORE_TURN` | Fires once in `run()` | Fires in BeforeTurnNode (per turn attempt) | Fires per attempt (continuation = multiple). Default hooks (RuntimeContextHook, InboxFlushHook, ModelChoiceBindHook) run per attempt — semantically correct (each attempt is a fresh turn). |
| `AFTER_TURN` | Fires once in `run()` | Fires in AfterTurnNode (per turn attempt) | Fires per attempt. Hooks receive constructed result. `TurnOutcomeNotifyHook` can now observe cancellation paths. |
| `FINALLY_TURN` | Fires once in `run()`'s finally | **Unchanged** — fires once in `run()`'s finally | No change. |
| `BEFORE_ITERATION` / `AFTER_ITERATION` | Fires in LLMNode | **Unchanged** | No change. |

### Comparison with alternatives

| Approach | Loop location | EndNode complexity | AFTER_TURN on cancel | Uses graph engine |
|---|---|---|---|---|
| BotAgentNode for-loop (current) | Graph caller | unchanged | yes (in `run()`) | no |
| Interceptor `around_turn` loop | Interceptor chain | unchanged | yes (in `run()`) | no |
| `ReActAgent.run()` loop | Agent run() | unchanged | yes (in `run()`) | no |
| EndNode checks continuation | — | **increased** | yes | yes |
| **BeforeTurnNode + AfterTurnNode** | **Graph topology** | **simplified** (result construction moved to AFTER) | **yes (in AfterTurnNode)** | **yes** |

### Open issues

1. **BEFORE_TURN fires per turn attempt** — this is a behavior change. `InboxFlushHook` (drains inbox before each iteration) is also `BeforeIterationHook`, so mid-turn inbox draining is unaffected. `RuntimeContextHook` resets context per turn — per-attempt reset is semantically correct for continuation. `ModelChoiceBindHook` re-binds model choice — per-attempt is correct. **All verified safe.**

2. **`max_iterations` interaction** — `state.iteration` resets to 0 in BeforeTurnNode. Each turn attempt gets a fresh iteration budget. `compile(max_iterations=N)` engine safety net no longer applies — the engine default is unlimited and the `LLMNode` business gate is the sole iteration cap (no compile-time formula to keep in sync with runtime `MAX_TURNS` renewal).

3. **Graph cycle detection** — `build_react_graph()` uses `cycle_detection="warn"`. The `AFTER → BEFORE` edge creates a cycle (same category as `TOOL → LLM`). Warning is expected and acceptable.

4. **`StartNode` routing change** — fresh start routes to `BEFORE` instead of `LLM`. Resume still routes to `state.resume_target` (TOOL). This is the only change to StartNode.

## Flip conditions

1. **If BEFORE_TURN per-attempt firing breaks existing hooks** — `InboxFlushHook` is also `BeforeIterationHook` (mid-turn draining unaffected), `RuntimeContextHook` resets per turn (correct for continuation), `ModelChoiceBindHook` re-binds (correct). All verified safe, but real usage may reveal edge cases. If breakage occurs, BEFORE_TURN can stay in `run()` and BeforeTurnNode only does mechanics.

2. **If AFTER_TURN per-attempt firing is undesired** — currently fires once per `run()`. With continuation, fires per turn attempt. If some hooks (e.g. `TrainingDataHook`) should fire once per logical turn (not per attempt), they should move to FINALLY_TURN, or a new `AFTER_LOGICAL_TURN` hook point. For now, per-attempt is semantically correct (each attempt is a turn).

3. **If the preliminary result in AfterTurnNode is insufficient** — AfterTurnNode constructs `AgentResult` from `state.llm_response` before EndNode runs. If hooks need information only available after EndNode (e.g. completion events), they won't have it. Current `AfterTurnHook` implementations only read `result.stop_reason`, `result.error`, `result.content`, `result.messages` — all available from state. Verified safe.
