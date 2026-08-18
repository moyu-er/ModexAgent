# Agent observability, reproducibility, and training data derivation

Status: implemented (2026-07-17) — design decided through grilling session; implemented across 9 tickets (T0–T8) with post-implementation Oracle review fixes. See "Implementation Notes" at end of file.

## Context

ModexAgent has a custom flat JSONL trace mechanism (`src/modex_agent/trace/`) that
records 5 operation kinds (LLM_CALL / TOOL_BATCH / TOOL_CALL / TURN_START / TURN_END)
per turn, written via `TraceCollectorHook` to `<workspace>/.modex/runtime_state/<pool>/trace/`.
It serves basic observability but has structural limits: no span tree, no cross-turn
tracing, no real-time observation, no metrics aggregation, no eval layer, and 3
`OperationKind` enum values (`APPROVAL` / `CONTROL_COMMAND` / `ERROR`) are defined
but never recorded.

The user wants three capabilities beyond raw observability:

1. **可观测 (Observable)** — agent behavior trackable and viewable, with industry-standard
   tooling.
2. **可复现 (Reproducible)** — agent executions replayable for debugging and regression.
3. **训练数据 (Training data)** — reasoning process and execution data collected for
   model training (algorithm side).

Industry research (OpenTelemetry GenAI semconv, ATSC draft, Langfuse/Phoenix/MLflow,
LangGraph time-travel, Rewind cassette replay) established key facts:

- OTel GenAI semconv is `Development` status (not stable) but usable with a pinned version.
- `temperature=0` is empirically non-deterministic (Qwen3-235B: 80 unique outputs from
  1000 identical prompts; GPT-4o: 72-point accuracy swings). True reproducibility requires
  the cassette pattern (capture all 6 side-effect categories), not just trace replay.
- LangGraph time-travel (checkpoint re-execution) is the shipped benchmark for snapshot/restore
  but is NOT deterministic — LLM calls re-fire.
