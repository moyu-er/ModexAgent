# Subagent Architecture Refactoring Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove legacy SubagentManager, unify peer→subagent semantics, add SubagentService with sync/async invocation, session-level concurrency with safe cleanup, and dynamic subagent creation.

**Architecture:** Delete `SubagentManager` + `helper-sync` entirely. Extract `current_conversation_id` ContextVar into standalone `framework/multi_agent/context.py`. Add `SubagentService` wrapping `AgentPool` for resident+dynamic subagents. Enhance `AgentPool` with `_session_meta`, `_sync_futures`, and lock-guarded TTL eviction. Rename all "peer" → "subagent" throughout framework and bot_project. Bot exposes async-only tools: `send_message_async` (with `message_type="task_request"` for delegation) + `create_subagent` for dynamic creation.

**Tech Stack:** Python 3.12+, asyncio, LiteLLM

**Calibration note:** Execute Phase 0 before Phase 1. Phase 0 reflects current
code inspection: `AgentPool` already has the async `task_request` path, but the
tool payload contract and pool lock ownership need to be made explicit before
broad deletion/renaming work starts.

**Phase dependency:** Phase 0 → Phase 1 → Phase 2 → Phase 3 → Phase 4 → Phase 5 → Phase 6. Phases must execute sequentially; tasks within a phase are ordered by dependency.

---

## Phase 0: Implementation Calibration

These tasks align the plan with the current implementation before deleting or
renaming broad surfaces. The refactor must preserve and extend the existing
`AgentPool` `task_request` path instead of adding another queue.

### Task 0.1: Verify task_request payload contract

**Files:**
- Test: `tests/unit/multi_agent/test_tools_enhanced_validation.py`
- Test: `tests/unit/multi_agent/test_pool.py`
- Modify: `framework/multi_agent/tools.py`
- Modify: `framework/multi_agent/pool.py`

- [ ] **Step 1: Add a failing test for SendMessageAsyncTool task_request payload**

Add a test that calls `send_message_async` with `message_type="task_request"`
and asserts the sent envelope payload contains `task_prompt` equal to the input
content.

Expected before implementation: FAIL because payload only contains `content`.

- [ ] **Step 2: Add a defensive AgentPool fallback test**

Add a test for `_dispatch_task_request()` with a legacy envelope containing
`payload={"content": "legacy task"}` and no `task_prompt`. Assert the pipeline
receives `InputMessage.content == "legacy task"`.

Expected before implementation: FAIL because `_dispatch_task_request()` reads an
empty task prompt.

- [ ] **Step 3: Update SendMessageAsyncTool**

When `message_type == "task_request"`, build payload with both fields:

```python
payload = {"content": content, "message_type": message_type}
if message_type == "task_request":
    payload["task_prompt"] = content
```

- [ ] **Step 4: Update AgentPool fallback**

In `_dispatch_task_request()`, read:

```python
task_prompt = envelope.payload.get("task_prompt") or envelope.payload.get("content", "")
```

- [ ] **Step 5: Run focused tests**

```bash
pytest tests/unit/multi_agent/test_tools_enhanced_validation.py tests/unit/multi_agent/test_pool.py -v
```

- [ ] **Step 6: Commit**

```bash
git add framework/multi_agent/tools.py framework/multi_agent/pool.py tests/unit/multi_agent/test_tools_enhanced_validation.py tests/unit/multi_agent/test_pool.py
git commit -m "fix: align task_request payload contract"
```

### Task 0.2: Document and test pool session lock ownership

**Files:**
- Test: `tests/unit/multi_agent/test_pool.py`
- Modify: `framework/multi_agent/pool.py`

- [ ] **Step 1: Add a test proving same-session pool dispatch is serialized**

Use two concurrent `_dispatch_agent_message()` calls with the same session id and
a pipeline stub that records overlap. Assert no overlap occurs.

- [ ] **Step 2: Add comments around `get_lock()`**

Document that `AgentPool.get_lock(session_id)` is the lifecycle/eviction lock for
pool-managed sessions. `AgentPipeline` keeps its own internal lock for direct
pipeline callers.

- [ ] **Step 3: Run focused tests**

```bash
pytest tests/unit/multi_agent/test_pool.py -v
```

- [ ] **Step 4: Commit**

```bash
git add framework/multi_agent/pool.py tests/unit/multi_agent/test_pool.py
git commit -m "test: document pool session lock ownership"
```

### Task 0.3: Define subagent lifecycle families and config boundaries

**Files:**
- Modify: `docs/superpowers/specs/2026-05-21-subagent-refactoring-design.md`
- Modify: `docs/superpowers/plans/2026-05-21-subagent-refactoring-plan.md`
- Later implementation targets: `framework/multi_agent/`, `framework/ioc/configs/`, `examples/bot_project/bot_config.yml`

- [ ] **Step 1: Record the three lifecycle families**

Document these as separate concepts before implementation begins:

- Resident subagent: configured identity, stable address, optional eager or lazy activation.
- Template subagent: preset definition only; no bus identity, memory, consumer, or session until instantiated.
- Dynamic subagent: task-scoped runtime instance, optional, config-gated, with TTL and isolated memory.

- [ ] **Step 2: Make dynamic creation optional by contract**

Add plan/spec requirements that `CreateSubagentTool` is not registered unless
dynamic subagents are enabled. Disabling dynamic creation must not affect
resident subagent messaging or task dispatch.

- [ ] **Step 3: Prefer template instantiation over arbitrary dynamic prompts**

Specify that common workflows such as code review should normally instantiate a
named template with a preconfigured system prompt and tool bundle. Ad-hoc prompt
creation should require a separate explicit policy flag.

- [ ] **Step 4: Define resource-saving behavior for resident subagents**

Specify a lazy-resident mode where descriptor/config registration does not imply
an active consumer loop, pipeline, or memory session until the subagent receives
work.

- [ ] **Step 5: Commit**

```bash
git add docs/superpowers/specs/2026-05-21-subagent-refactoring-design.md docs/superpowers/plans/2026-05-21-subagent-refactoring-plan.md
git commit -m "docs: separate resident template dynamic subagents"
```

## Phase 1: Framework Deletions

### Task 1.1: Extract current_conversation_id ContextVar before deleting SubagentManager

**Background:** `current_conversation_id` ContextVar is defined in `subagent_manager.py:27-28` but used by `pipeline.py:666,734`, `agent_session.py:407,419`, and `tools.py:158,291`. We must extract it to a standalone module before deleting `subagent_manager.py`.

**Files:**
- Create: `framework/multi_agent/context.py`
- Modify: `framework/pipeline/pipeline.py:666`
- Modify: `framework/session/agent_session.py:407`
- Modify: `framework/multi_agent/tools.py:158,291`
- Modify: `framework/multi_agent/__init__.py`

- [ ] **Step 1: Create context.py with current_conversation_id**

```python
# framework/multi_agent/context.py
"""Shared context variables for multi-agent coordination.

Defined in a standalone module (not __init__.py) to avoid circular imports
between pipeline, session, and multi_agent packages.
"""

import contextvars

current_conversation_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "current_conversation_id", default=None
)
```

- [ ] **Step 2: Update pipeline.py import**

In `framework/pipeline/pipeline.py:666`, replace:
```python
from ..multi_agent.subagent_manager import current_conversation_id
```
with:
```python
from ..multi_agent.context import current_conversation_id
```

Command: verify with `grep`:
```bash
grep -n "subagent_manager" framework/pipeline/pipeline.py
```
Expected: only one match (line 666), already fixed.

- [ ] **Step 3: Update agent_session.py import**

In `framework/session/agent_session.py:407`, replace:
```python
from ..multi_agent.subagent_manager import current_conversation_id
```
with:
```python
from ..multi_agent.context import current_conversation_id
```

Command:
```bash
grep -n "subagent_manager" framework/session/agent_session.py
```
Expected: only one match (line 407), already fixed; plus `_subagent_manager` instance var (line 128) which is deleted separately in Task 1.3.

- [ ] **Step 4: Update tools.py imports**

In `framework/multi_agent/tools.py:158` and `:291`, replace:
```python
from framework.multi_agent.subagent_manager import current_conversation_id
```
with:
```python
from framework.multi_agent.context import current_conversation_id
```

Command:
```bash
grep -n "subagent_manager" framework/multi_agent/tools.py
```
Expected: no matches (both imports updated).

- [ ] **Step 5: Add context.py to multi_agent __init__.py exports**

In `framework/multi_agent/__init__.py`, add after existing imports:
```python
from framework.multi_agent.context import current_conversation_id
```

