Status: ready-for-agent

# Agent Observability, Reproducibility, and Training Data

## Problem Statement

ModexAgent has a custom flat JSONL trace mechanism that records 5 operation kinds
(`LLM_CALL` / `TOOL_BATCH` / `TOOL_CALL` / `TURN_START` / `TURN_END`) per turn,
written via `TraceCollectorHook` to
`<workspace>/.modex/runtime_state/<pool>/trace/<session>/operations.jsonl`. It serves
basic local observability and enables a unique capability — the main agent can
self-read a subagent's trace file via `format_send_ack`, which reports the trace path
in the dispatch ack text. But the mechanism has structural limits that cannot be
fixed incrementally:

- **No span tree**: `OperationRecord` is flat — no `parent_span_id`. An agent reading
  its own trace cannot tell which LLM call belongs to which ReAct iteration, or which
  tool call is a child of which tool batch. Multi-step reasoning structure is lost.
- **No industry tooling compatibility**: the custom `OperationRecord` format is
  readable only by ModexAgent. No external observability tool (Jaeger, Langfuse,
  Phoenix, Datadog) can ingest it. Users who want real observability dashboards,
  eval layers, or cost analytics have no path.
- **No cross-process tracing**: external CLI agents (Pi/OpenCode) run as subprocesses.
  The parent's `invoke_agent` span records duration and exit code, but the
  subprocess's internal LLM/tool activity is invisible — the trace is shallow on the
  child side, with no way to link parent and child into one coherent trace tree.
- **No reproducibility**: `temperature=0` is empirically non-deterministic (Qwen3-235B
  produces 80 unique outputs from 1000 identical prompts; GPT-4o accuracy swings by
  72 points). The existing `TurnSnapshot` / `RuntimeStateCodec` infrastructure (proven
  by approval suspend/resume) only triggers on approval suspend — not per-iteration.
  There is no cassette mechanism for bit-identical replay. Bugs that depend on a
  specific LLM output or tool result cannot be reproduced.
- **No training data path**: the framework generates rich reasoning traces
  (`reasoning_content`, multi-step tool calls, approval decisions) but has no way to
  export them as training data. The algorithm team manually extracts data with
  ad-hoc scripts. Meanwhile, industry research (Microsoft Agent Lightning) proves
  that OTel spans captured during agent rollouts are *the* training-data substrate —
  `TraceToMessages` and `TraceToTriplet` adapters convert spans directly into SFT
  OpenAI-messages JSONL and RL triplets. ModexAgent cannot leverage this because its
  trace format is not OTel.
- **3 unused `OperationKind` values**: `APPROVAL` / `CONTROL_COMMAND` / `ERROR` are
  defined in the enum but never recorded — approval decisions, control commands, and
  errors are invisible in the trace.
- **`HookPoint` docstring is stale**: says "HookRunner dispatches via
  `getattr(hook, hook_point.value)`" but the implementation uses `isinstance` + ABC
  dispatch. This misleads contributors.

The user wants three capabilities that the current mechanism cannot provide:

1. **可观测 (Observable)** — agent behavior trackable and viewable, with
   industry-standard tooling, while preserving the local-file agent-self-read
   capability that makes ModexAgent's multi-agent coordination work.
2. **可复现 (Reproducible)** — agent executions replayable for debugging and
   regression, at two levels: checkpoint re-execution (default, low cost) and
   deterministic cassette replay (opt-in, bit-identical).
3. **训练数据 (Training data)** — reasoning process and execution data collected for
   model training, derived from the observability layer without a third write path.

## Solution

Replace the custom `OperationRecord` / `operations.jsonl` trace mechanism with an
**OpenTelemetry-native observability layer** that emits standard `gen_ai.*` spans,
while preserving the local-file agent-self-read capability via a default-on
`FileSpanExporter`. Build a **dual-path + derivation architecture** spanning three
capability layers:

- **Trace Path (通路 A)** — observability: existing 5 hook points emit OTel spans
  (`gen_ai.*` semantic conventions). Local file is default-on (zero external
  dependencies); remote OTLP to Langfuse/Phoenix/Datadog is optional and
  concurrent via OTel's native multi-`SpanProcessor` chain. Retains
  `reasoning_content` in traces while Memory layer still strips it. The legacy
  `OperationRecord` / `JsonFileTraceStore` / `operations.jsonl` is phased out via a
  3-phase migration (dual-write → agent reads new → legacy removed).
- **Repro Path (通路 B)** — reproducibility: B1 Checkpoint (default-on, per-iteration
  `TurnSnapshot` via `AfterIterationHook`-driven `CheckpointHook`) and B2 Cassette
  (opt-in, 6-category side-effect capture for bit-identical replay). Linked to Trace
  Path via `trace_id`.
- **Training Data Derivation (派生层)** — training data: write-time tagging
  (`gen_ai.training.relevant` L1 rule filter) + read-time derivation
  (`TrainingDataExporter` aggregates spans by `trace_id` into trajectory-level SFT
  and DPO JSONL). No third write path.

The framework ships with zero external process dependencies (Tier 1: local JSONL
only). Business deployments (bot_project) opt into remote backends (Tier 2:
Langfuse container; Tier 3: + OTel Collector) via YAML config. All new capabilities
are additive — disabling every new switch yields byte-for-byte today's behavior (plus
the stale docstring fix).

