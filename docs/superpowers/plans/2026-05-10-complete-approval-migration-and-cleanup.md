# Complete Approval Migration & Runtime State Cleanup

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Complete the in-progress approval migration from old `SuspendStrategy`/`ApprovalStateStore` to `ApprovalTransaction`/`TurnSnapshot`, then clean up remaining legacy artifacts (`AgentContext.metadata`/`extensions`, `ReActRuntime`, compat properties).

**Architecture:** Phase 1 wraps up codex's uncommitted approval migration changes (doc cleanup, whitespace, regression, commit). Phase 2 removes `metadata`/`extensions` from `AgentContext` and migrates all remaining consumers. Phase 3 retires the old `ReActRuntime` class in favor of `AgentRuntime`. Phase 4 removes compat `checkpoint_store` properties from `AgentRuntime` and `RuntimeServicesConfig`.

**Tech Stack:** Python 3.12 dataclasses, `StrEnum`, `pytest`, `ruff`, `mypy`, existing `framework/runtime/`, `framework/agents/react/`, `framework/pipeline/`.

---

## File Structure Map

```
Phase 1 files (approval migration completion):
  Modify: framework/interceptor/builtin/AGENTS.md:37        # deny_as_cancel doc ref
  Fix:    *.py EOF blank lines (git diff --check)

Phase 2 files (AgentContext metadata/extensions removal):
  Modify: framework/core/agent.py                           # drop metadata, extensions fields
  Modify: framework/core/context_extensions.py              # drop ExtensionKey if no consumers left
  Modify: framework/agents/react/runtime.py:33-40,71-107   # stop reading from ctx.extensions
  Modify: framework/agents/react/agent.py                   # stop writing to ctx.metadata
  Modify: framework/hook/builtin/*.py                       # migrate to typed state
  Modify: framework/interceptor/builtin/*.py                # migrate to typed state  
  Modify: framework/pipeline/pipeline.py                    # migrate metadata writes
  Delete: framework/agents/react/runtime.py entire file     # merged into AgentRuntime
  Test:   tests/unit/core/test_agent_context.py

Phase 3 files (ReActRuntime retirement):
  Modify: framework/agents/react/agent.py                   # stop importing ReActRuntime
  Modify: framework/agents/react/assembler.py               # stop constructing ReActRuntime
  Modify: framework/agents/react/nodes/start.py             # stop reading ReActRuntime
  Modify: framework/agents/react/nodes/llm.py               # stop reading ReActRuntime
  Modify: framework/agents/react/nodes/tool.py              # stop reading ReActRuntime
  Modify: framework/agents/react/nodes/end.py               # stop reading ReActRuntime
  Modify: framework/session/agent_session.py                # stop using ReActRuntime
  Modify: framework/control/runtime.py                      # use AgentRuntime.services
  Test:   tests/unit/agents/react/test_runtime.py
  Test:   tests/unit/agents/react/test_nodes.py
  Test:   tests/unit/agents/react/test_agent.py

Phase 4 files (compat properties cleanup):
  Modify: framework/runtime/services.py:99-100             # remove checkpoint_store property
  Modify: framework/agents/react/assembler.py:41           # rename checkpoint_store -> turn_store
  Modify: framework/pipeline/approval_renderer.py:60-68   # remove checkpoint_store from __init__
  Test:   tests/unit/runtime/test_runtime_services.py
```

---

## Phase 1: Complete the In-Progress Approval Migration

### Task 1: Fix Remaining `deny_as_cancel` Documentation Reference

**Files:**
- Modify: `framework/interceptor/builtin/AGENTS.md:37`

- [ ] **Step 1: Update the AGENTS.md reference**

Replace the old `_deny_as_cancel` reference with the new `ApprovalDenyPolicy.CANCEL_TURN` semantic.

**Before (line 37):**
```markdown
- eeny policy: `_deny_as_cancel` flag set in `ctx.metadata` — ReActAgent detects and pads remaining tools
```

**After:**
```markdown
- deny policy: `ApprovalDenyPolicy.CANCEL_TURN` — denied approvals cancel the turn through `CancellationState` in `TurnStateBase`
```

Run to apply:
```bash
# Edit the file directly, or use the following sed-like approach:
```
Since the exact text of that line needs to be verified, read the file first then edit.

- [ ] **Step 2: Verify no other `deny_as_cancel` remain**

Run:
```bash
python -m pytest tests/unit/approval -q
```
Expected: all tests pass (no old references break).

- [ ] **Step 3: Commit**

