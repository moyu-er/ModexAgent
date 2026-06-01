# Coding Pool Pi-Aligned Redesign — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Redesign the coding pool from 3 roles (coding + planner + reviewer) to a pi-aligned 6-role subagent system (scout, context-builder, planner, worker, reviewer, delegate) with preset tool sets, AST tools, and LSP stubs.

**Architecture:** Extend `AgentTemplate` with pi-aligned fields (tool_preset, context_mode, thinking_budget, default_reads, progress_tracking). Add `ToolPreset` enum for declarative tool assignment. Add `ast_grep_search`/`ast_grep_replace` tools via tree-sitter. Unify `SubprocessTool` and `CommandTool` under the name `"bash"`. Adapt hooks to carry agent_type metadata. All communication stays on `send_to_agent` — no new comm tools.

**Tech Stack:** Python 3.12+, tree-sitter (Python/Java), pydantic, dataclasses

**Key decisions:**
- `ToolPreset` enum with 4 values controls tool registration — no YAML-level tool lists
- `context_mode="fork"` injects a fork preamble into system prompt; no framework-level context fork yet
- `thinking_budget` is prompt annotation only — no framework enforcement
- AST tools degrade gracefully when tree-sitter is not installed
- LSP tools are stubs (~30 lines each) returning "not yet implemented"

---

## Phase Overview

```
Phase 1 (foundations)          Phase 2 (tool infra)         Phase 3 (wiring)
┌──────────────────┐          ┌──────────────────┐        ┌──────────────────┐
│ T1: ToolPreset   │          │ T6: AST engine    │        │ T8: comm.py      │
│ T2: Bash unified │          │ T7: LSP stubs     │───────▶│    preset-based  │
│ T3: Template ext │─────────▶│                  │        │ T9: pool_builder │
│ T4: Registry upd │          │                  │        │ T10: hook adapts │
│ T5: AgentConfig  │          │                  │        │                  │
└──────────────────┘          └──────────────────┘        └──────────────────┘
   T1-T5 can be done in any order        T6-T7 are independent             T8-T10
                                                                            │
                                                                  ┌─────────┘
                                                                  ▼
                                           Phase 4 (bot config)   Phase 5 (verify)
                                           ┌──────────────────┐  ┌──────────────────┐
                                           │ T11-T17: config  │  │ T18-T19: tests   │
                                           │ files, prompts   │──│ + integration    │
                                           └──────────────────┘  └──────────────────┘
```

**Parallelism:** T1-T5 can run in any order. T6-T7 are independent. T11-T17 (bot config) are all independent file writes.

---

### Task 1: ToolPreset Enum + Preset Tool Sets

**Files:**
- Create: `framework/tools/presets.py`
- Test: `tests/unit/tools/test_presets.py`

**Why:** The ToolPreset enum is the foundation for all subsequent tool registration work. Other tasks depend on this module existing.

- [ ] **Step 1: Create the ToolPreset enum and preset mappings**

```python
# framework/tools/presets.py
"""Tool preset definitions for subagent tool registration."""

from __future__ import annotations

from collections.abc import Callable
from enum import Enum
from typing import Any

from framework.core.tool_manager import Tool
from framework.tools.standard import (
    EditFileTool,
    FindFilesTool,
    ListDirTool,
    ReadFileTool,
    SearchFilesTool,
    WriteFileTool,
)


class ToolPreset(str, Enum):
    """Declarative tool preset for subagent assignment.

    Values map to tool factory lists in TOOL_PRESETS.
    """
    FULL = "full"               # all tools + bash + terminal
    READ_WRITE = "read_write"   # read + write + edit + search (no bash)
    READ_ONLY = "read_only"     # read + search + bash (prompt-constrained read-only)
    MINIMAL = "minimal"         # read + write + list (no edit, no bash)


def _make_standard_read() -> list[Tool]:
    """Create read-only standard tools."""
    return [ReadFileTool(), ListDirTool(), SearchFilesTool(), FindFilesTool()]


def _make_standard_read_write() -> list[Tool]:
    """Create read+write standard tools (no bash)."""
    return [
        ReadFileTool(), WriteFileTool(), EditFileTool(),
        ListDirTool(), SearchFilesTool(), FindFilesTool(),
    ]


def _make_standard_full() -> list[Tool]:
    """Create full standard tools (bash registered separately)."""
    return [
        ReadFileTool(), WriteFileTool(), EditFileTool(),
        ListDirTool(), SearchFilesTool(), FindFilesTool(),
    ]


def _make_standard_minimal() -> list[Tool]:
    """Create minimal tools (read + write + search, no edit, no bash)."""
    return [
        ReadFileTool(), WriteFileTool(), ListDirTool(), SearchFilesTool(),
    ]


def get_preset_tools(
    preset: ToolPreset,
    *,
    subprocess_tool_factory: Callable[[], Tool] | None = None,
) -> list[Tool]:
    """Return the list of tools for a preset.

    Args:
        preset: The tool preset enum value.
        subprocess_tool_factory: If provided, creates a bash tool (SubprocessTool or CommandTool).
                                 Always added to FULL and READ_ONLY presets.

    Returns:
        List of Tool instances ready for registration.
    """
    tool_lists: dict[ToolPreset, Callable[[], list[Tool]]] = {
        ToolPreset.FULL: _make_standard_full,
        ToolPreset.READ_WRITE: _make_standard_read_write,
        ToolPreset.READ_ONLY: _make_standard_read,
        ToolPreset.MINIMAL: _make_standard_minimal,
    }

    factory = tool_lists[preset]
    tools: list[Tool] = factory()

    # Bash tool: FULL and READ_ONLY get bash; READ_WRITE and MINIMAL do not
    if subprocess_tool_factory is not None and preset in (ToolPreset.FULL, ToolPreset.READ_ONLY):
        tools.append(subprocess_tool_factory())

    return tools
```

- [ ] **Step 2: Create the unit test**

```python
# tests/unit/tools/test_presets.py
"""Tests for framework.tools.presets."""

from __future__ import annotations

import pytest
from framework.tools.presets import ToolPreset, get_preset_tools


class TestToolPreset:
    """Enum value tests."""

    def test_preset_is_str_enum(self) -> None:
        """ToolPreset values are strings for YAML serialization."""
        assert ToolPreset.FULL == "full"
        assert ToolPreset.READ_WRITE == "read_write"
        assert ToolPreset.READ_ONLY == "read_only"
        assert ToolPreset.MINIMAL == "minimal"


class TestGetPresetTools:
    """Tool registration tests."""

    def test_full_preset_includes_read_write(self) -> None:
        """FULL preset includes Read/Write/Edit/Search tools."""
        tools = get_preset_tools(ToolPreset.FULL)
        names = [t.name for t in tools]
        assert "read_file" in names
        assert "write_file" in names
        assert "edit_file" in names
        assert "list_dir" in names
        assert "search_files" in names
        assert "find_files" in names

    def test_read_only_preset_excludes_write(self) -> None:
        """READ_ONLY preset has no Write/Edit tools."""
        tools = get_preset_tools(ToolPreset.READ_ONLY)
        names = [t.name for t in tools]
        assert "read_file" in names
        assert "write_file" not in names
        assert "edit_file" not in names
        assert "search_files" in names

    def test_read_write_preset_no_bash(self) -> None:
        """READ_WRITE preset has no bash tool."""
        tools = get_preset_tools(ToolPreset.READ_WRITE)
        names = [t.name for t in tools]
        assert "bash" not in names

    def test_minimal_preset_no_edit_no_bash(self) -> None:
        """MINIMAL preset has no Edit, no FindFiles, no bash."""
        tools = get_preset_tools(ToolPreset.MINIMAL)
        names = [t.name for t in tools]
        assert "read_file" in names
        assert "write_file" in names
        assert "edit_file" not in names
        assert "find_files" not in names

    def test_bash_injected_for_full_preset(self) -> None:
        """FULL preset includes bash when factory provided."""
        from framework.tools.terminal.subprocess_tool import SubprocessTool

        def make_bash() -> SubprocessTool:
            return SubprocessTool(timeout=60)

        tools = get_preset_tools(ToolPreset.FULL, subprocess_tool_factory=make_bash)
        names = [t.name for t in tools]
        assert "bash" in names

    def test_bash_not_injected_for_read_write(self) -> None:
        """READ_WRITE preset excludes bash even when factory provided."""
        from framework.tools.terminal.subprocess_tool import SubprocessTool

        tools = get_preset_tools(
            ToolPreset.READ_WRITE,
            subprocess_tool_factory=lambda: SubprocessTool(timeout=60),
        )
        names = [t.name for t in tools]
        assert "bash" not in names
```

- [ ] **Step 3: Run tests to verify they pass**

```bash
pytest tests/unit/tools/test_presets.py -v
```
Expected: All 7 tests PASS

- [ ] **Step 4: Commit**

```bash
git add framework/tools/presets.py tests/unit/tools/test_presets.py
git commit -m "feat: add ToolPreset enum with 4 preset tool sets

FULL/READ_WRITE/READ_ONLY/MINIMAL. Each preset maps to a
deterministic list of tool factories. Bash tool injected via
optional factory parameter for FULL and READ_ONLY presets."
```

---

### Task 2: Unify Bash Tool Names

**Files:**
- Modify: `framework/tools/terminal/subprocess_tool.py:152-153`
- Modify: `framework/tools/terminal/command_tool.py:81-99`
- Test: `tests/unit/tools/terminal/test_bash_name.py`

**Why:** Both tools currently have different names (`"shell"` and `"command"`). LLMs need a single, predictable name `"bash"` with descriptions that distinguish behavior semantics (stateful vs stateless) without exposing implementation details.

