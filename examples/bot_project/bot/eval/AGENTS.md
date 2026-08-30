# eval

Opt-in evaluation harness — runs as a **separate process** (CLI), never imported by the bot runtime. Measures agent capability through tool-using multi-turn tasks with world-state assertions, cassette-based deterministic replay, and local calibration metrics. No Langfuse SDK in the bot process (ADR-0024 IN9: OTLP-only).

## Module Table

| File | Purpose |
|------|---------|
| `task_spec.py` | `EvalItemSpec` frozen schema — multi-turn tasks, `EvalToolset` (NONE/READ_ONLY/READ_WRITE/FULL), per-case `deny_tools`, discriminated-union `WorldAssertion` (file_exists/file_absent/file_contains/command_exit), `from_item_input()` legacy fallback |
| `task_output.py` | Typed output models — `EvalTaskOutput`, `WorldResult`, `ToolStats`, `TurnRecord` (stop_reason typed as `StopReason` enum, not raw str) |
| `agent_harness.py` | Loads `react-harness.yml`, applies toolset/deny overlays, and calls the FW declared single-agent seam. Preserves provider-only cassette wrapping, `<workspace>` result normalization, production/clean runtime-service presets, and static prompt constraints |
| `experiment_runner.py` | `EvalRunner` v2 — multi-turn with **fresh AgentContext+Runtime per turn** (production semantics; shares only history/session/tools/services), world assertions (file via pathlib, command via `asyncio.to_thread(subprocess.run)`), tool_stats summed from each turn's stashed `TrajectoryMetrics` (`TurnCustomKey.TRAJECTORY_METRICS`, written by `RootSpanHook` before `clear_trace`; `source="metrics"`) — no trace-store read-back, no network; turns without a stash (OFF mode, hook-less harness) contribute zero; run archiving to `evals/runs/`; legacy single-turn path preserved |
| `memory_harness.py` | Declared root-memory-family harness: archive/core/session configuration comes from `memory-harness.yml` (32K session budget); eval-only memory trace/score hooks and Dream execution remain runtime behavior |
| `evaluators.py` | Langfuse `Evaluation` callables — `world_state_evaluator` (BOOLEAN, all assertions passed), `tool_success_evaluator` (NUMERIC, success_rate), plus pre-existing `completion_evaluator`/`response_length_evaluator`/`avg_accuracy` |
| `replay.py` | `GoldenReplayRunner` — fingerprint preflight plus six pass booleans: zero misses, clean turns, world assertions, no stop mismatches, non-vacuous oracle, and optional tool-success-rate floor; cassette merges reject unequal duplicate keys |
| `dataset_curator.py` | Langfuse v4 curation — `list_traces` via `/api/public/v2/observations` (type=AGENT, root-only), `fetch_trace_io`, `curate()` creates dataset items |
| `metrics.py` | Capability metrics from Langfuse or dormant local `spans.jsonl` data — per-subtree L2 averages, stop_reason/approval/handoff summaries, memory span metrics, and optional derived-memory score publication |
| `cli.py` | Typer CLI — `curate`, `run`, `compare`, `setup-judge`, `metrics`, `record-golden`, and `replay-golden` |
| `evalenv.py` | Frozen `LangfuseCredentials` and parameterized `from_env()` — the single narrow read seam for public/secret key pairs and optional host |
| `harbor/` | Harbor host/container execution, pool-mode assembly, standalone entry, live artifacts, budgets, and smoke/TB21 gates |
| `harbor/eval_overlay.py` | Frozen eval-arm loader whose schema mirrors FW `ScopeOverlay` 1:1 and adds only `single_agent`/`tools_remove`/`strip_mcp` sugar |
| `run-tb21.ps1` / `run-tb21.sh` + `RUNNING_TB21.md` | TB2.1 batch launchers (warm-up gate, checkpoint resume, `-Tasks` subset rerun, post-run sweep) + the run guide. Read `RUNNING_TB21.md` before starting, resuming, re-running, or triaging a TB2.1 batch — it maps the local run data (jobs/, state.db, spans.jsonl, evidence) and the poisoned-task wipe-and-rerun procedure |
| `probes/` | Probe schema, generation, rendering, dual-arm dispatch, evidence, scoring, budget, and harness integration |
| `judge/` + `judge_cli.py` | Rubric runner, memory judge, annotation, calibration, and judge CLI |
| `sentinel/` | Declared memory/no-memory arms, orchestration, execution, observation, results, report, and gate CLI |
| `live_gates/` | B1 cost and B3 experiment-linkage live gates; B1 assembles through the declared seam |
| `config/scopes/eval/eval.yml` | Pool-mode default/benchmark arm overlays; both strip peers, declare `strip_mcp`, and remove the glue tools (`tools_remove: [send_file_to_user, experience]`; the benchmark arm appends both to its existing `[process, terminal]` removals and additionally declares core-memory off with the benchmark prompt) — both arms inherit the target pool's subagent topology (single_agent sugar remains available for a dedicated ablation arm) |
| `config/scopes/eval/agents/*.yml` | Pool-as-root declarations for react harness, 32K memory harness, and the two sentinel arms |
| `agents/benchmark.md` / `agents/react-harness.md` | File-backed benchmark persona and standalone harness default prompt |
| `src/modex_agent/plugins/assembly/single_agent.py` | FW `assemble_declared_single_agent` seam and typed `SingleAgentInfra` substitution surface used by standalone eval paths |
| `__init__.py` | Package marker |