```bash
git add framework/interceptor/builtin/AGENTS.md
git commit -m "docs: replace deny_as_cancel reference with ApprovalDenyPolicy.CANCEL_TURN"
```

---

### Task 2: Fix EOF Whitespace Issues

**Files:**
- Modify: `framework/agents/react/nodes/end.py` — trailing whitespace
- Modify: `framework/agents/react/approval.py` — blank line at EOF
- Modify: `framework/control/exceptions.py` — blank line at EOF
- Modify: `framework/pipeline/pipeline.py` — blank line at EOF
- Modify: multiple test files in `tests/unit/` — blank line at EOF

- [ ] **Step 1: Run git diff --check to see all issues**

Run:
```bash
git diff --check
```
Expected: lists files with trailing whitespace or new blank line at EOF.

- [ ] **Step 2: Fix each file**

For each file listed by `git diff --check`:
- If "new blank line at EOF": remove the trailing blank line (ensure file ends with exactly one newline, no extra blank line)
- If trailing whitespace: remove spaces/tabs at end of lines

The files identified in codex's output:
```
framework/agents/react/approval.py:74: new blank line at EOF.
framework/control/exceptions.py:72: new blank line at EOF.
framework/pipeline/pipeline.py:899: new blank line at EOF.
tests/unit/agents/react/test_verification.py:35: new blank line at EOF.
tests/unit/pipeline/test_approval_renderer_edge.py: new blank line at EOF.
```
(Plus any others found by the scan.)

- [ ] **Step 3: Verify clean**

Run:
```bash
git diff --check
```
Expected: no output (all clean).

- [ ] **Step 4: Commit**

```bash
git add -u
git commit -m "style: fix EOF whitespace issues"
```

---

### Task 3: Run Full Regression Suite

- [ ] **Step 1: Run approval and runtime tests**

```bash
python -m pytest tests/unit/approval tests/unit/runtime tests/unit/pipeline/test_approval_renderer_edge.py -v
```
Expected: all pass (codex reported 82 passed).

- [ ] **Step 2: Run ReAct agent tests**

```bash
python -m pytest tests/unit/agents/react -v
```
Expected: all pass.

- [ ] **Step 3: Run bot project tests**

```bash
$env:PYTHONPATH='.'; python -m pytest examples/bot_project/tests/ -v
```
Expected: all pass or skip.

- [ ] **Step 4: Run static checks**

```bash
ruff check framework tests examples/bot_project
```
Expected: pass (no new errors from our changes).

---

### Task 4: Final Legacy Symbol Scan

- [ ] **Step 1: Scan for removed symbols**

Run (using grep, not rg):
Search for these patterns in `framework/`, `tests/`, `examples/` `*.py` files:
```
suspend_strategy
ApprovalStateStore
LocalFileApprovalStateStore
InMemoryApprovalStateStore
TurnStateSuspendStrategy
approval_strategy (as field name on RuntimeServicesConfig)
TurnResumeState (in non-docstring context)
StateStoreTurnResumeStateStore
_current_resume
deny_as_cancel
from framework.approval.state import
from framework.approval.store import
from framework.control.checkpoint import
```

Expected: only occurrences in docstrings that explain historical migration (e.g., `framework/agents/react/state.py` line 3-4 which says "Replaces the old TurnResumeState..."). All code references must be gone.

- [ ] **Step 2: Verify deleted files are gone**

Run:
```bash
Test-Path "framework/control/checkpoint.py"  # must be False
Test-Path "framework/approval/store.py"       # must be False
Test-Path "framework/approval/state.py"       # must be False
Test-Path "framework/agents/react/strategy.py" # must be False
```

---

### Task 5: Commit the Approval Migration

- [ ] **Step 1: Verify git status shows only intentional changes**

Run:
```bash
git status --short
git diff --stat
```
Expected: all modified files are related to approval migration (approval, state, nodes, pipeline, tests), no unrelated changes.

- [ ] **Step 2: Commit with comprehensive message**

```bash
git add -A
git commit -m "refactor: complete approval migration to TurnSnapshot

Remove SuspendStrategy, ApprovalStateStore, TurnStateSuspendStrategy.
Delete framework/agents/react/strategy.py.
Delete framework/approval/store.py and framework/approval/state.py.
Replace old approval workflow with ApprovalTransaction inside TurnSnapshot.
Add ReActSnapshotPolicy.serialize_approval/approval_from_snapshot/state_from_snapshot.
Replace hardcoded snapshot keys with StrEnum (ReActSnapshotPayloadKey et al).
Simplify ApprovalRenderer to pure rendering + buffering service.
Pipeline uses _handle_snapshot_approval() + TurnStateStore for approval resume.
Add test_turn_state_approval_e2e.py for multi-tool approval flows.
Remove old approval e2e, store, state, batch_atomicity tests.
Update docstrings to replace deny_as_cancel with cancel_turn semantic."
```

