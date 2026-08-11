# T05: InboxPoller integration — resolution

> Type: `wayfinder:grilling` (HITL)
> Status: **Resolved** (validated + corrected 2026-08-11)
> Blocks: T08

## Question

Does SessionTree replace InboxPoller, wrap it, or serve as its state backend?

## Resolution

**Tree is a shell layer over bus.send. InboxPoller is completely unchanged.**

### Architecture

```
Current:
  writers → bus.send → InboxProducer.send (persist) + poller.signal_wakeup
  poller → _dispatch_batch → consume_inbox → dispatch_envelope → pipeline.process_message
  InboxFlushHook (mid-turn) → consumer.consume → history.append

New:
  writers → tree.deliver → [track update] → bus.send → InboxProducer + signal_wakeup
  poller → _dispatch_batch → [on_dispatch_start] → consume_inbox → dispatch_envelope → pipeline.process_message
          finally → _end_dispatch → [on_dispatch_end] + inflight.pop + signal_wakeup
  InboxFlushHook (mid-turn) → consumer.consume → [on_consumed callback] → history.append
  graph node → tree.wait_quiesce → (loop: check quiesce → signal_wakeup → await event)
```

### Four integration points (all minimal, non-invasive)

**1. `tree.deliver` replaces `bus.send`**

All four current write paths call `tree.deliver` instead of `bus.send`. Tree internally calls `bus.send` after updating track state.

Convergence points (verified by code audit — see Validation Findings):

| Write path | Actual bus.send call site | Covers |
|---|---|---|
| `pool.submit_input` | `pool.py:287` | EXTERNAL_INPUT (user/DM/approval) |
| `SendStrategy._deliver` (base class) | `base.py:169` | SubagentDispatch + ParentReply (both inherit, no own `deliver`) |
| `PeerNormalStrategy.deliver` | `peer_normal.py:77` | PeerNormal + `_TracePropagatingPeerNormal` |
| `SubagentAutoSendHook._notify_parent` | `subagent_auto_send.py:478` | AGENT_RESULT |

**Correction from original design**: `SubagentDispatchStrategy` and `ParentReplyStrategy` do NOT have their own `deliver` methods — both inherit `SendStrategy._deliver` (base.py:169). The convergence point for both is `base.py`, not `subagent_dispatch.py` or `parent_reply.py`. Modifying `base.py:_deliver` covers both strategies in one edit.

**InboxPoller: zero changes.** It sees inbox pending → drives turn, same as before.

**2. `on_consumed` injected into `InboxConsumer` (NOT `pool.consume_inbox`)**

**Correction from original design**: T05 originally claimed `pool.consume_inbox` is "the single consume entry point — both fold-in and between-turn call it." This is **false**. Code audit confirmed:

- **Between-turn (idle session)**: `InboxPoller._dispatch_batch` → `pool.consume_inbox` → `bus.consume` → `InboxConsumer.consume` → `InboxMQ.consume`
- **Fold-in (busy session)**: `InboxFlushHook._flush` → `self._consumer.consume` **directly** (inbox_flush.py:50), **bypassing `pool.consume_inbox` and `bus.consume`**

Placing `on_consumed` in `pool.consume_inbox` would miss all fold-in consumes → tracks never close → `wait_quiesce` deadlocks.

**Correct placement**: inject `on_consumed` callback into `InboxConsumer` construction. Both consume paths flow through `InboxConsumer.consume` (consumer.py:62) — this is the true single convergence point.

```python
# InboxConsumer (minimal change — add optional callback)
class InboxConsumer(BaseInboxConsumer):
    def __init__(self, server, cache_size=1000, *, on_consumed=None):
        self._server = server
        self._on_consumed = on_consumed  # optional: SessionTreeManager.on_consumed
        ...

    async def consume(self, session_id, limit=100, *, only_types=None):
        messages = await self._server.consume(session_id, limit, only_types=only_types)
        # ... dedup ...
        if self._on_consumed is not None:
            for msg in result:
                await self._on_consumed(session_id, msg)
        return result
```

