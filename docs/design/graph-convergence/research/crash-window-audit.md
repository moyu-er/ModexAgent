# Crash-Window & Invariant Audit — Graph Scheduling Subsystem

> **状态(2026-08-15)**:本文档是**收敛前基线审计**(基于当时源码)。其中描述的部分机制已被 05/07/08 票定稿取代:dispatch 携带数据→纯唤醒、AgentNode 自建 collect→integrator 过滤、节点级 suspend 快照→退役、bootstrap 意图猜谜→显式 mode、D1/D4 重定性为 at-least-once/输出不丢。保留为历史证据 — 窗口编号(W1-W14)与 DEFECT 编号(D1-D8)仍被 12 票矩阵引用;实施以各票定稿为准,勿以本文为规范。

Ticket: `docs/design/graph-convergence/issues/01-crash-window-invariant-audit.md`
Method: static source audit. Every claim below was traced against current source (not doc statements). No tests were run.

Invariants each window is judged against:

- **I1 recoverable** — after any crash the persisted state is resumable by bootstrap/recovery.
- **I2 no input loss** — a node's input delivers are never permanently lost.
- **I3 bounded duplication** — duplicate delivery/re-execution is acceptable only where documented as by-design; anything undocumented or unbounded is flagged.
- **I4 lifecycle idempotence** — CAS transitions and orphan cleanup behave under crash windows.

Key code paths (canonical orderings, verified):

- `Node.run`: `load_latest` (node.py:190) → `begin_invocation` (node.py:194) → integrate = collect + `mark_consumed` (node.py:291-303) → `execute` (node.py:233) → `submit` (node.py:237) → `complete_invocation` (node.py:240) → `promote_delivers` (node.py:241); suspend node.py:250-254; crash node.py:260-269; finalize node.py:270-273.
- Deliver persistence happens inside `submit`, i.e. **before** `complete_invocation`: `_submit` → `ctx.dispatch` (node.py:444) → scheduler handler (linear.py:145-152 / parallel.py:342-350) → `route_deliver_from_dispatch` (_dispatch_utils.py:70-78) → `coordinator.route_deliver` (persistence_coordinator.py:233-273) → `DeliverStore.accumulate` INSERT PENDING with a fresh Snowflake id (deliver_store.py:438-472).

## (a) Window table

Summary first; details follow.

| ID | Crash point (between) | Invariant impact | Classification | Test coverage |
|----|----------------------|------------------|----------------|---------------|
| W1 | downstream deliver persisted ↔ source `complete_invocation` | duplicate downstream business payload (I3) | **DEFECT (D1)** — at-least-once behavior asserted by tests but undocumented; no idempotency key | partial (LINEAR only) |
| W2 | `execute` returns ↔ `submit` dispatch | pending outputs lost; full re-execution (I3 cost) | **DEFECT (D4)** — feeds 04(b) | NONE |
| W3 | input `mark_consumed` ↔ `complete_invocation` | input re-consumed on re-execution (I3) | OK-BY-DESIGN (input at-least-once; ADR-0038 D5 agent side) | partial |
| W4 | `complete_invocation` ↔ `promote_delivers` | CONSUMED_PENDING stranded (I2/I3) | OK-BY-DESIGN — bootstrap auto-promote (bootstrap.py:87-103) | partial (direct call, not via bootstrap) |
| W5 | mid-invocation process kill (orphan RUNNING node record) | orphan lifecycle (I4) | OK-BY-DESIGN — begin_invocation cleanup + finalize safety net | covered |
| W6 | node `suspend_invocation` ↔ instance-level PAUSED write | suspend split across two stores (I1) | OK-BY-DESIGN — both orderings recover | covered |
| W7 | ParallelScheduler: A's full checkpoint ↔ B's in-flight writes | stale co-runner state restored (I1 quality) | **DEFECT (D3, latent)** — no runtime enforcement; no current real-graph reader | NONE (recovery variant) |
| W8 | instance `begin_invocation(gid)` ↔ engine exit | orphan RUNNING graph instance (I1/I4) | OK-BY-DESIGN — recover_crashed picks up RUNNING | covered |
| W9 | `GraphInterrupt` drain ↔ instance status PAUSED/STOPPED write | control-plane split write (I4) | OK-BY-DESIGN; minor CAS-silent divergence (D6) | covered |
| W10 | store STOPPED/PAUSED write ↔ cooperative engine drain | in-flight node body finishes after STOPPED (I3) | **DEFECT (D6, P2)** — cooperative stop + instance-store CAS never raises | partial |
| W11 | instance `complete_invocation` ↔ `io_store.update_output` / `_finalize_instance` | COMPLETED with null output record; missed terminal event (I1 ok, observability loss) | **DEFECT (D7, P2)** | partial |
| W12 | re-invoke v2 bootstrap ↔ v1 leftover delivers | v1 delivers leak into v2 (I3) | **DEFECT (D2)** — deliver ledger keyed by gid only, no version | NONE |
| W13 | external deliver persisted ↔ engine notify / admission | delayed admission (I2 ok) | OK-BY-DESIGN with LINEAR admission gap (D8, P3) | partial |
| W14 | in-memory `_scheduled_deliver_ids` guard ↔ instance crash | within-run stranding impossible (exceptions propagate) | OK-BY-DESIGN | covered indirectly |

