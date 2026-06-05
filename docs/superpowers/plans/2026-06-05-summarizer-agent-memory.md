# Summarizer Agent Memory System Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Transform SummarizerAgent and DreamEngine into real agents with scoped file tools, replacing JSONL archive storage with MD directory model.

**Architecture:** Two new agents (ArchiveSummarizer, KnowledgeConsolidator) reuse the existing ReAct engine with four scoped file tools. Archive storage changes from channel-based JSONL to per-archive-id directories containing context.md, knowledge.md, index.md. Cleanup flow becomes a four-step process with idempotent archive generation before session pruning.

**Tech Stack:** Python 3.11+, asyncio, ReAct engine, Path-based file I/O

**Spec:** `docs/superpowers/specs/2026-06-05-summarizer-agent-memory-design.md`

---

## File Structure

### New Files

| File | Responsibility |
|------|---------------|
| `framework/memory/tools/__init__.py` | Package init, exports |
| `framework/memory/tools/scoped_read.py` | `ScopedReadFileTool` — read files within allowed dirs |
| `framework/memory/tools/scoped_write.py` | `ScopedWriteFileTool` — write/create files within allowed dirs |
| `framework/memory/tools/scoped_edit.py` | `ScopedEditFileTool` — string-replace edit within allowed dirs |
| `framework/memory/tools/scoped_list.py` | `ScopedListTool` — list directory within allowed dirs |
| `framework/memory/stores/dir_archive.py` | `DirArchiveStorage` — MD directory archive storage implementation |
| `framework/agents/summarizer/archive_agent.py` | `ArchiveSummarizer` — archive generation agent |
| `framework/agents/summarizer/consolidator.py` | `KnowledgeConsolidator` — knowledge update agent |
| `framework/memory/prompts/archive/agent_system.md` | System prompt for ArchiveSummarizer |
| `framework/memory/prompts/knowledge/consolidator_system.md` | System prompt for KnowledgeConsolidator |
| `tests/unit/memory/tools/test_scoped_read.py` | Tests for ScopedReadFileTool |
| `tests/unit/memory/tools/test_scoped_write.py` | Tests for ScopedWriteFileTool |
| `tests/unit/memory/tools/test_scoped_edit.py` | Tests for ScopedEditFileTool |
| `tests/unit/memory/tools/test_scoped_list.py` | Tests for ScopedListTool |
| `tests/unit/memory/stores/test_dir_archive.py` | Tests for DirArchiveStorage |
| `tests/unit/memory/test_archive_agent.py` | Tests for ArchiveSummarizer |
| `tests/unit/memory/test_consolidator.py` | Tests for KnowledgeConsolidator |

### Modified Files

| File | Change |
|------|--------|
| `framework/memory/layers/archive.py` | `ScopedArchiveMemoryManager` uses `DirArchiveStorage` instead of JSONL |
| `framework/memory/layers/config.py` | Add `SummarizerConfig` for agent parameters |
| `framework/memory/cleanup.py` | Four-step flow, `archive_agent` parameter |
| `framework/memory/injection/full_injection.py` | `_inject_archive` reads MD via `ArchiveStorage` protocol |
| `framework/memory/pruned/manager.py` | Full refresh from archive index.md files |
| `framework/memory/consolidation/dream_engine.py` | Use `KnowledgeConsolidator` agent |
| `framework/memory/prompts/__init__.py` | Register new prompt templates |
| `framework/memory/system.py` | Build `ArchiveSummarizer` + `KnowledgeConsolidator` |
| `framework/memory/__init__.py` | Export new modules |
| `framework/ioc/factories/memory.py` | Wire new agents into memory creation |
| `framework/ioc/configs/memory.py` | Add summarizer config section |
| `examples/bot_project/bot/service/core.py` | Adapt workspace rebuild for new components |

---

## Phase 1: ScopedFileTools

Foundation: four tools that validate paths against a whitelist before performing file operations.

### Task 1: ScopedReadFileTool

**Files:**
- Create: `framework/memory/tools/__init__.py`
- Create: `framework/memory/tools/scoped_read.py`
- Create: `tests/unit/memory/tools/__init__.py`
- Create: `tests/unit/memory/tools/test_scoped_read.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/unit/memory/tools/test_scoped_read.py
"""Tests for ScopedReadFileTool."""
from __future__ import annotations

import pytest
from pathlib import Path

from framework.memory.tools.scoped_read import ScopedReadFileTool


@pytest.fixture
def allowed_dir(tmp_path: Path) -> Path:
    """Create a temp directory with a test file."""
    (tmp_path / "test.txt").write_text("hello world", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "nested.md").write_text("nested content", encoding="utf-8")
    return tmp_path


@pytest.mark.asyncio
async def test_read_file_within_allowed_dir(allowed_dir: Path) -> None:
    tool = ScopedReadFileTool(allowed_dirs=[allowed_dir])
    result = await tool.execute(path=str(allowed_dir / "test.txt"))
    assert result.success
    assert "hello world" in result.output

@pytest.mark.asyncio
async def test_read_nested_file_within_allowed_dir(allowed_dir: Path) -> None:
    tool = ScopedReadFileTool(allowed_dirs=[allowed_dir])
    result = await tool.execute(path=str(allowed_dir / "sub" / "nested.md"))
    assert result.success
    assert "nested content" in result.output

@pytest.mark.asyncio
async def test_read_file_outside_allowed_dir(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret", encoding="utf-8")
    tool = ScopedReadFileTool(allowed_dirs=[allowed])
    result = await tool.execute(path=str(outside / "secret.txt"))
    assert not result.success
    assert str(allowed) in result.output

@pytest.mark.asyncio
async def test_read_nonexistent_file(allowed_dir: Path) -> None:
    tool = ScopedReadFileTool(allowed_dirs=[allowed_dir])
    result = await tool.execute(path=str(allowed_dir / "missing.txt"))
    assert not result.success

@pytest.mark.asyncio
async def test_description_does_not_contain_paths(tmp_path: Path) -> None:
    tool = ScopedReadFileTool(allowed_dirs=[tmp_path])
    desc = tool.description
    assert str(tmp_path) not in desc
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/unit/memory/tools/test_scoped_read.py -v`
Expected: FAIL — module `framework.memory.tools.scoped_read` not found

- [ ] **Step 3: Create package init**

```python
# framework/memory/tools/__init__.py
"""Scoped file tools for memory agents."""
from __future__ import annotations

from framework.memory.tools.scoped_read import ScopedReadFileTool
from framework.memory.tools.scoped_write import ScopedWriteFileTool
from framework.memory.tools.scoped_edit import ScopedEditFileTool
from framework.memory.tools.scoped_list import ScopedListTool

__all__ = [
    "ScopedEditFileTool",
    "ScopedListTool",
    "ScopedReadFileTool",
    "ScopedWriteFileTool",
]
```

- [ ] **Step 4: Implement ScopedReadFileTool**

```python
# framework/memory/tools/scoped_read.py
"""Scoped file read tool — only allows reading within whitelisted directories."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from framework.core.tool_manager import Tool, ToolResult

logger = logging.getLogger(__name__)


class ScopedReadFileTool(Tool):
    """Read a file. Only files within allowed_dirs are accessible."""

    name: str = "read_file"

    def __init__(self, allowed_dirs: list[Path]) -> None:
        self._allowed_dirs = [d.resolve() for d in allowed_dirs]
        self.description = "Read the content of a file. Only files in allowed directories are accessible."

    def _validate_path(self, raw_path: str) -> Path:
        """Validate and resolve path. Raises ValueError if outside allowed dirs."""
        resolved = Path(raw_path).resolve()
        for allowed in self._allowed_dirs:
            try:
                resolved.relative_to(allowed)
                return resolved
            except ValueError:
                continue
        allowed_str = "\n".join(f"  - {d}" for d in self._allowed_dirs)
        raise ValueError(
            f"Path '{raw_path}' is outside allowed directories.\n"
            f"Allowed directories:\n{allowed_str}"
        )

    async def execute(self, *, path: str, **kwargs: Any) -> ToolResult:
        try:
            resolved = self._validate_path(path)
        except ValueError as e:
            return ToolResult(success=False, output=str(e))
        if not resolved.exists():
            return ToolResult(success=False, output=f"File not found: {path}")
        if not resolved.is_file():
            return ToolResult(success=False, output=f"Not a file: {path}")
        try:
            content = resolved.read_text(encoding="utf-8")
            return ToolResult(success=True, output=content)
        except Exception as e:
            return ToolResult(success=False, output=f"Failed to read file: {e}")
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/unit/memory/tools/test_scoped_read.py -v`
Expected: 5 passed

