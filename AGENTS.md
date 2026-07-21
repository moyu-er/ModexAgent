<!-- Updated: 2026-07-17 | Branch: develop_gyt -->

# Repository Guidelines

## Project Layout

`src/modex_agent/` is the reusable agent framework (src layout — see ADR-0003). Key areas:

- `modex_agent/core/`: ABCs — `Agent[E]`, `ContentEmitter[E]`, `Tool`, `ContextManager`, skills, experience system, types. (The graph engine was extracted to the standalone `modex_graph` package per ADR-0033; the old `core/graph/` directory is deleted.)
- `src/modex_graph/`: standalone graph engine package (sibling of `modex_agent`, depends only on Pydantic + stdlib). Provides `Graph[S]`/`Node[S]`/`CompiledGraph`/`GraphEngine`/`GraphContext`/`NodeResult`/`Command`/`Task`/`BaseChannel`/`LastValue`/`ReducerChannel`/`GraphState`/`GraphRuntime`/`GraphBubbleUp` family. See ADR-0033.
- `modex_agent/agents/react/`: graph-based ReAct runtime (4-node: START→LLM→TOOL→END), approval suspension/resume, `TieredToolApprovalClassifier`. Built on `modex_graph`.
- `modex_agent/agents/external_coding/`: `ExternalCodingAgent` harness for Pi/OpenCode, provider-neutral streaming/events, session mapping, env/prompt injection, and cross-platform process-tree ownership.
- `modex_agent/agents/experience/`: `ExperienceReviewAgent` — ReAct agent that reviews conversations and creates/updates EXPERIENCE.md files.
- `modex_agent/core/experience/`: experience layer — `ExperienceManager`, `FileExperienceSource`, `ExperiencePromptBuilder`, `ExperienceCurator`, validation, metadata tracking.
- `modex_agent/memory/`: three-layer memory (session/archive/core) + compression + governance + injection policies. The Core Memory layer (formerly "Knowledge"; renamed per ADR-0035) holds the `SOUL.md` / `USER.md` / `MEMORY.md` triple. Storage is backend-pluggable via split store ABCs (`MessageStore`/`KVStore`/`CursorStore`/`ArchiveStore`) composed by `MemoryStoreBundle`.
- `modex_agent/persistence/`: hybrid persistence layer (ADR-0023). `ConnectionManager` + `MigrationRunner` own one per-workspace SQLite DB; SQLite adapters implement the split store ABCs and the runtime-state ABCs. `PersistenceBackend` enum (`FILE` / `SQLITE`) and `PersistenceConfig` drive IOC factory selection. SQLite is the bot's default; file remains the framework default.
- `modex_agent/multi_agent/`: star-topology subagent coordination, `AgentPool`, inbox, `AgentMessageBus`.
- `modex_agent/ioc/`: typed config (`AppConfig` via Pydantic) + 7 factory modules. Pool configuration lives in `modex_agent/multi_agent/pool_config/`.
- `modex_agent/runtime/`: `AgentRuntime`, `AgentRuntimeServices`, `TurnStateStore`, typed enums/models.
- `modex_agent/pipeline/`: `AgentPipeline` end-to-end orchestration, I/O adapters, approval renderer, slash commands.
- `modex_agent/hook/` + `modex_agent/interceptor/` + `modex_agent/control/`: three-layer runtime model (observe/AOP/control).
- `modex_agent/tools/`: tool registry, executor, MCP integration, terminal system (pexpect/tmux/winpty backends, input guard, poll loop), overflow management.
- `modex_agent/commands/`: slash command processor with two-stage dispatch (pre-lock routing + in-lock execution).
- `modex_agent/sandbox/`: sandboxed execution adapters (Subprocess/Docker/E2B/Landlock).
- `modex_agent/media/`: attachment/media handling (ADR-0013) — `MediaStore` ABC, MIME classification, security gate, storage routing.
- `modex_agent/cli/`: framework-side CLI shim — `modexbot`/`modexctl` facade for external coding agent peer messaging (see `src/modexctl/` for the registered `modexctl` console script).
- Additional modules: `approval/`, `adapters/`, `messaging/`, `workspace/`, `providers/`, `plugins/`, `input_pipeline/`, `trace/`, `registry/`, `utils/` — see `src/modex_agent/AGENTS.md` for the exhaustive module table (26 modules total).

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
- Memory scopes: Session, User, Tenant, Agent, Channel, Chat, Composite, Global (PeerPair removed in T04).
- Pruned catalog: cleanup writes pruned messages to `pruned/{session_id}/`, injection policy injects XML catalog at priority 85. Works independently of archive. All agents (main + subagent) get pruned injection.

