# Dynamic Subagent Creation & Unified Communication — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** LLM can dynamically create subagents at runtime from templates via a unified `send_to_agent` tool with XML-structured communication.

**Architecture:** Three new framework modules (`template.py`, `template_registry.py`, `message_xml.py`) form the foundation. Communication layer is refactored — sync tool deleted, async tool renamed and extended with template-aware routing. Hooks switch from text-prefix to XML output. Example project migrates from YAML-declared subagents to per-pool template files.

**Tech Stack:** Python 3.12+, Pydantic, pytest, PyYAML

---

## Phase 1: Foundation modules

### Task 1: AgentTemplate dataclass

**Files:**
- Create: `framework/multi_agent/template.py`
- Create: `tests/unit/multi_agent/test_template.py`

- [ ] **Step 1: Write the dataclass**

```python
# framework/multi_agent/template.py
"""AgentTemplate — preset definition for dynamically created subagents."""

from __future__ import annotations

from dataclasses import dataclass, field

from framework.ioc.configs.memory import MemoryConfig
from framework.ioc.configs.skills import SkillsConfig


@dataclass
class AgentTemplate:
    """Preset definition for a dynamically creatable subagent type.

    Communication tools (send_to_agent + list_communication_targets) are
    auto-injected by the framework — they must not appear in template config.
    """
    agent_type: str
    description: str = ""
    max_steps: int = 20
    standard_tools: bool = True
    use_terminal: bool = True
    mcp_filter: list[str] | None = None
    memory: MemoryConfig | None = None
    skills: SkillsConfig | None = None
```

- [ ] **Step 2: Write the test**

```python
# tests/unit/multi_agent/test_template.py
"""Tests for AgentTemplate."""

from framework.ioc.configs.memory import MemoryConfig
from framework.ioc.configs.skills import SkillsConfig
from framework.multi_agent.template import AgentTemplate


def test_agent_template_defaults():
    t = AgentTemplate(agent_type="test")
    assert t.agent_type == "test"
    assert t.description == ""
    assert t.max_steps == 20
    assert t.standard_tools is True
    assert t.use_terminal is True
    assert t.mcp_filter is None
    assert t.memory is None
    assert t.skills is None


def test_agent_template_full():
    t = AgentTemplate(
        agent_type="code-reviewer",
        description="Reviews code",
        max_steps=30,
        standard_tools=False,
        use_terminal=False,
        mcp_filter=["mcp-a"],
        memory=MemoryConfig(),
        skills=SkillsConfig(roots=["skills/reviewer"]),
    )
    assert t.max_steps == 30
    assert t.standard_tools is False
    assert t.mcp_filter == ["mcp-a"]
```

- [ ] **Step 3: Run tests**

Run: `pytest tests/unit/multi_agent/test_template.py -v`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add framework/multi_agent/template.py tests/unit/multi_agent/test_template.py
git commit -m "feat: add AgentTemplate dataclass for dynamic subagent presets"
```

---

### Task 2: AgentTemplateRegistry

**Files:**
- Create: `framework/multi_agent/template_registry.py`
- Create: `tests/unit/multi_agent/test_template_registry.py`

- [ ] **Step 1: Check YAML availability**

Run: `python -c "import yaml; print('ok')"`
Expected: ok (PyYAML already in requirements)

- [ ] **Step 2: Write the registry**

```python
# framework/multi_agent/template_registry.py
"""AgentTemplateRegistry — scans and loads per-pool subagent templates."""

from __future__ import annotations

import logging
from pathlib import Path

import yaml

from framework.ioc.configs.memory import MemoryConfig
from framework.ioc.configs.skills import SkillsConfig
from framework.multi_agent.template import AgentTemplate

logger = logging.getLogger(__name__)


class AgentTemplateRegistry:
    """Scans config/pools/*/templates/*.yml and loads AgentTemplate definitions.

    Templates are isolated by pool_name — a template only exists within
    the pool directory it's defined in.
    """

    def __init__(self, project_dir: Path) -> None:
        self._templates: dict[str, dict[str, AgentTemplate]] = {}
        self._load(project_dir)

    def _load(self, project_dir: Path) -> None:
        pools_dir = project_dir / "config" / "pools"
        if not pools_dir.exists():
            return

        for pool_dir in pools_dir.iterdir():
            if not pool_dir.is_dir():
                continue
            templates_dir = pool_dir / "templates"
            if not templates_dir.exists():
                continue

            pool_name = pool_dir.name
            self._templates[pool_name] = {}

            for yml_path in templates_dir.glob("*.yml"):
                try:
                    with open(yml_path, encoding="utf-8") as f:
                        raw = yaml.safe_load(f)
                    if not raw or "agent_type" not in raw:
                        logger.warning("Skipping invalid template: %s", yml_path)
                        continue

                    template = AgentTemplate(
                        agent_type=raw["agent_type"],
                        description=raw.get("description", ""),
                        max_steps=raw.get("max_steps", 20),
                        standard_tools=raw.get("standard_tools", True),
                        use_terminal=raw.get("use_terminal", True),
                        mcp_filter=raw.get("mcp_filter"),
                        memory=(
                            MemoryConfig.model_validate(raw["memory"])
                            if raw.get("memory") else None
                        ),
                        skills=(
                            SkillsConfig(roots=raw["skills"]["roots"])
                            if raw.get("skills") else None
                        ),
                    )
                    self._templates[pool_name][template.agent_type] = template
                    logger.debug("Loaded template %s for pool %s", template.agent_type, pool_name)
                except Exception:
                    logger.exception("Failed to load template: %s", yml_path)

    def list_templates(self, pool_name: str) -> list[AgentTemplate]:
        return list(self._templates.get(pool_name, {}).values())

    def get_template(self, pool_name: str, agent_type: str) -> AgentTemplate | None:
        return self._templates.get(pool_name, {}).get(agent_type)
```

- [ ] **Step 3: Write the test**

```python
# tests/unit/multi_agent/test_template_registry.py
"""Tests for AgentTemplateRegistry."""

import tempfile
from pathlib import Path

from framework.multi_agent.template_registry import AgentTemplateRegistry


def _write_yml(dir_path: Path, name: str, content: str) -> None:
    dir_path.mkdir(parents=True, exist_ok=True)
    (dir_path / f"{name}.yml").write_text(content, encoding="utf-8")


def test_registry_loads_templates():
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp)
        templates_dir = project / "config" / "pools" / "main" / "templates"
        _write_yml(templates_dir, "helper", """\
agent_type: helper
description: A helper agent
max_steps: 10
standard_tools: false
""")

        registry = AgentTemplateRegistry(project)
        templates = registry.list_templates("main")
        assert len(templates) == 1
        assert templates[0].agent_type == "helper"
        assert templates[0].max_steps == 10
        assert templates[0].standard_tools is False


