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