And add `"current_conversation_id"` to the `__all__` list.

- [ ] **Step 6: Run tests to verify import chain**

```bash
python -c "from framework.multi_agent.context import current_conversation_id; print('OK')"
python -c "from framework.pipeline.pipeline import AgentPipeline; print('OK')"
python -c "from framework.session.agent_session import AgentSession; print('OK')"
```

- [ ] **Step 7: Commit**

```bash
git add framework/multi_agent/context.py framework/pipeline/pipeline.py framework/session/agent_session.py framework/multi_agent/tools.py framework/multi_agent/__init__.py
git commit -m "refactor: extract current_conversation_id ContextVar to multi_agent/context.py"
```

---

### Task 1.2: Remove subagent_manager parameter from AgentPipeline

**Files:**
- Modify: `framework/pipeline/pipeline.py:156,207`

- [ ] **Step 1: Remove subagent_manager from AgentPipeline.__init__ signature**

In `framework/pipeline/pipeline.py`, remove parameter at line 156:
```python
# DELETE this line:
        subagent_manager: SubagentManager | None = None,
```

And remove the corresponding assignment at line 207:
```python
# DELETE this line:
        self.subagent_manager = subagent_manager
```

Also remove `SubagentManager` from the type imports at the top of the file (line 45):
```python
# In the TYPE_CHECKING block, DELETE:
    SubagentManager,
```

- [ ] **Step 2: Verify no remaining references**

```bash
grep -n "subagent_manager\|SubagentManager" framework/pipeline/pipeline.py
```
Expected: only the `current_conversation_id` import from `context` (already fixed).

- [ ] **Step 3: Run pipeline unit tests**

```bash
pytest tests/unit/pipeline/ -v --timeout=60
```

- [ ] **Step 4: Commit**

```bash
git add framework/pipeline/pipeline.py
git commit -m "refactor: remove subagent_manager parameter from AgentPipeline"
```

---

### Task 1.3: Remove subagent_manager parameter from AgentSession

**Files:**
- Modify: `framework/session/agent_session.py:80,103,128`

- [ ] **Step 1: Read the current AgentSession.__init__ signature**

```bash
grep -n "subagent_manager" framework/session/agent_session.py
```

Expected output: 3 lines — parameter declaration (~line 80), docstring (~line 103), instance assignment (~line 128).

- [ ] **Step 2: Remove all three references**

```python
# DELETE from __init__ parameter list:
        subagent_manager: Any | None = None,

# DELETE from docstring:
            subagent_manager: 可选的 SubagentManager，用于 turn 结束时取消子 Agent

# DELETE from __init__ body:
        self._subagent_manager = subagent_manager
```

- [ ] **Step 3: Verify no remaining references**

```bash
grep -n "subagent_manager\|SubagentManager\|_subagent_manager" framework/session/agent_session.py
```
Expected: only the `current_conversation_id` import from `context` (already fixed in Task 1.1).

- [ ] **Step 4: Run session unit tests**

```bash
pytest tests/unit/session/ -v --timeout=60
```

- [ ] **Step 5: Commit**

```bash
git add framework/session/agent_session.py
git commit -m "refactor: remove subagent_manager parameter from AgentSession"
```

---

### Task 1.4: Delete SubagentManager module and TaskCoordinationConfig

**Files:**
- Delete: `framework/multi_agent/subagent_manager.py`
- Modify: `framework/multi_agent/coordinator.py`

- [ ] **Step 1: Remove TaskCoordinationConfig from coordinator.py**

Read `framework/multi_agent/coordinator.py:32-39`. Delete the `TaskCoordinationConfig` dataclass:
```python
# DELETE:
@dataclass
class TaskCoordinationConfig:
    """任务协调配置。"""
    enable_for_subagent: bool = True
    default_timeout_seconds: float = 180.0
    supervision_check_interval: float = 5.0
    supervision_emit_heartbeat: bool = True
```

Also remove the import that only SubagentManager used — check `from framework.control.task_supervision import TaskSupervisor, TimeoutSupervisionPolicy` — keep it if used elsewhere.

- [ ] **Step 2: Delete subagent_manager.py**

```bash
rm framework/multi_agent/subagent_manager.py
```

- [ ] **Step 3: Clean up multi_agent/__init__.py**

Remove lines 36-40 that import from `subagent_manager`:
```python
# DELETE:
from framework.multi_agent.subagent_manager import (
    SubagentManager,
    TaskCoordinationConfig,
    current_conversation_id,
)
```

And remove `"SubagentManager"`, `"TaskCoordinationConfig"` from `__all__`.

- [ ] **Step 4: Verify no dangling imports**

```bash
grep -rn "from.*subagent_manager import\|import.*subagent_manager" framework/ --include="*.py"
```
Expected: no output (every import updated).

- [ ] **Step 5: Run full framework test suite**

```bash
pytest tests/unit/ -v --ignore=tests/unit/multi_agent/test_subagent_manager.py --timeout=120
```

- [ ] **Step 6: Commit**

```bash
git add -u framework/
git commit -m "refactor: delete SubagentManager, TaskCoordinationConfig"
```

---

### Task 1.5: Remove MemoryAgentRole.PEER enum value

**Files:**
- Modify: `framework/memory/core/scope.py:94-95`
- Modify: `framework/memory/recorder.py:99`
- Modify: `framework/ioc/factories/descriptors.py:163`

- [ ] **Step 1: Remove PEER from MemoryAgentRole enum**

In `framework/memory/core/scope.py`, change:
```python
class MemoryAgentRole(StrEnum):
    MAIN = "main"
    PEER = "peer"      # DELETE this line
    SUBAGENT = "subagent"
```

- [ ] **Step 2: Fix infer_agent_role function**

In `framework/memory/core/scope.py:94-95`, change:
```python
# DELETE:
    if MemoryAgentRole.PEER.value in normalized:
        return MemoryAgentRole.PEER
```

- [ ] **Step 3: Fix recorder.py reference**

In `framework/memory/recorder.py:99`, change the PEER value check:
```python
# Old:
            if v == MemoryAgentRole.PEER.value or v.startswith(MemoryAgentRole.PEER.value):
# New:
            if v == MemoryAgentRole.SUBAGENT.value or v.startswith(MemoryAgentRole.SUBAGENT.value):
```

- [ ] **Step 4: Fix descriptors.py reference**

In `framework/ioc/factories/descriptors.py:163`, change:
```python
# Old:
        MemoryAgentRole.PEER, system_prompt,
# New:
        MemoryAgentRole.SUBAGENT, system_prompt,
```

- [ ] **Step 5: Verify no remaining PEER references in framework**

```bash
grep -rn "MemoryAgentRole\.PEER" framework/ --include="*.py"
```
Expected: no output.

- [ ] **Step 6: Commit**

```bash
git add framework/memory/core/scope.py framework/memory/recorder.py framework/ioc/factories/descriptors.py
git commit -m "refactor: remove MemoryAgentRole.PEER enum value"
```

---

## Phase 2: Framework Renaming (peer → subagent)

### Task 2.1: Rename PeerAutoSendHook → SubagentAutoSendHook

**Files:**
- Rename: `framework/hook/builtin/peer_auto_send.py`
- Modify: `framework/hook/builtin/__init__.py`

- [ ] **Step 1: Create the renamed file**

```bash
cp framework/hook/builtin/peer_auto_send.py framework/hook/builtin/subagent_auto_send.py
```

- [ ] **Step 2: Update class name and all references in subagent_auto_send.py**

In `framework/hook/builtin/subagent_auto_send.py`:
- Line 1: docstring `"""PeerAutoSendHook` → `"""SubagentAutoSendHook`
- Line 20: `class PeerAutoSendHook:` → `class SubagentAutoSendHook:`
- Line 69: log message `"PeerAutoSendHook: skipped"` → `"SubagentAutoSendHook: skipped"`
- Line 75: log message `"PeerAutoSendHook: auto-forwarding"` → `"SubagentAutoSendHook: auto-forwarding"`

- [ ] **Step 3: Update __init__.py exports**

In `framework/hook/builtin/__init__.py`:
- Line 7: comment update
- Line 19: import `from framework.hook.builtin.subagent_auto_send import SubagentAutoSendHook`
- Line 29: `"PeerAutoSendHook"` → `"SubagentAutoSendHook"`

- [ ] **Step 4: Delete old file**

```bash
rm framework/hook/builtin/peer_auto_send.py
```

- [ ] **Step 5: Verify no remaining PeerAutoSendHook in framework**

