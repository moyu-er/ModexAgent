<!-- Parent: ../AGENTS.md -->
<!-- Updated: 2026-07-15 -->

# inbox

## Purpose

The message-queue (MQ) substrate for the poll-driven multi-agent messaging
model. One inbox per session, **owned by a pool**: each pool's `InboxMQ`
lives under `<workspace_data>/inbox/<pool_name>/` (file backend) or in the
workspace `state.db` (SQLite backend) and is consumed by that pool's
`InboxPoller` (the sole between-turn driver) and its fold-in hook.
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
| `server.py` | `InboxMQ` ABC — the inbox contract: `receive()` (idempotent), `consume(session_id, limit, *, only_types=None)` (atomic FIFO; filtered-out messages stay pending), `peek()`, `count()`, `clear()`, `sessions_with_pending()` (poller enumeration), `list_sessions()`, `deliver()` (sync, cross-process CLI), `reap_expired()`. `InboxServer` is kept as a deprecated alias. (The former `wakeup()`/`wait_wakeup()` per-session methods were removed — between-turn wakeup is now a pool-level `asyncio.Event` on `InboxPoller`, signalled from `LocalAgentMessageBus.send`.) |
| `server_local.py` | `LocalFileInboxMQ` — file-based implementation: `pending.jsonl` per session + `FileDeliveredIdTracker` (internal); one `asyncio.Lock` per session for single-process safety; `sessions_with_pending` reads the original `session_id` back from the first pending record's `agent_session_id` metadata. `LocalFileInboxServer` is kept as a deprecated alias |
| `server_memory.py` | `InMemoryInboxServer` — in-memory implementation for tests (extends `InboxMQ`) |
| `producer.py` | `InboxProducer` — local-cache dedup (`OrderedDict`, LRU); converts `AgentMessageEnvelope` to `InboxMessage` and persists via `receive()`; stores `source_kind`/`source_name` in metadata so `consume` can rebuild the original `AgentAddress` (preserving the channel/human origin of `external_input`) |
| `consumer.py` | `InboxConsumer` — shared reserve/consume/acknowledge/release receipt lifecycle for Poller and fold-in. Unacknowledged work persists through the pool's SessionRegistry; live claims prevent nested consumers from processing a held batch twice. `peek` merges saved receipts and MQ intake. |
| `tracker.py` | `DeliveredIdTracker` ABC + `FileDeliveredIdTracker` — **deprecated** (T11). Delivered-id tracking is now internal to `InboxMQ`; the ABC is kept only for backwards compatibility. `FileDeliveredIdTracker` remains as a private helper used by `LocalFileInboxMQ` |
| `types.py` | `InboxMessage` and `SessionWork` frozen Pydantic models. `SessionWork.pending` contains reserved InboxMessages; `SESSION_WORK_METADATA_KEY` names their existing SessionRegistry metadata slot. |
| `__init__.py` | Re-exports the public surface |

The SQLite backend adapter is `SqliteInboxMQ` in `modex_agent.persistence.adapters.inbox_mq`. It implements the same `InboxMQ` ABC against the workspace `state.db`, closing the cross-process atomicity gap the file backend has (T20).

## For AI Agents

### Working In This Directory
- The inbox is pure MQ — it does NOT drive turns or touch the broker. Between-
  turn driving is the `InboxPoller` (`multi_agent/inbox_poller.py`, event-driven
  via a pool-level `asyncio.Event` with a tick fallback); mid-turn fold-in is
  `InboxFlushHook` (`hook/builtin/`). Wakeup is signalled from
  `LocalAgentMessageBus.send` (the single convergence point of all inbox
  writers) directly to the poller — no broker `_inbox_wakeup` anymore. The
  inbox only persists + consumes.
- `InboxMQ` is the primary ABC name. `InboxServer` is a deprecated alias kept
  during the transition (T11). New code should use `InboxMQ`.
- `deliver()` is a sync method for cross-process CLI use (`modexctl send`
  opens a short-lived SQLite connection and calls `InboxMQ.deliver()`).
- Session dirs use a safe-encoded name derived from `session_id`
  (regex-sanitized; base64 for long ids).
- All servers guarantee exactly-once delivery: a `message_id` seen before is
  silently dropped.
- `consume(only_types=...)` is how fold-in avoids eating `external_input`:
  filtered messages are **not** consumed — they stay pending for the next
  between-turn.
- Every receiver acknowledges each message only after processing succeeds and
  releases its batch's claims in `finally`. Releasing an unacknowledged message
  makes its saved receipt available for reentry, without another MQ delivery.
- `set_on_consumed` fires on acknowledgement, not on destructive MQ removal.
  `SessionTreeManager` wires the shared registry through the bus at construction.

### Common Patterns
- Instantiate: `server = LocalFileInboxMQ(Path("data/inbox/<pool>"))` then
  `producer = InboxProducer(server)` / `consumer = InboxConsumer(server)`.
- All public methods are async except `deliver()` (sync, for CLI use).

## Dependencies

### Internal
- `modex_agent/multi_agent/envelope.py` — `AgentMessageEnvelope` consumed by the producer to build an `InboxMessage`
- `modex_agent/multi_agent/inbox_poller.py` — the poller (sole between-turn consumer)
- `modex_agent/hook/builtin/inbox_flush.py` — `InboxFlushHook` (mid-turn fold-in)
- `modex_agent/utils/file_io.py` — `read_json_robust` used by the tracker

<!-- MANUAL -->
