# DECISIONS — the capability flywheel log

One dated entry per improvement cycle. Every entry records the numbers
observed, the decision made (including "no change"), and why. Golden
cassette add/refresh/remove decisions also land here, per the maintenance
rules in `evals/README.md`: every cassette change has a recorded why.

## Standing procedure (six steps)

1. **Measure.** Run both commands from the repo root with the bot venv and
   capture full outputs (no cycle starts without them):

   ```text
   examples\bot_project\.venv\Scripts\python.exe -m bot.eval.cli metrics --workspace examples/bot_project --days 30
   examples\bot_project\.venv\Scripts\python.exe -m pytest examples/bot_project/tests_ext -q
   ```

   The second command is the keyless golden replay — no API keys, offline.

2. **Pick the weakest axis — with numbers.** Rank the observed axes (stop
   reasons, approval decisions, handoffs, cleanup metrics, L2 averages,
   golden suite status) and name the weakest one citing exact figures from
   step 1. Never pick an axis on intuition. A zero counts as "weakest" only
   if the zero is meaningful — a sensor that landed yesterday reports
   zeros that say nothing yet (cycle 1 is the example).

3. **Choose a candidate change against ADR-0024 IN16.** The four
   pre-scoped directions (ADR-0024, section IN16) are: **loop/stuck
   detection** (`TraceDrivenLoopDetectorHook` consuming `spans.jsonl`),
   **error-recovery taxonomy** (classification-driven recovery), **truth
   enforcement** (verification gate in `EndNode`), and
   **experience-review upgrade** (the `background_review.py` pattern). All
   are explicitly gated on calibration data; if the numbers do not yet
   justify any direction, the calibrated decision is "no change —
   accumulate data".

4. **Implement with a golden before/after diff.** Capture the golden suite
   result before the change, apply the change, replay keyless after. A
   behavior change that lacks a golden before/after diff does not ship
   from this flywheel.

5. **Ship** the change together with its DECISIONS.md entry.

6. **Validate in the next cycle.** The next run of step 1 is the acceptance
   test for the previous change: the targeted axis must move, with before
   and after numbers quoted side by side in the entry.

## Cycle 1 — 2026-08-15

**Inputs.** Fresh runs of both procedure commands, executed 2026-08-15 from
the repo root. Full outputs archived in
`.omo/evidence/task-16-eval-harness-capability.txt`.

Metrics (30-day window, workspace `examples/bot_project`):

- Stop reasons: completed 34, cancelled 2, error 4
- Approval decisions: approved 1
- Handoffs: 11
- L2 averages: 29 tool-bearing traces, tool success rate 100.0%,
  reasoning depth 197.0 tokens, trajectory compactness 1.1%
- Cleanup metrics: 0 triggers, 0 malformed records
- Langfuse trend comparison: unavailable (local v4 rc.3 returns 404 for
  `/api/public/v2/scores`)

Golden suite: **5 passed, keyless, in 3.40s** — `chat-notools`,
`file-pipeline`, `file-multi-turn`, `readonly-qa`, plus the
double-run-identity check. The only warnings are the known ReAct
graph-cycle warnings, expected for the ReAct loop.

**Observations per axis.**

- Stop reasons: 4 of 40 stop-reason records are errors (10.0%). The sample
  is small and the metrics carry no error taxonomy yet, so direction 2
  (error-recovery taxonomy) has a signal but not a calibrated one.
- Approvals: 1 approved, 0 denied. The approval path is barely exercised;
  nothing to act on.
- Handoffs: 11. Multi-agent delegation is active; no failure mode visible
  in this metric.
- L2 averages: 100.0% tool success across 29 tool-bearing traces. There is
  no tool-failure or loop signal to justify direction 1 (loop/stuck
  detection) today.
- Cleanup: all zeros — and expected to be. `CleanupMetricsHook` landed
  earlier the same day (2026-08-15, W3-a); no production cleanup has
  triggered since, so there is nothing to count. This is a
  "sensor not yet exposed to the phenomenon" zero, not an "all healthy"
  zero, and it must not be read as health.
- Golden: 5/5 green keyless. The baseline is healthy and every future
  cycle has a working before/after reference.
- Cross-check: every figure matches the task-15 run from earlier the same
  day, so no new traffic accumulated between the two runs; this snapshot
  is dominated by development-driven traces, not sustained production use.

**Known blind spots** (carried forward from the session issues log):

- Four-gate boundary: tampering the FINAL assistant text in a recorded
  cassette still replays green — goldens are trajectory-level regression
  checks, not bit-level content verification (T13 close-out finding).