```bash
grep -rn "PeerAutoSendHook" framework/ --include="*.py"
```
Expected: no output.

- [ ] **Step 6: Update imports in all non-framework files (bot, tests)**

Files to update (from the grep scan):
- `examples/bot_project/bot/service/builders.py:352` — import
- `examples/bot_project/bot/service/builders.py:427` — instantiation
- `examples/bot_project/tests/test_agent_communication.py:26` — import
- `examples/bot_project/tests/test_agent_communication.py:315,319,320,329,363` — usage
- `tests/unit/multi_agent/test_runtime_context_hook_integration.py:38` — import
- `tests/unit/multi_agent/test_runtime_context_hook_integration.py:161,166,186,190,255,264,292,297,305,327` — usage
- `tests/unit/multi_agent/test_peer_auto_send_hook.py` — entire file

Run bulk replacement:
```bash
# Replace class name
find tests/ examples/ -name "*.py" -exec sed -i 's/PeerAutoSendHook/SubagentAutoSendHook/g' {} +
```

- [ ] **Step 7: Rename test file**

```bash
mv tests/unit/multi_agent/test_peer_auto_send_hook.py tests/unit/multi_agent/test_subagent_auto_send_hook.py
```

- [ ] **Step 8: Run hook tests**

```bash
pytest tests/unit/multi_agent/test_subagent_auto_send_hook.py tests/unit/multi_agent/test_runtime_context_hook_integration.py -v
```

- [ ] **Step 9: Commit**

```bash
git add framework/hook/builtin/
git add tests/unit/multi_agent/test_subagent_auto_send_hook.py
git add -u
git commit -m "refactor: rename PeerAutoSendHook → SubagentAutoSendHook"
```

---

### Task 2.2: Rename PeerAgentValidator → SubagentAgentValidator

**Files:**
- Modify: `framework/multi_agent/peer_validator.py`
- Modify: `framework/multi_agent/__init__.py`

- [ ] **Step 1: Update class name**

In `framework/multi_agent/peer_validator.py:13`:
```python
# Old:
class PeerAgentValidator:
# New:
class SubagentAgentValidator:
```

- [ ] **Step 2: Update __init__.py exports**

In `framework/multi_agent/__init__.py:31`, change:
```python
from framework.multi_agent.peer_validator import PeerAgentValidator
```
to:
```python
from framework.multi_agent.peer_validator import SubagentAgentValidator
```

And update `__all__` entry.

- [ ] **Step 3: Update bot import**

In `examples/bot_project/bot/service/builders.py:384-385`:
```python
# Old:
            from framework.multi_agent.peer_validator import PeerAgentValidator
            PeerAgentValidator.validate(descriptor, parent_name)
# New:
            from framework.multi_agent.peer_validator import SubagentAgentValidator
            SubagentAgentValidator.validate(descriptor, parent_name)
```

- [ ] **Step 4: Verify no remaining PeerAgentValidator references**

```bash
grep -rn "PeerAgentValidator" framework/ examples/ tests/ --include="*.py"
```
Expected: no output.

- [ ] **Step 5: Commit**

```bash
git add framework/multi_agent/peer_validator.py framework/multi_agent/__init__.py examples/bot_project/bot/service/builders.py
git commit -m "refactor: rename PeerAgentValidator → SubagentAgentValidator"
```

---

### Task 2.3: Rename governance and compression factory functions

**Files:**
- Modify: `framework/ioc/factories/governance.py:64`
- Modify: `framework/ioc/factories/compression.py:12`
- Modify: `framework/ioc/factories/__init__.py`
- Modify: `framework/ioc/factories/descriptors.py:82,84`
- Modify: `examples/bot_project/bot/service/builders.py:13,263,270,288,300,421`
- Modify: `examples/bot_project/bot/service/core.py:54,597`
- Modify: `tests/unit/ioc/test_governance_factory.py`
- Modify: `tests/unit/ioc/test_compression_factory.py`

- [ ] **Step 1: Rename in governance.py**

In `framework/ioc/factories/governance.py:64`:
```python
# Old:
def create_peer_governance(
# New:
def create_subagent_governance(
```

Update the function's docstring to replace "peer" with "subagent".

- [ ] **Step 2: Rename in compression.py**

In `framework/ioc/factories/compression.py:12`:
```python
# Old:
def create_peer_compression_coordinator(
# New:
def create_subagent_compression_coordinator(
```

- [ ] **Step 3: Update __init__.py exports**

In `framework/ioc/factories/__init__.py`, change lines 9,14,33,34:
```python
from framework.ioc.factories.compression import create_subagent_compression_coordinator
from framework.ioc.factories.governance import create_governance, create_subagent_governance
```
And update `__all__` entries.

- [ ] **Step 4: Update descriptors.py**

In `framework/ioc/factories/descriptors.py:82,84`:
```python
# Old:
    from framework.ioc.factories.compression import create_peer_compression_coordinator
    coordinator = create_peer_compression_coordinator(cfg)
# New:
    from framework.ioc.factories.compression import create_subagent_compression_coordinator
    coordinator = create_subagent_compression_coordinator(cfg)
```

- [ ] **Step 5: Update bot builders.py**

In `examples/bot_project/bot/service/builders.py`:
- Line 13: `from framework.ioc.factories.governance import create_peer_governance` → `create_subagent_governance`
- Line 263: `from framework.ioc.factories.compression import create_peer_compression_coordinator` → `create_subagent_compression_coordinator`
- Line 270: `coordinator = create_peer_compression_coordinator(...)` → `create_subagent_compression_coordinator(...)`
- Line 288: same import fix
- Line 300: same call fix
- Line 421: `create_peer_governance(` → `create_subagent_governance(`

- [ ] **Step 6: Update bot core.py**

In `examples/bot_project/bot/service/core.py`:
- Line 54: import fix
- Line 597: `create_peer_governance(` → `create_subagent_governance(`

- [ ] **Step 7: Update test files**

In `tests/unit/ioc/test_governance_factory.py`:
```bash
sed -i 's/create_peer_governance/create_subagent_governance/g' tests/unit/ioc/test_governance_factory.py
```

In `tests/unit/ioc/test_compression_factory.py`:
```bash
sed -i 's/create_peer_compression_coordinator/create_subagent_compression_coordinator/g' tests/unit/ioc/test_compression_factory.py
```

- [ ] **Step 8: Verify no remaining old names**

```bash
grep -rn "create_peer_governance\|create_peer_compression_coordinator" framework/ examples/ tests/ --include="*.py"
```
Expected: no output.

- [ ] **Step 9: Run IOC factory tests**

```bash
pytest tests/unit/ioc/ -v
```

- [ ] **Step 10: Commit**

```bash
git add -u framework/ examples/ tests/
git commit -m "refactor: rename create_peer_* → create_subagent_* factory functions"
```

---

## Phase 3: Framework Additions

### Task 3.1: Add SessionRetentionPolicy to AgentPool

**Files:**
- Modify: `framework/multi_agent/pool.py`

- [ ] **Step 1: Add SessionMeta and SessionRetentionPolicy dataclasses**

At the top of `framework/multi_agent/pool.py`, after existing imports, add:

```python
from dataclasses import dataclass


@dataclass
class SessionMeta:
    """Per-session metadata for lifecycle tracking."""
    agent_name: str
    created_at: float
    last_active: float
    is_dynamic: bool = False


@dataclass
class SessionRetentionPolicy:
    """Controls session cleanup for subagent task sessions."""
    max_sessions_per_subagent: int = 50
    max_sessions_global: int = 200
    ttl_seconds: float = 86400.0  # 24h
    cleanup_interval_seconds: float = 1800.0  # 30min
```

- [ ] **Step 2: Add new fields to AgentPool.__init__**

In `AgentPool.__init__`, add parameters:
```python
        retention: SessionRetentionPolicy | None = None,
```

And in the body:
```python
        self._retention = retention or SessionRetentionPolicy()
        self._session_meta: dict[str, SessionMeta] = {}
        self._session_last_activity: dict[str, float] = {}
        self._sync_futures: dict[str, asyncio.Future] = {}
        self._cleanup_task: asyncio.Task | None = None
```

- [ ] **Step 3: Start cleanup task in __init__**

After the existing inbox poll task creation, add:
```python
        if self._retention.cleanup_interval_seconds > 0:
            self._cleanup_task = asyncio.create_task(self._cleanup_stale_sessions())
```

- [ ] **Step 4: Run type check**

```bash
python -c "from framework.multi_agent.pool import AgentPool, SessionMeta, SessionRetentionPolicy; print('OK')"
```

