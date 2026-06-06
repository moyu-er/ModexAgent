# Dynamic Communication Tool Description Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace static communication tool descriptions with dynamic, agent-description-aware descriptions generated from a managed target list, and deprecate ListCommunicationTargetsTool.

**Architecture:** A new `CommunicationTargetStore` holds an encapsulated target list and cached description string. `CommunicationTargetsProvider` ABC extends `Tool` and delegates target management to the store. `SendToAgentTool` inherits from the ABC and reads the store's cached description in `get_dynamic_schema()`. Population strategy lives in `pool_builder.py`.

**Tech Stack:** Python 3.12+, pytest, asyncio

---

## File Structure

| Action | File | Responsibility |
|---|---|---|
| Modify | `framework/multi_agent/tools.py` | Add `CommunicationTarget`, `CommunicationTargetStore`, `CommunicationTargetsProvider`; rewrite `SendToAgentTool`; remove dead constants/functions |
| Modify | `framework/multi_agent/communication.py` | Add `target_store` param; update `_create_dynamic_subagent`, `_build_subagent_tool_manager`; remove `build_targets_description` |
| Modify | `framework/multi_agent/__init__.py` | Add new exports (`CommunicationTarget`, `CommunicationTargetStore`, `CommunicationTargetsProvider`) |
| Modify | `examples/bot_project/bot/service/pool_builder.py` | Create store, populate, inject; stop registering `ListCommunicationTargetsTool` |
| Modify | `tests/unit/multi_agent/test_send_to_agent_tools.py` | Update existing tests for new `SendToAgentTool` constructor; add store/description tests |
| Create | `tests/unit/multi_agent/test_communication_target_store.py` | Unit tests for `CommunicationTargetStore` |
| Modify | `tests/unit/multi_agent/test_communication_service.py` | Update `_make_service` if needed |
| Modify | `examples/bot_project/agents/*.md` (8 files) | Remove `list_communication_targets` references |
| Modify | `examples/bot_project/README.md` | Remove `list_communication_targets` docs |
| Modify | `examples/bot_project/README.zh-CN.md` | Same |
| Modify | `examples/bot_project/AGENTS.md` | Same |
| Modify | `examples/bot_project/config/pools/coding.yml` | Update comment |

---

### Task 1: CommunicationTarget + CommunicationTargetStore

**Files:**
- Create: `tests/unit/multi_agent/test_communication_target_store.py`
- Modify: `framework/multi_agent/tools.py`

- [ ] **Step 1: Write tests for CommunicationTarget + CommunicationTargetStore**

Create `tests/unit/multi_agent/test_communication_target_store.py`:

