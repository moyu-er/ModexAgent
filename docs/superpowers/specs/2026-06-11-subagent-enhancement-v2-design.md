# Subagent Enhancement v2 — Design Spec

**Date**: 2026-06-11
**Status**: Draft
**Branch**: `develop_gyt`

## Overview

Enhance the subagent mechanism in ModexAgent with:
1. Guaranteed lifecycle hooks (crash-safe result notification)
2. Unified operation-level trace system for all agents
3. `output.md` protocol as the authoritative subagent result
4. Rewritten `SubagentAutoSendHook` (no communication tool dependency)
5. LRU `SubagentPool` for instance reuse

**Non-goal**: Fork context optimization (deferred).

---

## A. Hook Lifecycle — `FINALLY_TURN`

### Problem

`ReActAgent.run()` fires `AFTER_TURN` hooks only in the success path (`actual_turn()`). When a subagent crashes (exception in graph execution), `AFTER_TURN` never fires → `SubagentAutoSendHook` never notifies the parent → parent waits indefinitely.

### Design

| Item | Detail |
|------|--------|
| New hook point | `HookPoint.FINALLY_TURN = "finally_turn"` |
| New hook ABC | `FinallyTurnHook(Hook[R])` with `async def finally_turn(self, ctx, result: AgentResult \| None) -> None` |
| Trigger point | `ReActAgent.run()` `finally` block, **always** fires regardless of exit path |
| Payload | `HookPayload(data={"result": result})` — `result` may be `None` on early abort |

### Migration

`SubagentAutoSendHook` moves from `AfterTurnHook` to `FinallyTurnHook`.

### Files

| File | Change |
|------|--------|
| `framework/hook/abc.py` | Add `FinallyTurnHook` ABC, `HookPoint.FINALLY_TURN` |
| `framework/hook/runner.py` | Add `finally_turn` dispatch branch |
| `framework/agents/react/agent.py` | Replace TODO comments in `finally` with hook dispatch |
| `framework/hook/builtin/subagent_auto_send.py` | Change base class to `FinallyTurnHook` |

---

## B. Unified Trace System

### Design

Operation-level trace, stored per-session, JSON Lines format, hook-driven collection.

### Types (`framework/trace/types.py`)

```python
@dataclass
class OperationRecord:
    trace_id: str           # Globally unique
    session_id: str         # {conv}:{agent}[:{invocation}]
    agent_name: str
    invocation_id: str | None
    kind: OperationKind     # TURN_START / TURN_END / LLM_CALL / TOOL_BATCH / TOOL_CALL / APPROVAL / ERROR
    status: OperationStatus # CREATED / COMPLETED / FAILED
    timestamp: float        # Unix timestamp
    duration_ms: int | None # Null for start markers
    metadata: dict[str, Any]  # model, tool_name, token_count, error, etc.
    error: str | None
```

### Storage (`framework/trace/store.py`)

| Item | Detail |
|------|--------|
| ABC | `TraceStore` — `save(record)`, `list_by_session(session_id)`, `list_by_trace_id(trace_id)` |
| Implementation | `JsonFileTraceStore` |
| Path | `data/runtime_state/{pool_name}/trace/{session_id}/operations.jsonl` |
| Write | Append-only, one JSON record per line |
| Read | `list_by_session(session_id)`, `list_by_trace_id(trace_id)` |
JsonFileTraceStore is an implement of trace storage ABC

### Collection Hook (`framework/trace/hooks.py`)

`TraceCollectorHook` implements:
- `BeforeTurnHook` → `TURN_START`
- `FinallyTurnHook` → `TURN_END` (with result/error)
- `AfterLLMResponseHook` → `LLM_CALL`
- `BeforeToolExecutionHook` → `TOOL_BATCH`
- `AfterToolExecutionHook` → `TOOL_CALL` (per tool)

`trace_id` is generated once per turn (in `BeforeTurnHook`) and stored in `TurnStateBase.custom[TRACE_ID]` for downstream hooks to reference.

### Configuration

Default: always on. Can be toggled via `RuntimeServicesConfig` feature flag.

### Files

| File | Change |
|------|--------|
| `framework/trace/__init__.py` | New — module init |
| `framework/trace/types.py` | New — `OperationRecord`, helpers |
| `framework/trace/store.py` | New — `JsonFileTraceStore` |
| `framework/trace/hooks.py` | New — `TraceCollectorHook` |
| `framework/runtime/enums.py` | Add `TRACE_ID` to `TurnCustomKey` |