- [ ] **Step 6: Commit**

```bash
git add framework/memory/tools/ tests/unit/memory/tools/
git commit -m "feat(memory): add ScopedReadFileTool with path validation"
```

### Task 2: ScopedWriteFileTool

**Files:**
- Create: `framework/memory/tools/scoped_write.py`
- Create: `tests/unit/memory/tools/test_scoped_write.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/unit/memory/tools/test_scoped_write.py
"""Tests for ScopedWriteFileTool."""
from __future__ import annotations

import pytest
from pathlib import Path

from framework.memory.tools.scoped_write import ScopedWriteFileTool


@pytest.fixture
def allowed_dir(tmp_path: Path) -> Path:
    d = tmp_path / "allowed"
    d.mkdir()
    return d


@pytest.mark.asyncio
async def test_write_new_file(allowed_dir: Path) -> None:
    tool = ScopedWriteFileTool(allowed_dirs=[allowed_dir])
    result = await tool.execute(path=str(allowed_dir / "new.md"), content="hello")
    assert result.success
    assert (allowed_dir / "new.md").read_text() == "hello"

@pytest.mark.asyncio
async def test_overwrite_existing_file(allowed_dir: Path) -> None:
    (allowed_dir / "existing.md").write_text("old", encoding="utf-8")
    tool = ScopedWriteFileTool(allowed_dirs=[allowed_dir])
    result = await tool.execute(path=str(allowed_dir / "existing.md"), content="new")
    assert result.success
    assert (allowed_dir / "existing.md").read_text() == "new"

@pytest.mark.asyncio
async def test_write_outside_allowed_dir(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    tool = ScopedWriteFileTool(allowed_dirs=[allowed])
    result = await tool.execute(path=str(outside / "bad.md"), content="x")
    assert not result.success
    assert str(allowed) in result.output

@pytest.mark.asyncio
async def test_write_creates_parent_dirs(allowed_dir: Path) -> None:
    tool = ScopedWriteFileTool(allowed_dirs=[allowed_dir])
    result = await tool.execute(path=str(allowed_dir / "sub" / "deep.md"), content="deep")
    assert result.success
    assert (allowed_dir / "sub" / "deep.md").read_text() == "deep"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/unit/memory/tools/test_scoped_write.py -v`
Expected: FAIL

- [ ] **Step 3: Implement ScopedWriteFileTool**

```python
# framework/memory/tools/scoped_write.py
"""Scoped file write tool — only allows writing within whitelisted directories."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from framework.core.tool_manager import Tool, ToolResult

logger = logging.getLogger(__name__)


class ScopedWriteFileTool(Tool):
    """Write content to a file. Only files within allowed_dirs are writable."""

    name: str = "write_file"

    def __init__(self, allowed_dirs: list[Path]) -> None:
        self._allowed_dirs = [d.resolve() for d in allowed_dirs]
        self.description = "Write content to a file. Only files in allowed directories are writable."

    def _validate_path(self, raw_path: str) -> Path:
        resolved = Path(raw_path).resolve()
        for allowed in self._allowed_dirs:
            try:
                resolved.relative_to(allowed)
                return resolved
            except ValueError:
                continue
        allowed_str = "\n".join(f"  - {d}" for d in self._allowed_dirs)
        raise ValueError(
            f"Path '{raw_path}' is outside allowed directories.\n"
            f"Allowed directories:\n{allowed_str}"
        )

    async def execute(self, *, path: str, content: str, **kwargs: Any) -> ToolResult:
        try:
            resolved = self._validate_path(path)
        except ValueError as e:
            return ToolResult(success=False, output=str(e))
        try:
            resolved.parent.mkdir(parents=True, exist_ok=True)
            resolved.write_text(content, encoding="utf-8")
            return ToolResult(success=True, output=f"Wrote {len(content)} chars to {path}")
        except Exception as e:
            return ToolResult(success=False, output=f"Failed to write file: {e}")
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/unit/memory/tools/test_scoped_write.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add framework/memory/tools/scoped_write.py tests/unit/memory/tools/test_scoped_write.py
git commit -m "feat(memory): add ScopedWriteFileTool"
```

### Task 3: ScopedEditFileTool

**Files:**
- Create: `framework/memory/tools/scoped_edit.py`
- Create: `tests/unit/memory/tools/test_scoped_edit.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/unit/memory/tools/test_scoped_edit.py
"""Tests for ScopedEditFileTool."""
from __future__ import annotations

import pytest
from pathlib import Path

from framework.memory.tools.scoped_edit import ScopedEditFileTool


@pytest.fixture
def allowed_dir(tmp_path: Path) -> Path:
    d = tmp_path / "allowed"
    d.mkdir()
    (d / "file.md").write_text("hello old world", encoding="utf-8")
    return d


@pytest.mark.asyncio
async def test_edit_replace_text(allowed_dir: Path) -> None:
    tool = ScopedEditFileTool(allowed_dirs=[allowed_dir])
    result = await tool.execute(
        path=str(allowed_dir / "file.md"),
        old_text="old",
        new_text="new",
    )
    assert result.success
    assert (allowed_dir / "file.md").read_text() == "hello new world"

@pytest.mark.asyncio
async def test_edit_text_not_found(allowed_dir: Path) -> None:
    tool = ScopedEditFileTool(allowed_dirs=[allowed_dir])
    result = await tool.execute(
        path=str(allowed_dir / "file.md"),
        old_text="missing",
        new_text="replacement",
    )
    assert not result.success

@pytest.mark.asyncio
async def test_edit_outside_allowed_dir(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "f.md").write_text("x", encoding="utf-8")
    tool = ScopedEditFileTool(allowed_dirs=[allowed])
    result = await tool.execute(path=str(outside / "f.md"), old_text="x", new_text="y")
    assert not result.success

@pytest.mark.asyncio
async def test_edit_nonexistent_file(allowed_dir: Path) -> None:
    tool = ScopedEditFileTool(allowed_dirs=[allowed_dir])
    result = await tool.execute(
        path=str(allowed_dir / "missing.md"),
        old_text="a",
        new_text="b",
    )
    assert not result.success
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/unit/memory/tools/test_scoped_edit.py -v`
Expected: FAIL

- [ ] **Step 3: Implement ScopedEditFileTool**

```python
# framework/memory/tools/scoped_edit.py
"""Scoped file edit tool — string replacement within whitelisted directories."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from framework.core.tool_manager import Tool, ToolResult

logger = logging.getLogger(__name__)


class ScopedEditFileTool(Tool):
    """Edit a file by replacing text. Only files within allowed_dirs are editable."""

    name: str = "edit_file"

    def __init__(self, allowed_dirs: list[Path]) -> None:
        self._allowed_dirs = [d.resolve() for d in allowed_dirs]
        self.description = "Edit a file by replacing old_text with new_text. Only files in allowed directories are editable."

    def _validate_path(self, raw_path: str) -> Path:
        resolved = Path(raw_path).resolve()
        for allowed in self._allowed_dirs:
            try:
                resolved.relative_to(allowed)
                return resolved
            except ValueError:
                continue
        allowed_str = "\n".join(f"  - {d}" for d in self._allowed_dirs)
        raise ValueError(
            f"Path '{raw_path}' is outside allowed directories.\n"
            f"Allowed directories:\n{allowed_str}"
        )

    async def execute(
        self, *, path: str, old_text: str, new_text: str, **kwargs: Any
    ) -> ToolResult:
        try:
            resolved = self._validate_path(path)
        except ValueError as e:
            return ToolResult(success=False, output=str(e))
        if not resolved.exists():
            return ToolResult(success=False, output=f"File not found: {path}")
        try:
            content = resolved.read_text(encoding="utf-8")
            if old_text not in content:
                return ToolResult(
                    success=False,
                    output=f"old_text not found in {path}",
                )
            new_content = content.replace(old_text, new_text, 1)
            resolved.write_text(new_content, encoding="utf-8")
            return ToolResult(success=True, output=f"Edited {path}")
        except Exception as e:
            return ToolResult(success=False, output=f"Failed to edit file: {e}")
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/unit/memory/tools/test_scoped_edit.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add framework/memory/tools/scoped_edit.py tests/unit/memory/tools/test_scoped_edit.py
git commit -m "feat(memory): add ScopedEditFileTool"
```

