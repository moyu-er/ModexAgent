# Flush Legacy Runtime Artifacts — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Delete all remaining dead code, fix bugs introduced during Phase 1-4 migration, and bring the codebase to a clean state where `extensions`, `checkpoint_store`, `GraphMetaKey`, `ExtensionKey`, and legacy pipeline paths no longer exist.

**Architecture:** Five phases in dependency order. Phase A fixes immediate bugs (agent_session.py `metadata=` crash, pipeline `_prebuilt_runtime` crash). Phase B recovers 3 skipped test files. Phase C renames `checkpoint_store` to `turn_store` everywhere. Phase D migrates agent_session.py to AgentRuntimeServices. Phase E cleans up memory checkpoint duplication (P5). Phases A-D are self-contained; Phase E is exploratory. Phase F-G (Bot Rewire, Final Cleanup) are separate plans.

**Tech Stack:** Python 3.12, pytest, ruff, mypy, existing `framework/runtime/` and `framework/core/`.

**Tracking:** Each completed step will be checked off (`[x]`) in this document. On step completion the file will be re-saved.

---

## ⚠️ Progress Dashboard

| Phase | Tasks | Description | Status |
|-------|-------|-------------|:------:|
| A | 1-4 | Immediate Bug Fixes & Dead Code Removal | ✅ Done |
| B | 5-7 | Recover 3 Skipped Test Files | ✅ Done |
| C | 8-16 | `checkpoint_store` → `turn_store` Everywhere | ✅ Done |
| D | 17-19 | Migrate agent_session.py → AgentRuntimeServices | ✅ Done |
| E | — | Delete dead _save/_clear_checkpoint (P5) | ✅ Done |
| G | — | Deprecation shim removal + ruff fix (P7) | ✅ Done |
| F | TBD | Bot Project Rewire (P6) — Separate Plan | 🔴 |

**Current test baseline:** 1273 passed, 1 failed (pre-existing summarizer), 11 skipped.
**Last commit:** `0d61066 fix: remove unused logger variable in AgentRuntime.validate()`

---

## File Map

```
DELETE (dead code):
  framework/core/context_extensions.py          # ExtensionKey — only agent_session + pipeline still import
  framework/core/graph/constants.py:10-12       # GraphMetaKey class — only 2 test files import for it
  tests/unit/agents/react/test_runtime.py       # Import deleted ReActRuntime class → rewrite

MODIFY (bug fixes + cleanup):
  framework/core/agent.py:38                    # Remove `extensions` field
  framework/core/graph/__init__.py:2,13         # Remove GraphMetaKey export
  framework/session/agent_session.py            # Fix metadata= bug, clean extensions, migrate to AgentRuntimeServices
  framework/pipeline/pipeline.py                # Remove _prebuilt_runtime legacy, extensions dict, rename checkpoint_store
  framework/pipeline/approval_renderer.py:62,68 # Rename checkpoint_store param
  framework/agents/react/agent.py:263-284       # _save_checkpoint / _clear_checkpoint — fix API mismatch
  framework/agents/react/assembler.py:44,97     # Rename checkpoint_store → turn_store
  framework/core/agent_runtime_config.py:34,50  # Rename checkpoint_store → turn_store
  framework/multi_agent/factory.py:81,93,265,306 # Rename checkpoint_store → turn_store
  framework/control/runtime.py                  # Check for ReActRuntime / extensions usage

TESTS to fix:
  tests/unit/agents/react/test_assembler.py     # Update to AgentRuntime
  tests/unit/bot_project/test_bot_project_runtime_wiring.py  # Update to new API
  tests/unit/core/graph/test_engine.py:6        # Remove GraphMetaKey import
  tests/unit/agents/react/test_verification.py:23 # Remove GraphMetaKey import
  tests/unit/agents/react/test_react_agent.py   # Update checkpoint_store → turn_store references
```

---

## Phase A: Immediate Bug Fixes & Dead Code Removal

These are the bugs found during the 2026-05-10 code review — code that would crash at runtime if exercised.

### Task 1: Fix `agent_session.py:383` — `metadata=` TypeError

**Files:**
- Modify: `framework/session/agent_session.py:375-388`

**Background:** `AgentContext` no longer has a `metadata` field (deleted in commit `855fa97`). Line 383 passes `metadata={"session_id": session_id, "agent_name": agent_name}` to the constructor, which would raise `TypeError: AgentContext.__init__() got an unexpected keyword argument 'metadata'`. This bug has not been caught because bot project tests were not run after the migration.

- [ ] **Step 1: Remove `metadata=` from AgentContext construction**

Before (lines 375-388):
```python
agent_context = AgentContext(
    system_prompt=context_state.system_prompt,
    history=context_state.history,
    tool_manager=self._tool_manager,
    session_id=session_id,
    max_iterations=getattr(self._agent, "max_iterations", 10),
    temperature=getattr(message, "metadata", {}).get("temperature"),
    max_tokens=getattr(message, "metadata", {}).get("max_tokens"),
    metadata={"session_id": session_id, "agent_name": agent_name},
    extensions={
        ExtensionKey.RUNTIME_CTX_MGR: self._runtime_context_manager,
        ExtensionKey.ON_CHECKPOINT: on_checkpoint,
    },
)
agent_context.identity = turn_identity
```

