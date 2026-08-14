# T03: Version chain design — resolution

> Type: `wayfinder:grilling` (HITL)
> Status: **Resolved**
> Blocks: T06, T07

## Question

How does versioning work? What has version chains — tree, node, or both?

## Resolution

**Tree does NOT have a version chain. Only nodes do.**

### Why tree has no version chain

Tree is a persistent container spanning multiple calls. Its `tree_id` (= root session_id) is stable across the session's lifetime. Tree status (ACTIVE / COMPLETED / CANCELLED) reflects the current holistic state — no need to version it.

Tree version was originally proposed for "each new top-level input = new version." But on analysis:
- Quiesce detection does NOT filter by version — it checks all tracks + inbox pending
- Inbox does NOT filter by version — new calls consume old pending inbox messages
- Version's only purpose is distinguishing different calls (audit / debug / recovery)
- That purpose is served by **node version**, not tree version

### Node version chain

Each TreeNode has a version chain. **Every inbox dispatch cycle = one new node version** (managed by `on_dispatch_start`/`on_dispatch_end`, bound to InboxPoller — NOT ReAct hooks):

| Trigger | New node version? | Why |
|---|---|---|
| User input (EXTERNAL_INPUT → new dispatch) | ✅ | New dispatch cycle |
| Subagent dispatch (parent → child TASK_REQUEST → child dispatch) | ✅ | Child's new dispatch cycle |
| Subagent reply (child → parent AGENT_RESULT → parent new dispatch) | ✅ | Parent's new dispatch cycle |
| Peer message (peer → peer → new dispatch on receiving tree) | ✅ | Receiving peer's new dispatch cycle |
| HITL approval resume (approval decision → new dispatch) | ✅ | New dispatch cycle after pause |
| HITL approval suspend (agent pauses for approval) | ✅ (marks current version complete via on_dispatch_end) | The dispatch cycle ended (paused); approval resume starts a new version |
| InboxFlushHook fold-in consume | ❌ | Fold-in 不经过 _dispatch_batch, 不触发 on_dispatch_start → 不新建版本 |

### Node version lifecycle (managed by on_dispatch_start / on_dispatch_end)

```
NodeVersion:
  version: int                 # 0, 1, 2, ... (incrementing per session)
  parent_version: int | None   # Previous version (audit only, no chain query)
  status: NodeVersionStatus    # RUNNING / COMPLETED / CANCELLED
  
  on_dispatch_start: version+1, parent_version=旧, status=RUNNING
  on_dispatch_end: status → COMPLETED (任何完整退出: 正常/错误/HITL/取消)
  on_session_evicted: status → CANCELLED
```

**No success parameter** — `on_dispatch_end` marks version COMPLETED regardless of exit path. 错误退出也是 COMPLETED — inbox 调度周期结束了就是 complete. tree stays ACTIVE if not quiesced, user can retry.

**One record, in-place update** — 每个 node 只有一条记录, version 字段原地递增. 不维护多记录版本链. `parent_version` 仅记录上一个版本号 (审计用), 不用于查询.

**Each node has only one active version** — single-flight (InboxPoller `_inflight` dict) guarantees no concurrent dispatch for the same session.

Note: HITL approval suspend marks the version COMPLETED (the dispatch cycle ended). Approval resume creates a NEW version — it's a new dispatch cycle, not a continuation of the old one. The agent's session memory (history) carries forward; the version chain just tracks dispatch boundaries.

### Track and version relationship

- **MessageTrack does NOT filter by version for quiesce detection.** Track belongs to a `tree_id` + `target_session_id`, not to a specific node version.
- Track's `tree_version` field (from T01) is **removed** — tree has no version.
- Track may optionally carry `node_version` for audit/debug (which invocation produced this message), but quiesce ignores it.
- **Quiesce = no DISPATCHED tracks + no running nodes** — version-agnostic, inbox-agnostic. Does NOT check inbox.count (peer messages in inbox would cause false non-quiesce).

### Inbox and version relationship

- Inbox is keyed by `session_id`, not by version.
- New node version's turn consumes old pending inbox messages (inheritance — user requirement #7).
- "Inherit pending inbox" = inbox entries stay; new version's InboxFlushHook / poller naturally consumes them. No move, no copy.
- Restart scenario: process crashes mid-version → on restart, inbox still has pending messages → new ReACT turn (new version) consumes them.

### Simplified data model (updated from T01)

#### SessionTreeRecord (no version chain)