### Task 4: ScopedListTool

**Files:**
- Create: `framework/memory/tools/scoped_list.py`
- Create: `tests/unit/memory/tools/test_scoped_list.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/unit/memory/tools/test_scoped_list.py
"""Tests for ScopedListTool."""
from __future__ import annotations

import pytest
from pathlib import Path

from framework.memory.tools.scoped_list import ScopedListTool


@pytest.fixture
def allowed_dir(tmp_path: Path) -> Path:
    d = tmp_path / "allowed"
    d.mkdir()
    (d / "a.md").write_text("a", encoding="utf-8")
    (d / "b.md").write_text("b", encoding="utf-8")
    (d / "sub").mkdir()
    (d / "sub" / "c.md").write_text("c", encoding="utf-8")
    return d


@pytest.mark.asyncio
async def test_list_directory(allowed_dir: Path) -> None:
    tool = ScopedListTool(allowed_dirs=[allowed_dir])
    result = await tool.execute(path=str(allowed_dir))
    assert result.success
    assert "a.md" in result.output
    assert "b.md" in result.output

@pytest.mark.asyncio
async def test_list_subdirectory(allowed_dir: Path) -> None:
    tool = ScopedListTool(allowed_dirs=[allowed_dir])
    result = await tool.execute(path=str(allowed_dir / "sub"))
    assert result.success
    assert "c.md" in result.output

@pytest.mark.asyncio
async def test_list_outside_allowed_dir(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    tool = ScopedListTool(allowed_dirs=[allowed])
    result = await tool.execute(path=str(outside))
    assert not result.success
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/unit/memory/tools/test_scoped_list.py -v`
Expected: FAIL

- [ ] **Step 3: Implement ScopedListTool**

```python
# framework/memory/tools/scoped_list.py
"""Scoped directory listing tool — only within whitelisted directories."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from framework.core.tool_manager import Tool, ToolResult

logger = logging.getLogger(__name__)


class ScopedListTool(Tool):
    """List files in a directory. Only directories within allowed_dirs are accessible."""

    name: str = "list_dir"

    def __init__(self, allowed_dirs: list[Path]) -> None:
        self._allowed_dirs = [d.resolve() for d in allowed_dirs]
        self.description = "List files in a directory. Only directories in allowed paths are accessible."

    def _validate_path(self, raw_path: str) -> Path:
        resolved = Path(raw_path).resolve()
        for allowed in self._allowed_dirs:
            try:
                resolved.relative_to(allowed)
                return resolved
            except ValueError:
                continue
        allowed_str = "\n".join(f"  - {d}" for d in self._allowed_dirs)
        raise ValueError(
            f"Path '{raw_path}' is outside allowed directories.\n"
            f"Allowed directories:\n{allowed_str}"
        )

    async def execute(self, *, path: str, **kwargs: Any) -> ToolResult:
        try:
            resolved = self._validate_path(path)
        except ValueError as e:
            return ToolResult(success=False, output=str(e))
        if not resolved.exists():
            return ToolResult(success=False, output=f"Directory not found: {path}")
        if not resolved.is_dir():
            return ToolResult(success=False, output=f"Not a directory: {path}")
        try:
            entries = sorted(resolved.iterdir())
            lines = []
            for entry in entries:
                kind = "dir" if entry.is_dir() else "file"
                lines.append(f"  {kind}  {entry.name}")
            output = f"Contents of {path}:\n" + "\n".join(lines)
            return ToolResult(success=True, output=output)
        except Exception as e:
            return ToolResult(success=False, output=f"Failed to list directory: {e}")
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/unit/memory/tools/test_scoped_list.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add framework/memory/tools/scoped_list.py tests/unit/memory/tools/test_scoped_list.py
git commit -m "feat(memory): add ScopedListTool"
```

### Task 5: Run all Phase 1 tests together

- [ ] **Step 1: Run full tool test suite**

Run: `python -m pytest tests/unit/memory/tools/ -v`
Expected: All 16 tests pass

- [ ] **Step 2: Run existing memory tests to verify no regressions**

Run: `python -m pytest tests/unit/memory/ -v --timeout=30`
Expected: All existing tests pass (no changes to existing code yet)

---

## Phase 2: DirArchiveStorage

New MD-directory-based archive storage implementing the `ArchiveChannelStorage` protocol.

### Task 6: DirArchiveStorage implementation

**Files:**
- Create: `framework/memory/stores/dir_archive.py`
- Create: `tests/unit/memory/stores/test_dir_archive.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/unit/memory/stores/test_dir_archive.py
"""Tests for DirArchiveStorage — MD directory archive backend."""
from __future__ import annotations

import json
import pytest
from pathlib import Path

from framework.memory.archive_models import ArchiveChannel, ArchiveState, ArchiveWrite
from framework.memory.stores.dir_archive import DirArchiveStorage


@pytest.fixture
def storage(tmp_path: Path) -> DirArchiveStorage:
    return DirArchiveStorage(base_dir=tmp_path)


@pytest.mark.asyncio
async def test_read_state_default(storage: DirArchiveStorage) -> None:
    state = await storage.read_archive_state()
    assert state is None

@pytest.mark.asyncio
async def test_write_and_read_state(storage: DirArchiveStorage) -> None:
    state = ArchiveState(next_archive_id=5, knowledge_consumed_archive_id=3)
    await storage.write_archive_state(state_to_dict(state))
    result = await storage.read_archive_state()
    assert result is not None
    assert result["next_archive_id"] == 5

@pytest.mark.asyncio
async def test_append_and_read_channel_log(storage: DirArchiveStorage) -> None:
    write = ArchiveWrite(
        channel=ArchiveChannel.CONTEXT,
        summary="test context summary",
        metadata={"reason": "message_count"},
    )
    entry = await storage.append_channel_log("context", {"summary": write.summary, "metadata": dict(write.metadata)})
    assert entry["archive_id"] == 1

    write2 = ArchiveWrite(
        channel=ArchiveChannel.CONTEXT,
        summary="second summary",
        metadata={"reason": "token_pressure"},
    )
    entry2 = await storage.append_channel_log("context", {"summary": write2.summary, "metadata": dict(write2.metadata)})
    assert entry2["archive_id"] == 2

@pytest.mark.asyncio
async def test_read_channel_logs_since_id(storage: DirArchiveStorage) -> None:
    for i in range(3):
        await storage.append_channel_log("context", {"summary": f"summary {i}", "metadata": {}})
    logs = await storage.read_channel_logs("context", since_archive_id=1)
    assert len(logs) == 2

@pytest.mark.asyncio
async def test_archive_dir_created_on_append(tmp_path: Path) -> None:
    storage = DirArchiveStorage(base_dir=tmp_path)
    await storage.append_channel_log("context", {"summary": "test", "metadata": {}})
    assert (tmp_path / "1").is_dir()

@pytest.mark.asyncio
async def test_write_and_read_archive_md_files(tmp_path: Path) -> None:
    storage = DirArchiveStorage(base_dir=tmp_path)
    archive_id = await storage.write_archive_file(1, "context.md", "## Context\nTest summary")
    assert archive_id == 1
    content = await storage.read_archive_file(1, "context.md")
    assert content == "## Context\nTest summary"

@pytest.mark.asyncio
async def test_read_nonexistent_archive_file(storage: DirArchiveStorage) -> None:
    content = await storage.read_archive_file(999, "context.md")
    assert content is None

@pytest.mark.asyncio
async def test_list_archives(tmp_path: Path) -> None:
    storage = DirArchiveStorage(base_dir=tmp_path)
    await storage.write_archive_file(3, "context.md", "c3")
    await storage.write_archive_file(1, "context.md", "c1")
    await storage.write_archive_file(5, "context.md", "c5")
    ids = await storage.list_archives()
    assert ids == [5, 3, 1]  # descending

@pytest.mark.asyncio
async def test_list_archives_since_id(tmp_path: Path) -> None:
    storage = DirArchiveStorage(base_dir=tmp_path)
    for i in [1, 2, 3, 4, 5]:
        await storage.write_archive_file(i, "context.md", f"c{i}")
    ids = await storage.list_archives(since_id=2)
    assert ids == [5, 4, 3]  # > 2, descending

@pytest.mark.asyncio
async def test_is_archive_complete(tmp_path: Path) -> None:
    storage = DirArchiveStorage(base_dir=tmp_path)
    assert not await storage.is_archive_complete(1)
    await storage.write_archive_file(1, "context.md", "ctx")
    await storage.write_archive_file(1, "knowledge.md", "kn")
    await storage.write_archive_file(1, "index.md", "idx")
    assert await storage.is_archive_complete(1)


def state_to_dict(state: ArchiveState) -> dict:
    return {"next_archive_id": state.next_archive_id, "knowledge_consumed_archive_id": state.knowledge_consumed_archive_id}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/unit/memory/stores/test_dir_archive.py -v`