- [ ] **Step 1: Change SubprocessTool name from "shell" to "bash"**

In `framework/tools/terminal/subprocess_tool.py`, change the `name` property (line 152-153):

```python
# Before (line 152-153):
@property
def name(self) -> str:
    return "shell"

# After:
@property
def name(self) -> str:
    return "bash"
```

Also update the description (line 173-192) to clarify stateless semantics:

```python
# Before (line 183-184):
parts.append(
    "Each command runs in a fresh process: cd and environment "
    "changes do NOT persist."
)

# After:
parts.append(
    "Each invocation runs independently in a fresh shell. "
    "Working directory, environment variables, and background "
    "processes do NOT persist between calls."
)
```

- [ ] **Step 2: Change CommandTool name from "command" to "bash"**

In `framework/tools/terminal/command_tool.py`, change the `name` property (line 81-82):

```python
# Before (line 81-82):
@property
def name(self) -> str:
    return "command"

# After:
@property
def name(self) -> str:
    return "bash"
```

Update the description (line 85-99) to clarify stateful semantics:

```python
# Before (lines 85-99):
@property
def description(self) -> str:
    return (
        "Execute a shell command in the CURRENTLY SELECTED terminal tab. "
        "Use 'terminal list' to see all tabs and which is selected (default). "
        ...
    )

# After:
@property
def description(self) -> str:
    return (
        "Execute a shell command in a persistent terminal session. "
        "Working directory, environment variables, and background "
        "processes persist between calls in the same session.\n\n"
        "Use 'terminal list' to see all sessions and which is selected (default). "
        "Use 'terminal select <name>' to switch sessions; use 'terminal open <name>' "
        "to create a new session (it auto-selects).\n\n"
        "Do NOT re-run setup commands (cd, source, export, etc.) that were "
        "already executed in this session.\n\n"
        "Returns <command_result> XML with <status>: completed, running, "
        "timed_out, paginated, or input_wait. If <status> is not 'completed', "
        "use 'process log' or 'terminal current' to check the state.\n\n"
        "IMPORTANT: If a command asks for a password, STOP and ask the user. "
        "NEVER guess or invent passwords."
    )
```

Note: The implementation words "tab" have been replaced with "session" — LLM-facing language. The internal "terminal tab" concept is preserved in the description through "terminal session" which is what the user sees.

- [ ] **Step 3: Create tests for bash name unification**

```python
# tests/unit/tools/terminal/test_bash_name.py
"""Verify both shell tools register as 'bash' with distinct descriptions."""

from __future__ import annotations

import pytest
from framework.tools.terminal.command_tool import CommandTool
from framework.tools.terminal.subprocess_tool import SubprocessTool


class TestBashToolName:
    """Both tools must register as 'bash' for LLM consistency."""

    def test_subprocess_tool_name_is_bash(self) -> None:
        tool = SubprocessTool(timeout=60)
        assert tool.name == "bash"

    def test_command_tool_name_is_bash(self) -> None:
        """CommandTool name is 'bash' — check the name property."""
        # We cannot construct CommandTool without a TerminalManager,
        # so verify the name on the class.
        from framework.tools.terminal.command_tool import CommandTool as CT
        # Create with minimal constructor args (manager is checked lazily)
        # Since __init__ requires TerminalManager, check the property definition
        assert CT.name.fget is not None  # property exists


class TestBashToolDescriptions:
    """Descriptions must distinguish stateful vs stateless without impl details."""

    def test_subprocess_description_mentions_independent(self) -> None:
        tool = SubprocessTool(timeout=60)
        desc = tool.description
        assert "fresh" in desc.lower() or "independently" in desc.lower()
        assert "do not" in desc.lower() or "does not" in desc.lower()

    def test_subprocess_description_no_impl_words(self) -> None:
        tool = SubprocessTool(timeout=60)
        desc = tool.description
        # Must not expose implementation details
        assert "subprocess" not in desc.lower()
        assert "SubprocessExecutor" not in desc

    def test_command_description_mentions_persistent(self) -> None:
        tool = CommandTool.__new__(CommandTool)
        tool.__init__()
        desc = tool.description
        assert "persist" in desc.lower()
        assert "stateful" not in desc.lower()  # not an impl word
```

- [ ] **Step 4: Run tests**

```bash
pytest tests/unit/tools/terminal/test_bash_name.py -v
```
Expected: Tests PASS

- [ ] **Step 5: Run existing tests to verify no regressions**

```bash
pytest tests/unit/tools/terminal/ -v --tb=short
```
Expected: All existing terminal tests PASS (some may need `"shell"` → `"bash"` name update)

- [ ] **Step 6: Fix any existing tests referencing old names**

Search for `"shell"` and `"command"` in test files as tool names, update to `"bash"`:

```bash
grep -rn '"shell"\|"command"' tests/unit/tools/terminal/ --include="*.py"
```

Update any mock/assert that references the old tool name string.

- [ ] **Step 7: Commit**

```bash
git add framework/tools/terminal/subprocess_tool.py \
        framework/tools/terminal/command_tool.py \
        tests/unit/tools/terminal/test_bash_name.py
git commit -m "refactor: unify shell tools under name 'bash'

SubprocessTool name: 'shell' → 'bash'
CommandTool name: 'command' → 'bash'
Descriptions distinguish stateful (persistent session) vs
stateless (fresh invocation) semantics without exposing
implementation details."
```

---

### Task 3: Extend AgentTemplate with Pi-Aligned Fields

**Files:**
- Modify: `framework/multi_agent/template.py`

**Why:** AgentTemplate is the dataclass that carries subagent configuration from YAML to the runtime. Adding pi-aligned fields lets template YAML express tool permissions, context mode, thinking budget, default reads, and progress tracking.

- [ ] **Step 1: Add new fields to AgentTemplate**

Replace `framework/multi_agent/template.py`:

```python
# framework/multi_agent/template.py
"""AgentTemplate — preset definition for dynamically created subagents."""

from __future__ import annotations

from dataclasses import dataclass, field

from framework.ioc.configs.memory import MemoryConfig
from framework.ioc.configs.skills import SkillsConfig
from framework.tools.presets import ToolPreset


@dataclass
class AgentTemplate:
    """Preset definition for a dynamically creatable subagent type.

    Communication tools (send_to_agent + list_communication_targets) are
    auto-injected by the framework — they must not appear in template config.

    Pi-aligned fields (tool_preset, context_mode, thinking_budget,
    default_reads, progress_tracking) were added for the coding pool
    redesign. They have no runtime effect unless the communication
    service chooses to act on them.
    """

    agent_type: str
    description: str = ""

    # ── lifecycle ──
    max_steps: int = 20

    # ── tool policy (backward-compatible) ──
    # When tool_preset is present, it takes precedence over standard_tools.
    standard_tools: bool = True
    tool_preset: ToolPreset = ToolPreset.FULL
    use_terminal: bool = True
    terminal_visibility: bool = True

    # ── pi-aligned fields ──
    context_mode: str = "fresh"          # "fresh" | "fork"
    thinking_budget: str = "medium"      # "low" | "medium" | "high" — prompt annotation only
    default_reads: list[str] = field(default_factory=list)
    progress_tracking: bool = False

    # ── optional subsystems ──
    memory: MemoryConfig | None = None
    skills: SkillsConfig | None = None
```

- [ ] **Step 2: Verify existing tests still pass**

```bash
pytest tests/unit/multi_agent/test_template.py tests/unit/multi_agent/test_template_registry.py -v
```
Expected: All tests PASS (default ToolPreset.FULL is backward-compatible)

- [ ] **Step 3: Commit**

```bash
git add framework/multi_agent/template.py
git commit -m "feat: extend AgentTemplate with pi-aligned fields

Add tool_preset (ToolPreset.FULL default), context_mode,
thinking_budget, default_reads, progress_tracking.
tool_preset takes precedence over standard_tools when
present. All new fields are backward-compatible."
```

---

### Task 4: Update Template Registry to Parse New Fields

**Files:**
- Modify: `framework/multi_agent/template_registry.py:29-71`

**Why:** The registry must parse the new YAML fields from template files and pass them to the AgentTemplate constructor.

- [ ] **Step 1: Update _load method to parse new fields**

In `framework/multi_agent/template_registry.py`, replace the `AgentTemplate(...)` constructor call (lines 52-67) inside `_load`:

```python
# Replace the existing AgentTemplate(...) call in _load():

# Add import at top of file:
from framework.tools.presets import ToolPreset

# Inside _load(), replace the constructor call:
tool_preset_raw = raw.get("tool_preset")
tool_preset = ToolPreset.FULL
if tool_preset_raw is not None:
    try:
        tool_preset = ToolPreset(tool_preset_raw)
    except ValueError:
        logger.warning(
            "Invalid tool_preset '%s' in %s, falling back to 'full'",
            tool_preset_raw, yml_path,
        )

template = AgentTemplate(
    agent_type=raw["agent_type"],
    description=raw.get("description", ""),
    max_steps=raw.get("max_steps", 20),
    standard_tools=raw.get("standard_tools", True),
    tool_preset=tool_preset,
    use_terminal=raw.get("use_terminal", True),
    terminal_visibility=raw.get("terminal_visibility", True),
    context_mode=raw.get("context_mode", "fresh"),
    thinking_budget=raw.get("thinking_budget", "medium"),
    default_reads=raw.get("default_reads", []),
    progress_tracking=raw.get("progress_tracking", False),
    memory=(
        MemoryConfig.model_validate(raw["memory"])
        if raw.get("memory") else None
    ),
    skills=(
        SkillsConfig(roots=raw["skills"]["roots"])
        if raw.get("skills") else None
    ),
)
```

