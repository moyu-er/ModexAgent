# ReAct Hook / Interceptor / Control Integration Design

Date: 2026-05-02

## Goals

This design completes the integration between:

- `framework.agents.react`
- hooks
- interceptors
- runtime control commands
- approval suspend/resume
- clean/full runtime modes

The desired shape is:

- `clean` mode is genuinely clean: no hooks, no approval, no control/interceptor chain, no suspend/resume strategy, no runtime store, no injection queue. The agent should sanitize the runtime context once at turn entry, log one clear line, then execute the plain ReAct graph without scattered feature checks.
- `full` mode is extensible: hooks, interceptors, control, approval, runtime state, and injection are wired through explicit runtime services.
- Lifecycle ownership is clear. Turn, iteration, LLM, and tool boundaries are owned by the ReAct runtime, not by pipeline glue.
- Approval classification is not hidden inside a generic interceptor implementation. Approval should be an explicit runtime service that the Tool node can query.
- Control commands are drained at real execution boundaries: before turn, before each iteration, before LLM call/stream, and before each tool batch/tool call.
- Pipeline should assemble runtime services, not embed approval/control/recovery behavior inline.

## Current Problems

### 1. `clean` mode is only partially clean

`ReActGraph(mode="clean")` disables hooks and approval inside `LLMNode` and `ToolNode`, but `ReActAgent.run()` still calls:

- `BEFORE_TURN`
- `AFTER_TURN`
- checkpoint paths
- cancellation/control cleanup paths
- resume context cleanup

`LLMNode._call_llm()` can still read `INTERCEPTOR_CHAIN` from context if present. `ToolNode` can still see extensions that should not exist in clean mode.

This creates a misleading mode boundary. The runtime says clean, but the context can still carry full-mode services.

### 2. Interceptor scopes exist but are not consistently owned

`InterceptorChain` supports:

- `TURN`
- `ITERATION`
- `LLM_STREAM`
- `TOOL_CALL`

Only tool calls are reliably wrapped. LLM stream is conditionally wrapped. Turn and iteration scopes exist, and `ControlDrainInterceptor` / `TurnTimeoutInterceptor` implement them, but ReAct does not consistently call `around_turn()` or `around_iteration()`.

This means control and timeout interceptors may be configured but inert.

### 3. Approval has two execution models

`TieredToolApprovalInterceptor` has:

- `classify_tier()` used by `ToolNode`
- `around_tool_call()` that can request approval itself

The active approval flow in graph ReAct is really:

`ToolNode -> classify tier -> SuspendResumeStrategy -> GraphInterrupt -> Pipeline -> UI -> resume`

The `around_tool_call()` approval behavior is a second model. Keeping both active makes behavior difficult to reason about.

### 4. Pipeline does too much

`AgentPipeline._process_message_locked()` currently handles:

- input normalization
- context recovery
- approval store setup
- approval command parsing
- AgentContext construction
- suspend strategy injection
- GraphInterrupt handling
- resume execution
- output finalization
- checkpoint cleanup

The logic works but mixes unrelated concerns. It should assemble services and delegate turn execution to focused runtime components.

### 5. Hooks and interceptors overlap

Some hooks perform policy or runtime behavior that belongs elsewhere:

- tool policy guard overlaps with approval/classification
- progress hooks overlap with event/control progress
- runtime context hooks are mandatory for peer communication but are easy to inject into the wrong dispatch path

Hooks should remain side-effect-light lifecycle observers or content transformers. Interceptors should wrap execution boundaries. Control should inject external commands. Approval should be its own policy/coordinator.

## Design Principles

1. Runtime mode is decided once.

   `ReActAgent.run()` sanitizes or validates the runtime extensions at the start of the turn. Nodes should not be responsible for repeatedly checking whether a mode is clean.

2. Execution boundaries are explicit.

   ReAct owns:

   - turn boundary
   - iteration boundary
   - LLM call/stream boundary
   - tool batch boundary
   - individual tool call boundary

3. Hooks do not control flow.

   Hooks can observe, append lightweight context, transform final content, or emit progress. They should not be the primary mechanism for cancellation, approval, or hard policy enforcement.

