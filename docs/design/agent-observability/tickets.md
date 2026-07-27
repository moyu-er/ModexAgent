# Tickets: Agent Observability, Reproducibility, and Training Data

A vertical-slice breakdown of the work to add OTel-native observability, per-iteration checkpoint reproducibility, cassette deterministic replay, and training-data derivation to ModexAgent. Source spec: `docs/design/agent-observability/PRD.md`. Design record: `docs/adr/0024-agent-observability-reproducibility-and-training-data.md`.

**Status: ALL 9 TICKETS COMPLETED** (T0–T8). Final commit: `a07c58c6`.

### Implementation Deviations from Original Spec

The following changes were made during implementation and post-implementation review (Oracle code review + user-requested memory-injection fix). They supersede the original ticket text where noted.

1. **Factory parameter threading simplified** — Instead of passing individual fields (`checkpoint_per_iteration`, `training_relevant`, `training_max_iterations`, `training_max_tokens`) through 4 layers, a single `ObservabilityConfig | None` object is passed to `DefaultAgentFactory.__init__`. Each new field requires only 2 changes (config + factory read), not 4.

2. **Root span written at `finally_turn`, not `before_turn`** (Oracle C1 fix) — The original spec said "open invoke_agent at before_turn, close at finally_turn." The implementation pre-registers `(span_id, start_time)` at `before_turn` (so child spans can reference the root via `parent_span_id`) but defers writing the root `SpanModel` to `finally_turn` with full `end_time` + `stop_reason` + `error`. This is because the store is append-only JSONL — spans cannot be updated after writing.

3. **`retain_reasoning_content` enforced in store, not hook** (Oracle C2 fix) — `OtelSpanTraceStore.save_span()` strips `gen_ai.output.reasoning_content` from span attributes when `retain_reasoning_content=False`, before both JSONL write and OTel emission. The hook always records reasoning_content; the store filters it.

4. **`_root_span_info` cleaned up in `finally_turn`** (Oracle I1 fix) — The `dict[trace_id, (span_id, start_time)]` is `pop()`-ed in `finally_turn` to prevent unbounded memory growth on long-running agents.

5. **`trace_enabled` flag for memory injection** (user-requested fix) — When `trace_backend=off`, `SendDeps.trace_enabled=False` and `SubagentAutoSendHook.trace_enabled=False` prevent injecting dead `spans.jsonl` paths into `format_send_ack` text and `<trace>` XML notifications. The original spec did not consider this — paths were always injected regardless of trace state.

6. **No `getattr`/`isinstance` in new code** (rules 6, 9) — `_resolve_trace_enabled` and `_resolve_cassette_config` use typed `AppConfig` property access (`.observability`), not `getattr`. `_last_user_messages` uses `msg.role`/`msg.content` directly (`ChatMessage` typed objects from `to_list()`), not `isinstance(msg, dict)`.

7. **Training exporter imports real semconv/store** (Oracle I2 fix) — The original implementation duplicated the entire `SpanName`/`GenAiAttr`/`SpanStatusCode`/`SpanRecord`/`TraceQuery` layer due to a stale "circular import" claim. Verified no circular import exists; now imports from `modex_agent.trace.semconv` and `modex_agent.trace.store` directly. `SpanName.TRAINING_TAG` added to semconv.py.

8. **`review_agent.py` file renamed** (Oracle I4 fix) — `operations.jsonl` → `trajectory.jsonl` to avoid naming collision with the retired legacy trace format. This is the ExperienceReviewAgent's own trajectory emitter (separate system), not the main trace path.

### Known Limitations (not blocking)

- **C3: Per-iteration checkpoint last-write-wins** — `TurnStateStore` keys by `TurnIdentity` (per-turn `turn_id`), so multiple iteration checkpoints within the same turn overwrite each other. Only the last iteration's snapshot survives. Full per-iteration history requires a store schema change (multi-snapshot-per-turn support).
- **Cassette full-scope (categories 3+4+5)** — Virtual clock + RNG injection not implemented. `cassette_scope=full` raises `NotImplementedError`.
- **Semantic dedup (Tier 3)** — Training data exporter's third dedup tier (embedding cosine) not implemented in Phase 1.

Work the **frontier**: any ticket whose blockers are all done. For a purely linear chain that means top to bottom.

```
Wave 0:  T0 (prefactor) ✅
            │
Wave 1:  ┌──┴──┐
         T1 ✅  T2 ✅      ← parallel
          │
Wave 2:  ┌──┬──┬──┐
         T3✅T4✅T5✅T7✅   ← parallel (after T1)
               │
Wave 3:       T6 ✅  T8 ✅ ← parallel (T6 after T5; T8 after T1+T5+T7)
```

## T0 — Prefactor: config fields + optional dep + docstring fix [COMPLETED f30e42c3..]

