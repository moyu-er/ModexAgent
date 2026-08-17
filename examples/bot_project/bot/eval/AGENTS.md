# eval

Opt-in evaluation harness — runs as a **separate process** (CLI), never imported by the bot runtime. Measures agent capability through tool-using multi-turn tasks with world-state assertions, cassette-based deterministic replay, and local calibration metrics. No Langfuse SDK in the bot process (ADR-0024 IN9: OTLP-only).

## Module Table

| File | Purpose |
|------|---------|
| `task_spec.py` | `EvalItemSpec` frozen schema — multi-turn tasks, `EvalToolset` (NONE/READ_ONLY/READ_WRITE/FULL), per-case `deny_tools`, discriminated-union `WorldAssertion` (file_exists/file_absent/file_contains/command_exit), `from_item_input()` legacy fallback |
| `task_output.py` | Typed output models — `EvalTaskOutput`, `WorldResult`, `ToolStats`, `TurnRecord` (stop_reason typed as `StopReason` enum, not raw str) |
| `agent_harness.py` | Dual-mode assembly — `build_tool_manager` (preset via shared `build_preset_tool_manager` + FilteredToolManager), `_WorkspaceTokenNormalizer` (replaces abs workspace path with `<workspace>` token in tool results for cassette key stability), `wrap_provider` (provider-only cassette wrap — tools NEVER wrapped), `_ModelPinningProvider` (pins explicit model into keys), `_eval_observability` (builds `ObservabilityConfig` + `L2ScoreInjector` from `OTEL_FORMAT`/`LANGFUSE_HOST`/`LANGFUSE_BASIC_AUTH` env vars — mirrors `bot_config.yml` `${ENV}` interpolation so eval behaves identically to bot runtime), `build_runtime_services` (production: trace hooks via `build_trace_stores` + governance + loop/checkpoint + optional CassetteFlushHook), `build_trace_only_services` (clean: trace hooks ONLY — no governance/loop/checkpoint/turn_store), `static_system_prompt` (no timestamps, no abs paths — RuntimeProvider excluded) |
| `experiment_runner.py` | `EvalRunner` v2 — multi-turn with **fresh AgentContext+Runtime per turn** (production semantics; shares only history/session/tools/services), world assertions (file via pathlib, command via `asyncio.to_thread(subprocess.run)`), tool_stats from spans (both clean and production modes), run archiving to `evals/runs/`; legacy single-turn path preserved |
| `evaluators.py` | Langfuse `Evaluation` callables — `world_state_evaluator` (BOOLEAN, all assertions passed), `tool_success_evaluator` (NUMERIC, success_rate), plus pre-existing `completion_evaluator`/`response_length_evaluator`/`avg_accuracy` |
| `replay.py` | `GoldenReplayRunner` — fingerprint gate (model/temp/tool_names/tool_schema_sha256/prompt_sha256/platform), `merge_cassettes` (conflict-checked: duplicate keys byte-compared, differ → raise), `CaseResult` with four gates (fingerprint + zero misses + error-None+COMPLETED per turn + non-vacuous oracle) |
| `dataset_curator.py` | Langfuse v4 curation — `list_traces` via `/api/public/v2/observations` (type=AGENT, root-only), `fetch_trace_io`, `curate()` creates dataset items |
| `metrics.py` | Local calibration report — per-subtree L2 averages (shared `compute_root_subtrees` with RootSpanHook), stop_reason histogram, approval decisions, handoff counts, cleanup metrics (tokens_saved/savings_rate/thrash); reads `spans.jsonl` + `cleanup.jsonl` |
| `cli.py` | Typer CLI — `curate`, `run` (--toolset/--mode), `record-golden`, `replay-golden`, `metrics`, `compare` (v4 `experiments` API + `v3/scores` time-window aggregation, replacing v3 `dataset-runs` endpoint disabled in events_only mode) |
| `__init__.py` | Package marker |

## Key Constraints

- **Separate process**: the bot runtime never imports `bot.eval`; eval constructs its own agent instances via `agent_harness`
- **Env-driven observability**: eval reads `OTEL_FORMAT`/`LANGFUSE_HOST`/`LANGFUSE_BASIC_AUTH` env vars (same vars the bot runtime interpolates from `bot_config.yml`), enabling OTLP trace export + 12-metric score injection in both clean and production modes without loading `bot_config.yml`
- **Provider-only cassette wrap**: tools execute real in both record and replay (verify-the-world); `_WorkspaceTokenNormalizer` makes tool results path-stable across temp dirs
- **Four replay gates**: fingerprint + zero cassette misses (engine counter) + per-turn error-None+COMPLETED + non-vacuous oracle; `baseline: true` in meta.json marks cases whose oracle IS the gates+determinism
- **READ_ONLY preset includes shell** (`tools/presets.py`) — not a write-ablation; true ablation uses `toolset=NONE` or `deny_tools`
- **Planned**: `export-training` CLI (SFT/DPO export via `TrainingDataExporter` + session auto-discovery) and `spans.jsonl` retention policy — see `docs/langfuse/langfuse-deployment.md` §6 for the local-vs-Langfuse division of labor

## Langfuse

Query traces, scores, datasets, and observations via `langfuse-cli` (npx, no install) or the `langfuse` skill when debugging eval data, verifying score injection, or curating datasets.

## Related

- `examples/bot_project/evals/` — golden cases, run archives (gitignored), `README.md` (conventions + four-gate contract), `DECISIONS.md` (flywheel log)
- `examples/bot_project/tests/eval/` — unit tests for schema, evaluators, harness, runner, replay, metrics, curator, CLI
- `examples/bot_project/tests_ext/regression/` — keyless golden replay regression suite (four gates + double-run identity)
- `src/modex_agent/trace/` — framework trace hooks, cassette, scoring, score injector (the mechanism eval builds on)
