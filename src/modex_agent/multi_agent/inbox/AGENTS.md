<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-06-22 | Updated: 2026-06-22 -->

# inbox

## Purpose

Asynchronous message inbox subsystem for multi-agent deferred delivery. Implements a message-queue (MQ) semantic for agent-to-agent communication: messages are persisted and consumed atomically (FIFO, exactly-once delivery). Provides wakeup signaling integration with the `AgentPool` consumer loop and global deduplication via delivered-ID tracking.

## Key Files

| File | Description |
|------|-------------|
| `server.py` | `InboxServer` ABC — defines the inbox contract: `receive()` (idempotent), `consume()` (atomic FIFO), `peek()`, `count()`, `clear()` |
| `server_local.py` | `LocalFileInboxServer` — file-based implementation using `pending.jsonl` per session + `FileDeliveredIdTracker`; each session has its own `asyncio.Lock` for single-process safety |
| `server_memory.py` | `InMemoryInboxServer` — in-memory implementation for testing; uses `asyncio.Lock` for thread safety |
| `producer.py` | `InboxProducer` — producer with local-cache dedup (`OrderedDict`, LRU eviction); converts `AgentMessageEnvelope` to `InboxMessage` and persists via `InboxServer.receive()` |
| `consumer.py` | `InboxConsumer` — consumer with local-cache dedup; filters out already-seen messages before returning from `consume()` |
| `tracker.py` | `DeliveredIdTracker` ABC + `FileDeliveredIdTracker` implementation; tracks delivered message IDs to prevent re-delivery, with LRU cap of 10,000 IDs per session |
| `types.py` | `InboxMessage` dataclass — session_id, source, content, message_type, message_id (auto UUID), timestamp, metadata |
| `__init__.py` | Re-exports `InboxMessage`, `InboxServer`, `LocalFileInboxServer`, `InMemoryInboxServer`, `InboxProducer`, `InboxConsumer`, `InboxFlushHook` |

## For AI Agents

### Working In This Directory
- The inbox is pure MQ — it does NOT handle wakeup signals or broker interaction; those are managed by upper layers (`AgentMessageBus`, `AgentPool`)
- Session dirs use a safe-encoded name derived from `session_id` (regex-sanitized, base64 for long IDs)
- All servers guarantee exactly-once delivery: a `message_id` seen before is silently dropped
- `LocalFileInboxServer` stores data under `{workspace}/{safe_session_id}/pending.jsonl` and `delivered_ids.json`
- Producer and consumer both have local LRU caches (default 1000 entries) for fast dedup without filesystem access
- `InboxFlushHook` (from `framework/hook/builtin`) integrates with the agent runtime to flush pending inbox messages

### Common Patterns
- Instantiate: `server = LocalFileInboxServer(Path("data/inbox"))` then `producer = InboxProducer(server)` / `consumer = InboxConsumer(server)`
- All public methods are async (file I/O or lock-based)
- Producer dedup cache uses `OrderedDict` with `move_to_end()` for LRU behavior

## Dependencies

### Internal
- `framework/multi_agent/envelope.py` — `AgentMessageEnvelope` consumed by producer to build `InboxMessage`
- `framework/hook/builtin/` — `InboxFlushHook` for runtime integration
- `framework/utils/file_io.py` — `read_json_robust` used by tracker for robust JSON loading

<!-- MANUAL -->
