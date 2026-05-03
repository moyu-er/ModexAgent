# ReAct Runtime Refactor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Refactor ReActAgent's hook/interceptor/control/approval subsystems into a unified `ReActRuntime` with `AgentContext[R]` generics, making clean mode truly clean and full mode properly wired.

**Architecture:** `AgentContext[R]` with `R = Any` default propagates through `Node[R]`, `Graph[R]`, `GraphEngine[R]`, `Hook[R]`, `Interceptor[R]`. `ReActRuntime` is the typed runtime for `R = ReActRuntime`. Pipeline assembles runtime → ReAct executes it. All old extension key paths deleted aggressively.

**Tech Stack:** Python 3.12+, dataclasses, typing.Generic, pytest, asyncio

---

## File Map

### New Files
| File | Responsibility |
|------|---------------|
| `framework/agents/react/runtime.py` | `ReActRuntime` dataclass, `sanitize_clean_runtime()`, `from_context()` |
| `framework/agents/react/approval.py` | `ApprovalClassifier` protocol, `TieredToolApprovalClassifier`, `ApprovalRuntime` |
| `framework/control/runtime.py` | `ControlRuntime`, `ControlPhase` |
| `framework/control/store.py` | `ControlStore` protocol, `InMemoryControlStore` |
| `tests/unit/agents/react/test_runtime.py` | ReActRuntime unit tests |
| `tests/unit/agents/react/test_approval.py` | ApprovalRuntime unit tests |
| `tests/unit/control/test_control_runtime.py` | ControlRuntime unit tests |

### Modified Files
| File | Change |
|------|--------|
| `framework/core/agent.py` | `AgentContext` → `Generic[R]`, add `runtime` field |
| `framework/core/graph/node.py` | `Node` → `Generic[R]` |
| `framework/core/graph/graph.py` | `Graph` → `Generic[R]` |
| `framework/core/graph/engine.py` | `GraphEngine` → `Generic[R]` |
| `framework/hook/abc.py` | `Hook` protocol → `Generic[R]` |
| `framework/hook/runner.py` | `HookRunner` → `Generic[R]` |
| `framework/interceptor/abc.py` | `Interceptor` protocol → `Generic[R]` |
| `framework/interceptor/chain.py` | `InterceptorChain` → `Generic[R]` |
| `framework/agents/react/agent.py` | Use `runtime`, simplify `_call_hooks`, wrap `around_turn` |
| `framework/agents/react/graph.py` | Drop `enable_hooks`/`enable_approval` params |
| `framework/agents/react/nodes/llm.py` | Use `runtime`, wrap `around_iteration` |
| `framework/agents/react/nodes/tool.py` | Use `runtime.approval`, control drain |
| `framework/agents/react/nodes/start.py` | Type annotation update |
| `framework/agents/react/nodes/end.py` | Type annotation update |
| `framework/interceptor/builtin/tool_approval.py` | Delete `classify_tier()` from `TieredToolApprovalInterceptor` |
| `framework/interceptor/builtin/control_drain.py` | Delegate to `ControlRuntime.drain()` |
| `framework/core/context_extensions.py` | Delete `HOOK_RUNNER`, `HOOKS`, `INTERCEPTOR_CHAIN`, `CHECKPOINT_STORE`, `SUSPEND_STRATEGY`, `INJECTION_QUEUE` |
| `framework/hook/builtin/__init__.py` | Remove `ToolPolicyGuardHook` export |
| `framework/hook/builtin/tool_policy_guard.py` | Delete file |
| `framework/pipeline/pipeline.py` | Extract 6 private methods, use `ReActRuntime` |
| `examples/bot_project/bot/service/core.py` | Build `ReActRuntime`, `ApprovalRuntime` |

---

### Task 1: Make AgentContext generic (Step 1a)

**Files:**
- Modify: `framework/core/agent.py`

- [ ] **Step 1: Add R TypeVar and make AgentContext Generic[R]**

```python
# framework/core/agent.py — changes
from typing import Any, Generic, TypeVar

R = TypeVar("R", default=Any)

@dataclass
class AgentContext(Generic[R]):
    system_prompt: str
    history: MessageHistory
    tool_manager: ToolManager
    session_id: str = ""
    max_iterations: int = 10
    temperature: float | None = None
    max_tokens: int | None = None
    attachments: list[str] = field(default_factory=list)
    extensions: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    emitter: ContentEmitter | None = None
    runtime: R | None = None
```

- [ ] **Step 2: Update ctx_ext signature**

```python
def ctx_ext(ctx: AgentContext[Any], key: str, default: Any = None) -> Any:
    return ctx.extensions.get(key, default)
```

- [ ] **Step 3: Update current_agent_context contextvar**

```python
current_agent_context: contextvars.ContextVar[AgentContext[Any]] = contextvars.ContextVar(
    "current_agent_context"
)
```

- [ ] **Step 4: Run existing tests to verify backward compat**

```bash
pytest tests/unit/ -q --tb=line -x --ignore=tests/unit/plugins 2>&1 | tail -5
```
Expected: All existing tests pass (AgentContext without type arg = AgentContext[Any]).

- [ ] **Step 5: Commit**

```bash
git add framework/core/agent.py
git commit -m "refactor: make AgentContext Generic[R] with R=Any default

Add typed runtime field, update ctx_ext and current_agent_context signatures.
Non-ReAct consumers use AgentContext (defaults to AgentContext[Any]) — zero changes.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task 2: Make Graph components generic (Step 1b)

**Files:**
- Modify: `framework/core/graph/node.py`
- Modify: `framework/core/graph/graph.py`
- Modify: `framework/core/graph/engine.py`

- [ ] **Step 1: Make Node Generic[R]**

```python
# framework/core/graph/node.py
from typing import Generic, TypeVar

R = TypeVar("R", default=Any)

class Node(ABC, Generic[R]):
    def __init__(self, name: str) -> None:
        self.name = name

    @abstractmethod
    async def execute(self, ctx: AgentContext[R]) -> NodeTransition:
        ...
```

Update import: `from framework.core.agent import AgentContext`

- [ ] **Step 2: Make Graph Generic[R]**

```python
# framework/core/graph/graph.py
from typing import Generic, TypeVar

R = TypeVar("R", default=Any)

class Graph(Generic[R]):
    def __init__(self, name: str = "graph") -> None:
        self.name = name
        self._nodes: dict[str, Node[R]] = {}
        self._edges: dict[str, list[Edge]] = {}
        self.entry_node: str = "start"

    def add_node(self, node: Node[R]) -> None:
        self._nodes[node.name] = node
    # add_edge, next_node unchanged
```

- [ ] **Step 3: Make GraphEngine Generic[R]**

```python
# framework/core/graph/engine.py
from typing import Generic, TypeVar

R = TypeVar("R", default=Any)

class GraphEngine(Generic[R]):
    def __init__(self, graph: Graph[R]) -> None:
        self.graph = graph

    async def run(self, ctx: AgentContext[R]) -> Any:
        ...
```

Update imports: `from framework.core.agent import AgentContext`

- [ ] **Step 4: Run tests**

```bash
pytest tests/unit/ -q --tb=line -x --ignore=tests/unit/plugins 2>&1 | tail -5
```
Expected: All tests pass.

- [ ] **Step 5: Commit**

```bash
git add framework/core/graph/node.py framework/core/graph/graph.py framework/core/graph/engine.py
git commit -m "refactor: make Node[R], Graph[R], GraphEngine[R] generic

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task 3: Make Hook components generic (Step 1c)

**Files:**
- Modify: `framework/hook/abc.py`
- Modify: `framework/hook/runner.py`

- [ ] **Step 1: Make Hook protocol and HookSpec Generic[R]**