- No long-trajectory golden exists; governance/compaction behavior is
  unguarded (the 500-token sabotage stayed green on the small goldens).
- Langfuse trend comparison is structurally unavailable on the current
  local deployment (v2 scores API 404), so every cycle is a single
  snapshot, not a trend.

**Verdict: NO CHANGE — accumulate data.**

Rationale: ADR-0024 IN16's own prerequisite is that its strategies "must be
calibrated against data, not implemented blind". One snapshot showing
100.0% tool success on 29 tool-bearing traces, 1 approval, 11 handoffs, and
a 10.0% error-share on 40 stop records does not single out any of the four
directions; the only zero axis (cleanup) is zero because its sensor landed
hours ago. A calibrated no-change decision is the correct first flywheel
output.

**Next-cycle trigger** (whichever comes first):

1. At least 20 new stop-reason records beyond this snapshot — enough
   sample to rank axes meaningfully, or
2. the first nonzero cleanup trigger count, or
3. any intentional agent-behavior change (prompt, tool, governance) that
   needs a golden before/after diff, or
4. the weekly cadence date, 2026-08-22.

## 2026-08-18 — Golden v1 suite removed (all four cases)

**Observed:** audit of the four committed cases — `chat-notools` (zero world
assertions; constant-signal sensor with no discriminating power), `readonly-qa`
and `file-multi-turn` (file-shape assertions only; `file-multi-turn`'s summary
content was unasserted), `file-pipeline` (strongest, but one case cannot anchor
a standard). No case exercised execute-to-verify behavior assertions, and the
suite had zero governance/compression-sensitive trajectories — the double-run
sabotage check documented in tests_ext/regression/test_golden_replay.py was
explicitly unusable on it.

**Decision:** delete all four (suite, not mechanism). The cassette
record/replay machinery, the four-gate contract, and the harness are retained
unchanged. The `eval-regression` CI workflow is paused to manual
`workflow_dispatch` (guards kept; record job now discovers cases dynamically).
Rebuild standard captured in evals/README.md "Golden v2 (TODO)"; rebuild
itself is deferred until the eval-integration judge/benchmark pieces land
(docs/design/eval-integration/MAP.md).

**Why:** a weak suite anchors a weak standard — keeping any of the four would
temple the v2 rebuild toward the old shapes. Deletion is cheap and reversible
(git history) and the $0 replay gate returns the moment a v2 case is committed.

**Next trigger:** eval-integration judge architecture (ticket 03) resolution,
which determines the rubric-assertion layer referenced by the v2 standard.

## 2026-08-21 — Cleanup metrics telemetry retired

**Decision:** retire `CleanupMetricsHook` in eval-integration T12 (commit
55720fb0). The three runtime wirings, the class, and the `cleanup.jsonl`
read/write paths are deleted with no compatibility shim. Memory telemetry now
converges on the default-off `MemoryTraceHook` and memory spans.

**History:** the 2026-08-15 entry above describes the pre-retirement state and
is retained unchanged as the contemporaneous record.

## 2026-08-21 — Harbor pool driving spike

**Observed:** `tests/eval/harbor/test_pool_driving_spike.py` loads the checked-in
`coder` pool and drives the real `create_pool` runtime with
`component_registry=None`, a fake provider at the LLM resolution seam, real
pool data, and the pool-owned inbox poller. Both a direct answer and a `task`
dispatch to the real `explore` template complete through per-session emitter
futures. The delegated callback exposes the child session and its parent, and
`shutdown_all()` leaves the poller task done with no inflight turns. Production
phase-2 `register_communication_tools()` is required after `create_pool`; the
factory alone intentionally does not register `task`.

**Decision:** Harbor's future pool adapter will use `submit_input` plus a
per-session completion future as the driving contract and will reproduce the
production phase-2 communication-tool registration. It must call
`shutdown_all()`, stop the broker, and close pool memory in that order.

**Trace gap:** supplying `PoolData.trace_store` is insufficient when
`app_config=None`: neither the direct nor delegated session writes an
`invoke_agent` or `chat` span. Consequently there is no pool root span on which
to attach T14's five experiment attributes. The adapter must not claim trace
parity until it supplies observability hook configuration and a typed
`ExperimentLinkage` attachment seam. If that seam cannot be exposed through
pool assembly, the minimal alternative is an adapter-owned root-span hook that
calls `attach_experiment_attrs` before saving/exporting the root span; no
parallel hand-written attribute mapping is permitted.

