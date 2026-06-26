<!-- Updated: 2026-06-22 | Branch: develop_gyt -->

# Repository Guidelines

## Project Layout

`framework/` is the reusable agent framework (322+ Python files, 22 subdirectories). Key areas:

- `modex_agent/core/`: ABCs — `Agent[E]`, `ContentEmitter[E]`, `Tool`, `ContextManager`, graph engine (`Graph[R]`/`Node[R]`), skills, experience system, types.
- `modex_agent/agents/react/`: graph-based ReAct runtime (4-node: START→LLM→TOOL→END), approval suspension/resume, `RuntimeAssembler`.
- `modex_agent/agents/experience/`: `ExperienceReviewAgent` — ReAct agent that reviews conversations and creates/updates EXPERIENCE.md files.
- `modex_agent/core/experience/`: experience layer — `ExperienceManager`, `FileExperienceSource`, `ExperiencePromptBuilder`, `ExperienceCurator`, validation, metadata tracking.
- `modex_agent/memory/`: three-layer memory (session/archive/knowledge) + compression + governance + injection policies.
- `modex_agent/multi_agent/`: star-topology subagent coordination, `AgentPool`, inbox, `CommunicationTracker`, `AgentMessageBus`.
- `modex_agent/ioc/`: typed config (`AppConfig` via Pydantic) + 8 factory modules + `PoolConfig`.
- `modex_agent/runtime/`: `AgentRuntime`, `AgentRuntimeServices`, `TurnStateStore`, `RuntimeCommandStore`, typed enums/models.
- `modex_agent/pipeline/`: `AgentPipeline` end-to-end orchestration, I/O adapters, approval renderer, slash commands.
- `modex_agent/hook/` + `modex_agent/interceptor/` + `modex_agent/control/`: three-layer runtime model (observe/AOP/control).
- `modex_agent/tools/`: tool registry, executor, MCP integration, terminal system (pexpect/tmux/winpty backends, input guard, poll loop), overflow management.
- `modex_agent/commands/`: slash command processor with two-stage dispatch (pre-lock routing + in-lock execution).
- `modex_agent/sandbox/`: sandboxed execution adapters (Subprocess/Docker/E2B/Landlock).

`examples/bot_project/` is the primary end-to-end reference (Pool + Pipeline modes, WebUI React frontend, QQ adapter). Framework-generic behavior in `framework/`; business wiring in `examples/`.

## Commands

- `uv pip install -e ".[dev,llm,storage,gateway]"`: install
- `pytest tests/unit/ -v`: unit tests
- `pytest tests/integration/ -v -m integration`: integration tests
- `ruff check framework/ tests/`: lint
- `ruff format framework/ tests/`: format
- `mypy framework/`: type check

## Type Safety Rules (from rules/type-safety.md)

1. **Enums/constants over raw strings** for categories, roles, states, protocol values. Use `MessageRole`, `MessageType`, `FinishReason`, `DefaultValues`.
2. **Typed structures over loose dicts**. Use existing dataclasses: `ChatMessage`, `ToolCall`, `LLMResponse`, `InputMessage`, `OutputMessage`.
3. **Typed signatures**. No bare `Any`, `list`, `dict`, `object`, `list[Any]` in framework-facing APIs. Declare parameter and return types.
4. **ABCs before implementations**. No concrete dependency where a pluggable contract exists. All extension points use ABCs (zero Protocols).
5. **Framework vs examples separation**. `framework/` = reusable behavior; `examples/` = business wiring. No example-specific config in framework.
6. **No dynamic access** (`getattr`/`hasattr`) except at real extension boundaries. Prefer explicit typed attributes and method calls.

## Architecture Rules (from rules/architecture.md)