```python
class SessionTreeStatus(StrEnum):
    ACTIVE = "active"
    COMPLETED = "completed"       # No DISPATCHED tracks + no running nodes
    CANCELLED = "cancelled"

# Tree status is REVERSIBLE:
#   COMPLETED → ACTIVE  (root node receives new input — new dispatch cycle)
# Tree status depends on node state. A "completed" tree becomes "active" again
# when the root node receives a new input (user message / agent communication /
# HITL resume). This is NOT a version transition — the same tree instance
# reactivates because a new node version starts.

class SessionTreeRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    
    tree_id: str                  # = root session_id (stable across session lifetime)
    root_node_session_id: str     # Points to root node
    pool_name: str
    workspace_root: str
    
    status: SessionTreeStatus
    created_at: int
    updated_at: int
    completed_at: int | None
```

#### TreeNodeRecord (with version chain)

```python
class NodeVersionStatus(StrEnum):
    RUNNING = "running"           # Dispatch cycle in progress
    COMPLETED = "completed"       # Dispatch cycle ended (normal / error / HITL suspend / max_turns)
    CANCELLED = "cancelled"       # Session evicted / crashed

class TreeNodeRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    
    tree_id: str                  # = root session_id
    session_id: str               # This node's session (1:1 binding)
    
    # Version chain
    version: int                  # This invocation's version number
    parent_version: int | None    # Previous invocation's version
    
    parent_session_id: str | None # Root is None
    agent_name: str
    
    status: NodeVersionStatus
    created_at: int
    updated_at: int
```

#### MessageTrack (no version field)

```python
class MessageTrack(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    
    track_id: str
    tree_id: str                  # = root session_id (no version)
    
    message_id: str               # Inbox message_id (for consume matching)
    message_type: str             # TASK_REQUEST / AGENT_RESULT
    invocation_id: str | None
    
    target_session_id: str
    source_session_id: str
    
    status: MessageTrackStatus    # DISPATCHED / CONSUMED
    dispatched_at: int
    consumed_at: int | None
```

### Quiesce detection (version-agnostic)

```python
def is_quiesced(self, tree_id: str) -> bool:
    """Holistic, version-agnostic."""
    
    # 1. Any DISPATCHED tracks? (across ALL node versions, not filtered)
    pending_tracks = self._track_store.get_tracks_by_status(
        tree_id, {MessageTrackStatus.DISPATCHED}
    )
    if pending_tracks:
        return False
    
    # 2. Any pending inbox for any session in this tree?
    sessions = self._node_store.get_tree_sessions(tree_id)  # all sessions, all versions
    for sid in sessions:
        if await self._inbox.count(sid) > 0:
            return False
    
    return True
```

### Crash recovery (simplified — no tree version to recover)

```
recover_tree(tree_id):
  tree = tree_store.get_tree(tree_id)
  if tree is None: return
  
  # Recover node versions
  for node_record in node_store.get_nodes(tree_id):
    if node_record.status == RUNNING:
      # Was mid-ReAct when crashed
      # Check if session still exists
      if session_registry.get(node_record.session_id) is None:
        node_store.update_status(tree_id, session_id, version, CANCELLED)
      else:
        node_store.update_status(tree_id, session_id, version, COMPLETED)
        # A new version will be created when the next message arrives
  
  # Recover tracks
  for track in track_store.get_tracks(tree_id, {DISPATCHED}):
    # Was the message consumed while we were down?
    # Check inbox: if message_id is no longer pending, it was consumed
    if not inbox.has_message(track.target_session_id, track.message_id):
      track_store.update_status(track.track_id, CONSUMED)
    # Else: still DISPATCHED — will be consumed on next turn
  
  # Check quiesce
  if self.is_quiesced(tree_id):
    tree_store.update_status(tree_id, COMPLETED)
```

## Comments

### Resolved in this grilling session

- **Tree has no version chain** — tree is a persistent container. Only nodes have versions.
- **Node version = one inbox dispatch cycle** — every trigger (user input, agent communication, HITL resume) creates a new node version via on_dispatch_start. InboxFlushHook fold-in is the only exception (不经过 _dispatch_batch).
- **HITL approval suspend = node version COMPLETED** — the dispatch cycle ended (on_dispatch_end). Approval resume = new version.
- **Track is version-agnostic** — quiesce checks all tracks, not filtered by version.
- **Inbox is version-agnostic** — new versions consume old pending inbox. "Inherit pending inbox" = inbox entries stay, naturally consumed.
- **Tree version field removed from MessageTrack** — simplified from T01.
- **Tree status is reversible (COMPLETED → ACTIVE)** — tree completion is not terminal. When root node receives new input (user message / agent comm / HITL resume), tree transitions back to ACTIVE. Tree status depends on node state, not the other way around. Root node is the sole external entry point.
