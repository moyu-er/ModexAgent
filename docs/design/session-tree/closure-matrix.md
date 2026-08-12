# Phase 3-4 Design Closure Matrix

> **SUPERSEDED**: This matrix references the old `_LightGraphContext` design which has been replaced by `AgentContext.graph_instance_id` (a first-class `int | None` field). The authoritative design is in `issues/14-phase4-turn-context-config-pipeline.md` §6.3. This file is retained for historical context only — do not update it; update T14 instead.

## Dimension Selection Record

| Dimension | Selected? | Why |
|-----------|-----------|-----|
| data-flow | ✅ | graph_instance_id crosses boundaries via envelope.metadata; artifacts transported via ModexGraphContext |
| state-machine | ✅ | GraphInstanceStatus/InvocationStatus/MessageTrackStatus/TreeStatus with multi-value enums |
| interface | ✅ | TurnContextConfigurator ABC + 6 configurators; build_runtime_and_context param; resolver wiring |
| lifecycle | ✅ | ModexGraphContext in _active_contexts; resolver closure; TurnContextBuilder pool singleton |
| convergence | ✅ | 4 graph_instance_id injection sites; ReAct/External split; dead-end detection 2 schedulers |

## Closure Matrix (merged from 5 dimension tracers)

### data-flow
| path | checkpoints | status | note |
|------|-------------|--------|------|
| TurnContextDescriptor: origin→transport→consumption→termination | _build_turn_descriptor→param→configurators→discarded | closed | per-turn, ephemeral |
| GraphTurnArtifacts: origin→transport→consumption→termination | BotAgentNode.execute→ModexGraphContext._node_artifacts→descriptor→configurators→GC with context | closed | |
| graph_instance_id: origin→transport→consumption→termination | GraphContext.graph_instance_id→envelope.metadata(5 sites)→_build_turn_descriptor/ExternalTurnRunner→discarded | closed | 5th site (BotAgentNode.execute origin) documented in F5 fix |
| AgentMessageEnvelope: origin→transport→consumption→termination | SendStrategy.build_envelope / SubagentAutoSendHook inline / BotAgentNode.execute inline→tree.deliver→InboxPoller→consumed | closed | mutable dataclass, post-construction mutation proven |
| ModexGraphContext: origin→transport→consumption→termination | GraphOrchestrator.run_instance→_active_contexts→resolver→descriptor→configurators→finally pop | closed | |
| _LightGraphContext: origin→transport→consumption→termination | ExternalTurnRunner/facade.send→agent_context.graph_context→SubagentAutoSendHook→GC with AgentContext | closed | F3 fixed: super().__init__ |
| AgentContext.graph_context: origin→transport→consumption→termination | Configurator(ReAct)/ExternalTurnRunner(External)→field→5 components+SubagentAutoSendHook→GC with AgentContext | closed | |
| _active_contexts: origin→transport→consumption→termination | GraphOrchestrator.__init__→dict add/remove→get_graph_context→finally pop | closed | |
| track_consume: origin→transport→consumption→termination | BotAgentNode.execute(True)→tree.deliver param→MessageTrack created→on_consumed/on_dispatch_end | closed | |
| MessageTrack: origin→transport→consumption→termination | track_consume=True→MessageTrackStore→is_quiesced/InboxConsumer→CONSUMED/CANCELLED | closed | persisted, recover_tree cleans stale |
| IntegratedInput: origin→transport→consumption→termination | Node.run._integrate_upstream→param to execute→_format_integrated_input→envelope content→discarded | closed | |
| _pending_delivers: origin→transport→consumption→termination | Node.deliver()→instance attribute→_collect_delivers→submit→reset at Node.run start | closed | |

