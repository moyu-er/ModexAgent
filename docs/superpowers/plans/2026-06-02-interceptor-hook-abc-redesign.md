# Interceptor & Hook ABC Redesign — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace Protocol-based interceptor/hook with per-scope/per-point ABC hierarchy and delete all dead-code components.

**Architecture:** Per-scope ABC (`ToolCallInterceptor`, `TurnInterceptor`, etc.) with `_scope` class attribute; per-point ABC (`BeforeTurnHook`, etc.) with `_hook_point` class attribute. Multi-scope/multi-point via multiple inheritance; `scopes`/`hook_point` auto-derived from MRO. `InterceptorChain` and `HookRunner` dispatch logic unchanged.

**Tech Stack:** Python 3.12+, `abc.ABC`, `@abstractmethod`, `frozenset`, `asyncio`

**Spec:** `docs/superpowers/specs/2026-06-02-interceptor-hook-abc-redesign.md`

---

## File Structure

### Created (0)
No new files — all changes modify or delete existing files.

### Modified
| File | Responsibility |
|------|---------------|
| `framework/interceptor/abc.py` | Replace Protocol with ABC hierarchy + per-scope ABCs |
| `framework/interceptor/builtin/__init__.py` | Remove dead exports |
| `framework/interceptor/builtin/control_drain.py` | Migrate to `TurnInterceptor, IterationInterceptor` |
| `framework/interceptor/builtin/result_limit.py` | Migrate to `ToolCallInterceptor` |
| `framework/interceptor/chain.py` | Update type hints (logic unchanged) |
| `framework/interceptor/__init__.py` | Update exports |
| `framework/hook/abc.py` | Replace Protocol with ABC hierarchy + per-point ABCs |
| `framework/hook/builtin/__init__.py` | Remove dead exports |
| `framework/hook/builtin/logging.py` | Migrate to multi-point ABC |
| `framework/hook/builtin/runtime_context.py` | Migrate to per-point ABC |
| `framework/hook/builtin/inbox_flush.py` | Migrate to per-point ABC |
| `framework/hook/builtin/subagent_auto_send.py` | Migrate to per-point ABC |
| `framework/hook/builtin/progress_report.py` | Migrate to per-point ABC |
| `framework/hook/runner.py` | Update type hints (logic unchanged) |
| `framework/hook/__init__.py` | Update exports |
| `framework/agents/react/assembler.py` | Update `Interceptor` import |
| `framework/runtime/services.py` | Update type hints |
| `framework/multi_agent/factory.py` | Update type hints |
| `examples/bot_project/bot/service/core.py` | Update imports |
| `examples/bot_project/bot/service/pool_builder.py` | Update imports |

### Deleted
| File | Reason |
|------|--------|
| `framework/interceptor/builtin/turn_timeout.py` | Dead interceptor |
| `framework/interceptor/builtin/tool_timeout.py` | Dead interceptor |
| `framework/interceptor/builtin/tool_watch.py` | Dead interceptor |
| `framework/interceptor/builtin/llm_stream_watch.py` | Dead interceptor |
| `framework/interceptor/builtin/steer_inject.py` | Dead interceptor |
| `framework/interceptor/builtin/tool_policy_interceptor.py` | Dead interceptor |
| `framework/hook/builtin/subagent_cleanup.py` | Dead hook |
| `framework/hook/builtin/dynamic_tool_filter.py` | Dead hook |
| `framework/hook/builtin/llm_output_guard.py` | Dead hook |
| `framework/hook/builtin/tool_result_transform.py` | Dead hook |
| `tests/unit/test_steer_and_watch_interceptors.py` | Tests for deleted interceptors |
| `tests/unit/test_policy_interceptor.py` | Tests for deleted interceptor |
| `tests/unit/interceptor/test_tool_timeout_default.py` | Tests for deleted interceptor |
| `tests/unit/test_dynamic_tool_filter_hook.py` | Tests for deleted hook |
| `tests/unit/test_new_hooks.py` | Tests for deleted hooks (LLMOutputGuard, ToolResultTransform) |

---

## Phase 1: Delete Dead Code

### Task 1: Delete dead interceptor files