## Key Constraints

- **Separate process**: the bot runtime never imports `bot.eval`; eval constructs agents through the declared single-agent seam (directly or through `agent_harness`)
- **Env-driven observability**: eval reads `OTEL_FORMAT`/`LANGFUSE_HOST`/`LANGFUSE_BASIC_AUTH` env vars (same vars the bot runtime interpolates from `bot_config.yml`), enabling OTLP trace export + 12-metric score injection in both clean and production modes without loading `bot_config.yml`
- **Injector lifecycle**: the harness-built `L2ScoreInjector` is owned by `RootSpanHook` (a `ClosableHook`); in the bot runtime its resident `AsyncClient` closes via `AgentPipeline.stop()` → `HookRunner.aclose()`. The eval CLI builds no pipeline, so its client is released at process exit
- **Provider-only cassette wrap**: tools execute real in both record and replay (verify-the-world); `_WorkspaceTokenNormalizer` makes tool results path-stable across temp dirs
- **Replay gates**: fingerprint preflight, then the six runtime booleans listed in the `replay.py` row; `baseline: true` makes the non-vacuous oracle condition explicit for assertion-free determinism cases
- **READ_ONLY preset includes shell** (`tools/presets.py`) — not a write-ablation; true ablation uses `toolset=NONE` or `deny_tools`
- **Planned**: `export-training` CLI (SFT/DPO export via `TrainingDataExporter` over `LangfuseTraceQuery`, session auto-discovery via Langfuse `v2/sessions`). The `metrics` CLI keeps its jsonl glob — legacy/FILE-mode data only; otel_http data lives in Langfuse (retention = 180d TTL, see `docs/langfuse/langfuse-deployment.md` §10). See `docs/langfuse/langfuse-deployment.md` §6 for the local-vs-Langfuse division of labor

## Langfuse

Query traces, scores, datasets, and observations via `langfuse-cli` (npx, no install) or the `langfuse` skill when debugging eval data, verifying score injection, or curating datasets.

## Related

- `examples/bot_project/evals/` — golden cases, run archives (gitignored), `README.md` (cassette conventions), `DECISIONS.md` (flywheel and convergence decisions)
- `examples/bot_project/tests/eval/` — unit tests for schema, evaluators, harness, runner, replay, metrics, curator, CLI
- `examples/bot_project/tests_ext/regression/` — keyless golden replay regression suite (currently empty-by-policy)
- `src/modex_agent/trace/` — framework trace hooks, cassette, scoring, score injector (the mechanism eval builds on)

## Deviation Ledger (eval vs production)

每一项 eval-vs-production 偏离都必须是 pre-compile overlay 条目（todo 3 机制），或是本表列出的类型化 infra kwarg / registry decorator；装配后突变一律禁止（由架构守卫测试强制执行）。