### state-machine
| path | checkpoints | status | note |
|------|-------------|--------|------|
| GraphInstance: PENDING→RUNNING→COMPLETED | entry=run_instance, exit=reached_end=True, recovery=bootstrap skips | closed | |
| GraphInstance: RUNNING→FAILED | entry=reached_end=False(dead-end), exit=terminal, recovery=bootstrap skips | closed | T15-3 native detection |
| GraphInstance: RUNNING→CRASHED | entry=unhandled Exception, exit=terminal, recovery=bootstrap re-runs | closed | |
| GraphInstance: RUNNING→PAUSED | entry=GraphInterrupt, exit=resume→RUNNING, recovery=NOT TRACED | suspected gap S1 | pre-existing, not Phase 3-4 scope |
| GraphInstance: RUNNING→STOPPED | entry=user stop, exit=terminal, recovery=NOT TRACED | suspected gap S1 | pre-existing, not Phase 3-4 scope |
| Invocation: RUNNING→COMPLETED/CANCELED/CRASHED | entry=begin_invocation, exit=complete/cancel/crash, recovery=bootstrap | closed | T15-3: CANCELED entry reduced to GraphBubbleUp only |
| MessageTrack: DISPATCHED→CONSUMED/CANCELLED | entry=track_consume=True, exit=on_consumed/on_dispatch_end/recover_tree, recovery=persisted+recover_tree | closed | |
| Tree: ACTIVE→COMPLETED/CANCELLED | entry=first deliver, exit=quiesce/recover_tree/on_session_evicted, recovery=recover_tree | partially closed | stuck scenario: no timeout, deferred to heartbeat (S3) |
| reached_end: False→True | entry=default, exit=_dispatch_utils.py:71, monotonic, persisted | closed | |
| _active_contexts entry: created→stored→popped | entry=run_instance, exit=finally, in-memory only | closed | degradation on crash (§11.1) |
| TurnContextDescriptor: constructed→discarded | entry=_build_turn_descriptor, exit=turn ends, ephemeral | closed | |

### interface
| path | checkpoints | status | note |
|------|-------------|--------|------|
| TurnContextConfigurator ABC | caller=pipeline, params=desc, return=bool/None, definition=T14§3, wiring=pipeline list | closed | F1 fixed: sync applies+configure |
| TurnContextConfigPipeline | caller=build_runtime_and_context, params=ctx+desc, return=None, definition=T14§3, wiring=TurnContextBuilder._config_pipeline | closed | F1 fixed: sync configure |
| build_runtime_and_context | 19 callers(all sync), gains turn_descriptor param, pipeline at END | closed | F1 fixed: no await needed |
| _build_turn_descriptor | caller=_process_locked_inner, params=input_metadata+session+pool_data, return=descriptor, wiring=resolver on builder | suspected gap | F6: resolver access path (self._builder._graph_context_resolver) |
| tree_id_for_session | caller=execute, params=session_id, return=str|None, wiring=_resolve_pool().tree_manager | closed | |
| tree.deliver track_consume | caller=execute(True)+others(default), params=*,track_consume:bool=False | closed | |
| tree.wait_quiesce | caller=execute, params=tree_id only, return=None | closed | F2 fixed: signature change documented |
| get_graph_context | caller=resolver closure, params=gid, return=GraphContext|None, wiring=_active_contexts | closed | |
| set_graph_context_resolver | caller=pipeline_wiring, params=Callable, wiring=pool-level setter | suspected gap | F6: orchestrator lifetime vs pool lifetime |
| BotAgentNode.execute | caller=Node.run, params=ctx+integrated_input, wiring=_resolve_pool().tree_manager | closed | ctx downcast to ModexGraphContext (precondition) |
| SendStrategy.execute Site 1 | caller=AgentCommunicationService, params=SendRequest, return=AgentSendResult | closed | |
| SubagentAutoSendHook Site 2 | caller=finally_graph hook, params=ctx+session_id+content | closed | |
| ExternalTurnRunner Site 3 | caller=InboxPoller dispatch, params=input_msg+session | closed | F3 fixed: _LightGraphContext super().__init__ |
| DeliverRetryHook | caller=HookRunner AFTER_TURN, params=ctx+result | closed | get_tool() confirmed exists |