**Files:**
- Delete: `framework/interceptor/builtin/turn_timeout.py`
- Delete: `framework/interceptor/builtin/tool_timeout.py`
- Delete: `framework/interceptor/builtin/tool_watch.py`
- Delete: `framework/interceptor/builtin/llm_stream_watch.py`
- Delete: `framework/interceptor/builtin/steer_inject.py`
- Delete: `framework/interceptor/builtin/tool_policy_interceptor.py`

- [ ] **Step 1: Delete the 6 interceptor source files**

```bash
Remove-Item -LiteralPath "framework/interceptor/builtin/turn_timeout.py"
Remove-Item -LiteralPath "framework/interceptor/builtin/tool_timeout.py"
Remove-Item -LiteralPath "framework/interceptor/builtin/tool_watch.py"
Remove-Item -LiteralPath "framework/interceptor/builtin/llm_stream_watch.py"
Remove-Item -LiteralPath "framework/interceptor/builtin/steer_inject.py"
Remove-Item -LiteralPath "framework/interceptor/builtin/tool_policy_interceptor.py"
```

- [ ] **Step 2: Update `framework/interceptor/builtin/__init__.py`**

Replace the entire file with only the surviving exports:

```python
"""Built-in Interceptor implementations.

Framework-provided interceptors:
- control_drain: ControlDrainInterceptor
- result_limit: ToolResultLimitInterceptor
- tool_approval: ArgumentMatcher (tool path classification helper)
"""

from framework.interceptor.builtin.control_drain import ControlDrainInterceptor
from framework.interceptor.builtin.result_limit import ToolResultLimitInterceptor
from framework.interceptor.builtin.tool_approval import ArgumentMatcher

__all__ = [
    "ArgumentMatcher",
    "ControlDrainInterceptor",
    "ToolResultLimitInterceptor",
]
```

- [ ] **Step 3: Commit**

```bash
git add -A framework/interceptor/builtin/
git commit -m "refactor(interceptor): delete 6 unused built-in interceptors"
```

---

### Task 2: Delete dead hook files

**Files:**
- Delete: `framework/hook/builtin/subagent_cleanup.py`
- Delete: `framework/hook/builtin/dynamic_tool_filter.py`
- Delete: `framework/hook/builtin/llm_output_guard.py`
- Delete: `framework/hook/builtin/tool_result_transform.py`

- [ ] **Step 1: Delete the 4 hook source files**

```bash
Remove-Item -LiteralPath "framework/hook/builtin/subagent_cleanup.py"
Remove-Item -LiteralPath "framework/hook/builtin/dynamic_tool_filter.py"
Remove-Item -LiteralPath "framework/hook/builtin/llm_output_guard.py"
Remove-Item -LiteralPath "framework/hook/builtin/tool_result_transform.py"
```

- [ ] **Step 2: Update `framework/hook/builtin/__init__.py`**

Replace with only surviving exports:

```python
"""Built-in Hook implementations.

Framework-provided hooks:
- logging: RunLoggingHook
- runtime_context: RuntimeContextHook
- inbox_flush: InboxFlushHook
- subagent_auto_send: SubagentAutoSendHook
- progress_report: ProgressReportHook
- trace_writer: TraceFileWriter (event subscriber, not a Hook subclass)
"""

from framework.hook.builtin.inbox_flush import InboxFlushHook
from framework.hook.builtin.logging import RunLoggingHook
from framework.hook.builtin.progress_report import ProgressReportHook
from framework.hook.builtin.runtime_context import RuntimeContextHook
from framework.hook.builtin.subagent_auto_send import SubagentAutoSendHook
from framework.hook.builtin.trace_writer import TraceFileWriter

__all__ = [
    "InboxFlushHook",
    "ProgressReportHook",
    "RunLoggingHook",
    "RuntimeContextHook",
    "SubagentAutoSendHook",
    "TraceFileWriter",
]
```

- [ ] **Step 3: Commit**

```bash
git add -A framework/hook/builtin/
git commit -m "refactor(hook): delete 4 unused built-in hooks"
```

---

### Task 3: Delete dead test files

**Files:**
- Delete: `tests/unit/test_steer_and_watch_interceptors.py`
- Delete: `tests/unit/test_policy_interceptor.py`
- Delete: `tests/unit/interceptor/test_tool_timeout_default.py`
- Delete: `tests/unit/test_dynamic_tool_filter_hook.py`
- Delete: `tests/unit/test_new_hooks.py`

