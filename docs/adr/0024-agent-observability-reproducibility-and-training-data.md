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
