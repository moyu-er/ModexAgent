# ADR-0034: Graph Engine Phase c Preliminaries

Status: accepted (2026-07-21) — design completed via `/grill-with-docs`;
implemented via `/subagent-implement` (tickets 01–06 landed, whole-effort
review approved). See D1/D2/D3 for the three decisions; "Implementation
order" for the staging.

Supersedes: nothing. Refines ADR-0033 (D5.2 `around` scope; D14 dataclass
codec). Phase c items (parallel fan-out, subgraph nesting, additional
channels, BSP superstep) remain deferred per ADR-0033 D12 — this ADR does
not start Phase c; it clears the prerequisites.

## Context

ADR-0033 shipped Phase a: `modex_graph` extracted as a standalone package,
ReAct migrated to a 4-node graph, per-channel checkpoint infrastructure
landed, `GraphRuntime` AOP bridge established. A `/grill-with-docs` session
on 2026-07-21 surfaced three findings that block clean Phase c work:

### Finding 1 — Per-channel checkpoint is silently broken (TD#4)

`GraphState.checkpoint()` / `from_checkpoint()` (the per-channel codec
path that ADR-0033 D14 promised would replace 230 lines of hand-written
`ReActSnapshotPolicy._build_payload`) is **not exercised by ReAct in
production**. `ReActTurnState` overrides both methods to use Pydantic
`model_dump(mode="json")` / `model_validate()` (state.py:134–151). The
override exists because two codec gaps make the per-channel path lossy:

1. **PEP 604 unions** (`X | None`). `decode_value` (channel.py:166) only
   checks `origin is typing.Union`, not `types.UnionType`. Five
   `ReActTurnState` fields use `T | None` syntax and fall through to the
   `return data` (as-is) branch — which happens to work for `None` but
   breaks for non-`None` payloads that need type-coerced decode (e.g.
   nested `BaseModel` inside a union).

2. **stdlib `@dataclass`**. `encode_value` (channel.py:122) recognises
   Pydantic `BaseModel` subclasses but not stdlib dataclasses. Six
   `ReActTurnState` field types are stdlib dataclasses
   (`TurnIdentity`, `LLMResponse`, `AgentResult`, `MessageDelta`,
   `OperationState`, `CancellationState`); they fall through to
   `str(value)` — **lossy**. `TurnIdentity` nests a Pydantic `BaseModel`
   (`SessionInfo`), so the loss is real, not hypothetical.

A third, related defect: `react/codec.py` (50 lines) registers five
Pydantic `BaseModel` types via `register_codec(...)`. These registrations
are **dead code** — `encode_value` checks `isinstance(value, BaseModel)`
*before* consulting `_find_codec`, so registered `BaseModel` codecs are
never invoked. The file's own docstring (lines 14–18) admits "Stage 1
status: registrations exist but are NOT referenced by the graph engine
yet" — Phase a Stage 2 wired the override path, the file was never
cleaned up.

Net effect: ADR-0033 D14's "per-channel codec replaces hand-written
flattening" is **aspirational, not delivered**. The per-channel
infrastructure exists, is unit-tested in isolation
(`tests/unit/modex_graph/test_channels.py`), but the production ReAct
path bypasses it. Any Phase c work that touches channels (parallel
fan-out's multi-write detection; `ReducerChannel`-based state merge;
subgraph state isolation via `ctx.fork`) builds on a path that has never
carried real ReAct state.

### Finding 2 — AOP routing is split, and the split is undocumented (TD#3)

`ReactGraphRuntime.around(scope, ctx, body)` is the AOP entry point
declared on the `GraphRuntime` ABC. ReAct uses it for exactly **one**
scope: `ITERATION` (dispatched from `nodes/llm.py:208`). The other two
ReAct AOP scopes — `TOOL_CALL` and `LLM_STREAM` — **do not go through
`around`**:

- `TOOL_CALL`: `tool_executor.py:39` calls
  `interceptor_chain.around_tool_call(ctx, call_ctx, _actual)` directly.
- `LLM_STREAM`: `llm_client.py:143` calls
  `interceptor_chain.around_llm_stream(context, stream_ctx, _actual_stream)`
  directly.

