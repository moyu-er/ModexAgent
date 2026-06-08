<!-- Parent: ../AGENTS.md -->
<!-- Updated: 2026-05-31 -->

# unit

Pure unit tests — no external deps, must run offline. Mirror the framework package structure.

## Subdirectories

| Directory | Purpose |
|-----------|---------|
| `agents/` | ReActAgent tests — error handling, tool execution, streaming |
| `approval/` | Approval system tests |
| `bot_project/` | Bot project integration tests |
| `control/` | ControlChannel, EventBus, store, task supervision tests |
| `core/` | Core abstractions — AgentContext, AgentResult, emitter, tool manager |
| `core/graph/` | Graph engine tests |
| `core/skills/` | Skill system tests |
| `interceptor/` | Interceptor chain tests |
| `ioc/` | IOC config and factory tests |
| `memory/` | Memory system — core, stores, compression, consolidation, retention, injection |
| `messaging/` | Broker and bridge tests |
| `multi_agent/` | Multi-agent — factory, pool, inbox, hooks, skills |
| `pipeline/` | Pipeline tests — emitter, adapters, timeout, skills |
| `plugins/` | Plugin system tests |
| `providers/` | LLM provider tests |
| `runtime/` | Runtime services, store, codec tests |

| `tools/` | Tool registry, executor, MCP tests |
| `utils/` | Utility tests |

## For AI Agents

### Working In This Directory
- Use `pytest.mark.asyncio` for async tests (or `asyncio_mode = auto` from pyproject.toml)
- Mock `LLMProvider`, `ControlChannel`, `ControlEventBus` — never hit real APIs
- Test file naming: `test_<module_name>.py`
- `conftest.py` at any level for shared fixtures

### Key Test Patterns
```python
@pytest.mark.asyncio
async def test_something():
    ctx = AgentContext(...)
    result = await agent.run(ctx, emitter)
    assert result.content == "expected"
```