### W1 — Output duplicate-deliver window

Code path: `Node.run` execute (node.py:233) → `submit` (node.py:237) → `_submit` calls `ctx.dispatch` per deliver group (node.py:442-451) → handler validates topology and routes (linear.py:145-152, parallel.py:342-350) → `route_deliver_from_dispatch` (_dispatch_utils.py:70-78) → `coordinator.route_deliver` → `DeliverStore.accumulate` — INSERT of a PENDING row with a **new Snowflake deliver_id** (deliver_store.py:447-472). Only afterwards does `store.complete_invocation` mark the source COMPLETED (node.py:240).

Crash point: any instant after the downstream row is committed and before node.py:240 commits.

Recovery behavior: source invocation is left RUNNING (orphan) or is transitioned CRASHED by the exception path (node.py:260-261) / `finalize_invocation` (node.py:273) / next `begin_invocation` orphan cleanup (node_state_store.py:587-604). `bootstrap` derives it as a re-execute seed (bootstrap.py:71). The node re-executes from scratch and produces **new delivers with new Snowflake ids** — nothing correlates the retry's outputs with the pre-crash ones (no logical idempotency key exists anywhere in `deliver_states`; schema at deliver_store.py:406-426). Downstream then consumes both copies in one integration.

Invariant impact: I3. Duplication count grows by one full output set per crash-retry (unbounded in the number of crashes, bounded by crash count). The behavior is real and asserted by tests (`target.inputs == [["from-a", "from-a"]]`, test_linear_recovery_entry.py:255-281; sqlite orchestrator variant test_external_control_e2e.py:475-529), but **no ADR or AGENTS.md documents output-side at-least-once**: ADR-0033/0034 contain no at-least-once statement (rg over both files finds none), and `src/modex_graph/AGENTS.md` only claims "load_latest + collect_consumable_delivers idempotent restoration" for the input side. Per the strict classification rule this is a **DEFECT** (undocumented at-least-once + no dedup mechanism), not OK-BY-DESIGN. Feeds 06 (idempotency key), 04(c) (transactional outbox option), 12 (matrix).

### W2 — Crash between `execute` return and `submit` dispatch

Code path: `execute(ctx, integrated)` returns (node.py:233) → accumulated delivers exist **only in memory** (`_pending_delivers`, accumulated by `Node._deliver`, node.py:322-341; the docstring explicitly states delivers are in-memory during execute and persistence happens only via route_deliver in the dispatch handler) → `submit` (node.py:237) dispatches.

Crash point: process death after execute's side effects (LLM call, external HTTP, tool writes) but before any dispatch.

Recovery behavior: nothing of the output survived; the invocation is CRASHED/orphan-RUNNING; bootstrap re-executes the whole node body (bootstrap.py:71). All side effects repeat; the downstream eventually receives one copy (from the retry) — so I2 holds for inputs, but the re-execution cost and external side-effect duplication (suggestion.md §六.4) are unmitigated.

Invariant impact: I3 (cost / external duplication). For LLM-backed `BotAgentNode`s a crash here re-bills the whole agent turn. Classification: **DEFECT (cost/semantics)** — the in-memory accumulator is a documented design choice (node.py:328-338, Null-strategy tradeoff table in src/modex_graph/AGENTS.md) but "crash after execute = full re-run" is nowhere documented as the accepted semantic. Feeds 04(b) (persisted pending delivers / submit resumption).

### W3 — Input at-least-once (mark_consumed → complete_invocation crash)

Code path: `collect_consumable_delivers` returns PENDING + CONSUMED_PENDING (persistence_coordinator.py:277-295 → deliver_store.py:474-491, filter `status IN ('pending','consumed_pending')`) → `mark_delivers_consumed` transitions them to CONSUMED_PENDING bound to the current invocation (deliver_store.py:493-512) → execute → `complete_invocation` (node.py:240) → `promote_delivers` (node.py:241, promoted to CONSUMED_COMPLETED via deliver_store.py:514-527).

