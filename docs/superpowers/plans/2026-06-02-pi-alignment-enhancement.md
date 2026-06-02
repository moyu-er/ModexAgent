# Pi Alignment Enhancement — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task.
> Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Align the coding pool with pi-reference: fix fork context (system-prompt injection
+ two-stage truncation), dynamic parent, structured communication tool descriptions, oracle
role, web stubs, and progress tracking.

**Architecture:** Incremental changes to existing framework files (`communication.py`,
`template.py`, `tools.py`, `presets.py`) plus new web stubs and oracle config. Fork context
moves from deep-copy into subagent session messages to system-prompt injection via the
existing `MemorySystemContextManager.base_system_prompt` pipeline. Communication tools
gain dynamic descriptions based on caller `comm_kind`.

**Tech Stack:** Python 3.11+, pytest, asyncio, PyYAML, tree-sitter (existing).

---

### Task 1: SystemPromptMode enum + fork_max_messages in AgentTemplate

**Files:**
- Modify: `framework/tools/presets.py:36-41`
- Modify: `framework/multi_agent/template.py:14-51`

- [ ] **Step 1: Add SystemPromptMode enum to presets.py**

```python
# In framework/tools/presets.py, after ThinkingBudget class (line 41):

class SystemPromptMode(str, Enum):
    """System prompt assembly mode for subagent creation."""
    REPLACE = "replace"  # subagent uses its own complete prompt
    APPEND = "append"    # subagent prompt appended after parent's
```

- [ ] **Step 2: Update template.py imports**

```python
# In framework/multi_agent/template.py, line 10:
from framework.tools.presets import ContextMode, SystemPromptMode, ThinkingBudget, ToolPreset
```

- [ ] **Step 3: Add fields to AgentTemplate**

```python
# In framework/multi_agent/template.py, after line 47 (after progress_tracking):
    # ── system prompt control ──
    system_prompt_mode: SystemPromptMode = SystemPromptMode.REPLACE

    # ── fork context control ──
    fork_max_messages: int = 80  # only meaningful when context_mode == FORK
```

- [ ] **Step 4: Run existing tests to verify no breakage**

Run: `pytest tests/unit/multi_agent/test_template.py tests/unit/tools/test_presets.py -v`
Expected: All tests PASS

- [ ] **Step 5: Commit**

```bash
git add framework/tools/presets.py framework/multi_agent/template.py
git commit -m "feat: add SystemPromptMode enum and fork_max_messages to AgentTemplate"
```

---

### Task 2: template_registry parses new YAML fields

**Files:**
- Modify: `framework/multi_agent/template_registry.py:14,93-114`

- [ ] **Step 1: Update imports in template_registry.py**

```python
# Line 14 — add SystemPromptMode:
from framework.tools.presets import ContextMode, SystemPromptMode, ThinkingBudget, ToolPreset
```

- [ ] **Step 2: Parse system_prompt_mode and fork_max_messages in _load()**

```python
# After line 91 (thinking_budget parsing), add:

system_prompt_mode_raw = raw.get("system_prompt_mode", "replace")
try:
    system_prompt_mode = SystemPromptMode(system_prompt_mode_raw)
except ValueError:
    logger.warning(
        "Invalid system_prompt_mode '%s' in %s, falling back to 'replace'",
        system_prompt_mode_raw, yml_path,
    )
    system_prompt_mode = SystemPromptMode.REPLACE

fork_max_messages = raw.get("fork_max_messages", 80)
if not isinstance(fork_max_messages, int) or fork_max_messages < 1:
    fork_max_messages = 80
```

- [ ] **Step 3: Pass new fields to AgentTemplate constructor**

```python
# Add after the existing visible_targets= line (line 105):
                        system_prompt_mode=system_prompt_mode,
                        fork_max_messages=fork_max_messages,
```

- [ ] **Step 4: Run existing tests**

Run: `pytest tests/unit/multi_agent/test_template_registry.py -v`
Expected: All tests PASS

- [ ] **Step 5: Commit**

```bash
git add framework/multi_agent/template_registry.py
git commit -m "feat: parse system_prompt_mode and fork_max_messages in template_registry"
```

---

### Task 3: Dynamic communication tool descriptions (normal vs subagent)

**Files:**
- Modify: `framework/multi_agent/tools.py:63-251`

- [ ] **Step 1: Add NORMAL description builder for SendToAgentTool**

```python
# New module-level constant in tools.py, before SendToAgentTool class:

_NORMAL_SEND_DESCRIPTION = (
    "Dispatch a task to a subagent. Use this to delegate work.\n\n"
    "Parameters:\n"
    "  target_agent: Name of the subagent type or agent to dispatch to.\n"
    "  content: Complete task description with all necessary context.\n"
    "  invocation_id:\n"
    "    - null → Start a NEW task (framework generates a fresh session).\n"
    '    - "<id>" → CONTINUE an existing session using the invocation_id\n'
    "      returned from a previous dispatch. The subagent preserves its\n"
    "      memory and context.\n\n"
    "Dispatch patterns:\n"
    "  Sequential chain: Dispatch one subagent, wait for its result in\n"
    "    your next turn, then dispatch the next subagent passing relevant\n"
    "    context in the content field.\n"
    "  Parallel fan-out: Call this tool multiple times in the SAME turn\n"
    '    (recommended max 5 concurrent). Use invocation_id=null for each.\n'
    "    Results arrive in subsequent turns via inbox.\n\n"
    "Important: This tool does NOT wait for the subagent to finish.\n"
    "Results arrive in your inbox asynchronously."
)

_SUBAGENT_SEND_DESCRIPTION = (
    "Send a coordination message to your parent agent. Use ONLY for\n"
    "blocking decisions or important progress updates.\n\n"
    "Parameters:\n"
    "  target_agent: Your parent agent name (shown in list_communication_targets).\n"
    '  content: Structured message prefix:\n'
    '    "NEED_DECISION: <question>" — blocked, requires parent decision.\n'
    '    "PROGRESS_UPDATE: <info>" — non-blocking, important discovery.\n'
    "  invocation_id: Always use null for parent communication.\n\n"
    "Important:\n"
    "  - You can ONLY message your parent — not other subagents.\n"
    "  - Routine completion goes through your normal return — do NOT\n"
    "    use this tool for completion acknowledgements."
)
```

