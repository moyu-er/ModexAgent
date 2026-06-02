# Interceptor & Hook ABC Redesign

**Date**: 2026-06-02
**Status**: Draft
**Scope**: `framework/interceptor/`, `framework/hook/`, `examples/bot_project/`

## 1. Problem Statement

Current interceptor and hook mechanisms share the same design issues:

1. **Fat Protocol interface**: `Interceptor` Protocol defines 4 `around_*` methods; `Hook` Protocol defines 9 lifecycle methods. Every implementation sees all methods, but only implements 1-2. The rest are `...` stubs.

2. **No compile-time enforcement**: Protocol provides no `@abstractmethod`. Implementors get no IDE/mypy guidance on which methods they must implement for their declared scope.

3. **Dead code**: 6 of 8 built-in interceptors and 4 of 10 built-in hooks are never instantiated outside tests. They inflate the codebase and mislead developers about what the framework actually uses.

4. **Hardcoded registration**: Interceptor chain is assembled via imperative `chain.add()` calls in business code with no configuration-driven mechanism.

## 2. Design Goals

- Replace Protocol with per-scope/per-point ABC inheritance hierarchy
- Delete all dead-code interceptors and hooks
- Maintain full backward compatibility for `InterceptorChain` and `HookRunner` internals
- Symmetric design for both interceptor and hook subsystems

## 3. Dead Code Removal

### Interceptors to Delete

| File | Class | Reason |
|------|-------|--------|
| `interceptor/builtin/turn_timeout.py` | `TurnTimeoutInterceptor` | Zero production instantiation; bot_project test explicitly asserts it should NOT be in default chain |
| `interceptor/builtin/tool_timeout.py` | `ToolTimeoutInterceptor` | Only in tests; ReAct agent handles timeout via `_execute_tool_raw` |
| `interceptor/builtin/tool_watch.py` | `ToolWatchInterceptor`, `ToolCancelPolicy` | Only in tests; cancel monitoring handled by ControlRuntime |
| `interceptor/builtin/llm_stream_watch.py` | `LLMStreamWatchInterceptor` | Zero instantiation anywhere |
| `interceptor/builtin/steer_inject.py` | `SteerInjectInterceptor` | Only in tests; STEER busy-mode handled by pipeline |
| `interceptor/builtin/tool_policy_interceptor.py` | `ToolPolicyInterceptor` | Only in tests; policy filtering handled by DynamicToolFilterHook |

### Hooks to Delete

| File | Class | Reason |
|------|-------|--------|
| `hook/builtin/subagent_cleanup.py` | `SubagentMemoryCleanupHook` | Zero instantiation anywhere |
| `hook/builtin/dynamic_tool_filter.py` | `DynamicToolFilterHook` | Only in tests |
| `hook/builtin/llm_output_guard.py` | `LLMOutputGuardHook` | Only in tests |
| `hook/builtin/tool_result_transform.py` | `ToolResultTransformHook` | Only in tests |

### Retained Components

**Interceptors** (2):
- `ControlDrainInterceptor` — used in bot_project + framework pipeline/services
- `ToolResultLimitInterceptor` — used in bot_project + framework pipeline
- `ArgumentMatcher` — helper (not an interceptor), used by ApprovalRuntime

**Hooks** (6):
- `RunLoggingHook` — used in bot_project + agent_runtime_config
- `RuntimeContextHook` — used in bot_project
- `InboxFlushHook` — used in bot_project + framework factory/communication
- `SubagentAutoSendHook` — used in bot_project + framework communication
- `ProgressReportHook` — used in bot_project
- `TraceFileWriter` — used in bot_project (event subscriber, not a Hook subclass)

### Cleanup Scope

For each deleted component:
1. Delete the source file
2. Remove from `__init__.py` exports
3. Delete associated test files
4. Remove any doc/comment references in AGENTS.md files
5. Update AGENTS.md references to reflect deleted components. Reserved `InterceptorScope` values (AGENT_RUN, LLM_CALL, PIPELINE_STEP, POOL_TASK) remain in the enum for future use — no changes to the enum itself.

## 4. Interceptor ABC Hierarchy

### 4.1 Base Classes

```python
# framework/interceptor/abc.py

class Interceptor(ABC):
    """All interceptors' public base class."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique interceptor name for logging and diagnostics."""
        ...

    @property
    def scopes(self) -> frozenset[InterceptorScope]:
        """Auto-derived from MRO: collects _scope from all per-scope ABC ancestors."""
        result: set[InterceptorScope] = set()
        for cls in type(self).__mro__:
            if cls is object:
                continue
            s = getattr(cls, '_scope', None)
            if s is not None:
                result.add(s)
        return frozenset(result)
```

### 4.2 Per-Scope ABCs