def test_registry_pool_isolation():
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp)
        _write_yml(project / "config" / "pools" / "main" / "templates", "a",
                   "agent_type: a\ndescription: ''")
        _write_yml(project / "config" / "pools" / "coding" / "templates", "b",
                   "agent_type: b\ndescription: ''")

        registry = AgentTemplateRegistry(project)
        assert len(registry.list_templates("main")) == 1
        assert len(registry.list_templates("coding")) == 1
        assert registry.get_template("main", "a") is not None
        assert registry.get_template("main", "b") is None


def test_registry_empty_when_no_templates():
    with tempfile.TemporaryDirectory() as tmp:
        registry = AgentTemplateRegistry(Path(tmp))
        assert registry.list_templates("nonexistent") == []
        assert registry.get_template("nonexistent", "x") is None
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/unit/multi_agent/test_template_registry.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add framework/multi_agent/template_registry.py tests/unit/multi_agent/test_template_registry.py
git commit -m "feat: add AgentTemplateRegistry for per-pool template loading"
```

---

### Task 3: message_xml.py — XML builders

**Files:**
- Create: `framework/multi_agent/message_xml.py`
- Create: `tests/unit/multi_agent/test_message_xml.py`

- [ ] **Step 1: Write the XML builders**

```python
# framework/multi_agent/message_xml.py
"""XML message builders for inter-agent communication.

Two formats:
- build_agent_message: LLM actively called send_to_agent
- build_agent_result: hook-generated turn result (LLM didn't call comm tool)
"""

from __future__ import annotations

import xml.sax.saxutils as saxutils


def build_agent_message(
    *,
    source: str,
    invocation_id: str | None,
    content: str,
) -> str:
    """Build <agent_message> XML for LLM-initiated communication."""
    inv_attr = f' invocation_id="{saxutils.escape(invocation_id)}"' if invocation_id else ""
    lines = [
        f'<agent_message source="{saxutils.escape(source)}"{inv_attr}>',
        f"  <content>{saxutils.escape(content)}</content>",
        "</agent_message>",
    ]
    return "\n".join(lines)


def build_agent_result(
    *,
    source: str,
    invocation_id: str | None,
    status: str,
    stop_reason: str,
    content: str,
) -> str:
    """Build <agent_result> XML for hook-generated turn results."""
    inv_attr = f' invocation_id="{saxutils.escape(invocation_id)}"' if invocation_id else ""
    lines = [
        f'<agent_result source="{saxutils.escape(source)}"{inv_attr}'
        f' status="{saxutils.escape(status)}">',
        f"  <stop_reason>{saxutils.escape(stop_reason)}</stop_reason>",
        f"  <content>{saxutils.escape(content)}</content>",
        "</agent_result>",
    ]
    return "\n".join(lines)
```

- [ ] **Step 2: Write the test**

```python
# tests/unit/multi_agent/test_message_xml.py
"""Tests for message_xml builders."""

from framework.multi_agent.message_xml import build_agent_message, build_agent_result


def test_build_agent_message_with_invocation_id():
    result = build_agent_message(
        source="office-expert",
        invocation_id="abc123",
        content="Task done.",
    )
    assert '<agent_message source="office-expert" invocation_id="abc123">' in result
    assert "<content>Task done.</content>" in result


def test_build_agent_message_without_invocation_id():
    result = build_agent_message(
        source="main",
        invocation_id=None,
        content="Hello.",
    )
    assert 'source="main"' in result
    assert "invocation_id" not in result


def test_build_agent_result_completed():
    result = build_agent_result(
        source="office-expert",
        invocation_id="abc123",
        status="completed",
        stop_reason="missed_communication",
        content="All tasks finished.",
    )
    assert '<agent_result source="office-expert" invocation_id="abc123" status="completed">' in result
    assert "<stop_reason>missed_communication</stop_reason>" in result
    assert "<content>All tasks finished.</content>" in result


def test_build_agent_result_max_iterations():
    result = build_agent_result(
        source="planner",
        invocation_id="xyz789",
        status="max_iterations",
        stop_reason="max_iterations",
        content="Still working...",
    )
    assert 'status="max_iterations"' in result
    assert "<stop_reason>max_iterations</stop_reason>" in result


def test_xml_escapes_special_chars():
    result = build_agent_message(
        source="agent<>",
        invocation_id='id"&',
        content="<hello> & world",
    )
    assert "&lt;agent&lt;&gt;" in result
    assert "&lt;hello&gt; &amp; world" in result
```

- [ ] **Step 3: Run tests**

Run: `pytest tests/unit/multi_agent/test_message_xml.py -v`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add framework/multi_agent/message_xml.py tests/unit/multi_agent/test_message_xml.py
git commit -m "feat: add XML message builders for agent communication"
```

---

## Phase 2: Deletions

### Task 4: Delete SendToAgentTool (sync) + update __init__.py + update tests

**Files:**
- Modify: `framework/multi_agent/tools.py`
- Modify: `framework/multi_agent/__init__.py`
- Modify: `tests/unit/multi_agent/test_send_to_agent_tools.py`

- [ ] **Step 1: Delete SendToAgentTool class from tools.py**

Remove `SendToAgentTool` class (lines ~64-127 that define the sync tool). The `_COMMON_PARAMS`, `_INVOCATION_ID_PARAM`, `_build_dynamic_description` helpers remain — the async tool still uses them. Remove the import of `SendToAgentTool` if any callers reference it.

- [ ] **Step 2: Update __init__.py exports**

In `framework/multi_agent/__init__.py`, remove `SendToAgentTool` from the import line and from `__all__`. The old import was:
```python
from framework.multi_agent.tools import (
    ListCommunicationTargetsTool,
    SendToAgentAsyncTool,
    SendToAgentTool,
)
```
Change to:
```python
from framework.multi_agent.tools import (
    ListCommunicationTargetsTool,
    SendToAgentAsyncTool,
)
```

Also remove `"SendToAgentTool"` from `__all__`.

- [ ] **Step 3: Update tests**

In `tests/unit/multi_agent/test_send_to_agent_tools.py`:

Remove the sync-specific test methods:
- `TestSchema.test_sync_tool_has_required_invocation_id`
- `TestToolInvocationIdForwarding.test_sync_tool_forwards_invocation_id_to_service`

Update `TestNewToolExports.test_new_tools_exported_from_multi_agent`:
```python
def test_new_tools_exported_from_multi_agent(self) -> None:
    from framework.multi_agent import SendToAgentAsyncTool
    assert SendToAgentAsyncTool is not None
```

Remove `SendToAgentTool` from the import line at the top.

Update `_RecordingService.send_sync` — remove it since it's only used by the sync tool tests:
```python
class _RecordingService:
    def __init__(self) -> None:
        self.async_invocation_id: str | None = None

    async def send_async(
        self, *, target_agent: str, content: str,
        invocation_id: str | None, context,
    ) -> str:
        _ = target_agent, content, context
        self.async_invocation_id = invocation_id
        return "ok"

    def build_targets_description(self) -> str:
        return "Available targets:\n- office-expert (subagent)"
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/unit/multi_agent/test_send_to_agent_tools.py -v`
Expected: PASS (remaining tests)