## Persistence Architecture (ADR-0023)

Hybrid persistence: per-workspace SQLite (`<workspace>/.modex/state.db`) for transactional structured state, plus files for human-editable and binary data (core memory markdown, archive documents, media bytes, pruned JSONL, overflow chunks, config YAML). No data migration from files to DB; users opt in by setting `persistence.backend`.

- **`PersistenceBackend`** enum (`FILE` / `SQLITE`) + **`PersistenceConfig`** (frozen Pydantic) drive IOC factory selection. `SQLITE` is the bot's default; `FILE` remains the framework default.
- **`ConnectionManager`** owns one private `aiosqlite.Connection` per workspace DB, serializes adapter operations via a manager-owned lock, and runs migrations on open. `MigrationRunner` applies ordered SQL files tracked by a `schema_migrations` table (one explicit transaction per migration; no transaction-control statements in scripts). Two `DatabaseKind` streams: `WORKSPACE` (per-workspace) and `REGISTRY` (global).
- **Split store ABCs** (`MessageStore`/`KVStore`/`CursorStore`/`ArchiveStore` + `MemoryStoreBundle`) replace the deleted `MemoryStorage` god-interface. File backend: one `DefaultScopedStorage` implements all four. SQLite backend: four independent `Sqlite*Store` adapters.
- **New / evolved runtime-state ABCs:** `InboxMQ` (evolved from `InboxServer`; adds sync `deliver()` for CLI cross-process use; `DeliveredIdTracker` merged in as internal), `PoolRoutingStore` (extracted from `PoolSessionStore`), `ExternalSessionMapStore` (extracted from the former `ExternalSessionStore`, now removed), `WorkspaceRegistryStore` (deepened from `RegistryStore`), `ApprovalAuditStore` (new append-only audit log), `SessionArtifactCleaner` (DB + file cascade deletion). Old names are kept as deprecated aliases during transition.
- **`ContextForkBuilder`** simplified to pure computation (T18): `build()` queries the parent session's `MessageStore` and returns fork XML directly. No fork files written to disk; the cleanup registry is removed. `register_for_cleanup`/`cleanup` are retained as no-ops for caller compatibility.
- **Terminal state store removed** (T19): `JsonTerminalStateStore` and the `save_state()`/`load_state()` path in `BaseTerminalManager` were dead code and are deleted.
- **`RecordScope`** (frozen Pydantic) carries all scope dimensions; `canonical()` is the DB scope-key source, `to_path_segment()` drives file paths. `Scope` ABC replaces `MemoryScope`; `build_scope(dims)` is the factory. `PeerPairScope` removed (T04).

## Phase 1 Schema Optimization (ADR-0028 ~ ADR-0031)

Four ADRs landed together as Phase 1 of the persistence schema optimization:

- **ADR-0028 (RecordScope base/subclass split + pool removal):** `pool` field removed from the framework base `RecordScope` (now `extra="forbid"`); business layer subclasses via `BotRecordScope(pool=...)` in `examples/bot_project/bot/scope.py`. Subclass extra fields are auto-registered via `__init_subclass__` keyed by frozenset of extra field names. `canonical()` stamps `__scope_type__` with sorted comma-joined extra field names (content-based, not class-name-based) so structurally identical subclasses in different modules (e.g. `BotRecordScope` and modexctl's `_PoolScopedRecordScope`) produce identical scope_keys. `from_canonical()` dispatches via O(1) registry lookup.
- **ADR-0029 (Epoch-millisecond timestamp unification):** all DB timestamp columns are `INTEGER` milliseconds (not TEXT ISO strings). `utils/time.py` exports `now_ms()`/`now_s()` as the single source of truth. `ChatMessage.created_at` stays ISO string at the API surface but round-trips through the SQLite adapter as int ms (Supplement note in ADR-0029).
- **ADR-0030 (ColumnProjection abstraction):** `persistence/column_projection.py` provides declarative field-mapping that splits a dict into typed columns + residual JSON. `ContentCodec` handles the str-vs-list[dict] content duality. All SQLite adapters use this for INSERT/SELECT.
- **ADR-0031 (Schema simplification):** `scope` column removed (only `scope_key` remains); dead tables (`inbox_dead_letter`, `workspace_meta`) dropped; `inbox_topics` minimized; `message_id`/`content` nullable (framework API allows None); `role` CHECK extended to 6 values (user/assistant/system/tool/agent/pending).

## Multi-Agent Communication Rules

- Star topology: subagents communicate only through main agent. `subagent_validator.py` enforces at registration.
- Communication is exposed as a single tool: `send_to_agent`. The framework decides internally whether to use broker delivery, inbox delivery, or a new isolated subagent session.
- `AgentMessageBus` is the primary async channel. `InboxProducer`/`InboxConsumer` wrap `InboxMQ` (the evolved `InboxServer`; `InboxServer` is a deprecated alias) with local-cache dedup.
- Session ID format: `{prefix}.{agent_name}` (dot-separated; via `SessionIdFactory` / `DefaultSessionIdStrategy`). Subagent runs carry an `invocation_id` in `AgentContext` metadata, not in the session id.
- `AgentPool` manages resident agent lifecycle: consumer loop, inbox wakeup polling, per-session locks, TTL + LRU session eviction.
- `SubagentAutoSendHook` safety net: auto-forwards final output to parent if LLM forgets to use communication tools.
- Each subagent gets isolated Memory/ToolManager/SkillManager. Subagent memory is `RestrictedInjectionPolicy` (session-only, limited context window).
- External coding agents (Pi, OpenCode) participate in ADR-0019 peer topology as NORMAL main agents of their own dedicated pools (`pool_pi`, `pool_opencode`). They communicate back through the `modexctl send` CLI (calls the target workspace's synchronous `InboxMQ.deliver()` with XML-wrapped `<agent_message>` content), not through `send_to_agent` (which they do not have). FILE delivery appends the target inbox; SQLite delivery uses a short-lived transaction against `state.db`. Other agents talk to them via the standard `send_to_agent` tool. See ADR-0022 and `docs/design/external-coding-agent-integration/`.
- External backend lifetime converges on `StreamingProviderBackend.close()`: OpenCode SSE stays warm across normal turns, while Pi/OpenCode subprocess children are per-turn; cancellation and shutdown terminate full process trees. Cleanup failure retains backend/agent/pool ownership for retry. Do not add provider-specific branches above the adapter layer.

## Approval Architecture Rules (CRITICAL)

1. **One approval path only.** `ToolNode` → `ApprovalTransaction` → `TurnSnapshot` → `ApprovalRenderer`. Do NOT add approval logic to interceptors, hooks, or control consumers.
2. **`ControlDrainInterceptor` must not drain `APPROVAL_RESPONSE`.** Drain set is for cancel/inject/config only.
3. **`ApprovalRuntime` is a policy service, not a state owner.** Owns classifier + deny_policy. State lives in `ApprovalTransaction` inside `ReActTurnState`.
4. **Deny policy defaults to `TOOL_RESULT_ONLY`.** Rejection returns tool errors with `deny_reason`, continues ReAct loop. `CANCEL_TURN` is a configurable override.
5. **`deny_reason` lives on `ApprovalTransaction.deny_reason`.** Do not read from `ctx.metadata` or other locations.

## Testing

Unit tests under `tests/unit/` (mirrors `src/modex_agent/` structure), architecture guard tests under `tests/architecture/`, integration tests under `tests/integration/`. Write/update tests before production code when practical. Absolute imports (`from modex_agent.xxx`). Mock `LLMProvider`, `ControlChannel` — never hit real APIs.

## Documentation

Architecture Decision Records (ADRs) in `docs/adr/` (ADR-0001 ~ 0035) and design docs in `docs/design/`. See `docs/AGENTS.md` for the docs index. Read relevant ADRs before making significant architectural changes.

## Key Files

| File | Location | Description |
|------|----------|-------------|
| Root Guidelines | `AGENTS.md` | This file — project overview and conventions |
| Framework Overview | `src/modex_agent/AGENTS.md` | All framework modules with file counts and responsibilities |
| Tests Overview | `tests/AGENTS.md` | Unit, framework, and integration test suites |
| Docs Overview | `docs/AGENTS.md` | Index of `docs/` — ADRs, design docs, agent docs |
| Bot Reference | `examples/bot_project/AGENTS.md` | End-to-end reference implementation |

## Agent skills

### Issue tracker

Issues live as local markdown under `docs/design/<feature>/`. External PRs are not a triage surface. See `docs/agents/issue-tracker.md`.

### Triage labels

Canonical defaults: needs-triage, needs-info, ready-for-agent, ready-for-human, wontfix. See `docs/agents/triage-labels.md`.

### Domain docs

Multi-context: `CONTEXT-MAP.md` at root points to per-context `CONTEXT.md` files. See `docs/agents/domain.md`.
