# Pipeline Decomposition Design

Date: 2026-05-03

## Current State

After Phase 4.4 (commit `a173bca`), `_process_message_locked` was split into 6 private methods:

```
_process_message_locked()
├── _preprocess_input()           # sanitize + attachments + route + command
├── _detect_approval_command()    # parse approval actions
├── _assemble_context()           # load + recover + system prompt + builder
├── _build_runtime_and_context()  # ReActRuntime → AgentContext + emitter
├── _handle_approval_command()    # apply decision + resume + GraphInterrupt
└── _execute_turn()               # agent.run + GraphInterrupt → prompt + save
```

This was "private methods first" — the seams are now visible but still live inside `AgentPipeline`.

## Remaining Problems

**1. Pipeline still owns approval orchestration.**

`_handle_approval_command` (100 lines) manages: approval store lookup, decision application, resume state recovery, agent.run() with `_current_resume`, GraphInterrupt re-render, and result save. These are approval-specific concerns, not pipeline I/O.

**2. Turn execution has no stable abstraction.**

`_execute_turn` handles: conversation_id tracking, agent.run(), GraphInterrupt → approval prompt rendering, result save, and finally-block cleanup (flush + checkpoint). Each concern has a different lifecycle owner.

**3. Session-scoped state is scattered across dicts.**

`_approval_stores`, `_resume_stores`, `_session_locks`, `_session_tasks`, `_injection_queues`, `_approval_pending` — six dicts on `AgentPipeline`, all keyed by `session_id`, no unified lifecycle.

**4. `AgentPipeline.__init__` takes 35+ parameters.**

Many of them (`hooks`, `command_interceptor`, `router`, `deduplicator`, `context_builder`, `sanitizer`, `on_session_start/end`, `governance`, `safety`, `checkpoint_store`, `approval_workspace`, `user_interface`, `prebuilt_runtime`) are passed through to `_build_runtime_and_context` or `_process_message_locked`. The pipeline is a config bag, not a composable pipeline.

## Design Direction

**Goal:** Pipeline should be **a thin I/O loop** that assembles 3-4 focused components and delegates turn execution to them. It should not own approval logic, session state management, or runtime construction.

### Target Shape

```
AgentPipeline (thin I/O loop)
├── SessionManager        # per-session state: locks, tasks, queues, stores, cleanup
├── InputPipeline         # normalizer: sanitize → attachment → route → command intercept
├── ContextAssembler      # load + recover + history + system prompt + builder
├── TurnOrchestrator      # runtime assembly → agent.run → interrupt → result → save
│   ├── ApprovalCoordinator  # approval action parsing, decision application, resume/render
│   └── TurnResultHandler    # save result, flush memory, clear checkpoint, session-end hook
└── OutputAdapter         # already exists
```

### Component Responsibilities

| Component | Responsibility | Expected size |
|-----------|---------------|---------------|
| `SessionManager` | Create/reuse per-session locks, queues, stores. Cleanup on session end. | ~60 lines |
| `InputPipeline` | Sanitize, process attachments, apply route modifier, intercept commands. Returns normalized input or command response. | ~80 lines |
| `ContextAssembler` | Load context, recover checkpoint, write user message, build system prompt, apply multi-agent builder. Returns assembled context state. | ~150 lines |
| `TurnOrchestrator` | Build ReActRuntime → AgentContext → agent.run() → route by outcome. The main "run one turn" entry. | ~50 lines |
| `ApprovalCoordinator` | Detect approval actions, apply decisions, manage resume state, handle GraphInterrupt re-render. Owns approval store/lifecycle. | ~120 lines |
| `TurnResultHandler` | Save agent result, inject attachments, flush memory, clear checkpoint on clean completion, run session-end hook. | ~60 lines |

### What Pipeline Does After Decomposition

```python
class AgentPipeline:
    def __init__(self, agent, input_adapter, output_adapter, emitter_factory, ...):
        self.session = SessionManager()
        self.input_pipe = InputPipeline(sanitizer, command_interceptor, output_adapter)
        self.assembler = ContextAssembler(context_manager, builder, skill_manager)
        self.turn = TurnOrchestrator(agent, self.session, ...)
        self.approval = ApprovalCoordinator(self.session, user_interface, output_adapter)
        # I/O adapters
        self.input_adapter = input_adapter
        self.output_adapter = output_adapter
        self.emitter_factory = emitter_factory

    async def run(self):
        async for msg in self.input_adapter.receive():
            session_id = msg.session_id
            async with self.session.lock(session_id):
                result = await self._process_message(msg, session_id)
                # emit/send handled internally
```

### Migration Path

**Step A — Stabilize seams (no API change)**
- Move `_preprocess_input`, `_detect_approval_command`, `_assemble_context`, `_build_runtime_and_context`, `_handle_approval_command`, `_execute_turn` from `AgentPipeline` to standalone functions in `framework/pipeline/steps/`. Keep exact same logic. Pipeline calls them as module-level functions.

**Step B — Extract SessionManager**
- Move `_session_locks`, `_session_tasks`, `_injection_queues`, `_approval_stores`, `_resume_stores`, `_approval_pending` into `SessionManager` class. Pipeline holds one instance.

**Step C — Extract ApprovalCoordinator**
- Move `_handle_approval_command` + `_detect_approval_command` into `ApprovalCoordinator`. Pipeline delegates approval routing to it.

**Step D — Extract TurnOrchestrator**
- Move `_build_runtime_and_context` + `_execute_turn` into `TurnOrchestrator`. Pipeline calls `await self.turn.run(...)`.

**Step E — Collapse AgentPipeline.__init__**
- After extraction, `__init__` parameters drop from 35+ to ~15. Remaining params are the essential I/O adapters + assembled components.

### Non-Goals

- **Not** extracting to public API classes immediately. Step A keeps everything as private module functions.
- **Not** changing the `_process_message_locked` control flow. Same sequence, same semantics.
- **Not** touching the pool-mode path (`_initialize_pool`) separately — it shares most components.

### Design Principles

1. **Pipeline owns I/O, not logic.** The only thing Pipeline should do directly is `input_adapter.receive()` and delegation.

2. **Each component has one source of truth for its state.** SessionManager owns session-scoped dicts. ApprovalCoordinator owns approval store. No component pokes into another's internal state.

3. **Components are independent and testable.** Each can be unit-tested with injected dependencies, no Pipeline instance needed.

4. **Decomposition follows existing seams.** The 6 private methods from Phase 4.4 already define the boundaries. This design just moves them across file boundaries.