Expected: FAIL

- [ ] **Step 3: Implement DirArchiveStorage**

```python
# framework/memory/stores/dir_archive.py
"""MD-directory-based archive storage.

Archive layout:
  {base_dir}/
    state.json                  ← {next_archive_id, ...}
    1/context.md, knowledge.md, index.md
    2/context.md, knowledge.md, index.md
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from framework.memory.archive_models import ArchiveChannelStorage

logger = logging.getLogger(__name__)

_STATE_FILE = "state.json"
_REQUIRED_FILES = frozenset({"context.md", "knowledge.md", "index.md"})


class DirArchiveStorage:
    """File-based archive storage using per-ID directories with MD files."""

    def __init__(self, base_dir: Path) -> None:
        self._base = base_dir

    @property
    def base_dir(self) -> Path:
        return self._base

    @property
    def directory(self) -> Path:
        """Alias for base_dir, used by DefaultMemorySystem.get_archive_directory."""
        return self._base

    def _state_path(self) -> Path:
        return self._base / _STATE_FILE

    def _archive_dir(self, archive_id: int) -> Path:
        return self._base / str(archive_id)

    # -- ArchiveChannelStorage protocol --

    async def read_archive_state(self) -> dict[str, Any] | None:
        path = self._state_path()
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            logger.warning("Failed to read archive state", exc_info=True)
            return None

    async def write_archive_state(self, state: dict[str, Any]) -> None:
        self._base.mkdir(parents=True, exist_ok=True)
        self._state_path().write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    async def append_channel_log(
        self, channel: str, entry: dict[str, Any]
    ) -> dict[str, Any]:
        """Append entry, creating archive dir if needed. Returns stored record."""
        state_data = await self.read_archive_state() or {}
        next_id = state_data.get("next_archive_id", 1)

        archive_dir = self._archive_dir(next_id)
        archive_dir.mkdir(parents=True, exist_ok=True)

        record = {**entry, "archive_id": next_id, "channel": channel}

        # Write channel content as MD if summary is present
        summary = entry.get("summary", "")
        if summary:
            filename = f"{channel}.md"
            (archive_dir / filename).write_text(summary, encoding="utf-8")

        return record

    async def read_channel_logs(
        self,
        channel: str,
        since_archive_id: int = 0,
        limit: int = 1_000_000,
    ) -> list[dict[str, Any]]:
        """Read channel logs as list of dicts (for backward compat)."""
        results: list[dict[str, Any]] = []
        for archive_id in sorted(
            int(d.name) for d in self._base.iterdir()
            if d.is_dir() and d.name.isdigit() and int(d.name) > since_archive_id
        ):
            md_path = self._archive_dir(archive_id) / f"{channel}.md"
            if md_path.exists():
                content = md_path.read_text(encoding="utf-8")
                results.append({
                    "archive_id": archive_id,
                    "channel": channel,
                    "summary": content,
                    "metadata": {},
                })
            if len(results) >= limit:
                break
        return results

    async def save_channel_logs(
        self, channel: str, entries: list[dict[str, Any]]
    ) -> None:
        """Not used in new model — kept for protocol compliance."""
        _ = channel, entries

    # -- MD file operations --

    async def write_archive_file(self, archive_id: int, filename: str, content: str) -> int:
        """Write a file to an archive directory."""
        archive_dir = self._archive_dir(archive_id)
        archive_dir.mkdir(parents=True, exist_ok=True)
        (archive_dir / filename).write_text(content, encoding="utf-8")
        return archive_id

    async def read_archive_file(self, archive_id: int, filename: str) -> str | None:
        """Read a file from an archive directory."""
        path = self._archive_dir(archive_id) / filename
        if not path.exists():
            return None
        return path.read_text(encoding="utf-8")

    async def list_archives(self, since_id: int = 0, limit: int = 100) -> list[int]:
        """List archive IDs > since_id, descending order."""
        if not self._base.exists():
            return []
        ids = sorted(
            int(d.name) for d in self._base.iterdir()
            if d.is_dir() and d.name.isdigit() and int(d.name) > since_id
        )
        return list(reversed(ids[-limit:]))

    async def is_archive_complete(self, archive_id: int) -> bool:
        """Check if an archive directory has all three required MD files."""
        archive_dir = self._archive_dir(archive_id)
        if not archive_dir.exists():
            return False
        for fname in _REQUIRED_FILES:
            fpath = archive_dir / fname
            if not fpath.exists() or fpath.stat().st_size == 0:
                return False
        return True
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/unit/memory/stores/test_dir_archive.py -v`
Expected: 11 passed

- [ ] **Step 5: Commit**

```bash
git add framework/memory/stores/dir_archive.py tests/unit/memory/stores/test_dir_archive.py
git commit -m "feat(memory): add DirArchiveStorage for MD directory model"
```

---

## Phase 3: ArchiveSummarizer Agent

### Task 7: Archive Summarizer prompt templates

**Files:**
- Create: `framework/memory/prompts/archive/agent_system.md`

- [ ] **Step 1: Write the system prompt**

```markdown
<!-- framework/memory/prompts/archive/agent_system.md -->
You are an archive summarization agent. Your task is to analyze a conversation transcript and write summary files.

## Allowed Directories
You can ONLY read and write files in the directories listed in the user message.

## Output Files

### context.md
Conversation summary for context injection into future sessions.
Structure:
- ## Situation: The compressed historical task or topic
- ## Decisions: Confirmed decisions affecting future work
- ## Completed Work: Completed actions, results, tests, files
- ## Open Threads: Unfinished items (mark uncertain as possibly stale)
- ## Evidence: Key tool results, errors, paths, verification outcomes

Max {context_max_chars} characters (default 500). Write empty file if no useful context.

### knowledge.md
Durable memory candidates extracted from the transcript.
Structure:
- ## User Facts: Stable user identity, preferences, corrections
- ## Project Facts: Stable project structure, rules, configuration
- ## Decisions: Confirmed design or implementation decisions
- ## Reusable Lessons: Verified solutions, recurring patterns
- Do NOT capture negative claims; capture the FIX instead

Max {knowledge_max_chars} characters (default 600). Write empty file if no durable candidates.

### index.md
Ultra-concise index entry for the pruned catalog.
- 1-3 lines, max {index_max_chars} characters (default 100)
- Format: one-line topic description + time range
- This is a search index entry — keep it MINIMAL

## Execution Rules
- This is a SINGLE-TURN task. You receive the transcript once, write the files, then stop.
- No further user input will follow. Do your best analysis now.
- Write all three files using the provided tools, then stop.
- If a file would have no useful content, write an empty file.
- Work fast — minimize tool call rounds. Ideally write all files in one pass.
- Output ONLY file content via tool calls. Do not output analysis as text.

## Quality Rules
- Do not include introductory phrases, apologies, or offers to help
- Do not wrap content in markdown code blocks
- Write facts as declarative statements, not instructions
- Do not save transient state: task progress, PR numbers, commit SHAs
```

