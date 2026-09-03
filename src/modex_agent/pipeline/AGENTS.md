<!-- Parent: ../AGENTS.md -->
<!-- Updated: 2026-08-26 -->

# pipeline

## Purpose

End-to-end flow orchestration. `AgentPipeline` is now a **slimmed facade** (ADR-0025): it owns lifecycle (`run`/`stop`), pre-lock dispatch (`_process_message`: route/dedup/busy-mode/lock), session-query delegation, dream-task management, and the `control_channel`. The locked turn flow, turn preparation, approval resume, session bookkeeping, and background consolidation were extracted into five deep-module collaborators (all in this package), wired together by the factory (not the pipeline). The 5 mutable mirror setters are deleted; 11 backward-compat read-only delegation properties remain (see ADR-0025 D4 deviations).

## Key Files

| File | Description |
|------|-------------|
| `pipeline.py` | `AgentPipeline` slimmed facade (~347L, 13 params — ADR-0025 D4). Main loop: `run()` → `_process_message()` (route/dedup/busy-mode/lock) → delegates the locked turn to `TurnRunner.process_locked()`. Public turn entry: `process_message()`. Session queries: `is_session_active()`/`has_active_sessions()`/`get_active_turn_uuid()` (delegate to `TurnSessionRegistry`). Lifecycle: `run()`/`stop()`/`cleanup_session_resources()`. Owns the `_dream_task` + `DreamScanner`. 11 backward-compat read-only delegation properties expose turn_runner internals for code that reads them. The 5 mutable mirror setters are deleted (ADR-0025 D4). |
| `turn_runner_abc.py` | `TurnRunner` ABC (ADR-0025 D3) — the seam between `AgentPipeline` and concrete turn runners. 1 abstract method (`process_locked`) + 3 lifecycle methods with no-op defaults (`cleanup_session`, `load_pending_approval`, `bind_to_pipeline`) + 2 post-construction wiring methods (`set_pool_context`, `set_emitter_factory`) + 12 read-only properties with `None` defaults. `ReActTurnRunner` overrides all; `ExternalTurnRunner` overrides only `set_emitter_factory`. See ADR-0025 D3 deviations for why the surface is larger than the original "one method" spec. |
| `busy_input.py` | `BusyInputMode` (StrEnum: INTERRUPT/QUEUE/STEER) — how an agent handles a new message while busy (moved from `core/agent_runtime_config.py`, plan §15 B2). Dispatch lives in `pipeline._process_message`. |
| `turn_runner.py` | `ReActTurnRunner(TurnRunner)` — the deep cut that absorbs the entire `_process_message_locked` flow. `process_locked()` runs on_session_start → pool/context resolution → approval snapshot load → `TurnContextBuilder` composition → approval detect → snapshot-approval driver or normal turn execution. `_build_turn_descriptor()` reads graph metadata from `input_metadata`, resolves the live `GraphContext` via the builder's `graph_context_resolver`, and extracts per-node artifacts from `ctx.user_data["node_artifacts"]` — producing a `TurnContextDescriptor` that drives the configurator pipeline. `execute_turn()` runs one agent turn with `GraphInterrupt` handling + finally cleanup. `_handle_snapshot_approval()` is the thin approval-resume driver (apply decision → execute → delete snapshot → drain). Module-level `_safe_flush()` (memory flush with timeout). Composes `TurnContextBuilder` + `ApprovalResumer` + `TurnSessionRegistry`; **no back-reference to `AgentPipeline`** (pinned by `test_pipeline_modules_no_backref.py`). |
| `turn_context_builder.py` | `TurnContextBuilder` — pure turn-preparation: `build_turn_request()` (parse slash commands/approval actions → `TurnRequest` dataclass), `preprocess()` (sanitize/attachments/route modifier), `assemble()` (delegates to `context_assembler.assemble_context`), `build_runtime_and_context()` (typed `AgentContext` + emitter + `AgentRuntime`/`ReActTurnState`). Post-construction wiring: `graph_context_resolver` (closure → `GraphOrchestrator.get_graph_context`) and `config_pipeline` (`TurnContextConfigPipeline` with 6 graph configurators), both set by `pipeline_wiring.py` after pool assembly. `build_runtime_and_context()` accepts an optional `turn_descriptor: TurnContextDescriptor` — when non-None and `config_pipeline` is wired, the pipeline applies all matching configurators (binding, approval, max-turns, tool, topology, knowledge) onto `AgentContext` before returning. Holds the `TurnRequest` frozen dataclass. No back-reference to `AgentPipeline`. |
| `turn_context_config.py` | `TurnContextDescriptor` (frozen Pydantic — typed inputs for per-turn configuration) + `GraphTurnArtifacts` (frozen Pydantic — deliver_tool + topology + knowledge config) + `TurnContextConfigurator` ABC (sync `applies()`/`configure()`) + `TurnContextConfigPipeline` (ordered list, `configure(ctx, desc)`) + 6 concrete configurators: `GraphContextBindingConfigurator` (sets `graph_instance_id` + `graph_context`), `GraphApprovalConfigurator` (disables approval), `GraphMaxTurnsConfigurator` (caps at 3), `GraphToolConfigurator` (installs deliver + knowledge tools via `GraphToolPreset`), `GraphTopologyConfigurator` (publishes topology + description), `GraphKnowledgeConfigurator` (publishes knowledge dir + requirements). |
| `approval_resumer.py` | `ApprovalResumer` — pure approval state machine. `load_pending()` queries `TurnStateStore` for `SUSPENDED` turns; `apply_resume()` applies a decision, re-saves partial state, and restores `agent_context.runtime.state` on completion. **Single-direction dependency**: no knowledge of turn execution — the caller (`TurnRunner`) drives execute + delete_turn + drain. |
| `turn_session_registry.py` | `TurnSessionRegistry` — the 4 in-process bookkeeping dicts (session locks, running turn tasks, injection queues, turn UUIDs) plus queries and `cancel_turn()` (the active wakeup used by `/stop`/WebUI pause). Shared by `pipeline` and `TurnRunner`, removing any runner→pipeline back-ref. |
| `dream_scanner.py` | `DreamScanner` — `run_forever()` background loop that scans active contexts every `dream_interval` and triggers `DreamEngine` consolidation, scoped by per-scope `asyncio.Lock` from `runtime.dream_locks`. Pipeline owns the task lifecycle (`run()` creates it, `stop()` cancels + `DreamScanner.stop()`). |
| `adapters.py` | `InputAdapter` ABC only (B4: `OutputAdapter` family moved to `modex_agent/adapters/output.py`). `InputAdapter.configure_input_pipeline()` stores pipeline/ctx/output reference. `WebSocketInputAdapter` overrides with no-op. |
| `approval_renderer.py` | `ApprovalRenderer` — detects pending approval state, buffers agent messages during approval, applies unrelated-input auto-denial. Standalone `format_approval_prompt()`. Does NOT parse `/approve`/`/deny` (that's `parse_input_command` from `approval/response`). |
| `context_assembler.py` | `assemble_context()` — loads history, writes user message, builds system prompt, handles multimodal/attachment content (user message carries attachment `media://` ImageUrlParts alongside the text part when the model is IMAGE-capable; otherwise stays plain str), sideband prompts, runs `MultiAgentContextBuilder`. Called by `TurnContextBuilder.assemble()`. |
| `snapshot.py` | `PipelineSnapshot` — captures pipeline state for approval suspend/resume (used by `ApprovalResumer` / `TurnRunner._handle_snapshot_approval()`). |

## Flow

```
InputAdapter.receive()
  → pipeline._process_message()              [pre-lock dispatch]
    → router.route() (if configured)
    → dedup check
    → pre-lock slash command parse + dispatch_policy (BYPASS_QUEUE / DROP_IF_BUSY)
    → busy check (INTERRUPT / QUEUE / STEER)
       → QUEUE: slash commands dropped with busy notice
    → session lock (TurnSessionRegistry)
    → TurnRunner.process_locked()             [locked turn — deep cut]
      → on_session_start
      → resolve ctx_mgr + pool_data snapshot
      → ApprovalResumer.load_pending() → pending snapshot
      → TurnContextBuilder.build_turn_request() → TurnRequest
      → TurnContextBuilder.preprocess() — sanitize, attachments, route modifier
      → ApprovalRenderer.detect()
      → TurnContextBuilder.assemble() (→ context_assembler.assemble_context)
      → TurnContextBuilder.build_runtime_and_context() — AgentContext + emitter
      → if approval pending: TurnRunner._handle_snapshot_approval()
          → ApprovalResumer.apply_resume() → execute_turn → delete_turn → drain
      → else if trigger_agent: TurnRunner.execute_turn() → agent.run()
         → GraphInterrupt (approval) → render prompt, return None
         → AgentResult → save context, flush memory
      → cleanup_session (unregister turn, flush, on_session_end)
```

## Slash Commands

When `command_processor` is configured, the pipeline intercepts `/command` input before context assembly:

1. **Pre-lock** (`dispatch_policy`, in `pipeline._process_message`): Fast routing without session lock.
   - `BYPASS_QUEUE` / `DROP_IF_BUSY` → handled immediately
   - `APPROVAL_RESPONSE` / `NORMAL_QUEUE` → proceed to lock
2. **In-lock** (`handle`, inside `TurnContextBuilder.build_turn_request`): Full execution with `pending_approval`.
   - `NOTICE` → send to output adapter, return None
   - `APPROVAL_DECISION` → route to `TurnRunner._handle_snapshot_approval()`
   - `CONTINUE_AGENT` / `TRANSFORM_TO_USER_INPUT` → proceed to agent execution

See `modex_agent/commands/AGENTS.md` for full command subsystem documentation.

## Busy Input Modes

| Mode | Behavior | Slash Command Handling |
|------|----------|----------------------|
| **INTERRUPT** | Cancel current task, start new turn | Command parsed after cancel |
| **QUEUE** (default) | Push plain text to `injection_queue` | **Dropped with busy notice** |
| **STEER** | Send `INJECT_STEER` control command | Sent as steer payload |

Key invariant: slash commands must never bypass the command processor. In QUEUE mode, busy notice is sent instead of injecting raw text.

> Caveat: the `STEER` mode sends `INJECT_STEER` into the control channel, but nothing drains `INJECT_STEER` today — the command is written but never read. STEER is therefore effectively inert until a consumer is added. See `modex_agent/control/AGENTS.md`.

## Approval in Pipeline

- `ApprovalResumer.load_pending()` queries `TurnStateStore` for `SUSPENDED` turns (the pipeline's `_load_pending_approval_snapshot()` delegates to it).
- `ApprovalRenderer.detect()` checks pending snapshot, buffers agent messages, auto-denies on unrelated input.
- Partial approval: `ApprovalResumer.apply_resume()` re-saves the snapshot and re-prompts.
- Complete approval: `TurnRunner._handle_snapshot_approval()` calls `apply_resume()` → `execute_turn()` resumes → `delete_turn()` + `ApprovalRenderer.drain()`.

## Key Invariant

The pipeline facade assembles runtime services and handles platform I/O + pre-lock dispatch; `TurnRunner` owns the locked turn loop; `TurnContextBuilder`/`ApprovalResumer` are pure helpers with no execution coupling; ReAct owns the agent turn, LLM calls, tool execution, approval state, and resume boundaries. No deep module holds a code-level back-reference to `AgentPipeline` (DEC-9 guard A).

## For AI Agents

- `AgentPipeline` is a thin facade — for turn-execution logic read `turn_runner.py`; for turn-preparation read `turn_context_builder.py`; for approval state read `approval_resumer.py`.
- The facade delegates the locked turn to `TurnRunner.process_locked()`; it never executes agent turns itself.
- Snapshots capture the full agent state (including ReAct loop position) — suspension and resumption is transparent to the agent.
- Busy-input modes and slash-command dispatch live in `pipeline._process_message` (pre-lock); the in-lock command handling is in `TurnContextBuilder.build_turn_request`.
- `snapshot.py` is used exclusively for approval suspend/resume, not for general checkpointing.

## Dependencies

- `modex_agent.core.agent` — `Agent[E]` for execution
- `modex_agent.agents.react` — `ReActAgent`, `ReActTurnState` for turn execution
- `modex_agent.runtime` — `AgentRuntimeServices`, `TurnStateStore` for state and snapshot persistence
- `modex_agent.commands` — `CommandProcessor` for slash command parsing
- `modex_agent.approval` — approval response parsing and tier classification
- `modex_agent.multi_agent` — `MultiAgentContextBuilder` for multi-agent context assembly
- `modex_agent.memory` — memory compaction and consolidation after turns
- `modex_agent.control` — `InMemoryControlChannel` (the live `/stop` + pause mechanism), `AgentControlError`, control types