Crash point: after mark_consumed commits, before complete_invocation commits.

Recovery behavior: consuming invocation ends CRASHED/orphan-RUNNING. Bootstrap's auto-promote (step 4) **only** promotes CONSUMED_PENDING whose `consumed_by_invocation_id` refers to a COMPLETED invocation (bootstrap.py:97-103), so these stay CONSUMED_PENDING. They remain visible to `query_consumable`, and on re-execution the engine-default `Node._integrate_upstream` filters CONSUMED_PENDING **only on the resume path** (node.py:294-297) — a plain crash-retry (not resume-from-suspend) re-consumes them: input at-least-once. `AgentNode` overrides this to **always** filter CONSUMED_PENDING (agent_node.py:111-118), relying on agent session memory instead (ADR-0038 D5, docs/adr/0038-graph-node-agent-context-injection.md decision 5).

Invariant impact: I3 input side. Classification: **OK-BY-DESIGN** — the two-state consumption machine plus the resume filter is the documented durable-execution contract (ADR-0038 D5 for agent nodes; src/modex_graph/AGENTS.md "integrate — collect + mark_consumed. Idempotent"). Hypothesis 2 confirmed in both halves (bootstrap auto-promote gate verified at bootstrap.py:98-102; AgentNode always-filter verified at agent_node.py:115-118).

### W4 — complete_invocation → promote_delivers crash

Crash point: node.py:240 committed (COMPLETED + full state snapshot), crash before node.py:241.

Recovery behavior: delivers stranded CONSUMED_PENDING with a COMPLETED consumer; bootstrap step 4 auto-promotes exactly this case (bootstrap.py:87-103). `promote_delivers` also promotes all CONSUMED_PENDING for the node regardless of consumer id (persistence_coordinator.py:308-333), which additionally repairs a suspended invocation's stranded delivers on the resumed invocation's completion.

Invariant impact: I2/I3. Classification: **OK-BY-DESIGN** — bootstrap.py:16-17 documents the auto-promote as the designed repair for this window.

### W5 — Orphan RUNNING node invocation (process kill mid-run)

Crash point: anywhere between `begin_invocation` INSERT (node_state_store.py:621-642) and the terminal transition.

Recovery behavior: three converging repairs: (1) next `begin_invocation` on that node CAS-marks the prior non-suspended RUNNING record CRASHED (sqlite node_state_store.py:587-604; in-memory :287-296; suspended RUNNING is left in place as a rebuild source, per node_state_store.py:13); (2) `finalize_invocation` in the `finally` of `Node.run` (node.py:270-273, node_state_store.py:749-772) covers exception paths that skip the crash handler; (3) `bootstrap` treats any non-COMPLETED/non-CANCELED latest record (CRASHED or RUNNING, suspended or not) as a re-execute seed (bootstrap.py:66-72). Version chain continues at max+1 (node_state_store.py:606-612).

Invariant impact: I4. Classification: **OK-BY-DESIGN** (node_state_store.py:8-23 documents the strict-CAS / tolerant-CAS split). Coverage: tests/unit/modex_graph/test_node_state_store.py:140 (TestLifecycleTransitions), :239 (TestCASStrictness), :420 (Sqlite specifics); tests/unit/modex_graph/test_persistence_coordinator.py (`finalize_orphan_to_crashed`, `orphan_running_marked_crashed_on_begin`, `suspended_running_left_in_place_on_begin`).

### W6 — Node suspend ↔ instance-level PAUSED split write

Code path: node suspends (checkpoint + `suspend_invocation`, node.py:250-254; record stays RUNNING+suspended with snapshot, node_state_store.py:659-665) → `GraphInterrupt` propagates → orchestrator writes instance PAUSED (graph_orchestrator.py:370-372, instance_store suspend CAS RUNNING→PAUSED :369-370).

Crash points: (i) after node suspend, before instance write → instance stays RUNNING; on restart `recover_crashed` includes orphan RUNNING instances (graph_recovery.py:112-117); bootstrap seeds the suspended node (bootstrap.py:71) and `Node.run` takes the resume path (`load_latest(...).suspended`, node.py:190-191) — snapshot as input base, only PENDING delivers consumed (node.py:294-297). (ii) reverse order cannot occur (instance write is the later write). The finally-block `finalize_invocation` (graph_orchestrator.py:392) cannot corrupt a PAUSED instance because instance-store finalize CAS expects RUNNING (instance_store.py:375-376).

Invariant impact: I1. Classification: **OK-BY-DESIGN**. Coverage: tests/unit/modex_graph/test_node_run_lifecycle.py:145,161,253; tests/integration/graph_orchestration/test_distributed_persistence_e2e.py:318 (`test_suspend_resume_state_snapshot_survives`), :359 (`test_i16_resume_skips_reconsume`).