- [ ] **Step 2: Add NORMAL description builder for ListCommunicationTargetsTool**

```python
_NORMAL_LIST_DESCRIPTION = (
    "List all agents and subagent types available for dispatch.\n"
    "MUST be called BEFORE send_to_agent to verify target existence\n"
    "and invocation_id requirements."
)

_SUBAGENT_LIST_DESCRIPTION = (
    "List your parent agent for coordination messages.\n"
    "You can ONLY communicate with your parent — star topology."
)
```

- [ ] **Step 3: Update SendToAgentTool.get_dynamic_schema()**

```python
# Replace the existing get_dynamic_schema method (lines 103-111):

def get_dynamic_schema(self, caller_context: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return schema with description tailored to caller's comm_kind."""
    from framework.core.agent import current_agent_context
    ctx = current_agent_context.get(None)
    if ctx is not None and ctx.session_meta is not None and ctx.session_meta.comm_kind == AgentCommKind.SUBAGENT:
        desc = _SUBAGENT_SEND_DESCRIPTION
    else:
        desc = _build_dynamic_description(self._service, _NORMAL_SEND_DESCRIPTION)
    return {
        "type": "function",
        "function": {
            "name": self.name,
            "description": desc,
            "parameters": self.parameters,
        },
    }
```

- [ ] **Step 4: Add get_dynamic_schema() to ListCommunicationTargetsTool**

```python
# Add method to ListCommunicationTargetsTool class (after execute):

def get_dynamic_schema(self, caller_context: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return schema with description tailored to caller's comm_kind."""
    from framework.core.agent import current_agent_context
    ctx = current_agent_context.get(None)
    if ctx is not None and ctx.session_meta is not None and ctx.session_meta.comm_kind == AgentCommKind.SUBAGENT:
        desc = _SUBAGENT_LIST_DESCRIPTION
    else:
        desc = _NORMAL_LIST_DESCRIPTION
    return {
        "type": "function",
        "function": {
            "name": self.name,
            "description": desc,
            "parameters": self.parameters,
        },
    }
```

- [ ] **Step 5: Run existing tests to verify no breakage**

Run: `pytest tests/unit/multi_agent/ -v --timeout=30`
Expected: All tests PASS (tools still register and work; dynamic desc is called at schema-request time, not tested in unit tests)

- [ ] **Step 6: Commit**

```bash
git add framework/multi_agent/tools.py
git commit -m "feat: dynamic communication tool descriptions based on caller comm_kind"
```

---

### Task 4: Dynamic parent agent — remove hardcoding

**Files:**
- Modify: `framework/multi_agent/communication.py:102-125,447-451,459-522`

- [ ] **Step 1: Remove parent_memory_system from __init__**

```python
# In __init__, remove line 124:
        # parent_memory_system: MemorySystem | None = None,  ← REMOVED
# And remove line 134 (assignment):
        # self._parent_memory_system = parent_memory_system  ← REMOVED
```

- [ ] **Step 2: Compute parent_name from source in _create_dynamic_subagent**

```python
# After line 196 (name = template.agent_type), add:

        # ── Dynamic parent — the agent that dispatched this subagent ──
        parent_name = (source or self._source).name
```

- [ ] **Step 3: Thread parent_name into _wire_subagent_hooks**

```python
# Change _wire_subagent_hooks signature (line 430):
    def _wire_subagent_hooks(self, agent_name: str, parent_name: str) -> None:

# Change line 450:
                parent_name=parent_name,

# Update call site in _create_dynamic_subagent (around line 415):
        self._wire_subagent_hooks(name, parent_name=parent_name)
```

- [ ] **Step 4: Thread parent_name into _build_subagent_tool_manager**

```python
# Change signature (line 459):
    async def _build_subagent_tool_manager(
        self, template: AgentTemplate, agent_name: str,
        parent_name: str = "main",
    ):

# Change line 503-504:
        if visible is None:
            visible = [parent_name]

# Update call site in _create_dynamic_subagent (line 330):
        subagent_tm = await self._build_subagent_tool_manager(
            template, agent_name=name, parent_name=parent_name,
        )
```

- [ ] **Step 5: Remove parent_memory_system from pool_builder.py**

```python
# In examples/bot_project/bot/service/pool_builder.py, line 220:
        # parent_memory_system=memory_system,  ← REMOVE this line
```

- [ ] **Step 6: Run existing tests to verify no breakage**

Run: `pytest tests/unit/multi_agent/ -v --timeout=60`
Expected: All tests PASS

- [ ] **Step 7: Commit**

```bash
git add framework/multi_agent/communication.py examples/bot_project/bot/service/pool_builder.py
git commit -m "refactor: dynamic parent agent — remove hardcoded main_agent_name"
```

---

### Task 5: Fork context rewrite — system-prompt injection + two-stage truncation

**Files:**
- Modify: `framework/multi_agent/communication.py:209-327`

- [ ] **Step 1: Remove old fork deep-copy block (lines 236-327)**

Delete the entire `# ── Fork context: deep-copy parent session messages ...` block
(from line 236 through line 327). This includes:
- The `self._parent_memory_system is not None` check
- The `copy.deepcopy` message sanitization
- The `fork_marker` system message
- The `replace_messages` call