Full design rationale: see ADR-0024 (`docs/adr/0024-...`). Domain glossary updates:
see CONTEXT.md (Trace Replay, Checkpoint Re-execution, Deterministic Replay, Input
Replay, Trace Path, Repro Path, Cassette, Training Data Derivation, Trajectory,
Iteration Checkpoint).

## User Stories

### Trace Path — OTel Span Emission

1. As a framework developer, I want `TraceCollectorHook` refactored to emit
   OpenTelemetry spans with `gen_ai.*` semantic conventions
   (`gen_ai.operation.name`, `gen_ai.agent.name`, `gen_ai.request.model`,
   `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`,
   `gen_ai.output.content`, `gen_ai.output.reasoning_content`,
   `gen_ai.output.tool_calls`, `gen_ai.tool.name`, `gen_ai.tool.result`,
   `gen_ai.tool.duration_ms`), so that the trace output is consumable by any
   OTel-compatible tool (Jaeger, Langfuse, Phoenix, Datadog) without format
   translation.

2. As a framework developer, I want each ReAct turn to open an `invoke_agent`
   INTERNAL span (parent) at `before_turn` and close it at `finally_turn`, with
   child `chat` CLIENT spans (one per LLM call) and `execute_tool` INTERNAL spans
   (one per tool call) nested via `parent_span_id`, so that the span tree mirrors
   the ReAct execution structure and an agent reading its own trace can understand
   which LLM call belongs to which iteration.

3. As a framework developer, I want `reasoning_content` recorded as
   `gen_ai.output.reasoning_content` in the `chat` span attributes (not in the
   Memory layer), so that training data derivation has access to the reasoning
   process while the Memory layer's `ChatMessage.to_dict()` stripping remains
   unchanged and context-budget is unaffected.

4. As a framework developer, I want `usage.reasoning_tokens` recorded as a custom
   OTel attribute (because `gen_ai.*` semconv does not yet have a dedicated
   reasoning-tokens field), so that cost analysis accounts for reasoning-token
   billing on o1/o3-class models.

5. As a framework developer, I want the 3 currently-unused `OperationKind` values
   (`APPROVAL` / `CONTROL_COMMAND` / `ERROR`) to gain corresponding OTel span
   emissions — `human.review` span for approval decisions, custom span for control
   commands, error status for failures — so that approval decisions and control
   events are visible in the trace.

6. As a framework developer, I want `gen_ai.*` attribute access isolated behind a
   semconv adapter layer, so that when the OTel GenAI semantic conventions change
   (currently `Development` status), only the adapter needs updating, not every
   emission site.