After:
```python
agent_context = AgentContext(
    system_prompt=context_state.system_prompt,
    history=context_state.history,
    tool_manager=self._tool_manager,
    session_id=session_id,
    max_iterations=getattr(self._agent, "max_iterations", 10),
    temperature=getattr(message, "metadata", {}).get("temperature"),
    max_tokens=getattr(message, "metadata", {}).get("max_tokens"),
    extensions={
        ExtensionKey.RUNTIME_CTX_MGR: self._runtime_context_manager,
        ExtensionKey.ON_CHECKPOINT: on_checkpoint,
    },
)
agent_context.identity = turn_identity
```

- [ ] **Step 2: Verify the file parses**

```powershell
python -c "import framework.session.agent_session; print('OK')"
```
Expected: No `TypeError` or `ImportError`.

- [ ] **Step 3: Commit**

```bash
git add framework/session/agent_session.py
git commit -m "fix: remove metadata= kwarg from AgentContext construction in AgentSession"
```

---

### Task 2: Fix Pipeline `_prebuilt_runtime` Legacy Path Crash

**Files:**
- Modify: `framework/pipeline/pipeline.py:585-594`

**Background:** Lines 585-594 set `agent_context.runtime.memory_context` and `agent_context.runtime.pending_injector` on an `AgentRuntime` object. These properties were deleted from `AgentRuntime` in commit `6f0c77d`. If this code path executes (prebuilt_runtime without turn_store), it raises `AttributeError`.

**Analysis:** This path is reached only when `self._prebuilt_runtime is not None` AND `self.turn_store is None` (line 562 guards the new path). The prebuilt runtime is a bot-project-specific feature. Since bot project is not yet rewired (P6), this path is effectively unused. We delete it rather than fix it.

- [ ] **Step 1: Remove the legacy `_prebuilt_runtime` else-branch**

Open `framework/pipeline/pipeline.py` and locate lines 585-594:

```python
elif self._prebuilt_runtime is not None:
    # Legacy prebuilt runtime (backward compat)
    agent_context.runtime = self._prebuilt_runtime
    agent_context.runtime.memory_context = memory_context
    if pending is not None:
        from framework.memory.pending import DefaultPendingPrunedInputInjector
        agent_context.runtime.pending_injector = DefaultPendingPrunedInputInjector(
            pending,
            session,
        )
```

Replace with:

```python
elif self._prebuilt_runtime is not None:
    # Legacy path — prebuilt runtime without turn_store.
    # Bot project not yet rewired (P6), so this path is unused.
    # If reached, use the prebuilt runtime directly without
    # setting deleted memory_context/pending_injector properties.
    agent_context.runtime = self._prebuilt_runtime
```

- [ ] **Step 2: Verify the file compiles**

```powershell
python -c "import framework.pipeline.pipeline; print('OK')"
```
Expected: No errors.

- [ ] **Step 3: Update the docstring on `prebuilt_runtime` parameter**

In `pipeline.py`, update the `prebuilt_runtime` parameter docstring (around line 160) to add "DEPRECATED: will be removed after P6 Bot Rewire".

- [ ] **Step 4: Commit**

```bash
git add framework/pipeline/pipeline.py
git commit -m "fix: remove crashy memory_context/pending_injector assignments in pipeline legacy path"
```

---

### Task 3: Delete `AgentContext.extensions` Field

**Files:**
- Modify: `framework/core/agent.py:38`
- Modify: `framework/core/agent.py:29` (docstring)

**Background:** The `extensions` field on `AgentContext` was kept during Phase 2 only because `ReActRuntime.from_context()` still read from it. `ReActRuntime` is now deleted, and the two remaining consumers (`agent_session.py`, `pipeline.py`) will be migrated to use `AgentRuntimeServices` in later phases. The field itself is dead weight.

**But:** Deleting `extensions` right now would break `agent_session.py:384-387` and `pipeline.py:519-557` which still set the field. We need a different strategy: make `extensions` private (`_extensions`) so it's not part of the public API, and delete it entirely in Phase D when agent_session.py is migrated.

Alternatively, we can just leave it and delete it in Phase D together with `context_extensions.py`. Let me reconsider...

The priority order matters. `extensions` is set by both pipeline and agent_session.py. We can't delete it until those consumers stop writing to it. But Task 1 (fixing metadata=) doesn't need `extensions` to be deleted — it just removes a wrong kwarg. Tasks 2 and 3 can be reordered.

Let me change the approach: Task 3 should be the cleanup of `context_extensions.py` and `GraphMetaKey` first (these are truly dead), saving `extensions` field deletion for Phase D when both consumers are migrated.