- [ ] **Step 1: Delete the 5 test files**

```bash
Remove-Item -LiteralPath "tests/unit/test_steer_and_watch_interceptors.py"
Remove-Item -LiteralPath "tests/unit/test_policy_interceptor.py"
Remove-Item -LiteralPath "tests/unit/interceptor/test_tool_timeout_default.py"
Remove-Item -LiteralPath "tests/unit/test_dynamic_tool_filter_hook.py"
Remove-Item -LiteralPath "tests/unit/test_new_hooks.py"
```

- [ ] **Step 2: Verify remaining tests still pass**

Run: `pytest tests/unit/ -v --timeout=30`
Expected: All remaining tests pass (some may fail due to import changes from Task 1-2 — those get fixed in Phase 2)

- [ ] **Step 3: Commit**

```bash
git add -A tests/unit/
git commit -m "test: delete tests for removed interceptors and hooks"
```

---

## Phase 2: Interceptor ABC Migration

### Task 4: Rewrite `framework/interceptor/abc.py` with ABC hierarchy

**Files:**
- Modify: `framework/interceptor/abc.py`

- [ ] **Step 1: Replace the file with the new ABC hierarchy**

The new file keeps all existing context dataclasses (`LLMRequest`, `ToolCallContext`, `TurnContext`, `IterationContext`, `LLMCallContext`, `LLMStreamContext`, `LLMStreamChunk`) and next-call type aliases (`ToolCallNext`, `TurnNext`, `IterationNext`, `LLMStreamNext`) unchanged. Only the `Interceptor` Protocol section (lines 141-186) changes.

Replace the `Interceptor` Protocol class and everything below it with:

```python
# ---------------------------------------------------------------------------
# Interceptor ABC Hierarchy
# ---------------------------------------------------------------------------

class Interceptor(ABC):
    """All interceptors' public base class.

    Replaces the old Protocol. Each concrete interceptor inherits from
    one or more per-scope ABCs (ToolCallInterceptor, TurnInterceptor, etc.).
    The `scopes` property is auto-derived from MRO — no manual declaration needed.
    """

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
            s = getattr(cls, "_scope", None)
            if s is not None:
                result.add(s)
        return frozenset(result)


class ToolCallInterceptor(Interceptor):
    """TOOL_CALL scope interceptor ABC."""

    _scope = InterceptorScope.TOOL_CALL

    @abstractmethod
    async def around_tool_call(
        self,
        ctx: AgentContext,
        call: ToolCallContext,
        next_call: ToolCallNext,
    ) -> ToolResult:
        """Wrap individual tool call execution. Must return a legal ToolResult."""
        ...


class TurnInterceptor(Interceptor):
    """TURN scope interceptor ABC."""

    _scope = InterceptorScope.TURN

    @abstractmethod
    async def around_turn(
        self,
        ctx: AgentContext,
        next_call: TurnNext,
    ) -> AgentResult:
        """Wrap entire turn execution."""
        ...


class IterationInterceptor(Interceptor):
    """ITERATION scope interceptor ABC."""

    _scope = InterceptorScope.ITERATION

    @abstractmethod
    async def around_iteration(
        self,
        ctx: AgentContext,
        call: IterationContext,
        next_call: IterationNext,
    ) -> None:
        """Wrap single ReAct iteration."""
        ...


class LLMStreamInterceptor(Interceptor):
    """LLM_STREAM scope interceptor ABC."""

    _scope = InterceptorScope.LLM_STREAM

    async def around_llm_stream(
        self,
        ctx: AgentContext,
        call: LLMStreamContext,
        next_stream: LLMStreamNext,
    ) -> AsyncIterator[LLMStreamChunk]:
        """Wrap LLM streaming response. Default: pass-through."""
        async for chunk in next_stream():
            yield chunk
        return
```

Note: `LLMStreamInterceptor.around_llm_stream` has a default pass-through implementation (not abstract) because it's currently unused by any surviving interceptor. This allows future interceptors to opt-in without forcing implementation.

- [ ] **Step 2: Update `framework/interceptor/__init__.py` exports**

Add the new per-scope ABC class names to the public API:

```python
"""framework.interceptor — AOP interceptor chain.

Provides:
- InterceptorScope scope enum
- Interceptor base ABC + per-scope ABCs (ToolCallInterceptor, TurnInterceptor, etc.)
- Scope context types and next-call signatures
- InterceptorChain onion-chain executor
- Built-in interceptor implementations
"""

from framework.interceptor.abc import (
    Interceptor,
    InterceptorScope,
    IterationContext,
    IterationInterceptor,
    IterationNext,
    LLMCallContext,
    LLMStreamChunk,
    LLMStreamContext,
    LLMStreamInterceptor,
    LLMStreamNext,
    ToolCallContext,
    ToolCallInterceptor,
    ToolCallNext,
    TurnContext,
    TurnInterceptor,
    TurnNext,
)
from framework.interceptor.chain import InterceptorChain
from framework.interceptor.handler import (
    CommandHandlerRegistry,
    ControlCommandHandler,
    DefaultCancelHandler,
)

__all__ = [
    "CommandHandlerRegistry",
    "ControlCommandHandler",
    "DefaultCancelHandler",
    "Interceptor",
    "InterceptorChain",
    "InterceptorScope",
    "IterationContext",
    "IterationInterceptor",
    "IterationNext",
    "LLMCallContext",
    "LLMStreamChunk",
    "LLMStreamContext",
    "LLMStreamInterceptor",
    "LLMStreamNext",
    "ToolCallContext",
    "ToolCallInterceptor",
    "ToolCallNext",
    "TurnContext",
    "TurnInterceptor",
    "TurnNext",
]
```

- [ ] **Step 3: Commit**

```bash
git add framework/interceptor/abc.py framework/interceptor/__init__.py
git commit -m "refactor(interceptor): replace Protocol with per-scope ABC hierarchy"
```

---

### Task 5: Migrate `ControlDrainInterceptor` to ABC

**Files:**
- Modify: `framework/interceptor/builtin/control_drain.py`

- [ ] **Step 1: Update class declaration**

In `framework/interceptor/builtin/control_drain.py`, change:

```python
class ControlDrainInterceptor:
    """控制命令消费拦截器。"""
    scopes = frozenset([InterceptorScope.TURN, InterceptorScope.ITERATION])
```

To:

```python
from framework.interceptor.abc import InterceptorScope, IterationInterceptor, TurnInterceptor

class ControlDrainInterceptor(TurnInterceptor, IterationInterceptor):
    """控制命令消费拦截器。

    同时包裹 TURN 和 ITERATION 边界，消费控制命令并转为运行时动作。
    """
```

Add the `name` property:

```python
    @property
    def name(self) -> str:
        return "control_drain"
```

Remove the old `scopes` class attribute (now auto-derived from MRO).

Remove the import of `InterceptorScope` from the class-level scope list — it's still needed for `_drain_and_handle` method's command_types set, so keep the import at the top.

- [ ] **Step 2: Verify method signatures match ABC**

The existing `around_turn` and `around_iteration` methods already match the ABC signatures. No signature changes needed.

- [ ] **Step 3: Commit**

```bash
git add framework/interceptor/builtin/control_drain.py
git commit -m "refactor(interceptor): migrate ControlDrainInterceptor to per-scope ABC"
```

---

### Task 6: Migrate `ToolResultLimitInterceptor` to ABC

**Files:**
- Modify: `framework/interceptor/builtin/result_limit.py`

- [ ] **Step 1: Update class declaration**

Change:

```python
class ToolResultLimitInterceptor:
    scopes = frozenset([InterceptorScope.TOOL_CALL])
```

To:

```python
from framework.interceptor.abc import ToolCallInterceptor

class ToolResultLimitInterceptor(ToolCallInterceptor):
    """Tool result overflow interceptor."""
```

Add the `name` property:

```python
    @property
    def name(self) -> str:
        return "tool_result_limit"
```

Remove the old `scopes` class attribute. Remove the now-unused `InterceptorScope` import (check if it's used elsewhere in the file — it's not, only `InterceptorScope` import is for the scopes frozenset).

- [ ] **Step 2: Verify method signature matches ABC**

The existing `around_tool_call` already matches `ToolCallInterceptor.around_tool_call`. No signature change needed.