- [ ] **Step 5: Commit**

```bash
git add framework/multi_agent/pool.py
git commit -m "feat: add SessionRetentionPolicy and SessionMeta to AgentPool"
```

---

### Task 3.2: Implement session tracking helpers in AgentPool

**Files:**
- Modify: `framework/multi_agent/pool.py`

- [ ] **Step 1: Add _track_session method**

```python
    def _track_session(
        self, session_id: str, agent_name: str, is_dynamic: bool = False
    ) -> None:
        """Register a new session in metadata. Call INSIDE lock-protected section."""
        now = time.time()
        self._session_meta[session_id] = SessionMeta(
            agent_name=agent_name,
            created_at=now,
            last_active=now,
            is_dynamic=is_dynamic,
        )
        self._session_last_activity[session_id] = now
```

- [ ] **Step 2: Add _touch_session method**

```python
    def _touch_session(self, session_id: str) -> None:
        """Update last_active timestamp. Call INSIDE lock-protected section."""
        meta = self._session_meta.get(session_id)
        if meta is not None:
            now = time.time()
            meta.last_active = now
            self._session_last_activity[session_id] = now
```

- [ ] **Step 3: Integrate _touch_session into _dispatch_task_request**

Add `self._touch_session(session_id)` right after acquiring the per-session lock in `_dispatch_task_request`:

```python
    async def _dispatch_task_request(self, instance, descriptor, envelope):
        ...
        lock = self.get_lock(session_id)
        async with lock:
            self._touch_session(session_id)  # ADD THIS LINE
            result = await instance.pipeline.process_message(...)
```

Similarly in `_dispatch_agent_message`.

- [ ] **Step 4: Run existing pool tests**

```bash
pytest tests/unit/multi_agent/test_pool.py -v --timeout=60
```

- [ ] **Step 5: Commit**

```bash
git add framework/multi_agent/pool.py
git commit -m "feat: add session tracking (_track_session, _touch_session) to AgentPool"
```

---

### Task 3.3: Implement concurrency-safe session cleanup in AgentPool

**Files:**
- Modify: `framework/multi_agent/pool.py`

- [ ] **Step 1: Add _cleanup_stale_sessions background loop**

```python
    async def _cleanup_stale_sessions(self) -> None:
        """Background task: periodic TTL + LRU eviction of dynamic sessions."""
        while True:
            try:
                await asyncio.sleep(self._retention.cleanup_interval_seconds)
            except asyncio.CancelledError:
                break
            for sid in list(self._session_meta.keys()):
                await self._try_evict_if_stale(sid)
```

- [ ] **Step 2: Add _try_evict_if_stale with lock-guarded TOCTOU elimination**

```python
    async def _try_evict_if_stale(self, session_id: str) -> bool:
        """Evict a session only if stale AND no active task is using it.

        Acquires the session lock before making eviction decisions,
        eliminating the TOCTOU race window.
        """
        lock = self._session_locks.get(session_id)
        if lock is None:
            self._session_meta.pop(session_id, None)
            self._session_last_activity.pop(session_id, None)
            return False

        # Try to acquire the lock. If held → session is active → skip.
        try:
            await asyncio.wait_for(lock.acquire(), timeout=3.0)
        except (TimeoutError, asyncio.CancelledError):
            return False

        try:
            meta = self._session_meta.get(session_id)
            if meta is None:
                self._session_locks.pop(session_id, None)
                return False
            if not meta.is_dynamic:
                return False
            now = time.time()
            if now - meta.last_active < self._retention.ttl_seconds:
                return False  # Was touched since last scan

            # Evict
            instance = self._agents.get(meta.agent_name)
            if instance is not None and instance.context_manager is not None:
                try:
                    await instance.context_manager.clear(session_id)
                except Exception:
                    logger.exception(
                        "Failed to clear context for evicted session %s", session_id
                    )
            self._session_locks.pop(session_id, None)
            self._session_meta.pop(session_id, None)
            self._session_last_activity.pop(session_id, None)
            logger.info("Session evicted (stale): %s", session_id)
            return True
        finally:
            lock.release()
```

- [ ] **Step 3: Add _do_evict_sync for shutdown_all**

```python
    async def _do_evict_sync(self, session_id: str) -> None:
        """Evict a session during shutdown (no concurrency concerns)."""
        meta = self._session_meta.pop(session_id, None)
        if meta is not None:
            instance = self._agents.get(meta.agent_name)
            if instance is not None and instance.context_manager is not None:
                try:
                    await instance.context_manager.clear(session_id)
                except Exception:
                    logger.exception(
                        "Failed to clear context during shutdown eviction for %s",
                        session_id,
                    )
        self._session_locks.pop(session_id, None)
        self._session_last_activity.pop(session_id, None)
```

- [ ] **Step 4: Add cleanup task cancellation to shutdown_all**

At the top of `shutdown_all`, before cancelling consumers:
```python
        if self._cleanup_task is not None:
            self._cleanup_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._cleanup_task
            self._cleanup_task = None
```

At the bottom of `shutdown_all`, after draining all tasks, add:
```python
        for sid in list(self._session_locks.keys()):
            await self._do_evict_sync(sid)
```

- [ ] **Step 5: Run type check**

```bash
python -c "from framework.multi_agent.pool import AgentPool; print('OK')"
```

- [ ] **Step 6: Commit**

```bash
git add framework/multi_agent/pool.py
git commit -m "feat: add concurrency-safe session cleanup (TOCTOU-free) to AgentPool"
```

---

### Task 3.4: Add sync-future result channel to AgentPool._send_subagent_result

**Files:**
- Modify: `framework/multi_agent/pool.py`

- [ ] **Step 1: Modify _send_subagent_result to notify sync waiters**

Current code in `_send_subagent_result` (pool.py ~509-556) sends result via inbox/broker. Add Future notification at the end:

```python
    async def _send_subagent_result(
        self, descriptor, envelope, conversation_id, result
    ) -> None:
        # ... existing inbox/broker delivery code ...
        
        # NEW: Notify synchronous waiters via Future channel
        correlation_id = envelope.correlation_id
        future = self._sync_futures.pop(correlation_id, None)
        if future is not None and not future.done():
            future.set_result(result)
```

- [ ] **Step 2: Run existing pool tests**

```bash
pytest tests/unit/multi_agent/ -v --timeout=120 -k "not test_subagent_manager"
```

- [ ] **Step 3: Commit**

```bash
git add framework/multi_agent/pool.py
git commit -m "feat: add sync-future result channel to _send_subagent_result"
```

---

### Task 3.5: Add DelegateTaskTool to framework

**Files:**
- Modify: `framework/multi_agent/tools.py`

- [ ] **Step 1: Add DelegateTaskTool class**

Add after `SendMessageAsyncTool` in `framework/multi_agent/tools.py`:

```python
class DelegateTaskTool(Tool):
    """Synchronous task delegation via task_request, blocks until result.

    Used programmatically (not exposed to LLM in bot). Sends a task_request
    envelope to the target agent, registers a Future, and waits for the result.
    """

    def __init__(
        self,
        broker: MessageBroker,
        pool: Any,  # AgentPool
        self_address: AgentAddress,
        allowed_targets: list[str] | None = None,
        session_strategy: SessionIdStrategy | None = None,
    ):
        self._broker = broker
        self._pool = pool
        self._self_address = self_address
        self._allowed_targets = (
            set(allowed_targets) if allowed_targets else None
        )
        self._session_strategy = session_strategy or DefaultSessionIdStrategy()
        super().__init__(
            name="delegate_task",
            description=(
                "Delegate a task to another agent and wait for the result. "
                "This is a synchronous call — it blocks until the target agent completes "
                "the task and returns a result."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "target_agent": {
                        "type": "string",
                        "description": "Name of the target agent",
                    },
                    "task_prompt": {
                        "type": "string",
                        "description": "The task description",
                    },
                    "timeout": {
                        "type": "number",
                        "description": "Timeout in seconds (default 120)",
                        "default": 120,
                    },
                },
                "required": ["target_agent", "task_prompt"],
            },
            config=ToolConfig(),
        )

    async def execute(self, **kwargs) -> str:
        target_agent = kwargs.get("target_agent", "")
        task_prompt = kwargs.get("task_prompt", "")
        timeout = float(kwargs.get("timeout", 120))
        conversation_id = kwargs.get("conversation_id", "default")

        if not self._is_target_allowed(target_agent):
            return f"Error: delegate_task to {target_agent} is not allowed."

        correlation_id = uuid.uuid4().hex
        session_id = self._session_strategy.target_session(
            conversation_id, target_agent, self._self_address.name,
        )
        task_session_id = f"{conversation_id}:{target_agent}:{uuid.uuid4().hex[:8]}"

        envelope = AgentMessageEnvelope(
            payload={"task_prompt": task_prompt},
            source=self._self_address,
            target=AgentAddress(kind="agent", name=target_agent),
            message_type="task_request",
            conversation_id=conversation_id,
            agent_session_id=task_session_id,
            correlation_id=correlation_id,
        )

        future: asyncio.Future[AgentResult] = asyncio.get_running_loop().create_future()
        self._pool._sync_futures[correlation_id] = future

        try:
            await self._broker.send_to(
                AgentAddress(kind="agent", name=target_agent),
                envelope.to_broker_message(),
            )

            result = await asyncio.wait_for(future, timeout=timeout)
            return result.content or ""
        except TimeoutError:
            return "Error: Task timed out."
        finally:
            self._pool._sync_futures.pop(correlation_id, None)

    def _is_target_allowed(self, target: str) -> bool:
        if self._allowed_targets is None:
            return True
        return target in self._allowed_targets
```