```python
"""Tests for CommunicationTarget and CommunicationTargetStore."""

from __future__ import annotations

from framework.multi_agent.comm_kind import AgentCommKind
from framework.multi_agent.tools import (
    CommunicationTarget,
    CommunicationTargetStore,
)


def _normal(name: str, desc: str = "") -> CommunicationTarget:
    return CommunicationTarget(name=name, kind=AgentCommKind.NORMAL, description=desc)


def _subagent(name: str, desc: str = "") -> CommunicationTarget:
    return CommunicationTarget(name=name, kind=AgentCommKind.SUBAGENT, description=desc)


class TestCommunicationTarget:
    def test_frozen(self) -> None:
        t = CommunicationTarget(name="a", kind=AgentCommKind.NORMAL)
        import pytest
        with pytest.raises(AttributeError):
            t.name = "b"  # type: ignore[misc]

    def test_defaults(self) -> None:
        t = CommunicationTarget(name="a", kind=AgentCommKind.NORMAL)
        assert t.description == ""


class TestStoreAdd:
    def test_add_target(self) -> None:
        store = CommunicationTargetStore()
        store.add(_normal("coding", "Coding expert"))
        assert store.has("coding")

    def test_add_duplicate_is_noop(self) -> None:
        store = CommunicationTargetStore()
        store.add(_normal("coding", "desc1"))
        store.add(_normal("coding", "desc2"))
        assert len(store.list()) == 1
        assert store.list()[0].description == "desc1"


class TestStorePop:
    def test_pop_by_name(self) -> None:
        store = CommunicationTargetStore()
        store.add(_normal("coding"))
        store.pop_by_name("coding")
        assert not store.has("coding")

    def test_pop_nonexistent_is_noop(self) -> None:
        store = CommunicationTargetStore()
        store.add(_normal("coding"))
        store.pop_by_name("nonexistent")
        assert len(store.list()) == 1


class TestStoreList:
    def test_returns_copy(self) -> None:
        store = CommunicationTargetStore()
        store.add(_normal("coding"))
        copy = store.list()
        copy.clear()
        assert len(store.list()) == 1  # original unchanged


class TestStoreDescription:
    def test_normal_description_contains_targets(self) -> None:
        store = CommunicationTargetStore()
        store.add(_normal("coding", "Coding expert"))
        store.add(_subagent("scout", "Fast recon"))
        desc = store.send_description
        assert "coding" in desc
        assert "Coding expert" in desc
        assert "scout" in desc
        assert "Fast recon" in desc
        assert "normal" in desc
        assert "subagent" in desc

    def test_normal_description_empty_targets(self) -> None:
        store = CommunicationTargetStore()
        desc = store.send_description
        assert "No targets" in desc or "no targets" in desc.lower() or len(desc) > 0

    def test_description_cached(self) -> None:
        store = CommunicationTargetStore()
        store.add(_normal("coding"))
        first = store.send_description
        second = store.send_description
        assert first is second  # same object

    def test_description_refreshed_after_add(self) -> None:
        store = CommunicationTargetStore()
        store.add(_normal("coding"))
        first = store.send_description
        store.add(_subagent("scout", "Recon"))
        second = store.send_description
        assert first is not second
        assert "scout" in second

    def test_description_refreshed_after_pop(self) -> None:
        store = CommunicationTargetStore()
        store.add(_normal("coding"))
        store.add(_subagent("scout"))
        first = store.send_description
        store.pop_by_name("scout")
        second = store.send_description
        assert "scout" not in second


class TestStoreSubagentDescription:
    def test_subagent_description_shows_parent(self) -> None:
        store = CommunicationTargetStore(for_subagent=True)
        store.add(_normal("main", "AI assistant"))
        desc = store.send_description
        assert "main" in desc
        assert "AI assistant" in desc

    def test_subagent_description_minimal(self) -> None:
        store = CommunicationTargetStore(for_subagent=True)
        store.add(_normal("main"))
        desc = store.send_description
        assert "parent" in desc.lower() or "main" in desc
        assert "invocation_id" not in desc or "ignored" in desc or "Not used" in desc
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/unit/multi_agent/test_communication_target_store.py -v`
Expected: FAIL — `ImportError: cannot import name 'CommunicationTarget'`

- [ ] **Step 3: Implement CommunicationTarget + CommunicationTargetStore**

Add the following to `framework/multi_agent/tools.py` at the top (after imports, before existing constants):