- [ ] **Step 3: Commit**

```bash
git add framework/interceptor/builtin/result_limit.py
git commit -m "refactor(interceptor): migrate ToolResultLimitInterceptor to per-scope ABC"
```

---

### Task 7: Update `InterceptorChain` type hints

**Files:**
- Modify: `framework/interceptor/chain.py`

- [ ] **Step 1: Update type annotations**

In `framework/interceptor/chain.py`, the `InterceptorChain` generic parameter and `_interceptors` list currently use `Interceptor[R]` from the old Protocol. Update:

Change:
```python
from framework.interceptor.abc import (
    Interceptor,
    ...
)

class InterceptorChain(Generic[R]):
    def __init__(self, interceptors: list[Interceptor[R]] | None = None) -> None:
        self._interceptors: list[Interceptor[R]] = ...
```

The `Interceptor` import from `framework.interceptor.abc` still resolves to the new ABC base class. The type parameter `R` was used with the old Generic Protocol. Since the new ABC is not generic over `R`, remove the generic parameter:

```python
from framework.interceptor.abc import (
    Interceptor,
    InterceptorScope,
    IterationContext,
    IterationNext,
    LLMStreamChunk,
    LLMStreamContext,
    LLMStreamNext,
    ToolCallContext,
    ToolCallNext,
    TurnNext,
)

class InterceptorChain:
    """AOP onion-chain executor.

    Interceptors are ordered by list index; index 0 is outermost.
    Active scopes: TOOL_CALL, TURN, ITERATION, LLM_STREAM.
    """

    def __init__(self, interceptors: list[Interceptor] | None = None) -> None:
        self._interceptors: list[Interceptor] = list(interceptors) if interceptors else []

    @property
    def interceptors(self) -> list[Interceptor]:
        return list(self._interceptors)

    def add(self, interceptor: Interceptor) -> None:
        self._interceptors.append(interceptor)

    def insert(self, index: int, interceptor: Interceptor) -> None:
        self._interceptors.insert(index, interceptor)

    def extend(self, interceptors: list[Interceptor]) -> None:
        self._interceptors.extend(interceptors)
```

Also update the `_resolved` method return type:
```python
    def _resolved(self, scope: InterceptorScope) -> list[Interceptor]:
        return [i for i in self._interceptors if scope in i.scopes]
```

Remove the `R` TypeVar import if no longer needed.

- [ ] **Step 2: Run interceptor chain tests**

Run: `pytest tests/unit/test_interceptor_chain.py -v`
Expected: All tests pass (chain logic unchanged)

- [ ] **Step 3: Commit**

```bash
git add framework/interceptor/chain.py
git commit -m "refactor(interceptor): update InterceptorChain type hints for ABC"
```

---

### Task 8: Update downstream framework imports

**Files:**
- Modify: `framework/agents/react/assembler.py`
- Modify: `framework/runtime/services.py`
- Modify: `framework/multi_agent/factory.py`

- [ ] **Step 1: Update `framework/agents/react/assembler.py`**

Line 21-22 currently imports:
```python
from framework.interceptor.abc import Interceptor
from framework.interceptor.chain import InterceptorChain
```

These imports still work because `Interceptor` is now the ABC base class. No change needed to imports. Verify `RuntimeServicesConfig.interceptors: list[Interceptor] | None` resolves correctly — it does because the name is the same.

- [ ] **Step 2: Update `framework/runtime/services.py`**

Line 22 currently:
```python
from framework.interceptor.chain import InterceptorChain
```

No change needed — `InterceptorChain` import still works.

The `AgentRuntimeServices.interceptors: InterceptorChain | None` type hint (line 35) still works.

The `AgentRuntime.validate()` method (lines 99-108) imports `ControlDrainInterceptor` — this still works.

- [ ] **Step 3: Update `framework/multi_agent/factory.py`**

Lines 250-253 and 292-295 create `InterceptorChain` copies. No import changes needed.

- [ ] **Step 4: Verify no other broken imports**

Run: `pytest tests/unit/test_interceptor_chain.py tests/unit/test_control_drain_interceptor.py tests/unit/interceptor/test_tool_result_limit_overflow.py tests/unit/interceptor/test_runtime_state_interceptors.py -v`
Expected: All pass

