<!-- Generated: 2026-05-16 | Updated: 2026-05-16 -->

# tests

Test suites for the ModexAgent framework, organized by level.

## Subdirectories

| Directory | Purpose |
|-----------|---------|
| `unit/` | Pure unit tests — no external deps, must run offline (see `unit/AGENTS.md`) |
| `integration/` | Requires config files, LLM APIs, external services |
| `e2e/` | End-to-end multi-agent scenarios |

## For AI Agents

### Testing Requirements
1. Mirror package structure under `tests/unit/`
2. Use absolute imports (`from framework.xxx`) inside tests
3. Tag integration tests with `@pytest.mark.integration`
4. Run full suite: `pytest tests/unit/ -v`
5. Run single test: `pytest tests/unit/path/to/test.py::test_name -xvs`