4. Interceptors wrap calls.

   Interceptors are suitable for timeout, result truncation, command drain, monitoring, and low-level wrappers around LLM/tool calls.

5. Approval is explicit.

   Approval classification and suspend/resume should be modeled as `ApprovalRuntime`, not as a side channel on `InterceptorChain`.

6. Pipeline assembles runtime, ReAct executes runtime.

   Pipeline should not know internal ReAct node behavior. It should pass services and handle externally visible interruptions.

7. Graph nodes are durable workflow steps, not the whole control plane.

   Nodes should represent resumable macro phases and explicit routing decisions. They are not sufficient by themselves for live intervention while an LLM stream or long-running tool is already executing.

8. Control is a first-class side channel.

   Runtime control is not just another hook. It is the user/system command plane for the currently running turn. It needs command transport, command persistence, command routing, and cooperative cancellation/intervention handles inside active operations.

## Should Hook / Interceptor / Control Be Implemented as Graph Nodes?

Use graph nodes for durable workflow state. Do not use graph nodes as the only abstraction for hook/interceptor/control.

The current graph engine executes one node at a time and routes by `NodeTransition.reason`. That is a good fit for ReAct's coarse workflow:

- start
- LLM
- tool
- approval suspend/resume
- finalization
- error/cancel end

It is not enough for live control by itself because a node can spend a long time inside:

- streaming LLM output
- waiting for provider response
- executing a tool
- waiting for approval
- running a batch of tools

If control exists only as a node between `LLM` and `Tool`, it can only act after the current node returns. That misses the important cases: stop LLM output now, cancel a running tool now, detach a tool and continue the turn, or inject a steering message before the next token/tool call.

Therefore the design uses a layered model:

1. Graph nodes: durable macro steps and explicit routing.
2. Runtime control: command plane and active-operation handles.
3. Interceptors: wrappers around execution boundaries.
4. Hooks: lifecycle observation and lightweight transformation.

### Node Responsibilities

Nodes should be used for:

- `StartNode`: initialize or resume a graph state.
- `LLMNode`: execute one model step and route based on LLM output.
- `ToolNode`: execute or suspend tool batch.
- `ControlGateNode` optional: drain and apply commands at safe boundaries.
- `ApprovalNode` optional later: model human approval as a durable step.
- `AsyncToolJoinNode` optional later: collect detached tool completions.
- `EndNode`: convert terminal state into `AgentResult`.

Nodes should not be used for:

- per-token cancellation by themselves
- killing or detaching an already running tool by themselves
- raw hook dispatch
- generic policy enforcement that belongs to approval/runtime services

### Recommended Graph Shape

Immediate graph shape should stay close to the current ReAct topology:

```text
start -> llm -> tool -> llm -> end
          |      |
          |      +-> end(cancelled)
          +--------> end(no_tools/error/max_iterations)
```

Add control gates only where they create real value:

```text
start -> control_gate -> llm -> control_gate -> tool -> control_gate -> llm
```

This should not be the first implementation unless the engine can avoid graph noise. The simpler first step is to call `runtime.control.drain()` at the same boundaries inside `ReActAgent`, `LLMNode`, and `ToolNode`.

Once the graph engine supports better state snapshots and command-style routing, those drains can be promoted into explicit hidden nodes.

### Graph Engine Capabilities to Add Later

The current engine can remain simple for now, but the design should leave room for:

- typed graph state separate from `AgentContext.metadata`
- node output as state update plus routing command
- dynamic `goto` target from a node result
- persisted current node / next node / pending interrupts
- task identity for long-running node work
- stream events for node start/end/checkpoint/interruption
- resumable interrupts with stable interrupt ids

This would let a future graph runtime model approval, human input, async tools, and resume more cleanly without pushing all state into metadata.

## Proposed Runtime Model

Add a small runtime service object. This can initially live in `framework/agents/react/runtime.py`.