### W7 — Concurrent snapshot cut (ParallelScheduler only)

Code path: under ParallelScheduler every instance shares `ctx.state` (parallel.py:43-44); when node A completes, `complete_invocation(invocation, ctx.state.checkpoint())` persists a **full** GraphState dump (node.py:240) including `node_scratch` of every node — among them any half-written values of a still-running node B. On crash, `rebuild_main_state` returns the single newest COMPLETED/suspended snapshot (persistence_coordinator.py:337-363) and bootstrap loads it into ctx.state (bootstrap.py:52-54).

Determined facts: (1) `node_scratch[node_id]` is **not reset** anywhere on re-execution — `begin_invocation` only inserts a record (node_state_store.py:583-649) and `Node.run` resets only the instance attributes `_submit_result`/`_pending_delivers` (node.py:216,232), not `ctx.state.node_scratch`; (2) a re-executed B **can read** its stale scratch via `ctx.scratch` (context.py:240-267) and there is no write-set/reducer mechanism; (3) **no real node currently does so**: `rg scratch` over `src/modex_agent/agents/react` returns nothing (ReAct nodes use shared `ReActTurnState` fields, not node_scratch), `BotAgentNode` uses agent session memory rather than scratch (whole file examples/bot_project/bot/graph/agent_node.py), and the ReAct graph compiles under the default LINEAR scheduler (graph.py:125 default; react graph builder compiles without a scheduler argument, src/modex_agent/agents/react/graph.py). The only parallel-scheduler shipped graph is `examples/bot_project/config/graphs/review_cycle.yml:4` (`scheduler: parallel`), whose nodes are BotAgentNodes (no scratch reads). Shared graph fields written mid-await by parallel nodes: none found in shipped graphs (BotAgentNode writes only `ctx.user_data` per-node-keyed, agent_node.py:129-131).

Invariant impact: I1 (state restored is a consistent-enough cut only by node contract, not by construction). Classification: **DEFECT (latent)** — the mechanism is real (stale intermediate scratch survives into recovery and is readable), harm is currently unreachable in shipped graphs because the scratch-isolation contract happens to be respected by every existing node. This matches suggestion.md §六.3 including its caveat that isolation is a node contract, not a runtime guarantee (suggestion.md:313). Feeds 07 (prove-or-fix) + 12.

### W8 — Instance-level orphan RUNNING (begin_invocation(gid) ↔ engine exit)

Code path: `run_instance` loads latest metadata and (non-PAUSED case) calls `instance_store.begin_invocation(gid)` (graph_orchestrator.py:293), which creates version N+1 RUNNING and marks a prior RUNNING version CRASHED (instance_store.py:332-364, in-memory :161-182).

Crash point: process death any time after the begin_invocation commit and before the engine's normal exit.

Who marks CRASHED and when: (1) in-process exception handler (graph_orchestrator.py:381-383, `crash_invocation` unconditional CAS, instance_store.py:372-373); (2) nobody, if the process died — instead `recover_crashed` deliberately picks up both CRASHED **and orphan RUNNING** instances at startup (graph_recovery.py:112-117, docstring :100-103 "a process kill leaves the graph in RUNNING because the in-process exception handler never runs"); (3) any later `begin_invocation` on the same gid marks the prior RUNNING version CRASHED (instance_store.py:336-341). ADR-0040 explicitly defers a periodic stale-running reaper (docs/adr/0040, "Running-state dirty data cleanup" consequence) — ticket 09.

Invariant impact: I1/I4. Classification: **OK-BY-DESIGN**. Coverage: tests/unit/orchestration/test_graph_orchestrator.py:1429 (`test_p1_4_cancellederror_leaves_running_for_recovery`); tests/integration/graph_orchestration/test_external_control_e2e.py:402 (`test_process_crash_recovers_without_replaying_completed_prefix`).

### W9 — GraphDrained / control-path status transitions

Code path: `GraphControlService._pause` persists PAUSED **then** signals the engine (graph_control.py:198-211); `_stop` persists STOPPED then engine.stop() or, with no live engine, `_finalize_instance(gid, STOPPED)` directly (graph_control.py:213-231). The engine drains cooperatively at the next `ctx.control.check()` safe point (run_control.py:34-44, 54-61; linear.py:89 checks per iteration; parallel.py:170 per loop). `run_instance`'s `except GraphDrained` then re-writes STOPPED (if stop_requested) or suspends to PAUSED (graph_orchestrator.py:374-380).

