# Phase 3-4 Design Structure Map

> **SUPERSEDED**: This map references the old `_LightGraphContext` design which has been replaced by `AgentContext.graph_instance_id` (a first-class `int | None` field). The authoritative design is in `issues/14-phase4-turn-context-config-pipeline.md` §6.3. This file is retained for historical context only — do not update it; update T14 instead.

## Data items

1. **TurnContextDescriptor** (T14 §1) — Pydantic frozen model: agent_kind, execution_strategy, graph_context, graph_node_name, graph_instance_id, is_node_execution, graph_artifacts. Created by `_build_turn_descriptor`, consumed by configurators.
2. **GraphTurnArtifacts** (T14 §2) — Pydantic frozen: deliver_tool, topology_section, node_description, knowledge_config, knowledge_dir. Created by BotAgentNode.execute, stored on ModexGraphContext._node_artifacts.
3. **graph_instance_id** (int) — propagated via envelope.metadata["graph_instance_id"]. Origin: GraphContext.graph_instance_id (set at GraphContext construction). Transport: envelope.metadata. Consumer: _build_turn_descriptor reads from input_metadata.
4. **AgentMessageEnvelope** (existing) — mutable dataclass. metadata dict mutated post-construction at Site 1 (SendStrategy.execute).
5. **ModexGraphContext** (T13 D1) — GraphContext subclass. _node_artifacts: dict[str, GraphTurnArtifacts]. Created in GraphOrchestrator.run_instance, stored in _active_contexts.
6. **_LightGraphContext** (T14 §6.3) — GraphContext subclass, only graph_instance_id. Created in ExternalTurnRunner.process_locked / facade.send.
7. **AgentContext.graph_context** (existing field L101) — set by GraphContextBindingConfigurator. Default None.
8. **_active_contexts** (T13) — dict[int, GraphContext] on GraphOrchestrator. Stores ModexGraphContext per gid.
9. **track_consume parameter** (T13 D2) — bool, keyword-only on tree.deliver. True creates MessageTrack(EXTERNAL_INPUT, DISPATCHED).
10. **MessageTrack** (existing) — DISPATCHED→CONSUMED/CANCELLED. Created by track_consume=True, closed by on_consumed or on_dispatch_end.
11. **IntegratedInput/IntegratedPayload** (existing) — framework hands to execute. Phase 3 formats into envelope content.
12. **_pending_delivers** (existing) — Node instance attribute. Written by Node.deliver(), read by _collect_delivers.
13. **error_feedback** (T15-3 DELETED) — no longer injected. Node.run executes once.

## States

1. **GraphInstanceStatus**: PENDING→RUNNING→COMPLETED/FAILED/CRASHED/PAUSED/STOPPED. FAILED = reached_end=False (dead-end). COMPLETED = reached_end=True (dispatch to END).
2. **InvocationStatus** (node-level): RUNNING→COMPLETED/CANCELED/CRASHED. No FAILED at node level. CANCELED = GraphBubbleUp/UndeliveredError(deleted). CRASHED = unhandled Exception.
3. **MessageTrackStatus**: DISPATCHED→CONSUMED (on_consumed) / CANCELLED (on_dispatch_end fallback or tree recovery).
4. **TreeStatus**: ACTIVE→COMPLETED/CANCELLED. COMPLETED = quiesce. CANCELLED = recover_tree/on_session_evicted.
5. **reached_end** (bool): False (default) → True (only at _dispatch_utils.py:71 when target==END). Monotonic, never reset during run. Read by orchestrator at L343.
6. **_active_contexts entry lifecycle**: created at run_instance L336 → stored → popped at finally L386.
7. **TurnContextDescriptor immutability**: frozen=True, constructed per-turn, discarded after turn.

## Interfaces

