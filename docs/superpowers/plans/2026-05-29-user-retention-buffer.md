# UserRetentionBuffer Implementation Plan

> **For agentic workers:** Use subagent-driven-development to implement task-by-task.

**Goal:** Replace broken `pending_pruned_input` with `UserRetentionBuffer`, remove `_adjust_boundary_for_last_user`.

**Architecture:** Delete old pending files → create new URB (rename + `mark_all_completed` + dedup) → update framework refs → wire completion hook + cleanup extraction + governance injection → adapt bot_project. No backward compat.

---

### Task 1: Delete old pending files

**Files:** Delete `framework/memory/pending.py`, `framework/memory/layers/pending.py`, `tests/unit/memory/test_pending_pruned_inputs.py`, `tests/unit/memory/test_pending_injection_correctness.py`, `examples/bot_project/tests/test_pending_memory_config.py`

- [ ] Step 1: Delete the 5 files. `git rm` each.
- [ ] Step 2: Run `pytest tests/unit/memory/ tests/unit/messaging/ tests/unit/bot/ tests/unit/pipeline/ -q` → expect import errors from deleted modules
- [ ] Step 3: Commit

### Task 2: Create UserBufferEntry + UserRetentionBufferConfig

**Files:** Create `framework/memory/user_buffer.py`, `framework/memory/layers/user_buffer.py`

The new `user_buffer.py` replaces `pending.py`. Key differences from old `PendingPrunedInputEntry`:
- Fields renamed with `pruned_user_` prefix
- Added `completing_assistant_content: str | None` (None=unfinished, str=completed)
- Added `is_completed` property
- `from_message()` creates entries with `completing_assistant_content=None`

The new `layers/user_buffer.py` replaces `layers/pending.py`. Key API:
- `UserRetentionBuffer` ABC (was `PendingPrunedInputMemoryManager`)
- `ScopedUserRetentionBuffer` with `mark_all_completed(assistant_content: str)` and `upsert_pruned_user(entry)` (dedup + FIFO)
- `UserRetentionBufferConfig` (was `PendingPrunedInputMemoryConfig`)
- Storage key: `.user_retention_entries` (was `.pending_pruned_inputs`)

Full code for both files in the spec. Tests will be written in Task 11.

- [ ] Step 1: Write `framework/memory/user_buffer.py` (UserBufferEntry dataclass + factory methods)
- [ ] Step 2: Write `framework/memory/layers/user_buffer.py` (ScopedUserRetentionBuffer + ABC + Config)
- [ ] Step 3: Commit

### Task 3: Update `MemoryLayerName` and `MemoryLayerSet`

**Files:** Modify `framework/memory/core/scope.py:46`, `framework/memory/core/layers.py:240-260`, `framework/memory/core/models.py:74`, `framework/memory/layers/__init__.py`

- [ ] Step 1: In `scope.py`, change `MemoryLayerName.PENDING = "pending"` → `USER_RETENTION = "user_retention"`
- [ ] Step 2: In `layers.py`, rename `PendingPrunedInputMemoryManager` ABC → `UserRetentionBuffer` ABC, rename all method signatures
- [ ] Step 3: In `models.py`, rename `pending_pruned_input_entries` → `user_retention_entries`
- [ ] Step 4: In `layers/__init__.py`, update exports
- [ ] Step 5: Commit

### Task 4: Update all framework imports and references

**Files:** Modify `framework/memory/__init__.py`, `framework/memory/layers/config.py`, `framework/memory/layers/factory.py`, `framework/memory/system.py`, `framework/memory/default_system.py`, `framework/memory/context_governance.py`, `framework/memory/cleanup.py`, `framework/ioc/factories/memory.py`, `framework/ioc/factories/descriptors.py`, `framework/ioc/configs/memory.py`

- [ ] Step 1: `__init__.py` — replace all `PendingPruned*` / `pending_*` exports with `UserRetention*` / `user_*` equivalents
- [ ] Step 2: `layers/config.py` — rename `PendingPrunedInputMemoryConfig` → `UserRetentionBufferConfig`; update `MemoryLayerConfigSet.pending` → `.user_retention`; update `config.py` doc comments
- [ ] Step 3: `layers/factory.py` — rename `pending_manager` → `user_retention_manager`, `pending` → `user_retention` in MemoryLayerSet construction, `MemoryLayerName.PENDING` → `USER_RETENTION`
- [ ] Step 4: `system.py` — `pending_manager` → `user_retention`, update PendingInjectionGovernance import to UserRetentionBufferInjectionGovernance
- [ ] Step 5: `default_system.py` — `_pending_manager` → `_user_retention`, `pending_manager=` → `user_retention=`, update StorageFactory key
- [ ] Step 6: `context_governance.py` — rename `PendingInjectionGovernance` → `UserRetentionBufferInjectionGovernance`, update xml tag `<supplementary-context>` → `<pruned_conversation_context>`, metadata key `pending_pruned_inputs` → `user_retention_buffer`
- [ ] Step 7: `cleanup.py` — rename `pending` parameter → `user_retention`, update calls
- [ ] Step 8: `ioc/factories/memory.py` — rename all pending refs to user_retention
- [ ] Step 9: `ioc/factories/descriptors.py` — rename all pending refs
- [ ] Step 10: `ioc/configs/memory.py` — rename if needed
- [ ] Step 11: Run `pytest tests/unit/memory/test_content_format.py tests/unit/memory/test_injection_result.py tests/unit/memory/test_xml_truncate.py -v` to verify framework imports work
- [ ] Step 12: Commit