---

## Phase 2: Remove `metadata`/`extensions` from AgentContext

### Task 6: Inventory All Consumers of `ctx.metadata` and `ctx.extensions`

**Files:**
- Search: entire `framework/` and `examples/` and `tests/`

- [ ] **Step 1: Search for metadata usage**

Run: search for `ctx.metadata`, `context.metadata`, `\.metadata\[`, `metadata\[` across `*.py` files in `framework/` and `examples/bot_project/bot/`.

- [ ] **Step 2: Search for extensions usage**

Run: search for `ctx.extensions`, `context.extensions`, `\.extensions\[` across `*.py` files in `framework/`.

- [ ] **Step 3: Classify each usage**

For each hit, determine:
- **Already migrated**: Uses `ctx.runtime.state` or `ctx.runtime.services` → safe
- **Needs migration**: Still reads/writes metadata/extensions → list for Tasks 7-8
- **Test-only**: Test code that accesses old API → update test to use new API
- **Docstring**: Historical migration note → update wording

Expected categories after codex's work:
- `framework/agents/react/runtime.py:37-38` → `ctx.extensions.pop()` in `sanitize_clean_runtime()` — **needs removal** (ReActRuntime is being retired in Phase 3)
- `framework/agents/react/runtime.py:90-107` → `ctx.extensions.pop()` in `from_context()` — **needs removal**
- `framework/hook/builtin/` → may still access `ctx.metadata` — **check**
- `framework/interceptor/builtin/` → may still access `ctx.metadata` — **check**
- `framework/pipeline/pipeline.py` → may write `input_metadata` — **check**

---

### Task 7: Migrate Remaining Hook Metadata Consumers

**Files:**
- Modify: `framework/hook/builtin/peer_auto_send.py` — if it reads `ctx.metadata`
- Modify: `framework/hook/builtin/runtime_context.py` — if it accesses `RuntimeContext`
- Modify: `framework/hook/builtin/inbox_flush.py` — if it accesses metadata
- Test: `tests/unit/multi_agent/test_peer_auto_send_hook.py`

Pattern for migration: Replace `ctx.metadata.get("key")` with `ctx.runtime.state` field access.

- [ ] **Step 1: For each hook still using metadata, write a test showing old behavior still works with new API**

```python
# tests/unit/hook/test_runtime_state_hooks.py (update existing or add)
async def test_peer_auto_send_hook_reads_completed_tool_calls_from_state():
    from framework.agents.react.state import ReActTurnState
    state = ReActTurnState(
        identity=TurnIdentity(agent_id="bot", session_id="s1", turn_id="t1"),
        agent_kind=AgentKind.REACT,
        phase=TurnPhase.RUNNING,
    )
    # Set up state with completed tool calls, then verify hook reads them
```

- [ ] **Step 2: Implement the migration in each hook**

Replace metadata access with typed state access.
Actual implementation depends on `Step 1` inventory results — each hook may need different changes.

- [ ] **Step 3: Run hook tests**

```bash
python -m pytest tests/unit/test_hooks.py tests/unit/hook/ -v
```

- [ ] **Step 4: Commit**

```bash
git add framework/hook/builtin tests/unit/hook
git commit -m "refactor: migrate hooks from ctx.metadata to typed state"
```

---

### Task 8: Migrate Remaining Interceptor Metadata Consumers

**Files:**
- Modify: `framework/interceptor/builtin/control_drain.py` — if it uses metadata
- Modify: `framework/interceptor/builtin/steer_inject.py` — if it uses metadata
- Modify: `framework/interceptor/builtin/tool_approval.py` — if it uses metadata
- Test: `tests/unit/test_interceptor_chain.py`, `tests/unit/test_tiered_tool_approval.py`

- [ ] **Step 1: Inventory interceptor metadata usage**

Read each builtin interceptor file and identify any `ctx.metadata` or `context.metadata` usage.

- [ ] **Step 2: Migrate each to use `turn_state` on context models**

Interceptors already receive context models (`ToolCallContext`, `TurnContext`, etc.) that include `turn_state: TurnStateBase`. Replace metadata reads with typed field reads.