```python
@dataclass
class ReActRuntime:
    mode: Literal["clean", "full"]
    hooks: HookRunner | None = None
    interceptors: InterceptorChain | None = None
    approval: ApprovalRuntime | None = None
    control: ControlRuntime | None = None
    checkpoint_store: RuntimeStateStore | None = None
    control_store: ControlStore | None = None
    suspend_strategy: SuspendStrategy | None = None
    injection_queue: asyncio.Queue[str] | None = None
    governance: ContextGovernance | None = None
    safety: RuntimeSafetyPolicy | None = None
```

`AgentContext.extensions` can continue to carry these during migration, but ReAct should normalize them into `ReActRuntime` once:

```python
runtime = ReActRuntime.from_context(ctx, mode=self.mode)
ctx.metadata[ReActMetaKey.RUNTIME] = runtime
```

Longer term, `AgentContext` can gain a typed `runtime` field. That is not required for the first implementation.

## Clean Mode Contract

`clean` mode means:

- `HOOKS = []`
- `HOOK_RUNNER = None`
- `INTERCEPTOR_CHAIN = None`
- `SUSPEND_STRATEGY = None`
- `CHECKPOINT_STORE = None`
- `INJECTION_QUEUE = None`
- approval runtime disabled
- control runtime disabled
- no `GraphInterrupt` for approval
- no approval/resume state
- no hook dispatch
- no interceptor dispatch

At `ReActAgent.run()` entry:

```python
if self.mode == "clean":
    disabled = sanitize_clean_runtime(context)
    if disabled:
        logger.info(
            "ReActAgent clean mode: disabled runtime extensions: %s",
            ", ".join(disabled),
        )
```

The sanitizer should mutate `context.extensions` once, clearing full-mode services. It should also clear approval-related metadata:

- `RESUME_STATE`
- `TOOL_DECISIONS`
- `DENY_AS_CANCEL`
- `APPROVAL_DENIAL`
- `INJECTION_CYCLE`

The key point: nodes should not be littered with `if clean` branches. Their dependencies should simply be absent.

## Full Mode Contract

`full` mode may enable:

- hook runner
- interceptor chain
- approval runtime
- suspend/resume
- checkpoint/runtime store
- injection queue
- governance
- control drain
- timeout wrappers

`full` mode should validate incompatible combinations:

- approval runtime without suspend strategy: allowed only if configured for non-suspending approval; otherwise error at setup time.
- `ControlDrainInterceptor` configured but no control channel: configuration error.
- `TurnTimeoutInterceptor` configured but ReAct does not wrap turn: configuration error during tests; implementation should prevent this.
- clean mode plus any full extension: sanitized and logged, not an error.

## Lifecycle Ownership

### Turn Boundary

`ReActAgent.run()` owns the turn boundary.

Proposed flow:

```python
async def run(ctx, emitter):
    prepare_context(ctx, emitter)
    runtime = build_or_sanitize_runtime(ctx)

    async def actual_turn():
        if runtime.hooks:
            await runtime.hooks.dispatch(BEFORE_TURN, ctx)
        result = await engine.run(ctx)
        if runtime.hooks:
            await runtime.hooks.dispatch(AFTER_TURN, ctx, result=result)
        return result

    if runtime.interceptors:
        return await runtime.interceptors.around_turn(ctx, actual_turn)
    return await actual_turn()
```

This makes `TurnTimeoutInterceptor` and `ControlDrainInterceptor(TURN)` real.

### Iteration Boundary

`LLMNode.execute()` currently increments iteration and owns most iteration-start behavior. It should wrap the full iteration unit:

```python
async def execute(ctx):
    iteration = increment_iteration(ctx)

    async def actual_iteration():
        emit ITERATION_START
        dispatch BEFORE_ITERATION
        drain injections
        build messages
        call LLM
        dispatch AFTER_LLM_RESPONSE
        append assistant message
        route to tool/end

    if runtime.interceptors:
        await runtime.interceptors.around_iteration(ctx, IterationContext(...), actual_iteration)
    else:
        await actual_iteration()
```

`around_iteration()` should wrap the entire LLM-side iteration, not just a placeholder. This makes `ControlDrainInterceptor(ITERATION)` useful.

### LLM Boundary

There should be two LLM scopes eventually:

- `LLM_CALL` for non-streaming
- `LLM_STREAM` for streaming

