<!-- Generated: 2026-04-30 -->

# tests

## Purpose
Test suites for the ModexAgent framework, organized by test level: unit, integration, e2e.

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

### Phase 2 Test Coverage
| Area | Test Files |
|------|-----------|
| ControlChannel v2 | `test_control_channel_v2.py` |
| EventBus v2 | `test_event_bus_v2.py` |
| Checkpoint v2 | `test_checkpoint_v2.py` |
| Tiered Approval | `test_tiered_tool_approval.py` |
| Steer + Watch | `test_steer_and_watch_interceptors.py` |
| Control channel (v1) | `test_control_channel.py` |
| Interceptor chain | `test_interceptor_chain.py` |
| Hooks | `test_hooks.py`, `test_hook_error_policy.py` |
| ReAct error handling | `test_react_agent_error.py` |
