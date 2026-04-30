<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-04-30 -->

# react

## Purpose
ReAct agent implementation — the primary reasoning strategy. Implements Thought → Action → Observation loop with full Hook/Interceptor/Control integration.

## Key Files
| File | Description |
|------|-------------|
| `agent.py` | `ReActAgent` class — full loop with streaming, tool execution, hook dispatch, interceptor chain, checkpoint, injection |
| `builder.py` | `ReActAgentBuilder` — factory for constructing ReActAgent instances with configuration |

## For AI Agents

### Working In This Directory
- `ReActAgent.run()` is the main entry — calls hooks, drain injections, iterates, executes tools
- `deny_as_cancel` flow: interceptor sets `_deny_as_cancel` flag → ReActAgent pads remaining tools in batch → raises `ApprovalDenied`
- `_stream_with_control()`: active only when `interceptor_chain.has_scope(LLM_STREAM)` — uses `around_llm_stream` onion
- `_drain_injections()`: consumes from `ctx.injection_queue` with `_MAX_INJECTION_CYCLES` limit
- Checkpoint save on each assistant message, tool result, and cancellation

### Key Constants
| Constant | Value | Purpose |
|----------|-------|---------|
| `_HOOK_TIMEOUT` | 10.0s | Per-hook execution timeout |
| `_TOOL_TIMEOUT` | from DefaultValues | Per-tool execution timeout |
| `_MAX_INJECTION_CYCLES` | 5 | Max injection loops per turn |
| `_MAX_INJECTIONS_PER_PHASE` | 3 | Max injections per drain phase |
| `_MAX_TOOL_RESULT_CHARS` | 20000 | Tool result truncation |

### ReActEvent Enum
| Event | Emitted When |
|-------|-------------|
| `MODEL_OUTPUT` | LLM text delta (streaming) |
| `MODEL_REASONING` | LLM reasoning delta |
| `TOOL_CALL_START` | Before tool execution |
| `TOOL_CALL_END` | After tool execution (with result) |
| `ITERATION_START` / `ITERATION_END` | Per ReAct iteration |
| `FINAL_OUTPUT` | Confirmed final response |
| `START` / `ERROR` / `MAX_ITERATIONS` / `PROGRESS` | Lifecycle events |

### Testing Requirements
- Tests in `tests/unit/agents/`
- Mock `LLMProvider` and `ContentEmitter` for unit tests
- Test `deny_as_cancel` batch completion (all remaining tools padded)
- Test `CancelledError` checkpoint preservation
- Test `AgentControlError` handling
