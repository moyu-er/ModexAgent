# Memory Architecture Implementation Plan

> Source design: `design_doc/2026-05-05-memory-architecture-design.md`
> Goal: implement safer multi-level memory behavior without adding a new memory architecture.
> Core rule: reuse existing extension points (`MessageCompactionPolicy`, `BoundaryPolicy`,
> `SummaryStrategy`, `ArchiveStrategy`, `CommitPolicy`, `InjectionFilterStrategy`).

## Scope

This plan implements the first practical slice of the design and prepares later refinements:

1. Empty archive entries are not persisted.
2. Empty/no-semantic compression reports a skipped result, not a successful commit.
3. Hidden-history no-op compression is stopped.
4. `MessageCompactionPolicy` is actually used by the coordinator.
5. Assistant `tool_calls` messages and `tool` messages are excluded from semantic summaries by
   default.
6. DreamEngine and injection defensively ignore legacy empty archive records.
7. bot_project wires the improved framework policies through existing construction paths.
8. Context-only microcompact placeholders become more informative.

## File Map

Framework files:

- `framework/memory/archive/__init__.py`
  - Fix `SemanticArchiveStrategy` so empty semantic content performs no archive write.
  - Optionally change `ArchiveStrategy.archive(...)` to return a bool only if needed; prefer avoiding
    signature changes unless tests prove callers need the result.
- `framework/memory/compression/policies.py`
  - Fix trigger no-op behavior.
  - Add `MessageCompactionPolicy` dependency to `DefaultMemoryCompressionCoordinator`.
  - Use decisions for boundary and summary input.
  - Fix empty summary commit result semantics.
- `framework/memory/compaction/policy.py`
  - Tighten conservative defaults if needed.
  - Implement or remove `SemanticToolCompactionPolicy`.
- `framework/memory/compaction/boundary.py`
  - Keep existing `BoundaryPolicy` contract.
  - Add user-turn-aware behavior only after core coordinator tests pass.
- `framework/memory/consolidation/dream_engine.py`
  - Reject legacy empty archive markers and metadata.
- `framework/memory/injection/__init__.py`
  - Share/align empty marker handling for archive and compression summary injection.
- `framework/memory/context_governance.py`
  - Improve `MicrocompactGovernance` placeholder text.
- `framework/memory/__init__.py` and package `__init__.py` files
  - Update exports only if concrete policy names change.

bot_project files:

- `examples/bot_project/bot/service/core.py`
  - Parse `memory.main.compaction`.
  - Construct coordinator with selected existing compaction/boundary policies.
- `examples/bot_project/config/bot_config.yml`
  - Add `memory.main.compaction` settings.
  - Enable `tool_call_cleanup` only after tests prove completed-turn safety.
- `examples/bot_project/plugins/tool_call_cleanup/`
  - Keep behavior application-level; add safety tests if enabling by default.

Tests:

- `tests/unit/memory/test_compression_policies.py`
- `tests/unit/memory/compression/test_tool_chain.py`
- `tests/unit/memory/consolidation/test_dream_engine_registry.py`
- `tests/unit/memory/core/test_default_system.py`
- `tests/unit/bot_project/test_bot_project_runtime_wiring.py`
- `examples/bot_project/tests/test_policy.py`
- `examples/bot_project/tests/test_plugin_integration.py`

## Task 1: Empty Archive Write Prevention

**Files:**

- Modify: `framework/memory/archive/__init__.py`
- Test: `tests/unit/memory/test_archive_strategy.py` or existing memory archive test file if present.

Steps:

- [ ] Add tests for `SemanticArchiveStrategy.archive(...)`:
  - input has only assistant `tool_calls` and/or `tool` messages;
  - `summary=""`;
  - expected: archive manager `append(...)` is not called.
- [ ] Add tests for semantic fallback:
  - input has a user message and `summary=""`;
  - expected: archive manager receives one entry with `source="sanitized_fallback"`.
- [ ] Change `SemanticArchiveStrategy.archive(...)` so `_build_entry(...)` can return `None`, or so
  `archive(...)` explicitly checks `entry.metadata["source"] == "empty"` and returns without
  appending.
- [ ] Remove the persisted `"(no semantic content)"` path.
- [ ] Run:

```powershell
python -m pytest --rootdir F:\tool\pythonProject\ModexAgent F:\tool\pythonProject\ModexAgent\tests\unit\memory -q
```

Acceptance:

- No new archive entry is written for empty semantic content.
- Existing non-empty fallback behavior still writes one entry.
- No framework API gains a new architecture layer.

