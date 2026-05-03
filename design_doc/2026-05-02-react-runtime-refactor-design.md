# ReAct Hook / Interceptor / Control / Approval Integration Design

Date: 2026-05-02

## 1. Goals

Complete the integration between `framework.agents.react`, hooks, interceptors, runtime control commands, approval suspend/resume, and clean/full runtime modes.

Core principles:
- `clean` mode: no hooks, no approval, no control, no interceptor chain, no suspend/resume, no runtime store, no injection queue. Sanitize once at turn entry, log one clear line.
- `full` mode: hooks, interceptors, control, approval, runtime state, and injection wired through explicit runtime services.
- Pipeline assembles runtime services; ReAct executes them.
- Hooks = lightweight lifecycle observers; Interceptors = execution boundary wrappers; Control = command plane; Approval = policy + classifier.

## 2. Key Architectural Decisions

| Decision | Choice |
|----------|--------|
| Scope | Phase 4.1–4.5 full implementation |
| Testing | TDD — write tests before implementation code |
| Pipeline decomposition | Private methods first; promote to classes when stable |
| Backward compatibility | Aggressive cleanup — delete old paths, no fallback shims |
| Runtime injection | `AgentContext[R]` generic |
| Generic scope | Full-chain: `Node[R]`, `Graph[N,R]`, `Hook[R]`, `Interceptor[R]` |
| Implementation strategy | Approach A: Phased with `R = Any` default |

## 3. Generic Architecture

### 3.1 AgentContext[R]

```python
from typing import Generic, TypeVar

R = TypeVar("R", default=Any)

class AgentContext(Generic[R]):
    system_prompt: str
    history: MessageHistory
    tool_manager: ToolManager | None
    session_id: str
    max_iterations: int
    temperature: float | None = None
    max_tokens: int | None = None
    metadata: dict[str, Any]
    extensions: dict[str, Any]       # downgraded: transient non-runtime data only
    attachments: list[Any]
    emitter: ContentEmitter | None
    runtime: R                        # typed runtime field; default Any, ReActRuntime in ReAct
```

### 3.2 Generic Propagation Chain

```
Agent[E]                                                        # existing
AgentContext[R]                                                 # new
Node[R]            →  execute(ctx: AgentContext[R])
Graph[N, R]        →  add_node(Node[R])
GraphEngine[R]     →  run(ctx: AgentContext[R])
Hook[R]            →  before_turn(ctx: AgentContext[R]) etc.
Interceptor[R]      →  around_tool_call(ctx: AgentContext[R]) etc.
```

`R = Any` by default. Non-ReAct consumers use `AgentContext` (same as `AgentContext[Any]`) with zero changes.

## 4. ReActRuntime

### 4.1 Definition

```python
# framework/agents/react/runtime.py
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
    def from_context(cls, ctx: AgentContext, *, mode: str) -> ReActRuntime: ...
    @classmethod
    def clean(cls) -> ReActRuntime: ...
```

### 4.2 Clean Mode Contract

At `ReActAgent.run()` entry, sanitize once:

```python
if runtime.mode == "clean":
    logger.info("ReActAgent clean mode: all runtime extensions disabled")
```

All full-mode services are `None`. Sanitizer removes old extension keys:
`HOOK_RUNNER`, `HOOKS`, `INTERCEPTOR_CHAIN`, `CHECKPOINT_STORE`, `SUSPEND_STRATEGY`, `INJECTION_QUEUE`

And old metadata keys: `RESUME_STATE`, `TOOL_DECISIONS`, `DENY_AS_CANCEL`, `APPROVAL_DENIAL`, `INJECTION_CYCLE`

Nodes should not have `if clean` branches — their dependencies are simply absent.

### 4.3 Full Mode Validation

- `approval` present but `suspend_strategy` None when suspension is needed → `ConfigurationError`
- `interceptors` contain `ControlDrainInterceptor` but `control` is None → `ConfigurationError`
- Clean mode + any full-mode service → sanitize and log (not an error)

### 4.4 ExtensionKey Cleanup

Deleted keys: `HOOK_RUNNER`, `HOOKS`, `INTERCEPTOR_CHAIN`, `CHECKPOINT_STORE`, `SUSPEND_STRATEGY`, `INJECTION_QUEUE`

`safety` and `governance` are consumed by `from_context()` and placed on `ReActRuntime`.

## 5. Lifecycle Boundaries

### 5.1 Turn Boundary

`ReActAgent.run()` wraps engine execution in `around_turn()`:

```python
async def run(self, context, emitter):
    runtime = normalize_runtime(context)
    async def actual_turn():
        if runtime.hooks:
            await runtime.hooks.dispatch(BEFORE_TURN, context)
        result = await self.engine.run(context)
        if runtime.hooks:
            await runtime.hooks.dispatch(AFTER_TURN, context, result=result)
        return result

    if runtime.interceptors:
        return await runtime.interceptors.around_turn(context, actual_turn)
    return await actual_turn()
```

This makes `TurnTimeoutInterceptor` and `ControlDrainInterceptor(TURN)` functional.

### 5.2 Iteration Boundary

`LLMNode.execute()` wraps the full iteration body in `around_iteration()`:

```python
async def execute(self, ctx):
    iteration = increment_iteration(ctx)
    async def actual_iteration():
        # drain control, emit start, dispatch hooks, drain injections,
        # build messages, call LLM, route to tool/end
        ...
    if ctx.runtime.interceptors:
        await ctx.runtime.interceptors.around_iteration(ctx, IterationContext(iteration), actual_iteration)
    else:
        await actual_iteration()
```

### 5.3 LLM Boundary

No structural changes in Phase 4.1-4.5. `LLMNode._call_llm()` accesses chain via `runtime.interceptors`.

### 5.4 Tool Boundary

`around_tool_call()` kept as-is. Add pre-execution control drain in `ToolNode._execute_batch()`.

### 5.5 Summary of Changes

| Boundary | Before | After | New Capability |
|----------|--------|-------|----------------|
| TURN | No interceptor | `around_turn()` | Timeout/Cancel turn |
| ITERATION | No interceptor | `around_iteration()` | Cancel before iteration |
| LLM_STREAM | Conditional | `runtime.interceptors` | No change |
| TOOL_CALL | Interceptor | `runtime.interceptors` | No change |
| TOOL_BATCH | None | `runtime.control.drain()` | Cancel before batch |

## 6. Control Runtime

### 6.1 Core Model

```python
# framework/control/runtime.py (new file)
@dataclass
class ControlRuntime:
    channel: ControlChannel
    store: ControlStore
    registry: CommandHandlerRegistry
    max_commands: int = 3
    active: ActiveOperationRegistry | None = None  # placeholder for Phase 4.6

    async def drain(self, ctx: AgentContext, *, phase: ControlPhase) -> None: ...

class ControlPhase(str, Enum):
    BEFORE_TURN = "before_turn"
    BEFORE_ITERATION = "before_iteration"
    BEFORE_LLM = "before_llm"
    BEFORE_TOOL_BATCH = "before_tool_batch"
    BEFORE_TOOL_CALL = "before_tool_call"
```

### 6.2 ControlStore Protocol

```python
class ControlStore(Protocol):
    async def append_command(self, scope: ControlScope, command: ControlCommand) -> None: ...
    async def claim_commands(self, scope, *, limit, command_types=None) -> list[ControlCommand]: ...
    async def mark_handled(self, command_id: str, result: dict[str, Any]) -> None: ...
    async def append_event(self, event: ControlEvent) -> None: ...
```

First implementation: `InMemoryControlStore`.

### 6.3 ControlDrainInterceptor → Thin Adapter

Delegates to `ControlRuntime.drain()` when runtime exists.

### 6.4 Drain Call Sites

| Phase | Call Site |
|-------|-----------|
| `before_turn` | `ReActAgent.run()` — before `around_turn()` |
| `before_iteration` | `LLMNode.execute()` — `actual_iteration()` entry |
| `before_llm` | `LLMNode._call_llm()` — before provider call |
| `before_tool_batch` | `ToolNode._execute_batch()` — before batch |
| `before_tool_call` | `ReActAgent._execute_tool()` — before single tool |

### 6.5 Deferred

Live in-operation intervention (stop LLM stream mid-token, cancel running tool, detach tool) is deferred to Phase 4.6. `ActiveOperationRegistry` is a placeholder API only.

## 7. Approval Runtime

### 7.1 ApprovalClassifier Protocol

```python
# framework/agents/react/approval.py (new file)
class ApprovalClassifier(Protocol):
    def classify(self, tool_call: ToolCall, ctx: AgentContext[ReActRuntime]) -> str: ...
```

### 7.2 TieredToolApprovalClassifier

Extracted from `TieredToolApprovalInterceptor.classify_tier()`. Pure classifier — no approval interaction.

```python
@dataclass
class TieredToolApprovalClassifier:
    hardline: ToolNameMatcher | None = None
    dangerous: ToolNameMatcher | None = None
    sensitive: ToolNameMatcher | None = None
    argument_matcher: ArgumentMatcher | None = None

    def classify(self, tool_call: ToolCall, ctx: AgentContext[ReActRuntime]) -> str: ...
```

### 7.3 ApprovalRuntime

```python
@dataclass
class ApprovalRuntime:
    classifier: ApprovalClassifier
    suspend_strategy: SuspendStrategy
    deny_as_cancel: bool = True
```

### 7.4 ToolNode Changes

`_get_tier()` reads from `runtime.approval.classifier` directly. No traversing interceptor chain for `classify_tier()`.

### 7.5 TieredToolApprovalInterceptor

- `classify_tier()` method deleted
- `around_tool_call()` kept for non-graph runtimes only
- bot_project no longer adds it to `InterceptorChain` for classification

## 8. Hook Normalization

### 8.1 Single Dispatch Path

`ReActRuntime.from_context()` normalizes raw hooks into `HookRunner`:

```python
if hook_runner is None and hooks:
    hook_runner = HookRunner([HookSpec(hook=h, on_error=LOG) for h in hooks])
```

`_call_hooks` simplified to use only `runtime.hooks`.

### 8.2 Hook Cleanup

