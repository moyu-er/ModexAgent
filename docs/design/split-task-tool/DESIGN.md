# Design: Split `task` Tool Out of `send_to_agent`

> **Update (2026-08-20):** `task` gained an `invocation_id` continue-mode,
> superseding decision #4 ("No `invocation_id` on task"). Continuation timing
> is now notification-driven: every result notification from
> `SubagentAutoSendHook` ends with a state-conditional guidance paragraph
> (complete / deliverable-lost / judge / continue states) stating whether the
> task is complete and what to do next. Send acks are plain prose (no
> pseudo-structure lines), consultation messages (`ParentReplyStrategy`)
> carry an explicit `task` answer contract, and `_SubagentDispatchSubProvider`
> was deleted (its content was fully covered by `TaskDispatchTool.description`).

> **Update (2026-08-13):** Peer communication has been split out of `task` into
> a dedicated `send_to_peer` tool. `task` is now strictly subagent-scoped. See
> ADR-0019 §4 for the tool-surface split design. The original design below is
> preserved as historical context for the initial `send_to_agent` → `task`
> split.

> Branch: `feature/split-task-tool`
> Worktree: `F:/tool/pythonProject/ModexAgent-task-tool`
> Base: `develop_gyt` @ `ad0c5331`
> Date: 2026-08-01

## 1. Problem

Subagent dispatch quality is weak because `send_to_agent` is a single tool that
serves three unrelated purposes (new task dispatch, session continuation,
peer/consultation messaging). Its `content` parameter description is one line
("Complete task description with necessary context") and the system prompt
section (`_SubagentDispatchSubProvider`) only explains the mechanism
(`invocation_id` semantics), not how to construct a good task prompt.

The LLM never receives guidance on what makes a quality subagent prompt
(concrete objective, context, scope, expected output, verification, boundaries),
so it sends one-liners like "fix the bug".

## 2. Solution

Split a dedicated `task` tool out of `send_to_agent`. Both converge on the
same `AgentCommunicationService.send_async()` — the service layer, strategies,
and topology policy are **not modified**.

### 2.1 Tool Division

| Tool | Purpose | Direction | Registered for |
|------|---------|-----------|----------------|
| `task` (new) | Dispatch **new** subagent tasks with prompt-construction guidance | main → subagent | normal agents only |
| `send_to_agent` (existing, slimmed) | Continue existing sessions, consult parent, peer communication | all directions | all agents |

### 2.2 Convergence

```
task.execute(target_agent, content)
  └─ service.send_async(target, content, invocation_id=None, context)
       └─ SubagentDispatchStrategy.execute()           ← UNCHANGED
            ├─ build_envelope() → markdown message  ← UNCHANGED
            ├─ deliver via bus                          ← UNCHANGED
            └─ return AgentSendResult → format_send_ack ← UNCHANGED

send_to_agent.execute(target_agent, content, invocation_id)
  └─ service.send_async(target, content, invocation_id, context)
       └─ SubagentDispatchStrategy / ParentReplyStrategy / PeerNormalStrategy
                                                       ← UNCHANGED
```

Both call `AgentCommunicationService.send_async()` with the same signature.
`task` always passes `invocation_id=None` (new session only).

### 2.3 Subagent Reply Path (unchanged)

`SubagentAutoSendHook` fires on `FINALLY_TURN` and sends the result back via
the bus. It does not know or care which tool dispatched the task — the envelope
is identical. No changes to the hook.

## 3. Tool Specifications

### 3.1 `task` Tool

**Parameters:**

```python
_TASK_PARAMS = {
    "type": "object",
    "properties": {
        "target_agent": {
            "type": "string",
            "description": (
                "REQUIRED: exact name of the target subagent. "
                "MUST be one of the names listed under 'Available subagents:' "
                "in the tool description."
            ),
        },
        "content": {
            "type": "string",
            "description": (
                "Complete, self-contained task description. The subagent starts "
                "with a fresh context — it cannot see your conversation, "
                "reasoning, or tool results. Include: concrete objective, "
                "relevant context (file paths, constraints), scope (code or "
                "research), expected output, verification method, and boundaries."
            ),
        },
    },
    "required": ["target_agent", "content"],
}
```