- [ ] **Step 2: Ensure imports are correct**

Top of file needs: `import uuid`, `from framework.core.emitter import AgentResult`.

- [ ] **Step 3: Add to __init__.py exports**

In `framework/multi_agent/__init__.py`:
```python
from framework.multi_agent.tools import DelegateTaskTool
```
And add to `__all__`.

- [ ] **Step 4: Run type check**

```bash
python -c "from framework.multi_agent.tools import DelegateTaskTool; print('OK')"
```

- [ ] **Step 5: Commit**

```bash
git add framework/multi_agent/tools.py framework/multi_agent/__init__.py
git commit -m "feat: add DelegateTaskTool to framework multi_agent tools"
```

---

### Task 3.6: Add subagent_session_isolated memory factory

**Files:**
- Modify: `framework/memory/layers/factory.py`

**Calibration:** Current `SessionMemoryConfig` only has `max_messages`,
`checkpoint_key`, `last_recovered_key`, and `scope`. Do not add `max_tokens` or
keep-ratio arguments to `SessionMemoryConfig` in this task. Implement subagent
memory as session + session-scoped archive + pending, with knowledge disabled.

- [ ] **Step 1: Add factory method to MemoryLayerFactory**

At the end of the `MemoryLayerFactory` class (or as a new static method), add:

```python
    @staticmethod
    def subagent_session_isolated(
        max_session_messages: int = 50,
    ) -> "MemoryLayerConfigSet":
        """Subagent memory: full session isolation.

        - Session: standard SessionScope (max_messages, max_tokens)
        - Archive: SessionScope — each task session isolated (NOT UserScope)
        - Knowledge: disabled — no SOUL/USER/MEMORY.md access
        - Pending: enabled for input injection
        """
        from framework.memory.core.scope import SessionScope
        from framework.memory.layers.config import (
            ArchiveMemoryConfig,
            MemoryLayerConfigSet,
            PendingPrunedInputMemoryConfig,
            SessionMemoryConfig,
        )

        return MemoryLayerConfigSet(
            session=SessionMemoryConfig(
                max_messages=max_session_messages,
            ),
            archive=ArchiveMemoryConfig(scope=SessionScope()),
            knowledge=None,
            pending=PendingPrunedInputMemoryConfig(enabled=True),
        )
```

- [ ] **Step 2: Run type check**

```bash
python -c "from framework.memory.layers.factory import MemoryLayerFactory; print('OK')"
```

- [ ] **Step 3: Commit**

```bash
git add framework/memory/layers/factory.py
git commit -m "feat: add subagent_session_isolated() to MemoryLayerFactory"
```

---

### Task 3.7: Create SubagentService

**Files:**
- Create: `framework/multi_agent/subagent_service.py`
- Modify: `framework/multi_agent/__init__.py`

- [ ] **Step 1: Write subagent_service.py**

```python
"""SubagentService — lifecycle management for all subagents.

Replaces the legacy SubagentManager. All subagents (resident and dynamic)
live in AgentPool. Dynamic subagents are admitted to the pool and get
full resident treatment (consumer loop, inbox, retention).
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any

from framework.core.emitter import AgentResult, BufferingEmitter
from framework.core.events import EmitterConfig
from framework.core.types import InputMessage
from framework.multi_agent.address import AgentAddress
from framework.multi_agent.envelope import AgentMessageEnvelope

logger = logging.getLogger(__name__)


class SubagentService:
    """Manages subagent lifecycle through AgentPool.

    - Resident subagents: YAML-configured, registered at bot startup.
    - Dynamic subagents: Runtime-created, admitted to pool with TTL.
    - Sync: create_and_wait() — single-shot, no pool, returns AgentResult.
    """

    def __init__(
        self,
        pool: Any,  # AgentPool
        factory: Any,  # AgentFactory
        broker: Any,  # MessageBroker
        agent_bus: Any | None = None,  # AgentMessageBus
        retention_ttl_seconds: float = 86400.0,
    ):
        self._pool = pool
        self._factory = factory
        self._broker = broker
        self._agent_bus = agent_bus
        self._ttl = retention_ttl_seconds

    # ── Resident ──

    async def register_resident(
        self, descriptor: Any, **kwargs: Any
    ) -> Any:
        """Register a pre-configured subagent. Called at bot startup."""
        return await self._pool.register_resident(descriptor, **kwargs)

    # ── Dynamic ──

    async def admit_dynamic(
        self,
        descriptor: Any,  # AgentDescriptor
        initial_task: str,
        ttl_seconds: float | None = None,
    ) -> str:
        """Create a subagent at runtime and admit it to AgentPool.

        1. Register to AgentPool → consumer loop, inbox
        2. Send initial task_request asynchronously
        3. Set TTL for lifecycle management
        Returns: session_id for tracking.
        """
        ttl = ttl_seconds or self._ttl
        instance = await self._pool.register_resident(descriptor)

        conversation_id = f"conv-{descriptor.address.name}"
        session_id = f"{conversation_id}:{uuid.uuid4().hex[:8]}"

        envelope = AgentMessageEnvelope(
            payload={"task_prompt": initial_task},
            source=AgentAddress(kind="agent", name="main"),
            target=descriptor.address,
            message_type="task_request",
            conversation_id=conversation_id,
            agent_session_id=session_id,
            correlation_id=uuid.uuid4().hex,
        )

        await self._broker.send_to(
            descriptor.address, envelope.to_broker_message(),
        )

        now = time.time()
        self._pool._session_meta[session_id] = type(
            "SessionMeta", (), {
                "agent_name": descriptor.address.name,
                "created_at": now,
                "last_active": now,
                "is_dynamic": True,
            }
        )()

        logger.info(
            "Dynamic subagent admitted: %s session=%s ttl=%.0fh",
            descriptor.address.name, session_id, ttl / 3600,
        )
        return session_id

    # ── Sync (framework provides, bot may not expose to LLM) ──

    async def create_and_wait(
        self,
        descriptor: Any,  # AgentDescriptor
        task_prompt: str,
        timeout: float = 120.0,
    ) -> AgentResult:
        """Synchronous: create subagent, execute task, return result.

        Does NOT admit to AgentPool. Uses AgentSession directly.
        Session data is retained after execution.
        """
        instance = await self._factory.create_agent(
            descriptor, mode="session",
        )
        session_id = f"task-{uuid.uuid4().hex[:8]}"

        try:
            assert instance.session is not None
            emitter = BufferingEmitter(config=EmitterConfig())
            result = await asyncio.wait_for(
                instance.session.process_message(
                    message=InputMessage(
                        content=task_prompt, session_id=session_id,
                    ),
                    emitter=emitter,
                    session_id=session_id,
                ),
                timeout=timeout,
            )
            if not result.content and emitter.get_content():
                result.content = emitter.get_content()
            return result
        finally:
            await instance.stop()
```

- [ ] **Step 2: Add to __init__.py exports**

```python
from framework.multi_agent.subagent_service import SubagentService
```
And add `"SubagentService"` to `__all__`.

- [ ] **Step 3: Run import test**

```bash
python -c "from framework.multi_agent.subagent_service import SubagentService; print('OK')"
```

- [ ] **Step 4: Commit**

```bash
git add framework/multi_agent/subagent_service.py framework/multi_agent/__init__.py
git commit -m "feat: add SubagentService (resident + dynamic + sync)"
```

---

### Task 3.8: Separate resident, template, and dynamic lifecycle policies