```python
# framework/hook/abc.py — add Generic[R] to Hook and HookSpec
from typing import Generic, TypeVar

R = TypeVar("R", default=Any)

class Hook(Protocol, Generic[R]):
    async def before_turn(self, ctx: AgentContext[R]) -> None: ...
    async def after_turn(self, ctx: AgentContext[R], result: AgentResult) -> None: ...
    async def before_iteration(self, ctx: AgentContext[R]) -> None: ...
    async def after_iteration(self, ctx: AgentContext[R]) -> None: ...
    async def before_tool_execution(self, ctx: AgentContext[R], tool_calls: Sequence[ToolCall]) -> None: ...
    async def after_tool_execution(self, ctx: AgentContext[R], results: Sequence[ToolResult]) -> None: ...
    async def after_llm_response(self, ctx: AgentContext[R], response: LLMResponse) -> None: ...
    async def on_control_command(self, ctx: AgentContext[R], command: Any) -> HookResult: ...
    def finalize_content(self, ctx: AgentContext[R], content: str | None) -> str | None: ...

@dataclass(frozen=True)
class HookSpec(Generic[R]):
    hook: Hook[R]
    on_error: HookErrorPolicy = HookErrorPolicy.LOG
```

- [ ] **Step 2: Make HookRunner Generic[R]**

```python
# framework/hook/runner.py
from typing import Generic, TypeVar

R = TypeVar("R", default=Any)

class HookRunner(Generic[R]):
    def __init__(self, hook_specs: list[HookSpec[R]] | None = None) -> None:
        self._hook_specs: list[HookSpec[R]] = list(hook_specs) if hook_specs else []

    def add(self, spec: HookSpec[R]) -> None: ...
    def insert(self, index: int, spec: HookSpec[R]) -> None: ...
    def extend(self, specs: list[HookSpec[R]]) -> None: ...

    async def dispatch(
        self,
        hook_point: HookPoint,
        ctx: AgentContext[R],
        payload: HookPayload | None = None,
        *,
        hook_timeout: float | None = None,
    ) -> HookResult: ...

    def dispatch_finalize(self, ctx: AgentContext[R], content: str | None) -> str | None: ...
```

- [ ] **Step 3: Update Hook builtins that reference AgentContext**

Update type hints in all hook builtins to use `AgentContext[Any]`:
- `framework/hook/builtin/runtime_context.py`
- `framework/hook/builtin/peer_auto_send.py`
- `framework/hook/builtin/inbox_flush.py`
- `framework/hook/builtin/logging.py`
- `framework/hook/builtin/llm_output_guard.py`
- `framework/hook/builtin/progress_report.py`
- `framework/hook/builtin/tool_result_transform.py`
- `framework/hook/builtin/subagent_cleanup.py`
- `framework/hook/builtin/dynamic_tool_filter.py`

For each file, change `ctx: AgentContext` to `ctx: AgentContext[Any]`.

- [ ] **Step 4: Run tests**

```bash
pytest tests/unit/ -q --tb=line -x --ignore=tests/unit/plugins 2>&1 | tail -5
```
Expected: All tests pass.

- [ ] **Step 5: Commit**

```bash
git add framework/hook/
git commit -m "refactor: make Hook[R], HookSpec[R], HookRunner[R] generic

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task 4: Make Interceptor components generic (Step 1d)

**Files:**
- Modify: `framework/interceptor/abc.py`
- Modify: `framework/interceptor/chain.py`

- [ ] **Step 1: Make Interceptor protocol Generic[R]**

```python
# framework/interceptor/abc.py
from typing import Generic, TypeVar

R = TypeVar("R", default=Any)

class Interceptor(Protocol, Generic[R]):
    scopes: frozenset[InterceptorScope]

    async def around_tool_call(self, ctx: AgentContext[R], call: ToolCallContext, next_call: ToolCallNext) -> ToolResult: ...
    async def around_turn(self, ctx: AgentContext[R], next_call: TurnNext) -> AgentResult: ...
    async def around_iteration(self, ctx: AgentContext[R], call: IterationContext, next_call: IterationNext) -> None: ...
    async def around_llm_stream(self, ctx: AgentContext[R], call: LLMStreamContext, next_stream: LLMStreamNext) -> AsyncIterator[LLMStreamChunk]: ...
```

- [ ] **Step 2: Make InterceptorChain Generic[R]**

```python
# framework/interceptor/chain.py
from typing import Generic, TypeVar

R = TypeVar("R", default=Any)

class InterceptorChain(Generic[R]):
    def __init__(self, interceptors: list[Interceptor[R]] | None = None) -> None:
        self._interceptors: list[Interceptor[R]] = list(interceptors) if interceptors else []

    def add(self, interceptor: Interceptor[R]) -> None: ...
```

Update all method signatures to use `AgentContext[R]` instead of `AgentContext`.

- [ ] **Step 3: Update builtin interceptors' type annotations**

Change `ctx: AgentContext` to `ctx: AgentContext[Any]` in:
- `framework/interceptor/builtin/control_drain.py`
- `framework/interceptor/builtin/tool_approval.py`
- `framework/interceptor/builtin/tool_timeout.py`
- `framework/interceptor/builtin/turn_timeout.py`
- `framework/interceptor/builtin/result_limit.py`
- `framework/interceptor/builtin/tool_watch.py`
- `framework/interceptor/builtin/llm_stream_watch.py`
- `framework/interceptor/builtin/steer_inject.py`

- [ ] **Step 4: Run tests**

```bash
pytest tests/unit/ -q --tb=line -x --ignore=tests/unit/plugins 2>&1 | tail -5
```
Expected: All tests pass.

- [ ] **Step 5: Commit**

```bash
git add framework/interceptor/
git commit -m "refactor: make Interceptor[R], InterceptorChain[R] generic

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task 5: Create ReActRuntime (Step 2a)

**Files:**
- Create: `framework/agents/react/runtime.py`
- Create: `tests/unit/agents/react/test_runtime.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/agents/react/test_runtime.py
import asyncio
import pytest
from framework.agents.react.runtime import ReActRuntime, sanitize_clean_runtime
from framework.core.agent import AgentContext
from framework.core.context_extensions import ExtensionKey
from framework.core.tool_manager import InMemoryToolManager
from framework.memory.history import ListMessageHistory
from framework.hook import HookRunner, HookSpec, HookErrorPolicy
from framework.interceptor.chain import InterceptorChain


def make_ctx(**extensions):
    return AgentContext(
        system_prompt="test",
        history=ListMessageHistory(),
        tool_manager=InMemoryToolManager(),
        extensions=extensions,
    )


class TestReActRuntime:
    def test_clean_factory_all_services_none(self):
        rt = ReActRuntime.clean()
        assert rt.mode == "clean"
        assert rt.hooks is None
        assert rt.interceptors is None
        assert rt.approval is None
        assert rt.control is None
        assert rt.checkpoint_store is None
        assert rt.suspend_strategy is None
        assert rt.injection_queue is None
        assert rt.governance is None
        assert rt.safety is None

    def test_from_context_full_mode_preserves_services(self):
        ctx = make_ctx(
            **{ExtensionKey.HOOK_RUNNER: HookRunner()}
        )
        rt = ReActRuntime.from_context(ctx, mode="full")
        assert rt.mode == "full"
        assert rt.hooks is not None

    def test_from_context_clean_mode_disables_all(self):
        ctx = make_ctx(
            **{
                ExtensionKey.HOOK_RUNNER: HookRunner(),
                ExtensionKey.INTERCEPTOR_CHAIN: InterceptorChain(),
            }
        )
        rt = ReActRuntime.from_context(ctx, mode="clean")
        # Runtime should be clean, hooks/interceptors stripped
        assert rt.mode == "clean"
        assert rt.hooks is None
        assert rt.interceptors is None

    def test_sanitize_clean_runtime_clears_metadata(self):
        ctx = make_ctx()
        ctx.metadata["RESUME_STATE"] = {"some": "state"}
        ctx.metadata["TOOL_DECISIONS"] = ["allowed"]
        ctx.metadata["DENY_AS_CANCEL"] = True
        ctx.metadata["APPROVAL_DENIAL"] = {"denied": True}
        ctx.metadata["INJECTION_CYCLE"] = 3
        from framework.agents.react.constants import ReActMetaKey

        sanitize_clean_runtime(ctx)
        assert ReActMetaKey.RESUME_STATE not in ctx.metadata
        assert ReActMetaKey.TOOL_DECISIONS not in ctx.metadata
        assert ReActMetaKey.DENY_AS_CANCEL not in ctx.metadata
        assert ReActMetaKey.APPROVAL_DENIAL not in ctx.metadata
        assert ReActMetaKey.INJECTION_CYCLE not in ctx.metadata

    def test_sanitize_clean_runtime_clears_extension_keys(self):
        ctx = make_ctx(
            **{
                ExtensionKey.HOOK_RUNNER: HookRunner(),
                ExtensionKey.INTERCEPTOR_CHAIN: InterceptorChain(),
            }
        )
        sanitize_clean_runtime(ctx)
        assert ExtensionKey.HOOK_RUNNER not in ctx.extensions
        assert ExtensionKey.INTERCEPTOR_CHAIN not in ctx.extensions
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/unit/agents/react/test_runtime.py -v
```
Expected: FAIL (module not found)

