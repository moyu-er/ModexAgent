# T15: Technical debt cleanup (Phase 3-4 prerequisite)

> Type: `wayfinder:ticket`
> Status: **Active — design complete**
> Depends on: none (independent, prerequisite for T13/T14)

## Question

What technical debt in `modex_graph` must be cleaned up before Phase 3-4 implementation, based on issues discovered during design validation?

## Background

Phase 3-4 design validation (via parallel explore subagents + codegraph analysis) identified 3 technical debt items in the existing `modex_graph` codebase. These are existing bugs or dead code that Phase 3-4 builds on. Cleaning them first avoids building on a shaky foundation and follows the convergence rule: "If the old path is wrong, remove it."

## Resolution

### T15-1: Delete `GraphContext.fork()` (dead code)

**Finding**: `GraphContext.fork()` (context.py:139-206) has **zero production call sites**. Comprehensive grep across `src/modex_graph/`, `src/modex_agent/`, and `examples/bot_project/` found no `.fork(` calls. Only 9 test call sites exist (in 3 test files), all testing fork() itself.

The fork() docstring (context.py:170-174) explicitly states: "ParallelScheduler does NOT call fork() — it passes ctx directly (scratchpad model, per ADR-0034 D7 refinement)." The `copy(ctx)` pattern was removed; state isolation is via `node_scratch` keys.

**Cleanup scope**:
1. Delete `GraphContext.fork()` method (context.py:139-206)
2. Delete fork() docstring section (context.py:170-174)
3. Delete 9 test call sites:
   - `tests/unit/modex_graph/test_scheduler.py:419, 424, 429`
   - `tests/unit/modex_graph/test_parallel_scheduler.py:586, 598`
   - `tests/unit/modex_graph/test_node_run_lifecycle.py:335, 342, 353, 361`
4. Clean docstring references in: `context.py:100`, `compiled_graph.py:94`, `parallel.py:257`, `modex_graph/AGENTS.md:115`
5. `ModexGraphContext` (T13 D1) does NOT need fork() override — nothing calls it

**Risk**: ZERO. No production code calls fork(). Test deletion only removes tests of dead code.

### T15-2: Remove `ctx.current_invocation` field (concurrency race)

**Finding**: `ctx.current_invocation` (context.py:130) is a shared mutable field written at `node.py:209` inside `Node.run()`. Under ParallelScheduler (passes ctx directly to concurrent tasks), two nodes clobber each other's `current_invocation` — a data race.

The ContextVar mechanism (`get_execution()` / `set_execution()` in `execution_context.py`) was introduced to replace it. `node.py:211-222` already sets `exec_ctx.invocation = invocation` via the ContextVar. L209 is **redundant**.

**Blast radius** (verified via grep):
- **WRITE sites**: 3 — `node.py:209` (production, redundant), `context.py:130` (__init__ assignment), `test_node_run_lifecycle.py:350` (test)
- **READ sites with ContextVar fallback**: 1 — `context.py:336` (`scratch` property)
- **READ sites without fallback**: 8 — all in `test_node_run_lifecycle.py` (single file)
- **Construction sites**: 2 — `context.py:204` (fork pass-through, deleted by T15-1), `test_node_run_lifecycle.py:361` (test fork, deleted with fork tests)
- **`_current_instance` property** (context.py:296-306): already fully migrated — uses `get_execution()` only

**Cleanup scope**:
1. Delete `node.py:209` (`ctx.current_invocation = invocation`) — redundant
2. Delete `current_invocation` parameter from `GraphContext.__init__` (context.py:116) + assignment (context.py:130)
3. Simplify `context.py:336` fallback: `inv = exec_ctx.invocation if exec_ctx is not None else None`
4. Update docstrings: `context.py:100`, `execution_context.py:5,38`, `compiled_graph.py:94`, `parallel.py:257`, `modex_graph/AGENTS.md:115`
5. Rewrite tests in `test_node_run_lifecycle.py`:
   - `test_begin_sets_current_invocation*`: rewrite to assert via `get_execution()` during execution or via `store.load_latest()`
   - `test_current_invocation_not_inherited` + `test_current_invocation_settable_on_fork`: DELETE (fork deleted by T15-1, field removed)

**Risk**: LOW. 0 production reads without fallback. Test rewrites contained in 1 file.

### T15-3: Delete `UndeliveredError` + Node.run retry loop + `max_retry`

**Finding**: `Node.run` (node.py:246-282) has a retry loop: when `execute()` completes without delivers, it re-invokes `execute()` with injected `error_feedback` ("you must call deliver()"), up to `max_retry=3` times. After exhaustion, raises `UndeliveredError(RoutingError)`.

**Why this is wrong**:

1. **LLM-specific logic in generic graph engine.** The error_feedback injection assumes the node is an LLM that reads feedback and self-corrects. For FunctionNode/DelayNode/StartNode, retrying with feedback is useless.

2. **Redundant with scheduler native dead-end detection.** The scheduler already detects "no delivers → no dispatches → no next node → dead end → FAILED":
   - **ParallelScheduler**: no dispatches → no new instances → `_ready` empties + `running` empties → loop exits → `reached_end` stays False → FAILED. The `except UndeliveredError: pass` (parallel.py:287-288) literally just swallows the error so this natural path proceeds. If Node.run doesn't raise, the path is identical.
   - **LinearScheduler**: currently `else: raise RoutingError(...)` (linear.py:125-127) when `self._dispatches` is empty. The comment at L126 says "Unreachable: Node.run raises UndeliveredError before this point — safety net only." This safety net exists BECAUSE UndeliveredError is the primary mechanism. Change to `ctx.reached_end = False; break` — native dead-end detection.