### Task 5: Remove `_adjust_boundary_for_last_user` and update cleanup extraction

**Files:** Modify `framework/memory/cleanup.py`, modify `tests/unit/memory/test_cleanup.py`

- [ ] Step 1: In `cleanup.py`, delete `_adjust_boundary_for_last_user()` function entirely
- [ ] Step 2: In `_compute_boundary()`, remove the call `boundary = _adjust_boundary_for_last_user(messages, boundary)`. Keep only: count-based boundary → `_adjust_boundary_for_tool_chains` → `_adjust_boundary_for_first_user`
- [ ] Step 3: Update `cleanup_session()` — rename `pending` param to `user_retention`, update pending_entries extraction to use `UserBufferEntry`, rename `pending_extracted` → `user_retention_extracted`
- [ ] Step 4: In `test_cleanup.py`, delete `test_always_keeps_recent_user_message` test, update `test_no_pending_when_pending_manager_is_none` → `test_no_urb_when_urb_is_none`
- [ ] Step 5: Run `pytest tests/unit/memory/test_cleanup.py -v` → all passing
- [ ] Step 6: Commit

### Task 6: Implement completion hook in ScopedMessageHistory

**Files:** Modify `framework/memory/default_system.py:60-75`

Add `_urb_completion_hook(msg)` call BEFORE `_run_cleanup()` in both `append()` and `extend()`:

```python
async def append(self, message: ChatMessage | dict[str, Any]) -> None:
    await self._manager.add_messages(self._context, [message])
    if self._recorder is not None:
        await self._recorder.record([message], self._context)
    if self._user_retention is not None:
        await self._urb_completion_hook(message)
    await self._run_cleanup()
    async with self._cache_lock:
        self._cache = None

async def _urb_completion_hook(self, message: ChatMessage | dict[str, Any]) -> None:
    """If message is a plain assistant (no tool_calls), mark all URB entries completed."""
    msg_dict = message.to_dict() if hasattr(message, "to_dict") else dict(message)
    if msg_dict.get("role") != MessageRole.ASSISTANT.value:
        return
    if msg_dict.get("tool_calls"):
        return
    await self._user_retention.mark_all_completed(msg_dict.get("content", ""))
```

- [ ] Step 1: Add `_urb_completion_hook` to `ScopedMessageHistory`
- [ ] Step 2: Update `__init__` docstring to reflect `user_retention` param rename
- [ ] Step 3: Update `DefaultMemorySystem.create_message_history()` to pass `user_retention=self._layers.user_retention`
- [ ] Step 4: Commit

### Task 7: Implement UserRetentionBufferInjectionGovernance

**Files:** Modify `framework/memory/context_governance.py:331-350` (was PendingInjectionGovernance)

Rename class and update injection format. Key changes:
- `_clear_if_session_completed` → check for plain assistant, clear URB
- XML format: `<pruned_conversation_context>` with `<entry>` children
- `truncatable_paths: ["pruned_user_content", "completing_assistant_content"]`
- `memory_source: "user_retention_buffer"` in metadata
- Agent entries: add `role="agent"` attribute on `<entry>`

```python
class UserRetentionBufferInjectionGovernance(ContextGovernance):
    def __init__(self, injector, context_factory=None):
        self._injector = injector
        self._context_factory = context_factory

    async def apply(self, messages):
        if self._injector is None:
            return messages
        context = self._context_factory() if self._context_factory else None
        if context is None:
            return messages
        return await self._injector.apply(list(messages), context)
```