7. As a framework developer, I want the stale `HookPoint` docstring ("dispatches via
   `getattr(hook, hook_point.value)`") corrected to reflect the actual `isinstance`
   + ABC dispatch, so that contributors are not misled.

### Trace Path — Local File Exporter (Agent Self-Read)

8. As a framework developer, I want a `FileSpanExporter` (standard OTel
   `SpanExporter` subclass) that serializes each span as a JSON line and appends to
   `<workspace>/.modex/runtime_state/<pool>/trace/<session>/spans.jsonl`, so that
   the local trace file is in standard OTel span format and the agent can self-read
   it.

9. As a framework developer, I want `format_send_ack` (`result.py:70-74`) updated to
   point to `spans.jsonl` (Phase 2 of the migration; both paths during Phase 1
   dual-write), so that the main agent's dispatch ack reports the OTel span file
   path for subagent self-reading.

10. As a main agent, I want to read the `spans.jsonl` file of a subagent I
    dispatched, parse the JSON lines, and identify operations by `name` and
    `attributes.gen_ai.*` field names, so that I can follow the subagent's progress
    (which LLM calls it made, which tools it invoked, what results it got) —
    preserving the existing agent-self-read capability after the trace format
    change.

11. As a framework developer, I want the local `FileSpanExporter` to use the
    existing `read_jsonl_robust` helper for resilient reading (skip malformed lines,
    encoding fallback chain), so that partial writes during a crash do not corrupt
    the entire trace file.

12. As a framework developer, I want `trace_backend=OFF` to disable all trace
    emission (no `FileSpanExporter`, no OTLP), so that observability has zero
    overhead when not needed.

### Trace Path — Multi-Exporter Concurrent

13. As a business deployer (bot_project), I want to configure
    `trace_backend: file` (default, local JSONL) plus `otel_endpoint: <url>`
    (optional, remote OTLP HTTP) independently in `bot_config.yml`, so that local
    agent-self-read and remote observability dashboards work simultaneously without
    trade-off — the local file is always written for the agent, and the remote
    backend receives the same spans for ops/algorithm teams.

14. As a business deployer, I want the combination `trace_backend=FILE +
    otel_endpoint=set` to produce concurrent dual-export via OTel's native
    `SpanProcessor` chain (each processor independent: batched, retried, timed-out
    separately), so that one exporter failing does not affect the other.

15. As a business deployer, I want adding a new backend (e.g. Datadog) to require
    only adding an exporter configuration — no framework code change — so that
    backend selection is infrastructure-layer, not application-layer.

16. As a framework developer, I want the OTel SDK (`opentelemetry-sdk`,
    `opentelemetry-exporter-otlp-proto-http`) to be an optional dependency declared
    as an `[observability]` extra in `pyproject.toml`, so that `trace_backend=OFF`
    or `trace_backend=FILE` requires no OTel SDK install and the framework has zero
    external dependencies by default.

17. As a framework developer, I want the framework to raise a clear `ImportError`
    with install instructions when `trace_backend=OTEL_HTTP` is configured but the
    `[observability]` extra is not installed, so that misconfiguration fails fast
    with actionable guidance.

### Trace Path — Subprocess Propagation

18. As a framework developer, I want the parent process (ModexAgent) to open an
    `invoke_agent` CLIENT span when dispatching to an external CLI agent (Pi/OpenCode)
    and inject W3C `traceparent` / `tracestate` into the child subprocess environment
    via `inject(carrier=env, setter=EnvVarSetter())`, so that the child's trace
    context is linked to the parent's.

19. As a framework developer, I want `modexctl send` (the cross-process CLI) to be
    the injection point for `TRACEPARENT` env-var propagation, so that cross-pool
    peer messages carry trace context across process boundaries.

20. As a framework developer, I want the child process (Pi/OpenCode) to extract
    context via `extract(carrier=os.environ, getter=EnvVarGetter())` and open an
    `invoke_agent` INTERNAL span as a child of the parent trace, so that the
    subprocess's internal LLM/tool spans appear as descendants of the parent's
    `invoke_agent` span — linking parent and child into one coherent trace tree.

21. As a framework developer, I want the parent's `invoke_agent` CLIENT span to
    record duration and exit code even if the child is not OTel-instrumented, so
    that the trace is not broken (just shallow on the child side) when the external
    agent lacks instrumentation.

### Repro Path — Checkpoint (B1, Default-On)

22. As a framework developer, I want a `CheckpointHook` that multi-inherits
    `AfterIterationHook` and `SnapshotPolicy` (registered to `HookRunner` via
    `HookSpec`), so that per-iteration `TurnSnapshot` capture is driven by the
    existing `AFTER_ITERATION` dispatch — no graph-engine change, no new hook point.

23. As a framework developer, I want the `CheckpointHook` to produce a regular
    `TurnSnapshot` with `SnapshotReason.ITERATION`, reusing
    `ReActSnapshotPolicy.capture()` and `RuntimeStateCodec.encode_turn()` /
    `decode_turn()`, so that per-iteration snapshots use the same data model and
    storage as existing approval-suspend snapshots.

24. As a framework developer, I want `checkpoint_per_iteration=false` to disable
    per-iteration checkpointing, reverting to only approval-suspend snapshots
    (today's behavior), so that the new capability is opt-out for workspaces that
    do not need intermediate recovery.

25. As a framework developer, I want the `CheckpointHook` to be unregistered by
    default in the factory when `checkpoint_per_iteration=false`, so that the
    `isinstance` check in `HookRunner.dispatch` skips it entirely — zero overhead
    when disabled.

26. As a debugging developer, I want to resume agent execution from iteration N of
    a turn (iterations 1..N-1 read from snapshot deterministically; iteration N+
    re-runs with fresh LLM calls), so that I can reproduce and investigate a bug
    that occurred at a specific ReAct round without re-running the entire turn.

27. As a debugging developer, I want to list the checkpoint history for a turn
    (ordered by iteration), so that I can select which iteration to resume from.

### Repro Path — Cassette (B2, Opt-In)

28. As a framework developer, I want a `CassetteRecorder` that wraps the LLM
    provider client and captures: prompt, full response object, model id, sampling
    parameters, latency, and retry count for every LLM call (category 1), so that
    LLM outputs can be replayed bit-identically without network calls.

29. As a framework developer, I want the `CassetteRecorder` to also wrap the tool
    dispatcher and capture: tool name, input arguments, result text, error, and
    latency for every tool call (category 2), so that tool results can be replayed
    without re-execution.

30. As a framework developer, I want the `CassetteRecorder` to capture retry
    attempts and their backoff delays (category 6), so that retry sequences are
    reproducible.

31. As a framework developer, I want `repro.cassette=true` (default scope) to
    enable categories 1+2+6 (LLM + tools + retries) and `repro.cassette=full` to
    additionally enable categories 3+4+5 (time reads, RNG draws, external reads),
    so that the default scope covers 90% of bug reproduction at moderate overhead
    while full scope provides 100% fidelity for regression testing.

32. As a framework developer, I want the cassette stored as content-addressed files
    under `<workspace>/.modex/cassette/<trace_id>/` with an `index.json` manifest,
    so that cassette data is locally stored, deduplicated by content hash, and
    linked to the OTel trace via `trace_id` (cassette is payload, trace is index).

33. As a framework developer, I want the cassette to store raw data without
    redaction (because redaction breaks replay fidelity), so that the cassette is a
    faithful bit-identical capture — if sharing is needed, tokenization-with-vault
    is the user's responsibility, not the framework's.

34. As a framework developer, I want external CLI agent subprocesses (Pi/OpenCode)
    to be marked `repro.incomplete=true` on the parent's `invoke_agent` CLIENT span,
    so that consumers know the cassette is shallow for subprocess internals
    (uncapturable) and do not assume bit-identical replay for those spans.

35. As a framework developer, I want `repro.cassette=full` to require virtual clock
    and deterministic RNG injection (all `time.time()` / `random.random()` /
    `secrets.token_hex()` calls route through injection points), so that
    time-dependent and random-dependent behavior is reproducible — accepting that
    this is a non-trivial refactor of existing code and is deferred to the full
    scope.

36. As a debugging developer, I want a replay engine that loads a cassette and
    re-executes the agent with all boundaries faked from the cassette (no network
    calls fire), so that I get bit-identical reproduction of a past agent run —
    the only true reproducibility for LLM agents given `temperature=0`
    non-determinism.

### Training Data — Write-Time Tagging (L1)

37. As a framework developer, I want a `TrainingDataHook` (or extension of
    `TraceCollectorHook`) that tags spans with `gen_ai.training.relevant` (true/false)
    at write-time, so that the online hot path has microsecond-cost tagging and no
    format conversion or file IO.

38. As a framework developer, I want the L1 rule filter to set
    `gen_ai.training.relevant=false` when: `TurnPhase.FAILED` or `TurnPhase.CANCELLED`,
    iteration count exceeds a configurable threshold (default 20), or total token
    count exceeds a configurable threshold (default 100000), so that obviously
    garbage trajectories are rejected at the source.

39. As a framework developer, I want `training_relevant=false` (default) to disable
    L1 tagging entirely (no `gen_ai.training.relevant` attribute written), so that
    training-data collection is opt-in and has zero overhead when not needed.

### Training Data — Read-Time Derivation (L2 + L3)

40. As an algorithm engineer, I want a `TrainingDataExporter` (CLI or API) that
    queries traces where `gen_ai.training.relevant=true` within a time range,
    aggregates spans by `trace_id` into trajectories, and converts to SFT OpenAI
    messages JSONL, so that I can use agent execution traces as supervised
    fine-tuning data.

41. As an algorithm engineer, I want the SFT JSONL format to follow the OpenAI
    spec: one `{"messages":[...]}` per line, with `tool_calls` (where
    `function.arguments` is a JSON string, `id` unique per example) and `role:tool`
    results, so that the output is directly consumable by OpenAI fine-tuning, TRL,
    Axolotl, and LLaMA-Factory.

42. As an algorithm engineer, I want `reasoning_content` from the trace wrapped in
    `<think>...</think>` tags in the assistant message content (DeepSeek-R1 /
    OpenThoughts3 format), so that the SFT data captures the reasoning process for
    reasoning-model training (STaR, reasoning distillation).

43. As an algorithm engineer, I want the exporter to also produce an Anthropic
    variant (`tool_use` / `tool_result` content blocks instead of `tool_calls`
    field), so that the training data is consumable by Anthropic fine-tuning
    (Bedrock Claude).

44. As an algorithm engineer, I want the exporter to produce DPO preference-pair
    JSONL (`{prompt, chosen, chosen_model, chosen_rating, rejected, rejected_model,
    rejected_rating}`) from approval data — approved trajectories = chosen, denied
    trajectories = rejected — so that I can train preference models leveraging
    ModexAgent's unique structured approval signal (which most frameworks lack).

45. As an algorithm engineer, I want the DPO pairs filtered by minimum score gap
    (≥0.5) and minimum edit-distance ratio (≥0.1) and refusal filtering (drop
    "I'm sorry / I cannot..." chosen responses), so that trivial pairs and
    low-quality data are excluded.

46. As an algorithm engineer, I want the exporter to apply L2 heuristic scoring
    (tool success rate, reasoning depth, trajectory compactness) to each trajectory
    and include the scores as metadata in the JSONL, so that I can sort and filter
    trajectories by quality without re-computing.

47. As an algorithm engineer, I want an optional L3 LLM-as-judge mode that scores
    trajectories 1-5 via a configurable LLM, applied only to the L1+L2-passed
    subset, so that I get quality annotations at controlled cost.

48. As an algorithm engineer, I want the exporter to apply 3-tier deduplication
    (exact hash → MinHash LSH → semantic embedding cosine) before writing the
    final JSONL, so that duplicated and near-duplicated trajectories do not inflate
    the training set.

49. As an algorithm engineer, I want the exporter to be scope-aware — never
    exporting spans across tenant boundaries without explicit opt-in — so that
    multi-tenant memory scopes (Session/User/Tenant/Agent/Channel/Chat/Composite/
    Global) do not leak data across tenants (the pydantic-ai privacy lesson).

50. As an algorithm engineer, I want the exporter to support multi-granularity
    output: trajectory-level (full turn, primary), iteration-level (single ReAct
    round, auxiliary), and group-level (one LLM+TOOL cycle as a semantic sub-task),
    so that I can produce trajectory/step/group preference pairs from the same
    trace store (HPL paper, ICLR 2026 pattern).

### Configuration

51. As a business deployer, I want the existing `ObservabilityConfig` (currently 10
    lines: `run_logging` + `level`) extended with trace/repro/training fields, so
    that all observability configuration is in one Pydantic model driven by YAML.

52. As a business deployer, I want `trace_backend` to accept `off` / `file` /
    `otel_http` and `otel_endpoint` to be independently settable, so that I can
    choose local-only (default), remote-only, or concurrent local+remote without
    framework code changes.

53. As a business deployer, I want `retain_reasoning_content` (default true) to
    control whether `gen_ai.output.reasoning_content` is written to spans, so that
    I can disable reasoning retention for privacy-sensitive deployments.

54. As a business deployer, I want `checkpoint_per_iteration` (default true) and
    `cassette_enabled` (default false) and `cassette_scope` (`default` / `full`)
    as independent config fields, so that I can enable checkpoint without cassette,
    or cassette-full for regression testing, or neither for minimal overhead.

55. As a business deployer, I want `training_relevant` (default false),
    `training_max_iterations` (default 20), and `training_max_tokens` (default
    100000) as config fields, so that training-data collection thresholds are
    tunable without code changes.

### Migration — Legacy Trace Replacement

56. As a framework developer, I want Phase 1 (dual-write) to add an
    `OtelSpanTraceStore(TraceStore)` that implements `save()` by converting
    `OperationRecord` → OTel span, registered alongside the existing
    `JsonFileTraceStore` in the factory, so that both `operations.jsonl` and
    `spans.jsonl` are written during the transition period.

57. As a framework developer, I want Phase 2 to update `format_send_ack` to point
    to `spans.jsonl` and update agent prompts to read OTel span format, so that
    agents transition to the new format while the legacy file remains as a
    fallback.

58. As a framework developer, I want Phase 3 to refactor `TraceCollectorHook` to
    construct OTel spans directly (drop `OperationRecord` construction), remove
    `JsonFileTraceStore` and `OperationRecord`, and refactor `TraceStore` ABC into
    `TraceQuery` ABC (read-only: `list_by_session` / `list_by_trace_id` over
    `spans.jsonl`), so that the legacy format is fully retired after agent
    comprehension is verified.

59. As a framework developer, I want the `TraceStore` ABC's multi-store deduplicated
    write loop (`hooks.py:104-108`) to continue working during Phase 1, so that
    adding the new store requires no change to `TraceCollectorHook` itself.

### Framework / Business Separation

60. As a framework developer, I want all observability components
    (`FileSpanExporter`, `OtelSpanTraceStore`, `CheckpointHook`,
    `CassetteRecorder`, `TrainingDataExporter`, `TraceQuery`) to live in the
    framework (`src/modex_agent/`), not in `examples/bot_project/`, so that the
    observability layer is reusable across business deployments.

61. As a business deployer, I want the framework to provide the ABCs and default
    implementations while my deployment responsibility is limited to YAML
    configuration and (for Tier 2/3) container deployment, so that there is a
    clean framework/business separation (per AGENTS.md architecture rule 9).

62. As a framework developer, I want the worst case — all new features disabled
    (`trace_backend=OFF`, `checkpoint_per_iteration=false`, `cassette_enabled=false`,
    `training_relevant=false`) — to produce byte-for-byte today's behavior (plus the
    stale docstring fix), so that the new layer is purely additive and cannot
    regress existing functionality.

## Implementation Decisions

### Architecture: Dual-Path + Derivation

Three data paths linked by `trace_id`:

- **Trace Path (A)**: OTel span emission from 5 existing hooks. Samplable,
  redactable, streaming-first. Local file default-on; remote OTLP optional and
  concurrent. Retains `reasoning_content`; Memory layer unchanged.
- **Repro Path (B1)**: Per-iteration `TurnSnapshot` via `AfterIterationHook`-driven
  `CheckpointHook`. Extends existing approval-suspend snapshot mechanism. Backend:
  SQLite `turn_snapshots` table (existing) or JSON file (existing).
- **Repro Path (B2)**: Cassette — 6-category side-effect capture. Default scope:
  LLM + tools + retries (1+2+6). Full scope: + time + RNG + external reads (3+4+5).
  Backend: local content-addressed files under `<ws>/.modex/cassette/<trace_id>/`.
  No redaction (breaks fidelity). External CLI agents marked `repro.incomplete=true`.
- **Training Derivation**: Write-time `gen_ai.training.relevant` tag (L1). Read-time
  `TrainingDataExporter` aggregation by `trace_id` → trajectory SFT/DPO JSONL (L2
  scoring + L3 annotation). No third write path.

The Trace and Repro paths cannot merge: observability wants sampling + redaction +
low cost; reproducibility wants full fidelity + no sampling + no redaction. These
requirements conflict. Training data is a read-time consumer of the Trace Path.

### Module: Trace Emission (Path A)

`TraceCollectorHook` is refactored to use OTel SDK's `Tracer` API
(`tracer.start_as_current_span(...)`) instead of constructing `OperationRecord`.
The 5 hook points map to spans:

- `before_turn` → open `invoke_agent` INTERNAL span (parent)
- `after_llm_response` → close `chat` CLIENT span (fill `gen_ai.*` attributes)
- `before_tool_execution` → open `execute_tool` INTERNAL span
- `after_tool_execution` → close `execute_tool` span (fill result attributes)
- `finally_turn` → close `invoke_agent` span

A semconv adapter module isolates `gen_ai.*` attribute names. When semconv changes
(Development status), only the adapter updates.

`reasoning_content` is recorded as `gen_ai.output.reasoning_content` (Trace layer
only). `usage.reasoning_tokens` is a custom attribute (semconv gap).

### Module: FileSpanExporter (Local File, Default-On)

A standard OTel `SpanExporter` subclass that serializes each span as a JSON line and
appends to `<ws>/.modex/runtime_state/<pool>/trace/<session>/spans.jsonl`. Uses
`read_jsonl_robust` for resilient reading. This is the default exporter when
`trace_backend=FILE`.

The agent self-reads this file. `format_send_ack` reports the `spans.jsonl` path.
OTel span JSONL format is a strict superset of `OperationRecord` information (adds
`parent_span_id` for tree structure, standard `gen_ai.*` attributes). Agents read
JSON by field name — OTel attributes (`gen_ai.output.content`,
`gen_ai.tool.result`) are more self-descriptive than `metadata.*`.

### Module: Multi-Exporter (Concurrent)

OTel SDK's native `SpanProcessor` chain: each processor wraps an exporter and
operates independently (batched, retried, timed-out separately). Configuration:

- `trace_backend=FILE` → `BatchSpanProcessor(FileSpanExporter)` (default-on)
- `otel_endpoint=set` → `BatchSpanProcessor(OTLPSpanExporter)` (optional, concurrent)
- Both set → both processors active (concurrent dual-export)
- `trace_backend=OFF` + `otel_endpoint=None` → no processors (zero overhead)

OTel SDK (`opentelemetry-sdk`, `opentelemetry-exporter-otlp-proto-http`) is an
optional dependency: `[observability]` extra in `pyproject.toml`.
`trace_backend=OFF` or `FILE` requires no OTel SDK install (uses a lightweight
built-in span serializer). `trace_backend=OTEL_HTTP` without the extra raises a
clear `ImportError` with install instructions.

### Module: Subprocess Trace Propagation

W3C `traceparent` / `tracestate` env-var propagation (STABLE in OTel `env-carriers`
spec). Parent opens `invoke_agent` CLIENT span, injects via
`inject(carrier=env, setter=EnvVarSetter())`. `modexctl send` is the injection
point. Child extracts via `extract(carrier=os.environ, getter=EnvVarGetter())` and
opens `invoke_agent` INTERNAL span as child. Child's internal spans appear as
descendants. If child is not instrumented, parent span still records duration +
exit code — trace not broken, just shallow.

### Module: CheckpointHook (Path B1, Default-On)

`CheckpointHook(AfterIterationHook, SnapshotPolicy)` — multi-inherits two ABCs,
registered to `HookRunner` via `HookSpec`. The `AfterIterationHook` ABC already
exists and is already dispatched by `GraphEngine.run` via `_HOOK_DISPATCH`. No
graph-engine change. No new hook point.

Dispatch uses existing `isinstance` + ABC (not `getattr` — the stale `HookPoint`
docstring is corrected). If `CheckpointHook` is unregistered, `HookRunner` skips it
(`isinstance` check fails) — existing behavior unchanged.

Produces regular `TurnSnapshot` with `SnapshotReason.ITERATION`. Reuses
`ReActSnapshotPolicy.capture()` and `RuntimeStateCodec.encode_turn()` / `decode_turn()`.
Re-execution from iteration N: iterations 1..N-1 read from snapshot (deterministic);
iteration N+ re-runs (non-deterministic, fresh LLM calls).

`checkpoint_per_iteration=false` → factory does not register `CheckpointHook` →
only approval-suspend snapshots (today's behavior).

### Module: CassetteRecorder (Path B2, Opt-In)

Wraps LLM provider client (category 1: prompt + full response + model + params +
latency + retries) and tool dispatcher (category 2: name + args + result + error +
latency). Category 6 (retries) captured via retry decorator wrapper. Categories 3+4+5
(time + RNG + external reads) require virtual clock + deterministic RNG injection —
deferred to `cassette_scope=full`.

Cassette stored as content-addressed files under
`<ws>/.modex/cassette/<trace_id>/` with `index.json` manifest. No redaction (breaks
fidelity). External CLI agents marked `repro.incomplete=true` on the parent's
`invoke_agent` CLIENT span.

Replay engine loads cassette, fakes all boundaries (LLM client returns cassette
response, tool dispatcher returns cassette result, no network calls fire).
Bit-identical reproduction — the only true reproducibility for LLM agents.

### Module: TrainingDataHook (L1 Tagging)

Tags spans with `gen_ai.training.relevant` (true/false) at write-time. Rules:
`TurnPhase.FAILED/CANCELLED` → false; iteration count > threshold → false; total
tokens > threshold → false; else → true. One OTel attribute set — microsecond cost.
`training_relevant=false` (default) disables tagging entirely (no attribute written).

### Module: TrainingDataExporter (L2 + L3 Derivation)

Read-time consumer of the Trace Path. Queries traces where
`gen_ai.training.relevant=true`, aggregates spans by `trace_id` into trajectories.
Converts to:

- **SFT OpenAI messages JSONL**: `{"messages":[...]}` per line. `tool_calls` with
  `function.arguments` as JSON string, `id` unique per example. `reasoning_content`
  wrapped in `<think>...</think>` (DeepSeek-R1 format). Anthropic variant
  (`tool_use`/`tool_result` blocks).
- **DPO preference-pair JSONL**: `{prompt, chosen, chosen_model, chosen_rating,
  rejected, rejected_model, rejected_rating}`. Source: approval-as-preference
  (approved=chosen, denied=rejected). Filters: min score gap ≥0.5, min edit-distance
  ratio ≥0.1, refusal filtering.

L2 heuristic scoring: tool success rate, reasoning depth, trajectory compactness.
L3 (opt-in): LLM-as-judge 1-5 scoring on L1+L2-passed subset. 3-tier dedup (exact
hash → MinHash LSH → semantic cosine). Scope-aware export (never cross tenant
boundaries without opt-in).

Multi-granularity: trajectory-level (primary), iteration-level (auxiliary),
group-level (one LLM+TOOL cycle). The 4-node ReAct graph maps to "groups" per HPL
paper (ICLR 2026).

Ports Microsoft Agent Lightning's `group_genai_dict` unflatten utility pattern
(`gen_ai.prompt.N.role` → `{"prompt":[{"role":...}]}`).

### Module: ObservabilityConfig (Extended)

Extends existing `ObservabilityConfig` (currently 10 lines) with:

```python
class TraceBackend(str, Enum):
    OFF = "off"
    FILE = "file"
    OTEL_HTTP = "otel_http"

class CassetteScope(str, Enum):
    DEFAULT = "default"
    FULL = "full"

class ObservabilityConfig(BaseModel):
    # Existing (retained)
    run_logging: bool = True
    level: str = "INFO"
    # Trace Path (A)
    trace_backend: TraceBackend = TraceBackend.FILE
    otel_endpoint: str | None = None
    otel_service_name: str = "modex_agent"
    retain_reasoning_content: bool = True
    # Repro Path (B1/B2)
    checkpoint_per_iteration: bool = True
    cassette_enabled: bool = False
    cassette_scope: CassetteScope = CassetteScope.DEFAULT
    # Training Data Derivation
    training_relevant: bool = False
    training_max_iterations: int = 20
    training_max_tokens: int = 100000
```

Combinations:
- `trace_backend=FILE + otel_endpoint=None` → local only (default)
- `trace_backend=FILE + otel_endpoint=set` → local + remote (concurrent)
- `trace_backend=OTEL_HTTP + otel_endpoint=set` → remote only
- `trace_backend=OFF + otel_endpoint=None` → fully off

### Migration: 3-Phase Legacy Replacement

- **Phase 1 (dual-write)**: Add `OtelSpanTraceStore(TraceStore)` implementing
  `save()` by converting `OperationRecord` → OTel span. Both stores registered in
  factory. `TraceCollectorHook` unchanged. Both `operations.jsonl` and `spans.jsonl`
  written.
- **Phase 2 (agent reads new)**: `format_send_ack` points to `spans.jsonl`. Agent
  prompts updated to read OTel span format. Verify agent comprehension.
- **Phase 3 (legacy removed)**: `TraceCollectorHook` refactored to construct OTel
  spans directly (drop `OperationRecord`). `JsonFileTraceStore` and `OperationRecord`
  removed. `TraceStore` ABC refactored to `TraceQuery` ABC (read-only:
  `list_by_session` / `list_by_trace_id` over `spans.jsonl`).

### Stale Docstring Fix

`HookPoint` docstring ("dispatches via `getattr(hook, hook_point.value)`") corrected
to reflect `isinstance` + ABC dispatch. Low cost, done alongside Phase 3.

## Testing Decisions

### Test Philosophy

Only test external behavior, not implementation details. A good test verifies what
the system *does* (observable outputs: file contents, span attributes, exported
JSONL schemas), not how it does it (internal class structure, dispatch mechanism).
Existing seams are preferred to new ones. The fewer seams across the codebase, the
better.

### Seam 1: ReAct Turn Harness (Existing, Extended)

**Level**: Integration

**What it covers**: The entire observability data flow end-to-end through the
existing ReAct engine — trace emission → per-iteration checkpoint → training data
export. Single-flow errors surface here.

**How it works**: Run a full ReAct turn with a mock LLM provider and mock tools
(the existing `tests/unit/agents/react/` harness pattern). After the turn completes,
assert:

- `spans.jsonl` exists at the expected path and contains spans with correct
  `gen_ai.*` attributes, `parent_span_id` tree structure (invoke_agent → chat /
  execute_tool), and `reasoning_content` when the mock LLM provides it.
- Per-iteration `TurnSnapshot` records exist in the `TurnStateStore` with
  `SnapshotReason.ITERATION`, one per ReAct round, round-trippable via
  `RuntimeStateCodec.encode_turn()` / `decode_turn()`.
- `format_send_ack` reports the `spans.jsonl` path.
- `TrainingDataExporter` produces valid OpenAI messages JSONL
  (`{"messages":[...]}` per line, `tool_calls.function.arguments` as JSON string)
  from the turn's traces.
- `gen_ai.training.relevant` is tagged correctly (false for failed turns, true for
  successful turns within thresholds).
- DPO pairs produced from approval data (approved=chosen, denied=rejected).
- `trace_backend=OFF` produces no `spans.jsonl` and no overhead.
- `checkpoint_per_iteration=false` produces no `SnapshotReason.ITERATION` records.
- W3C `traceparent` is present in subprocess env when dispatching to external CLI
  agents.

**Prior art**: `tests/unit/agents/react/` — existing ReAct turn harness with mock
LLM. `tests/conformance/test_turn_state_store_conformance.py` — `TurnStateStore`
round-trip conformance. `tests/unit/hook/test_runner_propagate_control_error.py` —
`HookRunner` dispatch behavior.

### Seam 2: CassetteRecorder Unit Test (New)

**Level**: Unit

**What it covers**: L4 deterministic replay fidelity — the property that "replay
produces bit-identical results with no network calls." This property cannot be
verified at the ReAct harness level because the harness's mock LLM is itself
"no-network," which masks whether the cassette is actually enforcing replay
(isolation is needed to prove the cassette wrapper, not the mock, is the source of
replay fidelity).

**How it works**: Wrap a mock LLM provider with `CassetteRecorder`. Record a
call (prompt → response). Replay the cassette with a *different* mock provider
configured to raise if called (asserts no network). Verify the replayed response is
bit-identical to the recorded response. Repeat for tool dispatcher (record tool
call → replay → assert bit-identical result, no tool re-execution).

Assert cassette file structure: content-addressed files exist under
`<ws>/.modex/cassette/<trace_id>/`, `index.json` manifest is valid, `trace_id`
links to the OTel trace.

Assert `repro.incomplete=true` on `invoke_agent` CLIENT span when dispatching to
external CLI subprocess.

**Prior art**: `tests/unit/providers/` — provider test patterns.
`tests/unit/tools/` — tool executor test patterns.

### What is NOT a separate seam

- `FileSpanExporter` — covered by Seam 1 (`spans.jsonl` content verified)
- `CheckpointHook` dispatch — covered by Seam 1 (checkpoint records verified via
  existing `TurnStateStore` conformance + turn harness)
- `TrainingDataExporter` format compliance — covered by Seam 1 (exported JSONL
  validated against expected schema)
- Semconv adapter — covered by Seam 1 (span attributes verified)
- `ObservabilityConfig` parsing — covered by Seam 1 (config-driven behavior
  verified: `trace_backend=OFF` produces no file, etc.)
- W3C traceparent propagation — covered by Seam 1 (subprocess env verified)

## Out of Scope

- **OTel Collector deployment** — Tier 3 (Collector container) is a business
  deployment concern, not a framework deliverable. The framework emits standard
  OTLP; Collector configuration is the user's responsibility.
- **Langfuse/Phoenix/Datadog deployment** — Tier 2/3 backend deployment is
  business infrastructure. The framework provides the exporter; container
  orchestration is out of scope.
- **Cassette full-scope virtual clock/RNG injection refactor** — `cassette_scope=full`
  requires routing all `time.time()` / `random.random()` / `secrets.token_hex()`
  calls through injection points. This is a non-trivial refactor of existing code
  and is deferred to a follow-up. The default scope (1+2+6) is in scope.
- **External CLI agent (Pi/OpenCode) internal instrumentation** — Subprocess
  internals are uncapturable. The parent's `invoke_agent` span + `traceparent`
  propagation is in scope; instrumenting the external agents themselves is out of
  scope (they are separate projects).
- **Data migration from `operations.jsonl` to `spans.jsonl`** — Phase 1 dual-write
  writes both; Phase 3 stops writing legacy. No conversion of existing files is
  performed. Old `operations.jsonl` files remain readable but are not migrated.
- **Real-time streaming observation (SSE/WebSocket dashboard)** — The framework
  emits spans; real-time streaming to a dashboard is a backend feature (Langfuse
  provides this). The framework does not implement its own streaming observer.
- **Eval/scoring platform** — L3 LLM-as-judge scoring is a minimal built-in. Full
  eval platform (dataset management, experiment tracking, A/B comparison UI) is a
  backend feature (Langfuse/Phoenix provides this), not a framework deliverable.
- **Agent prompt updates for OTel span format** — Phase 2 requires verifying that
  agents can read OTel span format. The prompt updates themselves are a business
  concern (bot_project), not a framework deliverable. The framework provides the
  format; prompt engineering is the user's responsibility.
- **OTel GenAI semconv stabilization** — The semconv is `Development` status. The
  adapter layer isolates changes. Tracking and contributing to semconv upstream is
  out of scope.

## Further Notes

- **ADR-0024** (`docs/adr/0024-agent-observability-reproducibility-and-training-data.md`)
  contains the full design rationale with 12 decisions (D1-D12), consequences, and
  industry research references. This PRD is the implementation spec; the ADR is the
  design record.
- **CONTEXT.md** glossary has been updated with 11 new terms (Trace Replay,
  Checkpoint Re-execution, Deterministic Replay, Input Replay, Trace Path, Repro
  Path, Cassette, Training Data Derivation, Trajectory, Iteration Checkpoint) and 5
  new relationship statements. Use this vocabulary throughout implementation.
- **Industry validation**: Microsoft Agent Lightning
  (`github.com/microsoft/agent-lightning`) already implements OTel-spans-to-
  training-data adapters in production (`TraceToMessages`, `TraceToTriplet`). Its
  `group_genai_dict` unflatten utility pattern (`gen_ai.prompt.N.role` →
  `{"prompt":[{"role":...}]}`) is directly portable. The HPL paper (ICLR 2026)
  validates multi-granularity preference pairs from the same trace store, with the
  4-node ReAct graph mapping to "groups." The pydantic-ai privacy lesson (issue
  #2202) informs the scope-aware export requirement.
- **Deployment tiers**: Tier 1 (framework default, zero process) — local
  `FileSpanExporter` → `spans.jsonl`. Tier 2 (business opt-in) — + OTLP HTTP →
  Langfuse container. Tier 3 (full production) — + OTel Collector → multi-backend.
  The framework implements Tier 1; Tiers 2/3 are business deployment concerns.
- **Worst-case guarantee**: All new features disabled
  (`trace_backend=OFF`, `checkpoint_per_iteration=false`, `cassette_enabled=false`,
  `training_relevant=false`) yields byte-for-byte today's behavior (plus the stale
  docstring fix). The new layer is purely additive.