- Python 3.12+, `from __future__ import annotations` in all framework modules.
- `Agent[E]`, `ContentEmitter[E]` with `TypeVar("E", bound=AgentEvent)`.
- Per-turn state in `runtime.state` (typed `ReActTurnState`), not instance attributes or `ctx.metadata`.
- Frozen dataclasses for config/value objects; runtime objects hold state/connections.
- `MessageRole` lives in `modex_agent.core.types.MessageRole`.
- `GraphInterrupt` for approval suspension — never catch and swallow it.
- `TurnCustomKey` enum for per-turn custom state keys in `TurnStateBase.custom`.

## Memory Rules

- Compression mutates persisted session/archive memory via lifecycle hooks.
- Governance mutates only the LLM input copy before model calls. Never write governance output back to session.
- Tool-call chains must stay structurally legal: don't split `assistant.tool_calls` from matching `tool` results.
- `archive=None` is standard session-only mode for subagent memory.
- Subagent session memory is temporary; clear after subagent finishes.
- Memory scopes: Session, User, Tenant, Agent, Channel, Chat, PeerPair, Composite, Global.
- Pruned catalog: cleanup writes pruned messages to `pruned/{session_id}/`, injection policy injects XML catalog at priority 85. Works independently of archive. All agents (main + subagent) get pruned injection.

## Multi-Agent Communication Rules

- Star topology: subagents communicate only through main agent. `subagent_validator.py` enforces at registration.
- Communication is exposed as a single tool: `send_to_agent`. The framework decides internally whether to use broker delivery, inbox delivery, or a new isolated subagent session.
- `AgentMessageBus` is the primary async channel. `InboxProducer`/`InboxConsumer` wrap `InboxServer` with local-cache dedup.
- `CommunicationTracker` provides sideband memory: send/acknowledge bracket matching prevents memory compression from silently dropping pending communications.
- Session ID format: `{conversation_id}:{agent_name}[:{invocation_id}]` (via `DefaultSessionIdStrategy`).
- `AgentPool` manages resident agent lifecycle: consumer loop, inbox wakeup polling, per-session locks, TTL + LRU session eviction.
- `SubagentAutoSendHook` safety net: auto-forwards final output to parent if LLM forgets to use communication tools.
- Each subagent gets isolated Memory/ToolManager/SkillManager. Subagent memory is `RestrictedInjectionPolicy` (session-only, limited context window).

## Approval Architecture Rules (CRITICAL)

1. **One approval path only.** `ToolNode` → `ApprovalTransaction` → `TurnSnapshot` → `ApprovalRenderer`. Do NOT add approval logic to interceptors, hooks, or control consumers.
2. **`ControlDrainInterceptor` must not drain `APPROVAL_RESPONSE`.** Drain set is for cancel/inject/config only.
3. **`ApprovalRuntime` is a policy service, not a state owner.** Owns classifier + deny_policy. State lives in `ApprovalTransaction` inside `ReActTurnState`.
4. **Deny policy defaults to `TOOL_RESULT_ONLY`.** Rejection returns tool errors with `deny_reason`, continues ReAct loop. `CANCEL_TURN` is a configurable override.
5. **`deny_reason` lives on `ApprovalTransaction.deny_reason`.** Do not read from `ctx.metadata` or other locations.

## Testing

Unit tests under `tests/unit/` (mirrors `framework/` structure), framework-level tests under `tests/framework/`, integration tests under `tests/integration/`. Write/update tests before production code when practical. Absolute imports (`from modex_agent.xxx`). Mock `LLMProvider`, `ControlChannel` — never hit real APIs.

## Documentation

Architecture Decision Records (ADRs) in `docs/adr/` and superpowers documentation in `docs/superpowers/`. Read relevant ADRs before making significant architectural changes.

## Key Files

| File | Location | Description |
|------|----------|-------------|
| Root Guidelines | `AGENTS.md` | This file — project overview and conventions |
| Framework Overview | `framework/AGENTS.md` | All 24 framework modules with file counts and responsibilities |
| Tests Overview | `tests/AGENTS.md` | Unit, framework, and integration test suites |
| Docs Overview | `docs/AGENTS.md` | ADRs and superpowers documentation |
| Bot Reference | `examples/bot_project/AGENTS.md` | End-to-end reference implementation |
