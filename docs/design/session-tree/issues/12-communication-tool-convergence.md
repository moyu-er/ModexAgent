# T12: Communication tool convergence — task/send_to_agent/modexctl send → tree.deliver

> Type: `wayfinder:grilling` (HITL)
> Status: **Abandoned** — fully determined by T01 + T04 + T05
> Blocked by: T02, T04

## Question

How do `task`, `send_to_agent`, and `modexctl send` converge on `tree.deliver`?

## Resolution

**Abandoned.** T01 determines: all four write paths (`pool.submit_input`, `SubagentDispatchStrategy`, `SubagentAutoSendHook._notify_parent`, `PeerNormalStrategy`) call `tree.deliver` instead of `bus.send`. T04 determines: `message_id` unified before send, `InboxProducer` preserves it. T05 determines: `tree.deliver` intercepts TASK_REQUEST / AGENT_RESULT (create track), passes through EXTERNAL_INPUT / AGENT_MESSAGE (no track). The convergence point is `SessionTreeManager.deliver()`. No independent decision remains.

### Context

User's point #3: "任何通信工具都要有这部分的处理, 包括task/send_to_agent/modexctl send这些地方如果强制给出sessionId, 那么就需要创建tree node"

User's point #6: "发送消息(task/send_to_agent/modexctl send, 三者应该收敛的)时tree的待消费列表中insert一个元素"

Current communication paths:
1. **TaskDispatchTool.execute** → `AgentCommunicationService.send_async` → `SendStrategy.execute` → `bus.send(target_session, envelope)`
2. **SendToAgentTool.execute** → same path (subagent→parent only)
3. **modexctl send** → HTTP → `InboxMQ.deliver(session_id, envelope)` (bypasses bus, direct inbox write)
4. **pool.submit_input** (user message) → `bus.send(session_id, envelope)`

All four paths write to inbox, but through different mechanisms (bus vs direct inbox). They don't update any tree state.

### Proposed convergence

**Single convergence point: `tree.deliver(target_session_id, envelope)`**

```python
class SessionTreeManager:
    async def deliver(self, target_session_id: str, envelope: AgentMessageEnvelope) -> None:
        """The ONLY path to write to inbox. Updates tree state + delivers to inbox."""
        tree = self.get_or_create_tree(target_session_id)
        # 1. Create/update tree node (DISPATCHED state)
        tree.create_or_update_node(
            session_id=target_session_id,
            invocation_id=envelope.invocation_id,
            source=envelope.source.name,
        )
        # 2. Write to inbox (existing mechanism)
        await self._inbox.deliver(target_session_id, envelope)
        # 3. Signal poller/wakeup
        self._signal_wakeup(target_session_id)
```

**Wiring:**
- `AgentCommunicationService._send`: replace `bus.send` with `tree.deliver`
- `pool.submit_input`: replace `bus.send` with `tree.deliver`  
- `modexctl send` HTTP handler: replace `inbox.deliver` with `tree.deliver`
- All three `SendStrategy` subclasses: no change to their logic (they build envelopes), but the delivery step goes through tree

**What about peer (NORMAL) targets?**
- Peer communication also goes through `tree.deliver`
- But peer is a separate tree (peer = another main agent)
- `tree.deliver(peer_session, envelope)` → creates node in PEER's tree? Or in our tree?
- User said "只考虑subagent, peer是另一棵树" — peer communication may not create tree nodes
- **Decision needed**: does peer communication create tree nodes in either tree?

### Special case: modexctl send

`modexctl send` is an external CLI that sends messages via HTTP. It doesn't have tree context. Options:
- **Option A**: HTTP handler calls `tree.deliver` — but modexctl doesn't know tree_id. It only knows session_id. Since tree_id = session_id (main agent), this works: `tree.deliver(session_id, envelope)`.
- **Option B**: HTTP handler writes to inbox directly, and tree picks it up on next consume (InboxFlushHook creates a node?). But user said InboxFlushHook does NOT create nodes.
- **Option A is correct**: tree_id = session_id, so any message to a session implicitly targets that session's tree.

### Open questions

- Does `tree.deliver` replace `AgentMessageBus.send` entirely? Or does bus stay as the transport layer (tree calls bus internally)?
- What about the `InboxPoller` wakeup signal? Currently `bus.send` signals poller. If tree replaces bus, tree must signal poller.
- Does peer communication create tree nodes? If not, how does tree know to ignore peer messages?
- What about `SubagentAutoSendHook._notify_parent`? It calls `bus.send(parent_inbox, envelope)`. This should become `tree.deliver(parent_session, envelope)` + `tree.mark_node_completed(child_session, "completed")`.
