# 03 — `ReActTurnState` → `GraphState` + snapshot simplification

**What to build:** Migrate `ReActTurnState` from a plain `TurnStateBase` subclass to a `GraphState(BaseModel)` subclass with per-field `Annotated[T, ChannelSpec]` declarations. Rewrite `ReActSnapshotPolicy` to use `state.checkpoint()` / `state.from_checkpoint()`, collapsing ~310 lines of hand-written payload flattening to ~50 lines. After this ticket, ReAct's snapshot path goes through the new `modex_graph` channel checkpoint mechanism, but ReAct still uses the old `core/graph/` engine for execution. A snapshot round-trip parity test proves no data is lost in the simplification.

**Blocked by:** 02 — ReAct state types must be Pydantic `BaseModel` + channel codec registrations must exist.

**Status:** ready-for-agent

## Acceptance criteria

- [ ] `ReActTurnState` inherits `GraphState(BaseModel)` (instead of `TurnStateBase`); preserves all existing fields
- [ ] Each `ReActTurnState` field annotated with `Annotated[T, LastValue]` (default channel): `current_node`, `iteration`, `llm_response`, `tool_batches`, `approval`, `result`
- [ ] `messages` field (if added for ReducerChannel use) annotated with `Annotated[list[ChatMessage], ReducerChannel(reducer=operator.add)]`
- [ ] New explicit `result: Annotated[AgentResult | None, LastValue] = None` field on `ReActTurnState` — replaces `custom[TurnCustomKey.GRAPH_RESULT]` pattern
- [ ] `ReActTurnState.checkpoint()` returns `dict[str, JsonValue]` via per-channel serialization
- [ ] `ReActTurnState.from_checkpoint(data)` restores all fields via per-channel deserialization
- [ ] `ReActSnapshotPolicy.capture(state, reason)` rewritten to call `state.checkpoint()` instead of `_build_payload()` (~230 lines deleted)
- [ ] `ReActSnapshotPolicy.state_from_snapshot(snapshot)` rewritten to call `ReActTurnState.from_checkpoint(snapshot.state_payload)` instead of manual field-by-field reconstruction (~80 lines deleted)
- [ ] `ReActSnapshotPolicy.serialize_approval` and `approval_from_snapshot` deleted — `ApprovalTransaction` is now a `LastValue` channel that uses the registered Pydantic codec
- [ ] `ReActSnapshotPolicy.replace_approval(snapshot, new_tx)` rewritten for the new architecture: deserialize state from `snapshot.state_payload` via `ReActTurnState.from_checkpoint()` → assign `state.approval = new_tx` (mutable, not frozen) → re-checkpoint via `state.checkpoint()` → replace `snapshot.state_payload`. **Semantic preserved**: external approval decision path (`ApprovalRenderer` → `apply_decision` → `replace_approval`) works identically — `ApprovalTransaction.decisions` dict is mutated by `apply_decision`, then `replace_approval` persists the updated transaction into the snapshot.
- [ ] Old `ReActSnapshotPayloadKey` / `ApprovalSnapshotKey` / `ToolBatchSnapshotKey` / `ToolCallSnapshotKey` enums deleted (no longer needed — Pydantic `model_dump()` is the codec)
- [ ] `ReActRuntimeStateCodec.encode_turn` / `decode_turn` essentially unchanged (payload `dict[str, JsonValue]` ↔ JSON bytes)
- [ ] `TurnSnapshot.state_payload` structure unchanged (still `dict[str, JsonValue]`)
- [ ] `TurnStateStore.save_turn()` unchanged
- [ ] SQLite schema unchanged
- [ ] Snapshot round-trip parity test: serialize a `ReActTurnState` (populated with realistic data including `ApprovalTransaction`, `ToolBatchState` with calls, `ToolCallState` with decisions) via the OLD `ReActSnapshotPolicy._build_payload()` (captured pre-migration as a baseline fixture) AND via the NEW `state.checkpoint()`; assert the two payloads are equivalent (same keys, same values, same structure)
- [ ] Snapshot round-trip test: `state.checkpoint()` → `TurnSnapshot(state_payload=...)` → `ReActRuntimeStateCodec.encode_turn` → JSON → `decode_turn` → `state_from_snapshot` → assert restored state equals original state (all fields, including nested `ApprovalTransaction` / `ToolBatchState` / `ToolCallState`)
- [ ] All existing ReAct unit tests pass unchanged
- [ ] All existing ReAct integration tests pass unchanged
- [ ] All existing snapshot path tests (`tests/unit/memory/test_cleanup.py` etc.) pass unchanged
- [ ] ReAct still uses old `src/modex_agent/core/graph/` engine for execution — only state type + snapshot path changed
- [ ] `EndNode` writes `ctx.state.result` instead of `ctx.runtime.state.custom[TurnCustomKey.GRAPH_RESULT]` (this is the one node change in this ticket; full god-node disassembly is ticket 04)
- [ ] `ReActAgent.run()` reads `state.result` after engine returns (still old engine; the result field is now typed instead of dict-lookup)