- No `invocation_id` — structurally cannot continue sessions.
- `target_agent` and `content` naming matches `send_to_agent` for consistency.

**Description** (dynamic, built from store — only SUBAGENT targets):

```
Dispatch a new task to a subagent. The subagent starts with a fresh context
and runs autonomously — it cannot see your conversation, reasoning, or prior
tool results. Everything it needs must be in `content`.

When NOT to use this tool:
- If you want to read a specific file, use the read tool directly — it's faster
- If you are searching for a specific pattern, use grep or glob directly
- If no available subagent is a good fit for the task, do it yourself

When to use:
- Complex, multi-step tasks that need autonomous execution
- Tasks that require a specialized subagent's tools or knowledge

Usage notes:
1. Launch multiple tasks concurrently when they are independent — use multiple
   tool calls in a single message.
2. Once you delegate work to a subagent, do not duplicate that work yourself.
   Continue with non-overlapping tasks, or end your turn and wait for the result.
3. The subagent's result is returned to you only — relay a concise summary to
   the user if needed.
4. Construct a high-quality task with:
   - TASK: What exactly to do (concrete objective, not a topic)
   - CONTEXT: Relevant file paths, patterns, constraints
   - SCOPE: Write code or just research (search/read/analyze)
   - OUTPUT: Exactly what to return in the final reply
   - VERIFICATION: How to verify (e.g., test commands)
   - BOUNDARIES: What NOT to do, out-of-scope items
5. The subagent's output should generally be trusted.

A one-line task like "fix the bug" is insufficient — the subagent's result
quality is directly proportional to your prompt quality.

After dispatching, end your turn. You'll be resumed with the result when
the subagent finishes. To CONTINUE an existing session, use send_to_agent
with the invocation_id from a prior task result.

Available subagents (use the exact name as target_agent):
  - <name>: <full description>
  ...
```

- No normal/subagent target-type explanation (unlike send_to_agent).
- "When NOT to use" prevents LLM from using task instead of simple tools.
- "Launch concurrently" + "do not duplicate" prevents serial bottlenecking.
- Lists only `AgentCommKind.SUBAGENT` targets with full description.
- `get_dynamic_schema()`: `target_agent` enum = only SUBAGENT target names.

**execute():**

1. Resolve `target_agent` from `CommunicationTargetStore.get()`.
2. Validate `target.kind == AgentCommKind.SUBAGENT` (reject NORMAL targets).
3. Resolve caller context via `current_agent_context`.
4. Reject self-dispatch (caller == target).
5. Call `self._service.send_async(target, content=content, invocation_id=None, context)`.
6. Return ack text (`format_send_ack` result).

**Constructor:** minimal deps — `store`, `service`. (Does not need
`broker`/`registry`/`agent_bus` directly — only calls `service.send_async()`.)

### 3.2 `send_to_agent` Tool (slimmed)

**`_NORMAL_PARAMS.content` description** — change from:
> "Complete task description with necessary context."

to:
> "Message content — a continuation of an existing subagent session, a message to a peer, or a consultation. Not for dispatching new subagent tasks (use the `task` tool)."

**`_build_normal()`** — restructure:

- Keep the base instruction ("Communicate with another agent — your ONLY
  channel for messaging, continuation, and peer coordination.").
- Remove the subagent delegation guidance block ("Delegate a self-contained
  subtask; you direct it, it runs on your behalf...").
- Keep continuation guidance (`invocation_id` threading).
- Keep peer communication guidance (NORMAL targets, equal communication).
- Add a pointer: "For dispatching a NEW subagent task, use the `task` tool."
- **Target list**: still lists all targets (NORMAL + SUBAGENT), but SUBAGENT
  descriptions are **truncated to ~40 characters** with `...` if longer.
  NORMAL targets keep full descriptions.

```
Communicate with another agent — your ONLY channel for messaging,
continuation, and peer coordination.

Only `content` reaches the target; your reasoning, tool calls, and
this reply stay local. Sends are asynchronous — end your turn after;
the response comes back on its own. Don't send just to acknowledge.

Use this tool for:
  - Continuing an existing subagent session (pass invocation_id).
  - Messaging a peer agent (normal target) as an equal.
  - Replying to a remote agent that sent you a message.

For dispatching a NEW subagent task, use the `task` tool instead.

Available targets (use the exact name as target_agent):

Subagents (for continuing sessions; use the `task` tool for new tasks):
  - coder: Executes delegated implementation tas...
  - explore: Contextual grep for codebases

Peer targets (for messaging and coordination):
  - team-alpha: Another team agent for cross-team work
  - team-beta: ...
```

**Truncation**: a helper function truncates description to N chars (default 40)
and appends `...` if truncated. Applied only to SUBAGENT target descriptions in
`_build_normal()`. NORMAL targets and `_build_subagent()` are unchanged.

**`_SUBAGENT_PARAMS` and `_build_subagent()`** — unchanged (subagent mode
doesn't list targets, resolves parent dynamically).

**`get_dynamic_schema()`** — unchanged (enum still includes all target names,
since `send_to_agent` is still used for continuation with any subagent).

## 4. System Prompt Provider Changes

### 4.1 `_SubagentDispatchSubProvider` (modified)

**`applies()`**: `comm_kind != SUBAGENT` AND `task` tool exists in
`tool_manager`. No fallback to `send_to_agent` — if `task` is not registered,
this provider does not fire.

**`_subagent_target_names()`**: look up `get_tool("task")` instead of
`get_tool("send_to_agent")`. Cast to `TaskDispatchTool`.

**`content()`**:

```
## Dispatching Subagents

Subagents cannot see anything you output directly. To assign a NEW task,
use the `task` tool — its `content` parameter carries the full task
description, and the tool guides you to construct a high-quality prompt.

To CONTINUE an existing subagent session (e.g. after receiving a
NEED_DECISION response), use `send_to_agent` with the `invocation_id`
from the prior task result.

After dispatching, end your turn — the notification resumes you with the
result when the subagent finishes.

Subagents surface structured prefixes in their delivered result:
- `NEED_DECISION: <question>` — needs your decision. Continue the session
  (send_to_agent with same invocation_id) with your answer.
- `PROGRESS_UPDATE: <info>` — informational, no action needed.
```

### 4.2 `_PeerCommSubProvider` (unchanged)

Still references `send_to_agent` for peer reply contract.

### 4.3 `_SubagentConsultationSubProvider` (unchanged)

Still references `send_to_agent` for subagent→parent consultation.

### 4.4 `AgentCommunicationSystemPromptProvider.__init__` (unchanged signature)

Already takes `tool_manager` + `comm_kind`. The dispatch sub-provider looks up
`get_tool("task")` at call time.

## 5. Registration

### 5.1 Main Agent (pool_builder.py)

Inside `if strategy.requires_main_agent_tools:` block, after
`SendToAgentTool` registration, add `TaskDispatchTool` registration:

```python
tool_manager.register(
    TaskDispatchTool(store=main_store, service=main_service)
)
```

Both tools share the same `CommunicationTargetStore` instance.

### 5.2 Subagent (template.py) — unchanged

`AgentTemplate._build_tool_manager` does NOT register `task`. Structural
exclusion — subagents can never acquire the `task` tool.

### 5.3 External Agents — unchanged

`requires_main_agent_tools=False` skips the entire block. External agents
use `modexctl send` CLI.

### 5.4 Parity Helper (pool_builder.py)

`build_main_agent_tool_names`: add `"task"` alongside `"send_to_agent"`.

## 6. Description Truncation Helper

A shared utility for shortening subagent descriptions in `send_to_agent`:

```python
_SUBAGENT_DESC_LIMIT = 40

def _truncate_desc(desc: str, limit: int = _SUBAGENT_DESC_LIMIT) -> str:
    """Truncate description to limit chars, appending '...' if truncated."""
    if len(desc) <= limit:
        return desc
    return desc[:limit].rstrip() + "..."
```

Located in `tools.py` (module-level function). Used only in
`CommunicationTargetStore._build_normal()`.

## 7. Trace Handoff Span (trace/hooks.py)

Add `"task"` to the dispatch tool exclusion set alongside `"send_to_agent"`:

```python
_DISPATCH_TOOL_NAMES = frozenset({"send_to_agent", "task"})
```

Both are dispatch tools that create their own sessions — they should not
trigger regular handoff spans.

## 8. Package Exports (__init__.py)

Add `TaskDispatchTool` to:
- `src/modex_agent/multi_agent/__init__.py` imports and `__all__`

## 9. Validator (subagent_validator.py)

No code change. The `task` tool is excluded structurally (never registered
in `template.py`). Add a comment documenting this:

```python
# Note: the `task` tool is main-agent-only by registration.
# Subagents are built via AgentTemplate._build_tool_manager, which does
# not register it. No denied_tools check needed.
```

## 10. Unchanged Components

| Component | Why unchanged |
|-----------|---------------|
| `AgentCommunicationService` | Convergence point — both tools call `send_async()` |
| `SubagentDispatchStrategy` | Handles dispatch regardless of which tool called |
| `ParentReplyStrategy` | Subagent→parent, unrelated to task tool |
| `PeerNormalStrategy` | Cross-pool peer, unrelated to task tool |
| `TopologyPolicy` | Star topology enforcement, unrelated |
| `CommunicationTargetStore` | Shared store, both tools read from it |
| `SubagentAutoSendHook` | Fires on FINALLY_TURN, envelope-agnostic |
| `_PeerCommSubProvider` | Peer reply contract, references send_to_agent |
| `_SubagentConsultationSubProvider` | Consultation contract, references send_to_agent |
| `_SUBAGENT_PARAMS` | Subagent mode params, unchanged |
| `_build_subagent()` | Subagent mode description, unchanged |
| `SendToAgentTool.execute()` | Dispatch logic unchanged |
| `SendToAgentTool.get_dynamic_schema()` | Enum includes all targets (for continuation) |
| `message_format.py` / `build_dispatch_message()` | markdown message, shared by both tools |
| `envelope.py` / `AgentMessageEnvelope` | Envelope structure, shared |

## 11. Design Decisions Log

| # | Decision | Rationale |
|---|----------|-----------|
| 1 | Split task tool (not strengthen send_to_agent) | Mixing lightweight messaging with heavy task-construction guidance in one tool creates confusing descriptions and contradictory parameter semantics |
| 2 | Both tools converge on `send_async()` | Respects convergence rule — no third dispatch path |
| 3 | task params: `target_agent` + `content` (no `description`/`prompt`/`subagent_type`) | Consistent with send_to_agent naming; opencode's params don't fit ModexAgent's context |
| 4 | No `invocation_id` on task | Structural enforcement — task cannot continue sessions |
| 5 | task only lists SUBAGENT targets | Normal targets are peers, not task dispatch targets |
| 6 | send_to_agent subagent desc truncated to ~40 chars | Avoids duplication with task's full descriptions; no runtime check for task existence |
| 7 | task is default config for normal agents (no existence check) | Simpler, no coupling between send_to_agent and task tool |
| 8 | `_SubagentDispatchSubProvider.applies()` checks task tool existence, no fallback | If task isn't registered, the provider shouldn't fire — no half-measures |
| 9 | `description` param NOT in envelope | Matches send_to_agent behavior — no extra metadata passed to service |
| 10 | Trace handoff excludes both `send_to_agent` and `task` | Both are dispatch tools with own session creation |
| 11 | task description includes "When NOT to use" + concurrency guidance | Learned from opencode's task.txt — prevents LLM from using task instead of simple tools (read/grep/glob), encourages parallel dispatch, prevents work duplication |
| 12 | task description includes "result returned to you only" reminder | Subagent results are not visible to the user; LLM must relay summaries. Learned from opencode task.txt Usage #3 |