- [ ] **Step 5: Commit**

```bash
git add framework/agents/react/assembler.py framework/runtime/services.py framework/multi_agent/factory.py
git commit -m "refactor(interceptor): verify downstream framework imports compatible with ABC"
```

---

## Phase 3: Hook ABC Migration

### Task 9: Rewrite `framework/hook/abc.py` with ABC hierarchy

**Files:**
- Modify: `framework/hook/abc.py`

- [ ] **Step 1: Replace the Hook Protocol with ABC hierarchy**

Keep all existing types: `HookPoint`, `HookErrorPolicy`, `HookPayload`, `HookResult`, `HookSpec`. Only replace the `Hook` Protocol class (lines 92-157).

Replace the `Hook` Protocol with:

```python
class Hook(ABC, Generic[R]):
    """All hooks' public base class.

    Replaces the old Protocol. Each concrete hook inherits from one or more
    per-point ABCs (BeforeTurnHook, AfterTurnHook, etc.).
    The `hook_point` property is auto-derived from MRO.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique hook name for logging and diagnostics."""
        ...


class BeforeTurnHook(Hook[R]):
    _hook_point = HookPoint.BEFORE_TURN

    @abstractmethod
    async def before_turn(self, ctx: AgentContext[R]) -> None: ...


class AfterTurnHook(Hook[R]):
    _hook_point = HookPoint.AFTER_TURN

    @abstractmethod
    async def after_turn(self, ctx: AgentContext[R], result: AgentResult) -> None: ...


class BeforeIterationHook(Hook[R]):
    _hook_point = HookPoint.BEFORE_ITERATION

    @abstractmethod
    async def before_iteration(self, ctx: AgentContext[R]) -> None: ...


class AfterIterationHook(Hook[R]):
    _hook_point = HookPoint.AFTER_ITERATION

    @abstractmethod
    async def after_iteration(self, ctx: AgentContext[R]) -> None: ...


class BeforeToolExecutionHook(Hook[R]):
    _hook_point = HookPoint.BEFORE_TOOL_EXECUTION

    @abstractmethod
    async def before_tool_execution(
        self, ctx: AgentContext[R], tool_calls: Sequence[ToolCall]
    ) -> None: ...


class AfterToolExecutionHook(Hook[R]):
    _hook_point = HookPoint.AFTER_TOOL_EXECUTION

    @abstractmethod
    async def after_tool_execution(
        self, ctx: AgentContext[R], results: Sequence[ToolResult]
    ) -> None: ...


class AfterLLMResponseHook(Hook[R]):
    _hook_point = HookPoint.AFTER_LLM_RESPONSE

    @abstractmethod
    async def after_llm_response(
        self, ctx: AgentContext[R], response: LLMResponse
    ) -> None: ...


class OnControlCommandHook(Hook[R]):
    _hook_point = HookPoint.ON_CONTROL_COMMAND

    @abstractmethod
    async def on_control_command(self, ctx: AgentContext[R], command: Any) -> HookResult: ...


class FinalizeContentHook(Hook[R]):
    _hook_point = HookPoint.FINALIZE_CONTENT

    @abstractmethod
    def finalize_content(self, ctx: AgentContext[R], content: str | None) -> str | None: ...
```

- [ ] **Step 2: Update `framework/hook/__init__.py`**

Add per-point ABC class names to exports:

```python
from framework.hook.abc import (
    AfterIterationHook,
    AfterLLMResponseHook,
    AfterToolExecutionHook,
    AfterTurnHook,
    BeforeIterationHook,
    BeforeToolExecutionHook,
    BeforeTurnHook,
    FinalizeContentHook,
    Hook,
    HookErrorPolicy,
    HookPayload,
    HookPoint,
    HookResult,
    HookSpec,
    OnControlCommandHook,
)

__all__ = [
    "AfterIterationHook",
    "AfterLLMResponseHook",
    "AfterToolExecutionHook",
    "AfterTurnHook",
    "BeforeIterationHook",
    "BeforeToolExecutionHook",
    "BeforeTurnHook",
    "FinalizeContentHook",
    "Hook",
    "HookErrorPolicy",
    "HookPayload",
    "HookPoint",
    "HookResult",
    "HookSpec",
    "OnControlCommandHook",
]
```