Pool assembly wires it once:
```python
consumer = InboxConsumer(server=inbox_server, on_consumed=tree_manager.on_consumed)
```

**Note**: There are two `InboxConsumer` instances in the bot project path (bus's consumer + DefaultAgentFactory's consumer). Both must receive the same `on_consumed` callback. Since the callback is stateless (delegates to tree_manager), both can share the same tree_manager reference.

**Does NOT change**: `pool.consume_inbox`, `InboxFlushHook`, `InboxPoller._dispatch_batch`.

**3. Dispatch lifecycle callbacks in InboxPoller (tree binds to inbox dispatch mechanism, NOT hooks)**

**Design principle**: tree 绑定 inbox 投递/消费/调度机制, 不绑定 hook/react。生命周期与 inbox 的"投递 → 消费 → 调度 → 调度结束"对齐。

InboxPoller has two turn paths (`_run_turn` / `_materialize_then_turn`) with identical finally patterns (`inflight.pop + signal_wakeup`). Extract shared cleanup + add tree callbacks:

```python
# InboxPoller._dispatch_batch — entry (加 on_dispatch_start, +2 行)
async def _dispatch_batch(self, sid, instance):
    if self._tree_manager is not None:
        await self._tree_manager.on_dispatch_start(sid)    # new version RUNNING
    batch = await self._pool.consume_inbox(sid)
    for envelope in batch:
        await self._pool.dispatch_envelope(sid, instance, envelope)

# InboxPoller._end_dispatch — new shared cleanup method (+5 行)
async def _end_dispatch(self, sid: str) -> None:
    """Common cleanup after dispatch cycle ends."""
    self._inflight.pop(sid, None)
    if self._tree_manager is not None:
        await self._tree_manager.on_dispatch_end(sid)      # version COMPLETED + fallback track close
    self.signal_wakeup()

# _run_turn — finally 改为调 _end_dispatch (净 -1 行)
async def _run_turn(self, sid, instance):
    try:
        await self._ensure_session_registered(sid)
        await self._dispatch_batch(sid, instance)
    finally:
        await self._end_dispatch(sid)

# _materialize_then_turn — 同上
async def _materialize_then_turn(self, sid, template):
    try:
        ...
        await self._dispatch_batch(sid, instance)
    except Exception:
        logger.exception(...)
    finally:
        await self._end_dispatch(sid)
```

**改动量**: `_dispatch_batch` +2 行, 新增 `_end_dispatch` +5 行, 两个 finally 各改 1 行。`_loop`/`_tick`/`_maybe_start`/`_reconcile`/single-flight 全不变。

**Fold-in does NOT trigger these callbacks** — fold-in goes through `InboxFlushHook._flush` → `consumer.consume` → `history.append`, never reaching `_dispatch_batch`. This naturally implements T03's "InboxFlushHook fold-in consume does NOT create new version" rule.

**No success parameter** — `on_dispatch_end` marks version COMPLETED regardless of exit path (normal / error / HITL / cancel). 错误也是 complete — inbox 调度周期结束了就是 COMPLETED. tree stays ACTIVE if not quiesced, user can retry.

**Does NOT change**: `pool.dispatch_envelope`, `pool.consume_inbox`, `InboxFlushHook`, InboxPoller core logic (`_loop`/`_tick`/`_maybe_start`/`_reconcile`/single-flight).

**4. `wait_quiesce` triggers poller via `signal_wakeup` (idempotent)**

```python
# SessionTreeManager.wait_quiesce
async def wait_quiesce(self, tree_id, *, timeout=None):
    deadline = ... if timeout else None
    while True:
        if self.is_quiesced(tree_id):
            return
        # If there's pending inbox but no running turn, poller needs to wake
        self._poller.signal_wakeup()  # idempotent — Event.set is no-op if already set
        # Wait for state change (track consumed / message delivered / turn ended)
        await asyncio.wait_for(self._quiesce_event.wait(), timeout=remaining)
        self._quiesce_event.clear()
```