- [ ] **Step 2: Remove old fork preamble block (lines 209-218) and replace**

Delete lines 209-218 (the simple fork preamble string append). We'll replace
with the full two-stage truncation + XML formatting + persistence below.

- [ ] **Step 3: Add fork context helper — _build_fork_context_xml (new method)**

```python
# Add as a static/module-level function in communication.py, before AgentCommunicationService:

def _messages_to_xml(messages: list[Any], parent_name: str) -> str:
    """Convert a list of ChatMessage to an XML string for system-prompt injection."""
    lines = [
        f'<forked_context source="{parent_name}">',
        f"  <info>Inherited {len(messages)} messages from parent session.</info>",
    ]
    for i, msg in enumerate(messages):
        role = getattr(msg, "role", "unknown")
        content = getattr(msg, "content", "")
        if content is None:
            content = ""
        # Truncate per-message content for system-prompt sanity
        content_str = str(content)[:2000]
        name_attr = ""
        if role == "tool" and hasattr(msg, "name") and msg.name:
            name_attr = f' name="{msg.name}"'
        lines.append(f'  <message index="{i}" role="{role}"{name_attr}>')
        # CDATA to avoid XML escaping issues
        lines.append(f"    <![CDATA[{content_str}]]>")
        lines.append(f"  </message>")
    lines.append("</forked_context>")
    return "\n".join(lines)
```

- [ ] **Step 4: Add fork context lifecycle logic in _create_dynamic_subagent**

Replace the deleted block from Step 1+2 with:

```python
        # ── Fork context: two-stage truncation → XML → persist → system prompt ──
        from framework.tools.presets import ContextMode
        from framework.memory.core.scope import MemoryContext

        if template.context_mode == ContextMode.FORK and self._project_dir is not None:
            fork_workspace = (
                self._memory_dir
                or self._project_dir / "data" / "memory" / self._pool_name
                if self._pool_name
                else self._project_dir / "data" / "memory"
            )
            fork_file = (
                fork_workspace / "fork_contexts"
                / f"{name}_{invocation_id}.xml"
            )

            if fork_file.exists():
                # ── Resume: load persisted fork context ──
                fork_xml = fork_file.read_text(encoding="utf-8")
                logger.info(
                    "Fork context: loaded persisted file for %s/%s",
                    name, invocation_id,
                )
            else:
                # ── Initial creation: two-stage truncate + persist ──
                try:
                    parent_agent_name = parent_name
                    parent_session_id = self._session_strategy.format(
                        conversation_id=conversation_id,
                        agent_name=parent_agent_name,
                    )
                    parent_ctx = MemoryContext(session_id=parent_session_id)

                    # Read parent messages via abstract API
                    parent_messages = await subagent_ctx.memory_system.get_history(
                        parent_ctx, max_messages=10000,
                    )

                    if parent_messages:
                        # Stage 1: count-based truncation
                        truncated = parent_messages[-template.fork_max_messages:]

                        # Stage 2: lossy governance
                        if (
                            template.memory is not None
                            and template.memory.governance is not None
                            and template.memory.governance.lossy_compaction is not None
                        ):
                            from framework.memory.context_governance import (
                                CompositeGovernance,
                            )
                            governor = CompositeGovernance.from_config(
                                template.memory.governance
                            )
                            truncated, _stats = governor.apply(
                                [m.model_dump() if hasattr(m, "model_dump") else m
                                 for m in truncated]
                            )
                            # Re-wrap back to message objects if needed
                            from framework.memory.core.message import ChatMessage
                            truncated = [
                                ChatMessage(**m) if isinstance(m, dict) else m
                                for m in truncated
                            ]

                        # Format as XML
                        fork_xml = _messages_to_xml(truncated, parent_agent_name)
                    else:
                        fork_xml = (
                            f'<forked_context source="{parent_agent_name}">'
                            f"  <info>No parent messages available.</info>"
                            f"</forked_context>"
                        )

                    # Persist
                    fork_file.parent.mkdir(parents=True, exist_ok=True)
                    fork_file.write_text(fork_xml, encoding="utf-8")
                    logger.info(
                        "Fork context: persisted %d messages for %s/%s",
                        len(parent_messages) if parent_messages else 0,
                        name, invocation_id,
                    )
                except Exception:
                    logger.exception(
                        "Fork context: failed to build for %s, continuing with empty",
                        name,
                    )
                    fork_xml = (
                        f'<forked_context source="{parent_name}">'
                        f"  <info>Error building fork context — continuing empty.</info>"
                        f"</forked_context>"
                    )

            # ── Inject fork context into system prompt ──
            fork_preamble = (
                "\n\n---\n\n"
                "## Fork Context\n"
                f"You are a subagent running from a fork of agent '{parent_name}'.\n"
                "The context below is READ-ONLY reference. Do NOT continue the\n"
                "prior conversation. Your task starts now.\n\n"
                f"{fork_xml}"
            )
            system_prompt = system_prompt + fork_preamble
```

- [ ] **Step 5: Update subagent session creation — skip historical messages**

The `build_session_only_memory` call already creates an empty session (no
initial_messages). Ensure the fork XML is NOT passed as session messages:

```python
# The system_prompt variable now contains fork XML.
# build_session_only_memory receives it as system_prompt → base_system_prompt.
# subagent_ctx = build_session_only_memory(..., system_prompt=system_prompt)
# This line already exists and is unchanged.
```

- [ ] **Step 6: Run existing tests**

Run: `pytest tests/unit/multi_agent/ -v --timeout=60`
Expected: All tests PASS (fork behavior changes, but old fork tests that
expected deep-copy will need updating — see Task 11)

- [ ] **Step 7: Commit**

```bash
git add framework/multi_agent/communication.py
git commit -m "feat: fork context rewrite — system-prompt injection + two-stage truncation + persistence"
```