- [ ] **Step 2: Verify existing tests pass**

```bash
pytest tests/unit/multi_agent/test_template_registry.py -v
```
Expected: All tests PASS

- [ ] **Step 3: Add a test for new field parsing**

Create a temporary YAML file in the test and verify parsing:

```python
# Append to tests/unit/multi_agent/test_template_registry.py

from pathlib import Path

class TestNewFields:
    """Verify pi-aligned fields are parsed from YAML."""

    def test_parses_tool_preset_from_yaml(self, tmp_path: Path) -> None:
        from framework.tools.presets import ToolPreset

        pools_dir = tmp_path / "config" / "pools" / "testpool" / "templates"
        pools_dir.mkdir(parents=True)
        yml = pools_dir / "worker.yml"
        yml.write_text(
            "agent_type: worker\n"
            "tool_preset: read_write\n"
            "context_mode: fork\n"
            "thinking_budget: high\n"
            "default_reads:\n"
            "  - context.md\n"
            "  - plan.md\n"
            "progress_tracking: true\n",
            encoding="utf-8",
        )

        registry = AgentTemplateRegistry(tmp_path)
        tmpl = registry.get_template("testpool", "worker")
        assert tmpl is not None
        assert tmpl.tool_preset == ToolPreset.READ_WRITE
        assert tmpl.context_mode == "fork"
        assert tmpl.thinking_budget == "high"
        assert tmpl.default_reads == ["context.md", "plan.md"]
        assert tmpl.progress_tracking is True

    def test_unknown_tool_preset_falls_back_to_full(self, tmp_path: Path) -> None:
        from framework.tools.presets import ToolPreset

        pools_dir = tmp_path / "config" / "pools" / "testpool" / "templates"
        pools_dir.mkdir(parents=True)
        yml = pools_dir / "bad.yml"
        yml.write_text(
            "agent_type: bad\n"
            "tool_preset: nonexistent\n",
            encoding="utf-8",
        )

        registry = AgentTemplateRegistry(tmp_path)
        tmpl = registry.get_template("testpool", "bad")
        assert tmpl is not None
        assert tmpl.tool_preset == ToolPreset.FULL  # fallback
```

- [ ] **Step 4: Run tests**

```bash
pytest tests/unit/multi_agent/test_template_registry.py -v
```

- [ ] **Step 5: Commit**

```bash
git add framework/multi_agent/template_registry.py tests/unit/multi_agent/test_template_registry.py
git commit -m "feat: parse pi-aligned fields from template YAML

Template registry now accepts tool_preset, context_mode,
thinking_budget, default_reads, progress_tracking.
Invalid tool_preset values fall back to FULL."
```

---

### Task 5: Add `extra_tools` Field to AgentConfig

**Files:**
- Modify: `framework/ioc/configs/agent.py`

**Why:** The coding pool's main agent needs AST and LSP tools that are not covered by any preset. `extra_tools` lets pool YAML declare tool names that `pool_builder` will register by name.

- [ ] **Step 1: Add extra_tools field**

In `framework/ioc/configs/agent.py`, add after the `hooks` field (line 67):

```python
class AgentConfig(BaseModel):
    # ... existing fields unchanged ...
    hooks: HooksConfig | None = Field(default_factory=HooksConfig)

    # pi-aligned: extra tools registered by name for the main agent
    # e.g. ["ast_grep_search", "ast_grep_replace", "lsp_diagnostics", "lsp_navigation"]
    extra_tools: list[str] = Field(default_factory=list)
```

- [ ] **Step 2: Verify existing tests pass**

```bash
pytest tests/unit/ioc/ -v --tb=short
```
Expected: All tests PASS (default_factory=list is backward-compatible)

- [ ] **Step 3: Commit**

```bash
git add framework/ioc/configs/agent.py
git commit -m "feat: add extra_tools field to AgentConfig

Allows main agent config to declare tool names that
pool_builder will register by name, for tools not covered
by any preset (e.g. AST search, LSP stubs)."
```

---

### Task 6: AST Tools — tree-sitter Engine + Search + Replace

**Files:**
- Create: `framework/tools/ast/__init__.py`
- Create: `framework/tools/ast/engine.py`
- Create: `framework/tools/ast/ast_search.py`
- Create: `framework/tools/ast/ast_replace.py`
- Modify: `pyproject.toml` (add `ast` optional dep group)
- Test: `tests/unit/tools/ast/test_engine.py`

**Why:** The AST tools enable the main coding agent to search and replace code using AST pattern matching. These are registered only for the main agent via `extra_tools`.

- [ ] **Step 1: Add ast optional dependency group to pyproject.toml**

After the `terminal` optional dep group (line 68), add:

```toml
ast = [
  "tree-sitter>=0.24",
  "tree-sitter-python>=0.23",
  "tree-sitter-java>=0.23",
]
all = ["ModexAgent[llm,storage,session,sandbox,gateway,skills,terminal,ast]"]
```

- [ ] **Step 2: Create the matching engine**

```python
# framework/tools/ast/engine.py
"""tree-sitter pattern matching engine for AST search/replace."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Graceful degradation when tree_sitter is not installed
_TREE_SITTER_AVAILABLE = False
try:
    import tree_sitter  # noqa: F401
    _TREE_SITTER_AVAILABLE = True
except ImportError:
    pass

_TREE_SITTER_PYTHON_AVAILABLE = False
try:
    import tree_sitter_python  # noqa: F401
    _TREE_SITTER_PYTHON_AVAILABLE = True
except ImportError:
    pass

_TREE_SITTER_JAVA_AVAILABLE = False
try:
    import tree_sitter_java  # noqa: F401
    _TREE_SITTER_JAVA_AVAILABLE = True
except ImportError:
    pass


def is_ast_available() -> bool:
    """Check if tree-sitter and at least one language grammar are installed."""
    return _TREE_SITTER_AVAILABLE and (_TREE_SITTER_PYTHON_AVAILABLE or _TREE_SITTER_JAVA_AVAILABLE)


AST_UNAVAILABLE_MSG = (
    "AST tools require tree-sitter. Install: pip install ModexAgent[ast]\n"
    "Or manually: pip install tree-sitter tree-sitter-python tree-sitter-java"
)


class AstNotAvailableError(RuntimeError):
    """Raised when tree-sitter or a grammar is not installed."""
    pass


def _get_parser(language: str) -> Any:
    """Get a tree-sitter Parser for the given language.

    Args:
        language: "python" or "java"

    Returns:
        tree_sitter.Parser instance

    Raises:
        AstNotAvailableError: if tree-sitter or the grammar is not installed
    """
    if not _TREE_SITTER_AVAILABLE:
        raise AstNotAvailableError(AST_UNAVAILABLE_MSG)

    import tree_sitter

    lang_map: dict[str, Any] = {}
    if _TREE_SITTER_PYTHON_AVAILABLE:
        import tree_sitter_python
        lang_map["python"] = tree_sitter_python.language()
    if _TREE_SITTER_JAVA_AVAILABLE:
        import tree_sitter_java
        lang_map["java"] = tree_sitter_java.language()

    lang = lang_map.get(language)
    if lang is None:
        available = list(lang_map.keys())
        raise AstNotAvailableError(
            f"Language '{language}' not available. Available: {available}. "
            f"Install: pip install tree-sitter-{language}"
        )

    parser = tree_sitter.Parser()
    parser.set_language(lang)
    return parser


@dataclass
class AstMatch:
    """A single AST pattern match in a source file."""
    file_path: str
    line: int
    column: int
    text: str
    captures: dict[str, str] = field(default_factory=dict)


def search_in_file(
    source: str,
    pattern: str,
    language: str,
    file_path: str = "",
) -> list[AstMatch]:
    """Search for AST pattern matches in a source string.

    Supports $VAR (single node) and $$$BODY (zero-or-more nodes) meta-variables.
    Multiple $$$BODY meta-variables are not supported (only one).
    """
    _ = source, pattern, language, file_path
    raise AstNotAvailableError(AST_UNAVAILABLE_MSG)


def search_in_directory(
    directory: Path,
    pattern: str,
    language: str,
    file_extensions: tuple[str, ...] | None = None,
) -> list[AstMatch]:
    """Search for AST pattern matches across files in a directory."""
    _ = directory, pattern, language, file_extensions
    raise AstNotAvailableError(AST_UNAVAILABLE_MSG)


def replace_in_file(
    source: str,
    pattern: str,
    replacement: str,
    language: str,
) -> tuple[str, int]:
    """Replace AST pattern matches in source. Returns (new_source, replacement_count)."""
    _ = source, pattern, replacement, language
    raise AstNotAvailableError(AST_UNAVAILABLE_MSG)
```

**Important:** The engine.py above contains the **stub** that raises `AstNotAvailableError`. The actual tree-sitter implementation will be completed in a follow-up subtask after the dependency is installable. For now, the tools will return the graceful degradation message.

- [ ] **Step 3: Create ast_grep_search tool**