- [ ] **Step 1: Update docstring on `AgentContext`**

In `framework/core/agent.py` line 28, update:
```python
"""Agent execution context — typed runtime state replaces metadata/extensions."""
```
to:
```python
"""Agent execution context — typed runtime state via ``runtime`` field.

Historical ``metadata`` and ``extensions`` bags are deprecated.
The ``extensions`` field remains only for agent_session + pipeline
compatibility while those are migrated (Phase D). Do not add new keys.
"""
```

- [ ] **Step 2: Add deprecation comment on the field**

In `framework/core/agent.py` line 38, change:
```python
extensions: dict[str, Any] = field(default_factory=dict)
```
to:
```python
# DEPRECATED (Phase D): delete after agent_session + pipeline migration.
extensions: dict[str, Any] = field(default_factory=dict)
```

- [ ] **Step 3: Commit**

```bash
git add framework/core/agent.py
git commit -m "docs: mark AgentContext.extensions as deprecated (delete in Phase D)"
```

---

### Task 4: Delete `context_extensions.py` and `GraphMetaKey`

**Files:**
- Delete: `framework/core/context_extensions.py`
- Modify: `framework/core/graph/constants.py` — remove `GraphMetaKey` class
- Modify: `framework/core/graph/__init__.py` — remove `GraphMetaKey` export
- Modify: `framework/session/agent_session.py:15,384-387` — inline `ExtensionKey` values
- Modify: `framework/pipeline/pipeline.py:27,519-521` — inline `ExtensionKey` values
- Modify: `tests/unit/core/graph/test_engine.py:6` — remove `GraphMetaKey` import
- Modify: `tests/unit/agents/react/test_verification.py:23` — remove `GraphMetaKey` import

**Background:** `context_extensions.py` defines `ExtensionKey` with 6 string constants. Only `RUNTIME_CTX_MGR` and `ON_CHECKPOINT` are still used, both by `agent_session.py` and `pipeline.py`. The other 4 keys are dead. `GraphMetaKey` has only `GRAPH_RESULT` which was migrated to `TurnCustomKey.GRAPH_RESULT`.

- [ ] **Step 1: Check `ExtensionKey` consumers — verify only RUNTIME_CTX_MGR and ON_CHECKPOINT**

Run:
```powershell
rg "ExtensionKey\." framework/ --type py
```
Expected output: only `RUNTIME_CTX_MGR` and `ON_CHECKPOINT` references in `agent_session.py` and `pipeline.py`.

- [ ] **Step 2: Inline `ExtensionKey` values in `agent_session.py`**

In `framework/session/agent_session.py`:
- Remove line 15: `from ..core.context_extensions import ExtensionKey`
- Change lines 384-386:
  ```python
  extensions={
      ExtensionKey.RUNTIME_CTX_MGR: self._runtime_context_manager,
      ExtensionKey.ON_CHECKPOINT: on_checkpoint,
  },
  ```
  to:
  ```python
  extensions={
      "runtime_context_manager": self._runtime_context_manager,
      "on_checkpoint": on_checkpoint,
  },
  ```

- [ ] **Step 3: Inline `ExtensionKey` values in `pipeline.py`**

In `framework/pipeline/pipeline.py`:
- Remove line 27: `from ..core.context_extensions import ExtensionKey`
- Change lines 519-521:
  ```python
  extensions: dict[Any, Any] = {
      ExtensionKey.RUNTIME_CTX_MGR: self.runtime_context_manager,
  }
  ```
  to:
  ```python
  extensions: dict[Any, Any] = {
      "runtime_context_manager": self.runtime_context_manager,
  }
  ```

- [ ] **Step 4: Delete `GraphMetaKey` from `framework/core/graph/constants.py`**

Remove lines 10-12:
```python
class GraphMetaKey:
    """Keys used in ctx.metadata by the graph engine."""
    GRAPH_RESULT = "_graph_result"
```

After removal, `framework/core/graph/constants.py` should contain only:
```python
"""Core graph constants."""
from enum import StrEnum


class GraphNode(StrEnum):
    """Engine-recognized sentinel node names."""
    END = "__end__"
```

- [ ] **Step 5: Remove `GraphMetaKey` from `framework/core/graph/__init__.py`**

Remove `GraphMetaKey` from the import (line 2) and from `__all__` (line 13):
```python
# Before:
from .constants import GraphMetaKey, GraphNode
# After:
from .constants import GraphNode

# Before __all__:
#     "GraphMetaKey",
# After: remove that line
```

- [ ] **Step 6: Update test imports**

In `tests/unit/core/graph/test_engine.py` line 6:
```python
# Before:
from framework.core.graph.constants import GraphNode, GraphMetaKey
# After:
from framework.core.graph.constants import GraphNode
```

Check if `GraphMetaKey` is actually used in test bodies — search for `GraphMetaKey` within the file and replace any usage with `TurnCustomKey.GRAPH_RESULT`.