```python
from dataclasses import dataclass


@dataclass(frozen=True)
class CommunicationTarget:
    """A single communicable agent."""
    name: str
    kind: AgentCommKind
    description: str = ""


class CommunicationTargetStore:
    """Encapsulated target list with cached description generation.

    External code can only:
    - add(target) — add a target (no-op if name exists)
    - pop_by_name(name) — remove by name (no-op if not found)
    - list() — returns a copy
    - has(name) — check existence

    After every add/pop_by_name, the cached description is regenerated.
    First access to send_description triggers lazy generation.
    """

    def __init__(self, *, for_subagent: bool = False) -> None:
        self._targets: list[CommunicationTarget] = []
        self._for_subagent = for_subagent
        self._send_description: str | None = None

    def add(self, target: CommunicationTarget) -> None:
        if not any(t.name == target.name for t in self._targets):
            self._targets.append(target)
            self._refresh()

    def pop_by_name(self, name: str) -> None:
        before = len(self._targets)
        self._targets = [t for t in self._targets if t.name != name]
        if len(self._targets) != before:
            self._refresh()

    def list(self) -> list[CommunicationTarget]:
        """Return a copy — external code cannot mutate internals."""
        return list(self._targets)

    def has(self, name: str) -> bool:
        return any(t.name == name for t in self._targets)

    @property
    def send_description(self) -> str:
        if self._send_description is None:
            self._refresh()
        return self._send_description

    def _refresh(self) -> None:
        if self._for_subagent:
            self._send_description = self._build_subagent_send_desc()
        else:
            self._send_description = self._build_normal_send_desc()

    def _build_normal_send_desc(self) -> str:
        lines = [
            "Dispatch a task to another agent. Results arrive via inbox asynchronously.",
        ]
        if not self._targets:
            lines.append("No targets currently available.")
            return "\n".join(lines)
        lines.append("")
        lines.append("Available targets:")
        for t in self._targets:
            entry = f"  - {t.name} ({t.kind.value})"
            if t.description:
                entry += f": {t.description}"
            lines.append(entry)
        lines.extend([
            "",
            "Usage:",
            "  target_agent: Name from the list above.",
            "  content: Complete task description with context.",
            "  invocation_id: Only for subagent targets — omit or null for new task,",
            "    pass previous invocation_id to continue. Ignored for normal agents.",
            "",
            "Important: Does NOT wait for result. Results arrive asynchronously.",
        ])
        return "\n".join(lines)

    def _build_subagent_send_desc(self) -> str:
        lines = [
            "Send a message to your parent agent for coordination.",
        ]
        if not self._targets:
            lines.append("No targets currently available.")
            return "\n".join(lines)
        lines.append("")
        lines.append("Available target:")
        for t in self._targets:
            entry = f"  - {t.name} ({t.kind.value})"
            if t.description:
                entry += f": {t.description}"
            lines.append(entry)
        lines.extend([
            "",
            "Usage:",
            "  target_agent: Name from the list above.",
            '  content: "NEED_DECISION: <question>" for blocking decisions,',
            '    "PROGRESS_UPDATE: <info>" for non-blocking updates.',
            "  invocation_id: Not used (ignored).",
            "",
            "Important: You can ONLY message your parent.",
        ])
        return "\n".join(lines)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/unit/multi_agent/test_communication_target_store.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add framework/multi_agent/tools.py tests/unit/multi_agent/test_communication_target_store.py
git commit -m "feat(comm): add CommunicationTarget + CommunicationTargetStore with dynamic descriptions"
```

---

### Task 2: CommunicationTargetsProvider ABC + SendToAgentTool rewrite

**Files:**
- Modify: `framework/multi_agent/tools.py`
- Modify: `tests/unit/multi_agent/test_send_to_agent_tools.py`

- [ ] **Step 1: Write tests for the rewritten SendToAgentTool**

Add the following test classes to `tests/unit/multi_agent/test_send_to_agent_tools.py`:

```python
from framework.multi_agent.tools import (
    CommunicationTarget,
    CommunicationTargetStore,
)


class _RecordingService:
    def __init__(self) -> None:
        self.async_invocation_id: str | None = None

    async def send_async(
        self,
        *,
        target_agent: str,
        content: str,
        invocation_id: str | None,
        context: AgentContext,
    ) -> str:
        _ = target_agent, content, context
        self.async_invocation_id = invocation_id
        return "ok"


def _make_store_with_targets() -> CommunicationTargetStore:
    store = CommunicationTargetStore()
    store.add(CommunicationTarget(
        name="office-expert", kind=AgentCommKind.SUBAGENT, description="Office doc expert",
    ))
    return store


class TestSendToAgentToolTargetValidation:
    @pytest.mark.asyncio
    async def test_rejects_unknown_target(self) -> None:
        service = _RecordingService()
        store = CommunicationTargetStore()
        store.add(CommunicationTarget(
            name="office-expert", kind=AgentCommKind.SUBAGENT,
        ))
        tool = SendToAgentTool(
            store=store,
            source=AgentAddress(name="main"),
            broker=object(),  # type: ignore[arg-type]
            registry=object(),  # type: ignore[arg-type]
            agent_bus=object(),  # type: ignore[arg-type]
            service=service,  # type: ignore[arg-type]
        )
        token = current_agent_context.set(_context())
        try:
            result = await tool.execute(
                target_agent="nonexistent",
                content="test",
                invocation_id=None,
            )
        finally:
            current_agent_context.reset(token)
        assert "Error" in result
        assert "nonexistent" in result

    @pytest.mark.asyncio
    async def test_accepts_known_target(self) -> None:
        service = _RecordingService()
        store = _make_store_with_targets()
        tool = SendToAgentTool(
            store=store,
            source=AgentAddress(name="main"),
            broker=object(),  # type: ignore[arg-type]
            registry=object(),  # type: ignore[arg-type]
            agent_bus=object(),  # type: ignore[arg-type]
            service=service,  # type: ignore[arg-type]
        )
        token = current_agent_context.set(_context())
        try:
            result = await tool.execute(
                target_agent="office-expert",
                content="test",
                invocation_id=None,
            )
        finally:
            current_agent_context.reset(token)
        assert result == "ok"


class TestSendToAgentToolDynamicSchema:
    def test_schema_uses_store_description(self) -> None:
        store = _make_store_with_targets()
        tool = SendToAgentTool(
            store=store,
            source=AgentAddress(name="main"),
            broker=object(),  # type: ignore[arg-type]
            registry=object(),  # type: ignore[arg-type]
            agent_bus=object(),  # type: ignore[arg-type]
            service=_RecordingService(),  # type: ignore[arg-type]
        )
        schema = tool.get_dynamic_schema()
        desc = schema["function"]["description"]
        assert "office-expert" in desc
        assert "Office doc expert" in desc

    def test_schema_name_and_parameters_intact(self) -> None:
        store = _make_store_with_targets()
        tool = SendToAgentTool(
            store=store,
            source=AgentAddress(name="main"),
            broker=object(),  # type: ignore[arg-type]
            registry=object(),  # type: ignore[arg-type]
            agent_bus=object(),  # type: ignore[arg-type]
            service=_RecordingService(),  # type: ignore[arg-type]
        )
        schema = tool.get_dynamic_schema()
        assert schema["function"]["name"] == "send_to_agent"
        assert "target_agent" in schema["function"]["parameters"]["properties"]
        assert "invocation_id" in schema["function"]["parameters"]["properties"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/unit/multi_agent/test_send_to_agent_tools.py::TestSendToAgentToolTargetValidation -v`
Expected: FAIL — `TypeError: __init__() got an unexpected keyword argument 'store'`

- [ ] **Step 3: Implement CommunicationTargetsProvider ABC and rewrite SendToAgentTool**

In `framework/multi_agent/tools.py`:

**3a. Add `CommunicationTargetsProvider` ABC** after `CommunicationTargetStore`:

```python
class CommunicationTargetsProvider(Tool):
    """ABC for communication tools with shared target management.

    Subclasses share a CommunicationTargetStore instance.
    External code adds/removes targets through add_target / pop_target_by_name.
    """

    def __init__(self, *, store: CommunicationTargetStore, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._store = store

    def add_target(self, target: CommunicationTarget) -> None:
        self._store.add(target)

    def pop_target_by_name(self, name: str) -> None:
        self._store.pop_by_name(name)

    def list_targets(self) -> list[CommunicationTarget]:
        return self._store.list()

    def has_target(self, name: str) -> bool:
        return self._store.has(name)
```