### lifecycle
| path | checkpoints | status | note |
|------|-------------|--------|------|
| ModexGraphContext | holder=_active_contexts[gid], duration=per graph instance, release=finally pop, GC=with pop | closed | |
| _LightGraphContext | holder=AgentContext.graph_context, duration=per-turn, release=AgentContext discarded, GC=ephemeral | closed | |
| TurnContextBuilder | holder=pool singleton, duration=pool lifetime, gains resolver field | closed | |
| TurnContextConfigPipeline | holder=TurnContextBuilder._config_pipeline, duration=pool lifetime | closed | |
| _active_contexts dict | holder=GraphOrchestrator, duration=orchestrator lifetime, entries popped in finally | closed | _evict_if_terminal does NOT evict on PAUSED (verified) |
| graph_context_resolver closure | holder=TurnContextBuilder, captures orchestrator, duration=pool lifetime | suspected gap | F6: orchestrator must be pool/workspace-scoped |
| AgentContext | holder=per-turn call stack, duration=one turn, GC=after turn | closed | |

### convergence
| path | checkpoints | status | note |
|------|-------------|--------|------|
| graph_instance_id propagation | 4 sites + 1 origin, justified by different execution models | closed | SG1: Site 2 injection logic could be shared helper |
| Dead-end detection | 2 schedulers, justified by different architectures, same outcome | closed | T15-3 converges by removing retry loop |
| Resolver stale-reference gate | 3 different applies(), justified by deadlock prevention | closed | |
| GraphContext subclassing | ModexGraphContext vs _LightGraphContext, justified by ReAct/External split | closed | |
| Turn configuration | configurators (new) vs inline mutation (deleted) + External direct assignment | closed | External bypass justified |
| Crash recovery | bootstrap (single mechanism), no separate engine | closed | F4 fixed: stale retry loop reference removed |

## Findings (fixed)

| ID | Severity | Finding | Fix |
|----|----------|---------|-----|
| F1 | CRITICAL | build_runtime_and_context is sync, design inserted `await pipeline.configure()` — syntax error | Made applies()+configure() sync. All gates are pure field reads. |
| F2 | CRITICAL | wait_quiesce current signature has `timeout: float`, design calls with 1 arg — TypeError | Documented signature change: `wait_quiesce(tree_id: str) -> None` |
| F3 | HIGH | _LightGraphContext.__init__ didn't call super().__init__ — base invariants not established | Added super().__init__ with minimal Null stores |
| F4 | MEDIUM | T13 D5 referenced deleted retry loop + "T15-5" typo | Removed stale reference, fixed T15-5→T15-3 |
| F5 | MEDIUM | BotAgentNode.execute is 5th injection site (origin), not in T14 §6.2 table | Documented as origin point in §6.2 |

## Suspected gaps (need verification/design decision)

| ID | Gap | Why unclear |
|----|-----|-------------|
| F6 | set_graph_context_resolver wiring: TurnContextBuilder is pool-level, orchestrator lifetime unknown | Need to verify GraphOrchestrator is pool/workspace-scoped (not per-graph-instance). If pool-scoped, resolver closure is fine. If per-graph, need registry pattern. |
| S1 | PAUSED/STOPPED states not traced in T13/T14/T15 | Pre-existing states (GraphInterrupt/user stop). Phase 3-4 doesn't modify them. Need to decide: trace in Phase 3-4 or mark out-of-scope? |
| S2 | SSE child discovery: graph_instance_id via env var, but SubagentAutoSendHook reads ctx.graph_context | Need to verify SSE child has graph_context set (via ExternalTurnRunner reading metadata, or via env var → _LightGraphContext) |
| S3 | Tree ACTIVE stuck: no timeout, no supersession | Documented limitation, deferred to heartbeat detection (out of Phase 3-4 scope) |