---

### Task 6: Remove old fork-related tests, update existing tests

**Files:**
- Modify: `tests/unit/multi_agent/test_pool_consumer_monitoring.py` (if any fork refs)
- Check: `tests/unit/multi_agent/test_pool_deadlock.py` (if fork refs)

- [ ] **Step 1: Search for old fork-related test code**

Run: `grep -rn "parent_memory_system\|fork.*context\|fork_marker\|fork.*preamble" tests/`
Identify any tests that reference the old fork implementation patterns.

- [ ] **Step 2: Remove or update fork tests**

If any tests explicitly test the old `_layers.session.replace_messages`
or `parent_memory_system` in fork context, remove those test cases — the
behavior has changed completely. Add a note in the test docstring that
fork context tests live in `test_fork_context.py` (created in Task 11).

- [ ] **Step 3: Run remaining unit tests to verify no breakage**

Run: `pytest tests/unit/multi_agent/ -v --timeout=60`
Expected: All tests PASS

- [ ] **Step 4: Commit**

```bash
git add tests/
git commit -m "test: remove old fork deep-copy tests — replaced by test_fork_context.py"
```

---

### Task 7: System prompt append mode in _create_dynamic_subagent

**Files:**
- Modify: `framework/multi_agent/communication.py:198-234`

- [ ] **Step 1: Add append-mode logic before build_session_only_memory**

Insert after the fork/md-prompt loading and before the `build_session_only_memory` call:

```python
        # ── Append mode: concat parent prompt before subagent prompt ──
        from framework.tools.presets import SystemPromptMode

        if template.system_prompt_mode == SystemPromptMode.APPEND:
            parent_prompt = ""
            parent_name_for_append = parent_name
            if self._pool is not None:
                parent_instance = self._pool.get(parent_name_for_append)
                if parent_instance is not None and parent_instance.descriptor.system_prompt_template:
                    parent_prompt = parent_instance.descriptor.system_prompt_template
            if parent_prompt:
                system_prompt = parent_prompt + "\n\n---\n\n" + system_prompt
```

- [ ] **Step 2: Run unit tests**

Run: `pytest tests/unit/multi_agent/ -v --timeout=60`
Expected: All tests PASS

- [ ] **Step 3: Commit**

```bash
git add framework/multi_agent/communication.py
git commit -m "feat: system prompt append mode for delegate subagents"
```

---

### Task 8: Progress tracking prompt injection

**Files:**
- Modify: `framework/multi_agent/communication.py:198-234` (same area as Task 7)

- [ ] **Step 1: Add progress tracking injection**

After the system prompt assembly and before `build_session_only_memory`:

```python
        # ── Progress tracking prompt ──
        if template.progress_tracking:
            progress_instruction = (
                "\n\n---\n\n"
                "## Progress Tracking\n"
                "Maintain a file called `progress.md` in the current working directory.\n"
                "Update it after each significant step with:\n"
                "- What was checked/done\n"
                "- What was found\n"
                "- What remains\n"
                "Keep it concise — this is a scratch file for coordination, not documentation."
            )
            system_prompt = system_prompt + progress_instruction
```

- [ ] **Step 2: Run unit tests**

Run: `pytest tests/unit/multi_agent/ -v --timeout=60`
Expected: All tests PASS

- [ ] **Step 3: Commit**

```bash
git add framework/multi_agent/communication.py
git commit -m "feat: progress tracking prompt injection for subagents"
```

---

### Task 9: WebSearch + WebReader stub tools

**Files:**
- Create: `framework/tools/web/__init__.py`
- Create: `framework/tools/web/search.py`
- Create: `framework/tools/web/reader.py`

- [ ] **Step 1: Create __init__.py**

```python
"""Web tools — search and page reader.

WebSearchTool and WebReaderTool are placeholder stubs. Full implementations
with httpx + DuckDuckGo / custom search API are deferred.
"""
```

- [ ] **Step 2: Create search.py**

```python
"""WebSearchTool — search the web. Stub — not yet implemented."""

from __future__ import annotations

from typing import Any

from framework.core.tool_manager import Tool, ToolConfig


class WebSearchTool(Tool):
    """Search the web for information. NOT YET IMPLEMENTED.

    This is a placeholder stub. Use alternative approaches:
    - Check project documentation and codebase.
    - Ask the user for clarification.
    - Use existing MCP tools if configured.
    """

    def __init__(self) -> None:
        super().__init__(
            name="web_search",
            description=(
                "Search the web for information. NOT YET IMPLEMENTED — "
                "use alternative approaches (codebase search, user clarification)."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query.",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Maximum number of results (default 5).",
                        "default": 5,
                    },
                },
                "required": ["query"],
            },
            config=ToolConfig(),
        )

    async def execute(self, **kwargs: Any) -> str:
        query = kwargs.get("query", "")
        return (
            f"[web_search] Not yet implemented.\n\n"
            f"Query: {query}\n\n"
            f"Alternative approaches:\n"
            f"1. Search the codebase: use grep to find relevant files.\n"
            f"2. Check project docs in docs/ directory.\n"
            f"3. Ask the user for clarification.\n"
            f"4. Use MCP tools if configured for web access."
        )
```

- [ ] **Step 3: Create reader.py**