`ReactGraphRuntime.around` has pass-through branches for `TOOL_CALL` /
`LLM_STREAM` / `LLM_CALL` (runtime.py:158–181) that simply do
`return await body()` — **dead code, never invoked**.

The split is not arbitrary. `ToolCallContext` requires
`tool_call` / `tool_name` / `arguments` — data that exists only inside
`ToolNode`'s execution scope, not in `GraphContext.user_data`
(`AgentContext`) or `GraphContext.state` (`ReActTurnState`). Similarly
`LLMStreamNext = Callable[[], AsyncIterator[LLMStreamChunk]]` is an async
iterator, not a coroutine — `around`'s `body: Callable[[], Awaitable[Any]]`
signature **cannot express it**. These are not implementation oversights;
they reflect that typed interceptor contexts are node-local data that
cannot be lifted to the graph-runtime layer without either (a) violating
invariant 1 (`modex_graph` has zero `modex_agent` imports —
`ToolCallContext` / `LLMStreamContext` live in `modex_agent.interceptor.abc`)
or (b) adding a pure forwarding shell that fails the ADR-0007 deletion
test.

But the split is **invisible from the code**: a reader sees `around` with
three scope branches and reasonably assumes all three are wired. The
pass-through dead code actively misleads. Phase c's second graph consumer
would inherit this confusion.

### Finding 3 — `modex_graph`'s large unused API surface is appropriate

ADR-0033 shipped a broad public API: `Graph`, `Node`, `CompiledGraph`,
`GraphEngine`, `GraphNode`, `Command`, `Task`, `NodeResult`,
`GraphContext`, `GraphRuntime`, `GraphState`, `BaseChannel`,
`LastValue`, `ReducerChannel`, `register_codec`, `Codec`, and the
`GraphBubbleUp` exception family (`GraphInterrupt`, `GraphDrained`,
`ParentCommand`). A usage audit on 2026-07-21 found that ReAct — the sole
consumer — exercises roughly 40% of this surface. The remainder
(`Command.goto` in its `list[Task]` form, `add_conditional_edges`,
`CompiledGraph`-as-`Node`, `GraphDrained`, `ParentCommand`,
`ReducerChannel` in production state, `ctx.fork` outside the engine's
internal `Task` execution) is **declared, unit-tested in
`tests/unit/modex_graph/`, but unused by any production code**.

This looks like dead code by ADR-0007's standard ("keep zero-usage deep
modules with real seams" — the rule that extracting a seam needs two real
consumers, not one). But ADR-0007 governs **modules inside
`modex_agent`** — seams between sibling framework modules where
speculative abstraction creates real maintenance cost. `modex_graph` is a
**different situation**: a newly extracted standalone *package* whose job
is to be a reusable graph engine. A package's public API is its contract
with future consumers; providing a complete, coherent, well-tested
surface is the point, not premature abstraction. PocketFlow (a 100-line
reference graph engine) and LangGraph (the langchain-incubated graph
engine) both ship broad public APIs that exceed any single consumer's
usage — this is normal for a library.

Conflating "`modex_graph` has unused API" with "ADR-0007 violation" would
lead to deleting `Command.goto=list[Task]`, `CompiledGraph`-as-`Node`,
`ReducerChannel`, etc. — only to re-add them when Phase c's parallel
fan-out, subgraph nesting, or multi-write detection needs them. The
correct standard is: **does the API form a coherent, internally
consistent contract?** If yes, unused-by-ReAct is fine. If an API element
is *incoherent* (e.g. `react/codec.py`'s dead registrations that can
never be invoked), that is a defect to fix — but the defect is the
incoherence, not the unused-by-ReAct-ness.

## Decisions

### D1 — Repair per-channel checkpoint in two stages (TD#4)

