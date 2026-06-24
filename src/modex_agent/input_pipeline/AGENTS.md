<!-- Parent: ../AGENTS.md -->
<!-- Updated: 2026-06-22 -->

# input_pipeline

## Purpose

Extensible user-input stage pipeline framework. Provides generic abstractions for building channel-agnostic message processing pipelines — envelope, stage, context, and pipeline runner. The business layer (`examples/bot_project/bot/input_pipeline/`) provides concrete stages, context, and assembly.

Decouple user-input processing from the main `AgentPipeline`. Each channel adapter produces a seed `UserInputEnvelope`, the pipeline runs stages in order, and the result is either a `Continue` (envelope reaches the agent queue) or a `Terminate` (intercepted — e.g., control command, invalid skill).

## Key Files

| File | Description |
|------|-------------|
| `__init__.py` | Public API exports — `UserInputEnvelope`, `InputStage`, `StageResult`, `Continue`, `Terminate`, `InputContext`, `UserInputPipeline` |
| `envelope.py` | `UserInputEnvelope` dataclass — normalized input with `conversation_id`, `content`, `channel`, `explicit_pool`, `metadata` dict, `attachments`. Also `AttachmentRef` for structured attachment references. |
| `stage.py` | `InputStage` ABC with `process(envelope, ctx) -> StageResult`. `StageResult` ABC with `should_continue()`. `Continue(value=envelope)` to pass through; `Terminate(reason, response)` to stop. |
| `context.py` | `InputContext` ABC — minimal interface with `default_pool` property. Concrete dependencies (pool store, transcript store, enqueue callback) are provided by the business layer. |
| `pipeline.py` | `UserInputPipeline` — runs stages in order, stops at first `Terminate`. Single `handle(envelope, ctx) -> StageResult` entry point. |

## Design

```
Channel Adapter (QQ, WebSocket, ...)
    │  produces seed UserInputEnvelope
    ▼
UserInputPipeline.handle(envelope, ctx)
    │
    ├── Stage 1 → Continue(envelope) → Stage 2 → ... → Stage N
    │                │
    │                └── Terminate(reason, response)  ← stop, no enqueue
    │
    ▼
StageResult
    ├── Continue → enqueue to agent
    └── Terminate → surface response to user (notice, error)
```

## Extension Points

- **`InputStage`**: implement `process()` to inspect or mutate the envelope. Return `Continue(value=envelope)` to pass through, or `Terminate(reason=..., response=...)` to stop.
- **`InputContext`**: subclass to inject business-layer dependencies (pool store, transcript store, command adapter, enqueue callback).
- **`UserInputEnvelope.metadata`**: dict scratch space for cross-stage communication (e.g., `RoutingMeta` keys in bot_project).

## Constraints

- **No framework changes for business features**: All business-specific stages, context, and pipeline assembly live in `examples/bot_project/bot/input_pipeline/`. The framework only provides the generic abstractions.
- **Channel-agnostic**: The pipeline does not know about QQ, WebSocket, or any specific channel. Channel differentiation happens via `envelope.channel` and per-channel entry points (IM vs WebUI stage subsets).

## Dependencies

### Internal
- No framework-internal dependencies beyond the standard library and `abc`.

### Used By
- `examples/bot_project/bot/input_pipeline/` — concrete stages, context, and assembly
- `examples/bot_project/bot/adapters/qq.py` — QQ adapter produces seed envelopes
- `examples/bot_project/bot/webui/server.py` — WebSocket adapter produces seed envelopes

<!-- MANUAL: -->
