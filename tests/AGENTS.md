<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-07-17 | Updated: 2026-09-02 -->

# tests

Test suites for the ModexAgent framework — unit tests, framework-level tests, architecture guard tests, conformance tests, and integration tests.

## Purpose

The `tests/` directory mirrors the `src/modex_agent/` package structure with unit tests and adds framework-level, architecture guard, backend-conformance, and integration-level test suites. Tests use `pytest` with `pytest-asyncio` for async support.

## Subdirectories

| Directory | Purpose |
|-----------|---------|
| `unit/` | Unit tests mirroring current `src/modex_agent/` owners, including messaging transport models and persistence-owned session storage/registry |
| `framework/` | Framework-level tests — tool/MCP/terminal integration (real wiring, not mocked) |
| `architecture/` | AST-guard tests enforcing architectural invariants (ADR-0006 dependency tiers, exact E1/E2 facades and owner exports, old-path absence, no back-refs, dead-code-gone, god-object-gone, module seams preserved). Each test is an ADR gate — `EXPECTED_OFFENDERS` sets shrink to empty as fixes land; the assertion stays strict. |
| `conformance/` | Parametrized file↔SQLite backend equivalence suites (ADR-0023) — one file per split-store/runtime-state ABC (`MessageStore`, `KVStore`, `CursorStore`, `ArchiveStore`, `InboxMQ`, `PoolRoutingStore`, `ExternalSessionMapStore`, `ScopeRegistryStore` (file name still says "workspace registry"), `ApprovalAuditStore`, `TurnStateStore`, `TodoStore`, and persistence-owned `SessionStore`). The session suite compares `LocalFileSessionStore` from `persistence/adapters/file_session_store.py` with `SqliteSessionStore`. |
| `integration/` | Integration tests across multiple modules — `experience/`, `memory/`, `multi_agent/`, `bot_project/`. Excluded by default (`-m 'not integration'`); run explicitly with `-m integration`. |

> `tests_ext/` is declared in `pyproject.toml` `testpaths` but does not exist on disk — it is a reserved external/downstream test surface.

## For AI Agents

### Working In This Directory
- Run unit tests (default, integration excluded): `pytest tests/ -v`
- Run integration tests explicitly: `pytest tests/integration/ -v -m integration`
- Run architecture guards: `pytest tests/architecture/ -v`
- Run conformance suites: `pytest tests/conformance/ -v`
- Use absolute imports: `from modex_agent.xxx`
- Mock `LLMProvider`, `ControlChannel` — never hit real APIs
- `pytest-asyncio` mode is `auto` — all `async def` tests run as asyncio automatically
- `--import-mode=importlib` is set — avoids same-basename collisions across dirs
- Both `test_*.py` and `*_test.py` naming conventions are accepted

### Common Patterns
- Tests follow the same package structure as `src/modex_agent/`
- Transport model tests live under `unit/messaging/`; session store/registry tests live under `unit/persistence/`
- `AsyncMock` for async interfaces
- `conftest.py` for shared fixtures (4 files: `conformance/`, `unit/memory/`, `unit/persistence/adapters/`, `integration/bot_project/`)
- Architecture tests use AST parsing, not runtime — they enforce structural invariants
- Conformance tests parametrize over `file` + `sqlite` backends via shared `conftest.py` fixtures
- Integration tests may require more timeouts due to async coordination

### pytest Configuration (`pyproject.toml`)
- `asyncio_mode = "auto"` — no `@pytest.mark.asyncio` needed
- `addopts` includes `-m 'not integration'` — integration tests deselected by default
- Single custom marker: `integration`
- No coverage thresholds configured (`pytest-cov` available but opt-in via CLI)
- No parallel execution (`pytest-xdist` not configured)

## Dependencies

### Internal
- `src/modex_agent/` — all tested modules

### External
- `pytest` + `pytest-asyncio`

<!-- MANUAL -->
