# Repository Guidelines

## Project Layout

`framework/` is the reusable agent framework. Key areas:

- `framework/core/`: ABCs — Agent[E], Emitter, Tool, ContextManager, graph engine, skills, types.
- `framework/agents/react/`: graph-based ReAct runtime (4-node: START→LLM→TOOL→END).
- `framework/memory/`: session/archive/knowledge memory, compression, governance.
- `framework/multi_agent/`: star-topology peer/subagent coordination, inbox, communication tracker.
- `framework/ioc/`: typed config (`AppConfig`) + factory layer.
- `framework/runtime/`: `AgentRuntime`, `TurnStateStore`, `RuntimeCommandStore`.
- `framework/pipeline/`: `AgentPipeline`, adapters, approval renderer.
- `framework/hook/` + `framework/interceptor/` + `framework/control/`: three-layer runtime model.

`examples/bot_project/` is the primary end-to-end reference. Framework-generic behavior in `framework/`; business wiring in `examples/`.

## Commands

- `uv pip install -e ".[dev,llm,storage,gateway]"`: install
- `pytest tests/unit/ -v`: unit tests
- `pytest tests/integration/ -v -m integration`: integration tests
- `ruff check framework/ tests/`: lint
- `ruff format framework/ tests/`: format
- `mypy framework/`: type check

## Coding Rules

- Python 3.12+, `from __future__ import annotations` in all framework modules.
- Enums/constants over raw strings for categories, roles, states, protocol values.
- Typed structures over loose dicts (`ChatMessage`, `ToolCall`, `LLMResponse`, `InputMessage`, `OutputMessage`).
- Typed parameters and returns; avoid bare `Any`, `list`, `dict`, `object` in framework APIs.
- Protocols/ABCs for extension points; no concrete dependency where a pluggable contract exists.
- Framework behavior in `framework/`, business wiring in `examples/`; no example-specific config in framework.
- Avoid `getattr`/`hasattr` unless at a real extension boundary.
- `MessageRole` lives in `framework.core.types.MessageRole`.
- Per-turn state in `runtime.state` (typed turn state), not instance attributes or `ctx.metadata`.
- Frozen dataclasses for config; runtime = state/connections.
- `Agent[E]`, `ContentEmitter[E]` with `TypeVar("E", bound=AgentEvent)`.

## Memory Rules

- Compression mutates persisted session/archive memory via lifecycle hooks.
- Governance mutates only the LLM input copy before model calls. Never write governance output back to session.
- Tool-call chains must stay structurally legal: don't split `assistant.tool_calls` from matching `tool` results.
- `archive=None` is standard session-only mode for peer/subagent memory.
- Subagent session memory is temporary; clear after subagent finishes.
- Memory scopes: Session, User, Tenant, Agent, Channel, Chat, PeerPair, Composite, Global.

## Multi-Agent Communication Rules

- Star topology: peers communicate only through main agent. `peer_validator.py` enforces at registration.
- Three communication tools: `send_message` (sync broker), `send_message_async` (inbox-based, deferred), `dispatch_task` (isolated invocation session).
- `AgentMessageBus` is the primary async channel. `InboxProducer`/`InboxConsumer` wrap `InboxServer` with local-cache dedup.
- `CommunicationTracker` provides sideband memory: send/acknowledge bracket matching prevents memory compression from silently dropping pending communications.
- Session ID format: `{conversation_id}:{agent_name}` (via `DefaultSessionIdStrategy`). `dispatch_task` appends `:{invocation_id}` for isolated sessions.
- `AgentPool` manages resident agent lifecycle: consumer loop, inbox wakeup polling, per-session locks, TTL + LRU session eviction.
- `SubagentAutoSendHook` safety net: auto-forwards final output to parent if LLM forgets to use communication tools.
- Each peer gets isolated Memory/ToolManager/SkillManager. Peer memory is `RestrictedInjectionPolicy` (session-only, limited context window).

## Testing

Focused regression tests under `tests/unit/`. Write/update tests before production code when practical. Absolute imports (`from framework.xxx`).

## Approval Architecture Rules (CRITICAL)

1. **One approval path only.** `ToolNode` → `ApprovalTransaction` → `TurnSnapshot` → `ApprovalRenderer`. Do NOT add approval logic to interceptors, hooks, or control consumers.

2. **`ControlDrainInterceptor` must not drain `APPROVAL_RESPONSE`.** Drain set is for cancel/inject/config only.

3. **`ApprovalRuntime` is a policy service, not a state owner.** Owns classifier + deny_policy. State lives in `ApprovalTransaction` inside `ReActTurnState`.

4. **Deny policy defaults to `TOOL_RESULT_ONLY`.** Rejection returns tool errors with `deny_reason`, continues ReAct loop. `CANCEL_TURN` is a configurable override.

5. **`deny_reason` lives on `ApprovalTransaction.deny_reason`.** Do not read from `ctx.metadata` or other locations.
