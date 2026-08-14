# T01: SessionTree data model — resolution

> Type: `wayfinder:grilling` (HITL)
> Status: **Resolved** (validated + corrected 2026-08-11)
> Blocks: T02, T03, T05, T06, T08, T10, T11

## Question

Should SessionTree reuse modex_graph's data model (Node / NodeStateStore / version chain / DeliverStore / GraphContext), or build a separate implementation? If reuse: what "strengthening" of modex_graph is needed to support dynamic nodes?

## Resolution

**Build a separate implementation in modex_agent. Do NOT reuse modex_graph classes. Borrow patterns only.**

### Why not reuse modex_graph

R01 research informed this decision (see T01 original for R01 findings):
- **Persistence layer** (NodeStateStore, DeliverStore, GraphContext) is fully decoupled from compiled graph — operates on `graph_instance_id` + `node_id` strings.
- **Schedulers** (LinearScheduler, ParallelScheduler) have hard dependencies on `graph.nodes` being a closed set — cannot support dynamic nodes without significant changes.
- **Node ABC** is an active executor (`async execute(ctx, integrated_input)`) — but SessionTree's tree node is a **passive state tracker** (tracks message closure, not execution).
- **GraphSpecCompiler** is purely spec-driven — no dynamic mode, and bypassing it means not using most of modex_graph's value.

SessionTree's requirements (dynamic nodes, passive tracking, no scheduler, inbox-as-deliver-store) don't match modex_graph's design (static topology, active execution, scheduler-driven routing, separate DeliverStore). Forcing reuse would require weakening modex_graph's invariants for a use case it wasn't designed for.

### What patterns to borrow

- **Version chain + CAS** (from `NodeStateStore.begin_invocation` / `complete_invocation` pattern)
- **Persistence ABC + 3 implementations** (InMemory / File / Sqlite — from `InboxMQ` / `SessionStore` pattern)
- **Bootstrap recovery** (from `modex_graph/scheduler/bootstrap.py` — scan store for in-flight nodes, mark stale as crashed)
- **Liveness-guarded resolve** (from ADR-0039 registry design)

### Core principle: message tracking, not node execution state

Tree does NOT track "is the agent running?" as a completion metric — it tracks **message closure** via `MessageTrack` + **turn running state** via an in-memory set. A tree is "quiesced" when all message tracks are closed AND no turn is currently running.

### Data model (three stores, all per-workspace, ABC + InMemory/File/Sqlite)

#### SessionTreeStore — manages `SessionTreeRecord` (tree metadata):

```python
class SessionTreeStatus(StrEnum):
    ACTIVE = "active"
    COMPLETED = "completed"       # All tracks closed + no running
    CANCELLED = "cancelled"

class SessionTreeRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    
    tree_id: str                  # = root session_id (stable across session lifetime)
    root_node_session_id: str     # Points to root node
    pool_name: str
    workspace_root: str           # Workspace isolation
    status: SessionTreeStatus
    # Reversible: COMPLETED → ACTIVE when root receives new input (on_dispatch_start)
    # No version chain — tree is a persistent container (T03)
    created_at: int
    updated_at: int
    completed_at: int | None
```

#### TreeNodeStore — manages `TreeNodeRecord` (node metadata, 1:1 with sessionId):

```python
class NodeVersionStatus(StrEnum):
    RUNNING = "running"           # Dispatch cycle in progress
    COMPLETED = "completed"       # Dispatch cycle ended (normal / error / HITL suspend / max_turns)
    CANCELLED = "cancelled"       # Session evicted / crashed

class TreeNodeRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    
    tree_id: str                  # = root session_id
    session_id: str               # This node's session (1:1 binding = node_id)
    parent_session_id: str | None # Root is None
    agent_name: str
    
    # Version chain (T03): each inbox dispatch cycle = one new node version
    version: int                  # Incrementing per session
    parent_version: int | None    # Previous invocation's version
    status: NodeVersionStatus     # Version lifecycle (managed by on_dispatch_start/end)
    
    created_at: int
    updated_at: int
```

**Note**: T02 says "no node completion state machine" — this refers to tree NOT deriving "node complete" as a stored completion state. T03 adds `NodeVersionStatus` for **version lifecycle** (RUNNING/COMPLETED/CANCELLED), managed by `on_dispatch_start`/`on_dispatch_end` (bound to InboxPoller dispatch cycle, NOT ReAct hooks). These are different levels, not contradictory.

#### MessageTrackStore — manages `MessageTrack` (message tracking):