- The OTel GenAI spec has a grouping gap (Issue #94): flat `chat`/`execute_tool` spans
  cannot express ReAct-round boundaries. ModexAgent's 4-node loop is directly affected.
- Langfuse v4 is OTel-native, MIT-licensed, self-hostable, with an eval layer.
- Subprocess trace context propagation (W3C `traceparent` via env vars) is STABLE in OTel
  — solves the Pi/OpenCode cross-process tracing problem.

The existing `TurnSnapshot` / `RuntimeStateCodec` / `SqliteTurnStateStore` infrastructure
(proven by approval suspend/resume) provides the structural prerequisite for checkpoint
re-execution, but only triggers on approval suspend — not per-iteration. The `AfterIterationHook`
ABC already exists and is already dispatched by `GraphEngine.run`, but no hook consumes it
for checkpointing.

## Decision

Adopt a **dual-path + derivation** architecture spanning three capability layers.

### D1 — Dual data path + derivation layer

Two independent data paths linked by `trace_id`, plus a read-time derivation layer:

```
┌─ Path A: Trace (observability) ──────────────────────┐
│ 5 existing hooks → upgraded to OTel gen_ai.* spans    │
│ Samplable, redactable, streaming-first, low-overhead  │
│ Backend: OTel Collector → Langfuse (self-hosted)      │
│ Retains reasoning_content (D3); Memory still strips   │
└───────────────────────┬───────────────────────────────┘
                        │ trace_id (join key, not data copy)
┌─ Path B: Repro (reproducibility) ────────────────────┐
│ B1 Checkpoint (default-on): per-iteration TurnSnapshot│
│    via AfterIterationHook → CheckpointHook             │
│    Backend: SQLite turn_snapshots (existing)           │
│ B2 Cassette (opt-in): 6-category side-effect capture  │
│    Wraps LLM client + tool dispatcher + clock + RNG    │
│    Backend: local content-addressed files              │
│    No redaction (breaks fidelity)                      │
└───────────────────────┬───────────────────────────────┘
                        │ trace_id (derivation, not capture)
┌─ Derivation: Training Data ──────────────────────────┐
│ Write-time: tag spans gen_ai.training.relevant (L1)   │
│ Read-time: TrainingDataExporter aggregates by trace_id│
│   → trajectory-level SFT/DPO + iteration-level tool-use│
│   L2 heuristic scoring + L3 optional annotation       │
│ Backend: JSONL / HuggingFace dataset                   │
└────────────────────────────────────────────────────────┘
```

**Rationale**: Path A and Path B cannot merge — observability wants sampling +
redaction + low cost; reproducibility wants full fidelity + no sampling + no redaction.
These requirements conflict. Training data is a read-time consumer of Path A, not a
third write path (avoids triple-write overhead). The MLflow "evaluate recorded traces"
pattern validates this: generate traces once, derive multiple times.

### D2 — Reproducibility levels: L2 default + L4 opt-in

| Level | Mechanism | Deterministic | Default |
|---|---|---|---|
| L1 Trace Replay | Read-only re-render of recorded trace | N/A (no execution) | Always on (Path A) |
| L2 Checkpoint Re-execution | Resume from TurnSnapshot; nodes after checkpoint re-run | No (LLM re-fires) | **Default on** (Path B1) |
| L3 Input Replay | Re-run with saved inputs, possibly different model | No | Emerges from training data export |
| L4 Deterministic Replay | Bit-identical from cassette; no network | **Yes** | Opt-in (`repro.cassette=true`) |

**Rationale**: L2 covers 90% of debugging needs at low cost (reuses existing snapshot
infra). L4 is the only true reproducibility but has high overhead (6-category capture)
and is needed only for regression testing / hard bug reproduction. L1 is free (already
have the traces). L3 is not a built-in — it emerges when training data is exported and
re-fed to a different model.

### D3 — reasoning_content retained in Trace, stripped in Memory

The existing rule (CONTEXT.md: `ChatMessage.to_dict()` strips `reasoning_content`
before memory storage) is **unchanged for the Memory layer**. The Trace Path (Path A)
**retains** `reasoning_content` in OTel span attributes — this is a separate serialization
path, no conflict.

**Rationale**: Training data derivation needs reasoning traces (STaR, reasoning-model
training). Without Trace-layer retention, training data would only be available when
Cassette (L4) is enabled — too restrictive. Memory-layer stripping stays (context budget,
privacy). The two layers have different serialization paths and different policies.

**Custom attribute**: `usage.reasoning_tokens` is recorded as a custom OTel attribute
because `gen_ai.*` semconv does not yet have a dedicated reasoning-tokens field. This
is a known gap; track the semconv issue and migrate when standardized.

### D4 — Per-iteration checkpoint via AfterIterationHook

Checkpoint granularity is **per-iteration** (one ReAct round: think → act → observe),
not per-node and not per-turn.

- **Implementation**: A `CheckpointHook(AfterIterationHook, SnapshotPolicy)` registered
  to `HookRunner`. The `AfterIterationHook` ABC already exists (`src/modex_agent/hook/abc.py:124`)
  and is already dispatched by `GraphEngine.run` via `_HOOK_DISPATCH`. **No graph-engine
  change needed.** No new hook point needed.
- **Dispatch**: Uses existing `isinstance` + ABC dispatch (not `getattr`). The stale
  comment on `HookPoint` ("HookRunner 通过 getattr(hook, hook_point.value) 调度") is
  outdated — implementation uses `isinstance`; this comment should be corrected.
- **Snapshot**: Regular `TurnSnapshot` with `SnapshotReason.ITERATION`. Reuses
  `ReActSnapshotPolicy.capture()` and `RuntimeStateCodec.encode_turn()/decode_turn()`.
- **Re-execution from iteration N**: iterations 1..N-1 read from snapshot (deterministic);
  iteration N+ re-runs (non-deterministic, fresh LLM calls). This is L2 semantics.
- **Unregistered = no effect**: If `CheckpointHook` is not registered, `HookRunner`
  skips it (`isinstance` check fails) — existing behaviour is byte-for-byte unchanged.

**Rationale**: Per-iteration is the logical unit of ReAct reasoning (matches LangGraph's
super-step). Per-node is too fine (START/END are trivial; 40× storage for a 10-iteration
turn). Per-turn is too coarse (only one snapshot, no intermediate recovery). Per-iteration
is stable across graph-structure changes (adding PLAN/REFLECT/MEMORY nodes doesn't change
"iteration" semantics). The `after_iteration` hook point is a **three-in-one**: it drives
(1) per-iteration checkpoint, (2) OTel trace iteration grouping (solves Issue #94), and
(3) training-data iteration-boundary marking.

### D5 — Cassette: layered 6-category capture

| Category | Captured | Default | Full |
|---|---|---|---|
| 1. LLM calls (prompt + full response + model + params + latency + retries) | ✅ | ✅ | ✅ |
| 2. Tool calls (name + args + result + error + latency) | ✅ | ✅ | ✅ |
| 3. Time reads (every `time.time()`/`datetime.utcnow()`) | ✅ | ❌ | ✅ |
| 4. RNG draws (every `random.random()`/`secrets.token_hex()`) | ✅ | ❌ | ✅ |
| 5. External reads (vector search, DB, HTTP fetch) | ✅ | ❌ | ✅ |
| 6. Retries (each attempt + delay) | ✅ | ✅ | ✅ |

- **Default scope** (`repro.cassette=true`): 1+2+6 — covers 90% of bug reproduction
  (LLM output drift + tool results + retry sequences).
- **Full scope** (`repro.cassette=full`): +3+4+5 — for 100% fidelity (regression testing).
  Requires injected virtual clock + deterministic RNG (framework must route all time/RNG
  calls through injection points).
- **External CLI agents** (Pi/OpenCode): inherently L4-incomplete (subprocess internals
  uncapturable). Parent's `invoke_agent` CLIENT span marked `repro.incomplete=true`.
  W3C `traceparent` env-var propagation links parent and child traces (STABLE in OTel).
- **No redaction**: Cassette stores raw data locally (content-addressed). Redaction
  breaks fidelity. If sharing is needed, tokenization-with-vault is the only approach.
- **Cassette is payload, trace is index**: one `trace_id` ties them. OTel spans carry
  timing + structure; the cassette file carries the bytes.

**Rationale**: Rewind's empirical analysis shows categories 1+2 are the highest-frequency
drift sources. Categories 3+4 have low capture cost but high intrusion (must inject virtual
clock/RNG into all call sites) — defer to full scope. Category 5 has fuzzy boundaries
(memory is in-process and snapshottable; MCP tools / HTTP fetches are not) — mark unknown
boundaries as `cassette.incomplete` with a warning.

### D6 — Training data: trajectory-level + write-time tagging / read-time derivation

- **Granularity**: Trajectory-level (primary) = one turn's full execution aggregated by
  `trace_id`. Iteration-level (auxiliary) = single ReAct round. Turn-level is NOT produced
  (loses reasoning process, contradicts the core need).
- **Write-time** (online, microsecond cost): Tag spans with `gen_ai.training.relevant`
  (true/false) via L1 rule filter:
  - `TurnPhase.FAILED/CANCELLED` → false
  - iteration count > threshold → false (possible infinite loop)
  - total tokens > threshold → false (over budget)
  - else → true
- **Read-time** (offline, `TrainingDataExporter`): Aggregate spans by `trace_id` →
  trajectory. Convert to target format. Apply L2 heuristic scoring (tool success rate,
  reasoning depth, trajectory compactness). Optional L3 annotation (approval-as-preference
  or LLM-as-judge).
- **Storage path (amended 2026-08-17 collector migration; amended 2026-08-18
  write-only refactor)**: The former dual-path (every backend also writes local
  `spans.jsonl`) is superseded — the active path is OTel-only: spans are exported
  via the local OTel Collector (contrib 0.158.0) to Langfuse, the system of record
  for `otel_http` mode (the default). `TraceBackend.FILE` + `JsonlSpanQuery` are
  retained as a dormant, selectable fallback (ADR-0007). In `otel_http` mode the
  store is WRITE-ONLY: `save_span` feeds a bounded export queue drained by the
  daemon sender thread, nothing is buffered for read-back, and the read methods
  `list_by_session` / `list_by_trace_id` raise `NotImplementedError` (FILE
  branches unchanged). Same-process metric consumers read the scalar
  `MetricCounters` accumulated in `TraceSessionState` (plus the per-turn
  `TurnCustomKey.TRAJECTORY_METRICS` stash) — never span read-back; cross-process
  reads go through `LangfuseTraceQuery` (Langfuse v2 API).
- **Approval-as-preference** (L3, opt-in): `ApprovalDecision.APPROVED/DENIED` + `deny_reason`
  is a free human-preference signal. Same-task multiple trajectories → DPO pairs (chosen =
  approved trajectory, rejected = denied trajectory). Unique to ModexAgent (most frameworks
  lack approval mechanisms).
- **LLM-as-judge** (L3, opt-in): Score trajectories 1-5 via LLM. Only applied to L1+L2
  passed subset (cost control). Prompt template configurable.

**Format standards** (informed by industry research — LangSmith SFT cookbook, distilabel/Argilla
DPO pipeline, OpenAI function-calling fine-tuning guide, Microsoft Agent Lightning trace adapters):

The exporter emits two primary formats:

1. **SFT — OpenAI messages JSONL** (one `{"messages":[...]}` per line):
   ```json
   {"messages": [
     {"role": "system", "content": "..."},
     {"role": "user", "content": "..."},
     {"role": "assistant", "content": "", "tool_calls": [
       {"id": "call_0", "type": "function",
        "function": {"name": "terminal_command",
                     "arguments": "{\"cmd\":\"ls\"}"}}]},
     {"role": "tool", "tool_call_id": "call_0", "content": "file1.txt\nfile2.txt"},
     {"role": "assistant", "content": "<think>reasoning here</think>\nfinal answer"}
   ]}
   ```
   - `reasoning_content` from D3 is wrapped in `<think>...</think>` tags (DeepSeek-R1 / OpenThoughts3 format)
   - `tool_calls[].function.arguments` must be a **JSON string** (not object) per OpenAI spec
   - `tool_calls[].id` must be unique per example
   - Tool schema (`tools` array) included at top level for tool-use SFT
   - Anthropic variant: `tool_use`/`tool_result` content blocks instead of `tool_calls` field

2. **DPO — preference pair JSONL**:
   ```json
   {"prompt": "...", "chosen": "...", "chosen_model": "react_main",
    "chosen_rating": 5, "rejected": "...", "rejected_model": "react_main",
    "rejected_rating": 2}
   ```
   - Source: approval-as-preference (approved trajectory = chosen, denied = rejected)
   - Min score gap filter (≥0.5) and edit-distance ratio filter (≥0.1) to drop trivial pairs
   - Refusal filtering (drop "I'm sorry / I cannot..." chosen responses)

**Key industry validation — Microsoft Agent Lightning**: Agent Lightning (`github.com/microsoft/agent-lightning`) already implements OTel-spans-to-training-data adapters in production. Its `TraceToMessages` adapter reconstructs OpenAI messages from `gen_ai.prompt.N.*` / `gen_ai.completion.N.*` flattened span attributes (the inverse of OTel's array flattening), and `TracerTraceToTriplet` produces RL `(prompt, response, reward)` triplets. This directly validates our "training data derived from trace path" architecture — the OTel trace IS the training-data substrate, not a separate capture. We port Agent Lightning's `group_genai_dict` unflatten utility pattern.

**Multi-granularity collection** (HPL paper, ICLR 2026): The 4-node ReAct graph (START→LLM→TOOL→END) maps cleanly onto "groups" — one LLM+TOOL cycle is a semantic sub-task group. Store per turn: LLM span (turn-level), tool-execution subtree (step-level), full session trace (trajectory-level). This enables trajectory/step/group preference pairs from the same trace store.

**Deduplication** (3-tier, cheapest first):
1. Exact hash (SHA-256) — catches 5-15% verbatim copies, near-zero cost
2. MinHash LSH (n-gram Jaccard, threshold ~0.8) — catches 10-25% near-duplicates
3. Semantic embedding cosine (threshold ~0.92-0.95) — catches 15-35% functionally-identical

**Privacy gating** (pydantic-ai lesson): Because ModexAgent has multi-tenant memory scopes (Session/User/Tenant/Agent/Channel/Chat/Composite/Global), trace→training export MUST be scope-aware — never export spans across tenant boundaries without explicit opt-in. This is a hard filter in the export adapter, not a documentation note.

**Rationale**: Write-time tagging is near-zero cost (1 OTel attribute). Read-time derivation
avoids a third write path. Trajectory-level serves SFT (multi-step reasoning), RLHF
(trajectory reward), STaR (self-bootstrapped reasoning), and DPO (preference pairs).
Approval-as-preference leverages existing structured approval data — a signal most frameworks
lack. Agent Lightning validates the OTel→training-data path is production-proven.

### D7 — OTel-first, Collector fan-out, no vendor lock-in

- **Emission**: ModexAgent emits standard `gen_ai.*` OTel spans via OTLP. No vendor-specific
  SDK in framework code.
- **Collector**: OTel Collector as the single sink, with transform processors for PII
  redaction and tail sampling. Fan-out to multiple backends.
- **Primary backend**: Langfuse v4 (MIT, self-hosted, OTel-native, eval layer, `@observe`).
- **Optional backends**: Phoenix (Apache 2.0, eval), Datadog/Honeycomb (APM + alerting).
  All consume standard OTLP — one emission, multiple consumers.
- **Semconv isolation**: `gen_ai.*` attributes isolated behind an adapter layer. Semconv
  is `Development` status and will change; the adapter is the single point of change.
- **ATSC draft spans** (memory.read/write, human.review, agent.handoff): emitted as custom
  attributes now; migrate to ATSC standard when it stabilizes. These are the spans that
  raise vanilla-OTel Fault Detection Rate from 0.429 to 1.000 (per AgentTelemetry paper).

**Rationale**: Avoids vendor lock-in. Langfuse is the best fit (MIT, self-hostable, eval,
OTel-native). OTel Collector gives PII redaction + sampling at the infrastructure layer,
not in application code. The adapter layer isolates the unstable semconv.

### D8 — Subprocess trace propagation via W3C traceparent

External CLI agents (Pi/OpenCode) participate in the trace tree via W3C `traceparent` /
`tracestate` environment variables (STABLE in OTel `env-carriers` spec):

- Parent (ModexAgent): opens `invoke_agent` CLIENT span, injects `traceparent` into
  child env via `inject(carrier=env, setter=EnvVarSetter())`. `modexctl send` CLI is
  the injection point.
- Child (Pi/OpenCode): extracts context via `extract(carrier=os.environ, getter=EnvVarGetter())`,
  opens `invoke_agent` INTERNAL span as a child of the parent trace.

**Rationale**: Solves the cross-process tracing problem without modifying external agents'
internals. The child's internal spans appear as descendants of the parent's `invoke_agent`
CLIENT span. If the child is not OTel-instrumented, the parent span still records duration
and exit code — the trace is not broken, just shallow on the child side.

### D9 — Zero-process default: framework ships local-only, no external services required

The framework's default observability mode requires **no new software process** — no
OTel Collector, no Langfuse, no database. Three deployment tiers exist, but only Tier 1
is the framework default:

| Tier | New process? | Data flow | Who configures? |
|---|---|---|---|
| **Tier 1 (framework default)** | None | OTel SDK in-process → `FileSpanExporter` → local JSONL | Framework (automatic) |
| **Tier 2 (business opt-in)** | 1 docker compose | OTel SDK → OTLP HTTP → Langfuse container | Business (`bot_config.yml`) |
| **Tier 3 (full production)** | + Collector container | OTel SDK → Collector → multi-backend | Business (infra team) |

The OTel SDK (`opentelemetry-sdk`) is a **library, not a service** — it runs inside the
Python process via `BatchSpanProcessor` (async batched, <1ms/span overhead). Tier 1 writes
standard OTel span JSONL to local files using a custom `FileSpanExporter` (a standard OTel
`SpanExporter` subclass, not a new invention — Rewind uses the same pattern).

**Rationale**: The framework must work standalone with zero external dependencies (matches
existing design: `pexpect`/`tmux`/`winpty` are optional per-platform, MCP is optional, etc.).
Business users (bot_project) opt into Tier 2/3 by changing YAML config — no framework code
change needed because the OTel span format is identical across tiers; only the exporter
changes.

### D10 — Replace legacy trace with OTel span format; local file is default-on, multi-exporter concurrent

The existing `OperationRecord` format (`operations.jsonl`) is **replaced** by OTel span format (`spans.jsonl`). This is not a dual-write — the legacy format is deprecated and removed after a migration period. The local file exporter is **default-on**; additional exporters (OTLP HTTP to Langfuse/Phoenix/Datadog) can be **concurrently enabled** via config, writing to multiple destinations simultaneously.

**Why replacement, not dual-write** (revised from earlier draft):

1. **Agent can read OTel span format directly** — agents read JSON lines and identify semantics by field name, not by binding to the `OperationRecord` class. OTel's `gen_ai.*` attribute names are more self-descriptive than `metadata.*`. The `parent_span_id` field gives agents hierarchical understanding that `OperationRecord`'s flat structure cannot.
2. **Dual-write is redundant** — maintaining two serialization paths (`OperationRecord` + OTel span) for the same data is maintenance debt with no long-term value. The migration period is for safety, not for permanent coexistence.
3. **OTel SDK natively supports multi-exporter** — `SpanProcessor` chains allow concurrent export to local file + remote OTLP + console. This is OTel's core design, not a custom invention. One span construction, N export destinations.

**Multi-exporter architecture**:

```
TraceCollectorHook (refactored to emit OTel spans, not OperationRecord)
    │
    └─ Tracer.start_span() → OTel SDK
         │
         ├─ SpanProcessor #1: BatchSpanProcessor
         │   └─ FileSpanExporter (default-on, local JSONL)
         │       → <ws>/.modex/runtime_state/<pool>/trace/<session>/spans.jsonl
         │       Agent self-reads this file (format_send_ack points here)
         │
         ├─ SpanProcessor #2: BatchSpanProcessor (optional, config-driven)
         │   └─ OTLPSpanExporter → Langfuse / Phoenix / Datadog
         │       Ops/algorithm team reads this
         │
         └─ SpanProcessor #3: ConsoleSpanExporter (optional, debug)
             → stdout
```

Each `SpanProcessor` is independent: batched, retried, timed-out separately. One exporter failing does not affect others. Adding a new backend (e.g. Datadog) requires only adding an exporter — no framework code change.

**OTel span JSONL format** (what the agent reads, 1 line per span):

```jsonl
{"trace_id":"a1b2","span_id":"c3d4","parent_span_id":null,"name":"invoke_agent","kind":"INTERNAL","start_time":1721...,"end_time":1721...,"attributes":{"gen_ai.operation.name":"invoke_agent","gen_ai.agent.name":"react_main","gen_ai.session.id":"conv.main"},"status":{"code":"OK"}}
{"trace_id":"a1b2","span_id":"e5f6","parent_span_id":"c3d4","name":"chat","kind":"CLIENT","start_time":...,"end_time":...,"attributes":{"gen_ai.request.model":"gpt-4o","gen_ai.response.finish_reason":"stop","gen_ai.output.content":"Let me check...","gen_ai.output.reasoning_content":"The user wants...","gen_ai.usage.input_tokens":1200,"gen_ai.usage.output_tokens":350},"status":{"code":"OK"}}
{"trace_id":"a1b2","span_id":"g7h8","parent_span_id":"c3d4","name":"execute_tool","kind":"INTERNAL","start_time":...,"end_time":...,"attributes":{"gen_ai.tool.name":"terminal_command","gen_ai.tool.call_id":"call_x9y8","gen_ai.tool.arguments":"{\"cmd\":\"ls\"}","gen_ai.tool.result":"file1.txt\nfile2.txt","gen_ai.tool.duration_ms":450},"status":{"code":"OK"}}
```

**Field mapping** (OperationRecord → OTel span):

| OperationRecord field | OTel span equivalent | Notes |
|---|---|---|
| `trace_id` | `trace_id` | Same concept |
| `session_id` | `attributes.gen_ai.session.id` | |
| `agent_name` | `attributes.gen_ai.agent.name` | |
| `invocation_id` | `attributes.gen_ai.invocation.id` | Custom attribute |
| `kind` (enum) | `name` + `attributes.gen_ai.operation.name` | `llm_call` → `name="chat"`, `tool_call` → `name="execute_tool"` |
| `status` (enum) | `status.code` | `completed` → `OK`, `failed` → `ERROR` |
| `timestamp` | `start_time` | |
| `duration_ms` | `end_time - start_time` | Computed, not stored separately |
| `metadata.content` | `attributes.gen_ai.output.content` | |
| `metadata.reasoning` | `attributes.gen_ai.output.reasoning_content` | D3: retained in trace |
| `metadata.usage` | `attributes.gen_ai.usage.input_tokens` / `output_tokens` | + custom `reasoning_tokens` |
| `metadata.tool_calls` | `attributes.gen_ai.output.tool_calls` | |
| `metadata.tool_name` | `attributes.gen_ai.tool.name` | |
| `metadata.result` | `attributes.gen_ai.tool.result` | |
| `error` | `status.code=ERROR` + `status.message` | |

**Config-driven multi-exporter** (extends `ObservabilityConfig`):

```python
class TraceBackend(str, Enum):
    OFF = "off"
    FILE = "file"           # local JSONL (default)
    OTEL_HTTP = "otel_http" # remote OTLP HTTP

class ObservabilityConfig(BaseModel):
    # Local file exporter (default-on)
    trace_backend: TraceBackend = TraceBackend.FILE
    
    # Remote OTLP exporter (optional, independently enabled)
    otel_endpoint: str | None = None  # set = concurrent remote export
    otel_service_name: str = "modex_agent"
    
    # Combinations:
    #   trace_backend=FILE + otel_endpoint=None → local only (default)
    #   trace_backend=FILE + otel_endpoint=set  → local + remote (concurrent)
    #   trace_backend=OTEL_HTTP + otel_endpoint=set → remote only
    #   trace_backend=OFF + otel_endpoint=None → fully off
```

**`format_send_ack` update** (`result.py:70-74`): points to `spans.jsonl` (single path, not dual).

**Migration strategy** (phased, not big-bang):

| Phase | `OperationRecord` | `JsonFileTraceStore` | `operations.jsonl` | `spans.jsonl` | `format_send_ack` |
|---|---|---|---|---|---|
| 0 (today) | ✅ active | ✅ active | ✅ written | ❌ | points to `operations.jsonl` |
| 1 (dual-write) | ✅ active | ✅ active | ✅ written | ✅ written | points to both |
| 2 (agent reads new) | ✅ active | ✅ active | ✅ written | ✅ written | points to `spans.jsonl` |
| 3 (legacy removed) | ❌ removed | ❌ removed | ❌ no longer written | ✅ written | points to `spans.jsonl` |

- **Phase 1**: Add `OtelSpanTraceStore(TraceStore)` that implements `save()` by converting `OperationRecord` → OTel span. Both stores active. `TraceCollectorHook` unchanged.
- **Phase 2**: Update `format_send_ack` to point to `spans.jsonl`. Update agent prompts to read OTel format. Verify agent comprehension.
- **Phase 3**: Refactor `TraceCollectorHook` to construct OTel spans directly (drop `OperationRecord` construction). Remove `JsonFileTraceStore` and `OperationRecord`. `TraceStore` ABC refactored to `TraceQuery` ABC (read-only: `list_by_session` / `list_by_trace_id` over `spans.jsonl`).

**`FileSpanExporter` implementation**: A standard OTel `SpanExporter` subclass that serializes each span as a JSON line and appends to `{base_dir}/{session_id}/spans.jsonl`. Uses the existing `read_jsonl_robust` helper (`utils/file_io.py`) for resilient reading. This is the same pattern Rewind uses — local file + optional remote.

**Rationale**: OTel span format is a strict superset of `OperationRecord` information (adds `parent_span_id` for tree structure, standard `gen_ai.*` attributes for tooling compatibility). Agent self-read is preserved (agents read JSON, identify by field name — OTel attributes are more self-descriptive). Multi-exporter is OTel's native design, not a custom multi-store loop. The migration is phased to ensure agent comprehension is verified before legacy removal.

### D11 — Framework module with optional dependency, business opt-in via config

The observability/reproducibility/training-data subsystem lives in the **framework**
(`src/modex_agent/`), not in `examples/bot_project/`. It is **opt-in per business deployment**
via the existing `ObservabilityConfig` (already in `ioc/configs/observability.py`, currently
minimal — 10 lines with only `run_logging` + `level`).

**Config expansion** (extends existing `ObservabilityConfig`):

```python
class TraceBackend(str, Enum):
    OFF = "off"          # no tracing
    FILE = "file"        # local OTel span JSONL (framework default, zero deps)
    OTEL_HTTP = "otel_http"  # remote OTLP HTTP endpoint (business opt-in)

class CassetteScope(str, Enum):
    DEFAULT = "default"  # categories 1+2+6 (LLM + tools + retries)
    FULL = "full"        # all 6 categories

class ObservabilityConfig(BaseModel):
    # Existing (retained)
    run_logging: bool = True
    level: str = "INFO"
    
    # Trace Path (A)
    trace_backend: TraceBackend = TraceBackend.FILE
    otel_endpoint: str | None = None       # for OTEL_HTTP mode
    otel_service_name: str = "modex_agent"
    retain_reasoning_content: bool = True  # D3
    
    # Repro Path (B1/B2)
    checkpoint_per_iteration: bool = True  # D4
    cassette_enabled: bool = False         # D5, default off
    cassette_scope: CassetteScope = CassetteScope.DEFAULT
    
    # Training Data Derivation
    training_relevant: bool = False        # D6, default off
    training_max_iterations: int = 20      # L1 filter threshold
    training_max_tokens: int = 100000      # L1 filter threshold
```

**Dependency isolation** (`pyproject.toml`):

```toml
[project.optional-dependencies]
observability = [
    "opentelemetry-sdk>=1.28",
    "opentelemetry-exporter-otlp-proto-http>=1.28",
]
```

- `trace_backend=OFF` or `FILE`: no OTel import, zero overhead, zero external deps.
- `trace_backend=OTEL_HTTP`: requires `observability` extra; framework raises a clear
  `ImportError` with install instructions if missing.
- Cassette and training-data features have no external deps (pure stdlib + existing
  framework deps).

**Business usage** (`bot_config.yml`):

```yaml
observability:
  run_logging: true
  level: INFO
  trace_backend: file           # Tier 1: local (framework default)
  # trace_backend: otel_http    # Tier 2: Langfuse (business opt-in)
  # otel_endpoint: http://localhost:3000/api/public/otel
  checkpoint_per_iteration: true
  cassette_enabled: false
  training_relevant: true
```

**Framework vs business responsibility split**:

| Concern | Framework (`src/modex_agent/`) | Business (`examples/bot_project/`) |
|---|---|---|
| `TraceStore` / `OtelSpanTraceStore` ABC + impl | ✅ provides | — |
| `CheckpointHook(AfterIterationHook)` | ✅ provides | — |
| `CassetteRecorder` + replay engine | ✅ provides | — |
| `TrainingDataExporter` | ✅ provides | — |
| `ObservabilityConfig` schema | ✅ provides | — |
| YAML configuration | — | ✅ configures |
| Langfuse/Collector containers | — | ✅ deploys (Tier 2/3) |
| OTel SDK dependency install | — | ✅ `pip install -e ".[observability]"` |

**Rationale**: Matches the existing framework/examples separation (AGENTS.md architecture
rule 9). The framework already has `trace/` as a module and `ObservabilityConfig` as a
config section — this design extends them, not invents new layers. OTel as optional dep
matches the pattern of `pexpect`/`tmux`/`winpty` (platform-optional) and `aiosqlite`
(storage-optional).

### D12 — Cassette and training data are additive, never block existing trace

The existing trace mechanism (`TraceCollectorHook` → `JsonFileTraceStore` →
`operations.jsonl` → `format_send_ack` agent self-read) is the **baseline**. All new
capabilities are **additive layers** that can be independently disabled:

| Capability | Default | When disabled | Existing impact |
|---|---|---|---|
| OTel span dual-write (D10) | ON (Tier 1) | `trace_backend=OFF` | Legacy `operations.jsonl` still works |
| Per-iteration checkpoint (D4) | ON | `checkpoint_per_iteration=false` | Only approval-suspend snapshots (today's behavior) |
| Cassette (D5) | OFF | `cassette_enabled=false` (default) | No effect |
| Training tags (D6) | OFF | `training_relevant=false` (default) | No effect |
| Reasoning content in trace (D3) | ON | `retain_reasoning_content=false` | Trace has no reasoning; memory still strips |

**Worst case**: all new features off → behavior is byte-for-byte today's behavior (plus
the stale `HookPoint` docstring fix, which is cosmetic).

**Rationale**: The user's explicit requirement — "原版 trace 是本地化保存的, agent
可以自己读" — is preserved as the baseline. Every new layer is opt-in or default-on-with-
zero-impact. This follows the framework's existing pattern: approval is default-off,
memory is default-on but configurable, MCP is opt-in.

## Consequences

### Positive

- **Zero-process default**: Framework works standalone with no external services (D9). Business opts into Langfuse/Collector via YAML.
- **Industry-standard observability**: OTel `gen_ai.*` spans are portable across Langfuse, Phoenix, Datadog, Honeycomb. No vendor lock-in.
- **Legacy trace replaced, not retained**: `OperationRecord` / `operations.jsonl` is phased out in favor of OTel span format `spans.jsonl` (D10). Agent self-read preserved — agents read JSON by field name, and OTel attributes are more self-descriptive. Phased migration (dual-write → agent reads new → legacy removed) ensures safety.
- **Multi-exporter concurrent**: Local file (default-on) + remote OTLP (optional) write simultaneously via OTel's native `SpanProcessor` chain (D10). One span construction, N export destinations. Adding backends requires no framework code change.
- **Real reproducibility**: L4 cassette is the only proven approach for bit-identical replay of LLM agents (temperature=0 is not deterministic).
- **Free training data**: Derivation from existing traces adds zero write-path overhead. Approval-as-preference is a unique signal unavailable in most frameworks.
- **No graph-engine changes**: Per-iteration checkpoint reuses existing `AfterIterationHook` ABC and `HookRunner` dispatch. The engine stays agnostic.
- **Incremental adoption**: All new layers are additive (D12). L2 (checkpoint) is default-on at low cost. L4 (cassette) and training tags are opt-in. Existing behaviour is unchanged when disabled.
- **Framework/business separation**: Observability is a framework module with optional deps (D11). Business configures via YAML, deploys containers if Tier 2/3.
- **Subprocess tracing solved**: W3C `traceparent` env propagation is STABLE — Pi/OpenCode traces link to parent traces without modifying external agents.

### Negative

- **OTel GenAI semconv is `Development`**: will change. Mitigated by adapter layer (D7), but adapter maintenance is ongoing.
- **OTel SDK is an optional dependency**: business must `pip install -e ".[observability]"` for Tier 2/3. Tier 1 (local FILE) needs no OTel SDK — uses a lightweight built-in span serializer.
- **Legacy trace migration requires phased removal**: `OperationRecord` / `JsonFileTraceStore` / `operations.jsonl` are removed after a 3-phase migration (dual-write → agent reads new → legacy removed). During Phase 1-2, both files are written (storage overhead). Agent prompts may need updating to read OTel span format — verify comprehension before Phase 3.
- **OTel GenAI semconv is `Development`**: will change. Mitigated by adapter layer (D7), but adapter maintenance is ongoing.
- **OTel SDK is an optional dependency**: business must `pip install -e ".[observability]"` for Tier 2/3. Tier 1 (local FILE) needs no OTel SDK — uses a lightweight built-in span serializer.
- **L4 cassette is not bit-identical for external CLI agents**: subprocess internals are uncapturable. Marked `repro.incomplete=true`. L4 for external agents is shallow.
- **Cassette full-scope requires virtual clock + RNG injection**: all `time.time()` / `random.random()` / `secrets.token_hex()` calls must route through injection points. This is a non-trivial refactor of existing code — deferred to `repro.cassette=full`.
- **reasoning_content in traces increases storage**: reasoning may be longer than content. Mitigated by OTel tail sampling (drop long-reasoning spans for observability) and the Trace/Memory separation (Memory budget unaffected).
- **Training data privacy requires scope-aware export**: Multi-tenant memory scopes (Session/User/Tenant/Agent/Channel/Chat/Composite/Global) mean trace→training export must never cross tenant boundaries without opt-in. Hard filter in export adapter (pydantic-ai lesson from issue #2202).
- **`HookPoint` docstring is stale**: says "getattr dispatch" but implementation uses `isinstance`. Corrective edit needed (low cost, do alongside D4 implementation).

### Neutral

- Existing `TraceCollectorHook` is refactored (Phase 3) to construct OTel spans directly instead of `OperationRecord`. During Phase 1-2 it constructs both for dual-write safety.
- `OperationRecord` / `JsonFileTraceStore` / `operations.jsonl` are removed in Phase 3. `TraceStore` ABC is refactored to `TraceQuery` ABC (read-only query over `spans.jsonl`).
- The 3 unused `OperationKind` values (`APPROVAL` / `CONTROL_COMMAND` / `ERROR`) gain corresponding OTel span emissions (ATSC `human.review` / custom / error spans).
- `ObservabilityConfig` grows from 10 lines to ~25 lines — still a pure Pydantic data carrier, no logic (matches existing config pattern).
- `format_send_ack` (`result.py:70-74`) updated: Phase 1 points to both files, Phase 2+ points to `spans.jsonl` only.

## Implementation Notes (2026-07-17)

All 12 decisions (D1–D12) were implemented across 9 tickets (T0–T8) with 4280
tests passing. The following deviations from the original design were made
during implementation and post-implementation Oracle code review. They
supersede the original decision text where noted.

### IN1 — Root span deferred to `finally_turn` (supersedes D4 partial)

**Deviation**: The root `invoke_agent` span is pre-registered (span_id +
start_time) at `before_turn` but not written to `spans.jsonl` until
`finally_turn`. Child spans (chat, execute_tool) are written when they
complete, referencing the pre-registered root span_id via `parent_span_id`.

**Reason**: The store is append-only JSONL — spans cannot be updated after
writing. The original D4 text said "open at before_turn, close at
finally_turn," but this implies updating an existing span, which is impossible
in append-only mode. Deferring the root write to `finally_turn` captures full
duration + `stop_reason` + `error` in a single write.

**Side effect**: The root span appears LAST in `spans.jsonl` (after all
children), not first. Chronological readers may be surprised, but tree
reconstruction via `parent_span_id` is correct.

### IN2 — `retain_reasoning_content` enforced in store (clarifies D3)

**Deviation**: The hook (`TraceCollectorHook.after_llm_response`) always
records `gen_ai.output.reasoning_content` when the LLM provides it.
`OtelSpanTraceStore.save_span()` strips the attribute when
`retain_reasoning_content=False`, before both JSONL write and OTel emission.

**Reason**: The hook does not have access to `ObservabilityConfig`. The store
does (via constructor). Enforcing at the store level is the single chokepoint
that covers both local file and remote OTLP paths.

### IN3 — `_root_span_info` cleanup (adds to D4)

**Deviation**: `_root_span_info: dict[trace_id, (span_id, start_time)]` is
`pop()`-ed in `finally_turn` after writing the root span. The original D4 did
not mention cleanup, which would cause unbounded memory growth on long-running
agents.

### IN4 — `trace_enabled` flag for memory injection (adds to D9/D10/D12)

**Deviation**: When `trace_backend=off`, `SendDeps.trace_enabled=False` and
`SubagentAutoSendHook.trace_enabled=False` prevent injecting dead
`spans.jsonl` paths into `format_send_ack` text and `<trace>` XML
notifications. The original design (D9/D10/D12) did not consider that trace
paths are injected into agent communication — when tracing is disabled, these
paths point to files that will never be created, misleading the agent.

**Propagation path**:
`pool_builder._resolve_trace_enabled(app_config)` →
`_build_communication(trace_enabled=...)` →
`AgentCommunicationService(trace_enabled=...)` →
`SendDeps(trace_enabled=...)` →
`_subagent_trace_dir returns None` →
`format_send_ack skips trace line` / `SubagentAutoSendHook omits <trace> XML`

### IN5 — Factory uses `ObservabilityConfig` object, not individual fields (simplifies D11)

**Deviation**: `DefaultAgentFactory.__init__` takes a single
`observability_config: ObservabilityConfig | None` parameter instead of 4
individual fields (`checkpoint_per_iteration`, `training_relevant`,
`training_max_iterations`, `training_max_tokens`). The factory reads fields
from the config object internally.

**Reason**: The original D11 design would require 4 layers of parameter
threading for each new field. Passing the config object reduces this to 2
changes per new field (config + factory read).

### IN6 — No `getattr`/`isinstance` in new code (enforces rules 6, 9)

**Deviation**: `_resolve_trace_enabled` and `_resolve_cassette_config` in
`pool_builder.py` use typed `AppConfig` property access
(`app_config.observability`), not `getattr`. `_last_user_messages` in
`hooks.py` uses `msg.role`/`msg.content` directly (typed `ChatMessage`
objects from `to_list()`), not `isinstance(msg, dict)`.

### IN7 — Training exporter imports real semconv/store (fixes code duplication)

**Deviation**: The training exporter (`training_exporter.py`) imports
`GenAiAttr`, `SpanName`, `SpanStatusCode` from `modex_agent.trace.semconv`
and `SpanModel`, `SpanStatus`, `TraceQuery` from `modex_agent.trace.store`
directly. The original implementation duplicated the entire semconv layer
due to a stale "circular import" claim — verified no circular import exists
(`otel_store` → `store` is one-directional).

### IN8 — Known limitation: per-iteration checkpoint last-write-wins (limits D4)

**Limitation**: `TurnStateStore` keys by `TurnIdentity` (per-turn `turn_id`),
not per-iteration. Multiple `CheckpointHook.after_iteration` calls within the
same turn overwrite each other — only the last iteration's snapshot survives.
Full per-iteration history (PRD user stories #26, #27) requires a store
schema change (multi-snapshot-per-turn support). The `list_iteration_checkpoints`
helper is forward-compatible when the store supports it.

## Implementation Notes (2026-07-27) — Phase 2: Span gap remediation + Langfuse integration

A harness-engineering review (cross-referenced against the open-source
hermes-agent project and the OSS AI-observability landscape) identified that
the 2026-07-17 implementation has **5 blocking-level span gaps** where ticket
status claimed completion but code emission is absent or broken. Phase 2
remediates these gaps and confirms the Langfuse integration route. All
decisions below merge into the original D-numbers they affect; no new ADR
is created (per ADR governance: "merge refinements into the original").

### IN9 — OTLP-only route confirmed (reinforces D7, rejects SDK route)

**Decision**: Langfuse integration uses **OTLP-only** — the framework emits
standard `gen_ai.*` OTel spans via the existing `OtelSpanTraceStore` dual-path
(local `spans.jsonl` + optional OTLP export). Langfuse receives traces via its
native OTLP HTTP endpoint (`/api/public/otel`). The `langfuse` Python SDK is
**never** a framework dependency.

**Rejected alternative**: hermes-agent's plugin route (`LangfuseTraceHook`
using the `langfuse` SDK directly, parallel to OTel). Rejected because (a)
ModexAgent already has a complete OTel infrastructure (ADR-0024 D7/D10) —
hermes's SDK route exists because hermes has no native OTel; (b) the SDK
route creates double-write (two serialization paths), which D10 explicitly
opposed; (c) the SDK route loses the local `spans.jsonl` agent-self-read
path (D6), which is a harness-engineering advantage: the agent process can
read its own trace to drive runtime decisions (loop detection, error
recovery, predictive compression).

**Harness advantage of OTLP-only**: fixing the 5 span gaps (IN10) serves two
goals simultaneously — (1) Langfuse receives complete traces for human
analysis, (2) the local `spans.jsonl` becomes complete enough for the agent
to self-read in Phase 3 (`TraceDrivenLoopDetectorHook`, IN13). One fix, two
consumers. This dual-use is unique to the OTLP-only route.

**Configuration** (amended 2026-08-17, collector migration): `bot_config.yml`
points `otel_endpoint` at the local OTel Collector
(`http://localhost:4318/v1/traces`). `trace_backend=otel_http` (the default)
is OTel-only — OTLP via the collector to Langfuse; no local `spans.jsonl` is
written. `trace_backend=file` is the dormant no-network fallback (local jsonl
only; `otel_endpoint` is ignored in that mode).

**Langfuse deployment**: not bundled with the framework. The bot project
(`examples/bot_project/`) carries a Docker Compose reference + teaching
documentation. Framework code is Langfuse-agnostic.

### IN10 — Span gap remediation (fixes D4/D7/D10 implementation偏差)

Code audit found 5 blocking gaps where 2026-07-17 tickets claimed completion
but emission code is absent or broken. These are not new design — they are
the completion of already-designed ADR-0024 work.

| Gap | ADR claim | Code reality | Fix |
|-----|-----------|-------------|-----|
| **G1** | `chat` span covers LLM call | `chat` span written at `after_llm_response` with `end_time=None` — **LLM duration invisible** | New `BeforeLLMHook` ABC; `TraceCollectorHook` implements pre/post pair to capture `start_time` + `end_time` + `api_duration_s` |
| **G2** | `chat` span records LLM request | Only output recorded; no `gen_ai.request.model`, no input messages — **prompt content invisible** | `BeforeLLMHook` captures `gen_ai.request.model` + input messages via `PromptCaptureStrategy` (IN11). System prompt stored via `agent.start` span (IN18), not excluded |
| **G3** | `human.review` span covers approval (D7 ATSC draft) | `SpanName.HUMAN_REVIEW` defined but **never emitted** — `TraceCollectorHook` inherits no approval hook ABC. DPO export path broken (depends on approval = chosen/rejected) | New `AfterApprovalHook` ABC; `TraceCollectorHook` emits `human.review` span with decision + deny_reason |
| **G5** | D4 "per-iteration checkpoint via AfterIterationHook" | `TraceCollectorHook` does **not** implement `AfterIterationHook`; `CheckpointHook` does but stores snapshot, emits no span — **ReAct iteration boundaries invisible in trace** (flat span list under `invoke_agent`) | `TraceCollectorHook` implements `AfterIterationHook`, emits `iteration.start`/`iteration.end` boundary spans. Solves OTel Issue #94 (the problem D4 claimed to solve) |
| **G10** | D7 "agent.handoff span" | `send_to_agent` propagates traceparent but **emits no span** — multi-agent trace tree broken (child `invoke_agent` root has no parent link) | Emit `agent.handoff` span at `send_to_agent` dispatch point, linking parent turn to child turn |
| **G11** | D8 "subprocess trace propagation" | `otel_store._build_otlp_tracer` creates local `TracerProvider` but never calls `trace.set_tracer_provider()` — external coding agent CLIENT spans may not export | Call `trace.set_tracer_provider()` in `_build_otlp_tracer` |

**Additional attributes** (not gaps, but required for analysis):
- `gen_ai.usage.cache_read_input_tokens` + `gen_ai.usage.cache_creation_input_tokens` — **cache hit rate** (prompt-cache effectiveness, hermes prompt_caching.py reference)
- `gen_ai.request.model` on every `chat` span — **per-model performance breakdown**
- Tool `success`/`fail`/`error_type` attributes on `execute_tool` span — **tool correctness rate**
- `execute_tool_batch` span `end_time` — batch duration (currently missing)

### IN11 — PromptCaptureStrategy ABC (pluggable G2 capture scope)

**Decision**: G2's input-message capture is pluggable via a strategy ABC,
not hard-coded in `TraceCollectorHook`. This enables future capture-scope
tiers without modifying hook code.

```
src/modex_agent/trace/prompt_capture.py
├── PromptCaptureStrategy(ABC)              # extension point
│   └── capture(messages, model, *, tools, system_prompt) -> dict
├── OffPromptCapture                        # model name only, no content
├── HashPromptCapture                       # system prompt hash + length, no messages
├── SummaryPromptCapture (default)          # hash + length + last N messages truncated
│   # last N messages (default 6), each truncated to 2KB text / 1KB tool args
├── FullPromptCapture                       # full system prompt + tools + all messages
└── build_prompt_capture(config_value)      # factory: PromptCaptureMode → strategy
```

- `ObservabilityConfig.prompt_capture: PromptCaptureMode = "summary"` —
  accepts `off` / `hash` / `summary` / `full`, each backed by a
  `PromptCaptureStrategy` subclass (`OffPromptCapture`,
  `HashPromptCapture`, `SummaryPromptCapture`, `FullPromptCapture`).
- `ChatSpanHook` holds a `PromptCaptureStrategy` instance; calls
  `strategy.capture(...)` in `before_llm` to populate `chat` span's
  `gen_ai.request.messages` attribute.
- **All four strategies implemented**: `OffPromptCapture` (model name only),
  `HashPromptCapture` (system prompt hash + length, no messages),
  `SummaryPromptCapture` (default: hash + length + last N messages
  truncated), `FullPromptCapture` (full system prompt + tools + all
  messages untruncated).
- **System prompt**: stored via the `agent.start` span (IN18) emitted at
  `start_node_turn`. The `prompt_capture` config controls whether the
  system prompt is captured: `off` omits it entirely; `hash`, `summary`,
  and `full` all record `gen_ai.system_instructions` (full text),
  `gen_ai.system.prompt_hash` (SHA-256 first 16 chars), and
  `gen_ai.system.prompt_length` on the `agent.start` span. The original
  IN11 assumption that the system prompt is "per-turn identical" was
  incorrect: memory and experience injection modifies the system prompt
  at runtime, so per-turn capture has real analysis value. The
  `hash`/`summary`/`full` distinction applies to the `chat` span's input
  message capture; the `agent.start` span stores the full system prompt
  text whenever capture is not `off`.

**Rationale**: User requirement — "implementation must support future
extension / flexible switching to other tiers." ABC-first per architecture
rule 4/10. Deletion test: inlining truncation logic in the hook is shorter
but loses the extension point → ABC retained.

### IN12 — SQLite trace path deferred (OTel-only active path; FILE = no-network fallback)

**Decision**: **No SQLite trace store** is added. The former dual-path
retention (local `spans.jsonl` + optional OTLP export, "jsonl + OTLP dual
path retained") was superseded by the 2026-08-17 collector migration: the
active backend is OTel-only — OTLP via the local OTel Collector (contrib
0.158.0) to Langfuse (see the D6 storage-path amendment). Air-gapped
posture: trace-derived features (score injection, training-data export,
cross-session analysis) now require the Langfuse stack; `trace_backend=off`
still runs the agent (untraced, zero overhead); FILE mode remains the
no-network fallback for offline tracing needs (dormant, selectable).

**Considered and rejected**: A SQLite trace path (third `TraceQuery`
implementation alongside `JsonlSpanQuery`) was considered for cross-session
SQL aggregation. Rejected because: (a) agent self-read is satisfied by
`JsonlSpanQuery.list_by_session()` / `list_by_trace_id()` (FILE mode) and
the store's in-process buffer (otel_http mode); (b) human
cross-session analysis is satisfied by Langfuse's dashboard/score/dataset;
(c) a SQLite trace store is an ADR-level schema decision (coexist with
State DB? independent DB? migration strategy?) that doesn't unlock new
harness capabilities; (d) the only beneficiary is air-gapped cross-session
SQL analysis without Langfuse, which can be met by an offline
`jsonl → SQLite` import script over FILE-mode data.

**Code annotation**: `ObservabilityConfig.trace_backend` and
`OtelSpanTraceStore` carry comments documenting this deferral and the
future `TraceQuery` ABC extension point for SQLite if the air-gapped
analysis scenario materializes.

### IN13 — TraceDrivenLoopDetectorHook placeholder (Phase 3, not registered)

**Decision**: A `TraceDrivenLoopDetectorHook` class is added to
`src/modex_agent/hook/builtin/` as a **placeholder** — class body contains
only docstring + `name` property + `pass`. It is **not registered** in any
factory and has **no configuration** and **no tests**.

**Purpose**: Documents the Phase 3 harness-engineering entry point — an
agent-self-read hook that consumes the `TraceQuery` read path (store buffer
same-process / `LangfuseTraceQuery` cross-process; `JsonlSpanQuery` in the
dormant FILE fallback) to drive runtime decisions (loop detection via
tool-call fingerprint hashing, error
recovery strategy selection, predictive compression). Implementation is
deferred until Phase 2 data is available to observe actual agent behavior
patterns (where does the agent loop? where do tools fail? what's the
latency breakdown?). Reference: hermes-agent's `chat_completion_helpers.py`
stale-call circuit breaker (#58962) and `error_classifier.py` 27-reason
taxonomy are the migration targets, but their strategies should be
calibrated against real trace data first.

**Inheritance**: `AfterToolExecutionHook` + `AfterIterationHook` (the two
hook points a loop detector needs). Not `BeforeToolExecutionHook` —
detection happens after execution, intervention happens at the next
iteration boundary.

### IN16 — Phase 3 direction: harness intelligence via trace-driven decisions

**Status: FUTURE** — not yet planned for implementation. Recorded here to
preserve direction; detailed design will be written when Phase 2 data is
available to calibrate strategies.

**Current state after Phase 2 (amended 2026-08-17, collector migration):**
Traces are complete enough for both human analysis and agent self-read. The
division of labor is now fixed: the framework emits (non-blocking OTLP via
the local collector); the collector provides retry/buffer reliability
(outage redelivery); Langfuse is the system of record and the analysis
surface; cross-process reads go through `LangfuseTraceQuery` (Langfuse v2
API), while same-process metric readers use the session counters and the
per-turn `TRAJECTORY_METRICS` stash (the store's in-process buffer was
removed by the 2026-08-18 write-only refactor).
The `TraceDrivenLoopDetectorHook` placeholder exists (IN13).
No trace-consuming harness logic exists yet.

**Four directions** (reference: hermes-agent cross-reference):

1. **Loop/stuck detection** — implement `TraceDrivenLoopDetectorHook`
   consuming the `TraceQuery` read path: tool fingerprint hashing, output
   similarity, oscillation, stale-call circuit breaker. Hermes #58962
   incident (494 consecutive failures, 3+ days) shows the cost of not
   having this.

2. **Error recovery taxonomy** — replace overflow-only `error_recovery.py`
   with classification-driven recovery (rate limit / billing / content
   policy / network / overflow / SSL / thinking-signature). Hermes
   `error_classifier.py` 27-reason taxonomy is the reference.

3. **Truth enforcement** — verification gate in `EndNode`: intercept "code
   edit without verification", force test/lint/build. Hermes
   `verification_stop.py` + `verification_evidence.py` SQLite ledger is the
   reference. Highest-ROI intelligence boost.

4. **Experience review upgrade** — upgrade `ExperienceReviewAgent` to
   hermes `background_review.py` pattern: fork-daemon with prefix cache
   inheritance, tool whitelist, trace-driven context.

**Prerequisite:** Phase 2 must run in production to observe real agent
behavior patterns. Strategies must be calibrated against data, not
implemented blind. This is why Phase 3 is FUTURE, not PLANNED.

### IN14 — trace_backend tier refinement deferred

**Decision**: The existing three tiers (`off` / `file` / `otel_http`) are
retained. Future refinement tiers are **documented in code comments** but
not implemented:

- `file_lite` — local JSONL, excludes G2 request prompt + reasoning content
  (structured attributes only). Lowers IO for always-on local tracing.
- `otel_only` — OTLP export only, no local JSONL. Saves local IO when
  Langfuse is the analysis surface.
- `file_debug` — local JSONL + full prompt + reasoning content
  (cassette-redundant). Troubleshooting only.

**Rationale**: The three existing tiers cover 90% of scenarios. `spans.jsonl`
is session-isolated (`<session_id>/spans.jsonl`) and cleaned by
`SessionArtifactCleaner` on session deletion, so long-term accumulation
pressure is bounded. The real IO lever is G2's prompt truncation threshold
(IN11), not a new tier. Tier refinement is low priority.

### IN15 — Langfuse as analysis surface, not framework dependency

**Deployment boundary**: Langfuse deployment (Docker Compose, config,
dashboards) lives in `examples/bot_project/` as teaching documentation, not
in the framework. The framework's only Langfuse-aware code is
`OtelSpanTraceStore`'s OTLP exporter (which is vendor-neutral — any OTLP
endpoint works).

**Analysis workflow** (documented in bot, not coded in framework):
1. `bot_config.yml` sets `otel_endpoint` to Langfuse OTLP URL
2. Langfuse receives `gen_ai.*` spans, renders trace tree (turn → iteration
   → LLM call → tool execution, with `agent.handoff` linking multi-agent)
3. Cache hit rate = `cache_read_input_tokens / total_input_tokens` per
   `chat` span — Langfuse dashboard aggregation
4. Tool correctness rate = `success` count / total per `execute_tool` span
   — Langfuse dashboard aggregation
5. Trajectory analysis = trace tree visualization (per-iteration boundary
   spans make ReAct structure visible)
6. Flagged traces (circuit-breaker trip / max_iterations / consecutive tool
   errors / P90+ iteration count) → Langfuse dataset → eval (DeepEval /
   Inspect AI, Phase 3+)

**Framework delivers data; Langfuse delivers analysis.** The framework
does not consume its own trace for analysis — that is Langfuse's job. The
framework's self-read path (D6 `spans.jsonl`) is reserved for Phase 3
harness runtime decisions (IN13), not analysis.

### IN17 — OTel GenAI semconv compliance

**Decision**: All trace attributes follow the
[OpenTelemetry GenAI semantic conventions](https://github.com/open-telemetry/semantic-conventions-genai).
Attribute names use the standard `gen_ai.*` namespace with correct
hierarchy (e.g. `gen_ai.usage.cache_read.input_tokens`, not
`gen_ai.usage.cache_read_input_tokens`). Required attributes
(`gen_ai.operation.name`, `gen_ai.provider.name`) are emitted on every
span. Message content uses the OTel parts-based format
(`{role, parts: [{type, content}]}`).

**Dual emission for Langfuse compatibility**: Langfuse maps `input`/`output`
from `gen_ai.prompt` / `gen_ai.completion` (legacy names), not from the
current OTel standard `gen_ai.input.messages` / `gen_ai.output.messages`.
Both are emitted:
- `gen_ai.input.messages` (OTel standard, parts-based) — for standards-compliant OTLP consumers
- `gen_ai.prompt` (Langfuse legacy, JSON string) — for Langfuse `input` field
- `gen_ai.output.messages` (OTel standard, parts-based) — for standards-compliant OTLP consumers
- `gen_ai.completion` (Langfuse legacy, string) — for Langfuse `output` field

Langfuse trace-level fields mapped via `langfuse.*` namespace:
- `langfuse.session.id` — full session ID (subagents are independent sessions)
- `langfuse.user.id` — from `metadata['user_id']`, falls back to `"default"` (temporary, framework has no first-class user concept)
- `langfuse.observation.type` — `"generation"` on LLM spans

**OTLP trace tree**: `_emit_span_via_otel_sdk` injects `SpanContext` with
the framework's `trace_id` and `parent_span_id` via `NonRecordingSpan` +
`set_span_in_context`, so the OTel SDK exports linked trace trees. Span IDs
are truncated to 16 hex chars (OTel 64-bit; framework uses 32-char UUID hex).

**Reference docs**: `docs/otel/README.md` (attribute audit),
`docs/langfuse/README.md` (Langfuse OTLP mapping).

### IN18 — Span structure redesign (supersedes IN1 root span emission, IN10 hook table)

**Status**: implemented (2026-08-14). The single `TraceCollectorHook` that
handled all span emission was decomposed into 7 specialized hook classes,
assembled by a factory based on a tiered verbosity config. This section
documents the structural changes; attribute-level details live in
`docs/otel/README.md` and `docs/otel/spans.yaml`.

**Hook composition**: 7 hook classes replace the single `TraceCollectorHook`.
Each hook owns one span type and inherits the hook ABC(s) for its lifecycle
point(s). `build_trace_hooks()` (in `trace/factory.py`) assembles the list
from `ObservabilityConfig`:

| Hook class | Inherits | Span(s) emitted | Tier |
|---|---|---|---|
| `RootSpanHook` | `StartNodeTurnHook`, `FinallyGraphHook` | `invoke_agent` | all |
| `ChatSpanHook` | `BeforeLLMHook`, `AfterLLMResponseHook` | `chat` | standard, full |
| `ToolSpanHook` | `BeforeToolExecutionHook`, `AfterToolExecutionHook` | `execute_tool_batch` + `execute_tool` | standard, full |
| `HandoffSpanHook` | `AfterToolExecutionHook` | `agent.handoff` | standard, full |
| `ApprovalSpanHook` | `AfterApprovalHook` | `human.review` | standard, full |
| `AgentStartSpanHook` | `StartNodeTurnHook` | `agent.start` | full only |
| `IterationSpanHook` | `BeforeIterationHook`, `AfterIterationHook` | `iteration.start` + `iteration.end` | full only |

One `TraceSessionState` is shared across every hook in a single
`build_trace_hooks` call so child spans can resolve the root span ID seeded
by `RootSpanHook`. Registration order is execution order (`HookRunner`
dispatches in registration order); `RootSpanHook` is always first, and
`ToolSpanHook` precedes `HandoffSpanHook` so the batch span the handoff
parents to exists by the time the handoff hook reads it. Each hook is wrapped
in a `HookSpec` with `HookErrorPolicy.LOG` so a failing trace hook logs and
continues rather than crashing the agent.

**v4 immutability fix** (supersedes IN1): The root `invoke_agent` span is
no longer emitted twice. `RootSpanHook.start_node_turn` pre-registers the
span (stores `span_id` + `start_time` in `TraceSessionState.root_span_info`,
captures the trigger message) without emitting anything.
`RootSpanHook.finally_graph` emits one complete span with input + output +
aggregated usage + `end_time` + `stop_reason` + error status, then cleans
up the session state. This fixes Langfuse v4 immutability: v4 does not
merge two spans by `span_id` the way v3 did, so the old double-emission
pattern (start at `before_turn`, complete at `finally_turn`) produced two
separate observations instead of one merged root span. Single emission is
the correct approach for both v3 and v4.

**`agent.start` span**: Emitted by `AgentStartSpanHook` at `start_node_turn`,
fresh turn only (same hook point as `RootSpanHook`, runs after it because
`RootSpanHook` is registered first). Carries the system prompt and tool
definitions:

- `gen_ai.system_instructions`: full system prompt text (when
  `prompt_capture != off`)
- `gen_ai.system.prompt_hash`: SHA-256 first 16 chars (when
  `prompt_capture != off`)
- `gen_ai.system.prompt_length`: character count (when
  `prompt_capture != off`)
- `gen_ai.tool.definitions`: full tool definitions (when `capture_tools =
  True`)

This span is `FULL` tier only because system prompt capture is
analysis-heavy and not needed for standard observability.

**Subagent trace linking**: When a parent agent calls `send_to_agent` or
`task`, `HandoffSpanHook` emits an `agent.handoff` span and stores its
`span_id` in `TurnCustomKey.HANDOFF_SPAN_ID`. The child agent receives
`trace_id` and `parent_span_id` (the handoff span ID) via the
`input_metadata` envelope. `TurnContextBuilder.build_runtime_and_context()`
propagates these into `TurnCustomKey.TRACE_ID` and
`TurnCustomKey.PARENT_SPAN_ID` on the child's turn state.

The child's `RootSpanHook.start_node_turn` reuses the inherited `trace_id`
(same trace, not a new one) and creates a fresh `root_span_id`. The child's
`RootSpanHook.finally_graph` emits its `invoke_agent` root span with
`parent_span_id` set to the parent's handoff span ID, linking the two turns
into a single trace tree. When `parent_span_id` is set,
`langfuse.session.id` is set to `ctx.session.parent_session_id` so both
parent and child traces group under the same Langfuse session. This creates
a visual parent to child trace tree in the Langfuse UI.

**Iteration symmetry**: `IterationSpanHook` emits a symmetric pair:
`iteration.start` at `before_iteration` (marks begin, zero-duration) and
`iteration.end` at `after_iteration` (carries measured duration). Both are
parented to the root span. `AFTER_ITERATION` fires at current-iteration-end,
not next-iteration-start, so `state.iteration` is still the current value
in `after_iteration`. The old `iteration_number -= 1` hack is removed: no
decrement is needed because the iteration counter has not yet advanced
when `after_iteration` fires.

**Tier config**: Two new config fields control span verbosity and prompt
capture scope:

- `trace_spans: TraceSpanMode` — `minimal` (RootSpanHook only, 1 hook),
  `standard` (root + chat + tool + handoff + approval, 5 hooks, default),
  `full` (standard + agent_start + iteration, 7 hooks).
- `prompt_capture: PromptCaptureMode` — `off` (no prompt content),
  `hash` (system prompt hash + length only), `summary` (hash + length +
  last N messages truncated, default), `full` (full system prompt +
  tools + all messages untruncated). See IN11 for the system prompt
  capture behavior.
- `capture_tools: bool` — when `True`, `gen_ai.tool.definitions` is
  included on the `agent.start` span. Default `False`.

### IN19 — Score injection wiring, Langfuse rc.3 surface, Layer-2 eval v2

**Decision** (2026-08-15): three completions extending D5/D6/IN11 into the
eval capability:

1. **Score injection live at `RootSpanHook`** — the injector dead-end was
   wired in `finally_graph` (after root-span persistence, before
   `TraceSessionState.clear_trace`); injection attaches with
   `observation_id=root_span_id`; failures are warning-only.
   (Amended 2026-08-18, write-only refactor: the metrics now come from the
   session's scalar `MetricCounters` via
   `read_metrics(trace_id, root_span_id)` — no span read-back, no subtree
   extraction — and `inject_scores` takes a `TrajectoryMetrics`, not spans.
   The hook also stashes the metrics on `TurnCustomKey.TRAJECTORY_METRICS`
   before `clear_trace` for same-process readers.)
2. **Langfuse v4.0.0-rc.3 API surface** — `/api/public/v2/observations` is
   the only live query surface (`/api/public/traces`, `/api/public/v2/traces`,
   `/api/public/v2/scores` all 404), so curation derives agent-turn summaries
   from root `AGENT` observations; scores post via `/api/public/ingestion`.
3. **Layer-2 eval v2** — bot-side harness (`examples/bot_project/bot/eval/`):
   frozen multi-turn task schema (typed toolsets, per-case deny lists,
   discriminated world assertions), clean/production harness modes, and a
   cassette golden gate with four replay gates (replay checks
   `CassetteReplayEngine.misses` because `ReActAgent.run` converts provider
   lookup errors into an error stop, not an exception).
