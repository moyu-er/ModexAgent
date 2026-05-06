<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-04-30 -->

# unit

## Purpose
Pure unit tests — no external deps, must run offline. Mirror the framework package structure.

## Subdirectories
| Directory | Purpose |
|-----------|---------|
| `agents/` | ReActAgent tests — error handling, tool execution, streaming |
| `core/` | Core abstractions — AgentContext, AgentResult, error handling, emitter, tool manager |
| `core/skills/` | Skill system tests |
| `pipeline/` | Pipeline tests — emitter, adapters, timeout, skills |
| `session/` | AgentSession tests |
| `multi_agent/` | Multi-agent tests — factory, pool, inbox, hooks |
| `multi_agent/inbox/` | Inbox subsystem tests |
| `memory/` | Memory system tests — core, stores, compaction, compression, consolidation |
| `messaging/` | Broker and bridge tests |
| `plugins/` | Plugin system tests |
| `tools/` | Standard tools tests |
| `utils/` | Utility tests |
| `extensions/llm/` | LiteLLM provider tests |

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
    # Arrange
    ctx = AgentContext(...)
    # Act
    result = await agent.run(ctx, emitter)
    # Assert
    assert result.content == "expected"
```
## Current Runtime Status

Unit tests for ReAct runtime should live under `tests/unit/agents/react/` and
mock providers/tools directly. Include regressions for clean/full boundaries,
runtime state aliases, and cancellation metadata when those areas change.