- [ ] **Step 2: Commit**

```bash
git add framework/memory/prompts/archive/agent_system.md
git commit -m "feat(memory): add ArchiveSummarizer system prompt template"
```

### Task 8: ArchiveSummarizer agent implementation

**Files:**
- Create: `framework/agents/summarizer/archive_agent.py`
- Create: `tests/unit/memory/test_archive_agent.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/unit/memory/test_archive_agent.py
"""Tests for ArchiveSummarizer — archive generation agent."""
from __future__ import annotations

import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from framework.agents.summarizer.archive_agent import ArchiveSummarizer


@pytest.fixture
def mock_provider() -> AsyncMock:
    provider = AsyncMock()
    response = MagicMock()
    response.content = ""
    response.tool_calls = []
    response.finish_reason = "stop"
    provider.chat.return_value = response
    return provider


@pytest.fixture
def archive_dir(tmp_path: Path) -> Path:
    d = tmp_path / "archive" / "42"
    d.mkdir(parents=True)
    return d


def test_default_config() -> None:
    config = ArchiveSummarizer.default_config()
    assert config.context_max_chars == 500
    assert config.knowledge_max_chars == 600
    assert config.index_max_chars == 100
    assert config.max_iterations == 20


def test_build_system_prompt(archive_dir: Path) -> None:
    prompt = ArchiveSummarizer.build_system_prompt(
        archive_dir=archive_dir,
        context_max_chars=500,
        knowledge_max_chars=600,
        index_max_chars=100,
    )
    assert str(archive_dir) in prompt
    assert "context.md" in prompt
    assert "knowledge.md" in prompt
    assert "index.md" in prompt
    assert "500" in prompt


def test_build_tools(archive_dir: Path) -> None:
    tools = ArchiveSummarizer.build_tools(archive_dir=archive_dir)
    assert len(tools) == 4
    names = {t.name for t in tools}
    assert names == {"read_file", "write_file", "edit_file", "list_dir"}


@pytest.mark.asyncio
async def test_format_transcript() -> None:
    messages = [
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "Hi there"},
    ]
    text = ArchiveSummarizer.format_transcript(messages)
    assert "Hello" in text
    assert "Hi there" in text
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/unit/memory/test_archive_agent.py -v`
Expected: FAIL

- [ ] **Step 3: Implement ArchiveSummarizer**

```python
# framework/agents/summarizer/archive_agent.py
"""ArchiveSummarizer — generates context.md, knowledge.md, index.md via ReAct agent."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from framework.agents.summarizer.agent import SummarizerAgent
from framework.core.agent import AgentContext
from framework.core.emitter import AgentResult, ContentEmitter
from framework.core.tool_manager import InMemoryToolManager, ToolManagerConfig
from framework.memory.tools import (
    ScopedEditFileTool,
    ScopedListTool,
    ScopedReadFileTool,
    ScopedWriteFileTool,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ArchiveSummarizerConfig:
    """Configuration for ArchiveSummarizer."""
    context_max_chars: int = 500
    knowledge_max_chars: int = 600
    index_max_chars: int = 100
    max_iterations: int = 20


class ArchiveSummarizer:
    """Generates archive MD files (context.md, knowledge.md, index.md) using ReAct agent.

    Unlike SummarizerAgent's single-shot LLM call, this agent uses the ReAct engine
    with scoped file tools to write output files directly.
    """

    def __init__(
        self,
        provider: Any,
        config: ArchiveSummarizerConfig | None = None,
    ) -> None:
        self._provider = provider
        self._config = config or ArchiveSummarizerConfig()
        self._react_agent = SummarizerAgent(provider)

    @staticmethod
    def default_config() -> ArchiveSummarizerConfig:
        return ArchiveSummarizerConfig()

    @staticmethod
    def build_tools(archive_dir: Path) -> list:
        """Build scoped file tools for the given archive directory only."""
        resolved = archive_dir.resolve()
        return [
            ScopedReadFileTool(allowed_dirs=[resolved]),
            ScopedWriteFileTool(allowed_dirs=[resolved]),
            ScopedEditFileTool(allowed_dirs=[resolved]),
            ScopedListTool(allowed_dirs=[resolved]),
        ]
        # Note: session_dir is NOT in allowed_dirs — framework manages session.jsonl

    @staticmethod
    def build_system_prompt(
        archive_dir: Path,
        context_max_chars: int = 500,
        knowledge_max_chars: int = 600,
        index_max_chars: int = 100,
    ) -> str:
        """Build system prompt with allowed dirs and size constraints."""
        from framework.memory.prompts import create_default_registry

        try:
            registry = create_default_registry()
            base = registry.get_system(
                "archive/agent",
                context_max_chars=str(context_max_chars),
                knowledge_max_chars=str(knowledge_max_chars),
                index_max_chars=str(index_max_chars),
            )
        except Exception:
            base = ""

        if not base:
            base = _FALLBACK_SYSTEM_PROMPT.format(
                context_max_chars=context_max_chars,
                knowledge_max_chars=knowledge_max_chars,
                index_max_chars=index_max_chars,
            )

        allowed_section = f"\n\n## Allowed Directories\n  - {archive_dir.resolve()}"
        return base + allowed_section

    @staticmethod
    def format_transcript(messages: Sequence[dict[str, Any]]) -> str:
        """Format pruned messages into a readable transcript."""
        lines: list[str] = []
        for msg in messages:
            role = msg.get("role", "unknown")
            content = msg.get("content", "")
            if not content and not msg.get("tool_calls"):
                continue
            if role == "assistant" and msg.get("tool_calls"):
                tool_names = []
                for tc in msg.get("tool_calls", []):
                    fn = tc.get("function", {}) if isinstance(tc, dict) else {}
                    tool_names.append(fn.get("name", "?"))
                if content:
                    lines.append(f"[assistant -> tools: {', '.join(tool_names)}] {content}")
                else:
                    lines.append(f"[assistant -> tools: {', '.join(tool_names)}]")
                continue
            if role == "tool":
                name = msg.get("name", "unknown")
                if isinstance(content, str) and len(content) > 300:
                    content = content[:300] + "..."
                lines.append(f"[tool:{name}] {content}")
                continue
            if content:
                lines.append(f"[{role}] {content}")
        return "\n".join(lines)

    async def generate(
        self,
        pruned_messages: Sequence[dict[str, Any]],
        archive_dir: Path,
    ) -> AgentResult:
        """Run the archive agent to generate three MD files.

        Args:
            pruned_messages: Messages trimmed by cleanup.
            archive_dir: Target directory for context.md, knowledge.md, index.md.

        Returns:
            AgentResult from the ReAct execution.
        """
        archive_dir.mkdir(parents=True, exist_ok=True)
        transcript = self.format_transcript(pruned_messages)
        if not transcript.strip():
            # Nothing to archive — write empty files
            for fname in ("context.md", "knowledge.md", "index.md"):
                (archive_dir / fname).write_text("", encoding="utf-8")
            return AgentResult(content="", stop_reason="completed")

        system_prompt = self.build_system_prompt(
            archive_dir=archive_dir,
            context_max_chars=self._config.context_max_chars,
            knowledge_max_chars=self._config.knowledge_max_chars,
            index_max_chars=self._config.index_max_chars,
        )

        tools = self.build_tools(archive_dir)
        tool_manager = InMemoryToolManager(config=ToolManagerConfig(max_workers=4))
        for tool in tools:
            tool_manager.register(tool)

        # Use SummarizerAgent's run method with AgentContext
        from framework.core.agent import AgentContext
        from framework.memory.history import ListMessageHistory

        history = ListMessageHistory([
            {"role": "user", "content": f"## Conversation Transcript\n\n{transcript}"},
        ])

        context = AgentContext(
            system_prompt=system_prompt,
            history=history,
            tool_manager=tool_manager,
            max_iterations=self._config.max_iterations,
            temperature=0.3,
        )

        # Simple emitter that collects results
        class _Collector:
            def __init__(self) -> None:
                self.content = ""
            async def emit(self, event: Any, data: Any = None) -> None:
                if data and isinstance(data, str):
                    self.content += data
            async def emit_content(self, content: str) -> None:
                self.content += content
            async def emit_stream_end(self, resuming: bool = False) -> None:
                pass

        emitter: ContentEmitter = _Collector()  # type: ignore
        return await self._react_agent.run(context, emitter)


_FALLBACK_SYSTEM_PROMPT = """\
You are an archive summarization agent. Analyze the conversation transcript and write three files.

## Output Files

### context.md
Conversation summary. Include: Situation, Decisions, Completed Work, Open Threads, Evidence.
Max {context_max_chars} characters. Write empty file if no useful context.

### knowledge.md
Durable memory candidates. Include: User Facts, Project Facts, Decisions, Reusable Lessons.
Max {knowledge_max_chars} characters. Write empty file if no durable candidates.

### index.md
Ultra-concise index entry for the pruned catalog. 1-3 lines, max {index_max_chars} characters.

## Execution Rules
- SINGLE-TURN task. Write all files, then stop.
- No further user input will follow.
- Write all three files using tools, then stop.
- If no useful content, write empty file.
- Work fast — minimize tool call rounds.
"""
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/unit/memory/test_archive_agent.py -v`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add framework/agents/summarizer/archive_agent.py tests/unit/memory/test_archive_agent.py framework/memory/prompts/archive/agent_system.md
git commit -m "feat(memory): add ArchiveSummarizer agent with scoped tools"
```

---

## Phase 4: Cleanup Flow Refactor

### Task 9: Refactor cleanup_session to four-step flow

**Files:**
- Modify: `framework/memory/cleanup.py`
- Modify: `tests/unit/memory/test_cleanup.py`

- [ ] **Step 1: Read current cleanup.py to understand the exact structure**

Read: `framework/memory/cleanup.py` — understand the full function signature and flow.

- [ ] **Step 2: Add `archive_agent` parameter to cleanup_session**

In `framework/memory/cleanup.py`, add `archive_agent: ArchiveSummarizer | None = None` parameter to the `cleanup_session` function signature. Insert the four-step flow between the boundary computation and the archive JSONL step.

Key changes:
1. When `archive_agent is not None`, compute `next_archive_id` from state but do NOT write it.
2. Check if `archive_dir / str(next_archive_id)` is complete — skip if yes.
3. Run `archive_agent.generate(pruned_messages, archive_dir)`.
4. After session commit, call `_pruned_full_refresh`.
5. Finally increment `next_archive_id` in state.

When `archive_agent is None`, the existing JSONL archive path runs unchanged.

- [ ] **Step 3: Write tests for the new four-step flow**

Add tests in `tests/unit/memory/test_cleanup.py`:
- `test_cleanup_with_archive_agent_generates_md_files`
- `test_cleanup_archive_agent_failure_falls_back_to_no_archive`
- `test_cleanup_archive_id_only_increments_on_success`
- `test_cleanup_skips_agent_if_archive_dir_complete`

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/unit/memory/test_cleanup.py -v --timeout=30`