---

## C. output.md Protocol

### Design

Each subagent invocation has a dedicated `output.md` file. The subagent's system prompt instructs it to write results there. The hook checks this file when notifying the parent.

### Path

```
data/runtime_state/{pool_name}/output/{session_id}/output.md
```

- `session_id = {conversation_id}:{agent_name}:{invocation_id}`
- Created by `AgentCommunicationService._send()` when invoking a subagent
- Parent directory auto-created

### Creation Logic (`AgentCommunicationService._ensure_invocation()`)

```python
def _ensure_invocation(self, target_agent, conversation_id, invocation_id, target_kind):
    if target_kind != AgentCommKind.SUBAGENT:
        return invocation_id, None, None  # Normal→Normal: no trace/output

    # Generate or validate invocation_id
    if not invocation_id or str(invocation_id).lower() == "null":
        invocation_id = uuid4().hex[:8]
    else:
        # Check if existing trace exists for this invocation
        existing_session = self._session_strategy.format(
            conversation_id=conversation_id, agent_name=target_agent,
            invocation_id=invocation_id,
        )
        trace_path = self._runtime_dir / "trace" / existing_session
        if not trace_path.exists():
            invocation_id = uuid4().hex[:8]

    # Build paths
    session_id = self._session_strategy.format(
        conversation_id=conversation_id, agent_name=target_agent,
        invocation_id=invocation_id,
    )
    trace_dir = self._runtime_dir / "trace" / session_id
    output_path = self._runtime_dir / "output" / session_id / "output.md"

    trace_dir.mkdir(parents=True, exist_ok=True)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    return invocation_id, trace_dir, output_path
```

### System Prompt Injection

```
## Output Protocol

Your task result MUST be written to:
  {absolute_output_path}

- This file is your deliverable. What you say in conversation is transient.
- Write your final answer, analysis, or implementation result here.
- The system will notify your caller with this path when you finish.
- Do NOT rely on communication tools for result delivery — write to this file.
```

### Path Derivation (for hooks)

Paths are deterministically derived from `session_id` — hooks do not read them from runtime state:

```python
def _derive_paths(session_id: str, runtime_dir: Path) -> tuple[Path, Path]:
    trace_dir = runtime_dir / "trace" / session_id
    output_path = runtime_dir / "output" / session_id / "output.md"
    return trace_dir, output_path
```

`runtime_dir` is a constructor parameter of both `SubagentAutoSendHook` and `TraceCollectorHook` (`data/runtime_state/{pool_name}`).

### Files

| File | Change |
|------|--------|
| `framework/multi_agent/communication.py` | Add `_ensure_invocation()` method; inject output protocol into system prompt |
| `framework/runtime/enums.py` | Add `TRACE_ID` to `TurnCustomKey` (for within-turn trace correlation) |

---

## D. SubagentAutoSendHook Rewrite

### Key Changes

| Before | After |
|--------|-------|
| `AfterTurnHook` | `FinallyTurnHook` |
| Checks if LLM used `send_to_agent` tool | **No tool check** — always fires |
| Subagent has communication tools | **Subagent has NO communication tools** |
| Result goes via `send_to_agent` tool call | Result goes via hook notification + `output.md` |
| `_already_sent_in_history` fallback | Removed |
| `_communicated` set tracking | Removed |

### Always-Fire Logic