Injector builds XML:
```python
xml_parts = ['<pruned_conversation_context>']
for entry in entries:
    role_attr = ' role="agent"' if entry.pruned_user_role == MessageRole.AGENT.value else ""
    xml_parts.append(f'  <entry{role_attr}>')
    xml_parts.append(f'    <pruned_user_content>{_xml_escape(entry.pruned_user_content)}</pruned_user_content>')
    if entry.completing_assistant_content:
        xml_parts.append(f'    <completing_assistant_content>{_xml_escape(entry.completing_assistant_content)}</completing_assistant_content>')
    xml_parts.append('  </entry>')
xml_parts.append('</pruned_conversation_context>')
```

- [ ] Step 1: Rename `PendingInjectionGovernance` → `UserRetentionBufferInjectionGovernance`
- [ ] Step 2: Update injector to use new XML format and field names
- [ ] Step 3: Update `system.py` `wrap_governance()` to use renamed class
- [ ] Step 4: Update `_after_system_messages` helper (unchanged, reused)
- [ ] Step 5: Commit

### Task 8: Hook + injector binding validation

**Files:** Modify `framework/memory/system.py`

In `MemorySystemContextManager.__init__` (or `wrap_governance`), validate that hook and injection are both enabled or both disabled:

```python
if bool(user_retention) != bool(injection_enabled):
    raise ValueError(
        "UserRetentionBuffer completion hook and injection governance "
        "must both be enabled or both disabled"
    )
```

- [ ] Step 1: Add validation in `wrap_governance()` or `MemorySystemContextManager.__init__`
- [ ] Step 2: Commit

### Task 9: Update bot_project adapters

**Files:** Modify `examples/bot_project/bot/service/builders.py`

- [ ] Step 1: Change import `PendingPrunedInputMemoryConfig` → `UserRetentionBufferConfig`
- [ ] Step 2: Update `_build_memory_layer_config()`: `layer_config.pending` → `layer_config.user_retention`
- [ ] Step 3: Update `_session_only_memory_config()`: `pending=PendingPrunedInputMemoryConfig(enabled=True)` → `user_retention=UserRetentionBufferConfig(enabled=True)`
- [ ] Step 4: In `_build_memory_layer_config`, remove the compat dict transform (`pending_pruned_inputs` → `pending`)
- [ ] Step 5: Run `pytest examples/bot_project/tests/ -q` → passing
- [ ] Step 6: Commit

### Task 10: Verify the session.jsonl bug is fixed

**Files:** None (verification only)

- [ ] Step 1: Write a targeted test in `tests/unit/memory/test_cleanup.py`:

```python
@pytest.mark.asyncio
async def test_single_user_session_cleans_properly(self, registry):
    """Session with 1 user + 100 tool pairs: cleanup must actually prune."""
    layer_set = _make_layer_set(registry)
    context = _ctx()
    session = layer_set.session
    msgs = [_user_msg("question")]
    for i in range(50):
        msgs.append(_tool_call_msg(f"call_{i}"))
        msgs.append(_tool_result_msg(f"call_{i}", f"result_{i}"))
    msgs.append(_assistant_msg("final answer"))
    await _add_messages(session, context, msgs)
    
    result = await cleanup_session(
        session=session, archive=None, context=context,
        max_messages=20, max_tokens=None, keep_ratio=0.5,
        archive_strategy=None, user_retention=layer_set.user_retention,
    )
    
    assert result.triggered is True
    assert result.messages_pruned > 0, "Must prune messages when over limit"
    remaining = await session.get_all_messages(context)
    assert len(remaining) < len(msgs), "Session must be smaller after cleanup"
```

- [ ] Step 2: Run the test → verify it fails (prunes 0 messages) with old code and passes with new code
- [ ] Step 3: Commit

### Task 11: Write comprehensive tests

**Files:** Create `tests/unit/memory/test_user_buffer.py`

- [ ] Step 1: Test `UserBufferEntry.from_message()` with user and agent roles
- [ ] Step 2: Test `UserBufferEntry.from_dict()` roundtrip
- [ ] Step 3: Test `is_completed` property
- [ ] Step 4: Test `ScopedUserRetentionBuffer.mark_all_completed()` — marks all unfinished entries
- [ ] Step 5: Test `ScopedUserRetentionBuffer.upsert_pruned_user()` — dedup + append + FIFO evict
- [ ] Step 6: Test unfinished entries always at tail (invariant)
- [ ] Step 7: Test XML injection format (`<pruned_conversation_context>`)
- [ ] Step 8: Test governance integration with `UserRetentionBufferInjectionGovernance`
- [ ] Step 9: Test hook + injector binding validation
- [ ] Step 10: Run `pytest tests/unit/memory/test_user_buffer.py -v` → all passing
- [ ] Step 11: Commit

### Task 12: Full test suite verification

- [ ] Step 1: Run `pytest tests/ -q` → all passing
- [ ] Step 2: Fix any remaining test failures from rename
- [ ] Step 3: Commit