- [ ] **Step 5: Commit**

```bash
git add framework/multi_agent/tools.py framework/multi_agent/__init__.py tests/unit/multi_agent/test_send_to_agent_tools.py
git commit -m "refactor: delete sync SendToAgentTool, never used by bot_project"
```

---

### Task 5: Delete agent_source_prefix / ensure_agent_source_prefix

**Files:**
- Modify: `framework/core/message_utils.py`
- Modify: `framework/hook/builtin/inbox_flush.py`
- Modify: `framework/pipeline/context_assembler.py`
- Modify: `tests/unit/core/test_agent_message_utils.py`

- [ ] **Step 1: Remove functions from message_utils.py**

Delete these two functions from `framework/core/message_utils.py`:
- `agent_source_prefix()` (line 26-27)
- `ensure_agent_source_prefix()` (line 30-70ish)

- [ ] **Step 2: Remove usage from inbox_flush.py**

In `framework/hook/builtin/inbox_flush.py`:

Remove import:
```python
from framework.core.message_utils import ensure_agent_source_prefix
```

In `_flush()`, change:
```python
# old:
"content": ensure_agent_source_prefix(sanitized, safe_name),
# new:
"content": sanitized,
```

Messages are already self-describing XML — the prefix is redundant.

- [ ] **Step 3: Remove usage from context_assembler.py**

In `framework/pipeline/context_assembler.py`:

Remove import:
```python
from ..core.message_utils import ensure_agent_source_prefix
```

Change the `source_agent` block (around line 96-101):
```python
# old:
"content": ensure_agent_source_prefix(multimodal_content, str(source_agent)),
# new:
"content": multimodal_content,
```

Also remove the `MessageRole.AGENT` import if it was only used here (check: it is used elsewhere).

- [ ] **Step 4: Update tests**

In `tests/unit/core/test_agent_message_utils.py`:

Remove `ensure_agent_source_prefix` from import. Delete these two test functions:
- `test_ensure_agent_source_prefix_for_string_is_idempotent`
- `test_ensure_agent_source_prefix_for_multimodal_inserts_text_block`

Update `test_normalize_agent_messages_converts_role_without_duplicate_prefix` to not reference the prefix:
```python
def test_normalize_agent_messages_converts_role_without_duplicate_prefix() -> None:
    messages = [
        {
            "role": MessageRole.AGENT,
            "source_agent": "subagent-a",
            "content": "<agent_message source=\"subagent-a\"><content>hello</content></agent_message>",
        }
    ]
    converted, has_agent = normalize_agent_messages_for_llm(messages)
    assert has_agent is True
    assert converted == [{
        "role": MessageRole.USER,
        "content": "<agent_message source=\"subagent-a\"><content>hello</content></agent_message>",
    }]
```

- [ ] **Step 5: Run tests**

Run: `pytest tests/unit/core/test_agent_message_utils.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add framework/core/message_utils.py framework/hook/builtin/inbox_flush.py framework/pipeline/context_assembler.py tests/unit/core/test_agent_message_utils.py
git commit -m "refactor: remove agent_source_prefix, replaced by XML message format"
```

---

### Task 6: Remove subagent_configs from PoolConfig

**Files:**
- Modify: `framework/ioc/configs/pool.py`
- Modify: `tests/unit/bot/test_pool_isolation.py`

- [ ] **Step 1: Delete subagent_configs property**

In `framework/ioc/configs/pool.py`, delete the `subagent_configs` property (lines 42-44):
```python
# delete:
@property
def subagent_configs(self) -> list[AgentConfig]:
    return [a for a in self.agents if a.role == "subagent"]
```

- [ ] **Step 2: Update test**

In `tests/unit/bot/test_pool_isolation.py`, delete `test_subagent_configs_filters_correctly`. Only the pool isolation tests (terminal, MCP, etc.) should remain.

- [ ] **Step 3: Run tests**

Run: `pytest tests/unit/bot/test_pool_isolation.py -v`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add framework/ioc/configs/pool.py tests/unit/bot/test_pool_isolation.py
git commit -m "refactor: remove subagent_configs, subagents now defined via templates"
```

---

## Phase 3: Communication layer refactor

### Task 7: Refactor AgentCommunicationService for template-aware routing

**Files:**
- Modify: `framework/multi_agent/communication.py`

- [ ] **Step 1: Add template registry + AgentPool references**

Add new constructor parameter `template_registry` and `pool` (AgentPool). The service needs them to resolve template names and create dynamic agents:

```python
# framework/multi_agent/communication.py — additional constructor params

from framework.multi_agent.template_registry import AgentTemplateRegistry

if TYPE_CHECKING:
    from framework.multi_agent.pool import AgentPool

class AgentCommunicationService:
    def __init__(
        self,
        source: AgentAddress,
        broker: MessageBroker,
        registry: AgentRegistry,
        *,
        agent_bus: AgentMessageBus | None = None,
        session_strategy: DefaultSessionIdStrategy | None = None,
        comm_tracker: CommunicationTracker | None = None,
        template_registry: AgentTemplateRegistry | None = None,  # new
        pool: AgentPool | None = None,  # new
    ) -> None:
        ...
        self._template_registry = template_registry
        self._pool = pool
```

- [ ] **Step 2: Add `_resolve_target` method**

Add a new method that resolves `target_agent` to an `AgentCommKind` and template (if applicable):

```python
def _resolve_target(self, target_agent: str, pool_name: str | None) -> tuple[AgentCommKind | None, AgentTemplate | None]:
    """Resolve target_agent to comm_kind + optional template."""
    # 1. Check if registered in AgentPool
    from framework.multi_agent.pool import AgentPool
    if isinstance(self._registry, AgentPool):
        descriptor = self._registry.get_descriptor(target_agent)
        if descriptor is not None:
            return descriptor.comm_kind, None

    # 2. Check if it's a template type
    if self._template_registry is not None and pool_name is not None:
        template = self._template_registry.get_template(pool_name, target_agent)
        if template is not None:
            return AgentCommKind.SUBAGENT, template

    return None, None