Crash points and behavior: crash between the store write and the drain → store already PAUSED/STOPPED, engine may still be mid-node. For PAUSED the resume flow re-marks RUNNING via unconditional `update_status` before re-running (graph_recovery.py:195-197), so the later CAS transitions match. For STOPPED the instance is terminal and is not auto-recovered (graph_recovery.py:106, e2e test :366). Note: `run_instance`'s finally-block `finalize_invocation` CAS (expects RUNNING) silently no-ops on a STOPPED/PAUSED row because the **instance-store CAS never raises** on rowcount 0 (instance_store.py:378-396) — unlike the node store, which raises `InvocationStateError` (node_state_store.py:717-725). That asymmetry is deliberate-ish (instance finalize is a safety net) but undocumented; folded into D6.

Invariant impact: I4 (idempotence holds — transitions converge — but via silent CAS rather than strict CAS). Classification: OK-BY-DESIGN for recoverability; the CAS-divergence and stop-during-in-flight aspects are D6. Coverage: tests/unit/control/test_graph_control.py:215 (TestPause), :268 (TestStop), :304 (TestResume); e2e :277 (pause/cancel/resume parallel sqlite), :366 (stop terminal).

### W10 — Stop/pause while a node body is in flight

Because drain is cooperative (run_control.py:54-61), a STOPPED write (graph_control.py:226) does not interrupt a node body already executing: the body runs to completion, its `complete_invocation`, delivers and `promote_delivers` all land **after** the store says STOPPED, and the scheduler only observes the drain at the next safe point. The finally-block instance finalize then silently no-ops (see W9). Downstream effects of the stopped node's final delivers persist (PENDING) and will be consumed by any future run/recovery of that gid (bootstrap step 3) — a STOPPED instance's ledger is never quarantined.

Invariant impact: I3 (post-stop side effects) + I4 (silent CAS). Classification: **DEFECT (D6, P2)** — bounded (one node body), but "STOPPED means no further work" is not actually guaranteed, and nothing documents it. Feeds 09/12.

### W11 — _finalize_instance / IORecord failure window

Code path: `complete_invocation(invocation)` commits COMPLETED (graph_orchestrator.py:351) → FAILED override if dead-end (:352-353) → `io_store.update_output` (:355-359) → GraphOutput built (:360-369) → finally: `finalize_invocation` + `_finalize_instance` (unregister engine, drain node events, emit terminal output, evict) (:391-395, :644-676).

Crash points: (i) between :351 and :357 → COMPLETED instance whose latest IORecord keeps `output=None` (the record was created at begin_invocation, :294-304); result data still exists in node state snapshots, but the versioned I/O ledger is wrong. (ii) process death before `_finalize_instance` → terminal GraphOutput never emitted; on restart the registry is gone and nothing replays the terminal event (store status is terminal, so no recovery path re-emits). Emit **failures** inside `_finalize_instance` are already isolated so they never block eviction (graph_orchestrator.py:668-675).

Invariant impact: I1 unaffected (state is terminal and consistent); observability loss only. Classification: **DEFECT (D7, P2)**. Coverage: `test_p0_2_emit_failure_does_not_prevent_eviction` (test_graph_orchestrator.py:1325) covers adapter failure, not the crash-between window — NONE for the window itself.

### W12 — Re-invocation deliver leak (cross-version ledger)

Code path: `start_invoke` validates terminal status COMPLETED/FAILED/CRASHED (graph_orchestrator.py:428-440) → `run_instance` → `begin_invocation` creates version N+1 on the **same graph_instance_id** (graph_orchestrator.py:293, instance_store.py:342-364). The `deliver_states` table is keyed by `(deliver_id, graph_instance_id, node_id)` with **no version column** (deliver_store.py:406-426) and `query_consumable` filters only gid+node_id+status (deliver_store.py:481-489).

Leak paths, both verified:

1. **v1 PENDING leftovers hijack the re-invocation.** bootstrap step 3 scans every node's consumable delivers and adds any node with a PENDING deliver to seeds (bootstrap.py:77-85). Seeds non-empty ⇒ the step-5 re-invocation branch (empty seeds + RUNNING instance → `[entry_node]`, bootstrap.py:115-126, ADR-0040) **never fires**. v2 therefore does not start fresh at entry — it resumes v1's leftover deliver work. `_recheck_pending`'s store scan (parallel.py:546-577) admits the same rows mid-run.
2. **v1 CONSUMED_PENDING leftovers are re-consumed by plain nodes.** If v1's consuming invocation ended CRASHED (never COMPLETED), bootstrap step 4 does not promote (bootstrap.py:98-102); the rows stay CONSUMED_PENDING and remain queryable (deliver_store.py:482-489). A v2 fresh (non-resume) invocation of that node filters nothing (node.py:294-297 filters only on resume), so the v1 input is re-consumed — `mark_consumed` even re-binds `consumed_by_invocation_id` to the v2 invocation (deliver_store.py:493-512). `AgentNode`s are exempt (always-filter, agent_node.py:115-118).