- [ ] **Step 3: Run interceptor tests**

```bash
python -m pytest tests/unit/test_interceptor_chain.py tests/unit/test_tiered_tool_approval.py tests/unit/interceptor/ -v
```

- [ ] **Step 4: Commit**

```bash
git add framework/interceptor/builtin tests/unit/interceptor
git commit -m "refactor: migrate interceptors from ctx.metadata to typed turn_state"
```

---

### Task 9: Remove `metadata` and `extensions` from `AgentContext`

**Files:**
- Modify: `framework/core/agent.py:38-39`

- [ ] **Step 1: Write test verifying metadata/extensions are gone**

Update `tests/unit/core/test_agent_context.py`:

```python
from __future__ import annotations

from framework.core.agent import AgentContext
from framework.runtime.enums import AgentKind, TurnPhase
from framework.runtime.models import TurnIdentity, TurnStateBase
from framework.runtime.services import AgentRuntime, AgentRuntimeServices


def test_agent_context_no_longer_has_metadata_or_extensions() -> None:
    identity = TurnIdentity(agent_id="bot", session_id="s1", turn_id="t1")
    state = TurnStateBase(identity=identity, agent_kind=AgentKind.REACT, phase=TurnPhase.CREATED)
    runtime = AgentRuntime(services=AgentRuntimeServices(), state=state)
    ctx = AgentContext(
        identity=identity,
        system_prompt="system",
        history=None,
        tool_manager=None,
        runtime=runtime,
    )

    assert not hasattr(ctx, "metadata")
    assert not hasattr(ctx, "extensions")
```

- [ ] **Step 2: Run test — expected FAIL**

```bash
python -m pytest tests/unit/core/test_agent_context.py::test_agent_context_no_longer_has_metadata_or_extensions -v
```
Expected: FAIL, `metadata` attribute still exists.

- [ ] **Step 3: Remove fields from AgentContext**

In `framework/core/agent.py`, delete lines 38-39:
```python
    extensions: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
```

Also remove `Any` from the `typing` import if no other usage remains, or keep if `to_messages()` still uses it.

- [ ] **Step 4: Fix all compilation errors**

Run:
```bash
python -m pytest tests/unit/core -v
```
Fix any `AttributeError: 'AgentContext' object has no attribute 'metadata'` or `'extensions'` in framework code. Each fix should migrate the consumer to use `ctx.runtime.state` or `ctx.runtime.services` instead.

If any consumer truly needs session-level key-value storage, add a typed `session_data: dict[str, Any]` field with explicit justification in a comment. Do not restore the generic `metadata`/`extensions` bag.

- [ ] **Step 5: Run full framework tests**

```bash
python -m pytest tests/unit -v
```
Expected: all pass after consumer migrations from Tasks 7-8.

- [ ] **Step 6: Commit**

```bash
git add framework/core/agent.py framework/core/context_extensions.py tests/unit/core/test_agent_context.py
git commit -m "refactor: remove metadata and extensions from AgentContext"
```

---

## Phase 3: Retire `ReActRuntime` Class

### Task 10: Replace `ReActRuntime.from_context()` with `AgentRuntime`

**Files:**
- Modify: `framework/agents/react/agent.py` — stop creating `ReActRuntime`, use `ctx.runtime`
- Modify: `framework/agents/react/nodes/start.py` — use `ctx.runtime.services`
- Modify: `framework/agents/react/nodes/llm.py` — use `ctx.runtime.services`
- Modify: `framework/agents/react/nodes/tool.py` — use `ctx.runtime.services`
- Modify: `framework/agents/react/nodes/end.py` — use `ctx.runtime.services`
- Modify: `framework/agents/react/assembler.py` — stop populating `ctx.extensions` with runtime services
- Modify: `framework/session/agent_session.py` — use `AgentRuntimeServices`
- Modify: `framework/control/runtime.py` — use `AgentRuntime.services.control`
- Delete: `framework/agents/react/runtime.py` after all imports are removed

- [ ] **Step 1: Write test verifying ReActRuntime is no longer needed**