```

- [ ] **Step 3: Add dynamic creation method**

```python
async def _create_dynamic_subagent(
    self,
    template: AgentTemplate,
    target_agent_name: str,
    conversation_id: str,
    invocation_id: str,
    pool_name: str,
    project_dir: Path | None,
    content: str,
) -> AgentSendResult:
    """Create a dynamic subagent from template and send initial task."""
    if self._pool is None:
        return AgentSendResult(
            target_agent=target_agent_name,
            target_kind=AgentCommKind.SUBAGENT,
            session_id="",
            invocation_id=None,
            created_new_task=False,
            error="AgentPool not available for dynamic creation",
        )

    # Build AgentDescriptor from template
    from framework.multi_agent.descriptor import AgentDescriptor, AgentLLMConfig
    from framework.multi_agent.address import AgentAddress

    # Load system prompt from .md file
    system_prompt = ""
    if project_dir is not None:
        md_path = project_dir / "agents" / pool_name / f"{template.agent_type}.md"
        if md_path.exists():
            system_prompt = md_path.read_text(encoding="utf-8")

    descriptor = AgentDescriptor(
        address=AgentAddress(name=target_agent_name, comm_kind=AgentCommKind.SUBAGENT),
        llm_config=AgentLLMConfig(),
        system_prompt_template=system_prompt,
        max_iterations=template.max_steps,
        execution_strategy="react",
        context_strategy="persistent",
    )

    # Create MemorySystemContext with invocation_id-scoped session
    # (Delegated to pool_builder pattern — memory is created per-template)
    # For now, use a basic InMemoryContextManager as default
    from framework.core.context import InMemoryContextManager
    memory_ctx = InMemoryContextManager(base_system_prompt=system_prompt)

    # Register in pool
    await self._pool.register_resident(descriptor, context_manager=memory_ctx)

    # Send initial task
    session_id = self._session_strategy.format(
        conversation_id=conversation_id,
        agent_name=target_agent_name,
        invocation_id=invocation_id,
    )
    envelope = AgentMessageEnvelope(
        payload={"content": content, "message_type": "task_request"},
        source=self._source,
        target=AgentAddress(name=target_agent_name),
        message_type="task_request",
        conversation_id=conversation_id,
        agent_session_id=session_id,
        invocation_id=invocation_id,
    )
    await self._broker.send_to(
        AgentAddress(name=target_agent_name),
        envelope.to_broker_message(),
    )

    return AgentSendResult(
        target_agent=target_agent_name,
        target_kind=AgentCommKind.SUBAGENT,
        session_id=session_id,
        invocation_id=invocation_id,
        created_new_task=True,
    )
```

- [ ] **Step 4: Update `_send()` method routing**

At the top of `_send()`, after step 2 (target lookup), add dynamic creation logic:

```python
# In _send(), replace the target kind resolution with _resolve_target:
_target_kind, _template = self._resolve_target(target_agent, pool_name)

if _target_kind is None:
    return AgentSendResult(
        target_agent=target_agent,
        target_kind=AgentCommKind.NORMAL,
        session_id="",
        invocation_id=None,
        created_new_task=False,
        error=f"Target agent '{target_agent}' not found",
    )

# If SUBAGENT + template matched + empty invocation_id → create
if _target_kind == AgentCommKind.SUBAGENT and _template is not None and not invocation_id:
    import uuid
    name = f"{_template.agent_type}.{uuid.uuid4().hex[:8]}"
    new_invocation_id = uuid.uuid4().hex[:8]
    return await self._create_dynamic_subagent(
        template=_template,
        target_agent_name=name,
        conversation_id=conversation_id,
        invocation_id=new_invocation_id,
        pool_name=pool_name,
        project_dir=getattr(self, '_project_dir', None),
        content=content,
    )

target_kind = _target_kind
# ... rest of existing _send() logic
```

Note: `pool_name` needs to be accessible. Add it as a new parameter to `send_sync`/`send_async`/`_send` or derive from context. For simplicity, add `pool_name` to service constructor:

```python
def __init__(self, ..., pool_name: str | None = None, project_dir: Path | None = None):
    ...
    self._pool_name = pool_name
    self._project_dir = project_dir
```

- [ ] **Step 5: Update existing tests**

Run: `pytest tests/unit/multi_agent/test_send_to_agent_tools.py -v`
Expected: PASS (tests use `_RecordingService`, not real `AgentCommunicationService`)

- [ ] **Step 6: Commit**

```bash
git add framework/multi_agent/communication.py
git commit -m "feat: add template-aware routing to AgentCommunicationService"
```

---

### Task 8: Rename SendToAgentAsyncTool → SendToAgentTool

**Files:**
- Modify: `framework/multi_agent/tools.py`
- Modify: `framework/multi_agent/__init__.py`

- [ ] **Step 1: Rename class + update tool name/description**

In `framework/multi_agent/tools.py`:

Rename class `SendToAgentAsyncTool` → `SendToAgentTool`.

Update `__init__` tool name and description:
```python
super().__init__(
    name="send_to_agent",
    description=(
        "Send a message to another agent asynchronously. "
        "The agent processes the message and results arrive via inbox — "
        "this tool does NOT return the actual result directly. "
        "For subagent targets: if invocation_id is null/empty, a NEW subagent "
        "instance is created from the matching template. If invocation_id has a "
        "value, the message is routed to that existing session. "
        "For normal targets: invocation_id is ignored. "
        "Call list_communication_targets FIRST to see available targets "
        "and their invocation_id requirements."
    ),
    ...
)
```

- [ ] **Step 2: Update __init__.py exports**

In `framework/multi_agent/__init__.py`:
```python
from framework.multi_agent.tools import (
    ListCommunicationTargetsTool,
    SendToAgentTool,  # was SendToAgentAsyncTool
)
```
Update `__all__`:
```python
"SendToAgentTool",  # was "SendToAgentAsyncTool"
```

- [ ] **Step 3: Update bot_project references**

Update all `SendToAgentAsyncTool` → `SendToAgentTool` in `pool_builder.py`:
```python
# old:
tool_manager.register(SendToAgentAsyncTool(...))
# new:
tool_manager.register(SendToAgentTool(...))
```

Update in `builders.py` (AgentBuilderMixin._register_multi_agent_tools):
```python
# old:
self.tool_manager.register(SendToAgentAsyncTool(...))
# new:
self.tool_manager.register(SendToAgentTool(...))
```

Update print statement:
```python
print("   [OK] send_to_agent registered")
# was: print("   [OK] send_to_agent_async registered")
```

- [ ] **Step 4: Run existing tests**

Run: `pytest tests/unit/multi_agent/test_send_to_agent_tools.py tests/unit/bot/ -v`
Expected: tests referencing old class names will fail — fix in Task 17

- [ ] **Step 5: Commit**

```bash
git add framework/multi_agent/tools.py framework/multi_agent/__init__.py examples/bot_project/bot/service/pool_builder.py examples/bot_project/bot/service/builders.py
git commit -m "refactor: rename SendToAgentAsyncTool→SendToAgentTool, send_to_agent_async→send_to_agent"
```

---

### Task 9: Update ListCommunicationTargetsTool to show templates

**Files:**
- Modify: `framework/multi_agent/tools.py`

- [ ] **Step 1: Add template_registry + pool_name to constructor**

```python
class ListCommunicationTargetsTool(Tool):
    def __init__(
        self,
        *,
        self_address: AgentAddress,
        registry: AgentRegistry,
        template_registry: AgentTemplateRegistry | None = None,  # new
        pool_name: str | None = None,  # new
    ) -> None:
        self._self_address = self_address
        self._registry = registry
        self._template_registry = template_registry
        self._pool_name = pool_name
        ...