Concrete trigger: a FAILED v1 that dead-ended after delivering downstream (ctx.reached_end=False → FAILED, graph_orchestrator.py:346-353) leaves PENDING delivers; re-invoke then silently executes the leftover branch instead of the user's new run. Also note `start_invoke` accepts CRASHED instances, whose bootstrap seeds are the crashed nodes (bootstrap.py:66-72) — re-invoking a crashed instance is crash-recovery resume, not the fresh execution ADR-0040 describes for completed/failed (verified behaviorally by test_graph_orchestrator.py:1671-1693, where re-invoke of a CRASHED instance re-runs and re-crashes the entry node).

Invariant impact: I3 (cross-invocation duplication / unintended work). Classification: **DEFECT (D2, P0)** — semantics unsettled and version scoping absent; exactly ticket 08's question 2. Coverage: NONE (TestStartInvoke covers only clean re-invocation; no test exercises v1 leftovers under v2).

### W13 — External deliver path (deliver_to_node)

Code path: `GraphControlService._deliver` — requires an active coordinator from the registry (graph_control.py:254-259), validates instance status ∈ {RUNNING, PAUSED, PENDING} (:263-267), persists the deliver via `coordinator.route_deliver` with `source_node_id="__external__"` (:271-276, persistence_coordinator.py:233-273), then notifies the engine if one is registered (:277-279; LiveGraphEngineController → `GraphRunControl.notify_deliver` → wakeup, graph_control.py:130-131, run_control.py:46-48).

Windows: (i) crash between persist and notify → deliver is PENDING; the ParallelScheduler's `_recheck_pending` store scan re-discovers it on the next wake/completion (parallel.py:533-580) — admission delay only, no loss; (ii) deliver to a PAUSED instance → coordinator retained in the registry (only terminal statuses evict, graph_orchestrator.py:624-642) so the persist succeeds; the engine was already unregistered in the drain-exit finally (:393 → :661), so no notify — the deliver waits for `resume()`, whose bootstrap PENDING scan (bootstrap.py:77-85) then admits it; (iii) deliver to a PENDING (created, never run) instance → persisted through the create-time coordinator (covered by test_p0_5, test_graph_orchestrator.py:1408); when `run_instance` later rebuilds the coordinator (:306-320) SQLite stores see the persisted row (shared connection, gid-keyed tables) — for InMemory stores the row is lost, consistent with the documented no-recovery tradeoff (src/modex_graph/AGENTS.md persistence table).

Gap (D8): under the **LinearScheduler there is no in-run admission at all** — no wakeup is wired (only ParallelScheduler calls `ctx.control.set_wakeup`, parallel.py:164) and the next-node choice comes solely from recorded dispatches (linear.py:118-123). An external deliver to a linear graph is consumed only if that node happens to run again this run (consuming all consumable delivers, node.py:291-303) or at the next run's bootstrap. Also, linear recovery resumes only from `seeds[0]` (linear.py:85): with multiple PENDING-deliver seeds, the rest wait for future runs. I2 holds (nothing is lost), admission is delayed. Classification: OK-BY-DESIGN for I2 with **DEFECT (D8, P3)** for the admission gap. Coverage: tests/unit/control/test_graph_control.py:323 (TestDeliver); tests/integration/graph_orchestration/test_distributed_persistence_e2e.py:756, :818, :873; paused-instance → resume ordering: NONE.

### W14 — In-memory `_scheduled_deliver_ids` guard (ParallelScheduler)

`_recheck_pending` skips delivers already admitted in-memory (parallel.py:550-554, ids added at :563/:577). If an admitted instance crashes without consuming, its PENDING deliver would be invisible to the scan for the rest of the run — but an instance crash propagates out of `run_async` (D13 cancel-all, parallel.py:199-205), ending the run; the next run resets the guard (parallel.py:126-135). Same reasoning covers the in-memory ON_RECEIVE FIFO and ON_ALL_PREDS pending-dispatch queues, which are explicitly not persisted (parallel.py:90-92, :355-357) while the underlying delivers are (parallel.py:346-350). No within-run silent stranding is reachable. Classification: OK-BY-DESIGN.

## (b) Hypothesis verdicts