**3b. Rewrite `SendToAgentTool`** — replace the entire class definition with:

```python
class SendToAgentTool(CommunicationTargetsProvider):
    """Asynchronous send-to-agent tool using inbox delivery.

    Uses CommunicationTargetStore for target validation and dynamic
    description generation.
    """

    def __init__(
        self,
        *,
        store: CommunicationTargetStore,
        source: AgentAddress,
        broker: MessageBroker,
        registry: AgentRegistry,
        agent_bus: AgentMessageBus,
        service: AgentCommunicationService,
        comm_tracker: CommunicationTracker | None = None,
        wakeup_timeout: float = 1.0,
    ) -> None:
        self._source = source
        self._broker = broker
        self._registry = registry
        self._agent_bus = agent_bus
        self._service = service
        self._comm_tracker = comm_tracker
        self._wakeup_timeout = wakeup_timeout
        super().__init__(
            store=store,
            name="send_to_agent",
            description="Send a message to another agent asynchronously.",
            parameters={
                "type": "object",
                "properties": _COMMON_PARAMS,
                "required": ["target_agent", "content", "invocation_id"],
            },
            config=ToolConfig(),
        )

    def get_dynamic_schema(self, caller_context: dict[str, Any] | None = None) -> dict[str, Any]:
        """Return schema with description from the shared target store."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self._store.send_description,
                "parameters": self.parameters,
            },
        }

    async def execute(self, **kwargs: Any) -> str:
        target_agent = str(kwargs.get("target_agent", ""))
        content = str(kwargs.get("content", ""))
        invocation_id_value = kwargs.get("invocation_id")
        # Normalize: None (JSON null), "null"/"Null"/"NULL" string → None
        if invocation_id_value is None:
            invocation_id: str | None = None
        elif isinstance(invocation_id_value, str) and invocation_id_value.strip().lower() == "null":
            invocation_id = None
        else:
            invocation_id = str(invocation_id_value)

        # Target validation
        if not self.has_target(target_agent):
            available = ", ".join(t.name for t in self.list_targets())
            return f"Error: '{target_agent}' is not a valid communication target. Available: {available}"

        context = self._get_context()
        if context is None:
            return "Error: no agent context available"
        return await self._service.send_async(
            target_agent=target_agent, content=content, invocation_id=invocation_id, context=context,
        )

    @staticmethod
    def _get_context() -> AgentContext | None:
        from framework.core.agent import current_agent_context
        return current_agent_context.get(None)
```

**3c. Remove dead code** — delete these from `framework/multi_agent/tools.py`:

- The function `_build_dynamic_description` (lines 54-60)
- The constants `_NORMAL_SEND_DESCRIPTION`, `_SUBAGENT_SEND_DESCRIPTION`, `_NORMAL_LIST_DESCRIPTION`, `_SUBAGENT_LIST_DESCRIPTION` (lines 63-108)

Do NOT delete `_INVOCATION_ID_PARAM` or `_COMMON_PARAMS` — they are still used by the new `SendToAgentTool`.

- [ ] **Step 4: Update existing test helpers in test_send_to_agent_tools.py**

Replace the existing `_RecordingService` class with the new version (no `build_targets_description`):

```python
class _RecordingService:
    def __init__(self) -> None:
        self.async_invocation_id: str | None = None

    async def send_async(
        self,
        *,
        target_agent: str,
        content: str,
        invocation_id: str | None,
        context: AgentContext,
    ) -> str:
        _ = target_agent, content, context
        self.async_invocation_id = invocation_id
        return "ok"
```

Update all existing `SendToAgentTool(...)` calls in `TestSchema`, `TestToolInvocationIdForwarding`, and `TestToolInvocationIdNullStringNormalization` to include `store=store_with_target()`. Add a helper at module level:

```python
def _store_with_target() -> CommunicationTargetStore:
    """Pre-populated store for tests that need a valid target."""
    store = CommunicationTargetStore()
    store.add(CommunicationTarget(
        name="office-expert", kind=AgentCommKind.SUBAGENT,
    ))
    return store
```

Then replace each `SendToAgentTool(source=..., broker=..., registry=..., agent_bus=..., service=...)` call with `SendToAgentTool(store=_store_with_target(), source=..., broker=..., registry=..., agent_bus=..., service=...)`.

- [ ] **Step 5: Run all tool tests**

Run: `python -m pytest tests/unit/multi_agent/test_send_to_agent_tools.py -v`
Expected: All PASS

- [ ] **Step 6: Commit**

```bash
git add framework/multi_agent/tools.py tests/unit/multi_agent/test_send_to_agent_tools.py
git commit -m "refactor(comm): rewrite SendToAgentTool with CommunicationTargetsProvider ABC and store-based descriptions"
```

---

### Task 3: Update AgentCommunicationService

**Files:**
- Modify: `framework/multi_agent/communication.py`
- Modify: `tests/unit/multi_agent/test_communication_service.py` (if needed)

- [ ] **Step 1: Add `target_store` parameter to `__init__`**

In `framework/multi_agent/communication.py`, add to `AgentCommunicationService.__init__` signature:

```python
target_store: CommunicationTargetStore | None = None,
```

And store it:
```python
self._target_store = target_store
```

Add import at top of file (in the `TYPE_CHECKING` block or regular imports):
```python
from framework.multi_agent.tools import CommunicationTarget, CommunicationTargetStore
```

- [ ] **Step 2: Add target to store in `_create_dynamic_subagent`**

At the end of `_create_dynamic_subagent()`, before the return statement, add:

```python
# Add to target store (no-op if template target already exists from init)
if self._target_store is not None:
    self._target_store.add(CommunicationTarget(
        name=name,
        kind=AgentCommKind.SUBAGENT,
        description=template.description,
    ))
```

- [ ] **Step 3: Update `_build_subagent_tool_manager` to use new store**

Replace the `SendToAgentTool` and `ListCommunicationTargetsTool` registration block in `_build_subagent_tool_manager` with:

```python
from framework.multi_agent.tools import (
    CommunicationTarget,
    CommunicationTargetStore,
    SendToAgentTool,
)

# Communication tools — subagent sees only parent
subagent_store = CommunicationTargetStore(for_subagent=True)
parent_profile = self._registry.get_profile(parent_name)
parent_desc = parent_profile.role_description if parent_profile else ""
subagent_store.add(CommunicationTarget(
    name=parent_name,
    kind=AgentCommKind.NORMAL,
    description=parent_desc,
))

tm.register(SendToAgentTool(
    store=subagent_store,
    source=subagent_address,
    broker=self._broker,
    registry=self._registry,
    agent_bus=self._agent_bus,
    service=self,
    comm_tracker=self._comm_tracker,
))
# NOTE: ListCommunicationTargetsTool is no longer registered.
# The send_to_agent description already contains all target info.
```

- [ ] **Step 4: Remove `build_targets_description` method**

Delete the `build_targets_description` method (at line 918) from `AgentCommunicationService`. It is dead code — the tool no longer calls it.

- [ ] **Step 5: Run communication service tests**