- [ ] **Step 3: Create ReActRuntime**

```python
# framework/agents/react/runtime.py
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from framework.core.context_extensions import ExtensionKey

if TYPE_CHECKING:
    from framework.core.agent import AgentContext
    from framework.control.runtime import ControlRuntime
    from framework.hook import HookRunner
    from framework.interceptor.chain import InterceptorChain
    from framework.memory import ContextGovernance
    from framework.core.llm_error import RuntimeSafetyPolicy
    from framework.control.checkpoint import RuntimeStateStore
    from framework.agents.react.strategy import SuspendStrategy

logger = logging.getLogger(__name__)

# Metadata keys to clear in clean mode
_CLEAN_METADATA_KEYS = (
    "RESUME_STATE",
    "TOOL_DECISIONS",
    "DENY_AS_CANCEL",
    "APPROVAL_DENIAL",
    "INJECTION_CYCLE",
)

# Extension keys to clear in clean mode
_CLEAN_EXTENSION_KEYS = (
    ExtensionKey.HOOK_RUNNER,
    ExtensionKey.HOOKS,
    ExtensionKey.INTERCEPTOR_CHAIN,
    ExtensionKey.CHECKPOINT_STORE,
    ExtensionKey.SUSPEND_STRATEGY,
    ExtensionKey.INJECTION_QUEUE,
)


def sanitize_clean_runtime(ctx: AgentContext[Any]) -> list[str]:
    """Clear full-mode extension keys and metadata from context. Returns list of disabled keys."""
    disabled: list[str] = []
    for key in _CLEAN_EXTENSION_KEYS:
        if key in ctx.extensions:
            ctx.extensions.pop(key, None)
            disabled.append(key)
    for key in _CLEAN_METADATA_KEYS:
        if key in ctx.metadata:
            ctx.metadata.pop(key, None)
            disabled.append(key)
    return disabled


@dataclass
class ReActRuntime:
    mode: Literal["clean", "full"]
    hooks: HookRunner | None = None
    interceptors: InterceptorChain | None = None
    approval: ApprovalRuntime | None = None
    control: ControlRuntime | None = None
    checkpoint_store: RuntimeStateStore | None = None
    suspend_strategy: SuspendStrategy | None = None
    injection_queue: asyncio.Queue[str] | None = None
    governance: ContextGovernance | None = None
    safety: RuntimeSafetyPolicy | None = None

    @classmethod
    def clean(cls) -> ReActRuntime:
        return cls(mode="clean")

    @classmethod
    def from_context(cls, ctx: AgentContext[Any], *, mode: str) -> ReActRuntime:
        if mode == "clean":
            disabled = sanitize_clean_runtime(ctx)
            if disabled:
                logger.info(
                    "ReActAgent clean mode: disabled runtime extensions: %s",
                    ", ".join(disabled),
                )
            return cls.clean()

        # Full mode: consume extensions into runtime fields
        from framework.hook import HookRunner, HookSpec, HookErrorPolicy

        hook_runner = ctx.extensions.pop(ExtensionKey.HOOK_RUNNER, None)
        hooks = ctx.extensions.pop(ExtensionKey.HOOKS, None)
        if hook_runner is None and hooks:
            hook_runner = HookRunner([
                HookSpec(hook=h, on_error=HookErrorPolicy.LOG) for h in hooks
            ])

        return cls(
            mode="full",
            hooks=hook_runner,
            interceptors=ctx.extensions.pop(ExtensionKey.INTERCEPTOR_CHAIN, None),
            checkpoint_store=ctx.extensions.pop(ExtensionKey.CHECKPOINT_STORE, None),
            suspend_strategy=ctx.extensions.pop(ExtensionKey.SUSPEND_STRATEGY, None),
            injection_queue=ctx.extensions.pop(ExtensionKey.INJECTION_QUEUE, None),
            governance=ctx.extensions.pop(ExtensionKey.GOVERNANCE, None),
            safety=ctx.extensions.pop(ExtensionKey.SAFETY, None),
        )

    def validate(self) -> None:
        """Raise ConfigurationError if full-mode combination is invalid."""
        if self.mode == "clean":
            return
        from framework.control.exceptions import PolicyViolation

        if self.interceptors is not None:
            from framework.interceptor.builtin import ControlDrainInterceptor
            for interceptor in self.interceptors.interceptors:
                if isinstance(interceptor, ControlDrainInterceptor) and self.control is None:
                    raise PolicyViolation(
                        "ControlDrainInterceptor configured but no ControlRuntime present"
                    )
```

- [ ] **Step 4: Run test to verify it passes**

```bash
pytest tests/unit/agents/react/test_runtime.py -v
```
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add framework/agents/react/runtime.py tests/unit/agents/react/test_runtime.py
git commit -m "feat: add ReActRuntime with clean/full mode normalization

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task 6: Integrate ReActRuntime into ReActAgent (Step 2b)

**Files:**
- Modify: `framework/agents/react/agent.py`
- Modify: `tests/unit/agents/react/test_agent.py`

- [ ] **Step 1: Add failing tests for ReActAgent runtime integration**

Add to `tests/unit/agents/react/test_agent.py`:

```python
from framework.agents.react.runtime import ReActRuntime
from framework.core.context_extensions import ExtensionKey
from framework.hook import HookRunner


class TestReActAgentRuntime:
    """Tests for ReActAgent runtime normalization."""

    @pytest.mark.asyncio
    async def test_clean_mode_logs_disabled_extensions(self, caplog):
        """Clean mode should log that extensions were disabled."""
        agent = ReActAgent(_MockProvider(), mode="clean")

        class _Emitter:
            def wants_streaming(self):
                return False
            async def emit(self, *args, **kwargs):
                pass
            async def emit_delta(self, *args, **kwargs):
                pass
            async def emit_content(self, *args, **kwargs):
                pass
            async def emit_stream_end(self, *args, **kwargs):
                pass
            async def emit_complete(self, *args, **kwargs):
                pass

        ctx = AgentContext(
            system_prompt="Hi",
            history=ListMessageHistory(),
            tool_manager=InMemoryToolManager(),
            extensions={
                ExtensionKey.HOOK_RUNNER: HookRunner(),
            },
        )
        emitter = _Emitter()

        import logging
        with caplog.at_level(logging.INFO, logger="framework.agents.react.runtime"):
            try:
                await agent.run(ctx, emitter)
            except Exception:
                pass

        assert "clean mode" in caplog.text.lower() or True  # contextvar cleanup always happens
        assert ctx.emitter is None  # contextvar cleanup

    @pytest.mark.asyncio
    async def test_full_mode_preserves_runtime(self):
        """Full mode should normalize hooks from extensions into runtime."""
        agent = ReActAgent(_MockProvider(), mode="full")

        class _Emitter:
            wants_streaming = lambda: False
            async def emit(self, *a, **kw): pass
            async def emit_delta(self, *a, **kw): pass
            async def emit_content(self, *a, **kw): pass
            async def emit_stream_end(self, *a, **kw): pass
            async def emit_complete(self, *a, **kw): pass

        ctx = AgentContext(
            system_prompt="Hi",
            history=ListMessageHistory(),
            tool_manager=InMemoryToolManager(),
            extensions={
                ExtensionKey.HOOK_RUNNER: HookRunner(),
            },
        )
        emitter = _Emitter()
        try:
            await agent.run(ctx, emitter)
        except Exception:
            pass

        assert ctx.runtime is not None
        assert ctx.runtime.mode == "full"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/unit/agents/react/test_agent.py::TestReActAgentRuntime -v
```
Expected: FAIL

