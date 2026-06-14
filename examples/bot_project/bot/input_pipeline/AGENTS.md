<!-- Parent: ../AGENTS.md -->
<!-- Updated: 2026-06-14 -->

# input_pipeline

Converged user-input stage pipeline that processes incoming messages identically across IM (QQ) and WebUI channels. Both channels persist user messages to the same transcript store via the same mechanism, with control commands excluded from persistence.

## Architecture

```
                    Channel Adapter (S0)
                    produces seed envelope
                            │
              ┌─────────────┴─────────────┐
              ▼                           │
    IM entry (S4 first)        WebUI entry (S4 first)
              │                           │
              ▼                           ▼
    ┌─────────────────────────────────────────┐
    │ S4  SetChannelStage                      │  tag conversation with channel (runs first!)
    ├─────────────────────────────────────────┤
    │ S2  EnvironmentControlStage              │  IM-only: /cd, /pool, /exit, /pwd
    │ S3  SessionControlStage                  │  IM-only: /stop (cancel turn)
    ├─────────────────────────────────────────┤
    │ S5  ResolvePoolStage                     │  resolve pool + agent, persist UI choice
    │ S6  SkillParseStage                      │  validate /skillName, convert to XML
    │ S7  PersistUserMessageStage              │  write UserMessageEvent to transcript store
    │ S8  EnqueueStage                         │  build InputMessage, enqueue via ctx callback
    └─────────────────────────────────────────┘
```

- **S0 (adapter-side normalization)**: NOT a pipeline stage. Each channel adapter (QQ `_on_message`, WebSocket `_ws_send_message`) handles dedup, attachment download, content extraction and produces a seed `UserInputEnvelope`. This keeps channel-specific concerns in the adapter layer.
- **IM pipeline** (7 stages: S4→S2→S3→S5→S6→S7→S8): Full path for IM channels. S4 runs FIRST so ``ChannelRouterOutputAdapter`` can route command responses to the correct channel. S2/S3 intercept control commands before they reach persistence or the agent queue.
- **WebUI pipeline** (5 stages: S4→S5→S6→S7→S8): WebUI has UI-level controls for workspace/pool/session operations, so S2/S3 are skipped. Unknown `/command` typed in the chat box reaches S6 and terminates with an error notice.

## Pipeline Semantics

Each stage is an `InputStage` subclass. It receives a `UserInputEnvelope` and a `BotInputContext`, and returns a `StageResult`:

- **`Continue(value=envelope)`** — pass the (possibly modified) envelope to the next stage.
- **`Terminate(reason=..., response=...)`** — stop the pipeline. The envelope is not persisted and not enqueued. The `response` dict is surfaced to the user (pool switch notice, invalid skill error, etc.).

The pipeline runs stages in order and stops at the first `Terminate`. There is no branching or conditional skip beyond early termination.

## Stage Reference

### S2 — EnvironmentControlStage

`stages/environment_control.py`

IM-only. Intercepts `/cd <path>`, `/exit`, `/pwd`, and `/pool_name` before they reach persistence.

- `/cd`, `/exit`, and `/pwd` delegate to `ctx.command_adapter._try_intercept_control()` (framework-provided, no framework changes).
- `/pool_name` writes directly to `PoolSessionStore` and terminates with a user-facing notice.
- `/stop` is intentionally passed through — S3 owns it.
- Non-command messages pass through unchanged.

### S3 — SessionControlStage

`stages/session_control.py`

IM-only. Intercepts `/stop` to cancel the current turn via `ctx.command_adapter._try_intercept_control("/stop", full_session_id)`.

- Resolves `full_session_id` using the shared read-only `resolve_session_routing()` helper (does NOT persist — S5 owns persistence). This ensures `/stop` targets the conversation's current pool, not a bare conversation_id that would default to `main`.
- Non-`/stop` messages pass through unchanged.

### S4 — SetChannelStage

`stages/set_channel.py`

Both pipelines. **Runs first** in the IM pipeline (before S2/S3) so that ``ChannelRouterOutputAdapter`` can route command responses to the correct per-channel output adapter. Tags the conversation with its originating channel via `set_conv_channel(conversation_id, channel)`.

### S5 — ResolvePoolStage

`stages/resolve_pool.py`

Both pipelines. Resolves pool, agent, and full session ID for the conversation:

- If `explicit_pool` is set (WebUI pool selector), persists it to `PoolSessionStore` so `PoolRouter` routes correctly.
- If `explicit_pool` is None (IM), reads the stored pool from `PoolSessionStore` (falls back to `default_pool`).
- Stores `resolved_pool`, `resolved_agent`, and `full_session_id` in `envelope.metadata` using `RoutingMeta` enum keys (`RESOLVED_POOL`, `RESOLVED_AGENT`, `FULL_SESSION_ID`).

This makes S5 the single owner of pool resolution + persistence. Neither the WebUI server nor the QQ adapter resolves pools inline.

### S6 — SkillParseStage

