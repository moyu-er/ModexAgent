# Judge calibration runbook

Run every command from `examples/bot_project` with `.venv\Scripts\python.exe`.
This runbook is a dispatch checklist; do not run the human session or live judge
steps during implementation verification.

## 0. Load the live environment

```powershell
Get-Content .env | ForEach-Object {
  if ($_ -match '^\s*([^#][^=]*)=(.*)$') {
    $name = $matches[1].Trim()
    $value = $matches[2].Trim().Trim('"').Trim("'")
    Set-Item -Path "Env:$name" -Value $value
  }
}
$env:JUDGE_MODEL = "openai/step-3.7-flash"
$env:JUDGE_BASE_URL = "https://api.stepfun.com/step_plan/v1"
$env:JUDGE_API_KEY = (Get-Content .env | Where-Object { $_ -match '^JUDGE_API_KEY=' } | ForEach-Object { $_.Split('=', 2)[1].Trim().Trim('"').Trim("'") })
```

- **Input:** `.env` containing `JUDGE_API_KEY`, Langfuse credentials, and the
  `TEST_LLM_*` answer-model values used by the eval runner.
- **Expected output:** none. `$env:JUDGE_MODEL` is
  `openai/step-3.7-flash`, `$env:JUDGE_BASE_URL` is
  `https://api.stepfun.com/step_plan/v1`, and the API key remains unprinted.

## 1. Smoke retest dispatch (3-5 items, no annotation)

`b1_cost_smoke.py` records `gate=b1_cost_smoke`, a session ID, and a trace ID in
`evals/evidence/b1_cost_smoke.json`; it does **not** attach Langfuse experiment
attributes and therefore has no valid `judge --experiment` name. Do not invent
`b1-cost-smoke` as an experiment: T18 would return an empty report. Create the
real smoke experiment below from up to five recent traces (including the B1
trace when it is among the curated candidates):

```powershell
.venv\Scripts\python.exe -m bot.eval.cli curate --dataset b4-calibration-smoke-v1 --max 5 --no-filter-errors
.venv\Scripts\python.exe -m bot.eval.cli run --dataset b4-calibration-smoke-v1 --experiment b4-calibration-smoke-v1 --model $env:TEST_LLM_MODEL --max-concurrency 1 --mode clean
.venv\Scripts\python.exe -m bot.eval.cli judge --experiment b4-calibration-smoke-v1 --dataset b4-calibration-smoke-v1 --rubric-set general-agent --repeats 3 --limit 5 --archive-root evals/runs/judge
```

Healthy instances may have no error traces; `--no-filter-errors` disables the
interesting-case filter so successful conversations can be curated.

- **Inputs:** healthy Langfuse/collector; 3-5 curatable traces; configured
  `TEST_LLM_*` and `JUDGE_*` variables.
- **Expected output:** curate reports 3-5 items; run materializes experiment
  `b4-calibration-smoke-v1`; judge prints one line per trace and a final
  `judged=<3..5> ... agreement=<percent> repeats=3`. First-repeat full
  `JudgeResult` files appear under
  `evals/runs/judge/b4-calibration-smoke-v1/`.

Record the printed agreement as a decimal and assemble the smoke receipt:

```powershell
$RETEST_AGREEMENT = 1.0  # replace with the observed percentage divided by 100
.venv\Scripts\python.exe -m bot.eval.judge.annotate receipt --experiment b4-calibration-smoke-v1 --rubric-set general-agent --judge-model openai/step-3.7-flash --archive-dir evals/runs/judge/b4-calibration-smoke-v1 --retest-repeats 3 --retest-agreement $RETEST_AGREEMENT --output evals/evidence/b4_calibration.json
```

- **Input:** the smoke judge archive and the observed T18 agreement.
- **Expected output:** `evals/evidence/b4_calibration.json` with
  `mode="smoke"`, retest statistics, `kappa.status="pending"`,
  `confusion_matrices.status="pending"`, `calibrated=false`, and
  `gray_flag=true`. No calibration status is promoted.

## 2. Pilot 10 and resumable human annotation

```powershell
.venv\Scripts\python.exe -m bot.eval.cli curate --dataset b4-calibration-pilot-v1 --max 10
.venv\Scripts\python.exe -m bot.eval.cli run --dataset b4-calibration-pilot-v1 --experiment b4-calibration-pilot-v1 --model $env:TEST_LLM_MODEL --max-concurrency 1 --mode clean
.venv\Scripts\python.exe -m bot.eval.cli judge --experiment b4-calibration-pilot-v1 --dataset b4-calibration-pilot-v1 --rubric-set general-agent --repeats 3 --limit 10 --archive-root evals/runs/judge
.venv\Scripts\python.exe -m bot.eval.judge.annotate annotate --archive-dir evals/runs/judge/b4-calibration-pilot-v1 --output evals/judge/annotations/b4-calibration-pilot-v1.jsonl --rubric-set general-agent
```

- **Inputs:** exactly ten mixed good/bad/boundary experiment traces and the
  T18 judge archive. Keep the Langfuse trace view available when judging the
  candidate answer because T18's frozen `JudgeResult` contains verdict evidence
  but not the original candidate input/output.