1. **TurnContextConfigurator** (ABC, T14 §3) — applies()→bool + configure(ctx, desc)→None. 6 concrete implementations.
2. **TurnContextConfigPipeline** (T14 §3) — configure(ctx, desc|None). Short-circuits on None. Ordered list, registration order.
3. **build_runtime_and_context** (existing, T14 modifies) — gains turn_descriptor: TurnContextDescriptor|None=None param. Pipeline runs at END.
4. **_build_turn_descriptor** (new, T14 §5) — on ReActTurnRunner. Reads input_metadata, _is_subagent(), resolver(gid).
5. **tree_id_for_session** (new, T13) — SessionTreeManager. thin wrapper over _node_store.get. Returns str|None.
6. **tree.deliver** (existing, T13 modifies) — gains *, track_consume: bool=False.
7. **tree.wait_quiesce** (existing) — infinite block. No timeout.
8. **get_graph_context** (new, T13) — GraphOrchestrator. Returns GraphContext|None from _active_contexts.
9. **set_graph_context_resolver** (new, T14) — TurnContextBuilder. Post-construction setter for closure.
10. **BotAgentNode.execute** (rewrite, T13) — thin shell: artifacts→deliver→wait_quiesce→return.
11. **SendStrategy.execute** (existing, T14 §6.2 Site 1) — gains graph_instance_id injection between L79-80.
12. **SubagentAutoSendHook._notify_parent** (existing, T14 §6.2 Site 2) — gains graph_instance_id read from ctx.graph_context.
13. **ExternalTurnRunner.process_locked** (existing, T14 §6.2 Site 3) — gains _LightGraphContext from metadata.gid.
14. **DeliverRetryHook** (existing, T14 §8) — gains deliver tool existence check.

## Objects

1. **ModexGraphContext** — Holder: _active_contexts[gid]. Duration: per graph instance run. Release: finally pop. GC: when popped, if no other reference, GC'd. _node_artifacts GC'd with it.
2. **_LightGraphContext** — Holder: AgentContext.graph_context (per-turn). Duration: per-turn. Release: AgentContext discarded after turn. GC: ephemeral.
3. **TurnContextBuilder** — Holder: pool singleton (AgentInstance.pipeline). Duration: pool lifetime. Gains _graph_context_resolver field (set post-construction by pipeline_wiring).
4. **TurnContextConfigPipeline** — Holder: TurnContextBuilder._config_pipeline. Duration: pool lifetime (same as builder).
5. **_active_contexts dict** — Holder: GraphOrchestrator instance. Duration: orchestrator lifetime. Entries: per graph instance, popped in finally.
6. **graph_context_resolver closure** — Holder: TurnContextBuilder._graph_context_resolver. Captures orchestrator.get_graph_context. Duration: pool lifetime. Closure references orchestrator (must be alive when resolver called).
7. **AgentContext** — Holder: per-turn call stack. Duration: one turn. graph_context field set by configurator.

## Concerns

1. **graph_instance_id propagation** — 4 injection sites: SendStrategy.execute (Site 1, covers 3 strategies) / SubagentAutoSendHook (Site 2, independent) / ExternalTurnRunner (Site 3) / modexctl SendRequest (Site 4). Justification: Site 2 bypasses SendStrategy (constructs envelope inline, calls tree.deliver directly). Sites 3-4 are different receiver paths (external/CLI). Question: is Site 2 truly independent or should it converge to SendStrategy?
2. **Dead-end detection** — 2 mechanisms: LinearScheduler `else: reached_end=False; break` (T15-3) vs ParallelScheduler loop exhaustion (no dispatches→loop exits→reached_end stays False). Justification: different scheduler architectures (sequential vs concurrent). Same outcome (FAILED).
3. **Resolver stale-reference gate** — GraphApprovalConfigurator uses `graph_instance_id is not None` while other configurators use `graph_context is not None` or `is_node_execution and agent_kind==MAIN`. Justification: approval must fire even when graph crashed (prevent deadlock). Others skip (no artifacts to install).
4. **GraphContext subclassing** — ModexGraphContext (ReAct, full artifacts) vs _LightGraphContext (External/CLI, gid only). Justification: different execution models (ReAct has configurator pipeline + artifacts; External bypasses configurators).
5. **Turn configuration** — Configurators (new, 6 ordered) vs inline mutation (deleted, was 70 lines). Converged: all turns go through configurator pipeline.
6. **Crash recovery** — bootstrap (native, existing). No separate mechanism. Verified converged.