**Approval patch location:** eval-only approval disabling belongs immediately
after `PoolStore.read_pool("coder")` and before `create_pool`, via frozen
`model_copy` on `pool_spec.main.approval`. The checked-in pool YAML remains
unchanged, and no compatibility branch is added to production assembly.

**History (superseded 2026-08-23 by the scope-declaration convergence):** the
driving contract (submit_input + per-session completion future + the
shutdown order) is unchanged and lives on in `bot/eval/harbor/pool_mode.py`.
The assembly-location claims above are superseded: communication tools are
compiler-derived entries in each agent's compiled spec (no phase-2
registration call exists), and the approval-off rewrite applies frozen
`model_copy` on the loaded `ScopeSpec` tree between
`load_scope_declaration` and `boot_scope_spec` — `PoolStore` and the
`pool_spec.main` face are deleted with the roster road. Retained unchanged
as the contemporaneous record.

## 2026-08-24 — 单池 eval 剥离 peers

**Decision:** 按 `eval-config-convergence` plan todo 5，default eval arm 在编译前
声明式叠加 `strip_peers: true`。Peer targets 只有 workspace Phase 2 才能解析，
而 Harbor eval 单次只装配并运行一个 pool，永远不会执行该阶段；因此
`send_to_peer` 在此路径上是悬空死工具，现由同一 scope overlay 机制移除。

## 2026-08-24 — benchmark 收敛记档

**Decision:** benchmark arm 的单代理拓扑、工具删减、memory core 关闭与
`agents/benchmark.md` prompt 全部由 eval scope overlay 在编译前声明；运行时
prompt 与 descriptor 同取该 `file_prompt`。删除装配后的 roster 突变与专用
`benchmark_bash` 生命周期，统一由框架工具槽和 trial teardown 管理。工具成员
保持修正基线；有序 roster 改由槽位解析顺序决定（`bash` 在 ACI `edit` 前），
不再保留旧路通过注销后重注册 bash 形成的偶然顺序。

**Windows delta:** POSIX 继续得到 `PersistentBashTool`（480 秒、无工具内截断、
cwd 为 task workspace）及共享 session 的 `bash_input`；win32 经 FW fallback
得到 `SubprocessTool`。这是修复旧 benchmark 路在无 PTY 主机硬造 persistent
shell 的缺陷，测试按平台分档，Windows 档由对应 CI 执行。

## 2026-08-24 — eval config convergence 收官记档

**Standalone assembly:** todo 8 将 agent harness、experiment runner、replay 与
CLI record-golden 收敛到 poolless declared single-agent seam；声明只提供默认
prompt，case prompt 仍按计划由 per-turn runner 数据面注入。Cassette 继续只包装
provider。黄金 v1 套件按既有决策为空，因此回归命令仅产生 policy skips，指纹
零漂移且没有刷新 golden。

**Declared harnesses:** todo 9 以 `memory-harness.yml` 声明 root archive/core
memory 与 32K session budget；todo 10 以两份 sentinel 声明固定 memory/no-memory
arms。todo 11 的 Harbor entry 直接调用 FW seam（刻意不经 cassette helper），
B1 cost gate 同样经 seam 注入无工具、无 governance、trace-only infra。每条差异
均是 declaration/overlay 或 `SingleAgentInfra` typed substitution，不存在装配后
结构突变。

**Shared seams and credentials:** prompt 解析在 bot 层统一为
`resolve_declared_root_prompt`，旧平行 resolver 删除；production/eval wiring 共享
公开 `declared_assembly_deps` 与 `build_tool_overflow_interceptor_chain`。后者的
builder contract 是 eval 无 control channel 时恰好一项 tool-result limit，生产
有 channel 时按 limit → control-drain → llm-cancel 三项顺序。todo 13 以
`LangfuseCredentials.from_env` 收敛 14 个直接凭据读取点，包含 B1 fold-in。

**Guard adjudication and verification repairs:** todo 11 的 two-zone AST guard
在 assembly zone 禁止直接 agent/context 装配，同时按计划保留 runner plane 的
per-turn context 数据注入；这是精确边界，不是例外装配路。实施中同时将
split-brain goldens 刷新为 persistent-bash v2 companion 现实、将 ARM64 terminal
fixtures 改为 host-independent，并把真实 Langfuse round trips 标记为 live，
避免默认确定性测试触网。

**Evidence:** `.omo-evidence/task-3-eval-config-convergence.md` 至
`.omo-evidence/task-13-eval-config-convergence.md`（重点：8/9/10/11/12/13）。
