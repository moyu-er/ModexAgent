<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-06-22 -->

# interceptor

## Purpose
AOP onion-chain layer wrapping call boundaries. Interceptors form recursive closures around tool calls, turns, iterations, and LLM streams. Each can timeout, transform, or cancel the wrapped call. Approval does NOT go through interceptors -- handled via pipeline layer (TurnSnapshot + ApprovalTransaction).

## Key Files
| File | Description |
|------|-------------|
| `abc.py` | `Interceptor` ABC (not Protocol), `InterceptorScope` (9 defined, 4 active: TOOL_CALL/TURN/ITERATION/LLM_STREAM), context types (`ToolCallContext`, `TurnContext`, `IterationContext`, `LLMStreamContext`, `LLMStreamChunk`). `LLMStreamChunk` fields are now typed: `finish_reason: FinishReason | None`, `control_action: StreamControlAction | None` (B6). `LLMRequest`/`LLMCallContext`/`LLMStreamContext.messages` is now `Sequence[ChatMessage]` (B6). next-call signatures |
| `chain.py` | `InterceptorChain[R]` -- recursive closure builder per scope, `has_scope()` check, exception handling (AgentControlError propagates, generic -> ToolResult) |
| `__init__.py` | Public API exports |

## Subdirectories
| Directory | Purpose |
|-----------|---------|
| `builtin/` | 3 interceptors + 1 classification helper -- tool_timeout, result-limit, tool_approval |

## Active Scopes
| Scope | Method | Chain method | Purpose |
|-------|--------|-------------|---------|
| `TOOL_CALL` | `around_tool_call` | `around_tool_call()` | Wrap individual tool execution |
| `TURN` | `around_turn` | `around_turn()` | Wrap entire turn |
| `ITERATION` | `around_iteration` | `around_iteration()` | Wrap single ReAct iteration |
| `LLM_STREAM` | `around_llm_stream` | `around_llm_stream()` | Wrap LLM streaming response |

Reserved (defined but not wired): AGENT_RUN, LLM_CALL, PIPELINE_STEP, POOL_TASK.

## Design Rules
- Index 0 is outermost (enters first, exits last)
- `around_tool_call` MUST return a legal `ToolResult`
- Generic exceptions in tool scope are caught and converted to `ToolResult(error=...)`
- `AgentControlError` and `CancelledError` always propagate
- Use `has_scope()` to check if a scope has any registered interceptors

## Dependencies
- `modex_agent.control` -- ControlChannel, ControlCommandType
- `modex_agent.core.agent` -- AgentContext

<!-- MANUAL: -->
