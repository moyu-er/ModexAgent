# 07 — ADR-0033 Status accepted and final verification

**What to build:** ADR-0033's Status moves from `proposed` to `accepted` once all six implementation tickets (01–06) have landed. This is the closing ticket: it verifies the full regression suite passes, confirms the three workstreams delivered their promised outcomes, and updates the ADR's Consequences section if any deviation from the plan emerged during implementation.

This ticket exists because ADR-0033 was written `proposed` (design completed, implementation pending) — the `accepted` transition is the architectural record that the design was implemented as specified, or that any deviations are documented.

Verification scope:

1. **Stage A (per-channel checkpoint repair) — tickets 01 + 02.** Confirm:
   - `ReActTurnState` no longer overrides `checkpoint()` / `from_checkpoint()` (uses `GraphState` base class per-channel path).
   - `react/codec.py` is deleted; all side-effect imports cleaned.
   - `test_snapshot_round_trip.py` extended regression tests pass (PEP 604 unions with non-`None` values, nested dataclass + nested BaseModel, list[dataclass], parity vs OLD baseline).
   - `test_channels.py` extended tests pass (PEP 604 union, stdlib dataclass, nested BaseModel inside dataclass).
   - Approval suspend / resume path works (17 `from_checkpoint` callers continue to function — verified by the full `tests/unit/agents/react/` suite passing).

2. **Stage B (AOP routing documentation) — ticket 03.** Confirm:
   - `ReactGraphRuntime.around` routes `ITERATION` only (pass-through branches deleted).
   - `tool_executor.py` and `llm_client.py` carry the canonical-AOP-path comments.
   - `CONTEXT.md` `GraphRuntime` entry reflects `around` routes `ITERATION` only.
   - `ReActScope.TOOL_CALL` / `LLM_STREAM` / `LLM_CALL` retained as informational.
   - Existing ReAct test suite passes (zero behavior change).

3. **Stage C (graph_patterns + ADR) — tickets 04 + 05 + 06.** Confirm:
   - `examples/graph_patterns/__init__.py` exports `ConditionalNode`, `SwitchNode`, `build_conditional_graph`, `RetryNode`, `build_retry_graph`, `MapNode`, `ReduceNode`, `build_map_reduce_graph`.
   - `tests/unit/examples/graph_patterns/` has three test files, all passing.
   - The three patterns demonstrate non-ReAct graph compositions using the retained API.

4. **ADR-0033 update.** Move Status from `proposed` to `accepted`. If any implementation deviation emerged (e.g. a `TypeAdapter` edge case required a workaround; a pattern's API shape changed during implementation), document it in the Consequences section. If no deviations, note "implemented as specified".

5. **Full regression run.** `pytest tests/unit/ -v` passes (this is the final gate — no individual ticket's tests substitute for the full suite, because the work touches shared infrastructure: `modex_graph.channel`, `ReActTurnState`, `ReactGraphRuntime`).

**Blocked by:**

- 01 (modex_graph codec handles PEP 604 unions and stdlib dataclasses)
- 02 (ReActTurnState uses per-channel checkpoint path)
- 03 (AOP routing documentation and pass-through deletion)
- 04 (graph_patterns: conditional)
- 05 (graph_patterns: retry)
- 06 (graph_patterns: map_reduce)

**Status:** done

- [x] Full `pytest tests/unit/ -v` suite passes (473 tests across all affected areas: modex_graph, react, pipeline, runtime, turn_state_store, examples)
- [x] Stage A verification: `ReActTurnState` per-channel path confirmed (no override; `react/codec.py` deleted; extended `test_snapshot_round_trip.py` + `test_channels.py` pass)
- [x] Stage B verification: `ReactGraphRuntime.around` routes `ITERATION` only; comments + `CONTEXT.md` updated; `ReActScope` enum values retained; existing ReAct suite passes
- [x] Stage C verification: three `examples/graph_patterns/` modules + tests pass; `__init__.py` exports all 8 public pattern classes
- [x] ADR-0033 Status moved from `proposed` to `accepted`
- [x] ADR-0033 Consequences section: implemented as specified (no deviations); whole-effort reviewer APPROVED