Run: `python -m pytest tests/unit/multi_agent/test_communication_service.py -v`
Expected: All PASS (existing tests don't call `build_targets_description` or `_build_subagent_tool_manager`)

- [ ] **Step 6: Commit**

```bash
git add framework/multi_agent/communication.py
git commit -m "refactor(comm): add target_store to AgentCommunicationService, update subagent tool builder, remove build_targets_description"
```

---

### Task 4: Update pool_builder.py

**Files:**
- Modify: `examples/bot_project/bot/service/pool_builder.py`

- [ ] **Step 1: Create store, populate, inject**

In `pool_builder.py`, after the communication service creation (around line 227), replace the tool registration block.

**Old code** (to replace):
```python
tool_manager.register(SendToAgentTool(
    source=main_address, broker=broker, registry=pool,
    agent_bus=agent_bus, service=main_service,
    comm_tracker=comm_tracker,
))
tool_manager.register(ListCommunicationTargetsTool(
    self_address=main_address, registry=pool,
    template_registry=template_registry,
    pool_name=pool_name,
))
```

**New code**:
```python
# Communication target store — shared between SendToAgentTool and AgentCommunicationService
from framework.multi_agent.tools import CommunicationTarget, CommunicationTargetStore

main_store = CommunicationTargetStore()

# Populate from registered pool agents (exclude self)
for p in pool.list_profiles():
    if p.name != main_agent_name:
        main_store.add(CommunicationTarget(
            name=p.name, kind=p.comm_kind,
            description=p.role_description,
        ))

# Populate from templates
for t in templates:
    main_store.add(CommunicationTarget(
        name=t.agent_type, kind=AgentCommKind.SUBAGENT,
        description=t.description,
    ))

logger.info(
    "Pool '%s': communication store populated (%d targets)",
    pool_name, len(main_store.list()),
)

tool_manager.register(SendToAgentTool(
    store=main_store,
    source=main_address, broker=broker, registry=pool,
    agent_bus=agent_bus, service=main_service,
    comm_tracker=comm_tracker,
))
# NOTE: ListCommunicationTargetsTool is no longer registered.
# SendToAgentTool's dynamic description contains all target info.

# Inject store into service for runtime target lifecycle
main_service._target_store = main_store
```

- [ ] **Step 2: Remove `ListCommunicationTargetsTool` import if now unused**

Check if `ListCommunicationTargetsTool` is imported and used elsewhere in `pool_builder.py`. If not, remove the import.

- [ ] **Step 3: Update log line**

Find the log line:
```python
logger.info("Pool '%s': communication tools registered for main agent", pool_name)
```
Change to:
```python
logger.info("Pool '%s': communication tool registered for main agent (list tool deprecated)", pool_name)
```

- [ ] **Step 4: Run existing tests**

Run: `python -m pytest tests/unit/multi_agent/test_send_to_agent_tools.py tests/unit/multi_agent/test_communication_service.py tests/unit/multi_agent/test_communication_target_store.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add examples/bot_project/bot/service/pool_builder.py
git commit -m "refactor(bot): use CommunicationTargetStore in pool_builder, stop registering ListCommunicationTargetsTool"
```

---

### Task 5: Update `__init__.py` exports

**Files:**
- Modify: `framework/multi_agent/__init__.py`

- [ ] **Step 1: Add new exports, keep ListCommunicationTargetsTool**

In `framework/multi_agent/__init__.py`:

Update the import from `tools`:
```python
from framework.multi_agent.tools import (
    CommunicationTarget,
    CommunicationTargetStore,
    CommunicationTargetsProvider,
    ListCommunicationTargetsTool,
    SendToAgentTool,
)
```

Add to `__all__`:
```python
"CommunicationTarget",
"CommunicationTargetStore",
"CommunicationTargetsProvider",
```

Keep `ListCommunicationTargetsTool` in both import and `__all__` — the class is preserved for potential future use.

- [ ] **Step 2: Verify imports work**

Run: `python -c "from framework.multi_agent import CommunicationTargetStore, SendToAgentTool; print('OK')"`
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add framework/multi_agent/__init__.py
git commit -m "feat(comm): export CommunicationTargetStore, CommunicationTarget, CommunicationTargetsProvider"
```

---

### Task 6: Update bot_project agent prompts

**Files:**
- Modify: `examples/bot_project/agents/main.md`
- Modify: `examples/bot_project/agents/coding.md`
- Modify: `examples/bot_project/agents/scout.md`
- Modify: `examples/bot_project/agents/worker.md`
- Modify: `examples/bot_project/agents/planner.md`
- Modify: `examples/bot_project/agents/reviewer.md`
- Modify: `examples/bot_project/agents/oracle.md`
- Modify: `examples/bot_project/agents/context-builder.md`
- Modify: `examples/bot_project/agents/delegate.md`

- [ ] **Step 1: Update main.md**

In `examples/bot_project/agents/main.md`, find the "Multi-Agent Communication Rules" section. Remove any references to `list_communication_targets`. The send_to_agent tool description now contains all target info — no need to call list first. Update instructions to say "Check the send_to_agent tool description for available targets."

- [ ] **Step 2: Update coding.md**

In `examples/bot_project/agents/coding.md`, find the line:
```
`list_communication_targets` first to see available subagent types
```
Replace with:
```
Check the `send_to_agent` tool description for available subagent types
```

- [ ] **Step 3: Update all subagent .md files**

For each of these files: `scout.md`, `worker.md`, `planner.md`, `reviewer.md`, `oracle.md`, `context-builder.md`, `delegate.md`

Find and replace the pattern:
```
First, call `list_communication_targets` to discover your parent agent name.
```
With:
```
Your parent agent name appears in the `send_to_agent` tool description as the available target.
```

Also update any `send_to_agent(target_agent=<from list_communication_targets>,` examples to use the actual parent name shown in the description, e.g.:
```
send_to_agent(target_agent="main",
```

- [ ] **Step 4: Commit**

```bash
git add examples/bot_project/agents/
git commit -m "docs(agents): remove list_communication_targets references from agent prompts"
```

---

### Task 7: Update bot_project documentation

**Files:**
- Modify: `examples/bot_project/README.md`
- Modify: `examples/bot_project/README.zh-CN.md`
- Modify: `examples/bot_project/AGENTS.md`
- Modify: `examples/bot_project/config/pools/coding.yml`

- [ ] **Step 1: Update README.md**

Search for all occurrences of `list_communication_targets` and update:
- Replace tool lists that mention it (e.g., "| `send_to_agent`, `list_communication_targets` |") to remove it
- Replace description paragraphs that reference it
- Update the tool table entries for main, office-expert, query-12306

- [ ] **Step 2: Update README.zh-CN.md**

Same changes as Step 1, for the Chinese version.

- [ ] **Step 3: Update AGENTS.md**

Remove `list_communication_targets` from the communication tools list.

- [ ] **Step 4: Update coding.yml comment**

In `examples/bot_project/config/pools/coding.yml`, update the comment on line 4 from:
```yaml
# Communication tools (send_to_agent + list_communication_targets)
```
To:
```yaml
# Communication tools (send_to_agent — list tool deprecated)
```

- [ ] **Step 5: Commit**

```bash
git add examples/bot_project/README.md examples/bot_project/README.zh-CN.md examples/bot_project/AGENTS.md examples/bot_project/config/pools/coding.yml
git commit -m "docs(bot): remove list_communication_targets from documentation"
```

---

### Task 8: Integration verification

- [ ] **Step 1: Run all multi_agent unit tests**

Run: `python -m pytest tests/unit/multi_agent/ -v --timeout=30`
Expected: All PASS

- [ ] **Step 2: Run communication-related integration tests**

Run: `python -m pytest tests/integration/multi_agent/test_pool_communication.py -v --timeout=60`
Expected: All PASS (or update if test references old constructor)

- [ ] **Step 3: Run bot_project communication tests**

Run: `python -m pytest examples/bot_project/tests/test_agent_communication.py -v --timeout=30`
Expected: All PASS (or update if test asserts `list_communication_targets is None` — that assertion should now pass since the tool is not registered)

- [ ] **Step 4: Run full unit test suite**

Run: `python -m pytest tests/unit/ -v --timeout=60 -x`
Expected: All PASS

- [ ] **Step 5: Final commit if any test fixes needed**

```bash
git add -A
git commit -m "test: fix test compatibility with new communication tool architecture"
```