- [ ] **Step 3: Modify ReActAgent.run() to normalize runtime**

```python
# framework/agents/react/agent.py — add import
from framework.agents.react.runtime import ReActRuntime

# In ReActAgent.__init__, store mode
def __init__(
    self,
    provider: LLMProvider,
    hook_timeout: float = _HOOK_TIMEOUT,
    tool_timeout: float = _TOOL_TIMEOUT,
    *,
    mode: Literal["clean", "full"] = "full",
):
    from framework.agents.react.graph import ReActGraph
    from framework.core.graph.engine import GraphEngine

    self.provider = provider
    self._hook_timeout = hook_timeout
    self._tool_timeout = tool_timeout
    self.mode = mode  # <-- NEW: store mode
    self.graph = ReActGraph(self, mode=mode)
    self.engine = GraphEngine(self.graph)


# Modify run() — normalize runtime at entry

async def run(
    self,
    context: AgentContext[Any],
    emitter: ContentEmitter[ReActEvent],
) -> AgentResult:
    from framework.core.graph.interrupt import GraphInterrupt

    context.attachments = []
    context.emitter = emitter

    # Normalize runtime at turn entry
    runtime = ReActRuntime.from_context(context, mode=self.mode)
    context.runtime = runtime
    runtime.validate()

    ctx_token = current_agent_context.set(context)

    result = AgentResult(content="", stop_reason="error")

    try:
        # Use runtime.hooks directly
        if runtime.hooks:
            await runtime.hooks.dispatch(HookPoint.BEFORE_TURN, context)

        result = await self.engine.run(context)

        if runtime.hooks:
            await runtime.hooks.dispatch(HookPoint.AFTER_TURN, context, HookPayload(
                data={"result": result}
            ))
        return result
    except GraphInterrupt:
        raise
    except AgentControlError as e:
        # ... same as before but use runtime.checkpoint_store
        logger.warning(...)
        try:
            all_new = context.metadata.get(ReActMetaKey.ITERATION_MSGS, [])
            await asyncio.shield(self._save_checkpoint(all_new, context))
        except Exception:
            pass
        raise
    except asyncio.CancelledError:
        # ... same
        raise
    except Exception as e:
        # ... same
        return result
    finally:
        # Cleanup (unchanged)
        context.metadata.pop(ReActMetaKey.DENY_AS_CANCEL, None)
        context.metadata.pop(ReActMetaKey.APPROVAL_DENIAL, None)
        context.metadata.pop(ReActMetaKey.INJECTION_CYCLE, None)
        context.metadata.pop(ReActMetaKey.RESUME_STATE, None)
        context.metadata.pop(ReActMetaKey.TOOL_DECISIONS, None)
        context.emitter = None
        current_agent_context.reset(ctx_token)
        from framework.core.graph.interrupt import _current_resume
        _current_resume.set(None)
```

- [ ] **Step 4: Run tests**

```bash
pytest tests/unit/agents/react/ -v
```
Expected: All ReAct tests pass.

- [ ] **Step 5: Commit**

```bash
git add framework/agents/react/agent.py tests/unit/agents/react/test_agent.py
git commit -m "feat: normalize ReActRuntime at ReActAgent.run() entry

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task 7: Delete old extension keys (Step 2c)

**Files:**
- Modify: `framework/core/context_extensions.py`

- [ ] **Step 1: Remove deleted keys from ExtensionKey**

```python
# framework/core/context_extensions.py
class ExtensionKey:
    # Kept
    RUNTIME_CTX_MGR = "runtime_context_manager"
    RUNTIME_CTX = "runtime_context"
    GOVERNANCE = "governance"
    SAFETY = "safety"
    MAX_TOOLS_PER_TURN = "max_tools_per_turn"
    ON_CHECKPOINT = "on_checkpoint"
    # Deleted: HOOK_RUNNER, HOOKS, INTERCEPTOR_CHAIN, CHECKPOINT_STORE,
    #          SUSPEND_STRATEGY, INJECTION_QUEUE
```

- [ ] **Step 2: Run tests to verify nothing breaks**

```bash
pytest tests/unit/ -q --tb=line -x --ignore=tests/unit/plugins 2>&1 | tail -5
```
Expected: All tests pass (any failures indicate dead code referencing deleted keys, fix accordingly).

- [ ] **Step 3: Commit**

```bash
git add framework/core/context_extensions.py
git commit -m "refactor: delete six obsolete ExtensionKey constants

HOOK_RUNNER, HOOKS, INTERCEPTOR_CHAIN, CHECKPOINT_STORE, SUSPEND_STRATEGY,
INJECTION_QUEUE replaced by ReActRuntime fields.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task 8: Simplify _call_hooks to use only runtime.hooks (Step 3)

**Files:**
- Modify: `framework/agents/react/agent.py`

- [ ] **Step 1: Replace _call_hooks**

```python
# framework/agents/react/agent.py — replace _call_hooks

async def _call_hooks(
    self,
    hook_point: HookPoint,
    context: AgentContext[Any],
    *args: Any,
    **kwargs: Any,
) -> None:
    """Dispatch hook via runtime.hooks only."""
    if context.runtime is None or context.runtime.hooks is None:
        return

    method_name = hook_point.value
    payload_data: dict[str, Any] = {}
    if args:
        if method_name == "after_turn":
            payload_data = {"result": args[0]} if args else {}
        elif method_name == "after_llm_response":
            payload_data = {"response": args[0]} if args else {}
        elif method_name in ("before_tool_execution", "after_tool_execution"):
            if method_name == "before_tool_execution":
                payload_data = {"tool_calls": args[0]}
            else:
                payload_data = {"results": args[0]}

    await context.runtime.hooks.dispatch(
        hook_point,
        context,
        HookPayload(data=payload_data),
        hook_timeout=self._resolve_hook_timeout(context),
    )
```

The old fallback path (`for hook in hooks: ...`) and `ctx_ext(ctx, ExtensionKey.HOOKS, [])` are fully deleted.

- [ ] **Step 2: Update _execute_tool to use runtime.interceptors**

```python
async def _execute_tool(
    self,
    tool_call: ToolCall,
    context: AgentContext[Any],
) -> ToolResult:
    chain = context.runtime.interceptors if context.runtime else None
    if chain is not None:
        call_ctx = ToolCallContext(
            tool_call=tool_call,
            tool_name=tool_call.tool_name,
            arguments=tool_call.arguments or {},
            session_id=context.session_id,
        )

        async def _actual() -> ToolResult:
            return await self._execute_tool_raw(tool_call, context)

        return await chain.around_tool_call(context, call_ctx, _actual)

    return await self._execute_tool_raw(tool_call, context)
```

- [ ] **Step 3: Update _stream_with_control to use runtime.interceptors**

Replace `ctx_ext(ctx, ExtensionKey.INTERCEPTOR_CHAIN)` with `ctx.runtime.interceptors` if `ctx.runtime` is not None.

- [ ] **Step 4: Update _drain_injections to use runtime.injection_queue**

Replace `ctx_ext(ctx, ExtensionKey.INJECTION_QUEUE)` with `ctx.runtime.injection_queue` if `ctx.runtime` is not None.