Current implementation only has stream wrapping. The first implementation can keep that, but design should reserve `LLM_CALL` so non-streaming control/timeout/telemetry is not second-class.

Recommended first step:

- keep `_stream_with_control()` for streaming
- add `around_llm_call()` later, not in the first patch
- drain control before non-streaming call through iteration boundary for now

Future LLM live control should wrap stream execution like this:

```python
async with runtime.control.active.register(llm_stream_operation):
    async for chunk in provider_stream:
        await runtime.control.poll_active(operation_id)
        if operation.cancel_requested:
            break
        yield chunk
```

The first implementation should not require this. It only needs to preserve the operation-id and registry extension points.

### Tool Boundary

`ToolNode` owns tool batch execution. Individual tool execution remains wrapped through `InterceptorChain.around_tool_call()`.

Tool batch should have an explicit pre-execution control drain in full mode:

- before batch starts
- before each tool call if control chain has iteration/turn gaps

This can be achieved either through a new `TOOL_BATCH` scope or by making `ToolNode` call a `ControlRuntime.drain()` method before executing the batch. Avoid overloading `before_tool_execution` hook for cancellation.

Future tool live control should execute tools through a task wrapper:

```python
task = asyncio.create_task(actual_tool_call())
async with runtime.control.active.register(tool_operation(task)):
    result = await wait_with_control(task, operation_id)
```

The first implementation should not attempt to safely kill arbitrary tool code. Cancellation should be cooperative where possible. Detach/async completion should be a later feature with explicit `DetachedOperationStore`.

## Approval Runtime

Introduce:

```python
class ApprovalClassifier(Protocol):
    def classify(self, tool_call: ToolCall, ctx: AgentContext) -> ApprovalTier: ...

@dataclass
class ApprovalRuntime:
    classifier: ApprovalClassifier
    suspend_strategy: SuspendStrategy
    deny_as_cancel: bool = True
```

`TieredToolApprovalInterceptor.classify_tier()` should be moved or wrapped into a classifier implementation:

```python
class TieredToolApprovalClassifier:
    def classify(self, tool_call, ctx) -> ApprovalTier:
        ...
```

During migration, `ToolNode` may support both:

1. preferred: `runtime.approval.classifier`
2. fallback: find `classify_tier()` on interceptors

But the fallback should be marked transitional in code comments and tests should target the preferred path.

The `around_tool_call()` approval behavior should not be used in graph ReAct full mode. It can remain for simple non-graph runtimes, but bot_project should not depend on it.

## Control Runtime

Control is the runtime command plane. It is how users or system components send commands into a running turn. It must support both safe-boundary commands and, later, live intervention while an operation is active.

Control has four parts:

- command transport: `ControlChannel`
- command persistence: `ControlStore`
- command handling: `CommandHandlerRegistry`
- active operation handles: cancellation/detach/interrupt handles for LLM and tool work

Create:

```python
@dataclass
class ControlRuntime:
    channel: ControlChannel
    store: ControlStore
    registry: CommandHandlerRegistry
    max_commands: int = 3
    active: ActiveOperationRegistry

    async def drain(self, ctx: AgentContext, *, phase: ControlPhase) -> None:
        ...
```

`ControlDrainInterceptor` can become a thin adapter over `ControlRuntime.drain()`:

```python
async def around_turn(ctx, next_call):
    await runtime.control.drain(ctx, phase=BEFORE_TURN)
    return await next_call()
```

This keeps the interceptor API useful while centralizing control semantics. Command handlers remain reusable.

Minimum command phases:

- `before_turn`
- `before_iteration`
- `before_llm`
- `before_tool_batch`
- `before_tool_call`

For the first implementation, `before_turn` and `before_iteration` are enough to make existing interceptors real. Tool batch drain can follow.

### Control Store

Control commands should not be only in an in-memory queue. The long-term control plane needs persistence because approval, cancellation, async tool completion, and resume may span process boundaries.

Design:

```python
class ControlStore(Protocol):
    async def append_command(self, scope: ControlScope, command: ControlCommand) -> None: ...
    async def claim_commands(
        self,
        scope: ControlScope,
        *,
        limit: int,
        command_types: set[ControlCommandType] | None = None,
    ) -> list[ControlCommand]: ...
    async def mark_handled(self, command_id: str, result: ControlCommandResult) -> None: ...
    async def append_event(self, event: ControlEvent) -> None: ...
```