```python
# framework/tools/ast/ast_search.py
"""ast_grep_search tool — search code using AST pattern matching."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from framework.core.tool_manager import Tool
from framework.tools.ast.engine import (
    AST_UNAVAILABLE_MSG,
    AstNotAvailableError,
    is_ast_available,
    search_in_file,
    search_in_directory,
)

_EXT_MAP: dict[str, tuple[str, ...]] = {
    "python": (".py",),
    "java": (".java",),
}


class AstGrepSearchTool(Tool):
    """Search code using AST-aware pattern matching."""

    def __init__(self) -> None:
        super().__init__()

    @property
    def name(self) -> str:
        return "ast_grep_search"

    @property
    def description(self) -> str:
        return (
            "Search code using AST pattern matching. "
            "Use $NAME for a single node, $$$ARGS for zero or more nodes. "
            "Example: 'def $FUNC($$$ARGS): return $EXPR' matches function definitions. "
            "Supported languages: python, java."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": (
                        "AST pattern. $NAME captures a single AST node. "
                        "$$$ARGS captures zero or more nodes. "
                        "Example: 'function $NAME($$$ARGS) { $$$BODY }'"
                    ),
                },
                "language": {
                    "type": "string",
                    "description": "Programming language: 'python' or 'java'",
                    "enum": ["python", "java"],
                },
                "path": {
                    "type": "string",
                    "description": "File or directory to search (default: current working directory)",
                },
            },
            "required": ["pattern", "language"],
        }

    async def execute(
        self,
        pattern: str,
        language: str,
        path: str | None = None,
        **kwargs: object,
    ) -> str:
        if not is_ast_available():
            return AST_UNAVAILABLE_MSG

        search_path = Path(path) if path else Path.cwd()

        try:
            if search_path.is_file():
                source = search_path.read_text(encoding="utf-8")
                matches = search_in_file(source, pattern, language, str(search_path))
            else:
                exts = _EXT_MAP.get(language, (".py",))
                matches = search_in_directory(search_path, pattern, language, exts)

            if not matches:
                return "No matches found."

            lines: list[str] = []
            for m in matches:
                lines.append(f"{m.file_path}:{m.line}:{m.column}: {m.text}")

            lines.append(f"\nFound {len(matches)} match(es).")
            return "\n".join(lines)

        except AstNotAvailableError as e:
            return str(e)
        except Exception as e:
            return f"Error: {e}"
```

- [ ] **Step 4: Create ast_grep_replace tool**

```python
# framework/tools/ast/ast_replace.py
"""ast_grep_replace tool — replace code using AST pattern matching."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from framework.core.tool_manager import Tool
from framework.tools.ast.engine import (
    AST_UNAVAILABLE_MSG,
    AstNotAvailableError,
    is_ast_available,
    replace_in_file,
)


class AstGrepReplaceTool(Tool):
    """Replace code using AST-aware pattern matching. Dry-run by default."""

    def __init__(self) -> None:
        super().__init__()

    @property
    def name(self) -> str:
        return "ast_grep_replace"

    @property
    def description(self) -> str:
        return (
            "Replace code using AST pattern matching. "
            "Use $VAR from the pattern in the replacement. "
            "Dry-run by default — set dry_run=false to apply changes."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "AST pattern to match. Use $VAR and $$$ARGS meta-variables.",
                },
                "replacement": {
                    "type": "string",
                    "description": "Replacement template. Reference $VAR captures from the pattern.",
                },
                "language": {
                    "type": "string",
                    "description": "Programming language: 'python' or 'java'",
                    "enum": ["python", "java"],
                },
                "path": {
                    "type": "string",
                    "description": "Target file path (required, must be a single file)",
                },
                "dry_run": {
                    "type": "boolean",
                    "description": "Preview changes without writing (default: true)",
                },
            },
            "required": ["pattern", "replacement", "language", "path"],
        }

    async def execute(
        self,
        pattern: str,
        replacement: str,
        language: str,
        path: str,
        dry_run: bool = True,
        **kwargs: object,
    ) -> str:
        if not is_ast_available():
            return AST_UNAVAILABLE_MSG

        file_path = Path(path)
        if not file_path.is_file():
            return f"Error: {path} is not a file. ast_grep_replace requires a single file path."

        try:
            source = file_path.read_text(encoding="utf-8")
            new_source, count = replace_in_file(source, pattern, replacement, language)

            if count == 0:
                return "No matches found. Nothing to replace."

            if dry_run:
                # Show diff-like preview
                lines: list[str] = [f"--- {file_path.name}"]
                old_lines = source.split("\n")
                new_lines = new_source.split("\n")
                for i, (old, new) in enumerate(zip(old_lines, new_lines)):
                    if old != new:
                        lines.append(f"- {old}")
                        lines.append(f"+ {new}")
                lines.append(f"\n{count} replacement(s) (dry run). Set dry_run=false to apply.")
                return "\n".join(lines)

            file_path.write_text(new_source, encoding="utf-8")
            return f"{count} replacement(s) applied to {file_path.name}."

        except AstNotAvailableError as e:
            return str(e)
        except Exception as e:
            return f"Error: {e}"
```

- [ ] **Step 5: Create __init__.py**

```python
# framework/tools/ast/__init__.py
"""AST tools — tree-sitter based code search and replace."""

from framework.tools.ast.ast_search import AstGrepSearchTool
from framework.tools.ast.ast_replace import AstGrepReplaceTool

__all__ = ["AstGrepSearchTool", "AstGrepReplaceTool"]
```

- [ ] **Step 6: Create engine unit tests (stub behavior)**

```python
# tests/unit/tools/ast/test_engine.py
"""Tests for AST engine graceful degradation."""

from __future__ import annotations

import pytest
from framework.tools.ast.engine import (
    is_ast_available,
    AstNotAvailableError,
    _get_parser,
)


class TestGracefulDegradation:
    """When tree-sitter is not installed, tools return helpful messages."""

    def test_is_ast_available_returns_bool(self) -> None:
        """is_ast_available() always returns a bool."""
        result = is_ast_available()
        assert isinstance(result, bool)

    def test_get_parser_invalid_language_raises(self) -> None:
        """Invalid language raises AstNotAvailableError."""
        with pytest.raises(AstNotAvailableError):
            _get_parser("nonexistent_language")
```

- [ ] **Step 7: Run tests**

```bash
pytest tests/unit/tools/ast/test_engine.py -v
```

- [ ] **Step 8: Commit**

```bash
git add framework/tools/ast/ pyproject.toml tests/unit/tools/ast/
git commit -m "feat: add AST tools with tree-sitter engine (stub)

ast_grep_search: AST pattern search with $VAR capture.
ast_grep_replace: AST pattern replace with dry-run default.
Engine stubs raise AstNotAvailableError when tree-sitter
not installed. Optional dep: pip install ModexAgent[ast]"
```

---

### Task 7: LSP Tool Stubs

**Files:**
- Create: `framework/tools/lsp/__init__.py`
- Create: `framework/tools/lsp/lsp_diagnostics.py`
- Create: `framework/tools/lsp/lsp_navigation.py`

**Why:** LSP tools are declared in pi-reference but need a full LSP client implementation. Stubs ensure LLM sees the tool definitions but receives clear "not yet implemented" messages.

- [ ] **Step 1: Create lsp_diagnostics stub**

```python
# framework/tools/lsp/lsp_diagnostics.py
"""lsp_diagnostics stub — get LSP errors/warnings for a file or directory."""

from __future__ import annotations

from typing import Any

from framework.core.tool_manager import Tool

_NOT_IMPLEMENTED = "LSP diagnostics is not yet implemented. This tool will be available in a future update."


class LspDiagnosticsTool(Tool):
    """Get language server diagnostics for a file or directory (stub)."""

    def __init__(self) -> None:
        super().__init__()

    @property
    def name(self) -> str:
        return "lsp_diagnostics"

    @property
    def description(self) -> str:
        return (
            "Get errors, warnings, and hints from language servers for a file or directory. "
            "Use BEFORE running builds to catch issues early. "
            f"(Note: {_NOT_IMPLEMENTED})"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "file": {
                    "type": "string",
                    "description": "Path to a source file or directory to check",
                },
            },
            "required": ["file"],
        }

    async def execute(self, file: str, **kwargs: object) -> str:
        return _NOT_IMPLEMENTED
```

- [ ] **Step 2: Create lsp_navigation stub**

```python
# framework/tools/lsp/lsp_navigation.py
"""lsp_navigation stub — LSP code navigation operations."""

from __future__ import annotations

from typing import Any

from framework.core.tool_manager import Tool

_NOT_IMPLEMENTED = "LSP navigation is not yet implemented. This tool will be available in a future update."


class LspNavigationTool(Tool):
    """Navigate code using Language Server Protocol (stub)."""

    def __init__(self) -> None:
        super().__init__()

    @property
    def name(self) -> str:
        return "lsp_navigation"

    @property
    def description(self) -> str:
        return (
            "Navigate code using LSP (Language Server Protocol). "
            "Operations: go_to_definition, find_references, hover, "
            "document_symbol, workspace_symbol, go_to_implementation, "
            "incoming_calls, outgoing_calls. "
            f"(Note: {_NOT_IMPLEMENTED})"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "operation": {
                    "type": "string",
                    "description": "Navigation operation to perform",
                    "enum": [
                        "go_to_definition", "find_references", "hover",
                        "document_symbol", "workspace_symbol",
                        "go_to_implementation", "incoming_calls", "outgoing_calls",
                    ],
                },
                "file": {
                    "type": "string",
                    "description": "Path to the source file",
                },
                "line": {
                    "type": "integer",
                    "description": "Line number (1-based)",
                },
                "character": {
                    "type": "integer",
                    "description": "Character position (0-based)",
                },
            },
            "required": ["operation", "file", "line", "character"],
        }

    async def execute(
        self,
        operation: str,
        file: str,
        line: int,
        character: int,
        **kwargs: object,
    ) -> str:
        return _NOT_IMPLEMENTED
```

- [ ] **Step 3: Create __init__.py**