- [ ] **Step 5: Commit**

```bash
git add framework/memory/cleanup.py tests/unit/memory/test_cleanup.py
git commit -m "refactor(memory): cleanup_session four-step flow with archive_agent"
```

### Task 10: PrunedManager full refresh from index.md

**Files:**
- Modify: `framework/memory/pruned/manager.py`
- Modify: `tests/unit/memory/pruned/test_manager.py`

- [ ] **Step 1: Add `refresh_from_archives` method to PrunedManager**

This method traverses all archive directories, reads index.md files, and rebuilds the pruned index.

```python
async def refresh_from_archives(
    self,
    archive_storage: Any,  # DirArchiveStorage
    *,
    session_id: str = "",
) -> int:
    """Full refresh pruned index from all archive index.md files.

    Returns number of index entries loaded.
    """
    archive_ids = await archive_storage.list_archives()
    entries: list[PrunedIndexEntry] = []
    storage = self._get_storage(session_id)

    for aid in archive_ids:
        content = await archive_storage.read_archive_file(aid, "index.md")
        if not content or not content.strip():
            continue
        # Parse index.md: each non-empty line is a topic description
        lines = [line.strip() for line in content.strip().splitlines() if line.strip()]
        if not lines:
            continue
        topic = lines[0][:self._topic_max]
        from datetime import datetime
        now = datetime.now()
        entry = PrunedIndexEntry(
            id=aid,
            cleanup_time=int(now.timestamp()),
            cleanup_time_display=now.strftime("%Y-%m-%d %H:%M"),
            message_count=0,
            content_filename=f"archive/{aid}/context.md",
            start_time=0,
            end_time=0,
            start_time_display="",
            end_time_display="",
            topic=topic,
        )
        entries.append(entry)

    # Overwrite index with archive-sourced entries
    storage.save_index(entries)
    return len(entries)
```

- [ ] **Step 2: Write tests**

Add tests for `refresh_from_archives`:
- `test_refresh_from_archives_reads_index_md`
- `test_refresh_from_archives_skips_empty_index`
- `test_refresh_from_archives_replaces_existing_index`

- [ ] **Step 3: Run tests**

Run: `python -m pytest tests/unit/memory/pruned/test_manager.py -v`

- [ ] **Step 4: Commit**

```bash
git add framework/memory/pruned/manager.py tests/unit/memory/pruned/test_manager.py
git commit -m "feat(memory): PrunedManager refresh_from_archives from index.md"
```

---

## Phase 5: Injection Layer Changes

### Task 11: Update _inject_archive to read MD files

**Files:**
- Modify: `framework/memory/injection/full_injection.py`
- Modify: `tests/unit/memory/test_bot_project_memory_pipeline.py`

- [ ] **Step 1: Read current `_inject_archive` implementation**

Read: `framework/memory/injection/full_injection.py:165-239`

- [ ] **Step 2: Modify `_inject_archive` to support MD-based reading**

Add a new path that checks if `memory_system` provides `DirArchiveStorage`. If so, read context.md files directly. Otherwise fall back to existing JSONL path.

Key changes:
- Read `archive_storage.list_archives(limit=N)` to get recent IDs
- For each ID, `read_archive_file(id, "context.md")`
- Truncate each to 150 chars
- When truncated, add `file` attribute with full path to context.md
- Build same `<historical_context>` XML structure

- [ ] **Step 3: Write tests**

- `test_inject_archive_reads_md_files`
- `test_inject_archive_truncates_and_shows_path`
- `test_inject_archive_falls_back_to_jsonl`

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/unit/memory/test_bot_project_memory_pipeline.py -v --timeout=30`

- [ ] **Step 5: Commit**

```bash
git add framework/memory/injection/full_injection.py tests/unit/memory/test_bot_project_memory_pipeline.py
git commit -m "feat(memory): inject archive from MD files with truncation"
```

---

## Phase 6: KnowledgeConsolidator Agent

### Task 12: Knowledge Consolidator prompt template

**Files:**
- Create: `framework/memory/prompts/knowledge/consolidator_system.md`

- [ ] **Step 1: Write the system prompt**

Create `framework/memory/prompts/knowledge/consolidator_system.md` with the content from the design spec Section 5 system prompt.

- [ ] **Step 2: Commit**

```bash
git add framework/memory/prompts/knowledge/consolidator_system.md
git commit -m "feat(memory): add KnowledgeConsolidator system prompt"
```

### Task 13: KnowledgeConsolidator implementation

**Files:**
- Create: `framework/agents/summarizer/consolidator.py`
- Create: `tests/unit/memory/test_consolidator.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/unit/memory/test_consolidator.py
"""Tests for KnowledgeConsolidator."""
from __future__ import annotations