**Files:**
- Create or modify: `framework/multi_agent/lifecycle.py`
- Modify: `framework/multi_agent/subagent_service.py`
- Modify: `framework/multi_agent/pool.py`
- Modify: `framework/multi_agent/__init__.py`

- [ ] **Step 1: Add typed lifecycle enums**

Use enums instead of raw strings for lifecycle policy:

```python
class SubagentOrigin(str, Enum):
    RESIDENT = "resident"
    TEMPLATE_INSTANCE = "template_instance"
    DYNAMIC = "dynamic"


class SubagentActivationMode(str, Enum):
    EAGER = "eager"
    LAZY = "lazy"
    ON_DEMAND_TEMPLATE = "on_demand_template"
```

- [ ] **Step 2: Store origin and activation mode in subagent metadata**

Resident metadata must be separate from dynamic instance metadata. A resident
subagent may have a stable address before it has an active consumer loop.
Template definitions must not create an address, memory, session, or consumer
until instantiated.

- [ ] **Step 3: Add lazy-resident activation path**

Support descriptor registration without immediately starting full runtime
resources. On first message or task dispatch to a lazy resident target, activate
the descriptor through the normal pool registration path, then process the
message.

- [ ] **Step 4: Add dynamic namespace and retention guards**

Dynamic instances must use a separate id namespace such as
`dyn.<template>.<correlation_id>`. They must not reuse resident session ids,
archive keys, or retention metadata.

- [ ] **Step 5: Add focused tests**

Add tests proving:

- Dynamic creation disabled does not break resident task dispatch.
- Template registration does not allocate a bus identity or consumer.
- Lazy resident registration does not start a consumer until first message.
- Dynamic and resident subagents cannot collide on agent id/session/archive keys.

- [ ] **Step 6: Commit**

```bash
git add framework/multi_agent/lifecycle.py framework/multi_agent/subagent_service.py framework/multi_agent/pool.py framework/multi_agent/__init__.py tests/unit/multi_agent/
git commit -m "feat: separate subagent lifecycle policies"
```

---

## Phase 4: Bot Cleanup & Renaming

**Calibration:** Do not remove descriptor-building capability outright. Remove
the old `helper-sync`/`SpawnSubagentTool` path, but keep or rewrite a unified
`build_subagent_descriptor()` for configured resident subagents and dynamic
subagent defaults.

### Task 4.1: Delete helper-sync agent config

**Files:**
- Modify: `examples/bot_project/config/bot_config.yml`

- [ ] **Step 1: Remove helper-sync from bot_config.yml**

Remove lines 200-218 (the entire `helper-sync` agent block):
```yaml
# DELETE:
  - name: helper-sync
    role: subagent
    system_prompt: |
      ...
    max_steps: 10
    memory:
      ...
```

Also update the capability matrix comment (lines 10-32) to remove the `helper-sync` row.

- [ ] **Step 2: Update the peer role values to subagent**

In the same file, change `role: peer` to `role: subagent` for `office-expert` (line 122) and `query-12306` (line 163).

- [ ] **Step 3: Add subagent config fields**

For each subagent, add `session_retention` and memory isolation fields:

```yaml
    session_retention:
      max_sessions: 50
      ttl_hours: 24
    memory:
      short_term:
        max_messages: 50
        max_tokens: 30000
        keep_ratio_for_messages: 0.5
        keep_ratio_for_token: 0.5
      archive_scope: session
      knowledge: false
      governance: {}
```

- [ ] **Step 4: Verify YAML validity**

```bash
python -c "import yaml; yaml.safe_load(open('examples/bot_project/config/bot_config.yml')); print('OK')"
```

- [ ] **Step 5: Commit**

```bash
git add examples/bot_project/config/bot_config.yml
git commit -m "refactor: delete helper-sync, rename peer→subagent in bot_config.yml"
```

---

### Task 4.2: Delete SpawnSubagentTool from bot

**Files:**
- Modify: `examples/bot_project/bot/tools/custom.py`

- [ ] **Step 1: Remove SpawnSubagentTool class**

Delete the entire `SpawnSubagentTool` class (lines 79-165 approximately) and the `_SUBAGENT_EXCLUDED_TOOLS` constant (lines 31-36).

- [ ] **Step 2: Remove SpawnSubagentTool import from builders.py**

In `examples/bot_project/bot/service/builders.py`:
- Line 11: Delete `from bot.tools.custom import SpawnSubagentTool`
- Lines 164-178: Delete the subagent tool registration block

- [ ] **Step 3: Verify no remaining references**

```bash
grep -rn "SpawnSubagentTool\|spawn_subagent\|_SUBAGENT_EXCLUDED" examples/bot_project/ --include="*.py"
```
Expected: no output.

- [ ] **Step 4: Commit**

```bash
git add examples/bot_project/bot/tools/custom.py examples/bot_project/bot/service/builders.py
git commit -m "refactor: delete SpawnSubagentTool from bot"
```

---

### Task 4.3: Delete old subagent memory/skill methods from builders.py

**Files:**
- Modify: `examples/bot_project/bot/service/builders.py`

- [ ] **Step 1: Delete methods**

Remove these methods from `AgentBuilderMixin`:
- `_create_subagent_memory()` (lines 262-285) — the OLD subagent memory using build_subagent_descriptor
- `_cleanup_subagent_memory()` (lines 328-345)
- `_get_subagent_skill_manager()` (lines 201-248)
- `build_subagent_descriptor()` call (lines 164-178) — already removed in Task 4.2

- [ ] **Step 2: Delete instance attributes**

Remove from `AgentBuilderMixin.__init__` or wherever they're initialized:
```python
self._subagent_memory_systems: dict[str, Any] = {}
self._subagent_skill_managers: dict[str, Any] = {}
```

- [ ] **Step 3: Verify no remaining old-subagent references**

```bash
grep -n "subagent_memory_systems\|_subagent_skill_managers\|build_subagent_descriptor\|_cleanup_subagent_memory\|_get_subagent_skill_manager" examples/bot_project/bot/service/builders.py
```
Expected: no output.

- [ ] **Step 4: Commit**

```bash
git add examples/bot_project/bot/service/builders.py
git commit -m "refactor: delete old subagent memory/skill methods from builders"
```

---

### Task 4.4: Rename peer methods in builders.py

**Files:**
- Modify: `examples/bot_project/bot/service/builders.py`

- [ ] **Step 1: Rename method names**

```python
# Old → New:
_find_peer_cfgs()       → _find_subagent_cfgs()
_initialize_peer_agents() → _initialize_subagent_agents()
_create_peer_memory()     → _create_subagent_memory()
```

- [ ] **Step 2: Rename member variables**

```python
# Old → New:
self._peer_memory_systems  → self._subagent_memory_systems
peer_cfgs                  → subagent_cfgs
peer_names                 → subagent_names
```

- [ ] **Step 3: Update MemoryAgentRole references**

In `_create_subagent_memory()` (renamed from `_create_peer_memory`):
```python
# Old:
            default_agent_role=MemoryAgentRole.PEER,
# New:
            default_agent_role=MemoryAgentRole.SUBAGENT,
```

- [ ] **Step 4: Update all call sites**

In `_initialize_subagent_agents()`:
- Replace `self._peer_memory_systems` → `self._subagent_memory_systems`
- Replace `peer_cfgs` → `subagent_cfgs`
- Replace `self._find_peer_cfgs()` → `self._find_subagent_cfgs()`

- [ ] **Step 5: Run import test**

```bash
python -c "from examples.bot_project.bot.service.builders import AgentBuilderMixin; print('OK')"
```

Note: may need to run from project root with `PYTHONPATH` set.

- [ ] **Step 6: Commit**

```bash
git add examples/bot_project/bot/service/builders.py
git commit -m "refactor: rename peer methods/variables to subagent in builders"
```

---

### Task 4.5: Rename skills/peers/ → skills/subagents/

**Files:**
- Rename: `examples/bot_project/skills/peers/` → `examples/bot_project/skills/subagents/`

- [ ] **Step 1: Rename directory**

```bash
mv examples/bot_project/skills/peers examples/bot_project/skills/subagents
```

- [ ] **Step 2: Update references in builders.py**

In `_get_subagent_skill_manager` or wherever `skills/peers` is referenced:
```python
# Old:
            project_dir / "skills" / "peers" / name,
# New:
            project_dir / "skills" / "subagents" / name,
```

Also update any `skills/peers` references in `bot_config.yml` (already done in Task 4.1).

- [ ] **Step 3: Verify directory exists**