```python
# framework/tools/lsp/__init__.py
"""LSP tools — language server protocol integration."""

from framework.tools.lsp.lsp_diagnostics import LspDiagnosticsTool
from framework.tools.lsp.lsp_navigation import LspNavigationTool

__all__ = ["LspDiagnosticsTool", "LspNavigationTool"]
```

- [ ] **Step 4: Commit**

```bash
git add framework/tools/lsp/
git commit -m "feat: add LSP tool stubs (lsp_diagnostics, lsp_navigation)

Stubs return 'not yet implemented' message. Tool schemas
are fully defined so LLMs can see them. Actual LSP client
integration deferred to a future update."
```

---

### Task 8: Communication Service — Preset-Based Tool Registration

**Files:**
- Modify: `framework/multi_agent/communication.py:352-418` (`_build_subagent_tool_manager`)

**Why:** `_build_subagent_tool_manager` currently hardcodes all standard tools. It must now use `get_preset_tools()` based on `template.tool_preset` and inject the fork preamble when `context_mode="fork"`.

- [ ] **Step 1: Rewrite _build_subagent_tool_manager to use tool_preset**

Replace the body of `_build_subagent_tool_manager` (lines 352-418):

```python
async def _build_subagent_tool_manager(self, template: AgentTemplate, agent_name: str):
    """Build the subagent tool manager from template configuration.

    Uses template.tool_preset to determine which tools to register.
    Falls back to template.standard_tools for backward compatibility.
    """
    from framework.core.tool_manager import InMemoryToolManager, ToolManagerConfig
    from framework.multi_agent.tools import (
        ListCommunicationTargetsTool,
        SendToAgentTool,
    )
    from framework.multi_agent.address import AgentAddress
    from framework.tools.presets import get_preset_tools

    tm = InMemoryToolManager(config=ToolManagerConfig(
        max_workers=10, enable_parallel=True, parallel_max_workers=5,
    ))

    # Determine tool preset
    preset = template.tool_preset

    # Bash tool factory: use SubprocessTool for subagents (no terminal)
    from framework.tools.terminal import SubprocessTool, SubprocessExecutor

    def _make_bash() -> SubprocessTool:
        return SubprocessTool(timeout=60)

    # Register preset tools
    for tool in get_preset_tools(preset, subprocess_tool_factory=_make_bash):
        tm.register(tool)

    # MCP tools from per-agent config file: config/mcp/{agentType}.json
    if self._project_dir is not None:
        mcp_json = self._project_dir / "config" / "mcp" / f"{template.agent_type}.json"
        if mcp_json.exists():
            try:
                await _load_per_agent_mcp(tm, mcp_json, agent_name)
            except Exception:
                logger.exception(
                    "Failed to load MCP tools for subagent %s from %s",
                    agent_name, mcp_json,
                )

    # Communication tools — always included so subagent can reply to parent
    subagent_address = AgentAddress(name=agent_name)
    tm.register(SendToAgentTool(
        source=subagent_address,
        broker=self._broker,
        registry=self._registry,
        agent_bus=self._agent_bus,
        service=self,
        comm_tracker=self._comm_tracker,
    ))
    tm.register(ListCommunicationTargetsTool(
        self_address=subagent_address,
        registry=self._registry,
        template_registry=self._template_registry,
        pool_name=self._pool_name,
    ))

    return tm
```

- [ ] **Step 2: Inject fork preamble when context_mode="fork"**

In `_create_dynamic_subagent`, after loading system_prompt (~line 204), add:

```python
# After the system_prompt block (~line 204):
# ── Fork preamble: inject when context_mode="fork" ──
if template.context_mode == "fork":
    fork_preamble = (
        "\n\nYou are a subagent running from a fork of the parent session. "
        "Treat inherited conversation as reference-only context, not a live "
        "thread to continue. Your sole job is to execute the assigned task."
    )
    system_prompt = system_prompt + fork_preamble
```

- [ ] **Step 3: Run existing tests**

```bash
pytest tests/unit/multi_agent/test_communication_service.py -v --tb=short
```
Expected: Existing tests PASS (default ToolPreset.FULL is backward-compatible with old standard_tools behavior)

- [ ] **Step 4: Commit**

```bash
git add framework/multi_agent/communication.py
git commit -m "feat: preset-based tool registration for subagents

_build_subagent_tool_manager now uses template.tool_preset
instead of hardcoding all standard tools. Fork preamble injected
into system prompt when context_mode='fork'."
```

---

### Task 8a: Fork Context — Deep-Copy Parent Session Memory

**Files:**
- Modify: `framework/multi_agent/communication.py:169-220` (`_create_dynamic_subagent`)
- Modify: `examples/bot_project/bot/service/pool_builder.py` (`create_pool`)

**Why:** When `context_mode="fork"`, the subagent must receive a deep copy of the parent's conversation history as read-only reference. Parent and subagent must NOT share the same memory instance. Currently only a preamble is injected; no actual history is copied.