| Deviation | Mechanism | Why |
| --- | --- | --- |
| InMemoryMessageBroker | typed pool infra kwarg | 单 trial 不依赖生产消息基础设施；`harbor/pool_mode.py:execute_pool_entry` 创建内存 broker，并以 `broker=` 传给 `create_pool`。 |
| NullOutputAdapter | typed pool infra kwarg | Harbor 以 artifact/trace 收集结果，不向生产渠道回传；`harbor/pool_mode_assembly.py:build_eval_pool_assembly` 构造空输出适配器，由 `harbor/pool_mode.py:execute_pool_entry` 以 `output_adapter=` / `im_ui=` 传入。 |
| 无 MCP registry + strip_mcp | pre-compile overlay + typed pool infra kwarg | Trial 不加载生产 MCP 连接：两臂在 `eval.yml` 声明 `strip_mcp: true`（编译前清空 root 的 `mcp` 选择，roster 不依赖宿主 `config/mcp/registry.json`——试次容器本就不携带该文件）；`harbor/pool_mode.py:execute_pool_entry` 另显式传入 `mcp_registry=None`（不共享生产连接）。 |
| 无 KB | typed pool infra kwarg | Trial 不连接生产知识库；`harbor/pool_mode.py:execute_pool_entry` 显式传入 `kb_provider=None`。 |
| 空 SessionPoolIndex | typed pool infra kwarg | 单池 trial 无跨池会话索引状态；`harbor/pool_mode.py:execute_pool_entry` 以 `session_pool_index=SessionPoolIndex()` 注入空索引。 |
| workspace_registry=None | typed pool infra kwarg | 每个 trial 已固定一个 task workspace，不启用生产 multi-live registry；`harbor/pool_mode.py:execute_pool_entry` 显式传入 `workspace_registry=None`。 |
| interceptor 链无 control-drain/llm-cancel | typed pool infra kwarg | Harbor 没有 control channel；公共 `build_tool_overflow_interceptor_chain(control_channel=None)` 构造恰含 tool-result limit 的链，再以 `shared_interceptor_chain=` 传入。 |
| register_pool_budget LLM 工厂包装 | 注册表装饰器 | 预算必须包裹声明解析到的同一 LLM provider factory，而非另建 provider 路；`harbor/pool_budget.py:register_pool_budget` 覆盖注册 `bot_default` 并共享 ledger。 |
| trace_store 调用方持有 + persistence 随生产 wiring | typed pool infra kwarg | `harbor/pool_mode.py:execute_pool_entry` 持有并关闭 `PoolTraceStore`；assembly 按生产 backend 构造 persistence，再将两者传入 pool data / `create_pool`。 |
| converged (todo 6): benchmark arm fully declarative — overlay only | pre-compile overlay | `eval.yml` 声明工具删减、memory core 关闭与 `benchmark.md` prompt；approval-off 叠加 `strip_approval`，两臂均剥 peers；subagent 拓扑从目标池声明继承（task 工具 + 委派 provider 照常生效），`single_agent` 糖保留给专门的单代理消融臂 |
| default 臂剥离胶水工具（`tools_remove: [send_file_to_user, experience]`） | pre-compile overlay（负号条目）→ 编译器单次合并 | 胶水工具面向 IM/业务输出：`send_file_to_user` 经 NullOutputAdapter 无处投递，experience 审阅是业务侧后台循环，在 eval 试次中无意义；负号条目在名-合并基列表上剥除工具与注入的 hook，并经编译名册深绑定同步关闭 ExperienceManager |
| benchmark 臂同名剥离（追加进既有 `tools_remove: [process, terminal]`） | 同上（追加——overlay tools 为统一 append 语义，合并全部交编译器） | 同 default 臂理由 |
| standalone harness / experiment / replay / record-golden | 声明 + typed infra kwarg (`SingleAgentInfra`) | `react-harness.yml` 决定默认 prompt/tool family；provider、safety、workspace root、hooks 与 wrapper 是显式 infra 替换。Cassette 只包 provider，per-turn case prompt 仍由 runner 数据面注入。 |
| memory harness | 声明 + typed infra kwarg (`SingleAgentInfra`) | `memory-harness.yml` 声明 root memory family 与 32K session budget；trace/score cleanup hooks 和 Dream 操作使用装配返回的 memory handle。 |
| sentinel memory / no-memory arms | 声明 + typed infra kwarg (`SingleAgentInfra`) | 两份声明固定 memory 开关、32K、25 steps 与 roster；facts 播种和 span 观测是运行数据，不是装配突变。 |
| Harbor standalone entry + B1 cost gate | 声明 + typed infra kwarg (`SingleAgentInfra`) | entry 直接调用 FW seam（不经 cassette helper）；B1 以无工具、无 governance、trace hooks 的 typed infra 执行一轮真实 turn。 |

### Two-zone assembly guard

`tests/architecture/test_no_post_assembly_mutation.py` 按职责分区而非粗暴禁词：**Zone A** 对整个 `bot/eval` 零容忍直接 `ReActAgent`，并在 assembly-path 模块禁止 Modex `AgentContext`，所有结构装配必须走声明缝。**Zone B** 是计划 per-turn data-injection 条款明确保留的 runner plane：`experiment_runner.py` / `memory_harness.py` 持有每轮 context 数据，`replay.py` / `cli.py` 通过这些 runner API 驱动该平面而不另行装配。Harbor SDK adapter 的同名 context 不是 Modex assembly type。该精度是有意边界，不是守卫漏洞。