Each scope gets its own ABC with `_scope` class attribute and one `@abstractmethod`:

```python
class ToolCallInterceptor(Interceptor):
    _scope = InterceptorScope.TOOL_CALL

    @abstractmethod
    async def around_tool_call(self, ctx, call, next_call) -> ToolResult: ...

class TurnInterceptor(Interceptor):
    _scope = InterceptorScope.TURN

    @abstractmethod
    async def around_turn(self, ctx, next_call) -> AgentResult: ...

class IterationInterceptor(Interceptor):
    _scope = InterceptorScope.ITERATION

    @abstractmethod
    async def around_iteration(self, ctx, call, next_call) -> None: ...

class LLMStreamInterceptor(Interceptor):
    _scope = InterceptorScope.LLM_STREAM

    @abstractmethod
    async def around_llm_stream(self, ctx, call, next_stream) -> AsyncIterator[LLMStreamChunk]: ...
```

### 4.3 Concrete Implementations

**Single-scope** (inherit one ABC):

```python
class ToolResultLimitInterceptor(ToolCallInterceptor):
    @property
    def name(self) -> str: return "tool_result_limit"

    async def around_tool_call(self, ctx, call, next_call) -> ToolResult:
        result = await next_call()
        # overflow logic ...
        return result
```

**Multi-scope** (inherit multiple ABCs):

```python
class ControlDrainInterceptor(TurnInterceptor, IterationInterceptor):
    @property
    def name(self) -> str: return "control_drain"

    async def around_turn(self, ctx, next_call) -> AgentResult:
        await self._drain(ctx)
        return await next_call()

    async def around_iteration(self, ctx, call, next_call) -> None:
        await self._drain(ctx)
        await next_call()

    async def _drain(self, ctx) -> None:
        # shared drain logic
        ...
```

### 4.4 Scopes Auto-Derivation

`ControlDrainInterceptor.scopes` returns `frozenset({TURN, ITERATION})` automatically because:
- `TurnInterceptor._scope = TURN`
- `IterationInterceptor._scope = ITERATION`
- MRO walks both ancestors, collects both `_scope` values

No manual `scopes = frozenset({...})` needed.

### 4.5 InterceptorChain Compatibility

`InterceptorChain._resolved(scope)` reads `i.scopes` — this property still exists, just auto-derived. **No changes to chain.py logic.** The `chain.around_*` methods, `_build_*_chain` closures, and exception handling remain identical.

## 5. Hook ABC Hierarchy

### 5.1 Base Classes

```python
# framework/hook/abc.py

class Hook(ABC):
    """All hooks' public base class."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique hook name for logging and diagnostics."""
        ...

    @property
    def hook_point(self) -> HookPoint | None:
        """Auto-derived from class hierarchy: reads _hook_point from per-point ABC."""
        for cls in type(self).__mro__:
            if cls is object:
                continue
            hp = getattr(cls, '_hook_point', None)
            if hp is not None:
                return hp
        return None
```

### 5.2 Per-Point ABCs

9 HookPoints → 9 ABCs (only surviving hooks need new ABCs):

```python
class BeforeTurnHook(Hook):
    _hook_point = HookPoint.BEFORE_TURN
    @abstractmethod
    async def before_turn(self, ctx) -> HookResult: ...

class AfterTurnHook(Hook):
    _hook_point = HookPoint.AFTER_TURN
    @abstractmethod
    async def after_turn(self, ctx, result) -> HookResult: ...

class AfterToolExecutionHook(Hook):
    _hook_point = HookPoint.AFTER_TOOL_EXECUTION
    @abstractmethod
    async def after_tool_execution(self, ctx, results) -> HookResult: ...

class AfterLLMResponseHook(Hook):
    _hook_point = HookPoint.AFTER_LLM_RESPONSE
    @abstractmethod
    async def after_llm_response(self, ctx, response) -> HookResult: ...

# ... remaining per-point ABCs for BEFORE_ITERATION, AFTER_ITERATION,
#     BEFORE_TOOL_EXECUTION, ON_CONTROL_COMMAND, FINALIZE_CONTENT
```

### 5.3 Concrete Implementations

```python
class RunLoggingHook(BeforeTurnHook, AfterTurnHook, BeforeIterationHook, AfterIterationHook):
    """Implements 4 hook points via multi-inheritance."""
    @property
    def name(self) -> str: return "run_logging"
    # implements before_turn, after_turn, before_iteration, after_iteration

class InboxFlushHook(BeforeTurnHook):
    """Single hook point."""
    @property
    def name(self) -> str: return "inbox_flush"
    # implements before_turn
```

### 5.4 HookRunner Compatibility

