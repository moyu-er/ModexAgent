# T07: Crash recovery — tree version chain bootstrap + stale pending inbox

> Type: `wayfinder:grilling` (HITL)
> Status: **Abandoned** — fully determined by T03
> Blocked by: T02, T03, T06

## Question

How does SessionTree recover after a process crash?

## Resolution

**Abandoned.** T03 fully determines crash recovery: scan RUNNING node versions → mark COMPLETED (new version created on next message); scan DISPATCHED tracks → check if message still in inbox → if consumed, mark CONSUMED. Tree has no version chain (T03), so no version recovery needed. The recovery procedure is defined in T03's resolution.

### Context

User's point #7: "When sending a new message and building a new tree version, should inherit the original pending inbox messages (may have been running, then shut down, then user sends message after restart)."

modex_graph's `bootstrap` (for reference):
- Queries `NodeStateStore.load_latest(node_id)` for each node
- CRASHED / orphan RUNNING / suspended RUNNING → re-execute seeds
- COMPLETED / CANCELED → skip
- PENDING delivers → add to seeds
- Restores `ctx.state` from newest snapshot
- Auto-promotes CONSUMED_PENDING delivers

### Crash scenarios

1. **Crash during turn A (agent running, no subagent dispatched)**:
   - Tree version v0, root node DISPATCHED (message in inbox, consumed by agent)
   - On restart: root node status = DISPATCHED (or RUNNING if we track that)
   - Inbox still has the message (InboxMQ persists)
   - New user message arrives → create v1, inherit pending inbox (the old message)
   - v1 root node starts fresh, InboxFlushHook consumes old message → agent sees it

2. **Crash during subagent wait (agent dispatched subagent, waiting for tree quiesce)**:
   - Tree v0, root node DISPATCHED, child node DISPATCHED (subagent message in child inbox)
   - Subagent may or may not have completed (if external process, may still be running)
   - On restart: root node + child node still DISPATCHED
   - If subagent completed while we were down: SubagentAutoSendHook already delivered reply to parent inbox → parent inbox has reply
   - If subagent crashed: no reply will come
   - New user message → create v1, inherit pending inbox (child's task message + parent's reply if arrived)
   - Stale child node (subagent session gone) → mark CANCELLED

3. **Crash during graph node execution**:
   - Graph instance is RUNNING, node invocation is RUNNING
   - On restart: `GraphOrchestrator._recover_instances` marks CRASHED, `bootstrap` re-runs node
   - Tree v0 still has pending nodes
   - Node re-execution → new tree version? Or same version?

### Recovery procedure (initial proposal)

```
recover_tree(tree_id):
  record = tree_store.get_tree(tree_id)
  if record is None: return  # no tree to recover
  
  active_version = tree_store.get_active_version(tree_id)
  if active_version is None: return  # no active version
  
  for node in active_version.nodes:
    if node.status in (DISPATCHED, CONSUMED):  # was in-flight
      # Check if the session still exists
      if session_store.get(node.session_id) is None:
        # Session gone → mark CANCELLED
        tree_store.update_node_status(tree_id, version, node.session_id, CANCELLED)
      elif inbox.has_pending(node.session_id):
        # Message still in inbox → leave DISPATCHED (will be consumed on next turn)
        pass
      else:
        # Message consumed but node not COMPLETED → was mid-turn
        # Mark as COMPLETED (turn was interrupted, can't resume mid-turn)
        tree_store.update_node_status(tree_id, version, node.session_id, COMPLETED)
  
  # Check if tree is now quiesced
  pending = tree_store.get_pending_nodes(tree_id, version)
  if not pending:
    tree_store.update_version_status(tree_id, version, COMPLETED)
```

### Open questions for grilling

- How do we detect "subagent completed while we were down"? (Check if parent inbox has a reply from the subagent's invocation_id?)
- Should crash recovery create a new version, or try to resume the current one?
- How does "inherit pending inbox" work mechanically? (Move inbox entries? Or just let the new version's InboxFlushHook consume them?)
- For external subagents (opencode): if the opencode process is still running, the subagent may complete after restart. How do we receive its result? (SubagentAutoSendHook fires in the external process — but our process was down. The result is in opencode's session, not in our inbox. We need to poll / check on restart.)
- What about tree versions older than active? Are they ever recovered? (Probably not — they're history.)