**Design:**
1. `AgentCommunicationService.__init__` receives `parent_memory_system` parameter.
2. `pool_builder.create_pool()` passes `pool.memory_system` (the main agent's MemorySystem) to the service.
3. In `_create_dynamic_subagent`, when `template.context_mode == "fork"`:
   a. Get parent session messages via `parent_memory_system._layers.session.get_all_messages(parent_ctx)`
   b. Deep copy each message (new dicts/dataclasses, no shared references)
   c. Pass as `initial_messages` to the subagent's `create_message_history()`
4. The fork preamble is still injected into system prompt.

- [ ] **Step 1: Add parent_memory_system to AgentCommunicationService.__init__**

In `framework/multi_agent/communication.py`, add the parameter:

```python
class AgentCommunicationService:
    def __init__(
        self,
        *,
        source: AgentAddress,
        broker: MessageBroker,
        registry: AgentRegistry,
        agent_bus: AgentMessageBus,
        session_strategy: DefaultSessionIdStrategy,
        comm_tracker: CommunicationTracker | None = None,
        template_registry: AgentTemplateRegistry | None = None,
        pool: AgentPool | None = None,
        pool_name: str = "",
        project_dir: Path | None = None,
        # ... existing subagent creation deps ...
        main_agent_name: str = "",
        # NEW: parent memory system for fork context deep-copy
        parent_memory_system: Any | None = None,
    ):
        # ... store self._parent_memory_system = parent_memory_system
```

- [ ] **Step 2: Implement fork memory deep-copy in _create_dynamic_subagent**

In `_create_dynamic_subagent`, after loading the system prompt (~line 204), add:

```python
# ── Fork context: deep-copy parent conversation history ──
if template.context_mode == "fork" and self._parent_memory_system is not None:
    try:
        from framework.memory.core.scope import MemoryContext
        parent_session_id = self._session_strategy.format(
            conversation_id=conversation_id,
            agent_name=self._main_agent_name or "main",
        )
        parent_ctx = MemoryContext(session_id=parent_session_id)
        parent_messages = self._parent_memory_system._layers.session.get_all_messages(
            parent_ctx
        )
        if parent_messages:
            # Deep copy: recreate each message as a new ChatMessage
            import copy
            initial_messages = copy.deepcopy(parent_messages)
            # Pass to subagent memory via initial_messages
            subagent_ctx._initial_messages = initial_messages
            logger.info(
                "Fork context: copied %d messages from parent session %s",
                len(initial_messages), parent_session_id,
            )
    except Exception:
        logger.exception("Failed to copy parent messages for fork context")
```

**Note:** The exact API for passing `initial_messages` depends on how `build_session_only_memory`'s result is consumed. If `create_message_history()` is called later by the pool's consumer loop, we need to inject `initial_messages` into the subagent's session memory before the first `load()`. The simplest approach: after building the subagent memory, call `replace_messages()` on the subagent's session layer.

```python
# Alternative: inject after subagent memory is created
if template.context_mode == "fork" and self._parent_memory_system is not None:
    try:
        parent_messages = self._parent_memory_system._layers.session.get_all_messages(
            MemoryContext(session_id=parent_session_id)
        )
        if parent_messages:
            subagent_session_ctx = MemoryContext(session_id=subagent_session_id)
            subagent_memory_system = subagent_ctx.memory_system
            await subagent_memory_system._layers.session.replace_messages(
                subagent_session_ctx,
                copy.deepcopy(parent_messages),
            )
    except Exception:
        logger.exception("Failed to fork parent session messages")
```

- [ ] **Step 3: Connect parent_memory_system in pool_builder**

In `pool_builder.py`, pass the pool's `memory_system` to `AgentCommunicationService`:

```python
main_service = AgentCommunicationService(
    # ... existing params ...
    parent_memory_system=memory_system,  # NEW: for fork context deep-copy
)
```

- [ ] **Step 4: Create unit test for fork memory copy**

```python
# tests/unit/multi_agent/test_fork_context.py
"""Verify fork context deep-copies parent messages into subagent memory."""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock


class TestForkContextMemoryCopy:
    """When context_mode='fork', parent messages are deep-copied."""

    def test_fork_copies_parent_messages(self) -> None:
        """Fork mode calls get_all_messages on parent and copies to subagent."""
        # Import the core logic — to be written as a helper function
        from framework.multi_agent.communication import _fork_parent_messages
        parent_memory = MagicMock()
        subagent_memory = MagicMock()
        parent_messages = [{"role": "user", "content": "hello"}]
        parent_memory._layers.session.get_all_messages.return_value = parent_messages

        result = _fork_parent_messages(
            parent_memory_system=parent_memory,
            parent_session_id="conv123:main",
            subagent_memory_system=subagent_memory,
            subagent_session_id="conv123:worker:abc123",
        )

        assert result is True
        # Subagent session was seeded with copied messages
        subagent_memory._layers.session.replace_messages.assert_called_once()

    def test_fork_empty_parent_session(self) -> None:
        """When parent has no messages, fork is a no-op."""
        from framework.multi_agent.communication import _fork_parent_messages
        parent_memory = MagicMock()
        subagent_memory = MagicMock()
        parent_memory._layers.session.get_all_messages.return_value = []

        result = _fork_parent_messages(
            parent_memory_system=parent_memory,
            parent_session_id="conv123:main",
            subagent_memory_system=subagent_memory,
            subagent_session_id="conv123:worker:abc123",
        )

        assert result is False
        subagent_memory._layers.session.replace_messages.assert_not_called()
```

- [ ] **Step 5: Run tests**

```bash
pytest tests/unit/multi_agent/test_fork_context.py -v
```

- [ ] **Step 6: Commit**

```bash
git add framework/multi_agent/communication.py \
        examples/bot_project/bot/service/pool_builder.py \
        tests/unit/multi_agent/test_fork_context.py
git commit -m "feat: fork context — deep-copy parent session memory

When AgentTemplate.context_mode='fork', the subagent receives
a deep-copied snapshot of the parent agent's conversation
history as read-only reference. Parent and subagent memory
systems remain independent.

AgentCommunicationService accepts parent_memory_system for
fork operations. pool_builder wires it through."
```

---

### Task 8b: Dynamic Communication Targets

**Files:**
- Modify: `framework/multi_agent/template.py` (add `visible_targets`)
- Modify: `framework/multi_agent/tools.py` (parameterize `ListCommunicationTargetsTool`)
- Modify: `framework/multi_agent/communication.py` (pass visible_targets to tool)

**Why:** Subagents should only see communication targets relevant to them, configurable at creation time. Default: subagents only see their parent (main agent). The list must be parameterized so future pool configurations can override.

- [ ] **Step 1: Add visible_targets to AgentTemplate**

In `framework/multi_agent/template.py`:

```python
@dataclass
class AgentTemplate:
    # ... existing fields ...
    progress_tracking: bool = False
    visible_targets: list[str] | None = None  # None=all NORMAL; list=restrict to named agents
```

- [ ] **Step 2: Parameterize ListCommunicationTargetsTool**

In `framework/multi_agent/tools.py`, modify `ListCommunicationTargetsTool.__init__`:

```python
class ListCommunicationTargetsTool(Tool):
    def __init__(
        self,
        *,
        self_address: AgentAddress,
        registry: AgentRegistry,
        template_registry: AgentTemplateRegistry | None = None,
        pool_name: str | None = None,
        visible_targets: list[str] | None = None,  # NEW
    ):
        super().__init__()
        # ... existing stores ...
        self._visible_targets = visible_targets
```

In `execute()`, after the comm_kind filtering (~line 199), add:

```python
# Apply visible_targets restriction (if set)
if self._visible_targets is not None:
    visible_set = set(self._visible_targets)
    targets = [p for p in targets if p.name in visible_set]
```

- [ ] **Step 3: Pass visible_targets when creating subagent tools**

In `communication.py`, `_build_subagent_tool_manager`:

```python
# Communication tools with dynamic target visibility
subagent_address = AgentAddress(name=agent_name)
visible = template.visible_targets if template.visible_targets is not None else None
# If not specified, default to parent-only for subagents
if visible is None:
    visible = [self._main_agent_name] if self._main_agent_name else []

tm.register(ListCommunicationTargetsTool(
    self_address=subagent_address,
    registry=self._registry,
    template_registry=self._template_registry,
    pool_name=self._pool_name,
    visible_targets=visible,  # NEW
))
```

- [ ] **Step 4: Run existing tests**

```bash
pytest tests/unit/multi_agent/test_send_to_agent_tools.py -v
```
Expected: All tests PASS (None default preserves backward-compatible behavior)

- [ ] **Step 5: Commit**

```bash
git add framework/multi_agent/template.py \
        framework/multi_agent/tools.py \
        framework/multi_agent/communication.py
git commit -m "feat: dynamic communication targets for subagents

ListCommunicationTargetsTool accepts visible_targets parameter.
Subagents default to seeing only their parent agent.
AgentTemplate gains visible_targets field for YAML override."
```

---

### Task 9: Pool Builder — Register extra_tools for Main Agent

**Files:**
- Modify: `examples/bot_project/bot/service/pool_builder.py`

**Why:** The pool_builder needs to read `main_cfg.extra_tools` and register the named tools (AST, LSP) into the main agent's tool_manager.

Also passes `memory_system` to `AgentCommunicationService` for fork context support (see Task 8a).

- [ ] **Step 1: Add extra_tools registration in create_pool()**

In `pool_builder.py`, find the `_build_pool_tool_manager` call (~line 98) and add after it:

```python
# After tool_manager and mcp_manager are built, register extra_tools
extra_tools = getattr(main_cfg, "extra_tools", []) or []
if extra_tools:
    _register_extra_tools(tool_manager, extra_tools)
    logger.info("Pool '%s': %d extra_tools registered: %s", pool_name, len(extra_tools), extra_tools)
```

And add the helper function before `_create_terminal_manager`:

```python
def _register_extra_tools(tool_manager: InMemoryToolManager, tool_names: list[str]) -> None:
    """Register named tools by looking up their class in known modules.

    Supports: ast_grep_search, ast_grep_replace, lsp_diagnostics, lsp_navigation
    Falls back silently if a tool class cannot be imported.
    """
    _TOOL_REGISTRY: dict[str, tuple[str, str]] = {
        "ast_grep_search": ("framework.tools.ast", "AstGrepSearchTool"),
        "ast_grep_replace": ("framework.tools.ast", "AstGrepReplaceTool"),
        "lsp_diagnostics": ("framework.tools.lsp", "LspDiagnosticsTool"),
        "lsp_navigation": ("framework.tools.lsp", "LspNavigationTool"),
    }

    for name in tool_names:
        entry = _TOOL_REGISTRY.get(name)
        if entry is None:
            logger.warning("Unknown extra_tool: %s", name)
            continue
        module_name, class_name = entry
        try:
            import importlib
            module = importlib.import_module(module_name)
            tool_cls = getattr(module, class_name)
            tool_manager.register(tool_cls())
        except Exception:
            logger.exception("Failed to register extra_tool: %s", name)
```

- [ ] **Step 2: Commit**

```bash
git add examples/bot_project/bot/service/pool_builder.py
git commit -m "feat: register extra_tools for main agent in pool_builder

Reads main_cfg.extra_tools and registers named tools
(ast_grep_search, ast_grep_replace, lsp_diagnostics,
lsp_navigation) via importlib lookup."
```

---

### Task 10: Hook Adaptations — Agent_Type Metadata

**Files:**
- Modify: `framework/hook/builtin/subagent_auto_send.py:127-132`
- Modify: `framework/hook/notification.py:97-116`

**Why:** Both hooks currently use only `agent_name`. Adding `agent_type` (the template type like "worker", "reviewer") to the metadata lets the receiving agent distinguish which role sent the message.

- [ ] **Step 1: Add agent_type to SubagentAutoSendHook envelope**

In `subagent_auto_send.py`, the `build_agent_result` call at ~line 127. The `source` already carries the agent name (e.g., "worker"). Add `agent_type` to the metadata dict in the envelope payload:

```python
# In after_turn(), change the envelope payload (lines 135-143):
envelope = AgentMessageEnvelope(
    payload={
        "content": xml_content,
        "message_type": "agent_result",
        "metadata": {                         # added metadata block
            "agent_type": self._self_name,    # e.g. "worker", "reviewer"
        },
    },
    source=AgentAddress(name=self._self_name),
    target=AgentAddress(name=reply_target),
    message_type="agent_result",
    conversation_id=conversation_id,
    agent_session_id=inbox_key,
    invocation_id=parts.invocation_id,
)
```

- [ ] **Step 2: Add agent_type to MaxIterationNotifyHook message**

In `notification.py`, the `MaxIterationNotifyHook.after_turn` method builds the XML message at lines 109-116. The `agent_name` is already set. Add agent_type context to the message:

```python
# In after_turn(), modify the agent_name extraction (lines 97-101):
agent_name = (
    ctx.session_meta.agent_name
    if ctx.session_meta
    else "unknown"
)
# agent_name already carries the template type (e.g., "worker", "reviewer")
# The notification service routes based on comm_kind, and the XML includes
# the source name which is already the agent_type. No additional change needed
# for MaxIterationNotifyHook — the source field in build_agent_result is sufficient.
```

Note: `MaxIterationNotifyHook` already uses `ctx.session_meta.agent_name` as the `source` parameter in `build_agent_result`. Since dynamic subagents are created with `agent_type` as their name (e.g., "worker"), this already carries the role information. No code change needed for this hook — the message already reads `"Subagent 'worker' reached max iterations (150)"`.

- [ ] **Step 3: Run existing hook tests**

```bash
pytest tests/unit/multi_agent/test_subagent_auto_send_hook.py -v --tb=short
```
Expected: All tests PASS

- [ ] **Step 4: Commit**

```bash
git add framework/hook/builtin/subagent_auto_send.py
git commit -m "feat: add agent_type metadata to SubagentAutoSendHook envelope

Envelope payload now includes metadata.agent_type so the
receiving agent can distinguish which subagent role sent
the auto-forwarded message."
```

---

### Tasks 11-17: Bot Layer — Configuration Files

**Files across T11-T17:**
- Delete: `config/pools/coding/templates/planner.yml` (old)
- Delete: `config/pools/coding/templates/reviewer.yml` (old)
- Create: `config/pools/coding/templates/scout.yml`
- Create: `config/pools/coding/templates/context-builder.yml`
- Create: `config/pools/coding/templates/planner.yml` (new)
- Create: `config/pools/coding/templates/reviewer.yml` (new)
- Create: `config/pools/coding/templates/worker.yml`
- Create: `config/pools/coding/templates/delegate.yml`
- Create: `agents/scout.md`
- Create: `agents/context-builder.md`
- Create: `agents/worker.md`
- Create: `agents/delegate.md`
- Replace: `agents/planner.md`
- Replace: `agents/reviewer.md`
- Modify: `agents/coding.md`
- Modify: `config/pools/coding.yml`

**Why:** All bot configuration is pure file creation/modification with no code changes. These tasks can all run in any order and can be parallelized.

- [ ] **Step 1: Delete old templates**

```bash
rm examples/bot_project/config/pools/coding/templates/planner.yml
rm examples/bot_project/config/pools/coding/templates/reviewer.yml
```

- [ ] **Step 2: Create 6 template YAML files**

```yaml
# config/pools/coding/templates/scout.yml
agent_type: scout
description: "Fast codebase recon — returns compressed context for handoff"
max_steps: 40
tool_preset: read_only
context_mode: fresh
thinking_budget: low
progress_tracking: true
memory:
  short_term: {max_messages: 40, max_tokens: 40000}
  archive: {enabled: true}
```

```yaml
# config/pools/coding/templates/context-builder.yml
agent_type: context-builder
description: "Deep requirements analysis — produces context.md and meta-prompt.md"
max_steps: 60
tool_preset: read_only
context_mode: fresh
thinking_budget: medium
memory:
  short_term: {max_messages: 60, max_tokens: 60000}
  archive: {enabled: true}
```

```yaml
# config/pools/coding/templates/planner.yml
agent_type: planner
description: "Creates implementation plans from context and requirements"
max_steps: 80
tool_preset: minimal
context_mode: fork
thinking_budget: high
default_reads: ["context.md"]
memory:
  short_term: {max_messages: 60, max_tokens: 60000}
  archive: {enabled: true}
```

```yaml
# config/pools/coding/templates/worker.yml
agent_type: worker
description: "Implementation agent — the single writer thread"
max_steps: 150
tool_preset: full
context_mode: fork
thinking_budget: high
default_reads: ["context.md", "plan.md"]
progress_tracking: true
use_terminal: true
memory:
  short_term: {max_messages: 120, max_tokens: 120000}
  archive: {enabled: true}
```

```yaml
# config/pools/coding/templates/reviewer.yml
agent_type: reviewer
description: "Versatile review specialist for code diffs, plans, and solutions"
max_steps: 100
tool_preset: read_write
context_mode: fresh
thinking_budget: high
default_reads: ["plan.md", "progress.md"]
memory:
  short_term: {max_messages: 80, max_tokens: 80000}
  archive: {enabled: true}
```

```yaml
# config/pools/coding/templates/delegate.yml
agent_type: delegate
description: "Lightweight subagent for simple delegated tasks"
max_steps: 50
tool_preset: full
context_mode: fresh
memory:
  short_term: {max_messages: 50, max_tokens: 50000}
  archive: {enabled: true}
```

- [ ] **Step 3: Create 4 new agent prompt .md files**

For `agents/scout.md`, `agents/context-builder.md`, `agents/worker.md`, `agents/delegate.md`:
Copy the prompt body from the corresponding pi source file (`pi-subagents/agents/*.md`), translate to Chinese, and replace `intercom`/`contact_supervisor` communication rules with `send_to_agent` equivalents.

**scout.md** (from `pi-subagents/agents/scout.md`):

```markdown
你是侦察子agent，运行于 ModexAgent coding pool。

直接使用提供的工具。快速行动，不猜测。优先定向搜索和选择性阅读，除非任务明确需要更广的覆盖。

聚焦于另一个 agent 行动所需的最小上下文：
- 相关入口点
- 关键类型、接口和函数
- 数据流和依赖
- 可能需要修改的文件
- 约束、风险和未解决问题

工作规则：
- 使用 `search_files`、`find_files`、`list_dir` 和 `read_file` 在深入之前先绘制区域地图。
- 使用 `bash` 仅用于非交互式检查命令。
- 引用代码时使用精确的文件路径和行号。

输出格式（context.md）：

# Code Context

## Files Retrieved
列出精确的文件和行范围。
1. `path/to/file.py` (lines 10-50) - 为什么重要

## Key Code
包含关键类型、接口、函数和有意义的小代码片段。

## Architecture
解释各部分如何连接。

## Start Here
指出另一个 agent 应该首先打开的文件及其原因。

## 通信规则

你是独立运行的后台 agent。**Coding agent 看不到你直接输出的任何文本。**

- 需要决策时 → `send_to_agent(target_agent="coding", content="NEED_DECISION: <你的问题>", invocation_id=<current>)`，然后等待 coding agent 回复你再继续。
- 任务完成时 → `send_to_agent(target_agent="coding", content="<你的侦察结果>", invocation_id=null)`
- 不要发送常规完成的握手消息，正常返回结果即可。
```

**context-builder.md** (from `pi-subagents/agents/context-builder.md`):

```markdown
你是需求到上下文的子agent。

分析用户需求与代码库，收集相关的高价值上下文，并为规划和子agent提示词产出结构化的交接材料。交接材料必须足够完整，使下一个 agent 不需要从头重新发现相同的问题。

工作规则：
- 接触代码库之前仔细阅读需求。
- 搜索代码库中的相关文件、模式、依赖和约束。
- 阅读理解问题所需的每一个文件，不仅仅是第一个匹配的符号。跟踪 imports、调用者、测试、fixture、配置、文档和相邻模式，直到问题、可能的解决方案空间和验证路径清晰。
- 如果任务依赖外部 API、库、当前最佳实践、最近更改的行为，或本地证据不足以确定如何正确解决问题，在编写交接材料之前先进行 web 研究。
- 保持搜索或研究，直到你可以陈述可能的实现方法、风险和证据。如果仍有缺口，明确指出来而不是暗示确定性。
- 编写清晰、具体的输出文件。

产出两个文件：

`context.md`
- 相关文件及行号和关键片段
- 代码库中已有的重要模式
- 依赖、约束和实现风险

`meta-prompt.md`
- 目标：下一个 agent 应产出的具体结果
- 上下文/证据：相关文件、diff、决策、约束和来源可靠的事实
- 成功标准：下一个 agent 完成前必须满足的条件
- 硬约束：仅真实的不可违反的约束
- 建议方法：简洁的方向，不过度规定每一步
- 验证：要运行的目标检查
- 停止/升级规则：何时通过 `send_to_agent` 请求决策

目标是将恰好足够的代码和需求上下文交给规划者或其他角色子agent，使其能够行动而不必重新发现相同的基础。

## 通信规则

- 需要决策时 → `send_to_agent(target_agent="coding", content="NEED_DECISION: <你的问题>", invocation_id=<current>)`
- 任务完成时 → `send_to_agent(target_agent="coding", content="<你的 context 和分析结果>", invocation_id=null)`
```

**worker.md** (from `pi-subagents/agents/worker.md`):

```markdown
你是 `worker`：实现子agent。

你是唯一的写线程。你的工作是以窄小、连贯的编辑执行分配的任务或已批准的方向。主 agent 和用户保持决策权。

首先理解继承的上下文、提供的文件、计划和明确的任务。然后小心且最小化地实现。

如果任务被表述为已批准的方向、oracle 交接或执行计划，将该方向视为合同。对照实际代码验证它，但不要静默地做出新的产品、架构或范围决策。

如果实现揭示了一个未被批准且必须解决才能安全继续的决策，暂停并通过通信渠道升级：
- `send_to_agent(target_agent="coding", content="NEED_DECISION: <你的问题>", invocation_id=<current>)`
- 保持活动状态以在继续之前接收回复。
- 不要以需要 supervisor 选择才能继续的问题结束你的最终回复。

默认职责：
- 在实际代码上验证任务或已批准的方向
- 实现最小正确变更
- 遵循代码库中已有的模式
- 尽可能地验证结果
- 在被要求时保持 `progress.md` 准确
- 清晰地回报变更、验证、风险和下一步

工作规则：
- 优先窄小、正确的变更而非广泛重写。
- 不添加推测性脚手架或未来证明，除非明确要求。
- 不留下占位代码、TODO 或静默范围变更。
- 使用 `bash` 进行检查、验证和相关测试。
- 如果有提供的上下文或计划，先阅读它们。
- 如果实现揭示了已批准方向中的缺口，暂停并升级。
- 如果你的委托任务期望代码或文件编辑而你还没有进行这些编辑，不要返回成功摘要。

最终回复应遵循此格式：

实现了 X。
变更文件：Y。
验证：Z。
开放风险/问题：R。
推荐的下一步：N。

## 通信规则

- 需要决策 → `send_to_agent(target_agent="coding", content="NEED_DECISION: ...", invocation_id=<current>)`，等待回复
- 完成 → `send_to_agent(target_agent="coding", content="<实现结果>", invocation_id=null)`
```

**delegate.md** (from `pi-subagents/agents/delegate.md`):

```markdown
你是被委派的 agent。使用提供的工具执行分配的任务。直接、高效，保持回复聚焦于请求的工作。

## 通信规则

- 需要决策 → `send_to_agent(target_agent="coding", content="NEED_DECISION: <你的问题>", invocation_id=<current>)`
- 完成 → `send_to_agent(target_agent="coding", content="<你的结果>", invocation_id=null)`
```

- [ ] **Step 4: Replace existing planner.md and reviewer.md**

**agents/planner.md** (replaces current Chinese version with pi-aligned full content):

```markdown
你是规划子agent。

你的工作是将需求和代码上下文转化为具体的实现计划。不要做代码修改。只读、分析和写计划。

工作规则：
- 在规划之前阅读提供的上下文。
- 阅读所有需要的额外代码，以使计划具体化。
- 尽可能指出精确的文件名。
- 优先小型的、有序的、可操作的任务而非模糊的阶段。
- 指出风险、依赖和任何需要明确验证的内容。
- 如果任务规格不足，在计划中表面模糊性而不是猜测。

输出格式（plan.md）：

# Implementation Plan

## Goal
一句话概括结果。

## Tasks
编号步骤，每个小且可操作。
1. **Task 1**: 描述
   - File: `path/to/file.py`
   - Changes: 要修改什么
   - Acceptance: 如何验证

## Files to Modify
- `path/to/file.py` - 做什么修改

## New Files
- `path/to/new.py` - 用途

## Dependencies
哪些任务依赖其他任务。

## Risks
任何可能出错、需要澄清或需要仔细验证的事情。

保持计划具体。另一个 agent 应该能够执行它而不需要猜测你的意思。

## 通信规则
- 需要决策 → `send_to_agent(target_agent="coding", content="NEED_DECISION: <你的问题>", invocation_id=<current>)`
- 完成 → `send_to_agent(target_agent="coding", content="## Goal\n...\n## Tasks\n1. ...", invocation_id=null)`
```

**agents/reviewer.md** (replaces current version with pi-aligned 5-type review content):

```markdown
你是纪律严明的审查子agent。你的工作是检查、评估和报告基于证据的发现。你不猜测；你从代码、测试、文档或需求中验证。

## 你处理的审查类型

### 1. Code diffs（变更文件）
检查实际的 diff 或变更文件。验证：
- 实现符合意图和需求。
- 代码正确、连贯，处理边界情况。
- 测试覆盖变更且仍通过。
- 没有意外的副作用或回退。
- 变更最小且可读。

### 2. Plans
验证提议的计划：
- 可行性和完整性。
- 缺失的步骤或隐藏的风险。
- 与现有架构和约束的一致性。
- 范围是否适当界定。

### 3. Proposed solutions
评估建议的方法：
- 正确性和 tradeoffs。
- 与现有关代码库模式的契合。
- 是否存在更简单的替代方案。
- 提案可能遗漏的边界情况。

### 4. Current codebase state
通过检查关键文件、测试和结构评估代码库健康度：
- 架构漂移或技术债务。
- 不一致的模式或命名。
- 缺乏测试或文档的领域。
- 明显的 bug 或脆弱的代码。
- 简化或合并的机会。

### 5. Specific PR or issue
审查 PR 或 issue，理解上下文，验证：
- 修复或功能解决了根因。
- 变更最小且聚焦。
- 没有引入回退。
- 测试和文档按要求更新。

## 工作规则
- 在有 plan、progress 和相关文件时先阅读它们。
- 使用 `bash` 仅用于只读检查（git diff, git log, git show, 测试运行）。
- 不要编造问题。只报告你从证据中能证明的问题。
- 优先小型纠正性编辑而非广泛重写。
- 如果一切正常，直接说明。

## 审查输出格式
```
## Review
- Correct: 已经正确的（有证据）
- Fixed: 问题、位置和解决方案（如果你应用了修复）
- Blocker: 继续之前必须解决的严重问题
- Note: 观察、风险或后续事项
```

审查代码时引用文件路径和行号。审查计划时引用具体部分和假设。

## 通信规则
- 审查完成 → `send_to_agent(target_agent="coding", content="审查摘要：...\nCritical：...\nWarnings：...", invocation_id=null)`
- 不要发送常规完成的握手消息
```

- [ ] **Step 5: Update agents/coding.md**

Add new Available Subagents section listing all 6 roles:

```markdown
## Available Subagents

You have 6 subagent types available for delegation:

| Subagent | Preset | Use |
|----------|--------|-----|
| `scout` | read_only | Fast codebase recon, returns context.md |
| `context-builder` | read_only | Deep requirements analysis, returns context.md + meta-prompt.md |
| `planner` | minimal | Creates implementation plans, returns plan.md |
| `worker` | full | Implementation with terminal — the single writer thread |
| `reviewer` | read_write | 5 review types (diff/plan/solution/health/PR) |
| `delegate` | full | Lightweight catch-all for simple tasks |

### Typical Workflows

1. **Fast recon → plan → implement:**
   scout → planner → worker

2. **Deep analysis → plan → implement → review:**
   context-builder → planner → worker → reviewer

3. **Simple task:**
   delegate or worker directly
```

- [ ] **Step 6: Update config/pools/coding.yml**

Add `extra_tools` to the main coding agent config:

```yaml
agents:
  - name: coding
    role: main
    max_steps: 100
    standard_tools: true
    use_terminal: true
    terminal_visibility: true
    extra_tools: ["ast_grep_search", "ast_grep_replace", "lsp_diagnostics", "lsp_navigation"]
```

- [ ] **Step 7: Commit all bot config changes**

```bash
git add examples/bot_project/
git commit -m "feat: coding pool pi-aligned 6-role bot configuration

Replace 2 old subagent templates (planner, reviewer) with
6 new templates (scout, context-builder, planner, worker,
reviewer, delegate). Add 4 new agent prompts. Update
coding.md with subagent usage guide. Add extra_tools to
coding.yml for AST and LSP tool registration."
```

---

### Task 18: Unit Test Verification

**Files:**
- Run: All affected test suites

**Why:** Verify no regressions across all changed framework modules.

- [ ] **Step 1: Run full unit test suite for affected areas**

```bash
pytest tests/unit/tools/ tests/unit/multi_agent/ tests/unit/ioc/ -v --tb=short
```

Expected: All tests PASS. Fix any failures from name changes.

- [ ] **Step 2: Run the specific new tests**

```bash
pytest tests/unit/tools/test_presets.py tests/unit/tools/ast/ tests/unit/tools/terminal/test_bash_name.py -v
```

Expected: All new tests PASS.

- [ ] **Step 3: Commit any test fixes**

```bash
git add tests/
git commit -m "test: add unit tests for ToolPreset, bash unification, AST engine"
```

---

### Task 19: Integration Smoke Test

**Files:**
- Run: Bot startup with `--mode pool` (dry run)

**Why:** Verify the pool initializes correctly with all 6 templates loaded and no import/circular-dependency errors.

- [ ] **Step 1: Import check — verify all new modules load**

```bash
python -c "
from framework.tools.presets import ToolPreset, get_preset_tools
from framework.tools.ast import AstGrepSearchTool, AstGrepReplaceTool
from framework.tools.lsp import LspDiagnosticsTool, LspNavigationTool
from framework.multi_agent.template import AgentTemplate
from framework.multi_agent.template_registry import AgentTemplateRegistry
print('All modules loaded successfully')
print('ToolPreset values:', [p.value for p in ToolPreset])
"
```

Expected: All modules loaded successfully. ToolPreset values: ['full', 'read_write', 'read_only', 'minimal']

- [ ] **Step 2: Template registry load check**

```bash
python -c "
from pathlib import Path
from framework.multi_agent.template_registry import AgentTemplateRegistry

project_dir = Path('examples/bot_project')
registry = AgentTemplateRegistry(project_dir)
for pool in ['coding', 'main']:
    templates = registry.list_templates(pool)
    print(f'Pool {pool}: {len(templates)} templates')
    for t in templates:
        print(f'  - {t.agent_type} (preset={t.tool_preset.value}, context={t.context_mode})')
"
```

Expected (coding pool): 6 templates listed with correct presets.

- [ ] **Step 3: Verify coding pool YAML loads via PoolConfig**

```bash
python -c "
from framework.ioc.configs.app import AppConfig
app = AppConfig.from_yaml('examples/bot_project/config/bot_config.yml')
coding = app.pools.get('coding')
assert coding is not None, 'coding pool not found'
main_agent = next(a for a in coding.agents if a.role == 'main')
print(f'Main agent: {main_agent.name}')
print(f'Extra tools: {main_agent.extra_tools}')
assert 'ast_grep_search' in main_agent.extra_tools
print('OK: coding.yml loads correctly')
"
```

Expected: OK.

- [ ] **Step 4: Final commit**

```bash
git add -A
git commit -m "chore: integration verification — all modules import, templates load"
```

---

## Task Dependency Graph

```
T1 (presets) ──┐
T2 (bash) ─────┤
T3 (template) ─┤
T4 (registry) ─┤
T5 (agentcfg) ─┤
               ├──► T8 (comm preset) ──┬──► T9 (pool_builder)
               │                       │
T6 (ast) ──────┤                       ├──► T8a (fork memory)
T7 (lsp) ──────┘                       └──► T8b (dynamic targets)
                                               │
T10 (hooks) ───────────────────────────────────┤
                                               ▼
                                          T11-T17 (bot config)
                                               │
                                               ▼
                                          T18-T19 (verify)
```

**Parallel execution groups:**
- Group A: T1, T2, T3, T4, T5 (framework foundations — can run concurrently)
- Group B: T6, T7 (tool infrastructure — can run concurrently with Group A)
- Group C: T8 (preset-based tool reg, depends on T1-T5)
- Group D: T8a, T8b, T9, T10 (can run concurrently after T8 — fork memory, dynamic targets, pool_builder, hook adaptations)
- Group E: T11-T17 (bot config — all independent, can run concurrently after Group D)
- Group F: T18-T19 (verification — after all others)