## Task 2: Empty Summary Commit Semantics

**Files:**

- Modify: `framework/memory/compression/policies.py`
- Test: `tests/unit/memory/test_compression_policies.py`

Steps:

- [ ] Add a test for `DefaultCommitPolicy.commit(...)` where `plan.summary == ""`.
- [ ] Expected result:

```python
assert result.committed is False
assert result.retryable is False
assert result.reason == "nothing_to_archive"
```

- [ ] Assert archive `append(...)` is not called.
- [ ] Assert session messages are not replaced.
- [ ] Update empty summary marker handling to use stripped strings and include
  `"(no semantic content)"`.
- [ ] Keep archive failure behavior unchanged: archive write failure still preserves session.
- [ ] Run:

```powershell
python -m pytest --rootdir F:\tool\pythonProject\ModexAgent F:\tool\pythonProject\ModexAgent\tests\unit\memory\test_compression_policies.py -q
```

Acceptance:

- Empty/no-semantic compression is observable as a skip.
- No session mutation happens when nothing was archived.

## Task 3: Trigger No-Op Compression Fix

**Files:**

- Modify: `framework/memory/compression/policies.py`
- Test: `tests/unit/memory/test_compression_policies.py`

Steps:

- [ ] Add a test using `ScopedSessionMemoryManager` with `SessionMemoryConfig(max_messages=3)`.
- [ ] Add 6 user messages.
- [ ] Configure `DefaultCompressionTriggerPolicy(max_messages=10, max_tokens=8000, cooldown_messages=0)`.
- [ ] Expected: `should_compress(...)` returns `None`, because visible history is within configured
  budget and hidden history alone is not enough.
- [ ] Remove or constrain this trigger:

```python
if len(all_msgs) > len(visible):
    return CompressionTrigger(reason=CompressionReason.TOKEN_PRESSURE, score=0.5)
```

- [ ] Prefer removing it from default trigger policy. Idle auto-compact and explicit count/token
  pressure remain responsible for compression.
- [ ] Run the compression policy test file.

Acceptance:

- Hidden history by itself does not cause repeated no-op compression.
- Count and token pressure still trigger compression.

## Task 4: Coordinator Uses Compaction Decisions

**Files:**

- Modify: `framework/memory/compression/policies.py`
- Test: `tests/unit/memory/test_compression_policies.py`

Steps:

- [ ] Add `compaction: MessageCompactionPolicy | None = None` to
  `DefaultMemoryCompressionCoordinator.__init__(...)`.
- [ ] Default it to `ConservativeCompactionPolicy()`.
- [ ] In `maybe_compress(...)`, compute:

```python
decisions = self._compaction.decide_all(visible, context, str(trigger.reason))
```

- [ ] Pass decisions to `self._boundary.find_prune_boundary(...)`.
- [ ] Build `summarized` only from messages in the pruned prefix whose decision is `SUMMARIZE`.
- [ ] Put `DROP_FROM_SUMMARY` messages in `drop_messages`.
- [ ] Keep `keep_messages` as the full suffix.
- [ ] Add a test with messages:
  - user asks a question;
  - assistant has `tool_calls`;
  - tool result has large content;
  - assistant final answer has normal content.
- [ ] Force compression and use a custom `SummaryStrategy` that records the messages it receives.
- [ ] Expected summary input includes user/final assistant content, excludes assistant `tool_calls`
  and `tool` message.
- [ ] Run:

```powershell
python -m pytest --rootdir F:\tool\pythonProject\ModexAgent F:\tool\pythonProject\ModexAgent\tests\unit\memory\test_compression_policies.py -q
```

Acceptance:

- `ConservativeCompactionPolicy` is no longer dead code.
- Default semantic summary input excludes tool execution internals.

## Task 5: Boundary Regression Coverage

**Files:**

- Modify: `tests/unit/memory/compression/test_tool_chain.py`
- Modify if needed: `framework/memory/compaction/boundary.py`

Steps:

- [ ] Add tests for `ToolChainBoundaryPolicy.find_prune_boundary(...)` with real decisions:
  - boundary would cut through assistant tool-call plus tool result;
  - expected boundary moves before the chain or after a complete chain according to existing
    contract.
- [ ] Add tests where a `KEEP_RAW` decision appears in the prune range.
- [ ] Expected: boundary shrinks so `KEEP_RAW` remains in suffix.
- [ ] Do not rewrite `_find_tool_chain()` unless these tests expose a real failure.
- [ ] Run:

```powershell
python -m pytest --rootdir F:\tool\pythonProject\ModexAgent F:\tool\pythonProject\ModexAgent\tests\unit\memory\compression\test_tool_chain.py -q
```

Acceptance:

- Tool chain and `KEEP_RAW` protections are covered by tests.
- No standalone helper rewrite occurs without a failing test.

## Task 6: DreamEngine and Injection Legacy Empty Filtering

**Files:**

- Modify: `framework/memory/consolidation/dream_engine.py`
- Modify: `framework/memory/injection/__init__.py`
- Test: `tests/unit/memory/consolidation/test_dream_engine_registry.py`
- Test: `tests/unit/memory/test_compression_policies.py` or `tests/unit/memory/core/test_default_system.py`

Steps:

- [ ] Add DreamEngine test where archive entries include:
  - summary `"(no semantic content)"`;
  - metadata `{"source": "empty"}`;
  - metadata `{"semantic_count": 0}`;
  - one meaningful summary.
- [ ] Expected: only meaningful entry reaches summarizer/consolidation payload.
- [ ] Add injection test where archive has empty marker entries and one meaningful entry.
- [ ] Expected prompt sections include only the meaningful entry.
- [ ] Extract a small private helper if necessary, but avoid a new public module unless duplication
  becomes error-prone.
- [ ] Run:

```powershell
python -m pytest --rootdir F:\tool\pythonProject\ModexAgent F:\tool\pythonProject\ModexAgent\tests\unit\memory\consolidation F:\tool\pythonProject\ModexAgent\tests\unit\memory\core\test_default_system.py -q
```

Acceptance:

- Legacy empty records are ignored defensively.
- New empty records are still prevented at write time by Task 1.

## Task 7: Implement or Remove `SemanticToolCompactionPolicy`

**Files:**

- Modify: `framework/memory/compaction/policy.py`
- Modify: `framework/memory/compaction/__init__.py`
- Modify: `framework/memory/__init__.py`
- Test: `tests/unit/memory/test_compression_policies.py` or new `tests/unit/memory/test_compaction_policy.py`

Steps:

- [ ] Decide based on current behavior:
  - If it remains a placeholder, remove it from exports and tests.
  - If kept, make it a concrete policy over existing `MessageCompactionDecision`.
- [ ] Recommended first implementation: keep `ConservativeCompactionPolicy(high_value_tools=...)`
  and remove the placeholder class if no callers import it.
- [ ] Before deletion, run:

```powershell
rg -n "SemanticToolCompactionPolicy" F:\tool\pythonProject\ModexAgent
```

- [ ] If external compatibility is a concern, keep it as a thin alias with clear docstring:
  "Compatibility alias for ConservativeCompactionPolicy".
- [ ] Run memory unit tests.

Acceptance:

- There is no misleading placeholder promising semantic tool classification.
- Public exports remain coherent.

## Task 8: bot_project Coordinator Wiring

**Files:**

- Modify: `examples/bot_project/bot/service/core.py`
- Modify: `examples/bot_project/config/bot_config.yml`
- Test: `tests/unit/bot_project/test_bot_project_runtime_wiring.py`

Steps:

- [ ] Add `memory.main.compaction` config to `bot_config.yml`:

```yaml
    compaction:
      policy: "conservative"
      boundary: "tool_chain"
      raw_tool_archive: false
      high_value_tools:
        - "fetch"
        - "mcp-deepwiki"
        - "query_12306"
```

- [ ] In `_build_compression_coordinator(...)`, read `main_memory_config.get("compaction", {})`.
- [ ] Construct `ConservativeCompactionPolicy(high_value_tools=set(...))`.
- [ ] Pass it into `DefaultMemoryCompressionCoordinator(...)`.
- [ ] Keep LLM-backed `SummarizerStrategy`; do not introduce `EnvAwareSummaryStrategy`.
- [ ] Add unit test that `_build_compression_coordinator(...)` returns a coordinator whose
  compaction policy is configured with the expected high-value tools.
- [ ] Run:

```powershell
python -m pytest --rootdir F:\tool\pythonProject\ModexAgent F:\tool\pythonProject\ModexAgent\tests\unit\bot_project\test_bot_project_runtime_wiring.py -q
```

Acceptance:

- bot_project adapts through existing construction code.
- No framework module contains bot-specific tool names.

## Task 9: `tool_call_cleanup` Safety and Default

**Files:**

- Modify: `examples/bot_project/plugins/tool_call_cleanup/policy.py` only if tests expose a safety
  issue.