import pytest
from pathlib import Path

from framework.agents.summarizer.consolidator import KnowledgeConsolidator


def test_build_tools_read_archive_write_knowledge(tmp_path: Path) -> None:
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    knowledge_dir = tmp_path / "knowledge"
    knowledge_dir.mkdir()

    tools = KnowledgeConsolidator.build_tools(
        archive_dirs=[archive_dir],
        knowledge_dir=knowledge_dir,
    )
    names = {t.name for t in tools}
    assert names == {"read_file", "write_file", "edit_file", "list_dir"}

    # Verify read tool can access both dirs
    read_tool = next(t for t in tools if t.name == "read_file")
    assert read_tool._allowed_dirs == [archive_dir.resolve(), knowledge_dir.resolve()]

    # Verify write tool can only access knowledge dir
    write_tool = next(t for t in tools if t.name == "write_file")
    assert write_tool._allowed_dirs == [knowledge_dir.resolve()]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/unit/memory/test_consolidator.py -v`
Expected: FAIL

- [ ] **Step 3: Implement KnowledgeConsolidator**

```python
# framework/agents/summarizer/consolidator.py
"""KnowledgeConsolidator — updates knowledge files from archive knowledge.md."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from framework.agents.summarizer.agent import SummarizerAgent
from framework.core.agent import AgentContext
from framework.core.emitter import AgentResult, ContentEmitter
from framework.core.tool_manager import InMemoryToolManager, ToolManagerConfig
from framework.memory.tools import (
    ScopedEditFileTool,
    ScopedListTool,
    ScopedReadFileTool,
    ScopedWriteFileTool,
)

logger = logging.getLogger(__name__)


class KnowledgeConsolidator:
    """Updates knowledge files (SOUL.md, USER.md, MEMORY.md) by reading
    archive knowledge.md files using a ReAct agent with scoped tools.
    """

    def __init__(
        self,
        provider: Any,
        max_iterations: int = 20,
    ) -> None:
        self._provider = provider
        self._max_iterations = max_iterations
        self._react_agent = SummarizerAgent(provider)

    @staticmethod
    def build_tools(
        archive_dirs: list[Path],
        knowledge_dir: Path,
    ) -> list:
        """Build scoped tools: read archive + knowledge, write knowledge only."""
        resolved_archives = [d.resolve() for d in archive_dirs]
        resolved_knowledge = knowledge_dir.resolve()
        return [
            ScopedReadFileTool(allowed_dirs=resolved_archives + [resolved_knowledge]),
            ScopedWriteFileTool(allowed_dirs=[resolved_knowledge]),
            ScopedEditFileTool(allowed_dirs=[resolved_knowledge]),
            ScopedListTool(allowed_dirs=resolved_archives + [resolved_knowledge]),
        ]

    def build_system_prompt(
        self,
        archive_ids: list[int],
        knowledge_dir: Path,
        archive_base: Path,
    ) -> str:
        from framework.memory.prompts import create_default_registry

        archive_files: list[str] = []
        for aid in archive_ids:
            archive_files.append(f"  - {archive_base / str(aid) / 'knowledge.md'}")

        try:
            registry = create_default_registry()
            base = registry.get_system("knowledge/consolidator")
        except Exception:
            base = ""

        if not base:
            base = _FALLBACK_PROMPT

        allowed_section = (
            f"\n\n## Allowed Directories\n"
            f"  - {archive_base.resolve()}\n"
            f"  - {knowledge_dir.resolve()}"
        )
        files_section = "\n\n## Available Archive Files\n" + "\n".join(archive_files)
        return base + allowed_section + files_section

    async def consolidate(
        self,
        archive_ids: list[int],
        archive_base: Path,
        knowledge_dir: Path,
    ) -> AgentResult:
        """Run the consolidator agent.

        Args:
            archive_ids: Unprocessed archive IDs to read.
            archive_base: Base directory containing archive/{id}/ directories.
            knowledge_dir: Directory containing SOUL.md, USER.md, MEMORY.md.
        """
        if not archive_ids:
            return AgentResult(content="", stop_reason="completed")

        archive_dirs = [archive_base / str(aid) for aid in archive_ids]
        system_prompt = self.build_system_prompt(archive_ids, knowledge_dir, archive_base)
        tools = self.build_tools(archive_dirs, knowledge_dir)

        tool_manager = InMemoryToolManager(config=ToolManagerConfig(max_workers=4))
        for tool in tools:
            tool_manager.register(tool)

        from framework.memory.history import ListMessageHistory

        # List knowledge files as context
        knowledge_files = []
        for fname in ["SOUL.md", "USER.md", "MEMORY.md"]:
            fpath = knowledge_dir / fname
            if fpath.exists():
                content = fpath.read_text(encoding="utf-8")
                knowledge_files.append(f"## Current {fname}\n{content[:500]}...")
            else:
                knowledge_files.append(f"## Current {fname}\n(empty)")

        user_msg = (
            "Analyze the archive knowledge files and update knowledge files.\n\n"
            + "\n\n".join(knowledge_files)
        )

        context = AgentContext(
            system_prompt=system_prompt,
            history=ListMessageHistory([{"role": "user", "content": user_msg}]),
            tool_manager=tool_manager,
            max_iterations=self._max_iterations,
            temperature=0.2,
        )

        class _Collector:
            def __init__(self) -> None:
                self.content = ""
            async def emit(self, event: Any, data: Any = None) -> None:
                pass
            async def emit_content(self, content: str) -> None:
                self.content += content
            async def emit_stream_end(self, resuming: bool = False) -> None:
                pass

        return await self._react_agent.run(context, _Collector())  # type: ignore


_FALLBACK_PROMPT = """\
You are a knowledge consolidation agent. Read archive knowledge files and update knowledge files.

## Task
Read the knowledge archive files, analyze them, and update SOUL.md, USER.md, MEMORY.md.

## Execution Rules
- SINGLE-TURN task. Read archives, analyze, update knowledge, then stop.
- Use edit for small changes, write for full replacement.
- Preserve existing knowledge unless contradicted.
- Remove stale/redundant content.
- Max iterations: 20.

## File Size Constraints
- SOUL.md: max 4096 chars
- USER.md: max 4096 chars
- MEMORY.md: max 8192 chars

## Knowledge Files
- SOUL.md: agent identity, core principles — rarely changes
- USER.md: user profile, preferences
- MEMORY.md: long-term facts, project context

## What to capture
1. USER CORRECTIONS
2. USER PREFERENCES
3. DESIGN DECISIONS
4. SOLUTIONS
5. PERSONALITY/BEHAVIOR