In `tests/unit/agents/react/test_verification.py` line 23:
```python
# Before:
from framework.core.graph.constants import GraphNode, GraphMetaKey
# After:
from framework.core.graph.constants import GraphNode
```
Same check — replace any `GraphMetaKey.GRAPH_RESULT` usage with `TurnCustomKey.GRAPH_RESULT`.

- [ ] **Step 7: Delete `framework/core/context_extensions.py`**

```powershell
Remove-Item -LiteralPath "framework\core\context_extensions.py"
```

- [ ] **Step 8: Run unit tests to verify no breakage**

```powershell
python -m pytest tests/unit/ -x -q
```
Expected: all tests pass. If any test imports `ExtensionKey` or `GraphMetaKey`, fix those imports.

- [ ] **Step 9: Commit**

```bash
git add -A
git commit -m "refactor: delete context_extensions.py and GraphMetaKey, inline remaining ExtensionKey values"
```

---

## Phase B: Recover 3 Skipped Test Files

### Task 5: Rewrite `test_runtime.py` — Remove ReActRuntime Tests

**Files:**
- Delete: `tests/unit/agents/react/test_runtime.py`

**Background:** This file imports `from framework.agents.react.runtime import ReActRuntime, sanitize_clean_runtime`. Both are deleted. The tests covered:
1. `ReActRuntime.clean()` factory → replaced by `AgentRuntime(services=AgentRuntimeServices())`
2. `ReActRuntime.from_context()` → replaced by direct `AgentRuntime` construction
3. `sanitize_clean_runtime()` → replaced by pipeline's `AgentRuntimeServices()` all-None approach

These behaviors are already covered by other tests (`test_react_agent.py`, pipeline tests). No need to rewrite — just delete.

- [ ] **Step 1: Delete the file**

```powershell
Remove-Item -LiteralPath "tests\unit\agents\react\test_runtime.py"
```

- [ ] **Step 2: Remove from pytest --ignore if present**

Search for `test_runtime` in `pyproject.toml` or `pytest.ini` and remove any `--ignore` entry.

- [ ] **Step 3: Run tests to confirm no import error**

```powershell
python -m pytest tests/unit/ -x -q
```
Expected: No `ImportError` for `ReActRuntime` or `sanitize_clean_runtime`.

- [ ] **Step 4: Commit**

```bash
git rm tests/unit/agents/react/test_runtime.py
git commit -m "refactor: delete test_runtime.py (ReActRuntime class is removed)"
```

---

### Task 6: Update `test_assembler.py` — Return Type Change

**Files:**
- Modify: `tests/unit/agents/react/test_assembler.py`

**Background:** `RuntimeAssembler.assemble()` now returns `AgentRuntime` instead of `ReActRuntime`. The test file needs its assertions updated.

- [ ] **Step 1: Read the current test file**

```powershell
python -m pytest tests/unit/agents/react/test_assembler.py -v 2>&1 | Select-Object -First 30
```
Identify all failing assertions and imports.

- [ ] **Step 2: Fix imports and assertions**

Replace any `ReActRuntime` import with:
```python
from framework.runtime.services import AgentRuntime, AgentRuntimeServices
```

Replace any `assert isinstance(runtime, ReActRuntime)` with:
```python
assert isinstance(runtime, AgentRuntime)
```

Replace any `runtime.checkpoint_store` with `runtime.services.turn_store` or `runtime.turn_store`.

- [ ] **Step 3: Run the test**

```powershell
python -m pytest tests/unit/agents/react/test_assembler.py -v
```
Expected: all tests pass.

- [ ] **Step 4: Commit**

```bash
git add tests/unit/agents/react/test_assembler.py
git commit -m "test: update test_assembler.py for AgentRuntime return type"
```

---

### Task 7: Update `test_bot_project_runtime_wiring.py` — New API

**Files:**
- Modify: `tests/unit/bot_project/test_bot_project_runtime_wiring.py`

**Background:** This integration test uses old ReActRuntime / checkpoint_store APIs. Since bot project rewiring is P6 (separate plan), this test will be partially broken. Minimally fix imports and make it run without `ImportError`, then skip tests that depend on unwired bot services with `@pytest.mark.skip(reason="P6 Bot Rewire pending")`.

- [ ] **Step 1: Run the test to see errors**

```powershell
$env:PYTHONPATH='.'; python -m pytest tests/unit/bot_project/test_bot_project_runtime_wiring.py -v 2>&1 | Select-Object -First 50
```

- [ ] **Step 2: Fix import errors**

Replace any `ReActRuntime` import with `AgentRuntime`. Replace `checkpoint_store` with `turn_store`.

- [ ] **Step 3: Skip tests that need P6**

Add `@pytest.mark.skip(reason="Requires P6 Bot Rewire")` to any test that fails due to unwired bot services (not due to simple API rename).

- [ ] **Step 4: Run to verify no import errors**

