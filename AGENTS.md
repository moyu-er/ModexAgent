# Repository Guidelines

## Project Layout

`src/modex_agent/` is the reusable agent framework (src layout, ADR-0003). `src/modex_graph/` is the standalone graph engine (ADR-0033). Key framework areas: `agents/react/` (ReAct runtime), `agents/external_coding/` (Pi/OpenCode harness), `memory/` (three-layer), `persistence/` (hybrid SQLite+file, ADR-0023), `multi_agent/` (star-topology), `pipeline/`, `hook/`+`interceptor/`+`control/` (three-layer runtime), `tools/`, `approval/`, `sandbox/`, `media/`, `commands/`. See `src/modex_agent/AGENTS.md` for the exhaustive module table (26 modules).

`examples/bot_project/` is the primary end-to-end reference (Pool mode, WebUI React frontend, QQ + Telegram adapters). Framework-generic behavior in `src/modex_agent/`; business wiring in `examples/`.

## Commands

- `uv pip install -e ".[dev,llm,storage,gateway]"`: install
- `pytest tests/unit/ -v`: unit tests
- `pytest tests/integration/ -v -m integration`: integration tests
- `ruff check src/modex_agent tests/`: lint
- `ruff format src/modex_agent tests/`: format
- `mypy src/modex_agent`: type check

## Rules

Detailed rules in `rules/type-safety.md` (16 rules) and `rules/architecture.md` (15 rules). Read before any framework code change. Core principles:

- **Type safety:** enums/constants over raw strings; typed structures over loose dicts; declared types, no bare `Any`/`list`/`dict` in framework APIs; ABCs before implementations, zero Protocols; no `getattr`/`hasattr`/`isinstance` except at real extension boundaries. Pydantic `BaseModel` with `frozen=True, extra="forbid"` for all cross-module structured data; serialization via `model_dump()`/`model_validate()`, never hand-rolled `json.dumps`.
- **Architecture:** deep modules (interface simpler than implementation); deletion test before extracting; interface is the test surface; one adapter is hypothetical, two make a real seam; preserve locality; ABCs for interfaces; per-turn state in `runtime.state`; `GraphInterrupt` for approval suspension. See `rules/architecture.md` for the full list.

## Convergence Rules

1. **Converge, don't patch.** When multiple paths exist for the same concern (e.g. native vs external subagent wiring, main-agent vs subagent emitter injection), do NOT add a third branch or an if-else special case. Find the shared path and make ALL existing paths flow through it. The fix is correct only when every caller uses the same mechanism — no provider-specific or path-specific branches.

   Example: native subagent emitter wiring goes through a `_create_with_emitter` wrapper; external subagent wiring bypasses it and calls `set_emitter_factory` manually. Both achieve the same result through different mechanisms — this is an accidental divergence. The correct fix is to converge both to a single post-build wiring step, not to add a third path.

2. **No backward-compatibility shims for code just written.** If the old path is wrong, remove it; if it's right, converge to it. Do not add deprecation aliases, "fall back to old behavior if X is None" guards, or parallel implementations for code you just wrote. Convergence may require touching more files than a minimal patch — that is the cost of correctness in a high-complexity codebase.

   Example: adding `parent_modex_session_id` to `ExternalSessionMapStore` when `SessionInfo.parent_session_id` already stores the same relationship in `SessionStore` is a redundant parallel path. The correct fix is to remove the redundant field, not to keep both "for compatibility".

## Testing Rules

1. **Pre-existing test failures are bugs, not background noise.** Investigate root cause, fix the test or the code, commit together with your change. Never skip/xfail/delete to get green. Never commit with a red suite.

   Example: `test_tool_events_persisted` fails with `TypeError: MagicMock not JSON serializable`. This is not "pre-existing noise" — it's a test using `MagicMock()` where the emitter calls `json.dumps(event.to_dict())` on fields that return nested mocks. Fix the test to use real values or `MagicMock(spec=...)`, commit the fix with your change.

2. **Tests must exercise the real call-site pattern.** If no correct test seam exists for a bug pattern, that itself is the finding — note it and flag the architectural gap.

   Example: a unit test that mocks `ExternalCodingAgent.__init__` cannot reproduce a bug where `_handle_emission` drops child emissions because `child_discovery_sink` is None — the mock hides the None. The correct seam is to use a real agent with a mock `ChildSessionDiscoverySink`.

## Memory Rules

- Compression mutates persisted session/archive memory via lifecycle hooks.
- Governance mutates only the LLM input copy before model calls. Never write governance output back to session.
- Tool-call chains must stay structurally legal: don't split `assistant.tool_calls` from matching `tool` results.
- `archive=None` is standard session-only mode for subagent memory.
- Subagent session memory is temporary; clear after subagent finishes.
- Memory scopes: Session, User, Tenant, Agent, Channel, Chat, Composite, Global.

## Multi-Agent Communication Rules

- Star topology: subagents communicate only through main agent (`subagent_validator.py` enforces).
- Single LLM-facing tool: `send_to_agent`. The framework decides delivery mechanism internally.
- Session ID format: `{prefix}.{agent_name}` (dot-separated, via `SessionIdFactory`).
- `SubagentAutoSendHook` auto-forwards final output to parent.
- External coding agents (Pi, OpenCode) are NORMAL main agents of their own pools; they reply via `modexctl send` CLI, not `send_to_agent`. See ADR-0022 and `docs/design/external-coding-agent-integration/`.

## Approval Architecture Rules

1. One approval path only: `ToolNode` → `ApprovalTransaction` → `TurnSnapshot` → `ApprovalRenderer`. Do NOT add approval logic to interceptors, hooks, or control consumers.
2. `ApprovalRuntime` is a policy service, not a state owner. State lives in `ApprovalTransaction` inside `ReActTurnState`.
3. `deny_reason` lives on `ApprovalTransaction.deny_reason`. Do not read from `ctx.metadata`.

## Persistence

Hybrid: per-workspace SQLite (`<workspace>/.modex/state.db`) for transactional state, plus files for human-editable and binary data. `PersistenceBackend` enum (`FILE`/`SQLITE`) drives factory selection. SQLite is the bot's default. See ADR-0023 and ADR-0028~0031 for schema details. `RecordScope` (frozen Pydantic) carries scope dimensions; `canonical()` is the DB key source.

## Documentation

ADRs in `docs/adr/` (ADR-0001~0034), design docs in `docs/design/`. See `docs/AGENTS.md` for the index. Read relevant ADRs before significant architectural changes.

**ADR governance:** living documents, not append-only logs. Merge refinements in place. No parallel versions. Consolidate, don't proliferate. New ADRs only for genuinely new decisions.

## Key Files

| File | Description |
|------|-------------|
| `rules/type-safety.md` | 16 type safety rules — read before framework code changes |
| `rules/architecture.md` | 15 architecture rules — read before module design or refactor |
| `src/modex_agent/AGENTS.md` | Exhaustive framework module table (26 modules) |
| `examples/bot_project/AGENTS.md` | End-to-end reference implementation |
| `tests/AGENTS.md` | Test suite overview |
| `docs/AGENTS.md` | ADRs, design docs, agent docs index |