```python
# tests/unit/agents/react/test_runtime.py
from __future__ import annotations

import pytest
from framework.agents.react.agent import ReActAgent
from framework.core.agent import AgentContext
from framework.runtime.services import AgentRuntime, AgentRuntimeServices


async def test_react_agent_uses_agent_runtime_not_react_runtime(fake_provider, basic_tool_manager):
    """Verify ReActAgent runs with AgentRuntime, not old ReActRuntime."""
    from framework.agents.react.state import ReActTurnState
    from framework.runtime.enums import AgentKind, TurnPhase
    from framework.runtime.models import TurnIdentity
    from framework.memory.history import ListMessageHistory

    identity = TurnIdentity(agent_id="bot", session_id="s1", turn_id="t1")
    state = ReActTurnState(
        identity=identity,
        agent_kind=AgentKind.REACT,
        phase=TurnPhase.CREATED,
    )
    ctx = AgentContext(
        identity=identity,
        system_prompt="",
        history=ListMessageHistory(),
        tool_manager=basic_tool_manager,
        runtime=AgentRuntime(services=AgentRuntimeServices(), state=state),
    )
    agent = ReActAgent(fake_provider)
    # Should not raise AttributeError or import ReActRuntime
    result = await agent.run(ctx, _fake_emitter())
    assert result is not None
```

- [ ] **Step 2: Run test — expected FAIL**

Expected: fail because `ReActAgent.run()` still references `ReActRuntime.from_context()`.

- [ ] **Step 3: Update ReActAgent.run()**

In `framework/agents/react/agent.py`, find the `run()` method. Replace the `ReActRuntime.from_context(ctx, mode=...)` call and subsequent `runtime.validate()` / `runtime.sanitize_clean_runtime()` with direct access to `ctx.runtime`:

```python
# OLD (conceptual — actual code may differ):
# runtime = ReActRuntime.from_context(ctx, mode=self._mode)
# runtime.validate()

# NEW:
# clean mode: create AgentContext with AgentRuntimeServices() all None
# full mode: ctx.runtime already has services wired by assembler/pipeline
if self._mode == "clean":
    state = ReActTurnState(...)
    ctx.runtime = AgentRuntime(
        services=AgentRuntimeServices(),  # all None = clean
        state=state,
    )
```

- [ ] **Step 4: Update ReAct nodes**

In each node file (`nodes/start.py`, `llm.py`, `tool.py`, `end.py`), replace any access to `ReActRuntime` attributes with `ctx.runtime.services.X`:

| Old access | New access |
|---|---|
| `runtime.hooks` | `ctx.runtime.services.hooks` or `ctx.runtime.hooks` (delegation property) |
| `runtime.interceptors` | `ctx.runtime.services.interceptors` or `ctx.runtime.interceptors` |
| `runtime.approval` | `ctx.runtime.services.approval` or `ctx.runtime.approval` |
| `runtime.control` | `ctx.runtime.services.control` or `ctx.runtime.control` |
| `runtime.governance` | `ctx.runtime.services.governance` or `ctx.runtime.governance` |
| `runtime.checkpoint_store` | `ctx.runtime.services.turn_store` or `ctx.runtime.turn_store` |
| `runtime.injection_queue` | `ctx.runtime.services.pending_input_queue` |

- [ ] **Step 5: Update Pipeline and AgentSession**

In `framework/pipeline/pipeline.py` and `framework/session/agent_session.py`, stop calling `ReActRuntime.from_context()` and stop populating `ctx.extensions` with runtime service objects. Instead, create `AgentRuntimeServices` and set `ctx.runtime = AgentRuntime(services=..., state=...)`.

- [ ] **Step 6: Run full test suite**

```bash
python -m pytest tests/unit -v
```

- [ ] **Step 7: Delete ReActRuntime**

Once all tests pass without `ReActRuntime`:
```bash
git rm framework/agents/react/runtime.py
```

- [ ] **Step 8: Commit**

```bash
git add framework/agents/react framework/pipeline framework/session framework/control tests/unit
git commit -m "refactor: retire ReActRuntime, use AgentRuntime throughout"
```

---

## Phase 4: Remove Compat Properties

### Task 11: Remove `checkpoint_store` Compat Properties

**Files:**
- Modify: `framework/runtime/services.py:99-100` — remove `checkpoint_store` property
- Modify: `framework/agents/react/assembler.py:41` — rename `checkpoint_store` to `turn_store`
- Modify: `framework/pipeline/approval_renderer.py:60-68` — remove `checkpoint_store` from `__init__`
- Modify: `framework/core/agent_runtime_config.py` — remove `checkpoint_store` references
- Test: `tests/unit/runtime/test_runtime_services.py`

- [ ] **Step 1: Write test verifying checkpoint_store is gone**

