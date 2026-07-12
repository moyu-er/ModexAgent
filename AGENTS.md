<!-- Updated: 2026-06-22 | Branch: develop_gyt -->

# Repository Guidelines

## Project Layout

`src/modex_agent/` is the reusable agent framework (src layout — see ADR-0003). Key areas:

- `modex_agent/core/`: ABCs — `Agent[E]`, `ContentEmitter[E]`, `Tool`, `ContextManager`, graph engine (`Graph[R]`/`Node[R]`), skills, experience system, types.
- `modex_agent/agents/react/`: graph-based ReAct runtime (4-node: START→LLM→TOOL→END), approval suspension/resume, `TieredToolApprovalClassifier`.
- `modex_agent/agents/experience/`: `ExperienceReviewAgent` — ReAct agent that reviews conversations and creates/updates EXPERIENCE.md files.
- `modex_agent/core/experience/`: experience layer — `ExperienceManager`, `FileExperienceSource`, `ExperiencePromptBuilder`, `ExperienceCurator`, validation, metadata tracking.
- `modex_agent/memory/`: three-layer memory (session/archive/knowledge) + compression + governance + injection policies.
- `modex_agent/multi_agent/`: star-topology subagent coordination, `AgentPool`, inbox, `AgentMessageBus`.
- `modex_agent/ioc/`: typed config (`AppConfig` via Pydantic) + 7 factory modules. Pool configuration lives in `modex_agent/multi_agent/pool_config/`.
- `modex_agent/runtime/`: `AgentRuntime`, `AgentRuntimeServices`, `TurnStateStore`, typed enums/models.
- `modex_agent/pipeline/`: `AgentPipeline` end-to-end orchestration, I/O adapters, approval renderer, slash commands.
- `modex_agent/hook/` + `modex_agent/interceptor/` + `modex_agent/control/`: three-layer runtime model (observe/AOP/control).
- `modex_agent/tools/`: tool registry, executor, MCP integration, terminal system (pexpect/tmux/winpty backends, input guard, poll loop), overflow management.
- `modex_agent/commands/`: slash command processor with two-stage dispatch (pre-lock routing + in-lock execution).
- `modex_agent/sandbox/`: sandboxed execution adapters (Subprocess/Docker/E2B/Landlock).

`examples/bot_project/` is the primary end-to-end reference (Pool mode, WebUI React frontend, multi-channel IM adapters: QQ + Telegram). Framework-generic behavior in `src/modex_agent/`; business wiring in `examples/`.

## Commands

- `uv pip install -e ".[dev,llm,storage,gateway]"`: install
- `pytest tests/unit/ -v`: unit tests
- `pytest tests/integration/ -v -m integration`: integration tests
- `ruff check src/modex_agent tests/`: lint
- `ruff format src/modex_agent tests/`: format
- `mypy src/modex_agent`: type check

## Type Safety Rules

Full rules: `rules/type-safety.md` — read before any framework code change.

Core principles: enums/constants over raw strings (rule 1); typed structures over loose dicts (rule 2); declared parameter and return types, no bare `Any`/`list`/`dict` in framework-facing APIs (rule 3); ABCs before implementations, zero Protocols (rules 4, 7); framework vs examples separation (rule 5); no `getattr`/`hasattr`/`isinstance` except at real extension boundaries (rules 6, 9); exact field match on typed objects (rule 8).

**Pydantic-first structured data (rules 10–16):** cross-module internal data structures MUST be `BaseModel`, not `dict`/`TypedDict`/`@dataclass`. Frozen `@dataclass` is only a leaf value-object escape hatch (single-module, no serialization, no nested validation). Serialization boundaries go through `model_dump()`/`model_validate()`, never hand-rolled `json.dumps`. Nested structured fields are typed models, not `dict[str, Any]`. Discriminated unions over `Union[...]` of models. All public framework types are importable and documented in `AGENTS.md`.

## Architecture Rules

Full rules: `rules/architecture.md` — read before any module design or refactor.

Deep modules whose interface is simpler than implementation (rules 1, 3). Apply the deletion test before extracting or keeping a module (rule 4). Interface is the test surface (rule 5). One adapter is hypothetical; two make a real seam (rule 6). Preserve locality (rule 7). Name modules after domain concepts, not machinery (rule 8). Framework vs examples separation (rule 9). ABCs for interfaces, not Protocols (rule 10). Per-turn state in `runtime.state` (`ReActTurnState`), not instance attributes or `ctx.metadata` (rule 11). Config/value objects use Pydantic `BaseModel` with `frozen=True`; runtime objects with state/connections are regular classes (rule 12). `GraphInterrupt` for approval suspension — never catch and swallow it; approval state in `ApprovalTransaction` (rule 13). Centralize domain constants/enums, replace raw strings (rule 14).

Shared vocabulary: module, interface, depth, seam, adapter, leverage, locality (rule 2).

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
- Session ID format: `{prefix}.{agent_name}` (dot-separated; via `SessionIdFactory` / `DefaultSessionIdStrategy`). Subagent runs carry an `invocation_id` in `AgentContext` metadata, not in the session id.
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

Unit tests under `tests/unit/` (mirrors `src/modex_agent/` structure), architecture guard tests under `tests/architecture/`, integration tests under `tests/integration/`. Write/update tests before production code when practical. Absolute imports (`from modex_agent.xxx`). Mock `LLMProvider`, `ControlChannel` — never hit real APIs.

## Documentation

Architecture Decision Records (ADRs) in `docs/adr/` and superpowers documentation in `docs/superpowers/`. Read relevant ADRs before making significant architectural changes.

## Key Files

| File | Location | Description |
|------|----------|-------------|
| Root Guidelines | `AGENTS.md` | This file — project overview and conventions |
| Framework Overview | `src/modex_agent/AGENTS.md` | All framework modules with file counts and responsibilities |
| Tests Overview | `tests/AGENTS.md` | Unit, framework, and integration test suites |
| Docs Overview | `docs/AGENTS.md` | ADRs and superpowers documentation |
| Bot Reference | `examples/bot_project/AGENTS.md` | End-to-end reference implementation |

## Agent skills

### Issue tracker

Issues live as local markdown under `docs/design/<feature>/`. External PRs are not a triage surface. See `docs/agents/issue-tracker.md`.

### Triage labels

Canonical defaults: needs-triage, needs-info, ready-for-agent, ready-for-human, wontfix. See `docs/agents/triage-labels.md`.

### Domain docs

Multi-context: `CONTEXT-MAP.md` at root points to per-context `CONTEXT.md` files. See `docs/agents/domain.md`.