```powershell
$env:PYTHONPATH='.'; python -m pytest tests/unit/bot_project/test_bot_project_runtime_wiring.py -v
```
Expected: no `ImportError`; tests either pass or are skipped.

- [ ] **Step 5: Commit**

```bash
git add tests/unit/bot_project/test_bot_project_runtime_wiring.py
git commit -m "test: update bot project test for new runtime API, skip P6-dependent tests"
```

- [ ] **Step 6: Verify full unit test suite**

```powershell
python -m pytest tests/unit/ -v --tb=short 2>&1 | Select-Object -Last 10
```
Expected: 0 import errors. All tests pass or skip. No `--ignore` needed.

- [ ] **Step 7: Commit if any pyproject.toml changes**

```bash
git add pyproject.toml  # if --ignore entries were removed
git commit -m "build: remove --ignore entries for deleted test files"
```

---

## Phase C: Rename `checkpoint_store` → `turn_store` Everywhere

**Goal:** The old name `checkpoint_store` persists across ~15 locations in 7 files. The design doc specifies the new contract is `TurnStateStore` accessed via `turn_store`. Rename all parameter names, field names, and local variable names while preserving the runtime behavior.

**Scope:**
- `framework/pipeline/pipeline.py`: `checkpoint_store` param + field + `ApprovalRenderer` arg
- `framework/pipeline/approval_renderer.py`: `checkpoint_store` param + field
- `framework/session/agent_session.py`: `checkpoint_store` param + field
- `framework/agents/react/assembler.py`: `checkpoint_store` field in `RuntimeServicesConfig`
- `framework/core/agent_runtime_config.py`: `checkpoint_store` field + `RuntimeControl` usage
- `framework/multi_agent/factory.py`: `checkpoint_store` param + field
- `framework/agents/react/agent.py`: local variable `checkpoint_store` in `_save_checkpoint`

### Task 8: Rename in `pipeline.py`

**Files:**
- Modify: `framework/pipeline/pipeline.py:155,205,214,562,571-578,583`

- [ ] **Step 1: Rename constructor parameter**

Line 155:
```python
# Before:
checkpoint_store: Any | None = None,
# After:
turn_store_legacy: Any | None = None,  # DEPRECATED: use turn_store
```

Wait — `pipeline.py` already has BOTH `checkpoint_store` and `turn_store` as separate parameters (line 155 and 161). Check the actual relationship...

Actually, looking at lines 155 and 161:
```python
checkpoint_store: Any | None = None,  # old memory checkpoint store
turn_store: Any | None = None,        # new TurnStateStore
```

These are DIFFERENT concepts. `checkpoint_store` is the old memory checkpoint store (used for `_save_checkpoint` in agent.py). `turn_store` is the new `TurnStateStore`.

The `checkpoint_store` parameter is passed to `ApprovalRenderer` (line 214) and to `RuntimeAssembler` (via `RuntimeServicesConfig`). Both need to be migrated, but they can't just be renamed to `turn_store` because that would collide with the existing `turn_store` parameter.

Let me reconsider the scope. The `checkpoint_store` vs `turn_store` distinction in pipeline.py is:
- `checkpoint_store` → old memory checkpoint (save/load/clear) — used by ApprovalRenderer and agent._save_checkpoint
- `turn_store` → new TurnStateStore (save_turn/load_turn/delete_turn) — used by new runtime

These are genuinely different. The rename task needs to be more nuanced: not a simple rename, but a consolidation where the old checkpoint store's functionality is provided by `turn_store` or removed.

Given this complexity, let me adjust the task to be exploratory first, then rename.

- [ ] **Step 1: Audit all `checkpoint_store` semantics**

Read each `checkpoint_store` reference and classify:
- **A) Memory checkpoint** (`save(id, data)`, `load(id)`, `clear(id)`): agent.py `_save_checkpoint`, approval_renderer
- **B) TurnStateStore** (`save_turn(snapshot)`, etc.): assembler.py's `RuntimeServicesConfig.checkpoint_store`
- **C) Factory/IoC wiring**: multi_agent/factory.py, agent_session.py

Report findings in task completion.

- [ ] **Step 2: Rename category-B usages (assembler.py)**

In `framework/agents/react/assembler.py` line 44:
```python
# Before:
checkpoint_store: Any = None               # TurnStateStore
# After:
turn_store: Any = None                     # TurnStateStore
```

Line 97:
```python
# Before:
turn_store=config.checkpoint_store,
# After:
turn_store=config.turn_store,
```

Update all callers that pass `checkpoint_store=` to `RuntimeServicesConfig`:
- `pipeline.py` (if any) — `checkpoint_store=` → `turn_store=`
- `agent_session.py:401` — `checkpoint_store=` → `turn_store=`

- [ ] **Step 3: Rename pipeline's own field (multi-step)**

In `pipeline.py`, rename `self.checkpoint_store` to `self._legacy_checkpoint_store` to clarify it's the old memory store:

Line 205: `self.checkpoint_store = checkpoint_store` → `self._legacy_checkpoint_store = checkpoint_store`

Line 214: `checkpoint_store=checkpoint_store,` → `checkpoint_store=self._legacy_checkpoint_store,` (keeping ApprovalRenderer param name for now)

- [ ] **Step 4: Verify tests pass**

```powershell
python -m pytest tests/unit/ -x -q
```
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add framework/agents/react/assembler.py framework/pipeline/pipeline.py framework/session/agent_session.py
git commit -m "refactor: rename RuntimeServicesConfig.checkpoint_store to turn_store"
```

---

### Task 9: Rename in `agent_session.py`

**Files:**
- Modify: `framework/session/agent_session.py:85,133,392,401`

- [ ] **Step 1: Rename constructor parameter**

Line 85:
```python
# Before:
checkpoint_store: Any | None = None,
# After:
turn_store: Any | None = None,
```

Line 133:
```python
# Before:
self._checkpoint_store = checkpoint_store
# After:
self._turn_store = turn_store
```

Line 392:
```python
# Before:
if self._hook_runner or self._interceptor_chain or self._checkpoint_store:
# After:
if self._hook_runner or self._interceptor_chain or self._turn_store:
```

Line 401:
```python
# Before:
checkpoint_store=self._checkpoint_store,
# After:
turn_store=self._turn_store,
```

- [ ] **Step 2: Update callers**

Search for `AgentSession(.*checkpoint_store)` in `examples/` and `tests/` and rename each.

```powershell
rg "checkpoint_store" examples/ tests/ --type py
```

- [ ] **Step 3: Verify**

```powershell
python -m pytest tests/unit/ -x -q
```

- [ ] **Step 4: Commit**

```bash
git add framework/session/agent_session.py examples/ tests/
git commit -m "refactor: rename agent_session checkpoint_store to turn_store"
```

---

### Task 10: Rename in `agent_runtime_config.py`

**Files:**
- Modify: `framework/core/agent_runtime_config.py:34,50`

- [ ] **Step 1: Read the file**

Read `framework/core/agent_runtime_config.py` to understand the `checkpoint_store` usage context.

- [ ] **Step 2: Rename field and usage**

```python
# Before (line ~34):
checkpoint_store: TurnStateStore | None = None

# After:
turn_store: TurnStateStore | None = None
```

Update line ~50 where `checkpoint_store=store` is passed to replace with `turn_store=store`.

- [ ] **Step 3: Update callers**

```powershell
rg "checkpoint_store" framework/ examples/ --type py
```
Rename any remaining references in caller code.

- [ ] **Step 4: Commit**

```bash
git add framework/core/agent_runtime_config.py
git commit -m "refactor: rename agent_runtime_config checkpoint_store to turn_store"
```

---

### Task 11: Rename in `multi_agent/factory.py`

**Files:**
- Modify: `framework/multi_agent/factory.py:81,93,265,306`

- [ ] **Step 1: Rename field and usages**

Rename `default_checkpoint_store` → `default_turn_store`, `self._default_checkpoint_store` → `self._default_turn_store`, and all `checkpoint_store=` kwarg usages.

- [ ] **Step 2: Verify**

```powershell
python -m pytest tests/unit/multi_agent/ -x -q
```

- [ ] **Step 3: Commit**

```bash
git add framework/multi_agent/factory.py
git commit -m "refactor: rename multi_agent factory checkpoint_store to turn_store"
```

---

### Task 12: Rename in `approval_renderer.py`

**Files:**
- Modify: `framework/pipeline/approval_renderer.py:62,68`

- [ ] **Step 1: Rename parameter and field**

```python
# Before (line 62):
checkpoint_store: object | None = None,
# After:
checkpoint_store: object | None = None,  # legacy memory checkpoint, remove in P5

# Before (line 68):
self.checkpoint_store = checkpoint_store
# After:
self.checkpoint_store = checkpoint_store  # legacy memory checkpoint, remove in P5
```

The `ApprovalRenderer` uses this for old-style memory checkpoint save/load. This will be addressed in P5 (Memory Checkpoint Cleanup). For now, add a deprecation comment.

- [ ] **Step 2: Commit**

```bash
git add framework/pipeline/approval_renderer.py
git commit -m "docs: mark ApprovalRenderer.checkpoint_store as deprecated (P5 cleanup)"
```

---

### Task 13: Rename Local Variable in `agent.py`

**Files:**
- Modify: `framework/agents/react/agent.py:269,270,275,279,280,284`

- [ ] **Step 1: Rename local variable**

```python
# Before (line 269):
checkpoint_store = context.runtime.turn_store if context.runtime else None
if checkpoint_store is None:
    return
...
await checkpoint_store.save(checkpoint_id, data)
...
checkpoint_store = context.runtime.turn_store if context.runtime else None
...
await checkpoint_store.clear(checkpoint_id)

# After:
store = context.runtime.turn_store if context.runtime else None
if store is None:
    return