```

- [ ] **Step 2: Add template entries in execute()**

After the existing target list loop, add template entries:

```python
# After existing targets block (before summary), add:
if self._template_registry is not None and self._pool_name is not None:
    templates = self._template_registry.list_templates(self._pool_name)
    existing_names = {p.name for p in profiles}
    for t in templates:
        if t.agent_type not in existing_names:
            lines.append(f"## [template] {t.agent_type}")
            lines.append(f"  Kind: SUBAGENT")
            if t.description:
                lines.append(f"  Description: {t.description}")
            lines.append('  invocation_id: "" (creates new instance) OR "<existing>" (continue session)')
            lines.append(f'    Create: send_to_agent(target_agent="{t.agent_type}", content="...", invocation_id="")')
            lines.append("")
```

Also update summary to include template entries.

- [ ] **Step 3: Run tests**

Run: `pytest tests/unit/multi_agent/test_send_to_agent_tools.py -v -k "ListComm"`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add framework/multi_agent/tools.py
git commit -m "feat: ListCommunicationTargetsTool shows available templates"
```

---

## Phase 4: Hooks

### Task 10: Update InboxFlushHook — remove prefix, accept XML

**Files:**
- Modify: `framework/hook/builtin/inbox_flush.py`
- Modify: `tests/unit/multi_agent/inbox/test_inbox_flush_hook.py`

The prefix removal from `_flush()` was already done in Task 5. This task updates the test.

- [ ] **Step 1: Update test expectations**

In `tests/unit/multi_agent/inbox/test_inbox_flush_hook.py`, update `test_before_turn_injects_messages`:

```python
async def test_before_turn_injects_messages(self):
    server = InMemoryInboxServer()
    consumer = InboxConsumer(server=server)
    hook = InboxFlushHook(consumer=consumer, agent_name="main")

    xml_content = '<agent_message source="helper"><content>done</content></agent_message>'
    await server.receive(
        "s1",
        InboxMessage(session_id="s1", source="helper", content=xml_content, message_type="agent_message"),
    )

    history = ListMessageHistory([])
    ctx = AgentContext(
        system_prompt="",
        history=history,
        tool_manager=MagicMock(spec=ToolManager),
        session_id="s1",
    )
    await hook.before_turn(ctx)

    msgs = await history.to_list()
    assert len(msgs) == 1
    assert msgs[0]["role"] == "agent"
    assert msgs[0]["source_agent"] == "helper"
    # Content is the XML message itself, NOT prefixed with [From Agent ...]
    assert msgs[0]["content"] == xml_content
    assert msgs[0].get("meta_inbox") is True
```

- [ ] **Step 2: Run tests**

Run: `pytest tests/unit/multi_agent/inbox/test_inbox_flush_hook.py -v`
Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add tests/unit/multi_agent/inbox/test_inbox_flush_hook.py
git commit -m "test: update InboxFlushHook tests for XML message format"
```

---

### Task 11: Update SubagentAutoSendHook — XML + dynamic reply target

**Files:**
- Modify: `framework/hook/builtin/subagent_auto_send.py`
- Modify: `tests/unit/multi_agent/test_subagent_auto_send_hook.py`

- [ ] **Step 1: Rewrite hook to use XML and dynamic reply target**

Replace the `after_turn` method to:
1. Derive reply target from `session_meta` instead of hardcoded `parent_name`
2. Wrap content via `build_agent_result` instead of sending raw text
3. Use `send_to_agent` tool name detection (updated from `send_to_agent_async`)

```python
# framework/hook/builtin/subagent_auto_send.py — updated after_turn

async def after_turn(self, ctx: AgentContext, result: Any = None) -> None:
    if not result or not getattr(result, "content", None):
        return

    rt = ctx.runtime
    if rt is None:
        return

    # Check if send_to_agent was called this turn
    rc = rt._runtime_context
    if rc is None:
        rt_mgr = rt.services.runtime_context_manager
        if rt_mgr is not None:
            rc = await rt_mgr.get_context(ctx.session_id, None)
            rt._runtime_context = rc

    if rc is not None:
        calls = await rc.get_tool_calls()
        sent_tools = {"send_to_agent"}  # updated from send_to_agent_async
        if any(c.tool_name in sent_tools for c in calls):
            self._communicated.add(ctx.session_id)
            logger.debug(
                "SubagentAutoSendHook: skipped, message already sent via tool (agent=%s)",
                self._self_name,
            )
            return

    if ctx.session_id in self._communicated:
        return

    # Derive reply target from session_meta
    reply_target = self._resolve_reply_target(ctx)
    if reply_target is None:
        logger.warning("SubagentAutoSendHook: cannot determine reply target for %s", self._self_name)
        return

    # Build XML result
    invocation_id = None
    if ctx.session_meta is not None:
        invocation_id = ctx.session_meta.invocation_id

    from framework.multi_agent.message_xml import build_agent_result
    sanitized = self._sanitize_forward_content(result.content)
    xml_content = build_agent_result(
        source=self._self_name,
        invocation_id=invocation_id,
        status="completed",
        stop_reason="missed_communication",
        content=sanitized,
    )

    # Deliver via agent_bus
    session_id = ctx.session_id or ""
    from framework.multi_agent.session_id import DefaultSessionIdStrategy
    strategy = DefaultSessionIdStrategy()
    parts = strategy.parse(session_id)
    inbox_key = strategy.format(
        conversation_id=parts.conversation_id,
        agent_name=reply_target,
    )

    from framework.multi_agent.address import AgentAddress
    from framework.multi_agent.envelope import AgentMessageEnvelope
    envelope = AgentMessageEnvelope(
        payload={"content": xml_content, "message_type": "agent_result"},
        source=AgentAddress(name=self._self_name),
        target=AgentAddress(name=reply_target),
        message_type="agent_result",
        conversation_id=parts.conversation_id,
        agent_session_id=inbox_key,
        invocation_id=invocation_id,
    )

    try:
        await self._agent_bus.send(inbox_key, envelope)
        logger.info("SubagentAutoSendHook: delivered agent_result to %s", reply_target)
    except Exception:
        logger.exception("SubagentAutoSendHook: delivery failed")

    if self._svc is not None:
        try:
            await self._svc.notify(
                ctx=ctx,
                notification_type="missed_communication",
                reason="subagent did not call send_to_agent",
                details=f"agent '{self._self_name}' completed but did not call send_to_agent",
                content=sanitized[:2000] if sanitized else None,
            )
        except Exception:
            logger.exception("SubagentAutoSendHook: notification failed")
```

Add `_resolve_reply_target` method:
```python
def _resolve_reply_target(self, ctx: AgentContext) -> str | None:
    """Derive reply target from session_meta, falling back to constructor parent_name."""
    if ctx.session_meta is not None:
        sid = ctx.session_meta.session_id
        from framework.multi_agent.session_id import DefaultSessionIdStrategy
        parts = DefaultSessionIdStrategy().parse(sid)
        if parts.conversation_id:
            # conversation_id is the primary conv scope
            # For subagent sessions: {conv}:{agent}:{invocation_id}
            # Reply target = conv prefix = the agent that owns this conversation
            conv_parts = parts.conversation_id.split(":")
            if len(conv_parts) > 0:
                return conv_parts[0]
    return self._parent_name  # fallback
