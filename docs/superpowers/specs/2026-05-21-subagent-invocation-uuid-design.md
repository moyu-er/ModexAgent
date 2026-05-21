# Subagent Invocation UUID — Multi-Invocation Context Isolation

> 2026-05-21 | Incremental design — builds on `2026-05-21-subagent-refactoring-design.md`

## 1. Relationship to Base Spec

This spec is an **incremental extension** of
`docs/superpowers/specs/2026-05-21-subagent-refactoring-design.md` (the "base
spec"). The base spec covers:

- Deleting `SubagentManager`, unifying `peer` → `subagent`
- `SubagentService` with `register_resident`, `admit_dynamic`, `create_and_wait`
- `AgentPool` session tracking, TTL cleanup, sync-futures
- Bot tool exposure policy: async-only, `send_message_async` for task delegation

**What this spec adds (does NOT remove or contradict the base spec):**

1. **Two distinct communication tools**: `dispatch_task` (new) and `send_message_async` (modified)
2. **`invocation_id` UUID**: bridges context isolation, request-result correlation, and follow-up message routing
3. **Optional invocation_id on follow-up messages**: agent can target a specific invocation session or the default session
4. **Multi-invocation concurrency**: same subagent can handle multiple independent tasks in parallel, each in its own isolated session

**What this spec changes from the base spec:**

| Base spec | This spec |
|-----------|-----------|
| `send_message_async(message_type="task_request")` initiates tasks | `dispatch_task` initiates tasks; `send_message_async` sends messages only |
| No UUID concept | `invocation_id` UUID on task dispatch and optional on follow-up messages |
| `SendMessageAsyncTool` one tool for both | Two tools: `DispatchTaskTool` + `SendMessageAsyncTool` |
| Bot tools table (spec §3.1) had `send_message_async` doing double duty | Updated table below (§5) |

Everything else from the base spec — `SubagentService`, `SessionRetentionPolicy`, TTL cleanup,
`subagent_session_isolated()`, star topology, ACL — remains unchanged.

---

## 2. Problem

When the same subagent is called multiple times by the same parent agent,
each call may need a **clean, isolated context**. Without isolation:

```
main → send_message_async(office, "review file A")  → session: conv:office
main → send_message_async(office, "review file B")  → session: conv:office  ← same session!
```

The subagent sees both tasks in the same history. File B's review leaks into
File A's review. Concurrent tasks on the same subagent share and pollute each
other's context.

The framework already provides all the primitives needed for isolation
(`SessionScope` memory, per-session locks, TTL cleanup). What's missing is
a **task-initiation mechanism** that creates a new session per invocation and
gives the agent a **handle (UUID)** to route follow-up messages to the right
session.

---

## 3. Design: Two Communication Modes

```
┌──────────────────────────────────────────────────────────────────┐
│  dispatch_task                                                   │
│  ─────────────                                                   │
│  Start a new task → creates invocation_id → new isolated session │
│  session = {conv}:{target}:{inv_id}                              │
│  Returns: "invocation_id: inv_xxxxxxxxxxxx"                      │
│  Clean context. Multiple invocations run in parallel.            │
├──────────────────────────────────────────────────────────────────┤
│  send_message_async                                              │
│  ──────────────────                                              │
│  Send a message (optional invocation_id)                         │
│  With UUID:    routes to that specific invocation session        │
│  Without UUID: routes to the default session                     │
│  For follow-ups, progress updates, final results.                │
└──────────────────────────────────────────────────────────────────┘
```

### 3.1 `invocation_id` UUID — triple purpose

A single UUID serves three roles simultaneously:

| Role | Description |
|------|-------------|
| **Context isolation** | Appended to `session_id`: `conv:office:inv_abc`. Each unique session_id gets isolated memory via `SessionScope`. |
| **Request-result correlation** | Stored as `correlation_id` on the `AgentMessageEnvelope`. Parent agent can match incoming results to the requests they originated from. |
| **Follow-up routing key** | Passed as `invocation_id` parameter on subsequent `send_message_async` calls. Ensures follow-up messages land in the right session. |

### 3.2 UUID is optional — graceful degradation

Not every subagent interaction is a task dispatch. The `invocation_id` field is
**always optional** — agents that don't need or know about it simply don't pass
it. When absent:

- `send_message_async` routes to the default session (same behavior as today)
- The subagent receives a message without `invocation_id` and responds to the
  default session
- This preserves backward compatibility for simple message-passing use cases

### 3.3 UUID origin and lifecycle

```
dispatch_task(target, task_prompt)
  → tool generates invocation_id = "inv_" + uuid4().hex[:12]
  → stored on envelope.payload["invocation_id"]
  → stored as envelope.correlation_id
  → appended to session_id
  → returned to caller in tool response string

subagent receives task_request with payload.invocation_id
  → uses it for session isolation (handled by framework)
  → can send results via send_message_async(invocation_id=received_id)

subagent receives agent_message with no invocation_id
  → routes to default session (existing behavior)
  → no UUID needed for response
```

---

## 4. Data Model Changes

### 4.1 `AgentMessageEnvelope.payload` — new field

```python
# Envelope payload (dict — framework does not constrain fields)
{
    "content": "...",
    "message_type": "task_request" | "agent_message",
    "invocation_id": "inv_xxxxxxxxxxxx" | None   # ← NEW, always optional
}
```

### 4.2 Session ID construction

```python
# In SendMessageAsyncTool / DispatchTaskTool:

def _build_session_id(self, conversation_id, target_agent, invocation_id=None):
    base = f"{conversation_id}:{target_agent}"
    if invocation_id:
        return f"{base}:{invocation_id}"
    return base

# With invocation_id:    "conv_001:office:inv_a1b2c3d4e5f6"
# Without invocation_id: "conv_001:office"
```

### 4.3 Subagent receives messages — two cases

**Case A: task_request with `invocation_id`**

```python
envelope.payload = {
    "task_prompt": "review file A",
    "message_type": "task_request",
    "invocation_id": "inv_a1b2c3d4e5f6"
}
envelope.agent_session_id = "conv_001:office:inv_a1b2c3d4e5f6"
# → isolated session, clean context
# → subagent should use invocation_id when replying
```

**Case B: agent_message without `invocation_id`**

```python
envelope.payload = {
    "content": "hello",
    "message_type": "agent_message",
    # no invocation_id
}
envelope.agent_session_id = "conv_001:office"
# → default session
# → subagent replies without invocation_id
```

---

## 5. Tool Exposure Policy (updated from base spec §3.1)

| Tool | Main agent | Subagent | Notes |
|------|-----------|----------|-------|
| `dispatch_task` | ✅ | ❌ | Creates invocation_id. Subagent cannot spawn sub-subagents. |
| `send_message_async` | ✅ | ✅ (parent only) | Optional `invocation_id` parameter on both sides. |
| `DelegateTaskTool` (sync) | ❌ | ❌ | Framework-only, not exposed to LLM. |
| `CreateSubagentTool` | ✅ (opt-in) | ❌ | Dynamic creation, config-gated. |

---

## 6. Framework Changes

### 6.1 `DispatchTaskTool` — new tool

```python
# framework/multi_agent/tools.py — NEW class

class DispatchTaskTool(Tool):
    """Dispatch a new task to a subagent. Creates an isolated invocation session
    and returns an invocation_id for follow-up communication.

    Use this when:
    - Starting a new, independent task for a subagent
    - You need clean context (no history leakage from other tasks)
    - You want to run multiple tasks on the same subagent in parallel

    The returned invocation_id can be passed to send_message_async for
    follow-up messages targeting this specific task session.
    """

    def __init__(self, broker, self_address, *, allowed_targets=None,
                 agent_bus=None, registry=None, session_strategy=None):
        # Same dependencies as SendMessageAsyncTool

    async def execute(self, target_agent, task_prompt, context=None):
        # 1. ACL check
        # 2. Registry existence check
        # 3. Generate invocation_id
        inv_id = f"inv_{uuid4().hex[:12]}"
        # 4. Build session_id with invocation suffix
        # 5. Build task_request envelope with invocation_id in payload
        # 6. Send via broker
        # 7. Return invocation_id to caller
```

Parameters (LLM-facing):
- `target_agent` (required): name of the subagent
- `task_prompt` (required): the task description
- `context` (optional): additional context or background information

Returns: `"Task dispatched to {target}. invocation_id: {inv_id}"`

### 6.2 `SendMessageAsyncTool` — modified

New optional parameter:

```python
class SendMessageAsyncTool(Tool):
    # Existing parameters: target_agent, content, conversation_id, message_type
    # NEW parameter:
    #   invocation_id: str | None  — route to a specific invocation session

    async def execute(self, target_agent, content, *,
                      conversation_id="", message_type="agent_message",
                      invocation_id=None):
        # Build session_id with optional invocation suffix
        # Build envelope payload with optional invocation_id field
        # Send via agent_bus as before
```

When `invocation_id` is provided:
- `session_id` is built with the `:{inv_id}` suffix
- `payload["invocation_id"]` is set on the envelope
- The message routes to the specific invocation session

When `invocation_id` is absent:
- `session_id` is the base form (no suffix)
- No `invocation_id` in the payload
- The message routes to the default session (existing behavior)

### 6.3 No changes to other framework components

| Component | Change? |
|-----------|---------|
| `AgentPool` | None — already handles per-session locks, tracking, TTL cleanup |
| `SubagentService` | None — `admit_dynamic` already generates session_ids |
| `MemoryLayerFactory` | None — `SessionScope` already isolates by session_id |
| `AgentMessageEnvelope` | None — payload is a free-form dict |
| `DefaultSessionIdStrategy` | None — session_id format is handled at tool level |

---

## 7. Bot Business Layer Changes

### 7.1 Tool registration in `builders.py`

```python
# Main agent — _register_multi_agent_tools()

# 1. Task dispatch → always creates invocation session
tool_manager.register(DispatchTaskTool(
    broker=broker, self_address=main_address,
    allowed_targets=subagent_names, agent_bus=agent_bus,
    registry=self.agent_pool, session_strategy=strategy,
))

# 2. Message sending → optional invocation_id
tool_manager.register(SendMessageAsyncTool(
    broker=broker, self_address=main_address,
    allowed_targets=subagent_names, agent_bus=agent_bus,
    registry=self.agent_pool, session_strategy=strategy,
))
```

```python
# Each subagent — _initialize_subagent_agents()

tool_manager.register(SendMessageAsyncTool(
    broker=self.broker, self_address=subagent_address,
    allowed_targets=[parent_name], agent_bus=self.agent_bus,
    registry=self.agent_pool, session_strategy=strategy,
))
# subagent does NOT get DispatchTaskTool
```

### 7.2 System prompt guidance

```
## 调用 Subagent

当需要让 subagent 执行独立任务时，使用 dispatch_task：
- dispatch_task 会创建一个隔离的会话，返回 invocation_id
- 同一个 subagent 可以同时处理多个独立任务（每个有自己的 invocation_id）
- 后续跟进消息时，用 send_message_async 并传入 invocation_id
- 不要把不同任务的内容混在一个会话里

## 回复 Subagent

当你收到带有 invocation_id 的任务时：
- 你的会话是隔离的，看不到其他任务的历史
- 用 send_message_async(invocation_id="{收到的UUID}", content="...") 汇报结果
- 不要遗漏 invocation_id，否则会发到默认会话
```

### 7.3 Config

No new config keys. The `dispatch_task` tool is registered unconditionally for
main agents (no feature flag needed — it's the standard way to delegate work).
`CreateSubagentTool` remains config-gated for dynamic subagent creation.

---

## 8. Implementation Phases

This design is implemented as an **extension** of the base spec's Phase 5
(bot additions). The changes are:

| Phase | Task | Priority |
|-------|------|----------|
| 5a | Add `DispatchTaskTool` to `framework/multi_agent/tools.py` | Required |
| 5b | Add `invocation_id` parameter to `SendMessageAsyncTool.execute()` | Required |
| 5c | Register `DispatchTaskTool` in bot `builders.py` (main agent only) | Required |
| 5d | Update bot system prompts for invocation_id usage | Required |
| 5e | Add unit tests for `DispatchTaskTool` | Required |
| 5f | Add unit tests for `invocation_id` session routing | Required |

---

## 9. Verification

- [ ] `dispatch_task` creates isolated session (different invocations → different session_ids)
- [ ] `dispatch_task` returns `invocation_id` in tool response
- [ ] `invocation_id` is present in envelope payload and as `correlation_id`
- [ ] `send_message_async(invocation_id=uuid)` routes to the correct session
- [ ] `send_message_async()` without invocation_id routes to default session (backward compat)
- [ ] Multiple concurrent invocations of same subagent do not share context
- [ ] Subagent cannot use `dispatch_task` (tool not registered)
- [ ] TTL cleanup evicts stale invocation sessions
- [ ] Framework tests pass | Bot integration tests pass | Type check passes