**Stage 1 (this ADR's scope):** make the per-channel path correct and
remove the override.

1. `decode_value` (channel.py:166): extend the union check to
   `origin is Union or origin is types.UnionType`. PEP 604 `X | None`
   and `typing.Optional[X]` now behave identically.
2. `encode_value` (channel.py:122): after the `BaseModel` check and
   before `_find_codec`, add a stdlib-dataclass branch using
   `pydantic.TypeAdapter(type(value)).dump_python(value, mode="json")`.
   `TypeAdapter` natively handles stdlib dataclasses including nested
   `BaseModel` fields, nested dataclasses, `StrEnum` fields, `Optional`,
   and PEP 604 unions.
3. `decode_value` (channel.py:151): symmetric stdlib-dataclass branch
   using `TypeAdapter(field_type).validate_python(data)`.
4. Module-level `_TYPE_ADAPTERS: dict[type, TypeAdapter]` cache to avoid
   repeated construction.
5. Delete `ReActTurnState.checkpoint()` and `from_checkpoint()` overrides
   (state.py:134–151). `ReActTurnState` now uses `GraphState`'s
   per-channel path — the same path `tests/unit/modex_graph/test_channels.py`
   already validates.
6. Delete `react/codec.py` entirely. Its five `BaseModel` registrations
   are unreachable (per Finding 1) and become unnecessary once the
   dataclass branch handles `TurnIdentity` etc. via `TypeAdapter`.
7. Round-trip regression test in
   `tests/unit/agents/react/test_snapshot_round_trip.py` covering all
   13 `ReActTurnState` fields, including PEP 604 unions, nested
   dataclasses, nested `BaseModel` (`SessionInfo` inside `TurnIdentity`),
   `StrEnum` fields, and `list[dataclass]` (`message_delta`,
   `operations`, `tool_batches`).

**Stage 2 (deferred — tracked as a follow-up, not in this ADR's
implementation scope):** migrate the six stdlib dataclass value objects
(`CancellationState` → `OperationState` → `MessageDelta` →
`LLMResponse` / `AgentResult` → `TurnIdentity`) to Pydantic `BaseModel`.
This resolves the ADR-0033 D14 violation (`TurnIdentity` is a frozen
`@dataclass` that crosses module boundaries and carries nested
serialization — D14 restricts frozen `@dataclass` to single-module leaf
value objects with no nested validation). Once all six are `BaseModel`
subclasses, the `TypeAdapter` branch in `encode_value` / `decode_value`
becomes dead and is deleted, leaving a single serialization path
(`isinstance(value, BaseModel)` → `model_dump` / `model_validate`).

Stage 2 is its own ADR because each migration touches high-blast-radius
types (`TurnIdentity` has 203 callers) and must be validated
independently. Stage 1 unblocks Phase c without waiting for Stage 2
because the `TypeAdapter` transition makes the per-channel path correct
for all current `ReActTurnState` field types.

**Rejected alternatives:**

- **Path A (PEP 604 fix only, keep override).** Leaves ReAct on the
  `model_dump` path forever; per-channel infrastructure remains
  theoretical. Two serialization paths in perpetuity is not a complete
  state.
- **Path B (PEP 604 fix + remove override, skip dataclass fix).** Breaks
  approval snapshot round-trip: the six dataclass fields would go
  lossy via `str(value)` once the override is gone. Introduces a new
  bug while pretending to fix one.
- **Direct to Stage 2 (migrate all dataclasses first, TD#4 disappears
  naturally).** Correct end state, but blocks the Phase c prerequisite
  on a 1–2 week migration with high blast radius. Stage 1's
  `TypeAdapter` transition is the bridge.

### D2 — Document the AOP split; delete the dead pass-through (TD#3)

Accept that ReAct has **two AOP routing paths**, and that this is a
design fact, not a defect to unify:

1. **ITERATION** goes through `ctx.runtime.around(ReActScope.ITERATION,
   ctx, body)`. `IterationContext` can be constructed from
   `ctx.user_data` (`AgentContext`) + `ctx.state` (`ReActTurnState`) —
   both available at the graph-runtime layer. The `body: Callable[[],
   Awaitable[Any]]` signature matches `IterationNext`.
2. **TOOL_CALL** and **LLM_STREAM** go through `InterceptorChain`
   directly (`tool_executor.py:39`, `llm_client.py:143`). Their typed
   contexts (`ToolCallContext` / `LLMStreamContext`) carry node-local
   data (`tool_call`, `tool_name`, `arguments`, `LLMStreamNext` async
   iterator) that does not exist at the graph-runtime layer. Lifting
   them would require `GraphRuntime.around` to accept
   `modex_agent.interceptor.abc` types — violating invariant 1
   (`modex_graph` has zero `modex_agent` imports) — or adding a pure
   forwarding shell in `ReactGraphRuntime` that fails the ADR-0007
   deletion test.

**Implementation:**

1. Delete the `TOOL_CALL` / `LLM_STREAM` / `LLM_CALL` pass-through
   branches in `ReactGraphRuntime.around` (runtime.py:158–181). Only
   `ITERATION` remains. The deleted branches are dead code (never
   invoked — `tool_executor` and `llm_client` bypass `around` entirely).
2. Update `ReactGraphRuntime.around` docstring: "`around` routes
   `ITERATION` only. `TOOL_CALL` and `LLM_STREAM` are node-local AOP
   invoked directly via `InterceptorChain` because their typed contexts
   are not constructible from `GraphContext`."
3. Add comments at `tool_executor.py:39` and `llm_client.py:143`:
   "Canonical AOP path for `TOOL_CALL` / `LLM_STREAM`.
   `ctx.runtime.around` is for `ITERATION` only — see ADR-0034 D2."
4. Update ADR-0033 D5.2: refine "`around` routes all scopes" to
   "`around` routes `ITERATION` only; `TOOL_CALL` and `LLM_STREAM` are
   node-local." This is a precision refinement, not a reversal.
5. Update `CONTEXT.md` `GraphRuntime` entry to match.
6. `ReActScope.TOOL_CALL` / `LLM_STREAM` / `LLM_CALL` enum values:
   **retain as informational** (they document that these scopes exist
   as AOP concepts, even though they are not routed through `around`).
   The enum is the canonical vocabulary for AOP scopes; deleting values
   would lose information without simplifying anything.

**Rejected alternatives:**

- **Option A — extend `GraphRuntime.around` signature with a typed
  `call` parameter.** Violates invariant 1: `ToolCallContext` /
  `LLMStreamContext` live in `modex_agent.interceptor.abc`; importing
  them into `modex_graph`'s ABC breaks the package boundary.
- **Option B — add `around_tool_call` / `around_llm_stream` methods to
  `ReactGraphRuntime` (not on the ABC).** Passes the deletion test
  poorly: the methods would be pure forwarders to
  `InterceptorChain.around_tool_call` / `around_llm_stream` with no
  added abstraction. They exist only to make `ctx.runtime` the single
  AOP entry point — a cosmetic uniformity that costs an indirection.

### D3 — `modex_graph` API surface is governed by coherence, not by ReAct usage

`modex_graph`'s public API may include capabilities that no production
consumer currently exercises, provided each capability:

1. Forms part of a **coherent contract** — the API elements compose
   sensibly (e.g. `Command.goto=list[Task]` + `Task(state=...)` +
   `ctx.fork(state=...)` + `ReducerChannel` form a complete fan-out /
   fan-in story).
2. Is **unit-tested in isolation** — every public API element has a
   covering test in `tests/unit/modex_graph/` that exercises its
   behavior independent of any production consumer.
3. Is **not internally contradictory** — e.g. `react/codec.py`'s five
   `register_codec` calls for `BaseModel` types are contradictory because
   `encode_value` checks `isinstance(value, BaseModel)` before
   `_find_codec`, making the registrations unreachable. Contradictory
   API elements are defects to fix (D1 step 6 deletes the file); unused
   but coherent API elements are not.

This **does not supersede ADR-0007**. ADR-0007 governs seams between
sibling modules inside `modex_agent` — there, speculative abstraction
creates real maintenance cost (more files to understand, more import
paths to track, deeper call chains). `modex_graph` is a standalone
package whose purpose is to be a reusable engine; its API surface is the
contract with future consumers, not an internal seam. The two standards
are different because the situations are different.

**Concrete consequence:** the following `modex_graph` API elements are
**retained** despite zero production usage (all are unit-tested in
`tests/unit/modex_graph/`):

- `Command(goto=list[Task])` — `test_routing.py::TestCommandGotoListTask`
- `Command(goto=list[str])` — `test_routing.py::TestCommandGotoListStr`
- `add_conditional_edges` — `test_engine_topologies.py::TestConditionalBranch`
- `CompiledGraph`-as-`Node` — `test_subgraph.py::TestSubgraphAsNode`
- `ReducerChannel` — `test_channels.py::TestReducerChannelCheckpoint`
- `ctx.fork(state=...)` — exercised by `test_routing.py`'s `Task` tests
- `GraphDrained` / `ParentCommand` exception classes —
  `test_exceptions.py::TestEngineDoesNotSwallow` (propagation only;
  raise-site is a Phase c decision)

**Workstream (C) — `examples/graph_patterns/`:** to exercise the
retained API surface with realistic compositions and prove the engine
expresses non-ReAct shapes, add three pattern modules under
`examples/graph_patterns/` (each with a covering unit test under
`tests/unit/examples/graph_patterns/`):

1. `conditional.py` — `ConditionalNode[S](predicate: Callable[[S], str])`
   returns `NodeResult(transition=predicate(state))`. Example graph:
   if/else branch + merge.
2. `retry.py` — `RetryNode[S](body, max_retries, is_failure)` retries
   `body` synchronously in one `execute` call; companion
   `build_retry_graph(body_node, max_retries)` builds a topology-retry
   graph (self-loop + `transition="retry"|"success"|"failed"`).
3. `map_reduce.py` — `MapNode[S](items_fn, worker_node, state_fn)`
   emits `Command(goto=[Task(node=worker_node, state=...)])` for
   fan-out; `ReduceNode[S](reducer, result_field)` aggregates via
   `ReducerChannel`. Example graph: split → fan-out → reduce.

These are **examples, not framework modules** — they live under
`examples/` per ADR-0007 rule 9 (framework vs examples separation). They
do not migrate any production code to the graph engine (per the
grill-session finding that InputPipeline, Approval, and AgentPipeline are
all poor migration candidates — see "Rejected Phase c triggers" below).

## Rejected Phase c triggers

The grill session evaluated three candidate "second consumers" to
justify starting Phase c (parallel fan-out, subgraph nesting, etc.) and
rejected all three:

1. **InputPipeline migration.** The pipeline is a 25-line linear
   sequence with early-exit (`Terminate`). Migrating to a graph would
   replace 25 simple lines with `Graph` + `Node` + `GraphEngine` +
   `GraphContext` + `GraphRuntime` ceremony — net code increase, no
   new capability (static edges + `transition` already prove the linear
   shape).
2. **Approval state machine migration.** Approval already uses the graph
   engine's `ctx.interrupt(tx)` → `GraphInterrupt` → resume path. ADR-0025
   Non-goals explicitly rejects further approval refactoring: "would
   re-touch the approval `GraphInterrupt` state machine… with no
   offsetting benefit — react's turn stages are stable, not dynamically
   composed".
3. **AgentPipeline / OutputRenderer migration.** `AgentPipeline` is
   imperative orchestration (async stream + per-session lock + busy-input
   3-mode handling + dedup + command pre-routing). Its complexity comes
   from real concurrency concerns (locking, dedup, busy-handling) that a
   graph engine does not simplify.

A fourth candidate — multi-agent star topology as a subgraph-nesting
target — was evaluated and rejected as a category error: star topology
is inter-agent communication (async, isolated state, persistent
sessions, cross-process); subgraph nesting is intra-turn control flow
(sync, shared state, single `GraphEngine.run_async`). They are different
problems; subgraph nesting does not replace star topology.

**Conclusion:** no real Phase c trigger exists today. This ADR clears the
prerequisites (per-channel checkpoint repair, AOP documentation,
API-surface standard) so that when a real trigger emerges, Phase c
design starts from a clean foundation. Phase c itself remains deferred
per ADR-0033 D12.

## Consequences

### Positive

- **Per-channel checkpoint becomes the single serialization path.**
  ADR-0033 D14's promise is delivered: 230 lines of hand-written
  `ReActSnapshotPolicy._build_payload` is replaced by declarative
  per-field channels, and `ReActTurnState` no longer overrides
  `checkpoint()` / `from_checkpoint()`. Future `GraphState` subclasses
  get correct serialization for free (BaseModel + stdlib dataclass +
  PEP 604 union + StrEnum + nested types).
- **AOP routing is honest.** The dead pass-through branches are gone; a
  reader sees `around` routing `ITERATION` only, and `tool_executor` /
  `llm_client` calling `InterceptorChain` directly with a comment
  explaining why. Phase c's second graph consumer inherits a clear
  contract.
- **`modex_graph` API standard is explicit.** Future contributors will
  not propose deleting `Command.goto=list[Task]` or `CompiledGraph`-as-
  `Node` as "dead code" — the standard (coherent + tested +
  non-contradictory) is documented.
- **`examples/graph_patterns/` exercises the retained API.** The three
  patterns (conditional, retry, map_reduce) compose the retained API
  into realistic workflows, giving Phase c design a concrete reference.

### Negative

- **Stage 2 follow-up is now a tracked debt.** The `TypeAdapter` branch
  in `encode_value` / `decode_value` is a transition: it exists because
  six value objects are still stdlib dataclasses. Stage 2 (migrate to
  `BaseModel`, delete the `TypeAdapter` branch) is recorded as a
  follow-up. If Stage 2 never lands, the `TypeAdapter` branch is
  permanent — not broken, but a second serialization path that the
  "complete state" was meant to eliminate.
- **`examples/graph_patterns/` is not framework code.** The patterns
  live under `examples/` and are not imported by `modex_agent`. They
  are reference implementations, not reusable abstractions — promoting
  any of them to framework code would require ADR-0007's two-consumer
  test.
- **`ReActScope.TOOL_CALL` / `LLM_STREAM` / `LLM_CALL` retained as
  informational.** A future reader may wonder why these enum values
  exist if `around` does not route them. The docstring on `around` and
  this ADR (D2) are the explanation; if they are lost, the values look
  like dead code again.

### Neutral

- **No runtime behavior change from D2.** The deleted pass-through
  branches were never invoked; `tool_executor` and `llm_client` already
  called `InterceptorChain` directly. D2 is documentation + dead-code
  removal, not a routing change.
- **No runtime behavior change from D3.** The retained API elements were
  already unit-tested; the `examples/graph_patterns/` additions are new
  test coverage, not changes to existing behavior.
- **D1 is the only behavior-changing decision.** `ReActTurnState`
  switches from `model_dump` / `model_validate` to per-channel
  `checkpoint()` / `from_checkpoint()`. The round-trip regression test
  (D1 step 7) is the gate: if it passes, the switch is behavior-equivalent;
  if it fails, the failure identifies a per-channel edge case to fix
  before the override is removed.

## Implementation order

1. **Stage A — TD#4 Stage 1 (per-channel checkpoint repair).** ~1–2 days.
   D1 steps 1–7, in dependency order: PEP 604 fix → dataclass encode →
   dataclass decode → `TypeAdapter` cache → delete overrides → delete
   `react/codec.py` → clean imports → regression test.
2. **Stage B — TD#3 (AOP routing documentation).** ~0.5 days. D2 steps
   1–6. No behavior change; safe to land immediately after Stage A.
3. **Stage C — `examples/graph_patterns/` + this ADR.** ~1–2 days. D3
   workstream (C): three pattern modules with tests, then finalise this
   ADR's Status from `proposed` to `accepted`.

**Stage 2 follow-up** (D1 deferred scope) starts after Stage A lands and
is tracked as a separate ADR. It is not on the critical path for Phase c
design resumption — Stage A's `TypeAdapter` transition is sufficient.

## References

- ADR-0033 — Generalized Graph Engine (Phase a). D5.2 (`around` scope),
  D14 (dataclass codec), D12 (Phase c deferred).
- ADR-0007 — Keep zero-usage deep modules with real seams. Governs
  `modex_agent` internal seams; **does not govern `modex_graph` public
  API** (this ADR D3).
- ADR-0025 — Execution strategy abstraction and pipeline slimming.
  Non-goals section rejects further approval refactoring.
- `docs/handoff/graph-engine-phase-a-handoff.md` — Phase a handoff
  document; items 1–6 are Phase c candidates (all deferred by this ADR).
- `tests/unit/modex_graph/` — 8 test files covering the retained API
  surface (D3).
- PocketFlow (`F:\tool\pythonProject\references\PocketFlow`) — 100-line
  reference graph engine; `Flow(BaseNode)` proves graph-is-a-node at
  minimum complexity. Referenced for design comparison only; not in
  `modex_graph`'s dependency tree.