1. **Output duplicate-deliver window — CONFIRMED.** `route_deliver` is called strictly before `complete_invocation`: the full chain submit (node.py:237) → dispatch (node.py:444) → handler (linear.py:150-152 / parallel.py:346-348) → `route_deliver_from_dispatch` (_dispatch_utils.py:76) → `accumulate` INSERT (deliver_store.py:450-471) runs inside `submit`, and only then does node.py:240 execute. Crash in between → source re-executes (bootstrap.py:71) → new Snowflake ids (deliver_store.py:447) → duplicate business payloads downstream, asserted by test_linear_recovery_entry.py:255-281 and test_external_control_e2e.py:475-529. No idempotency key exists. This is D1.
2. **Input at-least-once — CONFIRMED.** Crash between mark_consumed (node.py:299-303 → deliver_store.py:493-512) and complete_invocation (node.py:240) leaves CONSUMED_PENDING with a non-COMPLETED consumer; bootstrap auto-promote gates on COMPLETED (bootstrap.py:98-102) so the rows stay consumable (deliver_store.py:482-489) and re-enter consumption on the engine's non-resume path (node.py:294-297 filters only when resuming). AgentNode always filters CONSUMED_PENDING (agent_node.py:115-118), delegating reduplication safety to agent session memory (ADR-0038 D5). One doc-code divergence found: ADR-0038 D5 also claims `BotAgentNode.execute` detects re-execution via existing session messages and skips duplicate `[Origin Request]` — **current source has no such detection**; the Origin Request block is appended unconditionally whenever `ctx.user_input` is present (examples/bot_project/bot/graph/agent_node.py:216-218). Only the `__start__`-payload skip exists (agent_node.py:244-247). See D5.
3. **Concurrent snapshot cut — mechanism CONFIRMED, current real-graph harm REFUTED.** A's `complete_invocation` checkpoint is a full shared-state dump (node.py:240, parallel.py:43-44) and can contain B's in-flight writes; `rebuild_main_state` may select that snapshot (persistence_coordinator.py:362); `node_scratch[node_id]` is reset nowhere on `begin_invocation` (node_state_store.py:583-649 inserts only; node.py:216/232 reset instance attrs only) and is readable via `ctx.scratch` (context.py:240-267). However, no shipped node reads its own scratch before writing: zero `scratch` references in `src/modex_agent/agents/react`, none in `BotAgentNode` (session-memory-based), and the ReAct graph is LINEAR (default scheduler, graph.py:125); the sole parallel shipped graph (review_cycle.yml:4) uses BotAgentNodes. Verdict for ticket 07: currently unreachable through real graphs; the exposure is contract-level (suggestion.md:313). This is D3.
4. **Orchestrator instance-level windows — verified, mostly OK.** Orphan RUNNING after engine crash: marked CRASHED by the in-process handler (graph_orchestrator.py:381-383), by a later `begin_invocation` (instance_store.py:336-341), or absorbed as recoverable by `recover_crashed`'s RUNNING scan (graph_recovery.py:115-117) — ADR-0040 defers the periodic stale-running reaper (ticket 09). Crash between node `suspend_invocation` and instance PAUSED write recovers through the orphan-RUNNING path with suspended-node resume semantics (W6). GraphDrained/stop transitions converge (W9/W10) with the silent-CAS caveat. `_finalize_instance` failure window: emit failures are isolated (graph_orchestrator.py:668-675) but crash-between windows lose the terminal event / IORecord output (W11, D7).
5. **Re-invocation deliver leak — CONFIRMED.** DeliverStore is keyed by graph_instance_id only (schema deliver_store.py:406-426; `query_consumable` filter :481-489 — status only, no version). After re-invoke, v1's leftover PENDING delivers become v2 seeds via bootstrap step 3 (bootstrap.py:77-85), suppressing the entry-node re-invocation branch (:115-126), and are admitted mid-run by `_recheck_pending` (parallel.py:546-577); v1's CONSUMED_PENDING leftovers (crashed consumer) are re-consumed by v2's fresh invocations of plain nodes (node.py:291-297). AgentNodes escape only the CONSUMED_PENDING half. This is D2; no covering test exists.
6. **External deliver path — verified, no loss, one admission gap.** Persist-then-notify ordering (graph_control.py:271-279) means a crash after persist costs at most a wakeup, which `_recheck_pending` (parallel.py:533-580) or the resume bootstrap (bootstrap.py:77-85) re-supplies; paused instances retain their coordinator (graph_orchestrator.py:624-642) so delivers persist and wait for resume. Gap: LINEAR graphs have no in-run admission (no wakeup wiring; dispatch-record-driven routing, linear.py:118-123) — D8.