`ControlChannel` can remain the fast in-process transport. `ControlStore` is the durable backing layer. A channel implementation can write-through to the store, or the runtime can read from both.

### Active Operation Registry

Live intervention requires runtime handles, not just queued commands.

```python
@dataclass
class ActiveOperation:
    operation_id: str
    phase: ControlPhase
    kind: Literal["llm_stream", "llm_call", "tool_call", "tool_batch"]
    cancel: Callable[[], Awaitable[None]]
    detach: Callable[[], Awaitable[DetachedOperationRef]] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

class ActiveOperationRegistry:
    def register(self, op: ActiveOperation) -> AsyncContextManager[None]: ...
    def get(self, operation_id: str) -> ActiveOperation | None: ...
```

This makes future commands possible:

- stop current LLM stream
- replace next LLM input with an injected user/system instruction
- cancel current tool call
- detach current tool call and continue without waiting
- receive detached tool result later and notify user

### What Is In Scope Now

Current implementation should only make control effective at safe boundaries:

- before turn
- before iteration
- before LLM starts
- before tool batch starts
- before each tool call starts

This is enough to make cancellation and injection reliable without rewriting provider/tool execution.

### What Is Deferred

Live in-operation intervention is intentionally deferred:

- stopping an LLM stream mid-token generation
- interrupting a provider call that does not expose cooperative cancellation
- killing arbitrary tool execution
- detaching a running tool and later joining/notifying
- changing graph route while a node is still executing

The design preserves space for these features through `ActiveOperationRegistry`, operation ids, durable control events, and future graph task snapshots.

## Hook Runtime

Hooks should be dispatched through only one path: `HookRunner`.

Current code supports both `HOOK_RUNNER` and raw `HOOKS`. That is acceptable as compatibility, but runtime assembly should normalize:

```python
if hook_runner is None and hooks:
    hook_runner = HookRunner([...])
```

After normalization, ReAct should use only `runtime.hooks`.

Hook ownership:

- `BEFORE_TURN`: ReActAgent
- `AFTER_TURN`: ReActAgent
- `BEFORE_ITERATION`: LLMNode iteration wrapper
- `AFTER_ITERATION`: ToolNode when tools complete, LLMNode when no tools
- `AFTER_LLM_RESPONSE`: LLMNode
- `BEFORE_TOOL_EXECUTION`: ToolNode before batch
- `AFTER_TOOL_EXECUTION`: ToolNode after batch
- `FINALIZE_CONTENT`: EndNode before emitting final output

Hooks should not:

- perform approval
- perform cancellation
- own timeout behavior
- mutate core graph routing except through a documented `HookResult` path

## Pipeline Runtime Assembly

Pipeline should build a runtime service bundle before `AgentContext`.

Proposed split:

```python
runtime = self._runtime_builder.build(
    session_id=session_id,
    mode=self.agent.mode,
    hooks=self.hooks,
    hook_runner=self.hook_runner,
    interceptor_chain=self.interceptor_chain,
    checkpoint_store=self.checkpoint_store,
    control_channel=self.control_channel,
    approval_workspace=self._approval_workspace,
    user_interface=self._user_interface,
)
```

Then:

```python
agent_context = AgentContext(..., extensions={
    ExtensionKey.REACT_RUNTIME: runtime,
})
```

During migration, keep existing extension keys too:

- `HOOK_RUNNER`
- `INTERCEPTOR_CHAIN`
- `CHECKPOINT_STORE`
- `SUSPEND_STRATEGY`

But the ReAct runtime should prefer `REACT_RUNTIME` when present.

## Pipeline Decomposition

The design should split `_process_message_locked()` into focused helpers. This can be incremental.

Recommended components:

1. `InputPreprocessor`
   - sanitizer
   - attachment processing
   - command interception
   - user/agent message normalization

2. `ContextAssembler`
   - load context
   - recover message checkpoint
   - append current user message
   - restore multimodal content
   - build system prompt
   - apply multi-agent context builder