- Modify: `examples/bot_project/config/bot_config.yml`
- Test: `examples/bot_project/tests/test_policy.py`
- Test: `examples/bot_project/tests/test_plugin_integration.py`

Steps:

- [ ] Add tests:
  - completed ReAct turn removes assistant `tool_calls` and `tool` messages;
  - incomplete turn ending with assistant `tool_calls` is not cleaned;
  - interrupted/simulated assistant marker is preserved if it is the final continuity message.
- [ ] If tests pass with current policy, enable:

```yaml
plugins:
  configurations:
    tool_call_cleanup:
      enabled: true
```

- [ ] If tests fail, fix policy before enabling.
- [ ] Run:

```powershell
python -m pytest --rootdir F:\tool\pythonProject\ModexAgent F:\tool\pythonProject\ModexAgent\examples\bot_project\tests\test_policy.py F:\tool\pythonProject\ModexAgent\examples\bot_project\tests\test_plugin_integration.py -q
```

Acceptance:

- Completed tool internals are cleaned for bot_project.
- Incomplete tool chains are not removed.

## Task 10: Microcompact Placeholder Quality

**Files:**

- Modify: `framework/memory/context_governance.py`
- Test: `tests/unit/bot_project/test_bot_project_runtime_wiring.py` or new
  `tests/unit/memory/test_context_governance.py`

Steps:

- [ ] Add a test for `MicrocompactGovernance` with an old large tool result.
- [ ] Expected compacted content includes:
  - tool name;
  - original char count;
  - the phrase `omitted from context`.
- [ ] Implement summary text like:

```text
[read_file result omitted from context: 8123 chars]
```

- [ ] Do not persist this placeholder to archive; governance already works on copied messages.
- [ ] Run governance tests and bot_project wiring tests.

Acceptance:

- LLM context gets a useful compact placeholder.
- Persistent memory remains unchanged.

## Task 11: User-Turn Boundary Improvement

**Files:**

- Modify: `framework/memory/compaction/boundary.py`
- Test: `tests/unit/memory/compression/test_tool_chain.py`
- Test: `tests/unit/memory/test_compression_policies.py`

Steps:

- [ ] Add a concrete boundary implementation using the existing `BoundaryPolicy` interface, for
  example `UserTurnToolChainBoundaryPolicy`.
- [ ] It should prefer boundaries:
  - before a user message; or
  - after a completed assistant final response.
- [ ] It must still call or reuse tool-chain protection logic.
- [ ] Do not add a new boundary result object.
- [ ] Wire it into bot_project config only after tests pass.

Acceptance:

- Compression cuts old history at more natural turn boundaries.
- Existing `BoundaryPolicy` interface remains intact.

## Task 12: Verification and Regression Suite

Run these before considering the memory implementation complete:

```powershell
python -m pytest --rootdir F:\tool\pythonProject\ModexAgent F:\tool\pythonProject\ModexAgent\tests\unit\memory -q
python -m pytest --rootdir F:\tool\pythonProject\ModexAgent F:\tool\pythonProject\ModexAgent\tests\unit\bot_project -q
python -m pytest --rootdir F:\tool\pythonProject\ModexAgent F:\tool\pythonProject\ModexAgent\examples\bot_project\tests -q
```

Optional broader checks:

```powershell
ruff check F:\tool\pythonProject\ModexAgent\framework F:\tool\pythonProject\ModexAgent\tests
mypy F:\tool\pythonProject\ModexAgent\framework\memory
```

Acceptance:

- Unit tests pass.
- No new public architecture layer is introduced.
- bot_project still initializes main, peer, and subagent memory through existing wiring.

## Implementation Order

Recommended order:

1. Task 1: Empty archive write prevention.
2. Task 2: Empty summary commit semantics.
3. Task 3: Trigger no-op fix.
4. Task 4: Coordinator uses compaction decisions.
5. Task 5: Boundary regression coverage.
6. Task 6: DreamEngine/injection legacy filtering.
7. Task 8: bot_project coordinator wiring.
8. Task 9: tool_call_cleanup safety/default.
9. Task 10: Microcompact placeholder quality.
10. Task 7 and Task 11 after the first slice is stable.
11. Task 12 after each phase and before final handoff.

## Completion Criteria

The implementation is complete when:

- Empty archive records are not written.
- Legacy empty records are filtered defensively.
- No-op compression reports skipped semantics.
- Hidden-history no-op compression does not repeat on every append.
- Tool-call assistant and tool messages are excluded from summary input by default.
- bot_project uses the improved coordinator and safe cleanup policy.
- Memory unit tests, bot_project unit tests, and bot_project example tests pass.