Tree does NOT query poller's `inflight` state. `signal_wakeup` is idempotent — poller wakes, checks its own inflight, decides whether to drive a turn.

`_quiesce_event` (asyncio.Event) is set by `on_consumed` (track closed), `deliver` (new message/track), and `on_dispatch_end` (dispatch cycle completed). Each set triggers a quiesce re-check.

**InboxPoller: zero changes.** `signal_wakeup` already exists and is called by `bus.send`.

### Quiesce detection (corrected — does NOT check inbox)

**Correction from original design**: T01/T02 originally proposed `quiesce = all tracks CONSUMED + no pending inbox (inbox.count)`. This is **wrong** for two reasons:

1. **Peer messages in inbox**: `inbox.count` doesn't filter by message type. Peer messages (AGENT_MESSAGE) sit in the inbox but should NOT affect the sender's tree quiesce — they belong to the receiving tree's concern.
2. **API coupling**: checking `inbox.count` couples tree to InboxMQ internals. Tree should be self-contained.

**Correct quiesce**: SQL query on track table + in-memory running set. No inbox access:

```python
def is_quiesced(self, tree_id) -> bool:
    """无 DISPATCHED track + 无 running node。不查 inbox。"""
    # SQL: SELECT COUNT(*) FROM message_tracks WHERE tree_id=? AND status='dispatched'
    if self._track_store.has_dispatched(tree_id):
        return False
    # _running: in-memory set, managed by on_dispatch_start (add) / on_dispatch_end (remove)
    sessions = self._node_store.get_tree_sessions(tree_id)
    return not any(s in self._running for s in sessions)
```

- Peer messages: no track → `has_dispatched` excludes them → don't affect quiesce ✅
- EXTERNAL_INPUT: no track, but triggers turn → running → not quiesced ✅
- TASK_REQUEST/AGENT_RESULT: have tracks → affect quiesce ✅

### Track closing rules (corrected — TASK_REQUEST not closed on consume)

**Correction from original design**: T01/T04 originally proposed closing tracks in `on_consumed` for all tracked message types. This creates a **短暂 quiesced 窗口**: subagent consumes TASK_REQUEST → track closes → tree briefly quiesced → graph node's `wait_quiesce` returns prematurely (before AGENT_RESULT arrives).

**Correct closing rules**:

| Message type | deliver 时 | on_consumed 时 | on_dispatch_end 兜底 |
|---|---|---|---|
| TASK_REQUEST | 创建 DISPATCHED | **不关闭** | 关闭 (兜底: fold-in 消费/subagent 不回复/crash) |
| AGENT_RESULT | 关闭对应 TASK_REQUEST (by invocation_id) + 创建 DISPATCHED | 关闭 CONSUMED | 关闭 (兜底) |
| EXTERNAL_INPUT | 无 track | 无操作 | 无操作 |
| AGENT_MESSAGE (peer) | 无 track | 无操作 | 无操作 |

**Why TASK_REQUEST is not closed on consume**: `on_consumed(TASK_REQUEST)` only means the subagent read the message — it hasn't replied yet. The track should stay DISPATCHED until the AGENT_RESULT arrives (closing the loop by invocation_id) or the subagent's dispatch cycle ends without replying (on_dispatch_end fallback).

**on_dispatch_end fallback**: when a subagent's dispatch cycle ends, close all unclosed TASK_REQUEST tracks targeting that session. This handles: fold-in consumption (subagent consumed but didn't reply), subagent chose not to reply, subagent crashed.

### What does NOT change

- InboxPoller core logic (`_loop`, `_tick`, `_maybe_start`, `_reconcile`, single-flight)
- `inflight: dict[sid, Task]` single-flight semantics (only moved to `_end_dispatch`)
- Fold-in path (`InboxFlushHook._flush` via `consumer.consume`)
- Materialize subagent lazily (`_materialize_then_turn` structure — only finally calls `_end_dispatch`)
- `pool.consume_inbox` (no change — on_consumed is in InboxConsumer, not here)
- `pool.dispatch_envelope` (no change — dispatch lifecycle is in InboxPoller, not here)
- `InboxFlushHook` (no change)