- `ToolPolicyGuardHook` deleted (classification is `ApprovalRuntime`'s job)
- `RuntimeContextHook` kept via `HookRunner`
- `PeerAutoSendHook` kept
- All hooks dispatched through single `HookRunner` path

## 9. Pipeline Decomposition

### 9.1 Private Methods

Extract 6 private methods from `_process_message_locked()`:

| Method | Responsibility |
|--------|---------------|
| `_preprocess_input()` | Sanitize + attachments + route modifier + command intercept |
| `_detect_approval_command()` | Parse approval actions; auto-deny unrelated messages |
| `_assemble_context()` | Load context + recover + write user message + system prompt + multi-agent builder |
| `_build_runtime_and_context()` | Build ReActRuntime → AgentContext[ReActRuntime] + emitter |
| `_handle_approval_command()` | Apply decisions + resume state + agent.run + GraphInterrupt |
| `_execute_turn()` | Normal turn: agent.run + GraphInterrupt → approval prompt + save |

### 9.2 Pipeline No Longer Owns

- SuspendResumeStrategy construction (owned by ApprovalRuntime)
- `ExtensionKey.SUSPEND_STRATEGY` injection
- `_approval_pending` / `_approval_stores` dict management
- Old extension key injection (`HOOKS`, `HOOK_RUNNER`, `INTERCEPTOR_CHAIN`, `CHECKPOINT_STORE`, `INJECTION_QUEUE`)

Pipeline only: assembles ReActRuntime → builds AgentContext → calls agent.run() → handles GraphInterrupt.

## 10. Implementation Order

```
Step1 ──→ Step2 ──→ Step3 ──→ Step4 ──→ Step5 ──→ Step6 ──→ Step7 ──→ Step8
基础层   Runtime   Hook归一化 边界挂载  Control    Approval   Pipeline   Bot同步
```

### Step 1: Generic Foundation (Phase 4.1a)
- `AgentContext[R]` with `R = Any` default
- `Node[R]`, `Graph[N,R]`, `GraphEngine[R]`, `Hook[R]`, `Interceptor[R]` signatures
- Tests: backward compat for non-ReAct consumers

### Step 2: ReActRuntime (Phase 4.1b)
- `ReActRuntime` dataclass + `from_context()` + `clean()` factory
- `sanitize_clean_runtime()` — clear old extension keys + metadata
- `ReActAgent.run()` normalizes runtime at entry
- Tests: clean mode clears full-mode extensions; logs one line; full mode preserves; incompatible combo raises ConfigurationError

### Step 3: Hook Normalization (Phase 4.5)
- `_call_hooks` simplified to use only `runtime.hooks`
- Delete `ctx_ext(HOOKS)` / `ctx_ext(HOOK_RUNNER)` paths
- Delete `ToolPolicyGuardHook` (pending)
- Tests: PeerAutoSendHook dispatched via runtime.hooks; order stable

### Step 4: Turn/Iteration Boundaries (Phase 4.2)
- `ReActAgent.run()` wraps in `around_turn()`
- `LLMNode.execute()` wraps in `around_iteration()`
- `LLMNode` drops `enable_hooks` parameter
- Tests: TurnTimeoutInterceptor blocks turn; ControlDrainInterceptor cancels before turn/iteration; clean mode ignores interceptor chain

### Step 5: ControlRuntime (Phase 4.2b)
- `ControlRuntime` + `ControlStore` protocol + `InMemoryControlStore`
- `ControlDrainInterceptor` delegates to `ControlRuntime.drain()`
- Drain at 5 safe boundaries
- `ActiveOperationRegistry` placeholder
- Tests: queued cancel before LLM; inject message before iteration; command marked handled; clean mode control is None

### Step 6: ApprovalRuntime (Phase 4.3)
- `ApprovalClassifier` protocol + `TieredToolApprovalClassifier`
- `ApprovalRuntime` dataclass
- `ToolNode._get_tier()` → `runtime.approval.classifier`
- Delete `classify_tier()` from `TieredToolApprovalInterceptor`
- Delete `ExtensionKey.SUSPEND_STRATEGY`
- Tests: normal tool executes; dangerous suspends; hardline preempted; denied cancel terminates turn

### Step 7: Pipeline Decomposition (Phase 4.4)
- Extract 6 private methods
- Delete inline approval/strategy construction
- Delete old extension key assignments
- Tests: approval command skips user message save; resume restores state; new approval during resume re-renders; non-approval turn unchanged

### Step 8: bot_project Sync
- Build `ReActRuntime` instead of injecting old extension keys
- Build `ApprovalRuntime` + `TieredToolApprovalClassifier`
- Clean mode config support
- Tests: pipeline mode starts; pool mode starts

## 11. Migration Compatibility

- All old extension keys deleted — no fallback
- `TieredToolApprovalInterceptor.classify_tier()` deleted
- bot_project updated simultaneously
- Other consumers (if any) must migrate to `ReActRuntime` API

## 12. Non-Goals

- Rewriting the graph engine
- Live mid-stream/mid-tool intervention (Phase 4.6)
- Making all hook/interceptor/control behavior graph nodes
- Forcing pipeline helper splits into public classes