```python
"""WebReaderTool — fetch and read URL content. Stub — not yet implemented."""

from __future__ import annotations

from typing import Any

from framework.core.tool_manager import Tool, ToolConfig


class WebReaderTool(Tool):
    """Fetch and read content from a URL. NOT YET IMPLEMENTED.

    This is a placeholder stub. Full implementation will support:
    - HTML → markdown conversion via markdownify
    - Configurable timeout
    - Response caching
    """

    def __init__(self) -> None:
        super().__init__(
            name="web_reader",
            description=(
                "Fetch and read content from a URL. NOT YET IMPLEMENTED."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "URL to fetch and read.",
                    },
                    "format": {
                        "type": "string",
                        "enum": ["markdown", "text"],
                        "description": "Output format (default: markdown).",
                        "default": "markdown",
                    },
                },
                "required": ["url"],
            },
            config=ToolConfig(),
        )

    async def execute(self, **kwargs: Any) -> str:
        url = kwargs.get("url", "")
        return (
            f"[web_reader] Not yet implemented.\n\n"
            f"URL: {url}\n\n"
            f"Use alternative approaches or ask the user for the content."
        )
```

- [ ] **Step 4: Run stub tool tests (write them inline)**

```bash
# Quick smoke test — register tools and verify name + stub response
python -c "
from framework.tools.web.search import WebSearchTool
from framework.tools.web.reader import WebReaderTool
import asyncio

async def main():
    ws = WebSearchTool()
    assert ws.name == 'web_search'
    result = await ws.execute(query='test')
    assert 'Not yet implemented' in result
    print('WebSearchTool: OK')

    wr = WebReaderTool()
    assert wr.name == 'web_reader'
    result = await wr.execute(url='https://example.com')
    assert 'Not yet implemented' in result
    print('WebReaderTool: OK')

asyncio.run(main())
"
```
Expected: WebSearchTool: OK, WebReaderTool: OK

- [ ] **Step 5: Commit**

```bash
git add framework/tools/web/
git commit -m "feat: add WebSearchTool and WebReaderTool placeholder stubs"
```

---

### Task 10: Write unit tests for web stubs

**Files:**
- Create: `tests/unit/tools/web/__init__.py`
- Create: `tests/unit/tools/web/test_search.py`
- Create: `tests/unit/tools/web/test_reader.py`

- [ ] **Step 1: Create test package init**

```python
# tests/unit/tools/web/__init__.py (empty)
```

- [ ] **Step 2: Create test_search.py**

```python
"""Tests for WebSearchTool stub."""

from __future__ import annotations

import pytest

from framework.tools.web.search import WebSearchTool


class TestWebSearchTool:
    def test_name_is_web_search(self) -> None:
        tool = WebSearchTool()
        assert tool.name == "web_search"

    def test_description_is_not_empty(self) -> None:
        tool = WebSearchTool()
        assert tool.description

    def test_parameters_has_query_required(self) -> None:
        tool = WebSearchTool()
        assert "query" in tool.parameters["required"]

    @pytest.mark.asyncio
    async def test_execute_returns_not_implemented(self) -> None:
        tool = WebSearchTool()
        result = await tool.execute(query="test query")
        assert "Not yet implemented" in result
        assert "test query" in result

    @pytest.mark.asyncio
    async def test_execute_without_query(self) -> None:
        tool = WebSearchTool()
        result = await tool.execute()
        assert "Not yet implemented" in result
```

- [ ] **Step 3: Create test_reader.py**

```python
"""Tests for WebReaderTool stub."""

from __future__ import annotations

import pytest

from framework.tools.web.reader import WebReaderTool


class TestWebReaderTool:
    def test_name_is_web_reader(self) -> None:
        tool = WebReaderTool()
        assert tool.name == "web_reader"

    def test_description_is_not_empty(self) -> None:
        tool = WebReaderTool()
        assert tool.description

    def test_parameters_has_url_required(self) -> None:
        tool = WebReaderTool()
        assert "url" in tool.parameters["required"]

    @pytest.mark.asyncio
    async def test_execute_returns_not_implemented(self) -> None:
        tool = WebReaderTool()
        result = await tool.execute(url="https://example.com")
        assert "Not yet implemented" in result
        assert "https://example.com" in result
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/unit/tools/web/ -v`
Expected: 7 tests PASS

- [ ] **Step 5: Commit**

```bash
git add tests/unit/tools/web/
git commit -m "test: unit tests for WebSearchTool and WebReaderTool stubs"
```

---

### Task 11: Write fork context unit tests

**Files:**
- Create: `tests/unit/multi_agent/test_fork_context.py`

- [ ] **Step 1: Create test_fork_context.py**

```python
"""Tests for fork context lifecycle — system-prompt injection with persistence."""

from __future__ import annotations

import tempfile
from pathlib import Path

from framework.tools.presets import ContextMode, SystemPromptMode, ToolPreset


class TestForkContextPersistence:
    """Fork context file lifecycle tests."""

    def test_fork_file_path_naming(self) -> None:
        """Fork file uses {agent_name}_{invocation_id}.xml naming."""
        agent_name = "planner"
        invocation_id = "abc12345"
        workspace = Path("/tmp/test_fork")

        fork_file = workspace / "fork_contexts" / f"{agent_name}_{invocation_id}.xml"
        expected = Path("/tmp/test_fork/fork_contexts/planner_abc12345.xml")
        assert fork_file == expected

    def test_fork_file_resume_detection(self, tmp_path: Path) -> None:
        """Resume skips re-truncation when fork file exists."""
        fork_dir = tmp_path / "fork_contexts"
        fork_dir.mkdir(parents=True)
        fork_file = fork_dir / "planner_abc123.xml"
        fork_file.write_text("<forked_context>test</forked_context>", encoding="utf-8")

        # Simulate resume check
        assert fork_file.exists() is True
        loaded = fork_file.read_text(encoding="utf-8")
        assert "test" in loaded


class TestForkContextXMLFormat:
    """Fork context XML structure tests."""

    def test_xml_contains_source_attribute(self) -> None:
        """XML root has source attribute with parent name."""
        xml = '<forked_context source="coding"><info>5 messages</info></forked_context>'
        assert 'source="coding"' in xml

    def test_xml_contains_info_element(self) -> None:
        """XML has info element with message count."""
        xml = '<forked_context source="main"><info>Inherited 3 messages</info></forked_context>'
        assert "<info>" in xml

    def test_xml_message_element_structure(self) -> None:
        """Each message has index, role, and CDATA content."""
        xml = (
            '<forked_context source="main">\n'
            '  <info>Inherited 1 messages</info>\n'
            '  <message index="0" role="user">\n'
            "    <![CDATA[Hello]]>\n"
            "  </message>\n"
            "</forked_context>"
        )
        assert 'index="0"' in xml
        assert 'role="user"' in xml
        assert "CDATA" in xml


class TestForkContextTemplateFields:
    """Template fork fields are parsed correctly."""

    def test_fork_max_messages_default(self) -> None:
        from framework.multi_agent.template import AgentTemplate

        t = AgentTemplate(agent_type="test", context_mode=ContextMode.FORK)
        assert t.fork_max_messages == 80

    def test_fork_max_messages_custom(self) -> None:
        from framework.multi_agent.template import AgentTemplate

        t = AgentTemplate(
            agent_type="test",
            context_mode=ContextMode.FORK,
            fork_max_messages=50,
        )
        assert t.fork_max_messages == 50

    def test_system_prompt_mode_replaced_for_oracle(self) -> None:
        from framework.multi_agent.template import AgentTemplate

        t = AgentTemplate(
            agent_type="oracle",
            context_mode=ContextMode.FORK,
            system_prompt_mode=SystemPromptMode.REPLACE,
        )
        assert t.system_prompt_mode == SystemPromptMode.REPLACE
```