```bash
ls examples/bot_project/skills/subagents/
```

- [ ] **Step 4: Commit**

```bash
git add examples/bot_project/skills/subagents/
git add -u examples/bot_project/
git commit -m "refactor: rename skills/peers/ → skills/subagents/"
```

---

## Phase 5: Bot Additions

### Task 5.1: Create SubagentService instance in BotService

**Files:**
- Modify: `examples/bot_project/bot/service/bot_service.py` (or `core.py` — whichever initializes AgentPool)

- [ ] **Step 1: Find where AgentPool is created**

```bash
grep -n "AgentPool\|agent_pool" examples/bot_project/bot/service/core.py
```

- [ ] **Step 2: Add SubagentService creation**

After `AgentPool` is created, add:
```python
        from framework.multi_agent.subagent_service import SubagentService

        self.subagent_service = SubagentService(
            pool=self.agent_pool,
            factory=self.agent_factory,
            broker=self.broker,
            agent_bus=self.agent_bus,
        )
```

- [ ] **Step 3: Expose subagent_service for builders.py**

Ensure `self.subagent_service` is accessible from `AgentBuilderMixin` (check class hierarchy — `BotService` likely inherits from `AgentBuilderMixin`).

- [ ] **Step 4: Commit**

```bash
git add examples/bot_project/bot/service/core.py
git commit -m "feat: create SubagentService instance in BotService"
```

---

### Task 5.2: Create CreateSubagentTool

**Files:**
- Create: `examples/bot_project/bot/tools/create_subagent.py`
- Modify: `examples/bot_project/bot/service/builders.py`

**Calibration:** `CreateSubagentTool` is optional. It must not be registered
unless `config.subagents.dynamic.enabled` is true. The tool should prefer
`template_name` + `task_prompt` over arbitrary `system_prompt`/`tools`; ad-hoc
prompt/tool creation should require a separate explicit config flag. This keeps
resident subagent use independent from dynamic creation.

- [ ] **Step 1: Write CreateSubagentTool**

```python
"""CreateSubagentTool — dynamic subagent creation for bot_project."""

from __future__ import annotations

import uuid
from typing import Any

from framework.core.tool_manager import Tool, ToolConfig
from framework.multi_agent.address import AgentAddress
from framework.multi_agent.descriptor import AgentDescriptor, AgentLLMConfig


class CreateSubagentTool(Tool):
    """Create a temporary subagent to handle a specific task.

    The subagent is admitted to AgentPool and gets:
    - Consumer loop (can receive follow-up messages)
    - Inbox (async results from task_request processing)
    - Session retention with TTL-based cleanup

    Business constraints:
    - Only main agent can create subagents
    - Subagent cannot create further subagents (tool not registered on them)
    - Subagent can only send messages to main (star topology)
    """

    def __init__(
        self,
        subagent_service: Any,
        agent_factory: Any,
        parent_address: AgentAddress,
        parent_name: str = "main",
    ):
        self._service = subagent_service
        self._factory = agent_factory
        self._parent_address = parent_address
        self._parent_name = parent_name
        super().__init__(
            name="create_subagent",
            description=(
                "Create a temporary subagent to handle a specific task. "
                "The subagent works asynchronously and sends results to your inbox.\n\n"
                "Use when: no existing subagent has the needed capabilities, "
                "you need an isolated context for analysis, or you need parallel "
                "workers for independent tasks.\n\n"
                "The subagent persists for 24h and can receive follow-up messages "
                "via send_message_async."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "task_prompt": {
                        "type": "string",
                        "description": "The task for the subagent to execute",
                    },
                    "system_prompt": {
                        "type": "string",
                        "description": "Custom system prompt. Defaults to a generic executor prompt.",
                    },
                    "tools": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Tools to grant. Defaults to file+shell+search.",
                    },
                    "ttl_hours": {
                        "type": "number",
                        "description": "Subagent lifetime in hours (default 24).",
                        "default": 24,
                    },
                },
                "required": ["task_prompt"],
            },
            config=ToolConfig(),
        )

    async def execute(self, **kwargs: Any) -> str:
        task_prompt: str = kwargs.get("task_prompt", "")
        system_prompt: str | None = kwargs.get("system_prompt")
        tools: list[str] | None = kwargs.get("tools")
        ttl_hours: float = float(kwargs.get("ttl_hours", 24))

        if not task_prompt.strip():
            return "Error: task_prompt is required."

        # Build descriptor
        name = f"task-{uuid.uuid4().hex[:8]}"
        allowed_tools = tools or [
            "read_file", "write_file", "edit_file",
            "list_dir", "shell", "search_files", "find_files",
        ]

        descriptor = AgentDescriptor(
            address=AgentAddress(kind="agent", name=name),
            llm_config=AgentLLMConfig(),
            system_prompt_template=system_prompt or (
                "Execute the given task and return results. "
                "Be thorough and precise. Use available tools as needed."
            ),
            allowed_tools=allowed_tools,
            context_strategy="session_isolated",
            execution_strategy="react",
            max_iterations=15,
            exposed_to_peers=True,
            allowed_callers=[self._parent_name],
        )

        session_id = await self._service.admit_dynamic(
            descriptor=descriptor,
            initial_task=task_prompt,
            ttl_seconds=ttl_hours * 3600,
        )

        return (
            f"Subagent '{name}' created (session: {session_id}). "
            "It will process the task asynchronously and send results to your inbox. "
            "Use send_message_async to communicate with it."
        )
```

- [ ] **Step 2: Register CreateSubagentTool in builders.py**

In `_register_multi_agent_tools()` for main agent, add:
```python
        from bot.tools.create_subagent import CreateSubagentTool

        if self.config.subagents.dynamic.enabled:
            self.tool_manager.register(CreateSubagentTool(
                subagent_service=self.subagent_service,
                agent_factory=self.agent_factory,
                parent_address=parent_address,
                parent_name=parent_name,
                allowed_templates=self.config.subagents.dynamic.allowed_templates,
                allow_ad_hoc=self.config.subagents.dynamic.allow_ad_hoc,
            ))
            print("   [OK] create_subagent registered")
```

- [ ] **Step 3: Add "create_subagent" to SpawnSubagentTool's old spot in builders.py callsite for peers**

In `_initialize_subagent_agents`, remove the old `SpawnSubagentTool` registration for peers (already done in Task 4.2). No replacement needed — subagents don't get CreateSubagentTool.

- [ ] **Step 4: Commit**

```bash
git add examples/bot_project/bot/tools/create_subagent.py examples/bot_project/bot/service/builders.py
git commit -m "feat: add CreateSubagentTool for dynamic subagent creation"
```

---

### Task 5.3: Update bot core.py for renamed functions

**Files:**
- Modify: `examples/bot_project/bot/service/core.py`

- [ ] **Step 1: Update import**

Line 54: `create_peer_governance` → `create_subagent_governance` (if not already done in Phase 2).

- [ ] **Step 2: Update function call**

Line 597: `create_peer_governance(` → `create_subagent_governance(`.

- [ ] **Step 3: Update comment**

Line 692: `PeerAutoSendHook` → `SubagentAutoSendHook`.

- [ ] **Step 4: Commit**

```bash
git add examples/bot_project/bot/service/core.py
git commit -m "refactor: update bot core.py for renamed functions"
```

---

## Phase 6: Test Cleanup & Extensions

### Task 6.1: Delete old subagent manager tests

**Files:**
- Delete: `tests/unit/multi_agent/test_subagent_manager.py`

- [ ] **Step 1: Delete test file**

```bash
rm tests/unit/multi_agent/test_subagent_manager.py
```

- [ ] **Step 2: Check for test_subagent_manager references in conftest or fixtures**

```bash
grep -rn "test_subagent_manager\|SubagentManager" tests/ --include="*.py" | grep -v ".pyc"
```

- [ ] **Step 3: Commit**

```bash
git rm tests/unit/multi_agent/test_subagent_manager.py
git commit -m "test: delete SubagentManager unit tests"
```

---

### Task 6.2: Rename peer-related test files and classes

**Files:**
- Rename: `tests/unit/multi_agent/test_peer_auto_send_hook.py` → `tests/unit/multi_agent/test_subagent_auto_send_hook.py`
- Modify: `tests/unit/multi_agent/test_subagent_auto_send_hook.py`
- Modify: `tests/unit/multi_agent/test_runtime_context_hook_integration.py`
- Modify: `examples/bot_project/tests/test_agent_communication.py`

- [ ] **Step 1: Rename test file**