Also export `HookRunner`:
```python
from framework.hook.runner import HookRunner
```

Add `"HookRunner"` to `__all__`.

- [ ] **Step 3: Commit**

```bash
git add framework/hook/abc.py framework/hook/__init__.py
git commit -m "refactor(hook): replace Protocol with per-point ABC hierarchy"
```

---

### Task 10: Migrate surviving hooks to ABC

**Files:**
- Modify: `framework/hook/builtin/logging.py`
- Modify: `framework/hook/builtin/runtime_context.py`
- Modify: `framework/hook/builtin/inbox_flush.py`
- Modify: `framework/hook/builtin/subagent_auto_send.py`
- Modify: `framework/hook/builtin/progress_report.py`

For each hook file:

1. Import the per-point ABC(s) instead of relying on Protocol
2. Change class declaration to inherit from the appropriate ABC(s)
3. Add `name` property
4. Remove any methods that were Protocol stubs (they're no longer needed)

**`RunLoggingHook`** — implements 4 hook points:
```python
from framework.hook.abc import (
    AfterIterationHook, AfterTurnHook, BeforeIterationHook, BeforeTurnHook, ...
)

class RunLoggingHook(BeforeTurnHook, AfterTurnHook, BeforeIterationHook, AfterIterationHook):
    @property
    def name(self) -> str: return "run_logging"
    # Keep existing before_turn, after_turn, before_iteration, after_iteration implementations
```

**`RuntimeContextHook`** — implements `before_turn` + `after_tool_execution`:
```python
from framework.hook.abc import AfterToolExecutionHook, BeforeTurnHook

class RuntimeContextHook(BeforeTurnHook, AfterToolExecutionHook):
    @property
    def name(self) -> str: return "runtime_context"
    # Keep existing implementations
```

**`InboxFlushHook`** — implements `before_turn`:
```python
from framework.hook.abc import BeforeTurnHook

class InboxFlushHook(BeforeTurnHook):
    @property
    def name(self) -> str: return "inbox_flush"
    # Keep existing before_turn implementation
```

**`SubagentAutoSendHook`** — implements `after_tool_execution`:
```python
from framework.hook.abc import AfterToolExecutionHook

class SubagentAutoSendHook(AfterToolExecutionHook):
    @property
    def name(self) -> str: return "subagent_auto_send"
    # Keep existing after_tool_execution implementation
```

**`ProgressReportHook`** — implements `after_tool_execution`:
```python
from framework.hook.abc import AfterToolExecutionHook

class ProgressReportHook(AfterToolExecutionHook):
    @property
    def name(self) -> str: return "progress_report"
    # Keep existing after_tool_execution implementation
```

For each file, remove any other lifecycle methods that were empty Protocol stubs (e.g., `async def after_iteration(...)` with `...` body). Only keep the methods required by the inherited ABCs.

- [ ] **Step 1: Migrate all 5 hooks**

- [ ] **Step 2: Verify hook tests pass**

Run: `pytest tests/unit/test_hooks.py tests/unit/test_hook_error_policy.py tests/unit/multi_agent/test_subagent_auto_send_hook.py tests/unit/multi_agent/inbox/test_inbox_flush_hook.py tests/unit/multi_agent/test_runtime_context_hook_integration.py -v`
Expected: All pass

- [ ] **Step 3: Commit**

```bash
git add framework/hook/builtin/
git commit -m "refactor(hook): migrate 5 surviving hooks to per-point ABC"
```

---

### Task 11: Update `HookRunner` type hints

**Files:**
- Modify: `framework/hook/runner.py`

- [ ] **Step 1: Update type annotations**

The `HookRunner` currently uses `HookSpec[R]` from the old Protocol. Since `Hook` is now ABC but still `Generic[R]`, the `HookSpec` and `HookRunner` generics should still work.

Verify the `dispatch` method's `getattr(hook, hook_point.value, None)` still works with ABC instances. Since ABC methods are real methods on the concrete class, `getattr` finds them. No logic change needed.

Only change: update the `Hook` import path comment if needed. The import `from framework.hook.abc import ...` still resolves correctly.

- [ ] **Step 2: Run hook runner tests**

Run: `pytest tests/unit/test_hook_error_policy.py tests/unit/test_hooks.py -v`
Expected: All pass

- [ ] **Step 3: Commit**

```bash
git add framework/hook/runner.py
git commit -m "refactor(hook): verify HookRunner compatible with ABC"
```

---

## Phase 4: bot_project Update

### Task 12: Update bot_project imports and tests

**Files:**
- Modify: `examples/bot_project/bot/service/core.py`
- Modify: `examples/bot_project/bot/service/pool_builder.py`
- Modify: `examples/bot_project/tests/test_runtime_defaults.py`

- [ ] **Step 1: Update `examples/bot_project/bot/service/core.py`**

The imports on lines 44-49 currently reference deleted classes. Remove references to deleted interceptors:
```python
from framework.interceptor.builtin import (
    ControlDrainInterceptor,
    ToolResultLimitInterceptor,
)
```
This still works — only the 2 surviving interceptors are imported.

Similarly for hooks on lines 43, 553, 627, 758, 780 — verify all imported hooks still exist.

- [ ] **Step 2: Update `examples/bot_project/bot/service/pool_builder.py`**

Line 10-11 imports `HookErrorPolicy, HookRunner, HookSpec` and `InboxFlushHook` — all still exist. No change needed.

- [ ] **Step 3: Update `examples/bot_project/tests/test_runtime_defaults.py`**

This test imports `ToolResultLimitInterceptor`, `ToolTimeoutInterceptor`, and `TurnTimeoutInterceptor`. The latter two are deleted. Update the test:

Remove imports of deleted interceptors:
```python
from framework.interceptor.builtin import ToolResultLimitInterceptor
# Remove: ToolTimeoutInterceptor (deleted)
# Remove: TurnTimeoutInterceptor (deleted)
```

Update the test assertions to only check surviving interceptors:
```python
def test_default_interceptor_chain_keeps_only_effective_defaults() -> None:
    chain = service._build_interceptor_chain()
    interceptors = chain.interceptors

    assert any(isinstance(item, ToolResultLimitInterceptor) for item in interceptors)
    # Removed assertions for deleted interceptors
```

- [ ] **Step 4: Run bot_project tests**

Run: `python -m pytest examples/bot_project/tests -q`
Expected: All pass

- [ ] **Step 5: Commit**

```bash
git add examples/bot_project/
git commit -m "refactor(examples): update bot_project for ABC migration"
```

---

## Phase 5: Final Verification

### Task 13: Full test suite verification

**Files:**
- All modified files

- [ ] **Step 1: Run full unit test suite**

Run: `pytest tests/unit/ -v`
Expected: All tests pass. If any fail, investigate and fix — they should be import errors from stale references only.

- [ ] **Step 2: Run bot_project tests**

Run: `python -m pytest examples/bot_project/tests -q`
Expected: All pass

- [ ] **Step 3: Run ruff lint**

Run: `ruff check framework/interceptor/ framework/hook/ examples/bot_project/`
Expected: No errors

- [ ] **Step 4: Run mypy type check**

Run: `mypy framework/interceptor/ framework/hook/`
Expected: No errors (may need to fix a few type annotation issues from the Protocol→ABC transition)

- [ ] **Step 5: Final commit**

```bash
git add -A
git commit -m "refactor: complete interceptor/hook ABC redesign"
```

---

## Task Dependency Graph

```
Task 1 (delete dead interceptors) ──┐
Task 2 (delete dead hooks) ─────────┼── Task 3 (delete dead tests)
                                     │
Task 4 (interceptor ABC) ────────────┼── Task 5 (ControlDrain ABC)
                                     ├── Task 6 (ResultLimit ABC)
                                     └── Task 7 (InterceptorChain types)
                                           │
Task 8 (downstream framework) ─────────────┘
                                           │
Task 9 (hook ABC) ─────────────────────────┼── Task 10 (migrate 5 hooks)
                                           └── Task 11 (HookRunner types)
                                                   │
Task 12 (bot_project update) ──────────────────────┘
                                                   │
Task 13 (full verification) ───────────────────────┘
```

Tasks 1-2 are independent. Task 3 depends on 1-2. Tasks 4-8 are sequential. Tasks 9-11 are sequential but independent of 4-8 (can run in parallel if desired). Task 12 depends on both chains. Task 13 is final.