- [ ] **Step 2: Run tests**

Run: `pytest tests/unit/multi_agent/test_fork_context.py -v`
Expected: 6 tests PASS

- [ ] **Step 3: Commit**

```bash
git add tests/unit/multi_agent/test_fork_context.py
git commit -m "test: fork context lifecycle, XML format, and template field tests"
```

---

### Task 12: Update test_template.py for system_prompt_mode

**Files:**
- Modify: `tests/unit/multi_agent/test_template.py`

- [ ] **Step 1: Add test for system_prompt_mode**

```python
# Add to tests/unit/multi_agent/test_template.py:

def test_agent_template_system_prompt_mode_default():
    from framework.tools.presets import SystemPromptMode
    t = AgentTemplate(agent_type="test")
    assert t.system_prompt_mode == SystemPromptMode.REPLACE


def test_agent_template_fork_max_messages_default():
    t = AgentTemplate(agent_type="test")
    assert t.fork_max_messages == 80


def test_agent_template_system_prompt_mode_append():
    from framework.tools.presets import SystemPromptMode
    t = AgentTemplate(agent_type="delegate", system_prompt_mode=SystemPromptMode.APPEND)
    assert t.system_prompt_mode == SystemPromptMode.APPEND
```

- [ ] **Step 2: Run tests**

Run: `pytest tests/unit/multi_agent/test_template.py -v`
Expected: All tests PASS (existing + 3 new)

- [ ] **Step 3: Commit**

```bash
git add tests/unit/multi_agent/test_template.py
git commit -m "test: system_prompt_mode and fork_max_messages template tests"
```

---

### Task 13: Bot layer — template YAML updates

**Files:**
- Modify: `examples/bot_project/config/pools/coding/templates/planner.yml`
- Modify: `examples/bot_project/config/pools/coding/templates/worker.yml`
- Modify: `examples/bot_project/config/pools/coding/templates/delegate.yml`
- Create: `examples/bot_project/config/pools/coding/templates/oracle.yml`

- [ ] **Step 1: Update planner.yml — add fork_max_messages + memory**

```yaml
# examples/bot_project/config/pools/coding/templates/planner.yml
agent_type: planner
description: "Creates implementation plans from context and requirements"
tool_preset: minimal
context_mode: fork
thinking_budget: high
max_steps: 80
system_prompt_mode: replace
default_reads:
  - context.md
fork_max_messages: 80
memory:
  session:
    max_messages: 200
    max_tokens: 200000
    keep_ratio_for_messages: 0.4
    keep_ratio_for_token: 0.4
  governance:
    tool_chain_repair: true
```

- [ ] **Step 2: Update worker.yml — add fork_max_messages + memory + progress**

```yaml
# examples/bot_project/config/pools/coding/templates/worker.yml
agent_type: worker
description: "Implementation agent for approved plans and tasks"
tool_preset: full
context_mode: fork
thinking_budget: high
max_steps: 150
system_prompt_mode: replace
progress_tracking: true
use_terminal: true
fork_max_messages: 80
memory:
  session:
    max_messages: 200
    max_tokens: 200000
    keep_ratio_for_messages: 0.4
    keep_ratio_for_token: 0.4
  governance:
    tool_chain_repair: true
```

- [ ] **Step 3: Update delegate.yml — add system_prompt_mode append**

```yaml
# examples/bot_project/config/pools/coding/templates/delegate.yml
agent_type: delegate
description: "Lightweight subagent — inherits parent prompt, no default reads"
tool_preset: full
context_mode: fresh
thinking_budget: medium
max_steps: 50
system_prompt_mode: append
```

- [ ] **Step 4: Create oracle.yml**

```yaml
# examples/bot_project/config/pools/coding/templates/oracle.yml
agent_type: oracle
description: "Decision-consistency oracle — prevents drift from inherited decisions"
tool_preset: read_only
context_mode: fork
thinking_budget: high
max_steps: 60
system_prompt_mode: replace
fork_max_messages: 80
memory:
  session:
    max_messages: 200
    max_tokens: 200000
    keep_ratio_for_messages: 0.4
    keep_ratio_for_token: 0.4
  governance:
    tool_chain_repair: true
```

- [ ] **Step 5: Commit**