```

Remove `parent_name` as required constructor param — make it a fallback only:
```python
def __init__(
    self,
    agent_bus: AgentMessageBus,
    self_name: str,
    parent_name: str = "main",  # fallback only
    notification_service: Any | None = None,
) -> None:
```

- [ ] **Step 2: Update tests**

In `tests/unit/multi_agent/test_subagent_auto_send_hook.py`, update `test_auto_sends_when_no_tool_call`:

Make `session_id` a 3-part subagent format and set `session_meta` on context:
```python
async def test_auto_sends_when_no_tool_call(self):
    bus = self._make_bus()
    hook = SubagentAutoSendHook(agent_bus=bus, self_name="office-expert")
    mgr = RuntimeContextManager()
    ctx = self._make_ctx([], session_id="conv_001:office-expert:abc123", runtime_mgr=mgr)
    # Set session_meta so hook can derive reply target
    from framework.core.agent import AgentSessionMeta
    from framework.multi_agent.comm_kind import AgentCommKind
    ctx.session_meta = AgentSessionMeta(
        conversation_id="conv_001",
        agent_name="office-expert",
        comm_kind=AgentCommKind.SUBAGENT,
        invocation_id="abc123",
    )
    result = AgentResult(content="Task completed successfully.")

    await hook.after_turn(ctx, result)

    assert bus.send.called
    envelope = bus.send.call_args[0][1]
    # Content should be <agent_result> XML
    assert "<agent_result" in envelope.payload["content"]
    assert 'source="office-expert"' in envelope.payload["content"]
    assert 'invocation_id="abc123"' in envelope.payload["content"]
    assert 'status="completed"' in envelope.payload["content"]
    assert "missed_communication" in envelope.payload["content"]
```

Add `test_skips_when_tool_called` updated to check for `send_to_agent` (not `send_to_agent_async`):
```python
async def test_skips_when_tool_called(self):
    bus = self._make_bus()
    hook = SubagentAutoSendHook(agent_bus=bus, self_name="office-expert")
    mgr = RuntimeContextManager()
    ctx = self._make_ctx([], session_id="conv_001:office-expert:abc123", runtime_mgr=mgr)
    # Record a send_to_agent call
    rc = await mgr.get_context(ctx.session_id, None)
    from framework.core.runtime_context import ToolCallRecord
    rc._tool_calls.append(
        ToolCallRecord(tool_name="send_to_agent", arguments={"target_agent": "main"}, result="ok")
    )
    result = AgentResult(content="Done.")
    await hook.after_turn(ctx, result)
    assert not bus.send.called
```

- [ ] **Step 3: Run tests**

Run: `pytest tests/unit/multi_agent/test_subagent_auto_send_hook.py -v`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add framework/hook/builtin/subagent_auto_send.py tests/unit/multi_agent/test_subagent_auto_send_hook.py
git commit -m "refactor: SubagentAutoSendHook uses XML + dynamic reply target from session_meta"
```

---

### Task 12: Update MaxIterationNotifyHook + AgentNotificationService

**Files:**
- Modify: `framework/hook/notification.py`

- [ ] **Step 1: Update AgentNotificationService._build_xml()**

Replace the entire `_build_xml()` method and remove the `_notify_user` / `_notify_parent` references to it. Instead, the `notify` method receives a pre-built XML string from hooks:

```python
class AgentNotificationService:
    def __init__(
        self,
        output_adapter: OutputAdapter,
        agent_bus: AgentMessageBus,
        session_strategy: DefaultSessionIdStrategy | None = None,
        # parent_map removed
    ):
        self._output_adapter = output_adapter
        self._agent_bus = agent_bus
        self._session_strategy = session_strategy or DefaultSessionIdStrategy()

    async def notify(
        self,
        ctx: AgentContext,
        xml_content: str,  # pre-built XML from build_agent_result
    ) -> None:
        if (
            ctx.session_meta is not None
            and ctx.session_meta.comm_kind == AgentCommKind.SUBAGENT
        ):
            await self._notify_parent(ctx, xml_content)
        else:
            await self._notify_user(ctx, xml_content)

    async def _notify_user(self, ctx: AgentContext, xml: str) -> None:
        from framework.core.types import OutputMessage
        await self._output_adapter.send(
            OutputMessage(content=xml), ctx.session_id,
        )

    async def _notify_parent(self, ctx: AgentContext, xml: str) -> None:
        # Derive parent from session_meta
        if ctx.session_meta is None:
            return
        # conversation_id is the parent conv scope
        parent_name = ctx.session_meta.conversation_id.split(":")[0] if ":" in ctx.session_meta.conversation_id else "main"

        session_id = ctx.session_id or ""
        parts = self._session_strategy.parse(session_id)
        inbox_key = self._session_strategy.format(
            conversation_id=parts.conversation_id,
            agent_name=parent_name,
        )

        from framework.multi_agent.address import AgentAddress
        from framework.multi_agent.envelope import AgentMessageEnvelope
        envelope = AgentMessageEnvelope(
            payload={"content": xml, "message_type": "agent_result"},
            source=AgentAddress(name=ctx.session_meta.agent_name),
            target=AgentAddress(name=parent_name),
            message_type="agent_result",
            conversation_id=parts.conversation_id,
            agent_session_id=inbox_key,
        )
        await self._agent_bus.send(inbox_key, envelope)
```

- [ ] **Step 2: Update MaxIterationNotifyHook**

```python
class MaxIterationNotifyHook:
    def __init__(self, notification_service: AgentNotificationService):
        self._svc = notification_service

    async def after_turn(self, ctx: AgentContext, result: AgentResult) -> None:
        if getattr(result, "stop_reason", None) != "max_iterations":
            return

        agent_name = (
            ctx.session_meta.agent_name
            if ctx.session_meta
            else "unknown"
        )
        invocation_id = ctx.session_meta.invocation_id if ctx.session_meta else None

        content = result.content or ""
        truncated = content[:2000]
        if len(content) > 2000:
            truncated += "\n... (truncated)"

        from framework.multi_agent.message_xml import build_agent_result
        xml = build_agent_result(
            source=agent_name,
            invocation_id=invocation_id,
            status="max_iterations",
            stop_reason="max_iterations",
            content=truncated,
        )
        await self._svc.notify(ctx=ctx, xml_content=xml)
```

- [ ] **Step 3: Update pool_builder references**

In `pool_builder.py`, remove `parent_map` when constructing `AgentNotificationService`:
```python
notification_service = AgentNotificationService(
    output_adapter=output_adapter,
    agent_bus=agent_bus,
    session_strategy=session_strategy,
    # parent_map removed
)
```

Also update the `SubagentAutoSendHook` constructor calls in `pool_builder.py` to drop `parent_name`:
```python
_add_hook(sub_instance.pipeline, SubagentAutoSendHook(
    agent_bus=agent_bus,
    self_name=sub_name,
    notification_service=notification_service,
))
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/unit/ -v -k "hook" --timeout=30 2>&1 | head -80`
Check that hook-related tests pass.

