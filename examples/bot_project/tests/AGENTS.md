<!-- Parent: ../AGENTS.md -->
<!-- Updated: 2026-06-13 -->

# tests

Test suites for the bot_project. Unit and integration tests covering service lifecycle, adapters, pool routing, WebUI, memory, and tool integration.

## Key Files

| File | Description |
|------|-------------|
| `__init__.py` | Package marker |
| `test_bot_service_cli.py` | BotService CLI argument parsing and mode selection |
| `test_config_loader.py` | Config loading, `${ENV_VAR}` interpolation, pool config parsing |
| `test_context_construction.py` | Agent context assembly and memory injection |
| `test_memory_construction.py` | Memory system construction per pool/agent |
| `test_agent_communication.py` | Inter-agent communication (send_to_agent, subagent dispatch) |
| `test_pool_experience_review.py` | Experience review in pool mode |
| `test_plugin_integration.py` | Plugin lifecycle and integration |
| `test_policy.py` | Runtime safety policy enforcement |
| `test_qq_adapter.py` | QQ input/output adapter behavior |
| `test_runtime_defaults.py` | Default runtime configuration values |
| `test_slash_commands.py` | Slash command parsing and dispatch |
| `test_terminal_integration.py` | Terminal tool integration tests |

## Subdirectories

| Directory | Purpose |
|-----------|---------|
| `webui/` | WebUI-specific tests (see `webui/AGENTS.md`) |

## For AI Agents

### Working In This Directory
- Run all tests: `python -m pytest examples/bot_project/tests -q`
- Tests use `pytest-asyncio` for async test functions.
- Mock patterns: prefer `AsyncMock` for adapter interfaces, avoid mocking internal implementation details.
- Tests involving subprocess must use short timeouts (see project feedback on slow tests).

### Common Patterns
- Service tests construct `BotService` with mock adapters and verify initialization order.
- Adapter tests use `InputAdapter`/`OutputAdapter` mocks.
- Pool routing tests verify `PoolSessionStore` persistence and `PoolRouter._extract_pool_command()`.