```bash
git add examples/bot_project/config/pools/coding/templates/
git commit -m "feat: update subagent templates — fork_max_messages, memory, append mode, oracle"
```

---

### Task 14: Bot layer — coding.yml config update

**Files:**
- Modify: `examples/bot_project/config/pools/coding.yml`

- [ ] **Step 1: Add oracle to agent summary table comment**

Update the comment section of coding.yml. Add oracle row to the table:

```yaml
#   oracle          | read_only  | fork    | high   | 60    | -        |
```

- [ ] **Step 2: Verify coding.yml loads correctly**

```bash
# Read the current file to check structure
python -c "
import yaml
with open('examples/bot_project/config/pools/coding.yml', encoding='utf-8') as f:
    cfg = yaml.safe_load(f)
print('Agents:', [a['name'] for a in cfg.get('agents', [])])
print('Templates: OK (loaded from coding/templates/)')
"
```
Expected: Lists agents including coding

- [ ] **Step 3: Commit**

```bash
git add examples/bot_project/config/pools/coding.yml
git commit -m "docs: update coding.yml agent summary table with oracle row"
```

---

### Task 15: Bot layer — agent prompt updates

**Files:**
- Modify: `examples/bot_project/agents/coding.md`
- Modify: `examples/bot_project/agents/scout.md`
- Modify: `examples/bot_project/agents/context-builder.md`
- Modify: `examples/bot_project/agents/planner.md`
- Modify: `examples/bot_project/agents/worker.md`
- Modify: `examples/bot_project/agents/reviewer.md`
- Modify: `examples/bot_project/agents/delegate.md`
- Create: `examples/bot_project/agents/oracle.md`

- [ ] **Step 1: Update coding.md — add dispatch patterns section**

Append to the existing coding.md:

```markdown

## Multi-Agent Dispatch Patterns

### Subagent Dispatch
Use `send_to_agent` to delegate work to subagents. Always call
`list_communication_targets` first to see available subagent types
and their invocation_id requirements.

### invocation_id Semantics
- `invocation_id: null` → Start a NEW task (fresh subagent session).
- `invocation_id: "<id>"` → CONTINUE an existing subagent session
  (preserves its memory and context).

### Sequential Chain
Dispatch one subagent, wait for its result in your next turn,
then dispatch the next:

```
Turn N:   send_to_agent(target_agent="scout", content="explore X",
           invocation_id=null)
Turn N+1: inbox receives scout result. Read context.md if produced.
          send_to_agent(target_agent="planner", content="plan from context",
           invocation_id=null)
Turn N+2: inbox receives planner result.
          send_to_agent(target_agent="worker", content="implement plan",
           invocation_id=null)
```

### Parallel Fan-Out
Call send_to_agent multiple times in the SAME turn (max 5 concurrent).
Each call uses invocation_id=null for independent subagents:

```
Turn N:   send_to_agent(target_agent="reviewer", content="review file A",
           invocation_id=null)
          send_to_agent(target_agent="reviewer", content="review file B",
           invocation_id=null)
          send_to_agent(target_agent="reviewer", content="review file C",
           invocation_id=null)
```

### Subagent Coordination Messages
Subagents use NEED_DECISION / PROGRESS_UPDATE prefixes in content:
- `NEED_DECISION: <question>` — requires your decision. Reply promptly.
- `PROGRESS_UPDATE: <info>` — informational, no reply needed.
```

- [ ] **Step 2: Update scout.md — communication rules**

Append to the existing scout.md:

```markdown

## Communication Rules

When you need a decision from your parent agent:
```
send_to_agent(target_agent=<from list_communication_targets>,
  content="NEED_DECISION: <your question>",
  invocation_id=null)
```

For important progress updates:
```
send_to_agent(target_agent=<parent>,
  content="PROGRESS_UPDATE: <what changed>",
  invocation_id=null)
```

Always call `list_communication_targets` to discover your parent agent
before using `send_to_agent`.

## Progress Tracking

You are running with progress tracking enabled. Maintain a file called
`progress.md` in the current working directory. Update it after each
significant step.
```

- [ ] **Step 3: Update context-builder.md — communication rules**

Same communication rules block as scout.md (NEED_DECISION / PROGRESS_UPDATE
prefixes). Also add conditional web_search note:

```markdown

## Web Research

Use `web_search` if it is available. Otherwise use alternative approaches
(system codebase search, project documentation, user clarification).

## Communication Rules

[same as scout.md communication rules]
```

- [ ] **Step 4: Update planner.md — communication rules**

Same communication rules block as scout.md.

- [ ] **Step 5: Update worker.md — communication rules + progress tracking**

Same communication rules block. Add progress tracking section (same as
scout.md). Add output format section:

```markdown

## Output Format

Your final response should follow this shape:
- Implemented: what was done
- Changed files: list of files modified
- Validation: how changes were verified
- Open risks/questions: anything unresolved
- Recommended next step
```

- [ ] **Step 6: Update reviewer.md — communication rules**

Same communication rules block. Add review output format:

```markdown

## Review Output Format

- Correct: what is already good (with evidence)
- Fixed: issue, location, and resolution (if you applied a fix)
- Blocker: critical issue that must be resolved before proceeding
- Note: observation, risk, or follow-up item
```

- [ ] **Step 7: Update delegate.md — concise append-mode prompt**

Since delegate uses append mode, it's a short supplement to parent prompt:

```markdown
You are a delegated agent. Execute the assigned task using the provided
tools. Be direct, efficient, and keep the response focused on the requested
work.

## Communication Rules

When blocked or needing a decision:
```
send_to_agent(target_agent=<parent>,
  content="NEED_DECISION: <question>",
  invocation_id=null)
```

For important progress:
```
send_to_agent(target_agent=<parent>,
  content="PROGRESS_UPDATE: <what changed>",
  invocation_id=null)
