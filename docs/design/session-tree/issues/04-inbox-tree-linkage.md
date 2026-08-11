# T04: Inbox-tree linkage — resolution

> Type: `wayfinder:grilling` (HITL)
> Status: **Resolved** by T01 (validated + corrected 2026-08-11)
> Blocks: T12

## Question

How to associate inbox entries with tree nodes? What schema? Does InboxMQ need migration?

## Resolution

**`message_id` is the linkage key, already unified end-to-end. No InboxMQ migration needed. No InboxProducer fix needed.**

### Validation finding: message_id is ALREADY preserved

**Correction from original design**: T04 originally claimed `InboxProducer.send` "may or may not preserve the envelope's message_id" and proposed a "one-line convergence fix." Code audit (producer.py:84) confirmed `InboxProducer.send` **already passes** `message_id=envelope.message_id` verbatim:

```python
# producer.py:79-86 (current code — verbatim)
msg = InboxMessage(
    session_id=session_id,
    source=envelope.source.name if envelope.source else "unknown",
    content=envelope.payload.get("content", ""),
    message_type=envelope.message_type,
    message_id=envelope.message_id,        # ← ALREADY preserves envelope.message_id
    metadata=metadata,
)
```

The `InboxMessage.message_id` default_factory (`uuid.uuid4().hex`, types.py:19) is only triggered when no value is passed — here a value IS passed. The round-trip is also preserved on consume (`bus.py:156`: `message_id=msg.message_id`).

**There is nothing to fix.** `envelope.message_id` = `InboxMessage.message_id` = `MessageTrack.message_id` is already unified end-to-end. The "one-line convergence fix" targets a line that is already correct.

### Linkage mechanism

```
tree.deliver(target_session_id, envelope):
  1. message_id = envelope.message_id  (generated at envelope construction)
  2. Create MessageTrack(message_id=message_id, target_session_id=target, ...)
  3. bus.send(target, envelope)  → InboxProducer.send preserves envelope.message_id
  → InboxMessage.message_id == envelope.message_id == MessageTrack.message_id

InboxConsumer.consume (with on_consumed callback):
  → messages = server.consume(session_id) → list[InboxMessage]
  → for each message:
      → on_consumed(session_id, message)  [callback injected into InboxConsumer]
      → tree looks up MessageTrack by message.message_id
      → if found and AGENT_RESULT: mark CONSUMED (close loop)
      → if found and TASK_REQUEST: do NOT close (wait for AGENT_RESULT or on_dispatch_end)
      → if not found (EXTERNAL_INPUT / peer): no-op
```

### Correction: on_consumed placement

**Original T04** (line 48) said: "InboxFlushHook._flush needs to call tree_manager.on_consumed(session_id, msg) for each consumed message" and "InboxFlushHook needs a reference to SessionTreeManager."

**Corrected**: `on_consumed` is injected into `InboxConsumer` (not InboxFlushHook). InboxFlushHook does NOT need a direct `SessionTreeManager` reference. See T05 (corrected) for the full rationale: `InboxFlushHook._flush` calls `InboxConsumer.consume` directly, so placing the callback there covers both fold-in and poller paths. InboxFlushHook stays unchanged.

### Why no InboxMQ migration

- `InboxMessage.metadata` is already a `dict[str, Any]` — carries arbitrary keys
- `message_id` is already a field on `InboxMessage` (types.py:19)
- `AgentMessageEnvelope.message_id` is already a field (envelope.py:61)
- `InboxProducer.send` already preserves `envelope.message_id` (producer.py:84) — no fix needed

### message_id flow (end-to-end, verified)

```
SubagentDispatchStrategy.build_envelope:
  envelope = AgentMessageEnvelope(message_id=uuid4().hex, ...)
  
tree.deliver(target, envelope):
  track = MessageTrack(message_id=envelope.message_id, ...)
  track_store.save(track)
  bus.send(target, envelope)
  
LocalAgentMessageBus.send → InboxProducer.send:
  inbox_msg = InboxMessage(message_id=envelope.message_id, ...)  # already preserved
  inbox.receive(session_id, inbox_msg)
  
InboxConsumer.consume (with on_consumed callback):
  messages = server.consume(session_id)
  for msg in messages:
    on_consumed(session_id, msg)  # msg.message_id matches track
  
SessionTreeManager.on_consumed:
  track = track_store.get_by_message_id(msg.message_id)
  if track and msg.message_type == AGENT_RESULT:
    track_store.update_status(track.track_id, CONSUMED)
  # TASK_REQUEST: do NOT close here (see T05 track closing rules)
```

## Comments

Resolved during T01 grilling. The key insight: `message_id` is the natural linkage key, already present on both envelope and inbox message, and already preserved by `InboxProducer.send`. No new schema, no new metadata field, no fix needed. The only convergence required is injecting `on_consumed` into `InboxConsumer` (see T05 corrected).