- [ ] **Step 5: Commit**

```bash
git add framework/hook/notification.py examples/bot_project/bot/service/pool_builder.py
git commit -m "refactor: MaxIterationNotifyHook + AgentNotificationService use build_agent_result, remove parent_map"
```

---

## Phase 5: Example project migration

### Task 13: Create template YAML files for all 4 subagents

**Files:**
- Create: `examples/bot_project/config/pools/main/templates/office-expert.yml`
- Create: `examples/bot_project/config/pools/main/templates/query-12306.yml`
- Create: `examples/bot_project/config/pools/coding/templates/reviewer.yml`
- Create: `examples/bot_project/config/pools/coding/templates/planner.yml`

- [ ] **Step 1: Create main pool templates**

```bash
mkdir -p examples/bot_project/config/pools/main/templates
mkdir -p examples/bot_project/config/pools/coding/templates
```

`examples/bot_project/config/pools/main/templates/office-expert.yml`:
```yaml
agent_type: office-expert
description: "处理 Office 文档（Word/Excel/PPT/PDF），支持格式转换、内容提取、编辑批注"
max_steps: 30
standard_tools: true
use_terminal: false
mcp_filter: []
memory:
  short_term: {max_messages: 80, max_tokens: 30000, keep_ratio_for_messages: 0.5, keep_ratio_for_token: 0.5}
  governance: {}
skills:
  roots: ["skills/main/office-expert"]
```

`examples/bot_project/config/pools/main/templates/query-12306.yml`:
```yaml
agent_type: query-12306
description: "查询 12306 火车票，余票、车次时刻表"
max_steps: 50
standard_tools: false
use_terminal: false
mcp_filter: ["12306-mcp"]
memory:
  short_term: {max_messages: 80, max_tokens: 50000}
```

`examples/bot_project/config/pools/coding/templates/reviewer.yml`:
```yaml
agent_type: reviewer
description: "Expert code reviewer — analyzes code for correctness, security, and style issues"
max_steps: 80
standard_tools: true
use_terminal: false
mcp_filter: []
memory:
  short_term: {max_messages: 80, max_tokens: 80000}
  long_term: {enabled: true}
  governance: {}
skills:
  roots: ["skills/coding/reviewer"]
```

`examples/bot_project/config/pools/coding/templates/planner.yml`:
```yaml
agent_type: planner
description: "Task planner — breaks down complex goals into structured implementation steps"
max_steps: 50
standard_tools: true
use_terminal: false
mcp_filter: []
memory:
  short_term: {max_messages: 50, max_tokens: 50000}
  governance: {}
skills:
  roots: ["skills/coding/planner"]
```

- [ ] **Step 2: Commit**

```bash
git add examples/bot_project/config/pools/main/templates/ examples/bot_project/config/pools/coding/templates/
git commit -m "feat: add subagent template YAML files for all 4 subagent types"
```

---

### Task 14: Extract system prompts to .md files

**Files:**
- Create: `examples/bot_project/agents/main/office-expert.md`
- Create: `examples/bot_project/agents/main/query-12306.md`

- [ ] **Step 1: Extract office-expert system prompt**

`examples/bot_project/agents/main/office-expert.md`:
```markdown
你是文档专家 Agent，擅长处理各种 Office 文档（Word、Excel、PowerPoint、PDF）。

## 核心规则 —— 违反则结果丢失
你是独立运行的后台 Agent，主 Agent 通过消息委托任务给你。
**主 Agent 看不到你直接输出的任何文本。唯一能让主 Agent 收到结果的方式是发起 `send_to_agent` 工具调用。**

### 操作模式
1. 收到任务 → 使用你的工具和技能执行
2. 任务完成后 → **最后一轮必须发起工具调用**

   ```
   send_to_agent(
     target_agent="main",
     content="任务执行摘要：...",
     invocation_id=null
   )
   ```

3. 没有 `send_to_agent` 调用的回复 → 主 Agent 收不到，等同于任务未完成

### 常见错误（必须避免）
- ❌ 错误：只写"任务完成了" → 主 Agent 永远看不到
- ✅ 正确：把结果作为 `send_to_agent` 的 `content` 参数发送
```

- [ ] **Step 2: Extract query-12306 system prompt**

`examples/bot_project/agents/main/query-12306.md`:
```markdown
你是 12306 火车票查询助手 Agent，专门处理火车票查询、余票查询、车次时刻表等任务。

## 核心规则 —— 违反则结果丢失
你是独立运行的后台 Agent，主 Agent 通过消息委托任务给你。
**主 Agent 看不到你直接输出的任何文本。唯一能让主 Agent 收到结果的方式是发起 `send_to_agent` 工具调用。**

### 操作模式
1. 收到任务 → 使用 12306 MCP 工具完成查询
2. 任务完成后 → **最后一轮必须发起工具调用**：

   ```
   send_to_agent(
     target_agent="main",
     content="查询摘要：...",
     invocation_id=null
   )
   ```

3. 没有 `send_to_agent` 调用的回复 → 主 Agent 收不到，等同于任务未完成

### 常见错误（必须避免）
- ❌ 错误：只写"查询完成" → 主 Agent 永远看不到
- ✅ 正确：把结果作为 `send_to_agent` 的 `content` 参数发送
```

Note: `send_to_agent_async` → `send_to_agent`, `invocation_id=null` (was empty string — now unified).

- [ ] **Step 3: Commit**

```bash
git add examples/bot_project/agents/main/office-expert.md examples/bot_project/agents/main/query-12306.md
git commit -m "feat: extract subagent system prompts to .md files"
```

---

### Task 15: Remove subagent entries from pool configs

**Files:**
- Modify: `examples/bot_project/config/pools/main.yml`
- Modify: `examples/bot_project/config/pools/coding.yml`

- [ ] **Step 1: Update main.yml**

Remove the `office-expert` and `query-12306` agent entries from `agents:` list. Keep only `main`.

Delete lines from `- name: office-expert` through the end of the `query-12306` block (approximately lines 63-127).

- [ ] **Step 2: Update coding.yml**

Remove the `reviewer` and `planner` agent entries. Keep only `coding`.

Delete lines from `- name: reviewer` through the end of the `planner` block (approximately lines 38-57).

- [ ] **Step 3: Commit**

```bash
git add examples/bot_project/config/pools/main.yml examples/bot_project/config/pools/coding.yml
git commit -m "refactor: remove subagent entries from pool configs, replaced by templates"
```

---

### Task 16: Update pool_builder.py + core.py

**Files:**
- Modify: `examples/bot_project/bot/service/pool_builder.py`
- Modify: `examples/bot_project/bot/service/core.py`
- Modify: `examples/bot_project/bot/service/builders.py`

- [ ] **Step 1: Remove subagent iteration from pool_builder.py**