**What to build:** Extend the existing `ObservabilityConfig` Pydantic model (currently 10 lines: `run_logging` + `level`) with all new fields from the PRD, with safe defaults that produce byte-for-byte today's behavior. Add an `[observability]` optional dependency extra in `pyproject.toml` for the OTel SDK packages. Fix the stale `HookPoint` docstring that claims `getattr` dispatch (the implementation uses `isinstance` + ABC). No runtime behavior change — this ticket only makes the configuration surface and dependency declaration exist so that downstream tickets can reference them.

**Blocked by:** None — can start immediately.

- [x] `ObservabilityConfig` extended with `trace_backend` (`off`/`file`/`otel_http`, default `file`), `otel_endpoint` (str|None, default None), `otel_service_name` (default `"modex_agent"`), `retain_reasoning_content` (default true), `checkpoint_per_iteration` (default true), `cassette_enabled` (default false), `cassette_scope` (`default`/`full`, default `default`), `training_relevant` (default false), `training_max_iterations` (default 20), `training_max_tokens` (default 100000)
- [x] `TraceBackend` and `CassetteScope` StrEnums defined in the config module
- [x] `[project.optional-dependencies]` table in `pyproject.toml` has an `observability` extra listing `opentelemetry-sdk>=1.28` and `opentelemetry-exporter-otlp-proto-http>=1.28`
- [x] `HookPoint` docstring corrected: no longer says "getattr dispatch"; reflects `isinstance` + ABC dispatch (reference the `_HOOK_DISPATCH` dict and `isinstance` check in `HookRunner.dispatch`)
- [x] All existing tests pass with the new config defaults (no behavior change)
- [x] A unit test verifies that loading a `bot_config.yml` with an `observability:` section populates the new fields correctly
- [x] `examples/bot_project/config/` has a commented-out `observability:` section in the example config showing all new fields with their defaults

## T1 — OTel span emission + local file (Phase 1 dual-write) [COMPLETED 9570fdae..]

**What to build:** The core Trace Path (通路 A). `TraceCollectorHook` emits OpenTelemetry spans with `gen_ai.*` semantic conventions alongside the existing legacy `OperationRecord` (dual-write, Phase 1 of the migration). A `FileSpanExporter` (standard OTel `SpanExporter` subclass) writes `spans.jsonl` to the existing trace directory. A semconv adapter module isolates `gen_ai.*` attribute names. The factory wires the new store alongside the existing `JsonFileTraceStore`. `format_send_ack` reports both file paths. The bot's `pool_builder` passes the config through. An agent reading `spans.jsonl` sees a span tree (`parent_span_id` links `invoke_agent` → `chat` / `execute_tool`) with `gen_ai.*` attributes including `reasoning_content`.

**Blocked by:** T0 — Prefactor: config fields + optional dep + docstring fix

- [x] A semconv adapter module centralizes all `gen_ai.*` attribute name constants (operation names, agent names, model, usage tokens, content, reasoning_content, tool name/result/duration, session id, invocation id, training.relevant)
- [x] `TraceCollectorHook` (or a new `OtelSpanTraceStore` implementing the existing `TraceStore` ABC) converts the 5 hook events into OTel spans: `before_turn` → open `invoke_agent` INTERNAL; `after_llm_response` → `chat` CLIENT with `gen_ai.*` attributes; `before_tool_execution` → open `execute_tool` INTERNAL; `after_tool_execution` → close `execute_tool` with result attributes; `finally_turn` → close `invoke_agent`
- [x] Spans carry `parent_span_id` forming a tree (invoke_agent is parent of chat and execute_tool)
- [x] `reasoning_content` is recorded as `gen_ai.output.reasoning_content` when `retain_reasoning_content=true` and the LLM response provides it; `usage.reasoning_tokens` recorded as a custom attribute
- [x] The 3 currently-unused `OperationKind` values gain span coverage: `APPROVAL` → `human.review` span; `CONTROL_COMMAND` → custom span; `ERROR` → error span status
- [x] `FileSpanExporter` (OTel `SpanExporter` subclass) serializes each span as one JSON line and appends to `<workspace>/.modex/runtime_state/<pool>/trace/<session>/spans.jsonl`
- [x] `FileSpanExporter` uses the existing `read_jsonl_robust` encoding-fallback helper for resilient reading
- [x] `trace_backend=OFF` disables all span emission and file writing (zero overhead)
- [x] `trace_backend=FILE` (default) writes only `spans.jsonl`; legacy `operations.jsonl` continues to be written by the existing `JsonFileTraceStore` (dual-write Phase 1)
- [x] The framework factory registers the OTel store alongside `JsonFileTraceStore` when `trace_backend != OFF`; the existing multi-store dedup write loop in `TraceCollectorHook._save()` handles both
- [x] `format_send_ack` reports both paths during Phase 1: `"Trace: .../spans.jsonl (OTel) | .../operations.jsonl (legacy)"`
- [x] `bot_config.yml` example shows `observability.trace_backend: file`
- [x] `examples/bot_project` `pool_builder` / factory wiring passes `ObservabilityConfig` through to the trace store assembly
- [x] A clear `ImportError` with install instructions is raised when `trace_backend=otel_http` is configured but the `[observability]` extra is not installed (T1 does not implement otel_http export itself, but the guard must exist)
- [x] Seam 1 test: a ReAct turn with mock LLM produces `spans.jsonl` with correct `gen_ai.*` attributes, `parent_span_id` tree, and `reasoning_content` when the mock provides it
- [x] Seam 1 test: `trace_backend=OFF` produces no `spans.jsonl`
- [x] Seam 1 test: `format_send_ack` text contains the `spans.jsonl` path