3. `RuntimeBuilder`
   - hook runner normalization
   - control runtime
   - approval runtime
   - runtime state store
   - interceptor chain
   - clean/full mode service selection

4. `ApprovalCoordinator`
   - detect approval commands
   - load approval/resume state
   - render approval prompts
   - delete consumed approval state

5. `TurnRunner`
   - build `AgentContext`
   - call `agent.run()`
   - handle `GraphInterrupt`
   - return `AgentResult | None`

6. `OutputFinalizer`
   - save assistant result
   - inject attachments metadata
   - flush context
   - clear checkpoint on clean completion
   - run session end hooks

This can be implemented with private methods first, then promoted to classes only if the seams remain stable.

## ReAct Graph Changes

`ReActGraph(mode)` should remain a small graph topology builder, but node constructors should not carry many feature booleans.

Current:

```python
LLMNode(agent, enable_hooks=enable)
ToolNode(agent, enable_approval=enable, enable_hooks=enable)
```

Preferred:

```python
LLMNode(agent)
ToolNode(agent)
```

Nodes ask `runtime = react_runtime(ctx)` and use missing services as no-ops.

For clean mode, services are absent because context was sanitized. For full mode, services are present.

This removes duplicate checks and prevents clean/full behavior from diverging across nodes.

## Node-Based Control Extension Path

The first implementation should keep control drains inside runtime boundaries. Later, once graph state and command routing are stronger, add explicit control nodes.

### `ControlGateNode`

Purpose:

- drain pending control commands at a safe boundary
- apply state updates
- route to normal next node, cancel end, or injected LLM step

Input state:

- current phase
- current iteration
- pending command ids
- latest user/system injection, if any

Output:

- `NodeTransition(next, "continue")`
- `NodeTransition(end, "turn_cancelled")`
- `NodeTransition(llm, "injected_message")`

Use cases:

- cancel before LLM starts
- inject message before next LLM iteration
- update runtime config before tool batch

### `ApprovalNode`

Purpose:

- make human approval a durable graph step instead of a side effect in `ToolNode`

Potential future shape:

```text
tool_classify -> approval -> tool_execute
                 |          |
                 +-> end(cancelled)
```

Current system already approximates this through `GraphInterrupt` and `SuspendResumeStrategy`. It is acceptable to keep that for now.

### `AsyncToolNode` and `AsyncToolJoinNode`

Purpose:

- start a long-running tool and return immediately
- persist detached operation ref
- later receive completion event and notify or resume

This is deferred. It requires `DetachedOperationStore`, output notification routing, and clearer semantics for whether the original turn continues or waits.

## Migration Plan

### Phase 4.1: Runtime Normalization

Add:

- `framework/agents/react/runtime.py`
- `ReActRuntime`
- `react_runtime(ctx)`
- `sanitize_clean_runtime(ctx)`

Change `ReActAgent`:

- store `self.mode`
- normalize runtime at run entry
- in clean mode, clear full-mode extension keys and log once
- use `runtime.hooks` in `_call_hooks`

Tests:

- clean mode clears full-mode extensions
- clean mode logs one info message
- full mode preserves extensions
- clean mode does not dispatch hooks passed in context

### Phase 4.2: Real Turn / Iteration Interceptor Boundaries

Change:

- `ReActAgent.run()` wraps engine execution in `around_turn()`
- `LLMNode.execute()` wraps iteration body in `around_iteration()`

Tests:

- `TurnTimeoutInterceptor` actually times out a blocked turn
- `ControlDrainInterceptor` cancels before a turn
- `ControlDrainInterceptor` cancels before an iteration
- clean mode ignores configured interceptor chain

### Phase 4.2b: Safe-Boundary Control Runtime

Add:

- `ControlRuntime`
- `ControlStore` protocol
- `ControlPhase`
- `ActiveOperationRegistry` placeholder API

Change:

- `ControlDrainInterceptor` delegates to `ControlRuntime.drain()` when runtime exists.
- `ReActAgent` / `LLMNode` / `ToolNode` drain control at safe boundaries.
- No live LLM/tool interruption yet.

Tests:

- queued cancel command cancels before LLM starts
- queued injection command is appended before next LLM iteration
- command is marked handled in the store
- clean mode ignores control runtime and logs disabled extension

### Phase 4.3: Approval Runtime Extraction

Add:

- `ApprovalClassifier`
- `TieredToolApprovalClassifier`
- `ApprovalRuntime`

Change:

- `ToolNode._get_tier()` prefers `runtime.approval.classifier`
- keep interceptor `classify_tier()` fallback temporarily
- bot_project builds classifier directly, not by adding approval interceptor only for classification

Tests:

- normal tool executes without approval
- dangerous tool suspends through `SuspendResumeStrategy`
- hardline tool returns denied/preempted without invoking tool
- fallback interceptor classifier still works during migration

### Phase 4.4: Pipeline Helpers

Extract private methods first:

- `_build_react_runtime(...)`
- `_consume_approval_command(...)`
- `_resume_approved_turn(...)`
- `_handle_graph_interrupt(...)`

Do not introduce large public classes until behavior is stable.

Tests:

- approval command path does not append a new user message as normal chat
- resume path restores `TurnResumeState`
- new approval request during resume renders prompts again
- non-approval turn path unchanged

### Phase 4.5: Hook Cleanup

Normalize hook use:

- all hooks go through `HookRunner`
- raw `HOOKS` become input to runner construction
- `RuntimeContextHook` is injected into runner when required

Tests:

- `PeerAutoSendHook` sees runtime context when hook runner is present
- hook dispatch order is stable
- `FINALIZE_CONTENT` runs in `EndNode`

### Phase 4.6: Future Live Intervention

This phase is intentionally deferred until the safe-boundary runtime is stable.

Add:

- operation ids for LLM stream/call and tool calls
- active operation registry implementation
- cooperative stream stop
- cooperative tool cancellation
- detached tool execution and completion notifications

Tests:

- stop LLM stream mid-generation
- cancel cooperative long-running tool
- detach long-running tool and continue turn
- detached tool completion emits notification
- restart can recover detached operation metadata

## Compatibility Rules

During migration:

- Existing extension keys remain valid.
- `CheckpointStore` names remain valid, but new code should use `RuntimeStateStore`.
- Existing `TieredToolApprovalInterceptor.classify_tier()` remains as fallback.
- `ToolApprovalInterceptor.around_tool_call()` remains available for simple runtimes but is not used by graph ReAct full mode.
- `ReActGraph(mode="clean")` and `ReActGraph(mode="full")` constructors remain source-compatible.

## Non-Goals

This design does not:

- rewrite the graph engine
- remove pipeline checkpoint recovery
- remove hook APIs
- remove interceptor APIs
- remove control command types
- force all pipeline helper splits into public classes immediately
- implement live mid-stream/mid-tool intervention in the first patch
- make all hook/interceptor/control behavior graph nodes immediately

## Acceptance Criteria

1. `ReActAgent(mode="clean")` with a context containing hooks, interceptors, checkpoint store, suspend strategy, and injection queue:
   - logs that full-mode extensions were disabled
   - executes without calling hooks
   - executes tools without approval classification
   - does not write runtime checkpoint store

2. `ReActAgent(mode="full")`:
   - dispatches hooks through `HookRunner`
   - wraps turn and iteration interceptors
   - wraps tool calls
   - wraps LLM stream when streaming and stream interceptors exist
   - uses approval runtime for tool classification

3. `ControlDrainInterceptor`:
   - can cancel before a turn
   - can cancel before an iteration
   - does not require pipeline-specific logic to be effective

4. Safe-boundary control:
   - can persist and claim commands
   - can mark commands handled
   - can inject a message before the next LLM step
   - exposes operation-id extension points for future live intervention

5. Approval flow:
   - dangerous tools suspend
   - approval prompts are rendered by pipeline/user interface
   - approved resume continues with original tool calls and decisions
   - denied cancel ends with `stop_reason="turn_cancelled"`

6. Pipeline:
   - no longer owns internal ReAct node decisions
   - assembles runtime services
   - handles externally visible approval interrupts
   - saves and clears context/checkpoints exactly once per turn outcome