`stages/skill_parse.py`

Both pipelines. Detects `/skillName` commands and validates against a pluggable `SkillRegistry`.

- Non-`/` messages pass through unchanged.
- Registered skills: stores `skill_xml` (LLM-ready XML form) and `skill_name` in `envelope.metadata`. The raw text stays in `envelope.content` for persistence.
- Unknown skills: terminates with a user-facing "Skill not found" notice. The message is NOT persisted.

**Extension point:** `SkillRegistry` is an ABC whose single `resolve(pool, name, content) -> ParsedSkill | None` method validates and converts a skill command. The concrete `PoolSkillManagerRegistry` wraps per-pool `SkillManager` instances — it calls `build_skill_command_xml()` (shared with `SkillCommandHandler`) to produce LLM-ready XML. The default is an empty registry (no skills recognized).

### S7 — PersistUserMessageStage

`stages/persist_user_message.py`

Both pipelines. Writes a `UserMessageEvent` to the workspace-scoped transcript store, keyed by `full_session_id`.

- Defense-in-depth: guards against control commands that leaked past S2/S3 using `content.startswith("/") and RoutingMeta.SKILL_XML not in envelope.metadata`. Valid skill commands (which have `SKILL_XML` set by S6) are correctly persisted; plain `/` commands without skill resolution are dropped.
- This is the **single** persistence path for user messages. No adapter or server writes `UserMessageEvent` directly.

### S8 — EnqueueStage

`stages/enqueue.py`

Both pipelines. Builds the final `InputMessage` and delivers it via `ctx.enqueue_message(msg)`.

- Uses `skill_xml` (from S6) if present, otherwise raw `envelope.content`.
- Carries `source` (= channel name, for `PoolRouter` agent address), `chat_id` (broker header), and attachment paths.
- Channel-agnostic: the physical queue (QQ `_message_queue` or WS queue) is injected through the context callback.

## Pool vs Workspace Scope

| Concept | Scope | How |
|---------|-------|-----|
| **Pool** | Per-conversation | `PoolSessionStore` keyed by `conversation_id`. S5 resolves and persists. Two channels never share pool state. |
| **Workspace** | Global (across pools) | `WorkspaceContext` with `cd`/`exit` via S2 (+ UI). Affects data directory, memory paths, terminal cwd. |

## Context Dependencies

`BotInputContext` (`context.py`) is the concrete `InputContext` injected into every stage. It holds:

| Field | Used by |
|-------|---------|
| `default_pool` | S5 (fallback when no stored pool) |
| `pool_session_store` | S2, S3 (read-only), S5 (resolve + persist) |
| `agent_pool_map` | resolution (agent_for_pool) |
| `agent_resolver` | S5 |
| `transcript_store` | S7 |
| `enqueue_message` | S8 |
| `command_adapter` | S2, S3 (`_try_intercept_control`) |

## No-Framework-Change Constraint

This pipeline reuses the framework's `InputAdapter._try_intercept_control` for `/cd`, `/exit`, and `/stop` handling. The call site is relocated into S2/S3 stages — no files under `framework/pipeline/` are modified. All new framework code lives under `framework/input_pipeline/` (envelope, stage, context, pipeline abstractions).

## Adding a New Stage

1. Create a class extending `InputStage` in `stages/`.
2. Implement `async def process(self, envelope: UserInputEnvelope, ctx: BotInputContext) -> StageResult`.
3. Return `Continue(value=envelope)` to pass through, or `Terminate(reason=..., response=...)` to stop.
4. Add the stage to `build_im_pipeline()` and/or `build_webui_pipeline()` in `assembly.py` at the correct position.
5. If the stage needs new context dependencies, add them to `BotInputContext` and update callers.

## Adding a New Channel

1. The channel adapter produces a seed `UserInputEnvelope` (S0 normalization — dedup, attachments, content extraction).
2. Decide which pipeline entry point to use: IM (S2..S8) or WebUI (S4..S8), or create a new assembly if the stage subset differs.
3. Build a per-channel `BotInputContext` with the channel's `enqueue_message` callback pointing at its own physical queue.
4. Wire: `result = await pipeline.handle(seed_envelope, ctx)`.

## Key Files

| File | Description |
|------|-------------|
| `context.py` | `BotInputContext` — concrete context with all stage dependencies |
| `assembly.py` | `build_im_pipeline()`, `build_webui_pipeline()` — stage ordering |
| `stages/environment_control.py` | S2 — `/cd`, `/pool`, `/exit` interception |
| `stages/session_control.py` | S3 — `/stop` turn cancellation |
| `stages/set_channel.py` | S4 — conversation channel tagging |
| `stages/resolve_pool.py` | S5 — pool/agent resolution + persistence |
| `stages/skill_parse.py` | S6 — skill validation + XML conversion |
| `stages/persist_user_message.py` | S7 — transcript store persistence |
| `stages/enqueue.py` | S8 — InputMessage construction + enqueue |