In `create_pool()`, remove the loop that iterates `pool_cfg.subagent_configs` and registers each subagent (approximately lines 198-288 that handle step 10.5 through step 11).

Replace with template loading:
```python
# Step 10.5: Load templates for list_communication_targets discovery
template_registry = AgentTemplateRegistry(project_dir)
templates = template_registry.list_templates(pool_name)
logger.info("Pool '%s': %d templates available for dynamic creation", pool_name, len(templates))

# Pass template_registry to AgentCommunicationService and tools
```

Update `AgentCommunicationService` construction to pass `template_registry` and `pool`:
```python
main_service = AgentCommunicationService(
    source=main_address, broker=broker, registry=pool,
    agent_bus=agent_bus, session_strategy=session_strategy,
    comm_tracker=comm_tracker,
    template_registry=template_registry,  # new
    pool=pool,  # new
    pool_name=pool_name,  # new
    project_dir=project_dir,  # new
)
```

Update `ListCommunicationTargetsTool` construction:
```python
tool_manager.register(ListCommunicationTargetsTool(
    self_address=main_address, registry=pool,
    template_registry=template_registry,  # new
    pool_name=pool_name,  # new
))
```

Update `SendToAgentTool` construction — also pass template_registry, pool, pool_name, project_dir.

Remove the subagent registration loop entirely. Remove the `parent_map` dict. Remove `_add_hook` calls that wired `SubagentAutoSendHook` per subagent (templates handle this at creation time).

Update `PoolInstance` if it references per-subagent fields that no longer exist.

- [ ] **Step 2: Update core.py _print_pool_info()**

In `examples/bot_project/bot/service/core.py`, update `_print_pool_info`:
```python
def _print_pool_info(self) -> None:
    print(f"\n[INFO] Pools: {list(self._pools.keys())}")
    for name, pi in self._pools.items():
        templates = pi.tool_manager  # or get from template_registry
        print(f"   {name}: {pi.main_agent_name}")
    ...
```

- [ ] **Step 3: Update builders.py AgentBuilderMixin**

Remove `_initialize_additional_subagents` — subagents are now created dynamically, not at startup.

Update `_register_multi_agent_tools` — rename `SendToAgentAsyncTool`→`SendToAgentTool` and update tool name in print.

- [ ] **Step 4: Verify pool_builder references**

Run: `grep -r "subagent_configs" examples/`
Expected: no remaining references (except in spec doc and test that was already updated)

- [ ] **Step 5: Commit**

```bash
git add examples/bot_project/bot/service/
git commit -m "refactor: pool_builder uses AgentTemplateRegistry, removes static subagent registration"
```

---

## Phase 6: Test updates

### Task 17: Update all existing tests to match new API

**Files:**
- Modify: `tests/unit/multi_agent/test_send_to_agent_tools.py`
- Modify: `tests/unit/bot/test_hooks_and_comm.py` (if exists)
- Modify: any test referencing old exports

- [ ] **Step 1: Fix test_send_to_agent_tools.py**

Update `TestNewToolExports.test_send_to_agent_tool_importable`:
```python
def test_send_to_agent_tool_importable(self) -> None:
    from framework.multi_agent.tools import SendToAgentTool
    assert SendToAgentTool.__name__ == "SendToAgentTool"
```

Update `TestNewToolExports.test_send_to_agent_async_tool_importable` — rename to verify the rename:
```python
def test_send_to_agent_tool_is_renamed(self) -> None:
    from framework.multi_agent.tools import SendToAgentTool
    # Old name should not exist
    assert not hasattr(
        __import__("framework.multi_agent.tools", fromlist=["SendToAgentAsyncTool"]),
        "SendToAgentAsyncTool"
    )
    # tool name attribute
    tool = SendToAgentTool(
        source=AgentAddress(name="main"),
        broker=object(),
        registry=object(),
        agent_bus=object(),
        service=_RecordingService(),
    )
    assert tool.name == "send_to_agent"
```

- [ ] **Step 2: Run all multi_agent tests**

Run: `pytest tests/unit/multi_agent/ -v --timeout=30`
Expected: PASS

- [ ] **Step 3: Fix test_agent_message_utils.py**

Already updated in Task 5. Verify:
Run: `pytest tests/unit/core/test_agent_message_utils.py -v`
Expected: PASS

- [ ] **Step 4: Run full test suite**

Run: `pytest tests/unit/ -v --timeout=60 2>&1 | tail -30`
Fix any remaining failures.

- [ ] **Step 5: Commit**

```bash
git add tests/
git commit -m "test: update tests for SendToAgentTool rename + XML format + template integration"
```

---

### Task 18: Integration test for dynamic subagent creation flow

**Files:**
- Create: `tests/unit/multi_agent/test_dynamic_subagent_integration.py`

- [ ] **Step 1: Write integration test**

```python
# tests/unit/multi_agent/test_dynamic_subagent_integration.py
"""Integration test: template → registry → AgentDescriptor → pool registration."""

import tempfile
from pathlib import Path

import pytest
from framework.multi_agent.template import AgentTemplate
from framework.multi_agent.template_registry import AgentTemplateRegistry


def _write_files(base: Path, pool: str, agent_type: str, yml_content: str, md_content: str):
    tpl_dir = base / "config" / "pools" / pool / "templates"
    tpl_dir.mkdir(parents=True, exist_ok=True)
    (tpl_dir / f"{agent_type}.yml").write_text(yml_content, encoding="utf-8")
    agents_dir = base / "agents" / pool
    agents_dir.mkdir(parents=True, exist_ok=True)
    (agents_dir / f"{agent_type}.md").write_text(md_content, encoding="utf-8")


def test_template_to_descriptor_pipeline():
    """Full pipeline: YAML template → AgentTemplate → system prompt resolution."""
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp)
        _write_files(project, "main", "helper",
            "agent_type: helper\ndescription: Test helper\nmax_steps: 15\n",
            "You are a helpful assistant."
        )

        registry = AgentTemplateRegistry(project)
        templates = registry.list_templates("main")
        assert len(templates) == 1

        t = templates[0]
        assert t.agent_type == "helper"
        assert t.max_steps == 15

        # System prompt resolution
        md_path = project / "agents" / "main" / "helper.md"
        assert md_path.exists()
        assert md_path.read_text(encoding="utf-8") == "You are a helpful assistant."
```

- [ ] **Step 2: Run test**

Run: `pytest tests/unit/multi_agent/test_dynamic_subagent_integration.py -v`
Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add tests/unit/multi_agent/test_dynamic_subagent_integration.py
git commit -m "test: integration test for template loading pipeline"
```

---

### Task 19: Final verification

- [ ] **Step 1: Run full test suite**

Run: `pytest tests/unit/ -x -v --timeout=60`
Expected: all passing

- [ ] **Step 2: Run lint**

Run: `ruff check framework/ examples/bot_project/bot/`
Expected: pass

- [ ] **Step 3: Final commit if needed**

```bash
git add -A
git status
# commit any remaining cleanup
```