### What changes

| Component | Change |
|---|---|
| `bus.send` callers (4 sites) | Call `tree.deliver` instead (tree calls bus.send internally) |
| `InboxConsumer.__init__` | Add optional `on_consumed` callback parameter |
| `InboxPoller._dispatch_batch` | Add `on_dispatch_start` at entry (+2 lines) |
| `InboxPoller._end_dispatch` (new) | Shared cleanup: `inflight.pop` + `on_dispatch_end` + `signal_wakeup` (+5 lines) |
| `_run_turn` / `_materialize_then_turn` finally | Change to call `_end_dispatch` (each -1 line) |
| Pool assembly | Inject `SessionTreeManager` into InboxPoller + wire `on_consumed` into InboxConsumer(s) |
| `SessionTreeManager` | Holds `poller` reference for `signal_wakeup` + `_quiesce_event` + `_running` set |

### Why this works

Tree is purely additive state management. It doesn't affect turn driving, single-flight, materialization, or fold-in. Poller's well-tested logic stays untouched. Tree's `wait_quiesce` leverages poller's existing `signal_wakeup` — the same mechanism `bus.send` uses. Turn lifecycle hooks in `dispatch_envelope` are non-invasive wrappers around the existing `process_message` call. `on_consumed` in `InboxConsumer` is a single injection point covering both consume paths.

## Validation Findings (2026-08-11)

Code audit discovered four corrections to the original T05 design:

1. **`pool.consume_inbox` is NOT the single consume entry point.** `InboxFlushHook._flush` (inbox_flush.py:50) calls `InboxConsumer.consume` directly, bypassing `pool.consume_inbox`. Placing `on_consumed` in `pool.consume_inbox` would miss fold-in consumes → track deadlock. **Fix**: inject `on_consumed` into `InboxConsumer` instead.

2. **Quiesce must NOT check `inbox.count`.** Peer messages in inbox would cause false non-quiesce. **Fix**: quiesce = no DISPATCHED tracks + no running nodes (SQL + in-memory, no inbox access).

3. **TASK_REQUEST tracks must NOT close on consume.** Closing on consume creates a brief quiesced window before AGENT_RESULT arrives. **Fix**: TASK_REQUEST closes on AGENT_RESULT deliver (by invocation_id) or on_dispatch_end fallback.

4. **Tree binds to inbox dispatch mechanism (InboxPoller), NOT to hooks/react.** Dispatch lifecycle (`on_dispatch_start`/`on_dispatch_end`) in InboxPoller's `_dispatch_batch` entry + `_end_dispatch` shared cleanup. No success parameter — any complete dispatch exit = COMPLETED. fold-in doesn't trigger these (not through _dispatch_batch). Covers all agent types (ReAct + external) since all go through InboxPoller.

## Comments

### Resolved in this grilling session

- **Option B: Tree wraps, does not replace poller** — confirmed
- **Four integration points: deliver, on_consumed (in InboxConsumer), dispatch lifecycle (in InboxPoller _dispatch_batch + _end_dispatch), wait_quiesce (via signal_wakeup)** — confirmed
- **Poller core logic: zero changes** — confirmed (_loop/_tick/_maybe_start/_reconcile/single-flight unchanged; only _dispatch_batch +2 lines, _end_dispatch +5 lines new, two finallys -1 line each)
- **signal_wakeup is idempotent — tree doesn't query poller internals** — confirmed
- **on_consumed in InboxConsumer covers both fold-in and between-turn** — confirmed (corrected from original "pool.consume_inbox")
- **Quiesce = track + running, not inbox.count** — confirmed
- **TASK_REQUEST track closing deferred to AGENT_RESULT or on_dispatch_end** — confirmed
- **Tree binds to inbox dispatch mechanism, NOT hooks/react** — confirmed (on_dispatch_start/end in InboxPoller, not in pool.dispatch_envelope or ReAct hooks)
- **No success parameter — error exit = COMPLETED, tree stays ACTIVE** — confirmed (错误也是 complete, 用户可重试)