...
await store.save(checkpoint_id, data)
...
store = context.runtime.turn_store if context.runtime else None
...
await store.clear(checkpoint_id)
```

- [ ] **Step 2: Verify**

```powershell
python -m pytest tests/unit/agents/react/ -x -q
```

- [ ] **Step 3: Commit**

```bash
git add framework/agents/react/agent.py
git commit -m "refactor: rename local checkpoint_store variable in agent.py"
```

---

### Tasks 14-16: Final `checkpoint_store` Sweep

- [ ] **Task 14: Global sweep for remaining `checkpoint_store`**

```powershell
rg "checkpoint_store" framework/ examples/ tests/ --type py
```

Expected results after Phase C:
- `pipeline.py`: `_legacy_checkpoint_store` (annotated DEPRECATED) + `checkpoint_store` in `__init__` param (annotated DEPRECATED)
- `approval_renderer.py`: annotated DEPRECATED
- All other files: ZERO matches for `checkpoint_store` (except test code that will be updated in Task 15)

- [ ] **Task 15: Update test code references**

```powershell
rg "checkpoint_store" tests/ --type py
```

Replace each with `turn_store` and update to use the new API if needed.

- [ ] **Task 16: Run full test suite + ruff**

```powershell
python -m pytest tests/unit/ -v
ruff check framework tests
```
Expected: all tests pass, no new lint errors.

Commit:
```bash
git add -A
git commit -m "refactor: complete checkpoint_store to turn_store rename across codebase"
```

---

## Phase D: Migrate `agent_session.py` to `AgentRuntimeServices`

**Goal:** Remove the last consumer of `AgentContext.extensions` and old `RuntimeAssembler.assemble()` pattern from `agent_session.py`. Build `AgentRuntimeServices` directly and set `agent_context.runtime = AgentRuntime(services=..., state=...)`.

### Task 17: Build `AgentRuntimeServices` in `AgentSession.process_message()`

**Files:**
- Modify: `framework/session/agent_session.py:375-402`

- [ ] **Step 1: Read the full context around line 363-410**

Read lines 363-410 to understand all the local variables in scope.

- [ ] **Step 2: Replace the AgentContext construction + RuntimeAssembler call**

Replace lines 363-402 with:

```python
# ---- typed AgentRuntime with ReActTurnState ----
from uuid import uuid4
from framework.runtime.enums import AgentKind, TurnPhase as RTurnPhase
from framework.runtime.models import TurnIdentity
from framework.agents.react.state import ReActTurnState
from framework.runtime.services import AgentRuntime, AgentRuntimeServices

turn_identity = TurnIdentity(
    agent_id=agent_name,
    session_id=session_id,
    turn_id=uuid4().hex,
    conversation_id=session_id,
)

react_state = ReActTurnState(
    identity=turn_identity,
    agent_kind=AgentKind.REACT,
    phase=RTurnPhase.CREATED,
)

services = AgentRuntimeServices(
    hooks=self._hook_runner,
    interceptors=self._interceptor_chain,
    governance=None,
    turn_store=self._turn_store,
    command_store=None,
    pending_input_queue=None,
    safety=RuntimeSafetyPolicy(),
    runtime_context_manager=self._runtime_context_manager,
)