suggestion.md §六 four boundaries: §六.1 TRUE (W3), §六.2 TRUE (W1), §六.3 mechanism TRUE / current-graph harm FALSE (W7, D3), §六.4 TRUE by architecture (W2, D4).

## (c) Prioritized DEFECT list

| ID | Window | Defect | Feeds |
|----|--------|--------|-------|
| D1 (P0) | W1 | Duplicate downstream delivers after crash-retry: no logical idempotency key; every retry mints new Snowflake ids; at-least-once behavior is test-asserted but documented nowhere. | 06 (key design), 04(c) (outbox option), 12 (matrix rows) |
| D2 (P0) | W12 | Re-invocation deliver leak: version-less deliver ledger lets v1 PENDING leftovers hijack v2's entry-node start and v1 CONSUMED_PENDING re-enter plain-node consumption; re-invoking a CRASHED instance behaves as crash-recovery, not fresh execution. | 08 (semantics + scoping), 12 |
| D3 (P1) | W7 | Concurrent full-snapshot can embed co-running nodes' intermediate writes; scratch never reset on re-execution; isolation is node contract, not runtime enforcement. Currently unreachable in shipped graphs. | 07 (prove-or-fix + guard test), 12 |
| D4 (P1) | W2 | Crash after `execute` before `submit` loses all pending outputs (in-memory accumulator, node.py:322-341) → whole-node re-execution including LLM turns and external side effects. | 04(b) (persisted pending delivers / submit resumption), 12 |
| D5 (P1) | W3 (agent half) | ADR-0038 D5 doc-code divergence: claimed session-based re-execution detection in `BotAgentNode.execute` does not exist; `[Origin Request]` is re-injected unconditionally on re-execution (agent_node.py:216-218), so a crashed agent node's retry duplicates the origin request in session memory (mitigated only by the CONSUMED_PENDING filter for upstream delivers). | 06 (interaction: external/duplicate input), 12; ADR-0038 needs correction |
| D6 (P2) | W9/W10 | Cooperative stop: STOPPED persists while an in-flight node body still completes and delivers; instance-store CAS silently no-ops on rowcount 0 (instance_store.py:378-396) vs node-store strict CAS raising — lifecycle idempotence is asymmetric and undocumented. | 09 (stale-state cleanup), 12 |
| D7 (P2) | W11 | Crash between instance `complete_invocation` and `io_store.update_output` leaves a COMPLETED instance with `output=None` in the versioned IORecord; crash before `_finalize_instance` loses the terminal GraphOutput with no replay. | 12 (matrix row), 08 (IORecord versioning interplay) |
| D8 (P3) | W13 | LINEAR graphs have no in-run external-deliver admission (no wakeup; dispatch-record-only routing, linear.py:118-123) and recover only from `seeds[0]` (linear.py:85) — multi-pending linear recovery defers work to later runs. No loss; delay only. | 10 (deliver scan convergence), 12 |

## (d) Uncovered-window test-gap list (for ticket 12)

1. W1 under ParallelScheduler (existing duplicate-deliver tests are LINEAR only: test_linear_recovery_entry.py:255, test_external_control_e2e.py:475).
2. W2 crash after `execute` before `submit` — no test anywhere asserts pending-output loss + full re-execution semantics.
3. W4 auto-promote exercised **through `bootstrap()`** (test_f2, test_distributed_persistence_e2e.py:497, calls `promote_delivers` directly).
4. W3 exact consumer-crash-after-mark_consumed re-consumption for an engine-default (non-Agent) node via the bootstrap seed path (current coverage is implicit through the mixed/at-least-once tests).
5. W7 recovery variant: concurrent snapshot containing a co-runner's half-written scratch, then crash + rebuild + re-execution reading stale scratch (normal-run isolation is covered by test_scratchpad_isolation.py only).
6. W10 stop while a node body is in flight (assert post-stop delivers and CAS convergence).
7. W11 crash between `complete_invocation` and `update_output` / before `_finalize_instance` (only emit-failure isolation is tested, test_graph_orchestrator.py:1325).
8. W12 all variants: v1 PENDING leftover hijacking v2 entry start; v1 CONSUMED_PENDING re-consumption by plain nodes in v2; re-invoke of CRASHED instance = recovery-resume semantics.
9. W13 deliver to PAUSED instance followed by resume consuming it end-to-end; external deliver to a LINEAR graph mid-run.
10. W9 suspend split-write crash (after node suspend, before instance PAUSED) recovered via orphan-RUNNING + suspended-node resume.

Well-covered windows (no gap): W5 (node store lifecycle/CAS/orphan), W6 (suspend/resume snapshot), W8 (orphan RUNNING instance, process-crash recovery e2e), W14 (implicitly via parallel crash tests).
