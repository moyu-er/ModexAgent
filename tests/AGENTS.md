<!-- Updated: 2026-06-10 -->

# tests

Test suites for the ModexAgent framework, organized by level.

## Subdirectories

| Directory | Purpose |
|-----------|---------|
| `unit/` | Pure unit tests — no external deps, must run offline (see `unit/AGENTS.md`) |
| `integration/` | Requires config files, LLM APIs, external services; tagged `@pytest.mark.integration` |
| `framework/` | Framework-level test fixtures and shared utilities |
| `framework/tools/terminal/` | Terminal system integration tests — guard, poll loop, prompt detection, backend tests, tool integration |

## For AI Agents

### Testing Requirements
1. Mirror package structure under `tests/unit/`
2. Use absolute imports (`from framework.xxx`) inside tests
3. Tag integration tests with `@pytest.mark.integration`
4. Run full suite: `pytest tests/unit/ -v`
5. Run single test: `pytest tests/unit/path/to/test.py::test_name -xvs`
6. `asyncio_mode = auto` from pyproject.toml — `@pytest.mark.asyncio` not required but accepted
