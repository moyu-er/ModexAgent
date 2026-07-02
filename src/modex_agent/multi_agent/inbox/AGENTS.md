<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-06-22 | Updated: 2026-07-02 -->

# inbox

## Purpose

The message-queue (MQ) substrate for the poll-driven multi-agent messaging
model. One inbox per session, **owned by a pool**: each pool's `InboxServer`
lives under `<workspace_data>/inbox/<pool_name>/` and is consumed by that
pool's `InboxPoller` (the sole between-turn driver) and its fold-in hook.
Messages are persisted and consumed atomically (FIFO, exactly-once); the inbox
is pure transport — it holds messages, it does not orchestrate turns.

An inbox holds **all** turn-starting inputs for a session, distinguished by
`message_type`: inter-agent (`task_request` / `subagent_result` /
`agent_message`) and external (`external_input` = human DM / WebUI / approval
decision). The fold-in hook consumes with `only_types` filtering so
`external_input` stays pending for the next between-turn (a human DM is a
separate turn).

## Key Files

| File | Description |
|------|-------------|
| `server.py` | `InboxServer` ABC — the inbox contract: `receive()` (idempotent), `consume(session_id, limit, *, only_types=None)` (atomic FIFO; filtered-out messages stay pending), `peek()`, `count()`, `clear()`, `sessions_with_pending()` (poller enumeration), `list_sessions()` |
| `server_local.py` | `LocalFileInboxServer` — file-based implementation: `pending.jsonl` per session + `FileDeliveredIdTracker`; one `asyncio.Lock` per session for single-process safety; `sessions_with_pending` reads the original `session_id` back from the first pending record's `agent_session_id` metadata |
| `server_memory.py` | `InMemoryInboxServer` — in-memory implementation for tests |
| `producer.py` | `InboxProducer` — local-cache dedup (`OrderedDict`, LRU); converts `AgentMessageEnvelope` to `InboxMessage` and persists via `receive()`; stores `source_kind`/`source_name` in metadata so `consume` can rebuild the original `AgentAddress` (preserving the channel/human origin of `external_input`) |
| `consumer.py` | `InboxConsumer` — local-cache dedup; wraps a server's `consume` |
| `tracker.py` | `DeliveredIdTracker` ABC + `FileDeliveredIdTracker` — delivered-id tracking with LRU cap of 10,000 ids per session |
| `types.py` | `InboxMessage` dataclass — `session_id`, `source`, `content`, `message_type`, `message_id`, `timestamp`, `metadata` |
| `__init__.py` | Re-exports the public surface |

## For AI Agents

### Working In This Directory
- The inbox is pure MQ — it does NOT drive turns or touch the broker. Between-
  turn driving is the `InboxPoller` (`multi_agent/inbox_poller.py`); mid-turn
  fold-in is `InboxFlushHook` (`hook/builtin/`); cross-process wakeup is
  `LocalAgentMessageBus.send`'s broker `_inbox_wakeup`. The inbox only
  persists + consumes.
- Session dirs use a safe-encoded name derived from `session_id`
  (regex-sanitized; base64 for long ids).
- All servers guarantee exactly-once delivery: a `message_id` seen before is
  silently dropped.
- `consume(only_types=...)` is how fold-in avoids eating `external_input`:
  filtered messages are **not** consumed — they stay pending for the next
  between-turn.

### Common Patterns
- Instantiate: `server = LocalFileInboxServer(Path("data/inbox/<pool>"))` then
  `producer = InboxProducer(server)` / `consumer = InboxConsumer(server)`.
- All public methods are async (file I/O or lock-based).

## Dependencies

### Internal
- `modex_agent/multi_agent/envelope.py` — `AgentMessageEnvelope` consumed by the producer to build an `InboxMessage`
- `modex_agent/multi_agent/inbox_poller.py` — the poller (sole between-turn consumer)
- `modex_agent/hook/builtin/inbox_flush.py` — `InboxFlushHook` (mid-turn fold-in)
- `modex_agent/utils/file_io.py` — `read_json_robust` used by the tracker

<!-- MANUAL -->