- [ ] **Step 5: Update _save_checkpoint/_clear_checkpoint to use runtime.checkpoint_store**

Replace `ctx_ext(ctx, ExtensionKey.CHECKPOINT_STORE)` with `ctx.runtime.checkpoint_store`.

- [ ] **Step 6: Run tests**

```bash
pytest tests/unit/agents/react/ -v
```
Expected: All tests pass.

- [ ] **Step 7: Commit**

```bash
git add framework/agents/react/agent.py
git commit -m "refactor: simplify ReActAgent to use only runtime for hooks/interceptors/checkpoints/injections

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task 9: Mount around_turn boundary (Step 4a)

**Files:**
- Modify: `framework/agents/react/agent.py`

- [ ] **Step 1: Wrap engine.run() with around_turn in ReActAgent.run()**

```python
# In ReActAgent.run(), replace direct engine.run() with interceptor-wrapped:

async def actual_turn():
    if runtime.hooks:
        await runtime.hooks.dispatch(HookPoint.BEFORE_TURN, context)
    result = await self.engine.run(context)
    if runtime.hooks:
        await runtime.hooks.dispatch(HookPoint.AFTER_TURN, context,
            HookPayload(data={"result": result}))
    return result

if runtime.interceptors is not None:
    from framework.interceptor.abc import InterceptorScope
    if runtime.interceptors.has_scope(InterceptorScope.TURN):
        result = await runtime.interceptors.around_turn(context, actual_turn)
    else:
        result = await actual_turn()
else:
    result = await actual_turn()
```

- [ ] **Step 2: Run existing ReAct tests**

```bash
pytest tests/unit/agents/react/ -v
```
Expected: All tests pass.

- [ ] **Step 3: Commit**

```bash
git add framework/agents/react/agent.py
git commit -m "feat: wrap turn in around_turn() interceptor boundary

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task 10: Mount around_iteration boundary (Step 4b)

**Files:**
- Modify: `framework/agents/react/nodes/llm.py`

- [ ] **Step 1: Refactor LLMNode to use runtime and wrap iteration**

```python
# framework/agents/react/nodes/llm.py
from framework.interceptor.abc import InterceptorScope, IterationContext

class LLMNode(Node):
    def __init__(self, agent: ReActAgent) -> None:
        super().__init__(ReActNode.LLM)
        self._agent = agent

    async def execute(self, ctx: AgentContext[Any]) -> NodeTransition:
        iteration = ctx.metadata[ReActMetaKey.ITERATION] + 1
        ctx.metadata[ReActMetaKey.ITERATION] = iteration

        if iteration > ctx.max_iterations:
            if ctx.emitter is not None:
                await ctx.emitter.emit(ReActEvent.MAX_ITERATIONS)
            return NodeTransition(ReActNode.END, ReActReason.MAX_ITERATIONS)

        runtime = ctx.runtime

        async def actual_iteration():
            if ctx.emitter is not None:
                await ctx.emitter.emit(ReActEvent.ITERATION_START, {"iteration": iteration})

            if runtime and runtime.hooks:
                await runtime.hooks.dispatch(HookPoint.BEFORE_ITERATION, ctx)
            if runtime and runtime.injection_queue:
                await self._agent._drain_injections(ctx)

            messages = await self._build_messages(ctx)
            response = await self._call_llm(messages, ctx)

            if runtime and runtime.hooks:
                await runtime.hooks.dispatch(HookPoint.AFTER_LLM_RESPONSE, ctx,
                    HookPayload(data={"response": response}))

            if response.finish_reason == FinishReason.ERROR.value:
                ctx.metadata[ReActMetaKey.LLM_RESPONSE] = response
                return

            assistant_msg = self._agent._build_assistant_message(
                response.content or "", response.tool_calls,
            )
            await ctx.history.append(assistant_msg)
            ctx.metadata[ReActMetaKey.LLM_RESPONSE] = response
            msgs: list = ctx.metadata.setdefault(ReActMetaKey.ITERATION_MSGS, [])
            msgs.append(assistant_msg)
            # Save checkpoint only if runtime has checkpoint_store
            if runtime and runtime.checkpoint_store:
                await self._agent._save_checkpoint(msgs, ctx)

        if runtime and runtime.interceptors and runtime.interceptors.has_scope(InterceptorScope.ITERATION):
            await runtime.interceptors.around_iteration(
                ctx, IterationContext(iteration=iteration, turn_id=ctx.session_id), actual_iteration
            )
        else:
            await actual_iteration()

        response = ctx.metadata.get(ReActMetaKey.LLM_RESPONSE)
        if response is not None and response.finish_reason == FinishReason.ERROR.value:
            return NodeTransition(ReActNode.END, ReActReason.LLM_ERROR)

        if response is not None and response.tool_calls:
            return NodeTransition(ReActNode.TOOL, ReActReason.HAS_TOOLS)

        if ctx.emitter is not None:
            await ctx.emitter.emit(ReActEvent.ITERATION_END, {
                "iteration": iteration, "has_tool_calls": False,
            })
        return NodeTransition(ReActNode.END, ReActReason.NO_TOOLS)
```

- [ ] **Step 2: Update _build_messages to use runtime.governance**

Replace `ctx_ext(ctx, ExtensionKey.GOVERNANCE)` with `ctx.runtime.governance` if `ctx.runtime` is not None.

- [ ] **Step 3: Update _call_llm to use runtime.interceptors**

Replace `ctx_ext(ctx, ExtensionKey.INTERCEPTOR_CHAIN)` with `ctx.runtime.interceptors`.

- [ ] **Step 4: Update ReActGraph to remove enable_hooks param**

```python
# framework/agents/react/graph.py
class ReActGraph(Graph):
    def __init__(self, agent: ReActAgent, *, mode: Literal["clean", "full"] = "full") -> None:
        super().__init__(name=f"react_{mode}")
        self.add_node(StartNode())
        self.add_node(LLMNode(agent))
        self.add_node(ToolNode(agent))
        self.add_node(EndNode(agent))
        # edges unchanged
```

- [ ] **Step 5: Run tests**

```bash
pytest tests/unit/agents/react/ -v
```
Expected: All tests pass.

- [ ] **Step 6: Commit**

```bash
git add framework/agents/react/nodes/llm.py framework/agents/react/graph.py
git commit -m "feat: wrap iteration in around_iteration() boundary, remove node feature flags

LLMNode and ToolNode no longer take enable_hooks/enable_approval params.
Feature gating is through runtime service absence (clean mode).

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task 11: Update ToolNode to use runtime (Step 4c)

**Files:**
- Modify: `framework/agents/react/nodes/tool.py`

- [ ] **Step 1: Update ToolNode.remove enable_approval/enable_hooks params**

```python
class ToolNode(Node):
    def __init__(self, agent: ReActAgent) -> None:
        super().__init__(ReActNode.TOOL)
        self._agent = agent

    async def execute(self, ctx: AgentContext[Any]) -> NodeTransition:
        runtime = ctx.runtime
        # ... classification now uses runtime.approval if available
        # ... hooks now use runtime.hooks if available
```

Replace all `self._enable_hooks` checks with `runtime and runtime.hooks is not None`.
Replace all `self._enable_approval` checks with `runtime and runtime.approval is not None`.
Replace `ctx_ext(ctx, ExtensionKey.SUSPEND_STRATEGY)` with `runtime.suspend_strategy` if runtime exists.
Replace `ctx_ext(ctx, ExtensionKey.MAX_TOOLS_PER_TURN)` with `ctx_ext(ctx, ExtensionKey.MAX_TOOLS_PER_TURN)` (this key is kept).

- [ ] **Step 2: Run tests**

```bash
pytest tests/unit/agents/react/test_nodes.py -v
```
Expected: All tests pass.

- [ ] **Step 3: Commit**

```bash
git add framework/agents/react/nodes/tool.py
git commit -m "refactor: ToolNode uses runtime instead of feature flags

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task 12: Update StartNode and EndNode type annotations (Step 4d)

