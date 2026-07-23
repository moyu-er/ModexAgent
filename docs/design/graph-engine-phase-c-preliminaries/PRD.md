# PRD: Graph Engine Phase c Preliminaries

Status: ready-for-agent
Parent ADR: ADR-0033 (Graph Engine Phase c Preliminaries)
Supersedes: none. Refines ADR-0033 (D5.2 `around` scope; D14 dataclass codec).

## Problem Statement

ADR-0033 shipped Phase a of the generalized graph engine: `modex_graph`
extracted as a standalone package, ReAct migrated to a 4-node graph,
per-channel checkpoint infrastructure landed, and the `GraphRuntime` AOP
bridge established. Three findings from a post-Phase-a review block clean
Phase c work:

1. **Per-channel checkpoint is silently broken.** `ReActTurnState`
   overrides `checkpoint()` / `from_checkpoint()` to use Pydantic
   `model_dump` / `model_validate`, bypassing the per-channel codec
   infrastructure that ADR-0033 D14 promised would replace 230 lines of
   hand-written `ReActSnapshotPolicy._build_payload`. Two codec gaps force
   the bypass: PEP 604 unions (`X | None`) are not handled by
   `decode_value`, and stdlib `@dataclass` field types (six of them:
   `TurnIdentity`, `LLMResponse`, `AgentResult`, `MessageDelta`,
   `OperationState`, `CancellationState`) fall through to `str(value)` —
   lossy. A third defect: `react/codec.py` registers five Pydantic
   `BaseModel` types via `register_codec`, but these registrations are
   unreachable dead code because `encode_value` checks `isinstance(value,
   BaseModel)` before consulting `_find_codec`. Any Phase c work that
   touches channels (parallel fan-out's multi-write detection;
   `ReducerChannel`-based state merge; subgraph state isolation via
   `ctx.fork`) builds on a path that has never carried real ReAct state.

2. **AOP routing is split, and the split is undocumented.** ReAct has
   two AOP routing paths: `ITERATION` goes through
   `ctx.runtime.around(ReActScope.ITERATION, ctx, body)`; `TOOL_CALL` and
   `LLM_STREAM` go directly to `InterceptorChain.around_tool_call` /
   `around_llm_stream` from `tool_executor.py` and `llm_client.py`. The
   split is a design fact — `ToolCallContext` / `LLMStreamContext` carry
   node-local data that cannot be lifted to the graph-runtime layer
   without violating invariant 1 (`modex_graph` has zero `modex_agent`
   imports) or adding a pure forwarding shell that fails the ADR-0007
   deletion test. But `ReactGraphRuntime.around` retains dead
   pass-through branches for `TOOL_CALL` / `LLM_STREAM` / `LLM_CALL` that
   actively mislead readers into thinking all three scopes are wired.

3. **`modex_graph`'s large unused API surface needs a standard.** Roughly
   60% of the public API (`Command.goto` in `list[Task]` form,
   `add_conditional_edges`, `CompiledGraph`-as-`Node`, `GraphDrained`,
   `ParentCommand`, `ReducerChannel` in production state, `ctx.fork`
   outside the engine's internal `Task` execution) is declared,
   unit-tested in `tests/unit/modex_graph/`, but unused by any production
   code. Without an explicit standard, future contributors may propose
   deleting these as "dead code" under ADR-0007 — only to re-add them
   when Phase c needs them.

From the user's perspective (a framework developer working on
`modex_agent` and `modex_graph`): before designing or implementing any
Phase c feature (parallel fan-out, subgraph nesting, additional channels,
BSP superstep), the engine's per-channel checkpoint must actually work,
the AOP routing must be honestly documented, and the API-surface standard
must be settled so that Phase c design doesn't relitigate "should this
API exist".

## Solution

Three workstreams land in sequence, clearing Phase c prerequisites without
starting Phase c itself:

1. **Repair per-channel checkpoint (TD#4 Stage 1).** Fix the two codec
   gaps (PEP 604 union handling; stdlib dataclass handling via Pydantic
   `TypeAdapter`), remove the `ReActTurnState.checkpoint()` /
   `from_checkpoint()` overrides, delete the dead `react/codec.py` file,
   and add regression coverage proving the per-channel path round-trips
   all 13 `ReActTurnState` field types correctly. After this, the
   per-channel codec is the single serialization path — `GraphState`
   subclasses get correct checkpoint / restore for free.

2. **Document the AOP split and delete dead pass-through code (TD#3).**
   Accept that `ITERATION` goes through `ctx.runtime.around` while
   `TOOL_CALL` and `LLM_STREAM` go directly to `InterceptorChain` — this
   is a design fact, not a defect to unify. Delete the dead pass-through
   branches in `ReactGraphRuntime.around`, update docstrings and
   comments to explain the split, and refine ADR-0033 D5.2.

3. **Settle the `modex_graph` API-surface standard and exercise retained
   API with realistic compositions (Workstream C).** Document (in
   ADR-0033 D15) that `modex_graph`'s public API is governed by coherence
   + tested + non-contradictory, not by ReAct usage. Add three pattern
   modules under `examples/graph_patterns/` (conditional, retry,
   map_reduce) with covering unit tests, proving the retained API
   composes into realistic non-ReAct workflows.

A Stage 2 follow-up (migrate six stdlib dataclass value objects to
Pydantic `BaseModel`, then delete the `TypeAdapter` transition branch) is
tracked as a separate future ADR — it is not on the critical path because
Stage 1's `TypeAdapter` transition makes the per-channel path correct for
all current field types.

## User Stories

1. As a framework developer, I want `ReActTurnState.checkpoint()` to use
   the per-channel codec path (not a Pydantic `model_dump` override), so
   that the per-channel infrastructure ADR-0033 D14 promised is actually
   exercised in production.

2. As a framework developer, I want `decode_value` to handle PEP 604
   union types (`X | None`) identically to `typing.Optional[X]`, so that
   fields declared with `T | None` syntax serialize and deserialize
   correctly through the per-channel codec.

3. As a framework developer, I want `encode_value` to recognize stdlib
   `@dataclass` instances and serialize them via Pydantic `TypeAdapter`
   (which handles nested `BaseModel`, nested dataclass, `StrEnum`, and
   `Optional` natively), so that `TurnIdentity`, `LLMResponse`,
   `AgentResult`, `MessageDelta`, `OperationState`, and
   `CancellationState` fields round-trip without loss.

4. As a framework developer, I want `decode_value` to reconstruct stdlib
   `@dataclass` instances via `TypeAdapter.validate_python`, so that
   `from_checkpoint` restores the correct runtime types (not raw dicts).

5. As a framework developer, I want a module-level `TypeAdapter` cache
   (`dict[type, TypeAdapter]`), so that repeated checkpoint / restore
   operations do not pay the `TypeAdapter` construction cost on every
   call.

6. As a framework developer, I want the `ReActTurnState.checkpoint()`
   override (which calls `model_dump(mode="json")`) removed, so that
   `ReActTurnState` uses `GraphState.checkpoint()` — the same per-channel
   path every other `GraphState` subclass uses.

7. As a framework developer, I want the `ReActTurnState.from_checkpoint()`
   override (which calls `model_validate`) removed, so that
   `from_checkpoint` restores state via the per-channel codec — the same
   path every other `GraphState` subclass uses.

8. As a framework developer, I want `react/codec.py` deleted, so that
   the five unreachable `register_codec` calls for `BaseModel` types
   (which `encode_value` never reaches because it checks
   `isinstance(value, BaseModel)` first) no longer mislead readers into
   thinking they are active.

9. As a framework developer, I want all imports of `react/codec.py`
   cleaned up (including the side-effect import in
   `test_snapshot_round_trip.py`), so that deleting the file does not
   break any import path.

10. As a framework developer, I want a round-trip regression test that
    covers all 13 `ReActTurnState` field types — including PEP 604
    unions with non-`None` values, nested dataclasses, nested
    `BaseModel` (`SessionInfo` inside `TurnIdentity`), `StrEnum` fields,
    and `list[dataclass]` collections — so that the switch from
    `model_dump` to per-channel codec is provably behavior-equivalent
    for approval suspend / resume.

11. As a framework developer, I want the existing `TestSnapshotParity`
    test (which currently compares OLD `_build_payload` vs NEW
    `model_dump`) updated to compare OLD `_build_payload` vs NEW
    per-channel path, so that the parity gate proves the per-channel
    path is equivalent to both the hand-written baseline and the
    `model_dump` override it replaces.

12. As a framework developer, I want `encode_value` / `decode_value`
    unit tests in `tests/unit/modex_graph/test_channels.py` covering
    PEP 604 union fields and stdlib dataclass fields in isolation, so
    that codec-level defects are caught at the `modex_graph` package
    level without needing a ReAct consumer to surface them.

13. As a framework developer reading `ReactGraphRuntime.around`, I want
    the method to route `ITERATION` only (not `TOOL_CALL` /
    `LLM_STREAM` / `LLM_CALL`), so that the code honestly reflects
    which scopes go through the graph-runtime AOP bridge.

14. As a framework developer, I want the dead pass-through branches for
    `TOOL_CALL` / `LLM_STREAM` / `LLM_CALL` in
    `ReactGraphRuntime.around` deleted, so that readers are not misled
    into thinking those scopes are wired through `around`.

15. As a framework developer, I want `tool_executor.py` and
    `llm_client.py` to carry a comment explaining that direct
    `InterceptorChain` invocation is the canonical AOP path for
    `TOOL_CALL` and `LLM_STREAM` (and that `ctx.runtime.around` is for
    `ITERATION` only), so that the AOP split is documented at the call
    site, not just in an ADR.

16. As a framework developer, I want ADR-0033 D5.2 refined from
    "`around` routes all scopes" to "`around` routes `ITERATION` only;
    `TOOL_CALL` and `LLM_STREAM` are node-local AOP invoked directly via
    `InterceptorChain`", so that the architectural record matches the
    code.

17. As a framework developer, I want the `CONTEXT.md` `GraphRuntime`
    entry updated to reflect that `around` routes `ITERATION` only, so
    that the domain glossary matches the implementation.

18. As a framework developer, I want `ReActScope.TOOL_CALL` /
    `LLM_STREAM` / `LLM_CALL` enum values retained as informational
    (documenting that these AOP scopes exist as concepts, even though
    they are not routed through `around`), so that the canonical AOP
    vocabulary is not lost.

19. As a framework developer, I want ADR-0033 D15 to document that
    `modex_graph`'s public API is governed by coherence + tested +
    non-contradictory (not by ReAct usage), so that future contributors
    do not propose deleting retained-but-unused API elements as "dead
    code" under ADR-0007.

20. As a framework developer, I want ADR-0033 D15 to explicitly list
    which API elements are retained despite zero production usage
    (`Command.goto=list[Task]`, `Command.goto=list[str]`,
    `add_conditional_edges`, `CompiledGraph`-as-`Node`,
    `ReducerChannel`, `ctx.fork(state=...)`, `GraphDrained`,
    `ParentCommand`) and cite their unit-test coverage, so that the
    retention rationale is auditable.

21. As a framework developer, I want a `conditional` graph pattern
    module under `examples/graph_patterns/` that provides a
    `ConditionalNode[S](predicate: Callable[[S], str])` returning
    `NodeResult(transition=predicate(state))`, so that if/else branching
    is expressible as a reusable graph composition.

22. As a framework developer, I want a `retry` graph pattern module
    under `examples/graph_patterns/` that provides a `RetryNode[S](body,
    max_retries, is_failure)` (synchronous retry within one `execute`
    call) and a companion `build_retry_graph(body_node, max_retries)`
    (topology retry via self-loop + `transition="retry"|"success"|
    "failed"`), so that retry-with-backoff workflows are expressible as
    graph compositions.

23. As a framework developer, I want a `map_reduce` graph pattern module
    under `examples/graph_patterns/` that provides a
    `MapNode[S](items_fn, worker_node, state_fn)` emitting
    `Command(goto=[Task(node=worker_node, state=...)])` for fan-out and
    a `ReduceNode[S](reducer, result_field)` aggregating via
    `ReducerChannel`, so that MapReduce workflows are expressible as
    graph compositions.

24. As a framework developer, I want each `examples/graph_patterns/`
    module to have a covering unit test under
    `tests/unit/examples/graph_patterns/`, so that the patterns are
    verified to produce correct graph execution behavior.

25. As a framework developer, I want the `modex_graph` package's
    `encode_value` / `decode_value` to handle all of the following type
    categories without per-type codec registration: Pydantic `BaseModel`
    (existing), stdlib `@dataclass` (new), PEP 604 union `T | None`
    (new), `typing.Optional[T]` (existing), `StrEnum` (existing),
    primitives (existing), so that future `GraphState` subclasses get
    correct serialization by default.

26. As a framework developer, I want ADR-0033's Status to move from
    `proposed` to `accepted` once all three workstreams land, so that
    the architectural record reflects completion.

27. As a future Phase c designer, I want the per-channel checkpoint
    path proven correct for real ReAct state (not just synthetic test
    state), so that Phase c features building on channels (parallel
    fan-out multi-write detection, subgraph state isolation) start from
    a trusted foundation.

28. As a future Phase c designer, I want the AOP routing contract
    documented honestly (which scopes go through `around`, which do not,
    and why), so that Phase c's second graph consumer inherits a clear
    AOP integration story.

29. As a future Phase c designer, I want the `examples/graph_patterns/`
    compositions as reference implementations, so that Phase c design
    can point to concrete examples of how the retained API composes
    into non-ReAct workflows.

30. As a framework developer, I want a Stage 2 follow-up tracked
    (migrate six stdlib dataclass value objects to Pydantic `BaseModel`,
    then delete the `TypeAdapter` transition branch) as a separate
    future ADR, so that the path to a fully unified serialization path
    is recorded without blocking Phase c on a 1–2 week high-blast-radius
    migration.

## Implementation Decisions

### Architectural decisions (from ADR-0033)

- **D1 — Per-channel checkpoint repair, two stages.** Stage 1 (this
  spec's scope) uses `TypeAdapter` as a transition: `encode_value` /
  `decode_value` gain a stdlib-dataclass branch that delegates to
  Pydantic's `TypeAdapter.dump_python(value, mode="json")` /
  `TypeAdapter(field_type).validate_python(data)`. Stage 2 (deferred,
  separate ADR) migrates the six dataclass value objects to `BaseModel`,
  making the `TypeAdapter` branch dead and deletable. Stage 1 unblocks
  Phase c without waiting for Stage 2 because the `TypeAdapter`
  transition handles all current `ReActTurnState` field types correctly.

- **D2 — AOP split is a design fact.** `ITERATION` goes through
  `ctx.runtime.around`; `TOOL_CALL` and `LLM_STREAM` go directly to
  `InterceptorChain`. Lifting the typed interceptor contexts
  (`ToolCallContext` / `LLMStreamContext`) to the graph-runtime layer
  would violate invariant 1 (`modex_graph` has zero `modex_agent`
  imports — those context types live in `modex_agent.interceptor.abc`)
  or add a pure forwarding shell that fails the ADR-0007 deletion test.
  The split is documented, not unified.

- **D3 — `modex_graph` API-surface standard.** Coherent + tested +
  non-contradictory. Does not supersede ADR-0007 (which governs
  `modex_agent` internal module seams); `modex_graph` is a standalone
  package whose API surface is the contract with future consumers.
  Retained-but-unused API elements (listed in user story 20) are kept.

### Modules to be modified

- **`modex_graph.channel`** — `encode_value` gains a stdlib-dataclass
  branch (after the `BaseModel` check, before `_find_codec`); `decode_value`
  gains a symmetric stdlib-dataclass branch; the PEP 604 union check is
  extended to `origin is Union or origin is types.UnionType`. A
  module-level `TypeAdapter` cache is added.

- **`modex_agent.agents.react.state`** — `ReActTurnState.checkpoint()`
  and `from_checkpoint()` overrides are deleted. The class now inherits
  `GraphState.checkpoint()` / `from_checkpoint()` (the per-channel path).
  The long docstring explaining why the override exists is replaced with
  a note that the override was removed per ADR-0033 D14.

- **`modex_agent.agents.react.codec`** — the entire file is deleted.
  Its five `register_codec` calls are unreachable (per D1) and
  unnecessary once the dataclass branch handles the same types via
  `TypeAdapter`.

- **`modex_agent.agents.react.runtime`** — `ReactGraphRuntime.around`
  loses its `TOOL_CALL` / `LLM_STREAM` / `LLM_CALL` pass-through
  branches; only `ITERATION` remains. Docstring updated.

- **`modex_agent.agents.react.tool_executor`** — a comment is added at
  the `InterceptorChain.around_tool_call` call site explaining this is
  the canonical AOP path for `TOOL_CALL` and that `ctx.runtime.around`
  is for `ITERATION` only.

- **`modex_agent.agents.react.llm_client`** — a comment is added at
  the `InterceptorChain.around_llm_stream` call site with the same
  rationale.

### Modules to be created

- **`examples/graph_patterns/conditional.py`** — `ConditionalNode[S]`
  and a `SwitchNode[S]` (multi-branch variant). `ConditionalNode`
  takes a `predicate: Callable[[S], str]` and returns
  `NodeResult(transition=predicate(state))`. The graph topology (if/else
  branches + merge) is demonstrated in the module's example graph
  builder.

- **`examples/graph_patterns/retry.py`** — `RetryNode[S]` (synchronous
  retry: calls `body.execute(ctx)` up to `max_retries + 1` times within
  a single `execute`, returning the last `NodeResult` or
  `transition="failed"`) and `build_retry_graph(body_node, max_retries,
  is_failure)` (topology retry: self-loop on the body node with
  `transition="retry"`, exit to END with `transition="success"` or
  `transition="failed"`).

- **`examples/graph_patterns/map_reduce.py`** — `MapNode[S]` (reads
  `items_fn(state)` to get a list, emits
  `Command(goto=[Task(node=worker_node, state=state_fn(item)) for item
  in items])`) and `ReduceNode[S]` (reads the `ReducerChannel`-backed
  field, applies `reducer(values)`, writes to `result_field`).

- **`examples/graph_patterns/__init__.py`** — package marker exporting
  the public pattern classes.

### Test modules to be modified

- **`tests/unit/modex_graph/test_channels.py`** — extend with:
  - `TestPEP604UnionCheckpoint`: `T | None` field with non-`None` value
    round-trips correctly (covers `cancellation`, `llm_response`,
    `approval`, `result` field shapes).
  - `TestStdlibDataclassCheckpoint`: stdlib `@dataclass` field
    round-trips correctly; nested `BaseModel` inside dataclass
    round-trips correctly; `list[dataclass]` round-trips correctly.

- **`tests/unit/agents/react/test_snapshot_round_trip.py`** — extend
  with:
  - `test_round_trip_preserves_pep604_union_with_value`: covers
    `cancellation` / `llm_response` / `approval` / `result` with real
    objects (not `None`).
  - `test_round_trip_preserves_nested_dataclass_with_basemodel`:
    `TurnIdentity` (stdlib dataclass) containing `SessionInfo`
    (Pydantic `BaseModel`) round-trips with all nested fields
    preserved.
  - `test_round_trip_preserves_list_of_dataclass`: `message_delta` /
    `operations` / `tool_batches` with multiple complex entries
    round-trip.
  - Update `TestSnapshotParity`: the NEW path is now per-channel (not
    `model_dump`); the OLD baseline (`_build_payload`) is retained; the
    parity assertion compares OLD baseline vs per-channel path.
  - Remove the `import modex_agent.agents.react.codec` side-effect
    import (line 22) — the file is deleted, so the import would fail.

### Test modules to be created

- **`tests/unit/examples/graph_patterns/__init__.py`** — package marker.
- **`tests/unit/examples/graph_patterns/test_conditional.py`** —
  verifies `ConditionalNode` routes to the correct branch based on
  predicate output; verifies a complete if/else + merge graph produces
  the expected final state.
- **`tests/unit/examples/graph_patterns/test_retry.py`** — verifies
  `RetryNode` retries `max_retries` times then succeeds / fails;
  verifies `build_retry_graph` self-loop topology exits correctly on
  success / failure / max-retries-exhausted.
- **`tests/unit/examples/graph_patterns/test_map_reduce.py`** —
  verifies `MapNode` emits `Command(goto=list[Task])` with the correct
  number of tasks; verifies `ReduceNode` aggregates `ReducerChannel`
  values correctly; verifies a complete split → fan-out → reduce graph
  produces the expected final aggregated state.

### Documentation to be updated

- **ADR-0033** — Status line gains a Refinements note: D5.2 refined
  by ADR-0033 D5 (`around` routes `ITERATION` only); D14 refined by
  ADR-0033 D14 (`TypeAdapter` transition, override removed); D12 (Phase c
  deferred) stands. *(Already applied during ADR-0033 writing.)*

- **ADR-0033** — Status moves from `proposed` to `accepted` once all
  three workstreams land and the regression test suite passes.

- **`CONTEXT.md`** — `GraphRuntime` entry updated to reflect `around`
  routes `ITERATION` only. *(Pending — applied as part of Stage B.)*

- **`docs/adr/AGENTS.md`** and **`docs/AGENTS.md`** — ADR index updated
  to include ADR-0033. *(Already applied during ADR-0033 writing.)*

### Type-shape decisions (from prototypes / ADR-0033)

The `TypeAdapter` transition in `encode_value` / `decode_value` encodes
a decision about how `modex_graph` handles stdlib dataclasses. The shape
is:

```python
# encode_value (conceptual — actual placement per channel.py structure)
import dataclasses
from pydantic import TypeAdapter

if isinstance(value, BaseModel):
    return value.model_dump(mode="json")
if dataclasses.is_dataclass(value) and not isinstance(value, BaseModel):
    adapter = _get_or_create_adapter(type(value))
    return adapter.dump_python(value, mode="json")
# ... existing _find_codec / primitive / str(value) fallback
```

```python
# decode_value (conceptual)
if origin is Union or origin is types.UnionType:
    non_none_args = [a for a in args if a is not type(None)]
    # ... existing union handling, now covering both Union and UnionType
if isinstance(field_type, type) and dataclasses.is_dataclass(field_type) and not issubclass(field_type, BaseModel):
    adapter = _get_or_create_adapter(field_type)
    return adapter.validate_python(data)
# ... existing BaseModel / Enum / primitive handling
```

```python
# TypeAdapter cache
_TYPE_ADAPTERS: dict[type, TypeAdapter] = {}

def _get_or_create_adapter(python_type: type) -> TypeAdapter:
    adapter = _TYPE_ADAPTERS.get(python_type)
    if adapter is None:
        adapter = TypeAdapter(python_type)
        _TYPE_ADAPTERS[python_type] = adapter
    return adapter
```

This shape is the Stage 1 transition. Stage 2 (deferred) deletes the
`dataclasses.is_dataclass` branches once the six value objects are
`BaseModel` subclasses.

### ReActScope enum decision

`ReActScope.TOOL_CALL` / `LLM_STREAM` / `LLM_CALL` are **retained as
informational**. They document that these AOP scopes exist as concepts
even though they are not routed through `around`. Deleting them would
lose vocabulary without simplifying anything; the enum is the canonical
AOP scope reference.

## Testing Decisions

### What makes a good test

Tests assert **external behavior**, not implementation details. For this
spec:

- A good checkpoint test asserts that `state.checkpoint()` produces a
  JSON-serializable dict and that `State.from_checkpoint(dict)` restores
  a state object equal to the original — it does not assert which codec
  branch (`BaseModel` vs `TypeAdapter` vs `_find_codec`) was taken.
- A good AOP test asserts that interceptors fire in the correct order
  with the correct typed context — it does not assert whether the call
  went through `ctx.runtime.around` or `InterceptorChain` directly.
- A good graph pattern test asserts that executing the pattern graph
  produces the expected final state — it does not assert internal node
  call counts or transition resolution internals.

### Test seams (4, minimized from ideal 1)

The work spans three layers (modex_graph codec / ReAct AOP / examples
patterns) and cannot collapse to a single seam. Each seam is at the
highest feasible point:

| Seam | Layer | Stage | New / Extended | Rationale |
|------|-------|-------|----------------|-----------|
| `tests/unit/agents/react/test_snapshot_round_trip.py` | ReAct behavior | A | Extended | Highest behavior seam; covers per-channel round-trip via ReAct's real state types |
| `tests/unit/modex_graph/test_channels.py` | modex_graph unit | A | Extended | Independent package test; isolates codec behavior without ReAct consumer |
| Existing ReAct test suite (`tests/unit/agents/react/`) | ReAct behavior | B | No new test | Regression gate; pass-through branches are dead code, deletion is zero behavior change |
| `tests/unit/examples/graph_patterns/test_{conditional,retry,map_reduce}.py` | Examples behavior | C | New | Each pattern module is independent and needs its own verification |

### Modules to be tested

- `modex_graph.channel` — `encode_value` / `decode_value` PEP 604 union
  and stdlib dataclass handling (Seam 2).
- `modex_agent.agents.react.state` — `ReActTurnState` per-channel
  checkpoint round-trip for all 13 field types (Seam 1).
- `modex_agent.agents.react.runtime` — `ReactGraphRuntime.around` routes
  `ITERATION` only (covered by existing ReAct test suite regression —
  Seam 3).
- `modex_agent.agents.react.tool_executor` / `llm_client` — direct
  `InterceptorChain` invocation unchanged (covered by existing ReAct
  test suite regression — Seam 3).
- `examples.graph_patterns.conditional` / `retry` / `map_reduce` —
  pattern graph execution (Seam 4).

### Prior art

- **`tests/unit/modex_graph/test_channels.py`** — existing
  `TestPydanticModelCheckpoint`, `TestCustomCodec`,
  `TestBaseChannelABC` provide the pattern for adding
  `TestPEP604UnionCheckpoint` and `TestStdlibDataclassCheckpoint`.
- **`tests/unit/modex_graph/test_engine_topologies.py`** — existing
  `TestLinearChain`, `TestConditionalBranch`, `TestLoopWithCycleGuard`,
  `TestHitlInterruptResume` provide the pattern for graph-pattern
  tests (construct `Graph`, add nodes / edges, compile, run, assert
  final state).
- **`tests/unit/modex_graph/test_routing.py`** — existing
  `TestCommandGotoListTask` provides the pattern for `map_reduce`'s
  fan-out test (construct `Task` list, assert `state_update` merges to
  parent via `ReducerChannel`).
- **`tests/unit/modex_graph/test_subgraph.py`** — existing
  `TestSubgraphAsNode` provides the pattern for `retry`'s
  `build_retry_graph` topology test.
- **`tests/unit/agents/react/test_snapshot_round_trip.py`** — existing
  `TestCheckpointRoundTrip`, `TestSnapshotParity`,
  `TestFullSnapshotCycle` provide the pattern for the extended
  per-channel round-trip tests. The OLD `_build_payload` baseline
  (captured as fixtures in the test file) is the parity reference.

## Out of Scope

- **Phase c itself.** Parallel fan-out execution, subgraph nesting
  (`Command(goto=..., graph=...)`, `ParentCommand` raise-site), additional
  channel types (`Topic`, `EphemeralValue`, `NamedBarrierValue`), BSP
  superstep loop, and non-ReAct production consumers remain deferred per
  ADR-0033 D12. This spec clears prerequisites; it does not start Phase c.

- **Stage 2 dataclass → BaseModel migration.** Migrating
  `CancellationState`, `OperationState`, `MessageDelta`, `LLMResponse`,
  `AgentResult`, and `TurnIdentity` from stdlib `@dataclass` to Pydantic
  `BaseModel` is tracked as a separate future ADR. It is not on the
  critical path because Stage 1's `TypeAdapter` transition makes the
  per-channel path correct for all current field types.

- **Production code migration to the graph engine.** InputPipeline,
  Approval state machine, and AgentPipeline / OutputRenderer were
  evaluated as Phase c "second consumer" candidates and rejected (see
  ADR-0033 "Rejected Phase c triggers"). This spec does not migrate any
  production code to the graph engine.

- **Multi-agent star topology replacement.** Subgraph nesting does not
  replace the existing `AgentPool` star topology — they are different
  problems (intra-turn control flow vs inter-agent communication). This
  spec does not touch the multi-agent system.

- **`ReactGraphRuntime.around` signature change.** Extending `around` to
  accept typed interceptor contexts (`ToolCallContext` /
  `LLMStreamContext`) was rejected (ADR-0033 D5 Option A) because it
  violates invariant 1. This spec does not change the `GraphRuntime` ABC
  signature.

- **`ReActScope` enum deletion.** `TOOL_CALL` / `LLM_STREAM` /
  `LLM_CALL` values are retained as informational. This spec does not
  delete enum values.

- **`react/codec.py` repurposing.** The file is deleted, not
  repurposed. Future codec registrations (if any) would go through the
  standard `register_codec` API in `modex_graph.channel`, not a
  ReAct-local file.

## Further Notes

### Execution order

Three stages, strictly sequential (each depends on the prior stage's
foundations, though Stage B is technically independent of Stage A — they
are sequenced to keep the review surface small):

1. **Stage A — TD#4 Stage 1 (per-channel checkpoint repair).** ~1–2
   days. Dependency order within Stage A: PEP 604 fix → dataclass encode
   → dataclass decode → `TypeAdapter` cache → delete overrides → delete
   `react/codec.py` → clean imports → regression test.

2. **Stage B — TD#3 (AOP routing documentation).** ~0.5 days. No
   behavior change; safe to land immediately after Stage A.

3. **Stage C — `examples/graph_patterns/` + ADR-0033 Status update.**
   ~1–2 days. Three pattern modules with tests, then ADR-0033 Status
   from `proposed` to `accepted`.

### Risk notes

- **Approval suspend / resume is the hot path.** 17 callers depend on
  `from_checkpoint` (including `approval_resumer.py` and
  `approval_renderer.py`). The round-trip regression test
  (`test_snapshot_round_trip.py` extended) is the gate: if it passes,
  the switch from `model_dump` to per-channel is behavior-equivalent; if
  it fails, the failure identifies a per-channel edge case to fix
  before the override is removed.

- **`TypeAdapter` for stdlib dataclasses has documented edge cases**
  (`InitVar`, `field(init=False)`, `__post_init__` field mutation,
  `KW_ONLY`, frozen + `__hash__` interaction). The six ReAct dataclasses
  are simple value objects (no `InitVar`, no `__post_init__`, no
  `KW_ONLY`), so these edge cases do not apply — but the regression test
  is the verification.

- **`test_snapshot_round_trip.py:22` side-effect import.** The line
  `import modex_agent.agents.react.codec  # noqa: F401 — registers
  channel codecs` must be removed when `codec.py` is deleted. Missing
  this will cause an `ImportError` at test collection time.

### References

- ADR-0033 — Graph Engine Phase c Preliminaries (parent ADR).
- ADR-0033 — Generalized Graph Engine (Phase a); D5.2, D14, D12.
- ADR-0007 — Keep zero-usage deep modules with real seams (governs
  `modex_agent` internal seams; does not govern `modex_graph` public
  API per ADR-0033 D15).
- ADR-0025 — Execution strategy abstraction and pipeline slimming
  (Non-goals reject further approval refactoring).
- `docs/handoff/graph-engine-phase-a-handoff.md` — Phase a handoff;
  Items 1–6 are Phase c candidates (all deferred by this spec).
- `tests/unit/modex_graph/` — 8 existing test files covering the
  retained API surface.