```python
class MessageTrackStatus(StrEnum):
    DISPATCHED = "dispatched"     # Message delivered to inbox, not yet closed
    CONSUMED = "consumed"         # Closed loop (AGENT_RESULT consumed, or on_dispatch_end fallback)
    CANCELLED = "cancelled"       # Session evicted

class MessageTrack(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    
    track_id: str
    tree_id: str                  # = root session_id (no version — T03 removed tree_version)
    
    # Message association
    message_id: str               # Inbox message_id (for consume matching)
    message_type: str             # TASK_REQUEST / AGENT_RESULT only
    invocation_id: str | None     # Links TASK_REQUEST ↔ AGENT_RESULT
    
    # Sender / receiver
    target_session_id: str        # Which session's inbox this was delivered to
    source_session_id: str        # Sender's session (parent or child)
    
    status: MessageTrackStatus
    dispatched_at: int
    consumed_at: int | None
```

**Track is version-agnostic** (T03): quiesce checks all tracks, not filtered by version. `tree_version` field removed (T03).

### Message handling rules (corrected)

| Message type | Source | Create track? | On consume (`on_consumed`) | On dispatch end (`on_dispatch_end`) | Intercept? |
|---|---|---|---|---|---|
| TASK_REQUEST | parent → child | ✅ DISPATCHED | ❌ **不关闭** (等 AGENT_RESULT) | ✅ 兜底关闭 | ✅ Yes |
| AGENT_RESULT | child → parent | ✅ DISPATCHED + 关闭对应 TASK_REQUEST (by invocation_id) | ✅ 关闭 CONSUMED | ✅ 兜底关闭 | ✅ Yes |
| EXTERNAL_INPUT | user → main | ❌ No track | 无操作 | 无操作 | Pass-through |
| AGENT_MESSAGE (peer) | peer → peer | ❌ No track | 无操作 | 无操作 | Pass-through. **完全不影响发送方 tree** — peer = 另一个 tree 的 root; peer 消息 = 接收 tree 的外部输入 (与用户输入同构). |

**Track closing rules** (see T05 corrected for full rationale):