## What to skip
- Code patterns derivable from source
- Git history, commit SHAs
- Tool invocation details
- Temporary errors
- Anything already in current files
"""
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/unit/memory/test_consolidator.py -v`
Expected: 1 passed

- [ ] **Step 5: Commit**

```bash
git add framework/agents/summarizer/consolidator.py tests/unit/memory/test_consolidator.py framework/memory/prompts/knowledge/consolidator_system.md
git commit -m "feat(memory): add KnowledgeConsolidator agent"
```

### Task 14: Update DreamEngine to use KnowledgeConsolidator

**Files:**
- Modify: `framework/memory/consolidation/dream_engine.py`

- [ ] **Step 1: Read current DreamEngine.run() implementation**

Read: `framework/memory/consolidation/dream_engine.py:98-188`

- [ ] **Step 2: Add consolidator parameter to DreamEngine.__init__**

Add `consolidator: KnowledgeConsolidator | None = None` parameter. When present, `run()` uses the consolidator agent instead of the two-phase LLM path.

The flow becomes:
1. Get unprocessed archive IDs (from KNOWLEDGE channel)
2. Resolve archive_base and knowledge_dir paths
3. Call `consolidator.consolidate(archive_ids, archive_base, knowledge_dir)`
4. `commit_cursor` + `prune_consumed_pairs` as before

When `consolidator is None`, the existing `self.consolidate()` path runs unchanged.

- [ ] **Step 3: Commit**

```bash
git add framework/memory/consolidation/dream_engine.py
git commit -m "feat(memory): DreamEngine uses KnowledgeConsolidator when available"
```

---

## Phase 7: Wiring & Configuration

### Task 15: Add SummarizerConfig to memory configuration

**Files:**
- Modify: `framework/ioc/configs/memory.py`

- [ ] **Step 1: Add SummarizerConfig**

```python
class SummarizerAgentConfig(BaseModel):
    """Configuration for the summarizer-as-agent memory system."""

    enabled: bool = False
    context_max_chars: int = 500
    knowledge_max_chars: int = 600
    index_max_chars: int = 100
    max_iterations: int = 20
```

Add `summarizer_agent: SummarizerAgentConfig | None = None` to `MemoryConfig`.

- [ ] **Step 2: Commit**

```bash
git add framework/ioc/configs/memory.py
git commit -m "feat(config): add SummarizerAgentConfig to MemoryConfig"
```

### Task 16: Wire agents in create_memory_system

**Files:**
- Modify: `framework/memory/system.py`
- Modify: `framework/ioc/factories/memory.py`

- [ ] **Step 1: Update `create_memory_system` to build ArchiveSummarizer**

In `framework/memory/system.py`, when `llm_provider` is provided and archive is enabled, construct `ArchiveSummarizer(llm_provider)` and pass it through to `DefaultMemorySystem`.

- [ ] **Step 2: Update `create_memory` factory**

In `framework/ioc/factories/memory.py`, check `cfg.summarizer_agent` config. If enabled and `llm_provider` is present, construct `ArchiveSummarizer` and `KnowledgeConsolidator`.

- [ ] **Step 3: Commit**

```bash
git add framework/memory/system.py framework/ioc/factories/memory.py
git commit -m "feat(memory): wire ArchiveSummarizer and KnowledgeConsolidator in factories"
```

### Task 17: Update bot_project workspace rebuild

**Files:**
- Modify: `examples/bot_project/bot/service/core.py`

- [ ] **Step 1: Verify workspace rebuild creates new agents**

The existing `_rebuild_pipeline_memory` and `_rebuild_pool_memory` call `create_memory` which now constructs `ArchiveSummarizer` and `KnowledgeConsolidator`. Verify that:
1. The `workspace_context.data_dir` flows through to memory creation
2. `DirArchiveStorage.base_dir` is derived from `data_dir / "memory" / {agent} / "archive" / {scope}`
3. No hardcoded paths

- [ ] **Step 2: Update any bot_config.yml if needed**

Check `examples/bot_project/config/bot_config.yml` for any archive/knowledge config that needs updating for the new system.

- [ ] **Step 3: Commit**

```bash
git add examples/bot_project/
git commit -m "chore(bot): adapt workspace rebuild for summarizer agent memory"
```

---

## Phase 8: Integration & Migration

### Task 18: Integration test — full pipeline

**Files:**
- Create: `tests/integration/memory/test_summarizer_agent_pipeline.py`

- [ ] **Step 1: Write integration test**

Test the full flow:
1. Create messages exceeding `max_messages`
2. Run `cleanup_session` with `archive_agent`
3. Verify `archive/1/context.md`, `knowledge.md`, `index.md` exist
4. Verify pruned index refreshed
5. Verify session trimmed
6. Verify archive_id incremented in state
7. Verify injection reads MD files

- [ ] **Step 2: Run integration test**

Run: `python -m pytest tests/integration/memory/test_summarizer_agent_pipeline.py -v --timeout=60`

- [ ] **Step 3: Commit**

```bash
git add tests/integration/memory/test_summarizer_agent_pipeline.py
git commit -m "test(memory): integration test for summarizer agent pipeline"
```

### Task 19: Run full test suite

- [ ] **Step 1: Run all memory tests**

Run: `python -m pytest tests/unit/memory/ tests/integration/memory/ -v --timeout=30`
Expected: All tests pass

- [ ] **Step 2: Run all framework tests**

Run: `python -m pytest tests/ -v --timeout=30 -x`
Expected: All tests pass

- [ ] **Step 3: Fix any regressions**

Address any test failures from the new code.

- [ ] **Step 4: Final commit**

```bash
git add -A
git commit -m "test(memory): verify full test suite passes with summarizer agent"
```

---

## Summarizer Session Storage

Summarizer agents have their **own isolated session storage**, completely separate from the multi-level memory system. NOT stored inside archive/knowledge/session layers. Only the summarizer itself can read/write this area.

### ArchiveSummarizer Session

```
{data_dir}/summarizer/{archive_id}/
  session.jsonl           ← ReAct session log (tool calls + LLM responses)
```

This is a peer of `memory/`, `runtime_state/`, etc. — NOT nested inside the memory hierarchy. Path derived from `data_dir` (same as `MODEX_DATA_DIR`), so workspace switching (cd/exit) naturally redirects.

**IMPORTANT**: The agent's scoped tools do NOT include this directory. Session.jsonl is written by framework code wrapping the agent execution — the agent is unaware of it and cannot read/write it.

The summarizer's scoped tools only access:
- `archive/{scope}/{archive_id}/` — to write output files (context.md, knowledge.md, index.md)

### KnowledgeConsolidator Session

```
{data_dir}/consolidator/{cursor}/
  session.jsonl           ← ReAct session log keyed by dream cursor position
```

Same pattern — framework-managed only, agent cannot access.

### Implementation

In `ArchiveSummarizer.generate()`:
- Framework creates `{data_dir}/summarizer/{archive_id}/` directory
- A `_SessionLogger` wrapper intercepts tool calls and LLM responses, appends to session.jsonl
- On completion, a final `{"status": "completed"}` entry is written
- On error, a `{"status": "error", "message": "..."}` entry is written
- ScopedFileTools `allowed_dirs` does NOT include session dir

In `DirArchiveStorage.is_archive_complete()`:
- Only checks output files (context.md, knowledge.md, index.md)
- Session log is checked by framework independently for retry decisions

---

## Dependency Graph

```
Phase 1 (ScopedFileTools)
  Task 1: ScopedReadFileTool
  Task 2: ScopedWriteFileTool     ── no deps
  Task 3: ScopedEditFileTool      ── no deps
  Task 4: ScopedListTool          ── no deps
  Task 5: Phase 1 integration     ── depends on Tasks 1-4

Phase 2 (DirArchiveStorage)
  Task 6: DirArchiveStorage       ── no deps

Phase 3 (ArchiveSummarizer)
  Task 7: Prompt templates        ── no deps
  Task 8: ArchiveSummarizer       ── depends on Phase 1 + Task 6

Phase 4 (Cleanup Refactor)
  Task 9:  cleanup_session        ── depends on Task 8
  Task 10: PrunedManager refresh  ── depends on Task 6

Phase 5 (Injection)
  Task 11: _inject_archive        ── depends on Task 6

Phase 6 (KnowledgeConsolidator)
  Task 12: Prompt template        ── no deps
  Task 13: KnowledgeConsolidator  ── depends on Phase 1
  Task 14: DreamEngine update     ── depends on Task 13

Phase 7 (Wiring)
  Task 15: Config                 ── depends on Task 8, 13
  Task 16: Factories              ── depends on Task 15
  Task 17: bot_project            ── depends on Task 16

Phase 8 (Integration)
  Task 18: Integration test       ── depends on all above
  Task 19: Full suite             ── depends on Task 18
```

Parallelizable tasks within phases:
- Tasks 1-4 can run in parallel
- Tasks 7 and 12 can run in parallel
- Tasks 10 and 11 can run in parallel