agent_context = AgentContext(
    system_prompt=context_state.system_prompt,
    history=context_state.history,
    tool_manager=self._tool_manager,
    session_id=session_id,
    max_iterations=getattr(self._agent, "max_iterations", 10),
    temperature=getattr(message, "metadata", {}).get("temperature"),
    max_tokens=getattr(message, "metadata", {}).get("max_tokens"),
    runtime=AgentRuntime(services=services, state=react_state),
    identity=turn_identity,
)
```

- [ ] **Step 3: Handle `on_checkpoint`**

The `on_checkpoint` function was stored in `extensions[ExtensionKey.ON_CHECKPOINT]`. After migration, check if `on_checkpoint` is still needed. If the `RuntimeContextHook` was the only reader, it's now dead code. Verify:

```powershell
rg "ON_CHECKPOINT|on_checkpoint" framework/ --type py
```

If no readers remain, delete the `on_checkpoint` local function definition (lines 358-361).

- [ ] **Step 4: Remove `context_extensions` import**

Remove line 15: `from ..core.context_extensions import ExtensionKey`

- [ ] **Step 5: Remove `RuntimeAssembler` import condition**

Remove the conditional import block at lines 393-402.

- [ ] **Step 6: Update `__init__` to store `turn_store` not `checkpoint_store`**

(This was already done in Task 9 — verify consistency.)

- [ ] **Step 7: Run tests**

```powershell
python -m pytest tests/unit/session/ -v
python -m pytest tests/unit/ -x -q
```
Expected: all tests pass. If agent_session.py tests don't exist, at minimum verify import doesn't crash.

- [ ] **Step 8: Commit**

```bash
git add framework/session/agent_session.py
git commit -m "refactor: migrate agent_session.py to AgentRuntimeServices, remove extensions usage"
```

---

### Task 18: Delete `AgentContext.extensions` Field

**Files:**
- Modify: `framework/core/agent.py:38`

**Background:** With `agent_session.py` migrated in Task 17, only `pipeline.py` still writes to `extensions`. Check if pipeline's usage is still necessary.

- [ ] **Step 1: Check pipeline's `extensions` dict usage**

Read `framework/pipeline/pipeline.py` lines 519-558. The `extensions` dict is constructed and passed to `AgentContext()`. Is it read anywhere?

Search:
```powershell
rg "ctx\.extensions|context\.extensions" framework/ --type py
```

If zero hits: the dict is set but never read. Safe to remove.

- [ ] **Step 2: Remove `extensions` field from `AgentContext`**

In `framework/core/agent.py` line 38, delete:
```python
# DEPRECATED (Phase D): delete after agent_session + pipeline migration.
extensions: dict[str, Any] = field(default_factory=dict)
```

Also remove `Any` from imports if no longer needed (check: `Any` is used in `to_messages()`).

- [ ] **Step 3: Remove pipeline's `extensions` dict construction**

In `framework/pipeline/pipeline.py`, remove lines 519-521 and adjust the `AgentContext` construction to not pass `extensions=`.

- [ ] **Step 4: Run full test suite**

```powershell
python -m pytest tests/unit/ -v
```
Expected: all pass. No `AttributeError` for `extensions`.

- [ ] **Step 5: Commit**

```bash
git add framework/core/agent.py framework/pipeline/pipeline.py
git commit -m "refactor: delete AgentContext.extensions field"
```

---

### Task 19: Remove `ctx_ext()` Compatibility Function (if any)

- [ ] **Step 1: Search for `ctx_ext`**

```powershell
rg "ctx_ext" framework/ tests/ --type py
```

Expected: zero hits (already deleted in commit `855fa97`).

If any hits: remove the function and update callers.

- [ ] **Step 2: Mark Phase D complete in progress dashboard**

---

## Phase E: Memory Checkpoint Cleanup (P5) — EXPLORATORY

**Design doc reference:** Phase 5, lines 1190-1195.

**Current state:** `agent.py:_save_checkpoint` calls `store.save(checkpoint_id, data)` on a `TurnStateStore`-typed object. But `TurnStateStore` does not have `.save(id, dict)` — it has `save_turn(snapshot)`. This is either:
a) A bug where the wrong store type is used, or
b) The actual runtime object is a hybrid that implements both APIs.

**Investigation needed before planning:**
- What concrete type is passed as `turn_store` in bot project and pipeline?
- Is there an old `MemoryCheckpointStore` that happens to be passed as `turn_store`?
- Can `_save_checkpoint` be replaced with `TurnSnapshot.message_delta` recovery?

**This phase is deferred until after Phases A-D are stable. It requires a separate mini-plan.**

---

## Phase F: Bot Project Rewire (P6) — SEPARATE PLAN

**Design doc reference:** Phase 6, lines 1197-1210.

**Status:** 🚫 Not started. Requires Phase E completion first. Will be a separate plan document.

---

## Phase G: Final Cleanup (P7) — SEPARATE PLAN

**Design doc reference:** Phase 7, lines 1212-1218.

**Checklist for when P1-P6 are complete:**
- [ ] Delete all temporary migration adapters
- [ ] Delete `_prebuilt_runtime` parameter from pipeline
- [ ] Delete `checkpoint_store` parameter from pipeline init
- [ ] Legacy symbol scan (full): `suspend_strategy`, `ApprovalStateStore`, `deny_as_cancel`, `_current_resume`, `TurnResumeState`, `ctx.metadata`, `ctx.extensions`
- [ ] Update AGENTS.md files: `checkpoint_store` → `turn_store`, `metadata` → `typed state`
- [ ] Run `ruff check framework tests`, `mypy framework`

---

## Self-Review Notes

**1. Spec coverage:** This plan covers all remaining items from the progress document (A1-A8, B1-B3, C1-C4), the design doc's P5 (Memory Checkpoint), P6 (Bot Rewire), and P7 (Final Cleanup). P6 and P7 are deferred to separate plans.

**2. Placeholder scan:** Phase E is marked EXPLORATORY — the concrete task list depends on investigation. This is intentional: the `_save_checkpoint` API mismatch requires runtime analysis before planning. All other phases have concrete steps.

**3. Type consistency:**
- `turn_store` consistently refers to `TurnStateStore` throughout Phase C
- `AgentRuntimeServices` matches the exact dataclass definition from `framework/runtime/services.py`
- `ReActTurnState` matches the exact dataclass from `framework/agents/react/state.py`

**4. Known gap:** The `agent.py:_save_checkpoint` calling `store.save(id, dict)` on a `TurnStateStore` is a type violation. This is the subject of Phase E exploration and is intentionally not fixed in Phase A to avoid breaking existing behavior without understanding the full picture.