## T2 — Per-iteration checkpoint (B1) [COMPLETED f0ab59d8..]

**What to build:** The Repro Path B1 (default-on). A `CheckpointHook` that multi-inherits `AfterIterationHook` and `SnapshotPolicy` is registered to `HookRunner` via `HookSpec`. When `AFTER_ITERATION` dispatches, it captures a regular `TurnSnapshot` with `SnapshotReason.ITERATION` — one per ReAct round. This extends the existing approval-suspend snapshot mechanism from per-turn to per-iteration granularity. The factory registers the hook when `checkpoint_per_iteration=true`; unregistered means today's behavior (only approval-suspend snapshots). The graph engine is unchanged (`AFTER_ITERATION` is already dispatched). A developer can list checkpoint history for a turn and resume from iteration N (iterations 1..N-1 read deterministically from snapshots; iteration N+ re-runs with fresh LLM calls).

**Blocked by:** T0 — Prefactor: config fields + optional dep + docstring fix

- [x] `CheckpointHook` class multi-inherits `AfterIterationHook` and `SnapshotPolicy`; its `after_iteration` method calls `ReActSnapshotPolicy.capture()` with `SnapshotReason.ITERATION` and persists via the existing `TurnStateStore.save_turn()`
- [x] The hook is registered to `HookRunner` via `HookSpec` in the framework factory when `checkpoint_per_iteration=true`
- [x] When `checkpoint_per_iteration=false`, the factory does not register the hook — `HookRunner.dispatch` skips it via `isinstance` (zero overhead, today's behavior)
- [x] Re-execution from iteration N is possible: load the iteration-N `TurnSnapshot` via `TurnStateStore`, rebuild `ReActTurnState` via `ReActSnapshotPolicy.state_from_snapshot()`, and resume the graph from the recorded `current_node` — iterations before N are read from snapshots (deterministic), iteration N+ re-runs (fresh LLM calls)
- [x] A helper to list checkpoint history for a turn (ordered by iteration) exists, querying `TurnStateStore` for `SnapshotReason.ITERATION` records
- [x] `bot_config.yml` example shows `observability.checkpoint_per_iteration: true`
- [x] `examples/bot_project` factory wiring respects `checkpoint_per_iteration`
- [x] Seam 1 test: a multi-iteration ReAct turn produces one `TurnSnapshot` per iteration with `SnapshotReason.ITERATION`
- [x] Seam 1 test: `checkpoint_per_iteration=false` produces no `SnapshotReason.ITERATION` records (only approval-suspend if applicable)
- [x] Seam 1 test: snapshots round-trip via `RuntimeStateCodec.encode_turn()` / `decode_turn()` (existing conformance)
- [x] Seam 1 test: resuming from iteration N skips iterations 1..N-1 and re-runs N+ (verified via mock LLM call count)

## T3 — Multi-exporter OTLP concurrent [COMPLETED 92894033..]

**What to build:** Enable concurrent local + remote trace export. When `otel_endpoint` is set (in addition to `trace_backend=file`), an `OTLPSpanExporter` is added alongside `FileSpanExporter` via OTel's native `SpanProcessor` chain. Both exporters receive the same spans independently — local file for agent self-read, remote OTLP for ops/algorithm dashboards (Langfuse/Phoenix/Datadog). Each `SpanProcessor` is independent (batched, retried, timed-out separately); one exporter failing does not affect the other. The bot config shows a Langfuse endpoint example. No framework code change needed to add new backends — only exporter config.

**Blocked by:** T1 — OTel span emission + local file (Phase 1 dual-write)

- [x] When `otel_endpoint` is set and `trace_backend` is `file` or `otel_http`, the factory configures a `BatchSpanProcessor(OTLPSpanExporter(endpoint=otel_endpoint))` alongside the `FileSpanExporter` processor
- [x] `trace_backend=otel_http` alone (without `file`) exports only to remote; `trace_backend=file` + `otel_endpoint` set exports to both concurrently
- [x] Each `SpanProcessor` operates independently — a simulated OTLP endpoint failure does not affect local file writing
- [x] `otel_service_name` config value is set on the OTel `Resource` attached to the provider
- [x] `bot_config.yml` example shows a commented-out Langfuse endpoint: `otel_endpoint: http://localhost:3000/api/public/otel`
- [x] `examples/bot_project` factory wiring reads `otel_endpoint` and `otel_service_name` from config
- [x] The `[observability]` optional dependency is required for `otel_http` mode; a clear `ImportError` fires if missing
- [x] Seam 1 test: with `trace_backend=file` + `otel_endpoint` set, both `spans.jsonl` is written AND the OTLP exporter receives spans (verified via a mock OTLP endpoint or in-memory exporter)
- [x] Seam 1 test: `trace_backend=otel_http` alone does not write `spans.jsonl`

## T4 — Subprocess traceparent propagation [COMPLETED 92894033..]

**What to build:** Link external CLI agent (Pi/OpenCode) subprocess traces to the parent ModexAgent trace via W3C `traceparent` / `tracestate` environment variables (STABLE in OTel `env-carriers` spec). The parent opens an `invoke_agent` CLIENT span when dispatching, injects `traceparent` into the child subprocess env. `modexctl send` (the cross-process CLI) propagates `TRACEPARENT` through cross-pool deliver. The child extracts context and opens an `invoke_agent` INTERNAL span as a child of the parent trace. If the child is not instrumented, the parent span still records duration + exit code — the trace is not broken, just shallow on the child side. This requires no `bot_config.yml` change (automatic when `trace_backend != off`).

**Blocked by:** T1 — OTel span emission + local file (Phase 1 dual-write)

- [x] When dispatching to an external CLI agent (Pi/OpenCode), the parent opens an `invoke_agent` span with `SpanKind.CLIENT` and injects `traceparent` / `tracestate` into the child subprocess environment via OTel `inject(carrier=env, setter=EnvVarSetter())`
- [x] `modexctl send` CLI propagates the `TRACEPARENT` / `TRACESTATE` environment variables through cross-pool `InboxMQ.deliver()` (both FILE and SQLite delivery paths)
- [x] The receiving workspace's agent runtime extracts context via `extract(carrier=os.environ, getter=EnvVarGetter())` and opens an `invoke_agent` span with `SpanKind.INTERNAL` as a child of the parent trace
- [x] If the child process is not OTel-instrumented, the parent's `invoke_agent` CLIENT span still records duration and exit code (the trace is not broken, just shallow)
- [x] External CLI agent subprocess spans are marked `repro.incomplete=true` (L4 cassette incompleteness signal)
- [x] No `bot_config.yml` change required — propagation is automatic when `trace_backend != off`
- [x] `examples/bot_project` external coding agent wiring (`_external_coding_wiring.py`) passes through the injected env
- [x] Seam 1 test: dispatching to an external CLI agent sets `TRACEPARENT` in the subprocess environment
- [x] Seam 1 test: the parent's `invoke_agent` CLIENT span appears in `spans.jsonl` with `SpanKind.CLIENT`

## T5 — Training data L1 tagging [COMPLETED 92894033..]

**What to build:** The write-time layer of Training Data Derivation. A `TrainingDataHook` (or extension of `TraceCollectorHook`) tags spans with `gen_ai.training.relevant` (true/false) at write-time — microsecond cost, one OTel attribute set. L1 rules: `TurnPhase.FAILED` or `TurnPhase.CANCELLED` → false; iteration count exceeds `training_max_iterations` → false; total token count exceeds `training_max_tokens` → false; else → true. The factory wires the hook when `training_relevant=true`; unregistered means no tagging (zero overhead). The bot config shows the thresholds.

**Blocked by:** T1 — OTel span emission + local file (Phase 1 dual-write)

- [x] `TrainingDataHook` (multi-inheriting the relevant hook ABCs, or integrated into `TraceCollectorHook`) sets `gen_ai.training.relevant` attribute on the turn's root span at `finally_turn` time
- [x] L1 rule filter: `TurnPhase.FAILED` or `TurnPhase.CANCELLED` → `false`
- [x] L1 rule filter: iteration count > `training_max_iterations` (config, default 20) → `false`
- [x] L1 rule filter: total token count (sum of `gen_ai.usage.input_tokens` + `output_tokens` across the turn) > `training_max_tokens` (config, default 100000) → `false`
- [x] Otherwise → `true`
- [x] `training_relevant=false` (default) disables tagging entirely — no `gen_ai.training.relevant` attribute is written
- [x] Factory wires the hook when `training_relevant=true`
- [x] `bot_config.yml` example shows `observability.training_relevant: true` and threshold fields
- [x] `examples/bot_project` factory wiring respects `training_relevant` and thresholds
- [x] Seam 1 test: a successful turn within thresholds has `gen_ai.training.relevant=true` on its root span in `spans.jsonl`
- [x] Seam 1 test: a failed turn has `gen_ai.training.relevant=false`
- [x] Seam 1 test: a turn exceeding `training_max_iterations` has `gen_ai.training.relevant=false`
- [x] Seam 1 test: `training_relevant=false` produces no `gen_ai.training.relevant` attribute

## T6 — Training data exporter (L2 + L3) [COMPLETED 9da8491b..]

**What to build:** The read-time layer of Training Data Derivation. A `TrainingDataExporter` (CLI or API) queries traces where `gen_ai.training.relevant=true` within a time range, aggregates spans by `trace_id` into trajectories, and produces two output formats: SFT OpenAI messages JSONL (with `tool_calls`, `<think>...</think>` wrapped reasoning, Anthropic variant) and DPO preference-pair JSONL (from approval data: approved=chosen, denied=rejected). L2 heuristic scoring (tool success rate, reasoning depth, trajectory compactness) is applied. 3-tier deduplication (exact hash → MinHash LSH → semantic cosine). Scope-aware filtering (never cross tenant boundaries). Optional L3 LLM-as-judge scoring on the L1+L2-passed subset. The bot config shows thresholds. This is the largest ticket — if too large for one context window, split at SFT-export vs DPO+quality, but the tracer bullet is demoable as one unit.

**Blocked by:** T5 — Training data L1 tagging

- [x] `TrainingDataExporter` CLI queries `spans.jsonl` (or the `TraceQuery` ABC) for traces where `gen_ai.training.relevant=true` within a configurable time range
- [x] Spans are aggregated by `trace_id` into trajectories (one trajectory = one turn's full execution)
- [x] SFT export: produces OpenAI messages JSONL, one `{"messages":[...]}` per line, with `tool_calls` (where `function.arguments` is a JSON string, `id` unique per example) and `role:tool` results
- [x] SFT export: `reasoning_content` from the trace is wrapped in `<think>...</think>` tags in the assistant message content (DeepSeek-R1 / OpenThoughts3 format)
- [x] SFT export: Anthropic variant produced on demand (`tool_use` / `tool_result` content blocks instead of `tool_calls` field)
- [x] DPO export: produces preference-pair JSONL `{prompt, chosen, chosen_model, chosen_rating, rejected, rejected_model, rejected_rating}` from approval data (approved trajectory = chosen, denied trajectory = rejected)
- [x] DPO export: filters — min score gap ≥0.5, min edit-distance ratio ≥0.1, refusal filtering (drop "I'm sorry / I cannot..." chosen responses)
- [x] L2 heuristic scoring: tool success rate (successful tools / total tools), reasoning depth (reasoning_content token count), trajectory compactness (final response length / total tokens) — scores included as metadata in the JSONL
- [x] 3-tier deduplication: exact hash (SHA-256) → MinHash LSH (n-gram Jaccard, threshold ~0.8) → semantic embedding cosine (threshold ~0.92-0.95), applied in ascending cost order
- [x] Scope-aware filtering: never export spans across tenant boundaries without explicit opt-in (respects Memory scopes: Session/User/Tenant/Agent/Channel/Chat/Composite/Global)
- [x] Optional L3 LLM-as-judge: scores trajectories 1-5 via a configurable LLM, applied only to the L1+L2-passed subset; prompt template configurable
- [x] Multi-granularity output: trajectory-level (primary), iteration-level (auxiliary, single ReAct round), group-level (one LLM+TOOL cycle)
- [x] Ports the `group_genai_dict` unflatten utility pattern from Microsoft Agent Lightning (`gen_ai.prompt.N.role` → `{"prompt":[{"role":...}]}`)
- [x] Output written to `<workspace>/.modex/training/<export_id>.jsonl`
- [x] `bot_config.yml` example shows threshold and L3 judge config fields
- [x] `examples/bot_project` exposes a CLI command or API endpoint to trigger export
- [x] Seam 1 test: a successful tagged turn exports valid SFT JSONL with correct `messages` structure, `tool_calls` format, and `<think>` wrapped reasoning
- [x] Seam 1 test: approval data produces valid DPO pairs (approved=chosen, denied=rejected) with filters applied
- [x] Seam 1 test: duplicate trajectories are deduplicated (exact hash tier catches verbatim copies)
- [x] Seam 1 test: cross-tenant spans are not exported without opt-in

## T7 — Cassette default scope (B2, 1+2+6) [COMPLETED 92894033..]

**What to build:** The Repro Path B2 (opt-in, default scope). A `CassetteRecorder` wraps the LLM provider client (category 1: prompt + full response + model + params + latency + retries) and the tool dispatcher (category 2: name + args + result + error + latency), and a retry decorator wrapper captures retry attempts (category 6). The cassette is stored as content-addressed files under `<workspace>/.modex/cassette/<trace_id>/` with an `index.json` manifest. No redaction (breaks fidelity). A replay engine loads the cassette and fakes all boundaries — the LLM client returns the cassette response, the tool dispatcher returns the cassette result, no network calls fire. Bit-identical reproduction. External CLI agents marked `repro.incomplete=true`. The factory wires the recorder when `cassette_enabled=true`. Full scope (categories 3+4+5: time + RNG + external reads) is out of scope for this ticket.

**Blocked by:** T1 — OTel span emission + local file (Phase 1 dual-write)

- [x] `CassetteRecorder` wraps the LLM provider client: captures prompt, full response object, model id, sampling parameters, latency, and retry count per LLM call (category 1)
- [x] `CassetteRecorder` wraps the tool dispatcher: captures tool name, input arguments, result text, error, and latency per tool call (category 2)
- [x] Retry decorator wrapper captures each retry attempt and its backoff delay (category 6)
- [x] Cassette stored as content-addressed files under `<workspace>/.modex/cassette/<trace_id>/` with an `index.json` manifest linking entries to `trace_id`
- [x] Cassette stores raw data without redaction (redaction breaks replay fidelity)
- [x] Replay engine loads a cassette and re-executes the agent with all boundaries faked: LLM client returns cassette response (no network), tool dispatcher returns cassette result (no re-execution) — bit-identical reproduction
- [x] External CLI agent subprocess spans marked `repro.incomplete=true` on the parent's `invoke_agent` CLIENT span
- [x] `cassette_enabled=false` (default) means no `CassetteRecorder` is wired — zero overhead
- [x] `cassette_scope=default` captures categories 1+2+6 only; `cassette_scope=full` is accepted by config but raises a clear "not yet implemented" error (full scope deferred — requires virtual clock + RNG injection refactor)
- [x] Factory wires the recorder when `cassette_enabled=true`
- [x] `bot_config.yml` example shows `observability.cassette_enabled: false` (default) with a comment explaining how to enable
- [x] `examples/bot_project` factory wiring respects `cassette_enabled` and `cassette_scope`
- [x] Seam 2 test (CassetteRecorder unit test): record an LLM call with a mock provider, replay with a different mock configured to raise if called, verify bit-identical response and no provider call
- [x] Seam 2 test: record a tool call, replay, verify bit-identical result and no tool re-execution
- [x] Seam 2 test: cassette file structure is valid (content-addressed files exist, `index.json` manifest parses, `trace_id` links to OTel trace)
- [x] Seam 2 test: `repro.incomplete=true` on `invoke_agent` CLIENT span for external CLI dispatch

## T8 — Legacy retirement (Phase 2+3) [COMPLETED 9da8491b..]

**What to build:** The "contract" phase of the trace format migration. `format_send_ack` is updated to point only to `spans.jsonl` (Phase 2). `TraceCollectorHook` is refactored to construct OTel spans directly — `OperationRecord` construction is dropped (Phase 3). `JsonFileTraceStore` and `OperationRecord` are removed. `operations.jsonl` is no longer written. The `TraceStore` ABC is refactored into a `TraceQuery` ABC (read-only: `list_by_session` / `list_by_trace_id` over `spans.jsonl`). Existing `operations.jsonl` files remain on disk (no data migration) but are no longer produced. This ticket is blocked by all trace-consuming features (T1 expand, T5 training tags, T7 cassette) to ensure no consumer depends on the legacy format before it is removed.

**Blocked by:** T1 — OTel span emission + local file (Phase 1 dual-write); T5 — Training data L1 tagging; T7 — Cassette default scope (B2, 1+2+6)

- [x] `format_send_ack` reports only `spans.jsonl` path (no longer mentions `operations.jsonl`)
- [x] `TraceCollectorHook` refactored to construct OTel spans directly via the OTel SDK `Tracer` API — no longer constructs `OperationRecord`
- [x] `OperationRecord` dataclass removed from the codebase
- [x] `JsonFileTraceStore` class removed from the codebase
- [x] `operations.jsonl` is no longer written by any code path
- [x] `TraceStore` ABC refactored to `TraceQuery` ABC (read-only interface: `list_by_session(session_id) -> list[Span]`, `list_by_trace_id(trace_id) -> list[Span]`); the write-side `save()` method is removed
- [x] A `JsonlSpanQuery` implementation of `TraceQuery` reads `spans.jsonl` using `read_jsonl_robust`
- [x] Existing `operations.jsonl` files on disk are not migrated or deleted (they remain readable by legacy tools if needed)
- [x] The factory no longer registers `JsonFileTraceStore`; only the OTel-based emission path remains
- [x] All existing tests updated to assert against `spans.jsonl` content (not `operations.jsonl`)
- [x] `examples/bot_project` ack text and any agent prompt references updated to `spans.jsonl`
- [x] Seam 1 test: a ReAct turn produces only `spans.jsonl` (no `operations.jsonl`)
- [x] Seam 1 test: `format_send_ack` text contains only `spans.jsonl` path
- [x] Seam 1 test: `TraceQuery.list_by_session()` returns spans from `spans.jsonl`
- [x] Seam 1 test: `TraceQuery.list_by_trace_id()` returns spans for a specific trace across sessions

---

# Phase 2: Span gap remediation + Langfuse integration (2026-07-27)

**Status: PLANNED** (design locked via grilling session; implementation pending)

A harness-engineering review identified 5 blocking-level span gaps where
Phase 1 tickets claimed completion but code emission is absent or broken.
Phase 2 remediates these gaps and confirms the Langfuse integration route
(OTLP-only, no `langfuse` SDK dependency). See ADR-0024 Implementation
Notes (2026-07-27) IN9–IN15 for the full decision record.

**Scope boundary**: Phase 2 = fix gaps + attributes + Langfuse deployment
docs. **Not in scope**: agent-self-read harness decisions (Phase 3,
`TraceDrivenLoopDetectorHook` placeholder only), LLM-as-judge tool
correctness scoring, DPO training data export repair (depends on G3 but
is independent work).

```
Wave 1:  T9 (hook ABCs)  ← foundation, blocks T10/T11
             │
Wave 2:  ┌──┴──┐
         T10   T11      ← parallel (T10 after T9; T11 after T9)
          │
Wave 3:  T12 (handoff + G11 fix)  ← after T10
          │
Wave 4:  T13 (Langfuse docs)  ← after T10/T11/T12 (needs all spans working)
```

## T9 — New hook ABCs: BeforeLLMHook + AfterApprovalHook [COMPLETED]

**What to build:** Add two new hook ABCs to `src/modex_agent/hook/abc.py`:
`BeforeLLMHook` (abstract method `before_llm(ctx, request)`) and
`AfterApprovalHook` (abstract method `after_approval(ctx, transaction)`).
Register both in `HookPoint` enum + `_HOOK_DISPATCH` dict in
`hook/runner.py`. `AfterIterationHook` already exists (D4) but
`TraceCollectorHook` doesn't implement it — T9 only adds the two missing
ABCs, T10 wires `TraceCollectorHook` to all three.

**Blocked by:** — (foundation ticket)

**Why:** G1 (LLM duration) and G2 (request prompt) both need a pre-LLM
hook point that doesn't exist. G3 (approval span) needs a post-approval
hook point that doesn't exist. hermes-agent's `nemo_relay/__init__.py:526-548`
provides the reference hook taxonomy: `pre_llm_call` / `post_llm_call` /
`pre_approval_request` / `post_approval_response` / `subagent_start` /
`subagent_stop`. ModexAgent maps these to its ABC-first hook system.

- [x] `BeforeLLMHook(Hook)` ABC in `hook/abc.py` with `before_llm(ctx, request)` abstract method
- [x] `AfterApprovalHook(Hook)` ABC in `hook/abc.py` with `after_approval(ctx, transaction)` abstract method
- [x] `HookPoint.BEFORE_LLM` + `HookPoint.AFTER_APPROVAL` enum values
- [x] `_HOOK_DISPATCH` entries for both new points
- [x] Dispatch call sites: `BEFORE_LLM` emitted in `LLMNode.execute()` before `ReactLlmClient.call()`; `AFTER_APPROVAL` emitted in `ApprovalResumer.apply_resume()` after decision applied
- [x] `HookRunner.dispatch` handles new points
- [x] Unit test: a no-op hook implementing each new ABC is dispatched correctly

## T10 — TraceCollectorHook: implement gaps G1/G2/G3/G5 + attributes [COMPLETED]

**What to build:** Extend `TraceCollectorHook` to implement `BeforeLLMHook`
+ `AfterApprovalHook` + `AfterIterationHook` (existing ABC, not yet
implemented by this hook). Emit: LLM duration (G1), request prompt via
`PromptCaptureStrategy` (G2), `human.review` approval span (G3),
`iteration.start`/`iteration.end` boundary spans (G5). Add attributes:
`gen_ai.usage.cache_read_input_tokens`, `gen_ai.usage.cache_creation_input_tokens`,
`gen_ai.request.model`, tool `success`/`fail`/`error_type`,
`execute_tool_batch` `end_time`.

**Blocked by:** T9 (hook ABCs)

**Why:** These are the 5 blocking gaps from IN10. All are ADR-0024
designed-but-not-emitted work, not new design.

- [x] `TraceCollectorHook` implements `BeforeLLMHook`: pre-LLM timestamp + `PromptCaptureStrategy.capture()` → `chat` span attributes
- [x] `TraceCollectorHook` implements `AfterApprovalHook`: emit `human.review` span with `decision` + `deny_reason` + `tool_name` + `tool_call_id`
- [x] `TraceCollectorHook` implements `AfterIterationHook`: emit `iteration.start`/`iteration.end` boundary spans with `iteration_number`
- [x] `chat` span gains `gen_ai.request.model`, `gen_ai.usage.cache_read_input_tokens`, `gen_ai.usage.cache_creation_input_tokens`, `api_duration_s`, `start_time`, `end_time`
- [x] `execute_tool` span gains `success`/`fail`/`error_type` attributes (derive from `ToolResult.error`)
- [x] `execute_tool_batch` span gains `end_time`
- [x] `PromptCaptureStrategy` ABC + `SummaryPromptCapture` impl in `src/modex_agent/trace/prompt_capture.py`
- [x] `ObservabilityConfig.prompt_capture: str = "summary"` field
- [x] Factory: `prompt_capture` config → `PromptCaptureStrategy` instance → `TraceCollectorHook`
- [x] Unit tests: each new span/attribute verified in `spans.jsonl`
- [x] Unit test: `PromptCaptureStrategy` ABC subclass replaces capture logic without hook code change

## T11 — Multi-agent handoff span (G10) [COMPLETED]

**What to build:** Emit `agent.handoff` span at the `send_to_agent`
dispatch point, linking parent turn's `invoke_agent` root span to the
child agent's `invoke_agent` root span via `parent_span_id` + shared
`trace_id`. The child's `invoke_agent` span already exists (Phase 1);
this ticket adds the connecting link span.

**Blocked by:** T9 (needs hook infrastructure for dispatch-point access)

**Why:** G10 — multi-agent trace tree is broken without the handoff span.
`send_to_agent` propagates `traceparent` (D8) but emits no span, so the
child's root span has no parent link in the trace.

- [x] `SpanName.AGENT_HANDOFF` in `semconv.py`
- [x] Emit `agent.handoff` span in `AgentCommunicationService._send()` (or `send_to_agent` tool executor)
- [x] Span attributes: `target_agent`, `message_type`, `parent_turn_id`, `child_turn_id`
- [x] `parent_span_id` = current turn's `invoke_agent` span_id; child's `invoke_agent` span inherits via traceparent
- [x] Unit test: multi-agent dispatch produces trace tree with `agent.handoff` linking parent → child
- [x] Integration test: Langfuse receives linked trace tree (manual verify)

## T12 — G11 fix: set_tracer_provider + external coding agent span export [COMPLETED]

**What to build:** Fix `otel_store._build_otlp_tracer()` to call
`trace.set_tracer_provider()` so the local `TracerProvider` is registered
globally. Currently a local provider is created but never set, so
`external_coding/agent.py`'s `trace.get_tracer(__name__)` returns the
no-op default provider, and external coding agent CLIENT spans never export.

**Blocked by:** T10 (verify no regression in existing OTLP export)

**Why:** G11 — external coding agent's OTel SDK spans (which use the
global tracer, not the `OtelSpanTraceStore`'s local tracer) silently
fail to export. This is a one-line fix but needs regression testing.

- [x] `trace.set_tracer_provider(provider)` call in `_build_otlp_tracer()`
- [x] Verify existing `spans.jsonl` + OTLP dual-path unaffected
- [x] Verify external coding agent `invoke_agent` CLIENT span now exports via OTLP
- [x] Unit test: external coding agent turn produces exportable span
- [x] Regression: all existing trace tests pass

## T13 — Langfuse deployment docs + bot config example [COMPLETED]

**What to build:** Langfuse deployment reference (Docker Compose) +
teaching documentation in `examples/bot_project/`. `bot_config.yml`
observability section example with `otel_endpoint` pointing to Langfuse.
**Not framework code** — documentation + config example only.

**Blocked by:** T10, T11, T12 (needs all spans working to document the
complete analysis surface)

**Why:** IN15 — Langfuse deployment is a business/ops decision, not
framework code. The bot project is the end-to-end reference implementation,
so deployment docs live there.

- [x] `examples/bot_project/docs/langfuse-deployment.md` — Docker Compose reference + config + dashboard setup
- [x] `examples/bot_project/config/bot_config.yml` observability section: Langfuse `otel_endpoint` example (commented out, opt-in)
- [x] Teaching doc: how to read trace tree in Langfuse (turn → iteration → LLM → tool, `agent.handoff` for multi-agent)
- [x] Teaching doc: cache hit rate / tool correctness rate / trajectory analysis workflows (per IN15)
- [x] Teaching doc: flagged-trace → dataset → eval workflow (Phase 3+ preview)

---

# Phase 3: Harness intelligence via trace-driven decisions (FUTURE)

**Status: FUTURE** — not yet planned for implementation. Outlined here to
preserve direction; detailed tickets will be written when Phase 2 data is
available to calibrate strategies.

**Current state:** Phase 2 made traces complete enough for both human analysis
(Langfuse) and agent self-read (`spans.jsonl`). The `TraceDrivenLoopDetectorHook`
placeholder exists (ADR-0024 IN13, class+docstring, not registered). No
trace-consuming harness logic exists yet — `ToolCallDeduplicator` is streak-
based in-memory, `error_recovery.py` is overflow-only, `EndNode` has no task-
completion verification, `ExperienceReviewAgent` reviews conversations not
traces.

**Direction (reference: hermes-agent cross-reference in ADR-0024):**

1. **Loop/stuck detection** — implement `TraceDrivenLoopDetectorHook` consuming
   `spans.jsonl`: tool fingerprint hashing, output similarity, oscillation,
   stale-call circuit breaker. Reference: hermes `chat_completion_helpers.py`
   #58962 (494 consecutive failures, 3+ days).

2. **Error recovery taxonomy** — replace overflow-only `error_recovery.py`
   with classification-driven recovery (rate limit backoff / billing credential
   rotation / content-policy fallback / network retry / overflow compress).
   Reference: hermes `error_classifier.py` 27-reason taxonomy.

3. **Truth enforcement** — verification gate in `EndNode`: intercept "code edit
   without verification", force test/lint/build. File-mutation validator +
   SQLite evidence ledger. Reference: hermes `verification_stop.py`.

4. **Experience review upgrade** — upgrade `ExperienceReviewAgent` to
   `background_review.py` pattern: fork-daemon with prefix cache inheritance,
   tool whitelist, trace-driven context. Reference: hermes `background_review.py`.

**Prerequisite:** Phase 2 must run in production long enough to observe real
agent behavior patterns in Langfuse. Strategies must be calibrated against
data, not implemented blind.