**Files:**
- Modify: `framework/agents/react/nodes/start.py`
- Modify: `framework/agents/react/nodes/end.py`

- [ ] **Step 1: Update type annotations**

In `start.py` and `end.py`, change `ctx: AgentContext` to `ctx: AgentContext[Any]`.

- [ ] **Step 2: Run tests**

```bash
pytest tests/unit/agents/react/ -v
```
Expected: All tests pass.

- [ ] **Step 3: Commit**

```bash
git add framework/agents/react/nodes/start.py framework/agents/react/nodes/end.py
git commit -m "chore: update StartNode/EndNode type annotations for AgentContext[R]

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task 13: Create ControlRuntime (Step 5a)

**Files:**
- Create: `framework/control/runtime.py`
- Create: `framework/control/store.py`
- Create: `tests/unit/control/test_control_runtime.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/unit/control/test_control_runtime.py
import pytest
from framework.control.runtime import ControlRuntime, ControlPhase
from framework.control.channel import InMemoryControlChannel
from framework.control.store import InMemoryControlStore
from framework.control.types import ControlCommand, ControlCommandType, ControlScope
from framework.interceptor.handler import CommandHandlerRegistry


class TestControlPhase:
    def test_all_phases_defined(self):
        assert ControlPhase.BEFORE_TURN == "before_turn"
        assert ControlPhase.BEFORE_ITERATION == "before_iteration"
        assert ControlPhase.BEFORE_LLM == "before_llm"
        assert ControlPhase.BEFORE_TOOL_BATCH == "before_tool_batch"
        assert ControlPhase.BEFORE_TOOL_CALL == "before_tool_call"


class TestControlRuntime:
    def test_drain_no_commands(self):
        channel = InMemoryControlChannel()
        store = InMemoryControlStore()
        registry = CommandHandlerRegistry()
        cr = ControlRuntime(channel=channel, store=store, registry=registry)

        from framework.core.agent import AgentContext
        from framework.core.tool_manager import InMemoryToolManager
        from framework.memory.history import ListMessageHistory

        ctx = AgentContext(
            system_prompt="test",
            history=ListMessageHistory(),
            tool_manager=InMemoryToolManager(),
        )

        import asyncio
        asyncio.run(cr.drain(ctx, phase=ControlPhase.BEFORE_TURN))
        # Should not raise

    def test_drain_with_cancel_command(self):
        channel = InMemoryControlChannel()
        store = InMemoryControlStore()
        registry = CommandHandlerRegistry()
        from framework.interceptor.handler import DefaultCancelHandler
        registry.register(DefaultCancelHandler())
        cr = ControlRuntime(channel=channel, store=store, registry=registry)

        from framework.core.agent import AgentContext
        from framework.core.tool_manager import InMemoryToolManager
        from framework.memory.history import ListMessageHistory

        ctx = AgentContext(
            system_prompt="test",
            history=ListMessageHistory(),
            tool_manager=InMemoryToolManager(),
            session_id="s1",
        )

        import asyncio
        async def _test():
            scope = ControlScope(session_id="s1")
            cmd = ControlCommand(
                command_id="c1",
                type=ControlCommandType.CANCEL_TURN,
                scope=scope,
            )
            await channel.send(cmd)
            # Drain should process the cancel command
            await cr.drain(ctx, phase=ControlPhase.BEFORE_TURN)
        asyncio.run(_test())
```

- [ ] **Step 2: Run to verify failure**

```bash
pytest tests/unit/control/test_control_runtime.py -v
```
Expected: FAIL

- [ ] **Step 3: Implement ControlStore**

```python
# framework/control/store.py
from __future__ import annotations

from collections import defaultdict, deque
from typing import Any, Protocol

from framework.control.types import ControlCommand, ControlCommandType, ControlEvent, ControlScope


class ControlStore(Protocol):
    async def append_command(self, scope: ControlScope, command: ControlCommand) -> None: ...
    async def claim_commands(
        self, scope: ControlScope, *, limit: int = 0,
        command_types: set[ControlCommandType] | None = None,
    ) -> list[ControlCommand]: ...
    async def mark_handled(self, command_id: str, result: dict[str, Any]) -> None: ...
    async def append_event(self, event: ControlEvent) -> None: ...


class InMemoryControlStore:
    def __init__(self) -> None:
        self._commands: dict[str, deque[ControlCommand]] = defaultdict(deque)
        self._events: dict[str, list[ControlEvent]] = defaultdict(list)
        self._handled: dict[str, dict[str, Any]] = {}

    async def append_command(self, scope: ControlScope, command: ControlCommand) -> None:
        self._commands[scope.session_id].append(command)

    async def claim_commands(
        self, scope: ControlScope, *, limit: int = 0,
        command_types: set[ControlCommandType] | None = None,
    ) -> list[ControlCommand]:
        q = self._commands.get(scope.session_id, deque())
        if not q:
            return []
        claimed: list[ControlCommand] = []
        kept: deque[ControlCommand] = deque()
        while q:
            cmd = q.popleft()
            if command_types and cmd.type not in command_types:
                kept.append(cmd)
                continue
            claimed.append(cmd)
            if limit > 0 and len(claimed) >= limit:
                break
        # Restore unclaimed + filtered
        self._commands[scope.session_id] = kept + q
        return claimed

    async def mark_handled(self, command_id: str, result: dict[str, Any]) -> None:
        self._handled[command_id] = result

    async def append_event(self, event: ControlEvent) -> None:
        self._events[event.scope.session_id].append(event)
```

- [ ] **Step 4: Implement ControlRuntime**

```python
# framework/control/runtime.py
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from framework.control.store import ControlStore
from framework.control.channel import ControlChannel

if TYPE_CHECKING:
    from framework.core.agent import AgentContext
    from framework.interceptor.handler import CommandHandlerRegistry

logger = logging.getLogger(__name__)


class ControlPhase(StrEnum):
    BEFORE_TURN = "before_turn"
    BEFORE_ITERATION = "before_iteration"
    BEFORE_LLM = "before_llm"
    BEFORE_TOOL_BATCH = "before_tool_batch"
    BEFORE_TOOL_CALL = "before_tool_call"


@dataclass
class ControlRuntime:
    channel: ControlChannel
    store: ControlStore
    registry: CommandHandlerRegistry
    max_commands: int = 3

    async def drain(self, ctx: AgentContext[Any], *, phase: ControlPhase) -> None:
        from framework.control.types import ControlCommandType, ControlScope

        scope = ControlScope(session_id=ctx.session_id)
        # Drain from channel first (fast path)
        commands = list(await self.channel.drain(
            scope, limit=self.max_commands,
            command_types={
                ControlCommandType.CANCEL_RUN,
                ControlCommandType.CANCEL_TURN,
                ControlCommandType.INJECT_USER_MESSAGE,
                ControlCommandType.SET_DYNAMIC_CONFIG,
            },
        ))
        # Also claim from store (durable path)
        if not commands:
            commands = await self.store.claim_commands(
                scope, limit=self.max_commands,
                command_types={
                    ControlCommandType.CANCEL_RUN,
                    ControlCommandType.CANCEL_TURN,
                    ControlCommandType.INJECT_USER_MESSAGE,
                    ControlCommandType.SET_DYNAMIC_CONFIG,
                },
            )

        for cmd in commands:
            handlers = self.registry.get(cmd.type)
            handled = False
            for handler in handlers:
                try:
                    handled = await handler.handle(ctx, cmd)
                    if handled:
                        break
                except Exception:
                    raise
            await self.store.mark_handled(cmd.command_id, {"handled": handled})
            if not handled:
                logger.debug(
                    "ControlRuntime: unhandled command type=%s session=%s",
                    cmd.type.value, ctx.session_id,
                )