```python
class SubagentAutoSendHook(FinallyTurnHook):

    async def finally_turn(self, ctx: AgentContext, result: AgentResult | None) -> None:
        if self._agent_bus is None:
            return

        # 1. Derive artifact paths from session_id (deterministic)
        trace_dir = self._runtime_dir / "trace" / ctx.session_id
        output_path = self._runtime_dir / "output" / ctx.session_id / "output.md"

        # 2. Check output.md status
        output_status = "written" if (output_path and output_path.exists()) else "missing"

        # 3. Determine stop condition
        stop_reason = getattr(result, "stop_reason", "error") if result else "error"
        error = getattr(result, "error", None) if result else "subagent crashed"

        is_normal, hint = self._classify_stop(stop_reason, output_status, error)

        # 4. Truncate last assistant output
        content = getattr(result, "content", "") if result else ""
        summary = self._truncate(content, max_chars=1500)

        # 5. Build XML notification
        xml = self._build_xml(
            agent_name=self._self_name,
            invocation_id=self._get_invocation_id(ctx),
            status="completed" if is_normal else "incomplete",
            stop_reason=str(stop_reason),
            is_normal=is_normal,
            error=error or "",
            hint=hint,
            summary=summary,
            trace_dir=trace_dir,
            output_path=output_path,
            output_status=output_status,
        )

        # 6. Send to parent inbox
        await self._notify_parent(ctx, xml)

    @staticmethod
    def _classify_stop(stop_reason, output_status, error) -> tuple[bool, str]:
        if error:
            return False, "Subagent crashed with an error. You may want to restart with a new invocation_id."
        if str(stop_reason) == "max_iterations":
            return False, "Subagent hit step limit — task may be incomplete. Continue with same invocation_id to resume."
        if output_status == "missing":
            return False, "Subagent finished but output.md was not written. You may want to re-run this task."
        return True, ""
```

### XML Notification Format

```xml
<subagent_notification>
  <agent>worker</agent>
  <invocation_id>a1b2c3d4</invocation_id>
  <status>completed</status>
  <stop_reason>completed</stop_reason>
  <is_normal>true</is_normal>
  <error></error>
  <hint></hint>
  <summary>已修复 auth.py 中的 SQL 注入漏洞，更新了验证逻辑并添加了参数化查询...</summary>
  <artifacts>
    <trace>trace/conv:worker:a1b2c3d4/operations.jsonl</trace>
    <output>output/conv:worker:a1b2c3d4/output.md</output>
    <output_status>written</output_status>
  </artifacts>
</subagent_notification>
```

### Error/Hint Matrix

| Scenario | `is_normal` | `hint` |
|----------|-------------|--------|
| COMPLETED + output.md ✅ | `true` | "" (empty) |
| COMPLETED + output.md ❌ | `false` | "Subagent finished but output.md was not written. You may want to re-run this task." |
| MAX_ITERATIONS | `false` | "Subagent hit step limit — task may be incomplete. Continue with same invocation_id to resume." |
| ERROR / crash | `false` | "Subagent crashed with an error. You may want to restart with a new invocation_id." |

### Files

| File | Change |
|------|--------|
| `framework/hook/builtin/subagent_auto_send.py` | Rewrite: `FinallyTurnHook` base, no tool check, XML output with trace/output paths |
| `framework/multi_agent/communication.py` | Remove communication tool registration from `_build_subagent_tool_manager()` |
| `framework/multi_agent/tools.py` | `SendToAgentTool` — **preserved** (for normal agent use only) |

---

## E. Fork Context Optimization

**Deferred**. See [ADR-004](../adr/004-fork-context.md) for future work.

---

## F. LRU Subagent Pool

### Design

Framework-layer abstraction. `send_to_agent` to a subagent type → `SubagentPool.acquire()` returns or creates an instance. Session isolation via `session_id` ensures no cross-task contamination.

### `SubagentPool` (`framework/multi_agent/pool_reuse.py`)

```python
@dataclass
class _PoolEntry:
    instance: AgentInstance
    created_at: float
    last_used: float

class SubagentPool:
    def __init__(
        self,
        max_size: int = 8,
        ttl_seconds: float = 1800.0,
        eviction_check_interval: float = 120.0,
    ):
        self._pool: dict[str, _PoolEntry] = {}  # key = agent_type
        self._lru_order: list[str] = []
        self._max_size = max_size
        self._ttl = ttl_seconds
        self._lock = asyncio.Lock()
        self._cleanup_task: asyncio.Task | None = None

    async def acquire(
        self,
        agent_type: str,
        factory: Callable[[], Awaitable[AgentInstance]],
    ) -> AgentInstance:
        async with self._lock:
            # Hit: return cached, update LRU
            if agent_type in self._pool:
                entry = self._pool[agent_type]
                entry.last_used = time.monotonic()
                self._touch_lru(agent_type)
                return entry.instance

            # Miss: evict if full, then create
            while len(self._pool) >= self._max_size:
                oldest = self._lru_order[0]
                await self._evict(oldest)

            instance = await factory()
            self._pool[agent_type] = _PoolEntry(
                instance=instance,
                created_at=time.monotonic(),
                last_used=time.monotonic(),
            )
            self._lru_order.append(agent_type)
            return instance

    async def evict(self, agent_type: str):
        ...

    async def _cleanup_stale(self):
        """TTL-based idle eviction."""
        now = time.monotonic()
        stale = [t for t in self._lru_order
                 if now - self._pool[t].last_used > self._ttl]
        for t in stale:
            await self._evict(t)
```

