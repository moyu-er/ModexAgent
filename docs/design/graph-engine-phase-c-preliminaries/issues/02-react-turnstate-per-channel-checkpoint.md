# 02 — ReActTurnState uses per-channel checkpoint path

**What to build:** `ReActTurnState` no longer overrides `checkpoint()` and `from_checkpoint()` — it inherits the `GraphState` base class per-channel path (the same path every other `GraphState` subclass uses, and the same path `tests/unit/modex_graph/test_channels.py` validates). This makes the per-channel codec the single serialization path for ReAct state, fulfilling ADR-0033 D14's promise to replace 230 lines of hand-written `ReActSnapshotPolicy._build_payload` with declarative per-field channels.

Three coupled deletions land together because they form a single behavioral unit:

1. **Override removal.** `ReActTurnState.checkpoint()` (which calls `model_dump(mode="json")`) and `ReActTurnState.from_checkpoint()` (which calls `model_validate`) are deleted. The class now uses `GraphState.checkpoint()` / `from_checkpoint()`. The long docstring explaining why the override existed is replaced with a note that the override was removed per ADR-0034 D1.

2. **Dead codec file deletion.** `react/codec.py` (50 lines) is deleted. Its five `register_codec` calls for `BaseModel` types (`ApprovalTransaction`, `ApprovalRequestState`, `ToolBatchState`, `ToolCallState`, `ToolArguments`) are unreachable dead code — `encode_value` checks `isinstance(value, BaseModel)` *before* consulting `_find_codec`, so registered `BaseModel` codecs are never invoked. Once ticket 01's `TypeAdapter` branch handles the same types, the registrations are also unnecessary.

3. **Import cleanup.** Three side-effect imports of `react/codec.py` are removed:
   - `tests/unit/agents/react/test_snapshot_round_trip.py:22` — `import modex_agent.agents.react.codec  # noqa: F401`
   - `tests/unit/agents/react/test_state_pydantic_migration.py:26` — same side-effect import
   - `tests/unit/agents/react/test_state_pydantic_migration.py:378` — `import modex_agent.agents.react.codec as _codec_mod` inside `test_codec_module_import_registers_all_five`
   
   Two test functions in `test_state_pydantic_migration.py` that verify the dead-code registrations are deleted (they assert `_find_codec(cls) is not None` for the five registered types, and directly call a registered codec's `encode`/`decode` — both test dead behavior that no longer exists after the file is deleted).

The regression gate is `test_snapshot_round_trip.py`, extended with per-channel coverage:

- `test_round_trip_preserves_pep604_union_with_value`: covers `cancellation` / `llm_response` / `approval` / `result` with real objects (not `None`) — the case that was silently broken before ticket 01.
- `test_round_trip_preserves_nested_dataclass_with_basemodel`: `TurnIdentity` (stdlib dataclass) containing `SessionInfo` (Pydantic `BaseModel`) round-trips with all nested fields preserved.
- `test_round_trip_preserves_list_of_dataclass`: `message_delta` / `operations` / `tool_batches` with multiple complex entries round-trip.
- `TestSnapshotParity` updated: the NEW path is now per-channel (not `model_dump`); the OLD baseline (`_build_payload` captured as fixtures) is retained; the parity assertion compares OLD baseline vs per-channel path — proving the per-channel path is equivalent to both the hand-written baseline and the `model_dump` override it replaces.

**Risk note:** Approval suspend / resume is the hot path (17 callers depend on `from_checkpoint`, including `approval_resumer.py` and `approval_renderer.py`). The extended `test_snapshot_round_trip.py` is the gate: if it passes, the switch is behavior-equivalent; if it fails, the failure identifies a per-channel edge case to fix before the override is removed.

**Blocked by:** 01 (modex_graph codec handles PEP 604 unions and stdlib dataclasses) — the codec must handle all `ReActTurnState` field types correctly before the override that bypasses it can be removed.

**Status:** ready-for-agent

- [ ] `ReActTurnState.checkpoint()` override deleted — class inherits `GraphState.checkpoint()`
- [ ] `ReActTurnState.from_checkpoint()` override deleted — class inherits `GraphState.from_checkpoint()`
- [ ] `react/codec.py` deleted entirely (50 lines dead code)
- [ ] Side-effect import removed from `test_snapshot_round_trip.py:22`
- [ ] Side-effect import removed from `test_state_pydantic_migration.py:26`
- [ ] `test_state_pydantic_migration.py:378` import + the enclosing `test_codec_module_import_registers_all_five` test function deleted (verified dead-code registrations, no longer applicable)
- [ ] The other test function in `test_state_pydantic_migration.py` that directly calls a registered codec's `encode`/`decode` is deleted (same rationale)
- [ ] `test_snapshot_round_trip.py::test_round_trip_preserves_pep604_union_with_value` added — covers `cancellation`/`llm_response`/`approval`/`result` with real objects
- [ ] `test_snapshot_round_trip.py::test_round_trip_preserves_nested_dataclass_with_basemodel` added — `TurnIdentity` nesting `SessionInfo` round-trips
- [ ] `test_snapshot_round_trip.py::test_round_trip_preserves_list_of_dataclass` added — `message_delta`/`operations`/`tool_batches` with complex entries round-trip
- [ ] `TestSnapshotParity` updated — NEW path is per-channel; OLD baseline retained; parity asserts OLD baseline == per-channel path
- [ ] Full `tests/unit/agents/react/` suite passes as regression gate (17 `from_checkpoint` callers continue to work)
