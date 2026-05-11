<!-- Parent: ../AGENTS.md -->
<!-- Updated: 2026-05-11 -->

# interceptor

## Purpose
AOP (Aspect-Oriented Programming) call-boundary wrapping layer. Interceptors form an onion chain around tool calls, turns, iterations, and LLM streams. Each interceptor can timeout, transform, or cancel the wrapped call. **Approval does NOT go through interceptors** — it is handled through the pipeline layer (TurnSnapshot + ApprovalTransaction).

## Key Files
| File | Description |
|------|-------------|
| `abc.py` | `Interceptor` Protocol, `InterceptorScope` (9 scopes), context types (`ToolCallContext`, `LLMStreamContext`, etc.), next-call types |
| `chain.py` | `InterceptorChain` — onion chain executor with `around_tool_call/around_turn/around_iteration/around_llm_stream` + `has_scope()` |
| `handler.py` | `CommandHandlerRegistry`, `DefaultCancelHandler` — command handler registration for `ControlDrainInterceptor` |
| `__init__.py` | Public API exports |

## Subdirectories
| Directory | Purpose |
|-----------|---------|
| `builtin/` | Framework-provided interceptors — timeout, watch, steer-inject, stream-watch, result-limit, control-drain, tool-policy (see `builtin/AGENTS.md`) |

## For AI Agents

### Working In This Directory
- New interceptors implement the `Interceptor` Protocol — a `scopes` frozenset + the matching `around_*` method
- Onion model: index 0 is outermost (enters first, exits last)
- ControlDrain at boundary level, ToolResultLimit innermost
- `InterceptorChain.has_scope()` for checking if a scope has any registered interceptors
- `around_tool_call` MUST return a legal `ToolResult`

### Interceptor Scopes
| Scope | Method | Purpose |
|-------|--------|---------|
| `TOOL_CALL` | `around_tool_call` | Wrap individual tool execution |
| `TURN` | `around_turn` | Wrap entire turn |
| `ITERATION` | `around_iteration` | Wrap single ReAct iteration |
| `LLM_STREAM` | `around_llm_stream` | Wrap LLM streaming response |
| `AGENT_RUN` | (future) | Wrap full agent run |
| `LLM_CALL`, `PIPELINE_STEP`, `POOL_TASK`, `MEMORY_OPERATION` | (future) | Reserved |

### Testing Requirements
- Tests in `tests/unit/test_interceptor_chain.py`, `tests/unit/test_control_drain_interceptor.py`, etc.
- Test onion ordering
- Test exception handling (AgentControlError propagates, generic exceptions converted to ToolResult)
- Approval-related tests are in `tests/unit/approval/`, NOT in interceptor tests

## Dependencies

### Internal
- `framework.control` — `ControlChannel`, `ControlEventBus`, `ControlCommandType`
- `framework.core.agent` — `AgentContext`

## Current Runtime Status

Interceptors wrap execution scopes such as tool calls, LLM streams, turns, and
iterations. The bot project default chain currently includes
`ControlDrainInterceptor` and `ToolResultLimitInterceptor` only.
Approval is handled through `ToolNode` → `ApprovalTransaction` → `TurnSnapshot` → `ApprovalRenderer`.