```
```

- [ ] **Step 8: Create oracle.md**

```markdown
You are the oracle: a high-context decision-consistency subagent.

Your primary job is to prevent the main agent from making hidden,
conflicting, or inconsistent decisions by treating the inherited
forked context as the authoritative contract. You are not the
primary executor. You do not silently become a second decision-maker.

Before you do anything else, reconstruct the key inherited decisions,
constraints, and open questions from the forked context and task.
Those decisions form your baseline contract. Preserve them unless
there is strong evidence they should be overturned.

Core responsibilities:
- Reconstruct inherited decisions, constraints, and open questions
- Identify drift between the current trajectory and inherited decisions
- Surface contradictions and hidden assumptions
- Call out when a proposed move conflicts with an earlier decision
- Protect consistency over novelty
- Exploit your clean forked context to spot things the main agent may
  have missed due to context rot

What you do NOT do:
- Do not edit files or write code
- Do not propose additional subagent trees unless explicitly asked
- Do not assume a worker handoff is the default outcome
- Do not continue the user conversation directly

Working rules:
- Use bash only for inspection, verification, or read-only analysis.
- If information is missing and it matters, ask the main agent.
- Prefer narrow, specific corrections over rewriting the whole plan.

## Communication Rules
[use same NEED_DECISION / PROGRESS_UPDATE pattern as other subagents]

## Output Format

Inherited decisions:
- the key decisions, constraints, and assumptions already in play

Diagnosis:
- what is actually going on
- what the main agent may be missing

Drift / contradiction check:
- where the current trajectory conflicts with inherited decisions
- what assumptions have quietly changed

Recommendation:
- the best next move and why
- if recommending a pivot, which inherited decision is being revised

Risks:
- what could still go wrong
- what assumptions remain uncertain

Need from main agent:
- specific question or decision required before continuing, if any
```

- [ ] **Step 9: Commit**

```bash
git add examples/bot_project/agents/
git commit -m "feat: update all agent prompts — structured comm rules, dispatch patterns, oracle"
```

---

### Task 16: Final integration verification

**Files:**
- No file changes — verification only

- [ ] **Step 1: Run full unit test suite**

Run: `pytest tests/unit/ -v --timeout=120 -x`
Expected: All tests PASS (no regressions)

- [ ] **Step 2: Verify template loading**

```bash
python -c "
from pathlib import Path
from framework.multi_agent.template_registry import AgentTemplateRegistry
from framework.tools.presets import ContextMode, SystemPromptMode, ToolPreset

registry = AgentTemplateRegistry(Path('examples/bot_project'))
templates = registry.list_templates('coding')
names = sorted([t.agent_type for t in templates])
print('Templates:', names)
assert 'oracle' in names, 'oracle template missing'
assert 'planner' in names
assert 'worker' in names
assert 'delegate' in names

# Verify oracle config
oracle = registry.get_template('coding', 'oracle')
assert oracle is not None
assert oracle.tool_preset == ToolPreset.READ_ONLY
assert oracle.context_mode == ContextMode.FORK
assert oracle.system_prompt_mode == SystemPromptMode.REPLACE
assert oracle.fork_max_messages == 80

# Verify delegate append mode
delegate = registry.get_template('coding', 'delegate')
assert delegate is not None
assert delegate.system_prompt_mode == SystemPromptMode.APPEND

print('All template assertions passed')
"
```
Expected: Templates: [...] with oracle, All template assertions passed

- [ ] **Step 3: Verify tool imports**

```bash
python -c "
from framework.tools.web.search import WebSearchTool
from framework.tools.web.reader import WebReaderTool
from framework.tools.presets import SystemPromptMode

ws = WebSearchTool()
assert ws.name == 'web_search'
wr = WebReaderTool()
assert wr.name == 'web_reader'

# Verify new enum
assert SystemPromptMode.REPLACE == 'replace'
assert SystemPromptMode.APPEND == 'append'
print('All import checks passed')
"
```
Expected: All import checks passed

- [ ] **Step 4: Commit (if any fixes applied)**

```bash
git add -u
git commit -m "chore: integration verification — all tests pass"
```

---

## Task Dependency Graph

```
Task 1 (enum + template fields)
  ├─> Task 2 (template_registry parsing)
  │     └─> Task 13 (template YAMLs)
  │           └─> Task 14 (coding.yml)
  │                 └─> Task 16 (verification)
  │
  ├─> Task 3 (dynamic descriptions)
  │     └─> Task 16
  │
  ├─> Task 4 (dynamic parent)
  │     └─> Task 5 (fork rewrite)
  │           ├─> Task 6 (old test cleanup)
  │           ├─> Task 7 (append mode)
  │           │     └─> Task 16
  │           ├─> Task 8 (progress tracking)
  │           │     └─> Task 16
  │           └─> Task 11 (fork tests)
  │                 └─> Task 16
  │
  └─> Task 9 (web stubs)
        └─> Task 10 (web tests)
              └─> Task 16

Task 12 (template tests) — independent, parallel
Task 15 (agent prompts) — independent after design confirmed, parallel
Task 16 (verification) — final, serial
```

**Parallel groups:**
- Group A: Task 1 → group barrier
- Group B: Task 2, Task 3, Task 12 (all read from template, no writes to same files)
- Group C: Task 4 → Task 5 → Task 6, Task 7, Task 8, Task 11 (serial chain on communication.py)
- Group D: Task 9 → Task 10 (web stubs, independent)
- Group E: Task 13 → Task 14 (bot configs, depends on Task 2 for field names)
- Group F: Task 15 (agent prompts, independent of everything)
- Group G: Task 16 (verification, after all)

Groups B, D, F can run concurrently. Group C (communication.py chain) must be serial.