3. **`max_retry` not configurable.** Class attribute on `Node` ABC (node.py:95), not settable via GraphSpec/NodeSpec. Only overridable by subclassing.

4. **Framework shouldn't decide retry policy.** "Node forgot to deliver" is a business concern. LLM node might want retry-with-feedback; function node might want immediate failure; reviewer node might want deliver-to-END. This belongs in the node's `execute()`, not in `Node.run`.

**Verified via code exploration**: removing UndeliveredError + retry loop does NOT break crash recovery. `bootstrap` only looks at CRASHED/RUNNING/PENDING invocation statuses. A node that completed without delivers is COMPLETED (not CRASHED), so bootstrap skips it. `reached_end` stays False → FAILED. Correct behavior.

**Cleanup scope**:

1. **`node.py`**: Remove retry loop (L246-282). Simplify to:
   ```python
   self._pending_delivers = []
   await self.execute(ctx, integrated)
   # proceed to submit (dispatches nothing if _pending_delivers empty)
   ```
   Remove `max_retry` attribute (L95) and `UndeliveredError` import (L45).

2. **`linear.py`**: Remove `try/except UndeliveredError` (L108-112). Change L125-127 from `raise RoutingError(...)` to:
   ```python
   ctx.reached_end = False
   break
   ```

3. **`parallel.py`**: Remove `try/except UndeliveredError: pass` (L285-288).

4. **`exceptions.py`**: Remove `UndeliveredError` class (L88-101). Update `RoutingError` docstring (L80-84) to remove subclass references.

5. **`__init__.py`**: Remove `UndeliveredError` from imports and `__all__`.

6. **Tests** — `test_deliver_submit.py:838-899`: Delete `TestUndeliveredDetection` class (6 tests testing the deleted retry loop). The orchestrator-level dead-end tests in `test_graph_orchestrator.py` (TestDeadEndFailed, TestReachedEndSemantics) still pass after the LinearScheduler fix — they test dead-end → FAILED, which is preserved by the native mechanism.

**Risk**: LOW. ParallelScheduler already handles it natively. LinearScheduler needs one-line change (raise → break). 6 tests deleted test a feature being removed. The retry-with-feedback behavior, if desired for LLM nodes, should be implemented in the node's `execute()` (business layer), not in `Node.run` (framework).

## Verification

- **T15-1 (fork)**: `grep -r '\.fork(' src/ examples/` returns 0 matches after deletion. Tests pass without fork tests.
- **T15-2 (current_invocation)**: `grep -r 'current_invocation' src/` returns 0 code matches (only updated docstrings). `get_execution()` is the sole invocation identity source. ParallelScheduler tests pass (no race).
- **T15-3 (UndeliveredError)**: `grep -r 'UndeliveredError' src/` returns 0 matches. Dead-end tests pass (LinearScheduler `else` branch now breaks instead of raising). ParallelScheduler dead-end detection works natively.

## Implementation order

All 3 items are independent and can be implemented in parallel. Recommended order (by risk ascending):

1. T15-1 (fork deletion) — zero risk, pure deletion
2. T15-3 (UndeliveredError + retry removal) — low risk, scheduler native detection covers it
3. T15-2 (current_invocation removal) — low risk, contained blast radius

T15 is a prerequisite for Phase 3-4 (T13/T14):
- T13 execute rewrite depends on T15-3 (retry loop removal) — execute is called once, no retry
- T14 configurators depend on T15-2 (current_invocation removal) for clean ContextVar usage
- T15-1 (fork deletion) is independent but included for cleanliness

## 不做的设计 (Explicitly Rejected)

### §A — crash_count guard (不做)

**考虑过**: `recover_crashed()` 新增 `crash_count` 字段 + `MAX_RECOVERY_ATTEMPTS = 3` 阈值,防止 consistently-crashing instance 无限重试。

**否决理由**: crash recovery 已经是收敛的原生机制。`recover_crashed` 是 thin wrapper,委托给 `run_instance → bootstrap → 正常调度`。`bootstrap` 是唯一恢复机制(查 store → 派生 seeds → 恢复 state)。scheduler 不区分 fresh start / recovery。Node.run 的 orphan cleanup 是 version chain 卫生(节点级),与 bootstrap 的 seed 派生(图级)互补,不冗余。限制重试次数是业务策略,不是框架职责。如果用户想限制,可在调用层实现。

### §B — Coupling A fix (不做,不再需要)

**考虑过**: 在 node.py 的 `except GraphBubbleUp` 和 `except Exception` 之间新增 `except UndeliveredError: store.cancel_invocation(invocation); raise`,使 node-invocation 标记为 CANCELED 而非 CRASHED。

**否决理由**: T15-3 删除了 `UndeliveredError` class。`except Exception` 不再捕获 UndeliveredError(因为它不存在了)。Coupling A 不再存在。node.py 的 `except Exception` 只捕获真正的 crash(非设计性终止),`crash_invocation` 语义正确。

### §C — _run_existing_instance setup redundancy cleanup (不做,可选)

**考虑过**: `_run_existing_instance` (graph_orchestrator.py:623-656) 做 compile + node_id restore + register,`run_instance` 立即丢弃并重建(coordinator + compile + register)。~8 行浪费工作。

**否决理由**: AGENTS.md 明确标注 "_run_existing_instance is the recovery core path — preserved, not deleted"。这是代码级冗余,非架构级。不影响正确性。可选清理,不在 Phase 3-4 范围。