- **TASK_REQUEST**: NOT closed on consume (subagent read the message but hasn't replied). Closed when:
  1. Corresponding AGENT_RESULT is delivered (matched by `invocation_id`) — normal case
  2. `on_dispatch_end(session_id)` fallback — covers fold-in consumption, subagent chose not to reply, crash
- **AGENT_RESULT**: closed on consume (`on_consumed`) — parent read the reply, loop closed.
- **Why TASK_REQUEST is not closed on consume**: closing on consume creates a brief quiesced window (subagent consumed TASK_REQUEST but hasn't replied AGENT_RESULT yet) → graph node's `wait_quiesce` could return prematurely.

### Quiesce detection (corrected — does NOT check inbox)

**Correction from original T01**: T01 originally proposed `quiesce = all tracks CONSUMED + no pending inbox (inbox.count)`. This is **wrong**:
1. `inbox.count` doesn't filter by message type → peer messages cause false non-quiesce
2. Couples tree to InboxMQ internals → tree should be self-contained

**Correct quiesce** — SQL query on track table + in-memory running set:

```python
def is_quiesced(self, tree_id: str) -> bool:
    """无 DISPATCHED track + 无 running node。不查 inbox。"""
    # SQL: SELECT COUNT(*) FROM message_tracks WHERE tree_id=? AND status='dispatched'
    if self._track_store.has_dispatched(tree_id):
        return False
    # _running: in-memory set, managed by on_dispatch_start (add) / on_dispatch_end (remove)
    sessions = self._node_store.get_tree_sessions(tree_id)
    return not any(s in self._running for s in sessions)
```

- Peer messages: no track → excluded → don't affect quiesce ✅
- EXTERNAL_INPUT: no track, but triggers dispatch → `_running` → not quiesced ✅
- TASK_REQUEST/AGENT_RESULT: have tracks → affect quiesce ✅

### session_id + message_id unification (already correct)

**Validation finding**: `InboxProducer.send` (producer.py:84) **already preserves** `envelope.message_id`. No fix needed. See T04 (corrected) for the full verification.

- `session_id`: determined before send (`SubagentDispatchStrategy.build_session` creates session before `build_envelope`)
- `message_id`: `AgentMessageEnvelope.message_id` generated at envelope construction, preserved through `InboxProducer.send` → `InboxMessage.message_id` → consume → `_reconstruct` → `AgentMessageEnvelope.message_id`
- Flow: `envelope.message_id` = `MessageTrack.message_id` = `InboxMessage.message_id` (already unified)

### Tree interfaces (SessionTreeManager) — 8 methods

**Design principle**: tree 绑定 inbox 投递/消费/调度机制, 不绑定 hook/react。生命周期与 inbox 的"投递 → 消费 → 调度 → 调度结束"对齐。

```python
class SessionTreeManager:
    """Pool-level tree manager. State management + message delivery + quiesce detection.
    
    Bound to inbox dispatch lifecycle (InboxPoller), NOT to ReAct hooks.
    Does NOT handle: pipeline, turn driving, scheduling.
    """
    
    def __init__(self, tree_store, node_store, track_store, bus, poller):
        self._running: set[str] = set()           # running session_ids (in-memory)
        self._quiesce_event: asyncio.Event = asyncio.Event()
    
    async def deliver(self, target_session_id: str, envelope: AgentMessageEnvelope) -> None:
        """Sole inbox write path for all agent communication. 4 write paths converge here.
        - TASK_REQUEST: create track (DISPATCHED)
        - AGENT_RESULT: close matching TASK_REQUEST (by invocation_id) + create track (DISPATCHED)
        - EXTERNAL_INPUT / AGENT_MESSAGE: no track, bus.send directly
        Then calls bus.send + signal_quiesce.
        Tree status → ACTIVE."""
    
    async def on_consumed(self, session_id: str, message: InboxMessage) -> None:
        """InboxConsumer callback (injected at construction). 
        Only closes AGENT_RESULT tracks. TASK_REQUEST NOT closed here.
        EXTERNAL_INPUT / AGENT_MESSAGE: no-op.
        fold-in consume triggers this (刷新 track 记录), but does NOT create new version."""
    
    async def on_dispatch_start(self, session_id: str) -> None:
        """Called by InboxPoller._dispatch_batch entry (inbox 调度此 session 开始).
        New node version (version+1, parent_version=旧, status=RUNNING). Adds to _running set.
        Tree status → ACTIVE (if was COMPLETED).
        fold-in 不触发此方法 (不经过 _dispatch_batch) → 不新建版本."""
    
    async def on_dispatch_end(self, session_id: str) -> None:
        """Called by InboxPoller._end_dispatch (调度周期结束, 在 _run_turn/_materialize_then_turn 的 finally).
        1. 兜底关闭所有未关闭 TASK_REQUEST tracks targeting this session
        2. Remove from _running
        3. Node version → COMPLETED (任何完整退出, 无论成功/错误/HITL/取消)
        4. If is_quiesced(tree_id) → tree status → COMPLETED
        5. signal _quiesce_event
        No success parameter — 错误也是 COMPLETED, tree stays ACTIVE if not quiesced."""
    
    def is_quiesced(self, tree_id: str) -> bool:
        """无 DISPATCHED track + 无 running node。不查 inbox。"""
    
    async def wait_quiesce(self, tree_id: str, *, timeout: float | None = None) -> None:
        """Block until tree quiesce. Loop: check → signal_wakeup → await _quiesce_event."""
    
    async def on_session_evicted(self, session_id: str) -> None:
        """Cleanup: cancel tracks for session, remove from _running. 
        If root → tree status CANCELLED."""
    
    async def recover_tree(self, tree_id: str) -> None:
        """Crash recovery: scan DISPATCHED tracks → check inbox → CONSUMED if no longer pending.
        Scan RUNNING node versions → COMPLETED or CANCELLED."""
```

### Version management rules (simplified — no success parameter, one record in-place)

**一条记录, 原地更新** (非多记录版本链). 每个 node 只有一个活跃版本 (single-flight 保证不会并发).

| 时机 | 操作 | 版本 | 状态 |
|---|---|---|---|
| `tree.deliver` → `_ensure_node` (新建会话) | 创建 node | version=0 | COMPLETED (初始, 未调度) |
| `tree.deliver` → `_ensure_node` (已有会话) | 无变化 | 不变 | 不变 |
| `on_dispatch_start` | **新建版本** | version+1, parent_version=旧 | RUNNING |
| `on_dispatch_end` (正常/错误/HITL/取消) | 状态刷新 | 不变 | **COMPLETED** |
| fold-in (InboxFlushHook) | **不触发** | 不变 | 不变 ✅ |
| `on_session_evicted` | 取消 | 不变 | CANCELLED |
| `recover_tree` (崩溃恢复) | 派生 | 不变 | RUNNING → COMPLETED 或 CANCELLED |

**关键简化**:
- **无 success 参数** — 任何完整退出 (inbox 调度周期结束) = COMPLETED. 错误退出也是 COMPLETED.
- **每个 node 只有一个活跃版本** — single-flight 保证不会同时两个 dispatch.
- **不用考虑历史版本** — `parent_version` 仅记录上一个版本号 (审计用), 不维护版本链查询.
- **报错后重新调用 = 新版本** — on_dispatch_start 创建新版本, 不关心历史.
- **fold-in 不新建版本** — fold-in 不经过 `_dispatch_batch`, 不触发 on_dispatch_start, 只触发 on_consumed (刷新 track 记录).

### Write path convergence (verified by code audit)

**Correction from original design**: `SubagentDispatchStrategy` and `ParentReplyStrategy` do NOT have their own `deliver` methods. Both inherit `SendStrategy._deliver` (base.py:169). The actual convergence scope is 4 **call sites**, not 4 strategy files:

| Write path | Actual bus.send call site | File to modify |
|---|---|---|
| `pool.submit_input` | `pool.py:287` | `pool.py` |
| `SendStrategy._deliver` (base) | `base.py:169` | `base.py` (covers SubagentDispatch + ParentReply) |
| `PeerNormalStrategy.deliver` | `peer_normal.py:77` | `peer_normal.py` (covers PeerNormal + _TracePropagatingPeerNormal) |
| `SubagentAutoSendHook._notify_parent` | `subagent_auto_send.py:478` | `subagent_auto_send.py` |

**Files that do NOT need modification** (verified — no `deliver` method, no `bus.send` call):
- `subagent_dispatch.py` (104 lines, no `deliver` override)
- `parent_reply.py` (81 lines, no `deliver` override)
- `service.py` (`_TracePropagatingPeerNormal` only overrides `build_envelope`, inherits `deliver`)

**modexctl send** converges automatically: CLI → HTTP → `facade.send` → `AgentCommunicationService._send` → strategy → `base.py:_deliver` → `tree.deliver`. Not a separate write path.

**`InboxMQ.deliver` (sync, server.py:117)** has zero production callers (only tests). Retained as ABC contract, not on the hot path.

### Separation of concerns

- **Tree**: manages tree status + node metadata + message tracks + quiesce detection + running set
- **Node**: 1:1 with sessionId. Managed separately (TreeNodeStore). Version chain tracks invocation boundaries.
- **MessageTrack**: independent storage (MessageTrackStore). Associated to node via `target_session_id` + `tree_id`.
- **Tree does NOT handle**: pipeline execution, turn driving, scheduling. Only: state management + message delivery + quiesce detection + dispatch lifecycle hooks (version + running).

## Validation Findings (2026-08-11)

Code audit discovered five corrections to the original T01 design:

1. **`message_id` is already preserved** by `InboxProducer.send` (producer.py:84). No fix needed. See T04 corrected.

2. **Quiesce must NOT check `inbox.count`**. Peer messages cause false non-quiesce. **Fix**: quiesce = no DISPATCHED tracks + no running nodes (SQL + in-memory, no inbox access).

3. **TASK_REQUEST tracks must NOT close on consume**. Closing on consume creates a brief quiesced window before AGENT_RESULT arrives. **Fix**: TASK_REQUEST closes on AGENT_RESULT deliver (by invocation_id) or on_dispatch_end fallback.

4. **`on_consumed` belongs in `InboxConsumer`**, not `pool.consume_inbox`. `InboxFlushHook._flush` bypasses `pool.consume_inbox` and calls `InboxConsumer.consume` directly. See T05 corrected.

5. **Convergence scope is 4 call sites, not 4 strategy files**. `SubagentDispatchStrategy` and `ParentReplyStrategy` both inherit `base.py:_deliver`. Modifying `base.py` covers both. See write path convergence table above.

6. **Dispatch lifecycle callbacks needed**: `on_dispatch_start`/`on_dispatch_end` bound to InboxPoller's dispatch cycle (`_dispatch_batch` entry + `_run_turn`/`_materialize_then_turn` finally). Tree binds to inbox dispatch mechanism, NOT to ReAct hooks. fold-in does not trigger these (goes through InboxFlushHook, not _dispatch_batch), naturally implementing T03's "fold-in = no new version" rule. No success parameter — any complete dispatch exit = COMPLETED.

7. **Peer messages are completely unrelated to sender's tree**. No track, no quiesce impact, no tree status change. Receiving tree treats as external input. The two dimensions (agent implementation type: native/external; message/topology type: TASK_REQUEST/AGENT_RESULT/AGENT_MESSAGE/EXTERNAL_INPUT) are orthogonal. Tree only cares about message type, not agent implementation.

## Comments

### Resolved in this grilling session

- **Separate implementation, not modex_graph reuse** — confirmed by R01 research + user direction
- **Message tracking model, not node execution state** — user clarified: "发送消息和消费消息是一对的, 每条消息都要有记录维护状态"
- **Three message types: track / no-track / no-intercept** — user confirmed
- **Quiesce = no DISPATCHED tracks + no running** — confirmed (corrected from inbox.count)
- **TASK_REQUEST not closed on consume** — confirmed (avoid brief quiesced window)
- **MessageTrack independent storage** — user chose Option B
- **session_id + message_id unified before send** — user requirement: "我希望是send前就确定"
- **Peer communication not intercepted** — user: "不要把peer调用也拦截进来"; peer 完全不影响发送方 tree
- **User input: no track, triggers running** — user: "用户输入是独立的源, 只能用running来判断"
- **tree status 必须维护** — user confirmed; tree 级别 status (ACTIVE/COMPLETED/CANCELLED), 不查所有 node 状态