### Integration

`AgentCommunicationService._create_dynamic_subagent()`:

```python
# Before: always creates new agent
await self._pool.register_resident(descriptor, ...)

# After: acquire from reuse pool
instance = await self._subagent_pool.acquire(
    agent_type=name,
    factory=lambda: self._create_and_register(descriptor, ...),
)
```

### Isolation Guarantee

Same `AgentInstance` handles different invocations → each has unique `session_id` → memory system isolates by `session_id` automatically. No cross-task context leakage.

### Files

| File | Change |
|------|--------|
| `framework/multi_agent/pool_reuse.py` | New — `SubagentPool` class |
| `framework/multi_agent/communication.py` | Integrate `SubagentPool.acquire()` in `_create_dynamic_subagent()` |
| `framework/multi_agent/__init__.py` | Export `SubagentPool` |

---

## Storage Layout (Final)

```
data/runtime_state/{pool_name}/
  trace/
    {session_id}/                    ← {conv}:{agent}:{invocation}
      operations.jsonl               ← Append-only, OperationRecord per line
  output/
    {session_id}/
      output.md                      ← Subagent result deliverable
```

All paths are session-scoped. Trace and output are independent artifacts with different purposes:
- **Trace**: execution log, consulted when debugging
- **output.md**: task result, consulted for the deliverable

---

## Data Flow

```
1. Normal Agent
   └── send_to_agent(target="worker", content="fix auth.py", invocation_id=null)
        │
2. AgentCommunicationService._send()
   ├── k = target_kind == SUBAGENT? → yes
   ├── invocation_id = _ensure_invocation() → "a1b2c3d4"
   ├── trace_dir / output_path created on disk
   ├── instance = SubagentPool.acquire("worker")
   ├── system_prompt += output.md protocol
   └── envelope (with trace/output metadata) → broker
        │
3. Subagent (worker)
   ├── TraceCollectorHook records each operation
   ├── Executes task, writes output.md
   └── Turn ends (any exit path)
        │
4. FINALLY_TURN hook fires
   └── SubagentAutoSendHook.finally_turn()
        ├── classify_stop(stop_reason, output_status, error)
        ├── truncate last assistant output
        ├── build XML with trace/output paths + hint
        └── send to parent inbox
        │
5. Parent receives XML notification in next InboxFlush
   ├── reads summary (truncated text)
   ├── optional: read output.md for full result
   └── optional: read operations.jsonl for execution trace
```

---

## send_to_agent Semantic Changes

| Direction | Before | After |
|-----------|--------|-------|
| Normal → Subagent | Sends task, subagent communicates back via tool | Sends task; subagent auto-notifies via hook |
| Subagent → Normal | `send_to_agent` tool call | **Removed** — subagent has no communication tools |
| Normal → Normal | Communication | Communication (unchanged) |
| invocation_id | User-controlled | Auto-managed by framework (create if missing/unknown) |

---

## Non-Breaking Changes

- `SendToAgentTool` implementation preserved for normal agents
- `AfterTurnHook` and existing `HookPoint` values unchanged
- `MemoryLayerFactory.subagent_session_isolated()` unchanged (still no archive/knowledge/experience, only session + pruned + userBuffer)
- Session ID format unchanged: `{conv}:{agent}:{invocation}`
- Inbox mechanism unchanged

---

## Testing

| Test Area | Coverage |
|-----------|----------|
| `HookRunner` | `finally_turn` dispatches correctly, errors in hook don't crash agent |
| `TraceCollectorHook` | Records TURN_START, LLM_CALL, TOOL_CALL, TURN_END; handles error exit |
| `JsonFileTraceStore` | Append, read by session_id, read by trace_id |
| `SubagentAutoSendHook` | All 4 stop conditions produce correct XML; crash path triggers; output.md check |
| `SubagentPool` | Acquire hit/miss, LRU eviction, TTL cleanup, session isolation |
| `_ensure_invocation` | New inv creation, existing inv reuse, unknown inv → new |
| Integration | Full subagent lifecycle: send → execute → crash / complete → parent receives XML |