Already done in Task 2.1 Step 7. Verify:
```bash
ls tests/unit/multi_agent/test_subagent_auto_send_hook.py
```

- [ ] **Step 2: Update class name in test file**

In `tests/unit/multi_agent/test_subagent_auto_send_hook.py:25`:
```python
# Old:
class TestPeerAutoSendHook:
# New:
class TestSubagentAutoSendHook:
```

- [ ] **Step 3: Update test_runtime_context_hook_integration.py**

Replace all `PeerAutoSendHook` → `SubagentAutoSendHook` references:
```bash
sed -i 's/PeerAutoSendHook/SubagentAutoSendHook/g' tests/unit/multi_agent/test_runtime_context_hook_integration.py
```

Also update class names and docstrings that reference "peer".

- [ ] **Step 4: Update test_agent_communication.py (bot tests)**

Replace all `PeerAutoSendHook` → `SubagentAutoSendHook`:
```bash
sed -i 's/PeerAutoSendHook/SubagentAutoSendHook/g' examples/bot_project/tests/test_agent_communication.py
```

Update class name `TestPeerAutoSendHookBot` → `TestSubagentAutoSendHookBot`.

- [ ] **Step 5: Commit**

```bash
git add -u tests/ examples/bot_project/tests/
git commit -m "test: rename PeerAutoSendHook → SubagentAutoSendHook in tests"
```

---

### Task 6.3: Add concurrency test for session cleanup

**Files:**
- Create: `tests/unit/multi_agent/test_session_cleanup.py`

- [ ] **Step 1: Write test for TOCTOU safety**

```python
"""Tests for concurrency-safe session cleanup in AgentPool."""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from framework.multi_agent.pool import AgentPool, SessionMeta, SessionRetentionPolicy


class TestSessionCleanup:
    """Verify that _try_evict_if_stale is safe under concurrent access."""

    @pytest.mark.asyncio
    async def test_eviction_skips_active_session(self):
        """If a session lock is held (active task), eviction skips it."""
        pool = _make_minimal_pool()
        sid = "conv:test:task-abc"
        pool._session_locks[sid] = asyncio.Lock()
        pool._session_meta[sid] = SessionMeta(
            agent_name="test-agent",
            created_at=time.time() - 100000,  # way past TTL
            last_active=time.time() - 100000,
            is_dynamic=True,
        )

        # Hold the lock → simulate active task
        await pool._session_locks[sid].acquire()

        result = await pool._try_evict_if_stale(sid)
        assert result is False  # Should skip, not evict
        assert sid in pool._session_locks  # Lock still present

    @pytest.mark.asyncio
    async def test_eviction_cleans_stale_idle_session(self):
        """If a session is stale and idle (lock not held), eviction proceeds."""
        pool = _make_minimal_pool()
        sid = "conv:test:task-def"
        pool._session_locks[sid] = asyncio.Lock()
        pool._session_meta[sid] = SessionMeta(
            agent_name="test-agent",
            created_at=time.time() - 100000,
            last_active=time.time() - 100000,
            is_dynamic=True,
        )

        mock_instance = MagicMock()
        mock_instance.context_manager.clear = AsyncMock()
        pool._agents["test-agent"] = mock_instance

        result = await pool._try_evict_if_stale(sid)
        assert result is True
        assert sid not in pool._session_locks
        assert sid not in pool._session_meta

    @pytest.mark.asyncio
    async def test_shutdown_ordering(self):
        """shutdown_all: cleanup task cancelled first, then consumers, then evict."""
        pool = _make_minimal_pool()
        cleanup_cancelled = False

        async def fake_cleanup():
            nonlocal cleanup_cancelled
            try:
                while True:
                    await asyncio.sleep(60)
            except asyncio.CancelledError:
                cleanup_cancelled = True
                raise

        pool._cleanup_task = asyncio.create_task(fake_cleanup())

        # Add a mock consumer
        pool._consumers["test-agent"] = asyncio.create_task(
            asyncio.sleep(60)
        )
        pool._agents["test-agent"] = MagicMock()
        pool._agents["test-agent"].stop = AsyncMock()

        await pool.shutdown_all(timeout=1.0)
        assert cleanup_cancelled is True


def _make_minimal_pool():
    """Create AgentPool with minimal dependencies for unit testing."""
    from framework.messaging.broker_memory import InMemoryMessageBroker
    from framework.multi_agent.factory import DefaultAgentFactory

    broker = InMemoryMessageBroker()
    factory = MagicMock(spec=DefaultAgentFactory)
    pool = AgentPool(
        broker=broker,
        agent_factory=factory,
        enable_inbox_polling=False,
        retention=SessionRetentionPolicy(
            ttl_seconds=3600,
            cleanup_interval_seconds=0,  # disable auto-cleanup loop
        ),
    )
    return pool
```

- [ ] **Step 2: Run the test**

```bash
pytest tests/unit/multi_agent/test_session_cleanup.py -v
```

- [ ] **Step 3: Commit**

```bash
git add tests/unit/multi_agent/test_session_cleanup.py
git commit -m "test: add concurrency-safe session cleanup tests"
```

---

### Task 6.4: Run full test suite and fix issues

- [ ] **Step 1: Run framework unit tests**

```bash
pytest tests/unit/ -v --timeout=120 2>&1 | tee test-output.txt
```

- [ ] **Step 2: Fix any failures**

Check for failures related to renamed imports, deleted classes, or missing parameters.

- [ ] **Step 3: Run framework type check**

```bash
mypy framework/ --ignore-missing-imports 2>&1 | head -100
```

- [ ] **Step 4: Run linter**

```bash
ruff check framework/ examples/bot_project/bot/ 2>&1 | head -50
```

- [ ] **Step 5: Run bot integration tests**

```bash
pytest tests/integration/ -v -m integration --timeout=120
```

- [ ] **Step 6: Commit any fixes**

```bash
git add -u
git commit -m "fix: test and lint fixes from full suite run"
```

---

## Implementation Order Summary

```
Phase 0 (Implementation Calibration)
  - 0.1 Verify task_request payload contract
  - 0.2 Document/test pool session lock ownership
  - 0.3 Define subagent lifecycle families and config boundaries

Phase 1 (Framework Deletions)
  ├── 1.1 Extract current_conversation_id         ← MUST DO FIRST
  ├── 1.2 Remove subagent_manager from AgentPipeline
  ├── 1.3 Remove subagent_manager from AgentSession
  ├── 1.4 Delete SubagentManager module
  └── 1.5 Remove MemoryAgentRole.PEER

Phase 2 (Framework Renaming)
  ├── 2.1 PeerAutoSendHook → SubagentAutoSendHook
  ├── 2.2 PeerAgentValidator → SubagentAgentValidator
  └── 2.3 create_peer_* → create_subagent_*

Phase 3 (Framework Additions)
  ├── 3.1 SessionRetentionPolicy
  ├── 3.2 Session tracking helpers
  ├── 3.3 Concurrency-safe cleanup
  ├── 3.4 Sync-future result channel
  ├── 3.5 DelegateTaskTool
  ├── 3.6 subagent_session_isolated()
  ├── 3.7 SubagentService
  └── 3.8 Lifecycle policies

Phase 4 (Bot Cleanup & Renaming)  ← depends on Phase 3
  ├── 4.1 Delete helper-sync config
  ├── 4.2 Delete SpawnSubagentTool
  ├── 4.3 Delete old subagent methods
  ├── 4.4 Rename peer → subagent in builders
  └── 4.5 Rename skills/peers/ → skills/subagents/

Phase 5 (Bot Additions)  ← depends on Phase 4
  ├── 5.1 SubagentService in BotService
  ├── 5.2 CreateSubagentTool
  └── 5.3 Update core.py references

Phase 6 (Test Cleanup)  ← depends on Phase 5
  ├── 6.1 Delete old subagent tests
  ├── 6.2 Rename peer test files/classes
  ├── 6.3 Add cleanup concurrency tests
  └── 6.4 Full suite run + fixes
```

---

## Type Safety Checklist (per rules/type-safety.md)

- [ ] No raw `str` for categories/roles — use `MemoryAgentRole` enum
- [ ] No raw `str` for subagent lifecycle policy — use `SubagentOrigin` and `SubagentActivationMode`
- [ ] No bare `dict[str, Any]` in new function signatures — use typed dataclasses (`SessionMeta`)
- [ ] No `getattr`/`hasattr` in new framework code — use explicit attributes
- [ ] No example-specific config or assumptions in `framework/` new code
- [ ] All new functions have return type annotations
- [ ] Framework and bot code kept in separate files