`HookRunner.dispatch()` currently uses `getattr(hook, hook_point.value, None)` to find methods. With the ABC approach:
- Hooks still have the method (it's an `@abstractmethod` implemented by the concrete class)
- `getattr` still finds it
- **No changes to runner.py dispatch logic**

The `hook_point` property is for metadata/inspection, not for dispatch. Dispatch remains `getattr`-based.

### 5.5 TraceFileWriter Note

`TraceFileWriter` is an event subscriber (subscribes to `ControlEventBus`), not a `Hook` subclass. It should remain as-is, not converted to per-point ABC. It is registered separately from the hook system.

## 6. Migration Path

### Phase 1: Delete Dead Code
1. Delete 6 interceptor files + 4 hook files
2. Remove from `__init__.py` exports
3. Delete associated test files
4. Update AGENTS.md references

### Phase 2: Interceptor ABC Migration
1. Create per-scope ABCs in `abc.py` (alongside existing Protocol temporarily)
2. Migrate `ControlDrainInterceptor` and `ToolResultLimitInterceptor` to new ABCs
3. Update `InterceptorChain` type hints from `Interceptor` Protocol to `Interceptor` ABC
4. Remove old Protocol definition

### Phase 3: Hook ABC Migration
1. Create per-point ABCs in `abc.py` (alongside existing Protocol temporarily)
2. Migrate 5 surviving hooks to new ABCs (`RunLoggingHook`, `RuntimeContextHook`, `InboxFlushHook`, `SubagentAutoSendHook`, `ProgressReportHook`)
3. Update `HookRunner` type hints
4. Remove old Protocol definition

### Phase 4: bot_project Update
1. Update imports in `bot/service/core.py` and `bot/service/pool_builder.py`
2. Update test assertions in `tests/test_runtime_defaults.py`

## 7. Files Changed

### Modified
| File | Change |
|------|--------|
| `framework/interceptor/abc.py` | Replace Protocol with ABC hierarchy |
| `framework/interceptor/builtin/__init__.py` | Remove dead exports, update retained ones |
| `framework/interceptor/builtin/control_drain.py` | Migrate to ABC inheritance |
| `framework/interceptor/builtin/result_limit.py` | Migrate to ABC inheritance |
| `framework/hook/abc.py` | Replace Protocol with ABC hierarchy |
| `framework/hook/builtin/__init__.py` | Remove dead exports, update retained ones |
| `framework/hook/builtin/logging.py` | Migrate to ABC inheritance |
| `framework/hook/builtin/runtime_context.py` | Migrate to ABC inheritance |
| `framework/hook/builtin/inbox_flush.py` | Migrate to ABC inheritance |
| `framework/hook/builtin/subagent_auto_send.py` | Migrate to ABC inheritance |
| `framework/hook/builtin/progress_report.py` | Migrate to ABC inheritance |
| `framework/interceptor/chain.py` | Update type hints (logic unchanged) |
| `framework/hook/runner.py` | Update type hints (logic unchanged) |
| `examples/bot_project/bot/service/core.py` | Update imports |
| `examples/bot_project/bot/service/pool_builder.py` | Update imports |

### Deleted
| File | Reason |
|------|--------|
| `framework/interceptor/builtin/turn_timeout.py` | Dead code |
| `framework/interceptor/builtin/tool_timeout.py` | Dead code |
| `framework/interceptor/builtin/tool_watch.py` | Dead code |
| `framework/interceptor/builtin/llm_stream_watch.py` | Dead code |
| `framework/interceptor/builtin/steer_inject.py` | Dead code |
| `framework/interceptor/builtin/tool_policy_interceptor.py` | Dead code |
| `framework/hook/builtin/subagent_cleanup.py` | Dead code |
| `framework/hook/builtin/dynamic_tool_filter.py` | Dead code |
| `framework/hook/builtin/llm_output_guard.py` | Dead code |
| `framework/hook/builtin/tool_result_transform.py` | Dead code |
| Associated test files for all above | Dead tests for dead code |

## 8. Risk Assessment

| Risk | Mitigation |
|------|------------|
| Multi-inheritance MRO conflict | Only single-level multi-inheritance; all ABCs share `Interceptor`/`Hook` as sole root. No diamond problem. |
| `getattr` dispatch breaks on ABC | ABC methods are real methods on the instance. `getattr` finds them. Verified by existing pattern. |
| bot_project test breakage | Phase 4 updates test assertions after all migrations complete. |
| External consumers of deleted classes | Deleted classes have zero production instantiation. Only test files reference them. |

## 9. Out of Scope

- Configuration-driven interceptor/hook registration (future work)
- Reserved scope activation (AGENT_RUN, LLM_CALL, PIPELINE_STEP, POOL_TASK)
- `TraceFileWriter` redesign (it's an event subscriber, not a Hook)
- `MaxIterationNotifyHook` migration (lives in `examples/`, not `framework/hook/builtin/`)