```python
# tests/unit/runtime/test_runtime_services.py
def test_agent_runtime_has_no_checkpoint_store_compat() -> None:
    from framework.runtime.services import AgentRuntime, AgentRuntimeServices
    runtime = AgentRuntime(
        services=AgentRuntimeServices(),
        state=TurnStateBase(
            identity=TurnIdentity(agent_id="a", session_id="s", turn_id="t"),
            agent_kind=AgentKind.REACT,
            phase=TurnPhase.CREATED,
        ),
    )
    # checkpoint_store property should NOT exist
    with pytest.raises(AttributeError):
        _ = runtime.checkpoint_store
```

- [ ] **Step 2: Run test — expected FAIL**

- [ ] **Step 3: Remove checkpoint_store property**

In `framework/runtime/services.py`, delete lines 98-100:
```python
@property
def checkpoint_store(self) -> None:
    return None
```

- [ ] **Step 4: Fix callers that reference checkpoint_store**

Search for `checkpoint_store` in `framework/` and `examples/`:

```bash
# Use grep to find:
# checkpoint_store
# .checkpoint_store
```

For each reference:
- If it's `assembler.py` field `checkpoint_store: Any = None`: rename to `turn_store: TurnStateStore | None = None`
- If it's `approval_renderer.py.__init__` parameter: drop the parameter entirely (no longer needed)
- If it's `agent_runtime_config.py`: rename to `turn_store`
- If it's in test code: update to use `turn_store`

- [ ] **Step 5: Remove `memory_context` and `pending_injector` null compat properties**

In `framework/runtime/services.py`, delete lines 91-96:
```python
@property
def pending_injector(self) -> None:
    return None

@property
def memory_context(self) -> None:
    return None
```

These were only needed for `ReActRuntime` compat.

- [ ] **Step 6: Run full test suite**

```bash
python -m pytest tests/unit -v
```

- [ ] **Step 7: Commit**

```bash
git add framework/runtime/services.py framework/agents/react/assembler.py framework/pipeline/approval_renderer.py framework/core/agent_runtime_config.py tests/unit/runtime
git commit -m "refactor: remove checkpoint_store compat properties"
```

---

## Phase 5: Final Verification

### Task 12: End-to-End Verification

- [ ] **Step 1: Run all unit tests**

```bash
python -m pytest tests/unit -v
```

- [ ] **Step 2: Run bot project tests**

```bash
$env:PYTHONPATH='.'; python -m pytest examples/bot_project/tests/ -v
```

- [ ] **Step 3: Run static checks**

```bash
ruff check framework tests examples/bot_project
mypy framework
```

- [ ] **Step 4: Run integration/e2e tests**

```bash
python -m pytest tests/integration tests/e2e -v
```

- [ ] **Step 5: Final legacy scan**

Search for:
```
suspend_strategy
ApprovalStateStore
TurnStateSuspendStrategy
deny_as_cancel
_current_resume
TurnResumeState
StateStoreTurnResumeStateStore
ctx.extensions
ctx.metadata
context.metadata
context.extensions
ReActRuntime (as class, not docstring)
from.*react.runtime import
from.*control.checkpoint import
from.*approval.state import
from.*approval.store import
checkpoint_store (outside Phase 4 completion)
```

Expected: ZERO matches in code (docstring references to removed components are acceptable).

- [ ] **Step 6: Verify git is clean**

```bash
git diff --check
git status --short
```

Expected: no output from `diff --check`, all changes committed.

---

## Self-Review Notes

**Spec coverage:** This plan covers:
1. Completing codex's approval migration (Phase 1: Tasks 1-5)
2. Removing metadata/extensions from AgentContext (Phase 2: Tasks 6-9)
3. Retiring ReActRuntime class (Phase 3: Task 10)
4. Removing checkpoint_store compat properties (Phase 4: Task 11)
5. Final verification (Phase 5: Task 12)

**Placeholder scan:** Task 6 (inventory) requires actual search results before Tasks 7-8 can list specific consumers. This is intentional — the inventory must be done at execution time because the exact consumer list depends on the current uncommitted state. Tasks 7-8 have the migration pattern documented but the specific file changes will be determined by Task 6 results.

**Type consistency:** Throughout the plan:
- `AgentRuntime` consistently wraps `AgentRuntimeServices` + `TurnStateBase`
- `ctx.runtime.state` → typed as `TurnStateBase`, narrowed by `require_react_state()`
- `ctx.runtime.services` → `AgentRuntimeServices` with delegation properties on `AgentRuntime`
- `turn_store` consistently refers to `TurnStateStore` (not `checkpoint_store`)