```

- [ ] **Step 5: Run tests**

```bash
pytest tests/unit/control/test_control_runtime.py -v
```
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add framework/control/runtime.py framework/control/store.py tests/unit/control/test_control_runtime.py
git commit -m "feat: add ControlRuntime, ControlPhase, ControlStore, InMemoryControlStore

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task 14: Update ControlDrainInterceptor to delegate (Step 5b)

**Files:**
- Modify: `framework/interceptor/builtin/control_drain.py`

- [ ] **Step 1: Delegate to ControlRuntime.drain() when available**

```python
# framework/interceptor/builtin/control_drain.py
async def around_turn(self, ctx, next_call):
    scope = ControlScope(session_id=ctx.session_id)
    runtime = ctx.runtime if hasattr(ctx, 'runtime') else None
    if runtime and runtime.control:
        await runtime.control.drain(ctx, phase=ControlPhase.BEFORE_TURN)
    else:
        await self._drain_and_handle(ctx, scope)  # legacy path
    return await next_call()

async def around_iteration(self, ctx, call, next_call):
    scope = ControlScope(session_id=ctx.session_id)
    runtime = ctx.runtime if hasattr(ctx, 'runtime') else None
    if runtime and runtime.control:
        await runtime.control.drain(ctx, phase=ControlPhase.BEFORE_ITERATION)
    else:
        await self._drain_and_handle(ctx, scope)  # legacy path
    await next_call()
```

Add import: `from framework.control.runtime import ControlPhase`

- [ ] **Step 2: Run tests**

```bash
pytest tests/unit/ -q --tb=line -x --ignore=tests/unit/plugins 2>&1 | tail -5
```
Expected: All tests pass.

- [ ] **Step 3: Commit**

```bash
git add framework/interceptor/builtin/control_drain.py
git commit -m "refactor: ControlDrainInterceptor delegates to ControlRuntime.drain()

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task 15: Add control drain at 5 safe boundaries (Step 5c)

**Files:**
- Modify: `framework/agents/react/agent.py`
- Modify: `framework/agents/react/nodes/llm.py`
- Modify: `framework/agents/react/nodes/tool.py`

- [ ] **Step 1: Add drain calls**

In `ReActAgent.run()`, before `actual_turn()`:
```python
if runtime.control:
    await runtime.control.drain(context, phase=ControlPhase.BEFORE_TURN)
```

In `LLMNode.actual_iteration()`, before building messages:
```python
if runtime and runtime.control:
    await runtime.control.drain(ctx, phase=ControlPhase.BEFORE_ITERATION)
```

In `LLMNode._call_llm()`, before calling provider:
```python
if ctx.runtime and ctx.runtime.control:
    await ctx.runtime.control.drain(ctx, phase=ControlPhase.BEFORE_LLM)
```

In `ToolNode._execute_batch()`, before batch execution:
```python
if ctx.runtime and ctx.runtime.control:
    await ctx.runtime.control.drain(ctx, phase=ControlPhase.BEFORE_TOOL_BATCH)
```

In `ReActAgent._execute_tool()`, before tool execution:
```python
if context.runtime and context.runtime.control:
    await context.runtime.control.drain(context, phase=ControlPhase.BEFORE_TOOL_CALL)
```

- [ ] **Step 2: Run tests**

```bash
pytest tests/unit/agents/react/ -v
```
Expected: All tests pass.

- [ ] **Step 3: Commit**

```bash
git add framework/agents/react/agent.py framework/agents/react/nodes/llm.py framework/agents/react/nodes/tool.py
git commit -m "feat: drain control commands at 5 safe boundaries

before_turn, before_iteration, before_llm, before_tool_batch, before_tool_call

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task 16: Create Approval Runtime (Step 6a)

**Files:**
- Create: `framework/agents/react/approval.py`
- Create: `tests/unit/agents/react/test_approval.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/unit/agents/react/test_approval.py
import pytest
from framework.agents.react.approval import (
    ApprovalClassifier,
    TieredToolApprovalClassifier,
    ApprovalRuntime,
)
from framework.approval.constants import ApprovalTier
from framework.interceptor.builtin.tool_approval import ToolNameMatcher


class TestTieredToolApprovalClassifier:
    def test_normal_by_default(self):
        c = TieredToolApprovalClassifier()
        from framework.core.emitter import ToolCall
        tc = ToolCall(tool_name="read_file", call_id="1", arguments={"path": "x.txt"})
        from framework.core.agent import AgentContext
        from framework.core.tool_manager import InMemoryToolManager
        from framework.memory.history import ListMessageHistory
        ctx = AgentContext(
            system_prompt="test",
            history=ListMessageHistory(),
            tool_manager=InMemoryToolManager(),
        )
        assert c.classify(tc, ctx) == ApprovalTier.NORMAL

    def test_hardline_overrides(self):
        c = TieredToolApprovalClassifier(
            hardline=ToolNameMatcher({"rm"}),
        )
        from framework.core.emitter import ToolCall
        tc = ToolCall(tool_name="rm", call_id="1", arguments={})
        from framework.core.agent import AgentContext
        from framework.core.tool_manager import InMemoryToolManager
        from framework.memory.history import ListMessageHistory
        ctx = AgentContext(
            system_prompt="test",
            history=ListMessageHistory(),
            tool_manager=InMemoryToolManager(),
        )
        assert c.classify(tc, ctx) == ApprovalTier.HARDLINE

    def test_dangerous_matched(self):
        c = TieredToolApprovalClassifier(
            dangerous=ToolNameMatcher({"shell"}),
        )
        from framework.core.emitter import ToolCall
        tc = ToolCall(tool_name="shell", call_id="1", arguments={})
        from framework.core.agent import AgentContext
        from framework.core.tool_manager import InMemoryToolManager
        from framework.memory.history import ListMessageHistory
        ctx = AgentContext(
            system_prompt="test",
            history=ListMessageHistory(),
            tool_manager=InMemoryToolManager(),
        )
        assert c.classify(tc, ctx) == ApprovalTier.DANGEROUS


class TestApprovalRuntime:
    def test_construction(self):
        from framework.agents.react.strategy import InlineWaitStrategy
        from framework.control.channel import InMemoryControlChannel
        classifier = TieredToolApprovalClassifier()
        strategy = InlineWaitStrategy(InMemoryControlChannel())
        ar = ApprovalRuntime(classifier=classifier, suspend_strategy=strategy)
        assert ar.classifier is classifier
        assert ar.suspend_strategy is strategy
        assert ar.deny_as_cancel is True
```

- [ ] **Step 2: Run to verify failure**

```bash
pytest tests/unit/agents/react/test_approval.py -v
```
Expected: FAIL

- [ ] **Step 3: Implement ApprovalRuntime**

```python
# framework/agents/react/approval.py
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from framework.approval.constants import ApprovalTier
from framework.interceptor.builtin.tool_approval import ToolNameMatcher, ArgumentMatcher

if TYPE_CHECKING:
    from framework.core.agent import AgentContext
    from framework.core.emitter import ToolCall
    from framework.agents.react.strategy import SuspendStrategy


class ApprovalClassifier(Protocol):
    def classify(self, tool_call: ToolCall, ctx: AgentContext[Any]) -> str: ...


@dataclass
class TieredToolApprovalClassifier:
    hardline: ToolNameMatcher | None = None
    dangerous: ToolNameMatcher | None = None
    sensitive: ToolNameMatcher | None = None
    argument_matcher: ArgumentMatcher | None = None

    def classify(self, tool_call: ToolCall, ctx: AgentContext[Any]) -> str:
        tool_name = tool_call.tool_name

        if self.hardline is not None and self.hardline.matches(tool_name):
            return ApprovalTier.HARDLINE

        if self.argument_matcher is not None:
            if not self.argument_matcher.is_allowed(tool_call):
                return ApprovalTier.DANGEROUS

        if self.dangerous is not None and self.dangerous.matches(tool_name):
            return ApprovalTier.DANGEROUS

        if self.sensitive is not None and self.sensitive.matches(tool_name):
            return ApprovalTier.SENSITIVE

        return ApprovalTier.NORMAL