- **Expected output:** the annotation CLI presents trace identity, judge
  summary/verdict/evidence, and the matching rubric description before each
  prompt. It accepts only `MET`, `UNMET`, or `NA`; invalid input re-shows the
  same item. Each accepted pair is appended immediately to the JSONL. On
  interruption, rerun the same command; completed `(trace_id, criterion)` pairs
  are skipped.

## 3. Compute κ and persist fail-closed calibration status

Prepare `evals/judge/calibration/b4-calibration-pilot-v1.input.json` as the
strict `CalibrationInput` shape below. Build each dimension's `judge` and
`human` vectors from the annotation JSONL in identical trace order. Preserve
three repeat verdict vectors in `retest_reviews`. Populate `bias_items` with the
judge verdict and actual candidate answer character length from Langfuse.

```json
{
  "dimensions": [
    {
      "name": "task_completion",
      "judge": ["MET", "UNMET"],
      "human": ["MET", "UNMET"]
    }
  ],
  "retest_reviews": [
    ["MET", "UNMET"],
    ["MET", "UNMET"],
    ["MET", "UNMET"]
  ],
  "bias_items": [
    {"verdict": "MET", "answer_length": 120},
    {"verdict": "UNMET", "answer_length": 260}
  ]
}
```

Dispatch the typed T19 calculation and sole status writer:

```powershell
.venv\Scripts\python.exe -m bot.eval.judge.annotate calibrate --input evals/judge/calibration/b4-calibration-pilot-v1.input.json --rubric-set general-agent --judge-model openai/step-3.7-flash --run-record evals/judge/calibration/b4-calibration-pilot-v1.run.json --status-dir evals/judge/calibration
```

- **Input:** schema-valid `CalibrationInput` containing all pilot dimensions,
  all three repeat vectors, and real answer lengths.
- **Expected output:** a complete `CalibrationRunRecord` at the requested path;
  κ/confusion matrices/retest/NA/bias/skew metrics in its `report`; and the
  fail-closed status file
  `evals/judge/calibration/general-agent@openai%2Fstep-3.7-flash.json`.
  The command prints `calibrated=True` only when every T19 gate passes.

Pilot decision:

- Any conclusive dimension κ below `0.60`, overall κ below `0.67`, retest below
  `0.95`, NA/CANNOT_ASSESS rate at or above `5%`, long/short gap at or above
  `10pp`, or FP/FN skew above `2x`: revise prompt/model/rubric and restart at
  pilot 10. Scores stay gray.
- If the pilot passes: continue to the final 20-30 item set.

## 4. Expand to 20-30 and freeze the full receipt

Repeat sections 2-3 with these concrete names and a chosen final size:

```powershell
$FINAL_LIMIT = 30
.venv\Scripts\python.exe -m bot.eval.cli curate --dataset b4-calibration-final-v1 --max $FINAL_LIMIT
.venv\Scripts\python.exe -m bot.eval.cli run --dataset b4-calibration-final-v1 --experiment b4-calibration-final-v1 --model $env:TEST_LLM_MODEL --max-concurrency 1 --mode clean
.venv\Scripts\python.exe -m bot.eval.cli judge --experiment b4-calibration-final-v1 --dataset b4-calibration-final-v1 --rubric-set general-agent --repeats 3 --limit $FINAL_LIMIT --archive-root evals/runs/judge
.venv\Scripts\python.exe -m bot.eval.judge.annotate annotate --archive-dir evals/runs/judge/b4-calibration-final-v1 --output evals/judge/annotations/b4-calibration-final-v1.jsonl --rubric-set general-agent
.venv\Scripts\python.exe -m bot.eval.judge.annotate calibrate --input evals/judge/calibration/b4-calibration-final-v1.input.json --rubric-set general-agent --judge-model openai/step-3.7-flash --run-record evals/judge/calibration/b4-calibration-final-v1.run.json --status-dir evals/judge/calibration
.venv\Scripts\python.exe -m bot.eval.judge.annotate receipt --experiment b4-calibration-final-v1 --rubric-set general-agent --judge-model openai/step-3.7-flash --archive-dir evals/runs/judge/b4-calibration-final-v1 --annotations evals/judge/annotations/b4-calibration-final-v1.jsonl --retest-repeats 3 --retest-agreement 1.0 --calibration-run evals/judge/calibration/b4-calibration-final-v1.run.json --output evals/evidence/b4_calibration.json
```

- **Inputs:** 20-30 final traces, completed annotations, typed final
  `CalibrationInput`, and its generated run record. Replace the final receipt's
  `--retest-agreement 1.0` with the observed final agreement; the run record is
  authoritative for the full receipt's retest fields.
- **Expected output:** the same `b4_calibration.v1` shape as smoke, now with
  `mode="full"`, complete per-dimension/overall κ, complete confusion matrices,
  retest statistics, NA/bias fields, and gray/calibrated status matching the
  persisted T19 report. Keep this exact file for the F2 audit.

Any judge prompt, judge model, or rubric-set change invalidates this result and
requires rerunning the affected set from pilot 10.
