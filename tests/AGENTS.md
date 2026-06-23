<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-06-22 -->

# tests

Test suites for the ModexAgent framework — unit tests, framework-level tests, and integration tests.

## Purpose

The `tests/` directory mirrors the `framework/` package structure with unit tests and adds framework-level and integration-level test suites. Tests use `pytest` with `pytest-asyncio` for async support.

## Subdirectories

| Directory | Purpose |
|-----------|---------|
| `unit/` | Unit tests mirroring `framework/` package structure (21 sub-modules) |
| `framework/` | Framework-level tests (e.g., `framework/tools/` tool tests) |
| `integration/` | Integration tests across multiple modules — `experience/`, `memory/`, `multi_agent/` |

## For AI Agents

### Working In This Directory
- Run all tests: `pytest tests/ -v`
- Use absolute imports: `from framework.xxx`
- Mock `LLMProvider`, `ControlChannel`, `ControlEventBus` — never hit real APIs
- `pytest-asyncio` for async test functions (use `async def` + `await`)

### Common Patterns
- Tests follow the same package structure as `framework/`
- `AsyncMock` for async interfaces
- `conftest.py` for shared fixtures per package
- Integration tests may require more timeouts due to async coordination

## Dependencies

### Internal
- `framework/` — all tested modules

### External
- `pytest` + `pytest-asyncio`

<!-- MANUAL -->