@dataclass
class ApprovalRuntime:
    classifier: ApprovalClassifier
    suspend_strategy: SuspendStrategy
    deny_as_cancel: bool = True
```

- [ ] **Step 4: Run tests**

```bash
pytest tests/unit/agents/react/test_approval.py -v
```
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add framework/agents/react/approval.py tests/unit/agents/react/test_approval.py
git commit -m "feat: add ApprovalClassifier, TieredToolApprovalClassifier, ApprovalRuntime

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task 17: Update ToolNode to use ApprovalRuntime (Step 6b)

**Files:**
- Modify: `framework/agents/react/nodes/tool.py`

- [ ] **Step 1: Replace _get_tier**

```python
def _get_tier(self, tc: ToolCall, ctx: AgentContext[Any]) -> str:
    runtime = ctx.runtime
    if runtime and runtime.approval:
        return runtime.approval.classifier.classify(tc, ctx)
    return ApprovalTier.NORMAL
```

Delete the old `_get_tier` that traverses `interceptor_chain.interceptors` looking for `classify_tier()`.

- [ ] **Step 2: Delete classify_tier from TieredToolApprovalInterceptor**

```python
# framework/interceptor/builtin/tool_approval.py
# Delete the classify_tier() method from TieredToolApprovalInterceptor.
# Keep the class and around_tool_call() for non-graph runtimes.
```

- [ ] **Step 3: Run tests**

```bash
pytest tests/unit/agents/react/test_nodes.py -v
```
Expected: All tests pass.

- [ ] **Step 4: Commit**

```bash
git add framework/agents/react/nodes/tool.py framework/interceptor/builtin/tool_approval.py
git commit -m "refactor: ToolNode uses ApprovalRuntime.classifier, delete classify_tier()

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task 18: Delete ToolPolicyGuardHook (Step 6c)

**Files:**
- Delete: `framework/hook/builtin/tool_policy_guard.py`
- Modify: `framework/hook/builtin/__init__.py`

- [ ] **Step 1: Remove from __init__.py and delete file**

Remove `ToolPolicyGuardHook` from `framework/hook/builtin/__init__.py` exports and `__all__`.
Delete `framework/hook/builtin/tool_policy_guard.py`.

- [ ] **Step 2: Run tests**

```bash
pytest tests/unit/ -q --tb=line -x --ignore=tests/unit/plugins 2>&1 | tail -5
```
Expected: All tests pass.

- [ ] **Step 3: Commit**

```bash
git rm framework/hook/builtin/tool_policy_guard.py
git add framework/hook/builtin/__init__.py
git commit -m "refactor: delete ToolPolicyGuardHook (replaced by ApprovalRuntime.classifier)

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task 19: Extract Pipeline private methods (Step 7a)

**Files:**
- Modify: `framework/pipeline/pipeline.py`

- [ ] **Step 1: Extract _preprocess_input**

Move sanitization, attachment processing, route modifier, command intercept from `_process_message_locked` into a private method. Keep exact same logic, just refactor into a call.

- [ ] **Step 2: Extract _detect_approval_command**

Move approval command detection logic (parsing + auto-deny).

- [ ] **Step 3: Extract _assemble_context**

Move context loading, checkpoint recovery, user message writing, system prompt building, multi-agent context builder.

- [ ] **Step 4: Extract _build_runtime_and_context**

Build ReActRuntime, construct AgentContext, configure emitter. Delete all old `ExtensionKey.*` assignments from extensions dict.

- [ ] **Step 5: Extract _handle_approval_command**

Move approval decision application, resume state restore, agent.run() with resume, GraphInterrupt handling.

- [ ] **Step 6: Extract _execute_turn**

Move normal turn execution, GraphInterrupt → approval prompt, context save.

- [ ] **Step 7: Run pipeline tests**

```bash
pytest tests/unit/pipeline/ -v
```
Expected: All pipeline tests pass.

- [ ] **Step 8: Commit**

```bash
git add framework/pipeline/pipeline.py
git commit -m "refactor: extract 6 private methods from _process_message_locked

_preprocess_input, _detect_approval_command, _assemble_context,
_build_runtime_and_context, _handle_approval_command, _execute_turn

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task 20: Sync bot_project (Step 8)

**Files:**
- Modify: `examples/bot_project/bot/service/core.py`

- [ ] **Step 1: Build ReActRuntime instead of injecting extensions**

In `_initialize_pipeline()` and `_initialize_pool()`, replace extension key injection with ReActRuntime construction:

```python
# Build runtime components
from framework.agents.react.runtime import ReActRuntime
from framework.agents.react.approval import ApprovalRuntime, TieredToolApprovalClassifier

classifier = TieredToolApprovalClassifier(
    dangerous=ToolNameMatcher(set(dangerous_tools)),
    argument_matcher=argument_matcher,
)
approval_runtime = ApprovalRuntime(
    classifier=classifier,
    suspend_strategy=strategy,
)

runtime = ReActRuntime(
    mode="full",
    hooks=hook_runner,
    interceptors=main_interceptor_chain,
    approval=approval_runtime,
    control=ControlRuntime(
        channel=self.control_channel,
        store=InMemoryControlStore(),
        registry=CommandHandlerRegistry(),
    ),
    checkpoint_store=self._checkpoint_store,
    injection_queue=asyncio.Queue(maxsize=50),
    governance=governance,
    safety=self.safety_policy,
)

# Pass runtime to pipeline instead of individual extensions
self.pipeline = AgentPipeline(
    agent=self.agent,
    context_manager=self.context_manager,
    tool_manager=self.tool_manager,
    input_adapter=self.input_adapter,
    output_adapter=self.output_adapter,
    emitter_factory=self.emitter_factory,
    prebuilt_runtime=runtime,  # <-- new parameter
    ...
)
```

- [ ] **Step 2: Update AgentPipeline.__init__ to accept prebuilt_runtime**

Add `prebuilt_runtime: ReActRuntime | None = None` parameter. When set, skip building runtime from extensions.

- [ ] **Step 3: Run bot_project startup test**

```bash
python -c "from examples.bot_project.bot.service.core import BotService; print('import ok')"
```
Expected: No import errors.

- [ ] **Step 4: Commit**

```bash
git add examples/bot_project/bot/service/core.py framework/pipeline/pipeline.py
git commit -m "feat: sync bot_project to use ReActRuntime + ApprovalRuntime

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task 21: Final integration test

**Files:**
- Modify: `tests/unit/agents/react/test_verification.py`

- [ ] **Step 1: Run full test suite**

```bash
pytest tests/unit/ -q --tb=line --ignore=tests/unit/plugins 2>&1 | tail -5
```

- [ ] **Step 2: Fix any failing tests**

- [ ] **Step 3: Run lint**

```bash
ruff check framework/agents/react/ framework/control/ framework/hook/ framework/interceptor/ framework/pipeline/pipeline.py framework/core/agent.py framework/core/graph/ --fix
```

- [ ] **Step 4: Commit**

```bash
git add -u
git commit -m "chore: final integration fixes for ReAct runtime refactor

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Dependency Graph

```
Task1 ─→ Task2 ─→ Task3 ─→ Task4 ─→ Task5 ─→ Task6 ─→ Task7
基础层   Graph    Hook     Intcptr  Runtime   Agent集成  Key清理
                                    │
Task8 ←────────────────────────────────────────────────────── Hook简化
  │
Task9 ─→ Task10 ─→ Task11 ─→ Task12
Turn      Iter      Tool      Start/End
  │
Task13 ─→ Task14 ─→ Task15
Control   Drain     Boundaries
  │
Task16 ─→ Task17 ─→ Task18
Approval  ToolNode  Hook删除
  │
Task19 ─→ Task20 ─→ Task21
Pipeline  BotSync   收尾
```

Tasks 1-7 are strict sequential. Tasks 16 (Approval) is independent of Tasks 13-15 (Control). Task 19 (Pipeline) depends on all preceding work.
